#!/usr/bin/env python3
"""Build the pinned real Rust proof library and official reference checker.

Linux x86_64 recipe; no pool, GPU, credentials, or proof stub. Requires the
specific rustup toolchain from configs/proof-build.json. Writes only generated
build/artifacts paths. Reference checkouts remain untouched.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import time

from neuroctl.workspace import source_check

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs/proof-build.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(argv: list[str], *, cwd: Path, env: dict, log: Path, timeout: int) -> dict:
    start = time.monotonic()
    log.parent.mkdir(parents=True, exist_ok=True)
    timed_out = False
    with log.open("wb") as stream:
        # A distinct group lets timeout stop Cargo and its compiler children.
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=stream,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            import signal
            timed_out = True
            os.killpg(process.pid, signal.SIGKILL)
            code = process.wait(timeout=30)
    return {"command": argv, "returncode": code, "timed_out": timed_out,
            "elapsed_seconds": round(time.monotonic() - start, 3),
            "log": str(log.relative_to(ROOT)), "log_sha256": sha256(log),
            "passed": code == 0 and not timed_out}


def checked_output(argv: list[str], env: dict, cwd: Path = ROOT) -> str:
    return subprocess.run(argv, env=env, cwd=cwd, check=True, timeout=60,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True).stdout.strip()


def prepare_source(config: dict, env: dict) -> dict:
    donor = ROOT / "upstream/cppminer"
    archive = subprocess.run(
        ["git", "archive", "--format=tar", config["cppminer_commit"], *config["source_paths"]],
        cwd=donor, env=env, timeout=60, check=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE).stdout
    source = ROOT / "build/proof-source"
    if source.is_symlink():
        raise ValueError("Generated proof source directory must not be a symlink")
    # Regenerate only this explicitly owned build copy. Cargo's reusable target
    # and download caches are elsewhere; never patch the reference submodule.
    if source.exists():
        shutil.rmtree(source)
    source.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar.getmembers():
            target = (source / member.name).resolve()
            if not target.is_relative_to(source.resolve()) or member.issym() or member.islnk():
                raise ValueError("Unexpected path/link in pinned Rust source archive")
        tar.extractall(source, filter="data")
    # git archive restores commit timestamps. Touch extracted files so a newer
    # source pin with old timestamps cannot reuse stale path-crate fingerprints.
    for path in source.rglob("*"):
        if path.is_file():
            os.utime(path, None)
    files = {str(path.relative_to(source)): sha256(path) for path in sorted(source.rglob("*"))
             if path.is_file()}
    return {"directory": str(source.relative_to(ROOT)), "file_count": len(files),
            "files_sha256": files,
            "inventory_sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()}


def build(args) -> dict:
    config = json.loads(CONFIG.read_text())
    report = {"schema": "neuro.proof_build.v1", "passed": False,
              "config": config, "config_sha256": sha256(CONFIG), "commands": [],
              "stub_used": False, "gpu_execution": False, "network_pool_connection": False,
              "source_check": source_check(ROOT)}
    if not report["source_check"]["passed"]:
        raise ValueError("Pinned source check failed")
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "AMD64"}:
        raise ValueError("This observed proof build recipe requires Linux x86_64; Windows remains unverified")
    donor_lock = ROOT / "upstream/cppminer/rust/cp-proof-ffi/Cargo.lock"
    reference_lock = ROOT / "rust/pearl-proof-check/Cargo.lock"
    for path, key in ((donor_lock, "donor_lock_sha256"), (reference_lock, "reference_lock_sha256")):
        if sha256(path) != config[key]:
            raise ValueError(f"Pinned lock changed: {path.relative_to(ROOT)}")
    cargo_path = args.cargo or shutil.which("cargo")
    if not cargo_path:
        raise ValueError(f"Install rustup minimal toolchain {config['rust_toolchain']} and put cargo on PATH")
    cargo = str(Path(cargo_path).absolute())
    rustc = str(Path(args.rustc).absolute()) if args.rustc else str(Path(cargo).with_name("rustc"))
    env = os.environ.copy()
    for key in ("RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER"):
        if env.get(key):
            raise ValueError(f"Unset {key} for this pinned build recipe")
    env.update({"RUSTUP_TOOLCHAIN": config["rust_toolchain"], "RUSTC": rustc,
                "CARGO_TARGET_DIR": str(ROOT / "build/proof-target"),
                "CARGO_BUILD_JOBS": str(config["jobs"]), "RUST_BACKTRACE": "0",
                "RAYON_NUM_THREADS": "2"})
    env["PATH"] = str(Path(cargo).parent) + os.pathsep + env.get("PATH", "")
    rust_version = checked_output([rustc, "-Vv"], env)
    cargo_version = checked_output([cargo, "--version"], env)
    metadata = dict(line.split(": ", 1) for line in rust_version.splitlines() if ": " in line)
    if (metadata.get("release") != config["rust_toolchain"] or
            metadata.get("commit-hash") != config["rustc_commit"] or
            metadata.get("host") != config["platform"] or
            not cargo_version.startswith(f"cargo {config['cargo_version']} ")):
        raise ValueError("Compiler does not match the recorded toolchain and host pin")
    report["toolchain"] = {"rustc": metadata, "cargo": cargo_version}
    report["source_copy"] = prepare_source(config, env)
    manifest = ROOT / "build/proof-source/rust/cp-proof-ffi/Cargo.toml"
    reference = ROOT / "rust/pearl-proof-check/Cargo.toml"
    # Explicit target also overrides CARGO_BUILD_TARGET / Cargo config. The
    # report must describe these artifacts, never stale host-default outputs.
    common = ["--locked", "--release", "--target", config["platform"],
              "--jobs", str(config["jobs"])]
    recipes = [
        ("donor-build", [cargo, "build", "--manifest-path", str(manifest), *common]),
        ("donor-tests", [cargo, "test", "--manifest-path", str(manifest), "--lib", *common]),
        ("reference-build", [cargo, "build", "--manifest-path", str(reference), *common]),
        ("native-libs", [cargo, "rustc", "--manifest-path", str(manifest), "--lib", *common,
                         "--", "--print", "native-static-libs"]),
    ]
    for name, command in recipes:
        row = run(command, cwd=ROOT, env=env, log=ROOT / f"artifacts/proof-build/{name}.log",
                  timeout=config["command_timeout_seconds"])
        report["commands"].append({"name": name, **row})
        if not row["passed"]:
            report["failure"] = name
            return report
    release = ROOT / "build/proof-target" / config["platform"] / "release"
    library = release / "libcp_proof_ffi.a"
    checker = release / "pearl-proof-check"
    report["artifacts"] = {
        name: {"path": str(path.relative_to(ROOT)), "bytes": path.stat().st_size,
               "sha256": sha256(path)} for name, path in (("proof_library", library), ("reference_checker", checker))}
    report["source_check_after"] = source_check(ROOT)
    report["locks_unchanged"] = (sha256(donor_lock) == config["donor_lock_sha256"] and
                                sha256(reference_lock) == config["reference_lock_sha256"] and
                                sha256(manifest.with_name("Cargo.lock")) == config["donor_lock_sha256"])
    report["passed"] = report["source_check_after"]["passed"] and report["locks_unchanged"]
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cargo", help="Path to the cargo/rustup executable")
    parser.add_argument("--rustc", help="Defaults to rustc next to cargo")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/proof-build.json")
    args = parser.parse_args()
    try:
        report = build(args)
    except (OSError, ValueError, subprocess.SubprocessError, KeyError) as error:
        report = {"schema": "neuro.proof_build.v1", "passed": False,
                  "error": str(error), "stub_used": False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"schema": report["schema"], "passed": report["passed"],
                      "report": str(args.output), "error": report.get("error"),
                      "failed_stage": report.get("failure")}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
