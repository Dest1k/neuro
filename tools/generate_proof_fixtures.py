#!/usr/bin/env python3
"""Derive fixed offline jackpot/target vectors without calling a native miner.

Reuses the independently computed Python signal/noise/matrix formulas from
iteration 3. Expected values never come from the Rust prover/verifier or C++.
Checking is the default; --write explicitly changes the expected artifacts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct

import generate_pearl_fixtures as pearl

ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / "native" / "fixtures"
TARGET = 1 << 235
FACTOR = pearl.TILE_ROWS * pearl.TILE_COLS * pearl.MILESTONES * 128
MASK32 = (1 << 32) - 1
MASK256 = (1 << 256) - 1


def rotate_left(value: int, count: int) -> int:
    return ((value << count) | (value >> (32 - count))) & MASK32


def tile_record(blake3, transcript: list[int], seed: bytes, tiles: int,
                cols: int, index: int) -> dict:
    message = [0] * 16
    for milestone in range(pearl.MILESTONES):
        slot = milestone % 16
        message[slot] = rotate_left(message[slot], 13) ^ transcript[milestone * tiles + index]
    message_bytes = struct.pack("<16I", *message)
    digest = blake3(message_bytes, key=seed).digest()
    value = int.from_bytes(digest, "little")
    # Smallest unscaled target whose scaled bound accepts this exact hash.
    accept = (value + FACTOR - 1) // FACTOR
    if not 0 < accept <= MASK256 // FACTOR:
        raise ValueError("Fixture hash has no representable neighboring targets")
    return {
        "row": index // (cols // pearl.TILE_COLS) * pearl.TILE_ROWS,
        "col": index % (cols // pearl.TILE_COLS) * pearl.TILE_COLS,
        "message_u32": message,
        "message_le_hex": message_bytes.hex(),
        "hash_hex": digest.hex(),
        "hash_u256_be_hex": digest[::-1].hex(),
        "meets_fixed_target": value <= TARGET * FACTOR,
        "target_accept_be_hex": accept.to_bytes(32, "big").hex(),
        "target_reject_be_hex": (accept - 1).to_bytes(32, "big").hex(),
    }


def render_tile(tile: dict) -> str:
    digest = lambda name: "Digest" + pearl.byte_array(bytes.fromhex(tile[name]))
    message = "{{" + ", ".join(f"0x{word:08x}u" for word in tile["message_u32"]) + "}}"
    return (f"{{{tile['row']}, {tile['col']}, {message},\n"
            f"        {digest('hash_hex')},\n"
            f"        {digest('target_accept_be_hex')},\n"
            f"        {digest('target_reject_be_hex')}}}")


def generate() -> tuple[bytes, bytes, dict]:
    blake3 = pearl.load_blake3()
    previous = json.loads((DESTINATION / "pearl_manifest.json").read_text())
    previous_cases = {case["name"]: case for case in previous["cases"]}
    salt_a = blake3(b"pearl/cert-v3/noise-seed/A").digest()
    salt_b = blake3(b"pearl/cert-v3/noise-seed/B").digest()
    entries, cases = [], []
    total_tiles = 0
    for case in pearl.CASES:
        digests, transcript, metadata = pearl.make_case(blake3, case, salt_a, salt_b)
        if metadata != previous_cases[case.name]:
            raise ValueError(f"Preprocessing fixture changed: {case.name}; investigate before changing proof gold")
        tiles = case.rows // pearl.TILE_ROWS * (case.cols // pearl.TILE_COLS)
        records = [tile_record(blake3, transcript, digests["a_noise_seed"], tiles,
                               case.cols, index) for index in range(tiles)]
        signal_tile_row = metadata["changed_row"] // pearl.TILE_ROWS * pearl.TILE_ROWS
        # Prefer proofs revealing the actual modified signal row, rather than
        # accidentally covering only the zero portion of A.
        def select(winning: bool) -> dict:
            eligible = [tile for tile in records if tile["meets_fixed_target"] == winning]
            if not eligible:
                raise ValueError(f"No {'winning' if winning else 'losing'} tile in fixed job {case.name}")
            return next((tile for tile in eligible if tile["row"] == signal_tile_row), eligible[0])
        winner, loser = select(True), select(False)
        entries.append(f'    {{"{case.name}",\n        {render_tile(winner)},\n        {render_tile(loser)}}},')
        cases.append({
            "name": case.name, "cert_version": case.cert_version,
            "header_hex": metadata["header_hex"], "seed_hex": metadata["seed_hex"],
            "shape": metadata["shape"], "changed_row": metadata["changed_row"],
            "a_noise_seed_hex": digests["a_noise_seed"].hex(),
            "tiles_examined": tiles,
            "winning_tiles": sum(tile["meets_fixed_target"] for tile in records),
            "all_jackpot_hashes_sha256": hashlib.sha256(b"".join(
                bytes.fromhex(tile["hash_hex"]) for tile in records)).hexdigest(),
            "winner_reveals_changed_row": winner["row"] == signal_tile_row,
            "loser_reveals_changed_row": loser["row"] == signal_tile_row,
            "winner": winner, "loser": loser,
        })
        total_tiles += tiles
    header = "\n".join([
        "// Generated by tools/generate_proof_fixtures.py --write; do not hand edit.",
        "// Independent Python expectations; fixed easy offline target, not network work.",
        "#pragma once", "", '#include "neuro/pearl_input.hpp"', "#include <array>", "",
        "namespace neuro::proof_golden {", "using Digest = neuro::pearl::Digest;",
        "struct Tile {", "    int row, col;", "    std::array<std::uint32_t, 16> message;",
        "    Digest hash, target_accept_be, target_reject_be;", "};",
        "struct Fixture { const char* name; Tile winner, loser; };",
        f"inline constexpr Digest pool_target_be = {pearl.byte_array(TARGET.to_bytes(32, 'big'))};",
        "inline constexpr std::uint32_t pool_nbits = 0x1e080000u;",
        f"inline const std::array<Fixture, {len(cases)}> fixtures = {{{{",
        "\n".join(entries), "}};", "} // namespace neuro::proof_golden", "",
    ]).encode()
    sources = ["tools/generate_proof_fixtures.py", "tools/generate_pearl_fixtures.py",
               "native/fixtures/pearl_manifest.json", "requirements-fixtures.txt"]
    manifest = {
        "schema": "neuro.proof_fixture_manifest.v1",
        "scope": "independent_jackpot_and_target_vectors_for_synthetic_dense_jobs",
        "sources_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in sources},
        "header": "native/fixtures/proof_golden.hpp",
        "header_sha256": hashlib.sha256(header).hexdigest(),
        "pool_target_be_hex": TARGET.to_bytes(32, "big").hex(),
        "pool_target_nbits": "0x1e080000", "target_factor": FACTOR,
        "scaled_bound_be_hex": (TARGET * FACTOR).to_bytes(32, "big").hex(),
        "hash_integer_encoding": "little endian", "target_integer_encoding": "big endian",
        "jackpot_formula": "msg[i%16] = rol32(msg[i%16],13) XOR cumulative_tile_xor[i]; keyed BLAKE3 of 16 u32LE words with a_noise_seed",
        "selection": "first winner/loser covering changed signal row if available, otherwise first in row-major tile order",
        "target_was_chosen_before_search": True,
        "proof_verification_observed_by_generator": False,
        "network_work": False, "gpu_execution": False,
        "cases": cases,
    }
    return header, (json.dumps(manifest, indent=2) + "\n").encode(), {
        "cases": len(cases), "tiles_examined": total_tiles,
        "selected_winners": len(cases), "selected_losers": len(cases),
        "all_selected_proofs_reveal_changed_row": all(
            case["winner_reveals_changed_row"] and case["loser_reveals_changed_row"] for case in cases),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    args = parser.parse_args()
    try:
        header, manifest, counts = generate()
        artifacts = {"proof_golden.hpp": header, "proof_manifest.json": manifest}
        mismatches = []
        for name, data in artifacts.items():
            path = DESTINATION / name
            if args.write:
                path.write_bytes(data)
            elif not path.exists() or path.read_bytes() != data:
                mismatches.append(name)
        print(json.dumps({"schema": "neuro.proof_fixture_check.v1",
                          "status": "FAIL" if mismatches else "PASS",
                          "mode": "write" if args.write else "check",
                          **counts, "mismatches": mismatches}))
        return 1 if mismatches else 0
    except (ValueError, RuntimeError, OSError, KeyError) as error:
        print(json.dumps({"schema": "neuro.proof_fixture_check.v1", "status": "ERROR", "error": str(error)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
