#!/usr/bin/env python3
"""Cross-check genuine miner PlainProofs with a separately pinned Pearl verifier.

Generates a bounded synthetic corpus, verifies winner/loser and controlled
corruption cases in separate processes, and checks the real Rust gzip payload.
Uses no GPU, live pool, wallet, API key, or model service.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import os
from pathlib import Path
import struct

from neuroctl.experiments import run_command

ROOT = Path(__file__).resolve().parents[1]


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def process(argv: list[str], timeout: int = 30) -> tuple[dict, dict]:
    result = run_command(argv, cwd=ROOT, timeout_s=timeout,
                         max_log_bytes=16384, stage="offline-proof-verification")
    if result["timed_out"] or result["launch_error"] or not result["output_complete"]:
        raise RuntimeError(f"Incomplete proof process: {result}")
    try:
        payload = json.loads(result["stdout"])
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Proof process returned invalid JSON: exit={result['returncode']}; {result['stderr']}") from error
    return result, payload


def inspect_proof(raw: bytes) -> dict:
    """Inspect a generated dense bincode corpus item and locate mutation bytes.

    This is a test fixture reader, not a production proof decoder. Each shape
    and length is asserted before modification so mutation tests do not quietly
    target the wrong field when a wire format changes.
    """
    def u64(offset: int) -> int:
        return struct.unpack_from("<Q", raw, offset)[0]
    if len(raw) < 100 or u64(16) != 4096 or u64(24) != 128:
        raise ValueError("Unexpected proof parameters in generated corpus")
    result = {"m": u64(0), "n": u64(8)}
    cursor = 32
    for name, rows in (("a", 8), ("bt", 16)):
        leaves = u64(cursor)
        if leaves != rows * 4:
            raise ValueError("Unexpected leaf count for this 8x16/K4096 profile")
        cursor += 8
        first_data = None
        for _ in range(leaves):
            length = u64(cursor)
            if length != 1024:
                raise ValueError("Unexpected serialized Merkle leaf byte length")
            cursor += 8
            if first_data is None:
                first_data = cursor
            cursor += length
        if u64(cursor) != leaves:
            raise ValueError("Unexpected leaf index count")
        cursor += 8
        indices = [u64(cursor + index * 8) for index in range(leaves)]
        cursor += leaves * 8
        total_offset = cursor
        total = u64(cursor)
        root_offset = cursor + 8
        cursor += 40
        root = raw[root_offset:root_offset + 32].hex()
        siblings = u64(cursor)
        if siblings > 64:
            raise ValueError("Unexpected sibling count in generated proof")
        cursor += 8 + siblings * 32
        if u64(cursor) != rows:
            raise ValueError("Unexpected row count in generated proof")
        cursor += 8
        row_indices = [u64(cursor + index * 8) for index in range(rows)]
        cursor += rows * 8
        result[name] = {"leaf_indices": indices, "row_indices": row_indices,
                        "total_leaves": total, "root_hex": root,
                        "first_byte_offset": first_data,
                        "total_leaves_offset": total_offset, "root_offset": root_offset}
    if raw[cursor:] not in (b"", b"\x00"):
        raise ValueError("Unexpected trailing data in generated dense proof")
    return result


def mutate(raw: bytes, offset: int, *, value: int | None = None) -> bytes:
    result = bytearray(raw)
    if value is None:
        result[offset] ^= 1
    else:
        struct.pack_into("<Q", result, offset, value)
    return bytes(result)


def check(args) -> dict:
    os.environ["RAYON_NUM_THREADS"] = "2"
    native, reference = args.native.resolve(), args.reference.resolve()
    config = json.loads((ROOT / "configs/proof-build.json").read_text())
    build_report = json.loads((ROOT / "artifacts/proof-build.json").read_text())
    expected = json.loads((ROOT / "native/fixtures/proof_manifest.json").read_text())
    pearl_expected = {case["name"]: case for case in json.loads(
        (ROOT / "native/fixtures/pearl_manifest.json").read_text())["cases"]}
    if not build_report.get("passed") or digest(reference.read_bytes()) != build_report["artifacts"]["reference_checker"]["sha256"]:
        raise ValueError("Reference checker does not match the successful real build report")
    if build_report.get("config_sha256") != digest((ROOT / "configs/proof-build.json").read_bytes()):
        raise ValueError("Proof build configuration changed")
    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    exported, export_result = process([str(native), "export", str(work)], timeout=120)
    if exported["returncode"] != 0 or export_result.get("exported") != 6:
        raise ValueError("Native corpus generation failed")
    corpus = json.loads((work / "manifest.json").read_text())
    if corpus.get("schema") != "neuro.proof_corpus.v1" or len(corpus.get("cases", [])) != 6:
        raise ValueError("Unexpected corpus manifest")
    if corpus["pool_target_be_hex"] != expected["pool_target_be_hex"] or corpus["pool_nbits"] != expected["pool_target_nbits"]:
        raise ValueError("Exported corpus changed the fixed target")
    cases_by_name = {case["name"]: case for case in expected["cases"]}
    seen = set()
    checks, compression, proof_inventory = [], [], []
    for case in corpus["cases"]:
        fixture = cases_by_name[case["fixture"]]
        winner = case["expected_valid"]
        if type(winner) is not bool:
            raise ValueError("Expected validity must be boolean")
        side = "winner" if winner else "loser"
        golden = fixture[side]
        identity = (case["fixture"], side)
        if identity in seen:
            raise ValueError("Duplicated proof corpus case")
        seen.add(identity)
        if (case["header_hex"] != fixture["header_hex"] or case["cert_version"] != fixture["cert_version"] or
                case["row"] != golden["row"] or case["col"] != golden["col"] or
                case["jackpot_hash_hex"] != golden["hash_hex"]):
            raise ValueError("Exported proof selection differs from independent expected data")
        def artifact(name: str) -> Path:
            path = (work / name).resolve()
            if path.parent != work or not path.is_file() or path.stat().st_size > 1024 * 1024:
                raise ValueError("Invalid generated corpus artifact path/size")
            return path
        proof_file, zipped_file = artifact(case["file"]), artifact(case["gzip_file"])
        encoded = proof_file.read_bytes()
        raw = base64.b64decode(encoded, validate=True)
        decoded = inspect_proof(raw)
        if decoded["m"] != fixture["shape"]["rows"] or decoded["n"] != fixture["shape"]["cols"]:
            raise ValueError("Serialized proof dimensions differ from the independent job")
        for side_name, anchor, width, dimension, commitment in (
                ("a", golden["row"], 8, decoded["m"], "commitment_a"),
                ("bt", golden["col"], 16, decoded["n"], "commitment_b")):
            matrix = decoded[side_name]
            if (matrix["row_indices"] != list(range(anchor, anchor + width)) or
                    matrix["leaf_indices"] != list(range(anchor * 4, (anchor + width) * 4)) or
                    matrix["total_leaves"] != dimension * 4 or
                    matrix["root_hex"] != pearl_expected[case["fixture"]]["digests"][commitment]):
                raise ValueError("Serialized proof rows/tree/roots differ from independent expectations")
        compressed = base64.b64decode(zipped_file.read_bytes(), validate=True)
        unzipped = gzip.decompress(compressed)
        compression.append({"case": case["name"], "passed": unzipped == raw,
                            "plain_bytes": len(raw), "gzip_bytes": len(compressed)})
        if unzipped != raw:
            raise ValueError("Rust gzip output does not contain the exact serialized proof")
        proof_inventory.append({"case": case["name"], "base64_bytes": len(encoded),
                                "raw_bytes": len(raw), "sha256": digest(raw),
                                "gzip_sha256": digest(compressed)})
        scenarios = [{"name": side, "path": proof_file, "header": case["header_hex"],
                      "target": corpus["pool_target_be_hex"], "nbits": corpus["pool_nbits"], "valid": winner,
                      "official_stage": "verified" if winner else "verification"}]
        if winner:
            offsets = decoded["a"]
            mutations = {
                "changed_a_leaf": mutate(raw, offsets["first_byte_offset"]),
                "changed_a_root": mutate(raw, offsets["root_offset"]),
                "wrong_a_tree_size": mutate(raw, offsets["total_leaves_offset"],
                                             value=fixture["shape"]["rows"] * 4096 // 1024 + 1),
                "wrong_declared_m": mutate(raw, 0, value=fixture["shape"]["rows"] * 2),
                "truncated_bincode": raw[:len(raw) // 2],
                "trailing_bincode": raw + b"\x00\x01",
            }
            for name, changed in mutations.items():
                path = work / f"{case['name']}_{name}.b64"
                path.write_bytes(base64.b64encode(changed))
                stage = "deserialize" if name in {"truncated_bincode", "trailing_bincode"} else "verification"
                scenarios.append({**scenarios[0], "name": name, "path": path, "valid": False, "official_stage": stage})
            invalid_base64 = work / f"{case['name']}_invalid_base64.b64"
            invalid_base64.write_bytes(b"!" + encoded[1:])
            scenarios.append({**scenarios[0], "name": "invalid_base64", "path": invalid_base64,
                              "valid": False, "official_stage": "base64"})
            wrong_header = bytearray.fromhex(case["header_hex"])
            wrong_header[0] ^= 1
            scenarios.append({**scenarios[0], "name": "changed_header", "header": wrong_header.hex(),
                              "valid": False, "official_stage": "verification"})
            scenarios.append({**scenarios[0], "name": "zero_target", "target": "00" * 32, "nbits": "0",
                              "valid": False, "official_stage": "verification"})
        for scenario in scenarios:
            common = ["--header-hex", scenario["header"], "--proof-base64-file", str(scenario["path"]),
                      "--cert-version", str(case["cert_version"])]
            commands = {
                "donor_ffi": [str(native), "verify", *common, "--target-be", scenario["target"]],
                "official_pearl": [str(reference), *common, "--nbits", scenario["nbits"]],
            }
            for verifier, argv in commands.items():
                result, payload = process(argv)
                valid = payload.get("valid") if verifier == "donor_ffi" else payload.get("verified")
                passed = (type(valid) is bool and valid == scenario["valid"] and
                          result["returncode"] == (0 if scenario["valid"] else 1))
                if not scenario["valid"]:
                    passed = passed and bool(payload.get("error"))
                if verifier == "official_pearl":
                    passed = passed and payload.get("upstream_commit") == config["reference_commit"]
                    passed = passed and payload.get("stage") == scenario["official_stage"]
                checks.append({"case": case["name"], "scenario": scenario["name"], "verifier": verifier,
                               "expected_valid": scenario["valid"], "passed": passed,
                               "process": result, "result": payload})
    if len(seen) != 6:
        raise ValueError("Incomplete proof corpus coverage")
    return {
        "schema": "neuro.proof_crosscheck.v1", "passed": all(row["passed"] for row in checks),
        "proof_kind": "PlainProof", "proofs_generated": len(proof_inventory),
        "verification_checks": len(checks),
        "expected_acceptances": sum(row["expected_valid"] for row in checks),
        "expected_rejections": sum(not row["expected_valid"] for row in checks),
        "official_reference_commit": config["reference_commit"],
        "golden_manifest_sha256": digest((ROOT / "native/fixtures/proof_manifest.json").read_bytes()),
        "binaries": {"native_sha256": digest(native.read_bytes()), "official_sha256": digest(reference.read_bytes())},
        "gzip_checks": compression, "proof_inventory": proof_inventory, "checks": checks,
        "stark_generated": False, "network_work": False, "pool_acceptance": False,
        "gpu_execution": False, "windows_execution": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", type=Path, default=ROOT / "build/proof-cmake/neuro_pearl_proof")
    parser.add_argument("--reference", type=Path,
                        default=ROOT / "build/proof-target/x86_64-unknown-linux-gnu/release/pearl-proof-check")
    parser.add_argument("--work-dir", type=Path, default=ROOT / "artifacts/proof-corpus")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/proof-check.json")
    args = parser.parse_args()
    try:
        report = check(args)
    except (OSError, ValueError, RuntimeError, KeyError, struct.error) as error:
        report = {"schema": "neuro.proof_crosscheck.v1", "passed": False, "error": str(error)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: report.get(key) for key in (
        "schema", "passed", "proofs_generated", "verification_checks", "expected_acceptances", "expected_rejections", "error")}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
