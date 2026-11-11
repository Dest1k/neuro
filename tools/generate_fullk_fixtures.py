#!/usr/bin/env python3
"""Generate/check independent, synthetic full-K arithmetic golden fixtures.

Only Python's standard library is used. The oracle performs dense scalar dot
products with exact integers and checks the INT32 range at every milestone. It
does not call the C++ oracle, use GPU packing helpers, or read candidate output.
No-argument invocation checks existing files; --write explicitly regenerates.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import struct
import sys


INNER = 4096
RANK = 128
MILESTONES = INNER // RANK
TILE_ROWS = 8
TILE_COLS = 16
MASK32 = (1 << 32) - 1
ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / "native" / "fixtures"


@dataclass(frozen=True)
class Case:
    id: int
    name: str
    rows: int
    cols: int
    seed: int = 0


CASES = (
    Case(0, "zeros", 16, 16),
    Case(1, "mixed_signed", 16, 32),
    Case(2, "int7_edges", 32, 16),
    Case(3, "int8_edges", 16, 16),
    Case(4, "milestone_edges", 32, 32),
    Case(5, "coordinate_mix", 32, 48, 0x12345678),
)


def coordinate_value(row: int, k: int, seed: int, is_bt: bool) -> int:
    """Explicit uint32 mixing, with independent salts for A and transposed B."""
    x = seed ^ (0x9E3779B9 if is_bt else 0x243F6A88)
    x ^= (row * 0x85EBCA6B) & MASK32
    x ^= (k * 0xC2B2AE35) & MASK32
    x ^= x >> 16
    x = (x * 0x7FEB352D) & MASK32
    x ^= x >> 15
    x = (x * 0x846CA68B) & MASK32
    x ^= x >> 16
    return (x & 127) - 64


def input_value(case: Case, row: int, k: int, is_bt: bool) -> int:
    milestone = k // RANK
    if case.id == 0:
        return 0
    if case.id == 1:
        if is_bt:
            return (row * 29 + k * 13 + milestone * 3) % 127 - 63
        return (row * 17 + k * 11 + milestone * 7) % 127 - 64
    if case.id in (2, 3):
        edges = (-64, 63, -64, 0, 63, -1, 1, 63) if case.id == 2 else (
            -128, 127, -128, 0, 127, -1, 1, 127
        )
        index = (row * 7 + k * 5 + 3 * milestone + 1) % 8 if is_bt else (
            row * 5 + k * 3 + milestone
        ) % 8
        return edges[index]
    if case.id == 4:
        last = k % RANK == RANK - 1
        if k % RANK != 0 and not last:
            return 0
        if is_bt:
            return (row * 11 + milestone * 5 + (3 if last else 0)) % 19 - 9
        return (row * 7 + milestone * 3 + (5 if last else 0)) % 17 - 8
    if case.id == 5:
        return coordinate_value(row, k, case.seed, is_bt)
    raise ValueError(f"Unknown fixture pattern id: {case.id}")


def make_input(case: Case) -> tuple[list[int], list[int]]:
    # A: M x K. Bt: N x K; its row is the corresponding column of B.
    a = [input_value(case, row, k, False) for row in range(case.rows) for k in range(INNER)]
    bt = [input_value(case, row, k, True) for row in range(case.cols) for k in range(INNER)]
    if any(value < -128 or value > 127 for value in a + bt):
        raise ValueError("Input cannot be represented as signed INT8")
    return a, bt


def scalar_oracle(case: Case, a: list[int], bt: list[int]) -> tuple[list[int], list[int]]:
    """O(M*N*K): increment each cell once; never recompute milestone prefixes."""
    tile_cols = case.cols // TILE_COLS
    tile_count = (case.rows // TILE_ROWS) * tile_cols
    transcript = [0] * (MILESTONES * tile_count)
    final_c = [0] * (case.rows * case.cols)
    for row in range(case.rows):
        a_offset = row * INNER
        for col in range(case.cols):
            bt_offset = col * INNER
            tile = (row // TILE_ROWS) * tile_cols + col // TILE_COLS
            cumulative = 0
            for milestone in range(MILESTONES):
                start = milestone * RANK
                for k in range(start, start + RANK):
                    cumulative += a[a_offset + k] * bt[bt_offset + k]
                # Python integers are exact; these cases fit both INT64
                # intermediates and the required cumulative INT32 result.
                if not -(1 << 31) <= cumulative < (1 << 31):
                    raise ArithmeticError("Golden cumulative result exceeds signed INT32")
                transcript[milestone * tile_count + tile] ^= cumulative & MASK32
            final_c[row * case.cols + col] = cumulative
    return transcript, final_c


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pack_i8(values: list[int]) -> bytes:
    return bytes(value & 255 for value in values)


def pack_u32_le(values: list[int]) -> bytes:
    return struct.pack(f"<{len(values)}I", *values)


def pack_i32_le(values: list[int]) -> bytes:
    return struct.pack(f"<{len(values)}i", *values)


def render_array(name: str, values: list[int], unsigned: bool) -> str:
    ctype = "std::uint32_t" if unsigned else "std::int32_t"
    formatted = [f"0x{value:08x}u" for value in values] if unsigned else [str(value) for value in values]
    rows = ["    " + ", ".join(formatted[start:start + 8]) + "," for start in range(0, len(values), 8)]
    return (
        f"inline constexpr std::array<{ctype}, {len(values)}> {name} = {{{{\n"
        + "\n".join(rows) + "\n}};\n"
    )


def generate() -> tuple[bytes, bytes, dict[str, int]]:
    header = [
        "// Generated by tools/generate_fullk_fixtures.py --write; do not hand edit.",
        "// Source: synthetic arithmetic only. No Pearl proof or GPU execution evidence.",
        "#pragma once", "", "#include \"neuro/fullk.hpp\"", "",
        "#include <array>", "#include <cstddef>", "#include <cstdint>", "",
        "namespace neuro::golden {", "",
        "static_assert(fullk::inner == 4096 && fullk::rank == 128 &&",
        "              fullk::milestones == 32 && fullk::hash_rows == 8 &&",
        "              fullk::hash_cols == 16, \"Golden arithmetic contract mismatch\");", "",
        "struct Fixture {",
        "    const char* name;",
        "    fullk::Shape shape;",
        "    fullk::Pattern pattern;",
        "    std::uint32_t seed;",
        "    const std::uint32_t* transcript;",
        "    std::size_t transcript_count;",
        "    const std::int32_t* final_c;",
        "    std::size_t final_count;",
        "};", "",
    ]
    manifest_cases = []
    fixture_rows = []
    total_transcript = total_final = 0
    for case in CASES:
        if case.rows < 16 or case.cols < 16 or case.rows % 16 or case.cols % 16:
            raise ValueError("Fixture dimensions must be positive multiples of 16")
        a, bt = make_input(case)
        transcript, final_c = scalar_oracle(case, a, bt)
        t_name = f"case_{case.id}_transcript"
        c_name = f"case_{case.id}_final_c"
        header.extend((render_array(t_name, transcript, True), render_array(c_name, final_c, False)))
        fixture_rows.append(
            f"    {{\"{case.name}\", {{{case.rows}, {case.cols}}}, fullk::Pattern::{case.name}, "
            f"0x{case.seed:08x}u,\n"
            f"     {t_name}.data(), {t_name}.size(), {c_name}.data(), {c_name}.size()}},"
        )
        manifest_cases.append({
            "id": case.id,
            "name": case.name,
            "shape": {"rows": case.rows, "cols": case.cols, "inner": INNER},
            "pattern": case.name,
            "pattern_id": case.id,
            "seed_u32": case.seed,
            "a": {
                "dtype": "int8", "layout": "row_major", "shape": [case.rows, INNER],
                "byte_count": len(a), "sha256": digest(pack_i8(a)),
            },
            "bt": {
                "dtype": "int8", "layout": "row_major_transposed_b", "shape": [case.cols, INNER],
                "byte_count": len(bt), "sha256": digest(pack_i8(bt)),
            },
            "transcript": {
                "dtype": "uint32", "endianness": "little",
                "layout": "milestone_major_then_tile_row_then_tile_col",
                "shape": [MILESTONES, case.rows // TILE_ROWS, case.cols // TILE_COLS],
                "word_count": len(transcript), "byte_count": len(transcript) * 4,
                "sha256": digest(pack_u32_le(transcript)),
            },
            "final_c": {
                "dtype": "int32", "endianness": "little", "layout": "row_major",
                "shape": [case.rows, case.cols], "element_count": len(final_c),
                "byte_count": len(final_c) * 4, "sha256": digest(pack_i32_le(final_c)),
            },
        })
        total_transcript += len(transcript)
        total_final += len(final_c)
    header.extend((
        f"inline const std::array<Fixture, {len(CASES)}> fixtures = {{{{",
        "\n".join(fixture_rows), "}};", "", "} // namespace neuro::golden", "",
    ))
    header_bytes = "\n".join(header).encode("utf-8")
    manifest = {
        "schema": "neuro.fullk_fixture_manifest.v1",
        "source": "synthetic arithmetic only",
        "generator": "tools/generate_fullk_fixtures.py",
        "generator_sha256": digest(Path(__file__).read_bytes()),
        "header": "native/fixtures/fullk_golden.hpp",
        "header_sha256": digest(header_bytes),
        "oracle": "independent_python_stdlib_dense_incremental_scalar",
        "gpu_execution_observed": False,
        "mining_evidence": False,
        "proof_verified": False,
        "inner": INNER,
        "rank": RANK,
        "milestones": MILESTONES,
        "tile_shape": [TILE_ROWS, TILE_COLS],
        "transcript_index": "milestone * ((rows / 8) * (cols / 16)) + tile_row * (cols / 16) + tile_col",
        "transcript_operation": "xor uint32 representations of all 8x16 cumulative INT32 cell values after each 128 products",
        "final_c_index": "row * cols + col",
        "cases": manifest_cases,
    }
    manifest_bytes = (json.dumps(manifest, indent=2, ensure_ascii=True) + "\n").encode("utf-8")
    counts = {"cases": len(CASES), "transcript_words": total_transcript, "final_c_values": total_final}
    return header_bytes, manifest_bytes, counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Verify existing generated files without writing (default)")
    mode.add_argument("--write", action="store_true", help="Explicitly regenerate both golden files")
    args = parser.parse_args(argv)
    header, manifest, counts = generate()
    artifacts = {
        DESTINATION / "fullk_golden.hpp": header,
        DESTINATION / "fullk_manifest.json": manifest,
    }
    if args.write:
        DESTINATION.mkdir(parents=True, exist_ok=True)
        for path, content in artifacts.items():
            path.write_bytes(content)
        result = {"schema": "neuro.fullk_fixture_check.v1", "status": "PASS", "mode": "write", **counts}
        print(json.dumps(result, sort_keys=True))
        return 0
    mismatches = []
    for path, expected in artifacts.items():
        try:
            actual = path.read_bytes()
        except FileNotFoundError:
            mismatches.append({"path": str(path.relative_to(ROOT)), "reason": "missing"})
            continue
        if actual != expected:
            mismatches.append({"path": str(path.relative_to(ROOT)), "reason": "content_mismatch"})
    result = {
        "schema": "neuro.fullk_fixture_check.v1", "status": "FAIL" if mismatches else "PASS",
        "mode": "check", **counts, "mismatches": mismatches,
        "gpu_execution_observed": False, "mining_evidence": False,
    }
    print(json.dumps(result, sort_keys=True))
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
