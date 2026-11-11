#!/usr/bin/env python3
"""Check/generate independent Pearl preprocessing and arithmetic fixtures.

Requires the official Python BLAKE3 binding pinned in requirements-fixtures.txt.
All other computation is Python standard-library scalar integer arithmetic.
The generator never imports/calls the native implementation, CPPminer, numpy,
or a candidate output. No arguments means --check; only --write changes files.
These are synthetic offline jobs, not proof, pool, or GPU execution evidence.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import importlib.metadata
import itertools
import json
from operator import mul
from pathlib import Path
import struct
import sys


ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / "native" / "fixtures"
BLAKE3_VERSION = "1.0.11"
UPSTREAM_COMMIT = "5b68506af5e6433ee5c25aa9e6fbb00b537590aa"
K = 4096
RANK = 128
MILESTONES = K // RANK
TILE_ROWS, TILE_COLS = 8, 16
MASK32, MASK64 = (1 << 32) - 1, (1 << 64) - 1
HEADER = bytes((i * 7 + 11) & 255 for i in range(76))
SEED = bytes((i * i * 13 + i * 17 + 29) & 255 for i in range(32))
CONFIG = struct.pack("<II", K, RANK) + bytes((0, 7, 0, 0, 0, 0, 0, 15)) + bytes(36)
LABEL_A = b"A_tensor" + bytes(24)
LABEL_B = b"B_tensor" + bytes(24)


@dataclass(frozen=True)
class Case:
    name: str
    rows: int
    cols: int
    cert_version: int


CASES = (
    Case("legacy_zero_b", 128, 128, 2),
    Case("v3_zero_b", 128, 128, 3),
    Case("v3_rectangular_zero_b", 128, 256, 3),
)


def load_blake3():
    try:
        from blake3 import blake3
        version = importlib.metadata.version("blake3")
    except (ImportError, importlib.metadata.PackageNotFoundError) as error:
        raise RuntimeError(
            "Install the pinned fixture dependency in an isolated environment: "
            "python -m pip install --require-hashes -r requirements-fixtures.txt"
        ) from error
    if version != BLAKE3_VERSION:
        raise RuntimeError(f"Expected blake3 {BLAKE3_VERSION}; found {version}")
    return blake3


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def splitmix64(value: int) -> int:
    value = (value + 0x9E3779B97F4A7C15) & MASK64
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
    value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & MASK64
    return value ^ (value >> 31)


def fold_seed(seed: bytes) -> int:
    result = 0
    for index, byte in enumerate(seed):
        result ^= byte << ((index % 8) * 8)
    return result


def signal_matrices(case: Case) -> tuple[bytes, bytes, int]:
    a = bytearray(case.rows * K)
    bt = bytes(case.cols * K)
    rng = fold_seed(SEED)
    changed_row = splitmix64(rng ^ 0xD1B54A32D192ED03) % case.rows
    row_start = changed_row * K
    for k in range(K):
        random_word = splitmix64(rng ^ (((row_start + k) * 0x9E3779B97F4A7C15) & MASK64))
        a[row_start + k] = (((random_word >> 32) % 128) - 64) & 255
    return bytes(a), bt, changed_row


def random_hash(blake3, index: int, label: bytes, key: bytes, for_pairs: bool) -> bytes:
    # The counter occupies bytes 4..7 for pairs, bytes 0..3 for uniform noise.
    message = bytearray(64)
    struct.pack_into("<I", message, 4 if for_pairs else 0, index + 1)
    message[32:] = label
    return blake3(message, key=key).digest()


def permutation_pairs(blake3, label: bytes, key: bytes) -> list[tuple[int, int]]:
    pairs = []
    for block in range(K // 8):
        words = struct.unpack("<8I", random_hash(blake3, block, label, key, True))
        for word in words:
            first = word & (RANK - 1)
            second = first ^ (1 + (((RANK - 1) * word) >> 32))
            if not (0 <= first < RANK and 0 <= second < RANK and first != second):
                raise ArithmeticError("Permutation pair outside the fixed rank")
            pairs.append((first, second))
    return pairs


def noisy_matrix(blake3, signal: bytes, row_count: int, label: bytes, key: bytes,
                 pairs: list[tuple[int, int]]) -> tuple[list[list[int]], bytes]:
    rows = []
    for row in range(row_count):
        uniform = []
        first_block = row * RANK // 32
        for block in range(first_block, first_block + RANK // 32):
            uniform.extend((byte & 63) - 16 for byte in random_hash(blake3, block, label, key, False))
        source = signal[row * K:(row + 1) * K]
        noisy = [
            (source[k] if source[k] < 128 else source[k] - 256) + uniform[pos] - uniform[neg]
            for k, (pos, neg) in enumerate(pairs)
        ]
        if any(value < -128 or value > 127 for value in noisy):
            raise ArithmeticError("Noisy signal exceeds INT8 range")
        rows.append(noisy)
    raw = bytes(value & 255 for value in itertools.chain.from_iterable(rows))
    return rows, raw


def scalar_arithmetic(case: Case, a: list[list[int]], bt: list[list[int]]) -> tuple[list[int], list[int]]:
    """Dense O(M*N*K), retaining every cumulative INT32 milestone.

    Slicing each row into 128-element chunks once avoids repeated slicing in
    the cell loop. sum(map(mul,...)) is scalar exact Python integer arithmetic;
    no array/matmul library or native candidate participates in expected data.
    """
    a_blocks = [[row[start:start + RANK] for start in range(0, K, RANK)] for row in a]
    bt_blocks = [[row[start:start + RANK] for start in range(0, K, RANK)] for row in bt]
    tiles_per_row = case.cols // TILE_COLS
    tiles = (case.rows // TILE_ROWS) * tiles_per_row
    transcript = [0] * (MILESTONES * tiles)
    final_c = [0] * (case.rows * case.cols)
    for row in range(case.rows):
        for col in range(case.cols):
            total = 0
            tile = (row // TILE_ROWS) * tiles_per_row + col // TILE_COLS
            for milestone, (a_block, b_block) in enumerate(zip(a_blocks[row], bt_blocks[col])):
                total += sum(map(mul, a_block, b_block))
                if not -(1 << 31) <= total < (1 << 31):
                    raise ArithmeticError("Cumulative matrix value exceeds INT32")
                transcript[milestone * tiles + tile] ^= total & MASK32
            final_c[row * case.cols + col] = total
    return transcript, final_c


def fingerprint(blake3, raw: bytes, dtype: str, shape: list[int], layout: str) -> dict:
    return {
        "dtype": dtype, "shape": shape, "layout": layout,
        "byte_count": len(raw), "sha256": sha256(raw),
        "blake3": blake3(raw).hexdigest(),
    }


def make_case(blake3, case: Case, salt_a: bytes, salt_b: bytes) -> tuple[dict, list[int], dict]:
    signal_a, signal_bt, changed_row = signal_matrices(case)
    job_key = blake3(HEADER + CONFIG).digest()
    # Commit the signal bytes, before noise; round up to a 1024-byte chunk.
    padded_a = signal_a + bytes((-len(signal_a)) % 1024)
    padded_bt = signal_bt + bytes((-len(signal_bt)) % 1024)
    commitment_a = blake3(padded_a, key=job_key).digest()
    commitment_b = blake3(padded_bt, key=job_key).digest()
    if case.cert_version >= 3:
        bound_a = blake3(commitment_a + struct.pack("<I", case.rows) + bytes(28), key=salt_a).digest()
        bound_b = blake3(commitment_b + struct.pack("<I", case.cols) + bytes(28), key=salt_b).digest()
    else:
        bound_a, bound_b = commitment_a, commitment_b
    b_noise_seed = blake3(job_key + bound_b).digest()
    a_noise_seed = blake3(b_noise_seed + bound_a).digest()
    a_pairs = permutation_pairs(blake3, LABEL_A, a_noise_seed)
    b_pairs = permutation_pairs(blake3, LABEL_B, b_noise_seed)
    a_rows, noisy_a = noisy_matrix(blake3, signal_a, case.rows, LABEL_A, a_noise_seed, a_pairs)
    bt_rows, noisy_bt = noisy_matrix(blake3, signal_bt, case.cols, LABEL_B, b_noise_seed, b_pairs)
    transcript, final_c = scalar_arithmetic(case, a_rows, bt_rows)
    transcript_bytes = struct.pack(f"<{len(transcript)}I", *transcript)
    final_c_bytes = struct.pack(f"<{len(final_c)}i", *final_c)
    a_pair_bytes = struct.pack(f"<{K * 2}I", *itertools.chain.from_iterable(a_pairs))
    b_pair_bytes = struct.pack(f"<{K * 2}I", *itertools.chain.from_iterable(b_pairs))
    digests = {
        "job_key": job_key, "commitment_a": commitment_a, "commitment_b": commitment_b,
        "bound_a": bound_a, "bound_b": bound_b,
        "b_noise_seed": b_noise_seed, "a_noise_seed": a_noise_seed,
        "signal_a_hash": blake3(signal_a).digest(), "signal_bt_hash": blake3(signal_bt).digest(),
        "noisy_a_hash": blake3(noisy_a).digest(), "noisy_bt_hash": blake3(noisy_bt).digest(),
        "final_c_hash": blake3(final_c_bytes).digest(),
    }
    manifest = {
        "name": case.name,
        "shape": {"rows": case.rows, "cols": case.cols, "inner": K},
        "cert_version": case.cert_version,
        "header_hex": HEADER.hex(), "seed_hex": SEED.hex(), "changed_row": changed_row,
        "digests": {name: value.hex() for name, value in digests.items()},
        "signal_a": fingerprint(blake3, signal_a, "int8", [case.rows, K], "row_major"),
        "signal_bt": fingerprint(blake3, signal_bt, "int8", [case.cols, K], "row_major_transposed_b"),
        "a_permutation_pairs": fingerprint(blake3, a_pair_bytes, "uint32_le", [K, 2], "k_major_then_pair_index"),
        "b_permutation_pairs": fingerprint(blake3, b_pair_bytes, "uint32_le", [K, 2], "k_major_then_pair_index"),
        "noisy_a": fingerprint(blake3, noisy_a, "int8", [case.rows, K], "row_major"),
        "noisy_bt": fingerprint(blake3, noisy_bt, "int8", [case.cols, K], "row_major_transposed_b"),
        "transcript": fingerprint(blake3, transcript_bytes, "uint32_le",
            [MILESTONES, case.rows // TILE_ROWS, case.cols // TILE_COLS],
            "milestone_major_then_tile_row_then_tile_col"),
        "final_c": fingerprint(blake3, final_c_bytes, "int32_le", [case.rows, case.cols], "row_major"),
        "commitment_padding_bytes": {"a": len(padded_a) - len(signal_a), "bt": len(padded_bt) - len(signal_bt)},
    }
    return digests, transcript, manifest


def byte_array(values: bytes) -> str:
    return "{{" + ", ".join(f"0x{value:02x}" for value in values) + "}}"


def render_transcript(name: str, words: list[int]) -> str:
    lines = [f"inline constexpr std::array<std::uint32_t, {len(words)}> {name} = {{{{"]
    for start in range(0, len(words), 8):
        lines.append("    " + ", ".join(f"0x{word:08x}u" for word in words[start:start + 8]) + ",")
    lines.extend(("}};", ""))
    return "\n".join(lines)


def generate() -> tuple[bytes, bytes, dict]:
    blake3 = load_blake3()
    if len(CONFIG) != 52 or len(HEADER) != 76 or len(SEED) != 32:
        raise ValueError("Fixed job byte lengths changed")
    salt_a = blake3(b"pearl/cert-v3/noise-seed/A").digest()
    salt_b = blake3(b"pearl/cert-v3/noise-seed/B").digest()
    # Domain strings and published salt bytes are both preserved by the pin.
    if salt_a.hex() != "8249406ca0ed15169616f692fcf076f892dbdb2a7023b852f0d47719c390017b":
        raise ArithmeticError("BLAKE3 domain salt A does not match the pinned protocol")
    if salt_b.hex() != "11300632ec6301ca2be2af718b3f4d4f1ae9c63988e8cc044844301d71b89aa9":
        raise ArithmeticError("BLAKE3 domain salt B does not match the pinned protocol")
    digest_names = (
        "job_key", "commitment_a", "commitment_b", "bound_a", "bound_b",
        "b_noise_seed", "a_noise_seed", "signal_a_hash", "signal_bt_hash",
        "noisy_a_hash", "noisy_bt_hash", "final_c_hash",
    )
    header = [
        "// Generated by tools/generate_pearl_fixtures.py --write; do not hand edit.",
        "// Independent synthetic-job preprocessing/arithmetic vectors; no proof or GPU claim.",
        "#pragma once", "", '#include "neuro/pearl_input.hpp"', "",
        "#include <array>", "#include <cstddef>", "#include <cstdint>", "",
        "namespace neuro::pearl_golden {", "", "using Digest = neuro::pearl::Digest;", "",
        "static_assert(fullk::inner == 4096 && fullk::rank == 128 &&",
        "              fullk::hash_rows == 8 && fullk::hash_cols == 16,",
        '              "Pearl golden arithmetic contract mismatch");', "",
        "struct Fixture {", "    const char* name;", "    fullk::Shape shape;",
        "    int cert_version;", "    std::array<std::uint8_t, 76> header;",
        "    std::array<std::uint8_t, 32> seed;", "    int changed_row;",
        *[f"    Digest {name};" for name in digest_names],
        "    const std::uint32_t* transcript;", "    std::size_t transcript_count;", "};", "",
        f"inline constexpr std::array<std::uint8_t, 76> fixed_header = {byte_array(HEADER)};",
        f"inline constexpr std::array<std::uint8_t, 32> fixed_seed = {byte_array(SEED)};", "",
    ]
    fixture_entries, cases = [], []
    total_words = total_final = 0
    for index, case in enumerate(CASES):
        digests, transcript, metadata = make_case(blake3, case, salt_a, salt_b)
        name = f"case_{index}_transcript"
        header.append(render_transcript(name, transcript))
        entry = [
            f'    {{"{case.name}", {{{case.rows}, {case.cols}}}, {case.cert_version},',
            f"     fixed_header, fixed_seed, {metadata['changed_row']},",
            *[f"     Digest{byte_array(digests[field])}, // {field}" for field in digest_names],
            f"     {name}.data(), {name}.size()}},",
        ]
        fixture_entries.append("\n".join(entry))
        cases.append(metadata)
        total_words += len(transcript)
        total_final += case.rows * case.cols
    header.extend((
        f"inline const std::array<Fixture, {len(CASES)}> fixtures = {{{{",
        "\n".join(fixture_entries), "}};", "", "} // namespace neuro::pearl_golden", "",
    ))
    header_bytes = "\n".join(header).encode("utf-8")
    manifest = {
        "schema": "neuro.pearl_fixture_manifest.v1",
        "scope": "synthetic_job_protocol_preprocessing_and_scalar_arithmetic",
        "source": "independent Python formulas based on pinned protocol source; no native candidate calls",
        "generator": "tools/generate_pearl_fixtures.py",
        "generator_sha256": sha256(Path(__file__).read_bytes()),
        "header": "native/fixtures/pearl_golden.hpp", "header_sha256": sha256(header_bytes),
        "requirements": "requirements-fixtures.txt",
        "requirements_sha256": sha256((ROOT / "requirements-fixtures.txt").read_bytes()),
        "cryptographic_implementation": {
            "package": "blake3", "version": BLAKE3_VERSION,
            "source": "https://github.com/oconnor663/blake3-py",
            "implementation": "official Rust implementation via PyO3 binding",
            "locked_regeneration_platform": "CPython 3.11/3.12; Linux x86_64; glibc >= 2.17",
        },
        "semantic_reference": {
            "repository": "https://github.com/1640675651/CPPminer",
            "commit": UPSTREAM_COMMIT, "file": "src/common/cp_noise.c",
        },
        "gpu_execution_observed": False, "proof_verified": False,
        "pool_acceptance_observed": False, "mining_evidence": False,
        "input_strategy": "first zero-B attempt: zero A/Bt, then one A row filled using the fixed seed and splitmix64",
        "header_formula": "header[i] = (i * 7 + 11) & 255, i = 0..75",
        "seed_formula": "seed[i] = (i * i * 13 + i * 17 + 29) & 255, i = 0..31",
        "mining_config": {"cutlass_fused": False, "contiguous_tiles": True,
            "tile_shape": [8, 16], "byte_count": len(CONFIG), "hex": CONFIG.hex(), "sha256": sha256(CONFIG)},
        "domain_salts": {"a": salt_a.hex(), "b": salt_b.hex()},
        "seed_binding": "cert2 uses raw commitment roots; cert3 hashes root || dim_le32 || 28 zero bytes with the corresponding domain salt as BLAKE3 key",
        "integer_encoding": "INT8 raw two's-complement bytes; pair/transcript uint32 little-endian; final C int32 little-endian",
        "hash_modes": "commitments keyed by job_key on 1024-byte-zero-padded signal; *_hash/fingerprints unkeyed over exact raw bytes; job_key and seed chain unkeyed; cert3 root binding keyed by domain salts",
        "inner": K, "rank": RANK, "milestones": MILESTONES,
        "transcript_index": "milestone * ((rows / 8) * (cols / 16)) + tile_row * (cols / 16) + tile_col",
        "final_c_storage": "all cells computed independently; full i32LE bytes fingerprinted; not duplicated in C++ header",
        "cases": cases,
    }
    manifest_bytes = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    counts = {"cases": len(CASES), "transcript_words": total_words, "final_c_values_hashed": total_final}
    return header_bytes, manifest_bytes, counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Check generated files without rewriting (default)")
    mode.add_argument("--write", action="store_true", help="Explicitly regenerate the header and manifest")
    args = parser.parse_args(argv)
    try:
        header, manifest, counts = generate()
    except (RuntimeError, ValueError, ArithmeticError) as error:
        print(json.dumps({"schema": "neuro.pearl_fixture_check.v1", "status": "ERROR", "error": str(error)}))
        return 2
    artifacts = {DESTINATION / "pearl_golden.hpp": header, DESTINATION / "pearl_manifest.json": manifest}
    mismatches = []
    if args.write:
        DESTINATION.mkdir(parents=True, exist_ok=True)
        for path, content in artifacts.items():
            path.write_bytes(content)
    else:
        for path, expected in artifacts.items():
            try:
                actual = path.read_bytes()
            except FileNotFoundError:
                mismatches.append({"path": str(path.relative_to(ROOT)), "reason": "missing"})
                continue
            if actual != expected:
                mismatches.append({"path": str(path.relative_to(ROOT)), "reason": "content_mismatch"})
    print(json.dumps({
        "schema": "neuro.pearl_fixture_check.v1", "status": "FAIL" if mismatches else "PASS",
        "mode": "write" if args.write else "check", **counts, "mismatches": mismatches,
        "gpu_execution_observed": False, "proof_verified": False, "pool_acceptance_observed": False,
    }, sort_keys=True))
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
