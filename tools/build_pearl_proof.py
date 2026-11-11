#!/usr/bin/env python3
"""Build the pinned real Rust proof library and official reference checker.

Native Linux x86_64 or Windows x64 MSVC release build; no pool, GPU, credentials,
or proof stub. Requires the specific rustup toolchain from configs/proof-build.json
and, on Windows, an x64 MSVC/SDK developer environment plus a writable short
CARGO_HOME (for example C:\\nc). Writes generated build/artifacts paths and the
selected Cargo cache. Reference checkouts remain untouched.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
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
        grouping = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                    if os.name == "nt" else {"start_new_session": True})
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=stream,
                                   stderr=subprocess.STDOUT, **grouping)
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=stream, stderr=subprocess.STDOUT, timeout=30,
                               check=False)
            else:
                import signal
                os.killpg(process.pid, signal.SIGKILL)
            code = process.wait(timeout=30)
    return {"command": argv, "returncode": code, "timed_out": timed_out,
            "elapsed_seconds": round(time.monotonic() - start, 3),
            "log": log.relative_to(ROOT).as_posix(), "log_sha256": sha256(log),
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
    files = {path.relative_to(source).as_posix(): sha256(path) for path in sorted(source.rglob("*"))
             if path.is_file()}
    return {"directory": source.relative_to(ROOT).as_posix(), "file_count": len(files),
            "files_sha256": files,
            "inventory_sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()}


def select_target(config: dict, requested: str | None) -> tuple[str, dict]:
    if platform.machine().lower() not in {"x86_64", "amd64"}:
        raise ValueError("The recorded proof profiles require native x86_64")
    host_system = platform.system()
    native = {"Linux": "x86_64-unknown-linux-gnu", "Windows": "x86_64-pc-windows-msvc"}.get(host_system)
    target = requested or native
    if target not in config["platforms"] or target != native:
        raise ValueError("Choose the native Linux x86_64 or Windows x64 MSVC target; tests must run locally")
    profile = config["platforms"][target]
    if profile["system"] != host_system:
        raise ValueError("Target profile does not match this operating system")
    return target, profile


def native_libraries(log: Path, target: str) -> list[str]:
    entries = re.findall(r"native-static-libs:\s*([^\r\n]+)", log.read_text(encoding="utf-8", errors="replace"))
    if not entries:
        raise ValueError("rustc did not report the real static library's native link dependencies")
    parsed = [shlex.split(entry) for entry in entries]
    if not parsed[0] or any(row != parsed[0] for row in parsed[1:]):
        raise ValueError("rustc reported missing or conflicting native link dependencies")
    pattern = r"[A-Za-z0-9_.+-]+\.lib" if target.endswith("-msvc") else r"-l[A-Za-z0-9_.+-]+"
    if any(not re.fullmatch(pattern, value) for value in parsed[0]):
        raise ValueError("Unexpected native library format; record and review the actual rustc output")
    return parsed[0]


def configure_cargo_home(env: dict, requested: str | None, target: str) -> dict:
    """Keep pinned git/submodule checkout paths below Windows MAX_PATH.

    The pinned Pearl checkout includes a 246-character path relative to
    CARGO_HOME, in the CUTLASS submodule. An at-most-eight-UTF-16-unit cache
    root leaves that full path at most 255 units. Cargo still checks out this
    submodule for the CPU checker. No global Git config or registry changes.
    """
    selected = requested or env.get("CARGO_HOME")
    source = "argument" if requested else "environment" if selected else "cargo_default"
    windows = target == "x86_64-pc-windows-msvc"
    if not selected:
        if windows:
            raise ValueError("Windows proof dependencies need a short writable CARGO_HOME; "
                             "set CARGO_HOME=C:\\nc or pass --cargo-home C:\\nc")
        return {"path": None, "source": source, "windows_short_path_checked": False}
    path = Path(selected).expanduser().absolute()
    if windows:
        units = len(str(path).encode("utf-16-le")) // 2
        if not re.fullmatch(r"[A-Za-z]:", path.drive) or units > 8:
            raise ValueError("Windows CARGO_HOME must be a short local absolute path "
                             "of at most 8 UTF-16 units, such as C:\\nc; "
                             "the pinned CUTLASS checkout otherwise exceeds MAX_PATH")
        path.mkdir(parents=True, exist_ok=True)
        # Fail before Cargo downloads/builds if this user cannot write the cache.
        # This creates no lasting marker and does not request administrator access.
        with tempfile.TemporaryFile(dir=path):
            pass
    env["CARGO_HOME"] = str(path)
    return {"path": str(path), "source": source,
            "windows_short_path_checked": windows}


def build(args) -> dict:
    config = json.loads(CONFIG.read_text())
    target, profile = select_target(config, args.target)
    report = {"schema": "neuro.proof_build.v2", "passed": False,
              "config": config, "config_sha256": sha256(CONFIG), "commands": [],
              "target": target, "profile": "release", "crt": profile["crt"],
              "rustflags": profile["rustflags"],
              "stub_used": False, "gpu_execution": False, "network_pool_connection": False,
              "source_check": source_check(ROOT)}
    if not report["source_check"]["passed"]:
        raise ValueError("Pinned source check failed")
    donor_lock = ROOT / "upstream/cppminer/rust/cp-proof-ffi/Cargo.lock"
    reference_lock = ROOT / "rust/pearl-proof-check/Cargo.lock"
    for path, key in ((donor_lock, "donor_lock_sha256"), (reference_lock, "reference_lock_sha256")):
        if sha256(path) != config[key]:
            raise ValueError(f"Pinned lock changed: {path.relative_to(ROOT)}")
    cargo_path = args.cargo or shutil.which("cargo")
    if not cargo_path:
        raise ValueError(f"Install rustup minimal toolchain {config['rust_toolchain']} and put cargo on PATH")
    cargo = str(Path(cargo_path).absolute())
    rustc = (str(Path(args.rustc).absolute()) if args.rustc else
             str(Path(cargo).with_name("rustc.exe" if os.name == "nt" else "rustc")))
    env = os.environ.copy()
    for key in ("RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER"):
        if env.get(key):
            raise ValueError(f"Unset {key} for this pinned build recipe")
    env.update({"RUSTUP_TOOLCHAIN": config["rust_toolchain"], "RUSTC": rustc,
                "CARGO_TARGET_DIR": str(ROOT / "build/proof-target"),
                "CARGO_BUILD_JOBS": str(config["jobs"]), "RUST_BACKTRACE": "0",
                "RAYON_NUM_THREADS": "2", "RUSTFLAGS": " ".join(profile["rustflags"])})
    report["cargo_home"] = configure_cargo_home(env, args.cargo_home, target)
    env["PATH"] = str(Path(cargo).parent) + os.pathsep + env.get("PATH", "")
    rust_version = checked_output([rustc, "-Vv"], env)
    cargo_version = checked_output([cargo, "--version"], env)
    metadata = dict(line.split(": ", 1) for line in rust_version.splitlines() if ": " in line)
    if (metadata.get("release") != config["rust_toolchain"] or
            metadata.get("commit-hash") != config["rustc_commit"] or
            metadata.get("host") != target or
            not cargo_version.startswith(f"cargo {config['cargo_version']} ")):
        raise ValueError("Compiler does not match the recorded toolchain and host pin")
    report["toolchain"] = {"rustc": metadata, "cargo": cargo_version}
    target_cfg = checked_output([rustc, "--print", "cfg", "--target", target, *profile["rustflags"]], env)
    report["toolchain"]["target_cfg"] = target_cfg.splitlines()
    if profile["crt"] == "dynamic" and 'target_feature="crt-static"' in target_cfg.splitlines():
        raise ValueError("The Windows proof recipe requires the dynamic release CRT")
    report["source_copy"] = prepare_source(config, env)
    manifest = ROOT / "build/proof-source/rust/cp-proof-ffi/Cargo.toml"
    reference = ROOT / "rust/pearl-proof-check/Cargo.toml"
    # Explicit target also overrides CARGO_BUILD_TARGET / Cargo config. The
    # report must describe these artifacts, never stale host-default outputs.
    common = ["--locked", "--release", "--target", target,
              "--jobs", str(config["jobs"])]
    recipes = [
        ("donor-build", [cargo, "build", "--manifest-path", str(manifest), *common]),
        ("donor-tests", [cargo, "test", "--manifest-path", str(manifest), "--lib", *common,
                         "--", "--test-threads=2"]),
        ("reference-build", [cargo, "build", "--manifest-path", str(reference), *common]),
        ("native-libs", [cargo, "rustc", "--manifest-path", str(manifest), "--lib", *common,
                         "--", "--print", "native-static-libs"]),
    ]
    for name, command in recipes:
        row = run(command, cwd=ROOT, env=env, log=ROOT / f"artifacts/proof-build/{target}/{name}.log",
                  timeout=config["command_timeout_seconds"])
        report["commands"].append({"name": name, **row})
        if not row["passed"]:
            report["failure"] = name
            return report
    report["native_static_libs"] = native_libraries(ROOT / report["commands"][-1]["log"], target)
    release = ROOT / "build/proof-target" / target / "release"
    library = release / profile["proof_library"]
    checker = release / profile["reference_checker"]
    report["artifacts"] = {
        name: {"path": path.relative_to(ROOT).as_posix(), "bytes": path.stat().st_size,
               "sha256": sha256(path)} for name, path in (("proof_library", library), ("reference_checker", checker))}
    report["source_check_after"] = source_check(ROOT)
    report["locks_unchanged"] = (sha256(donor_lock) == config["donor_lock_sha256"] and
                                sha256(reference_lock) == config["reference_lock_sha256"] and
                                sha256(manifest.with_name("Cargo.lock")) == config["donor_lock_sha256"])
    report["passed"] = report["source_check_after"]["passed"] and report["locks_unchanged"]
    return report


def save_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(json.dumps(report, indent=2) + "\n")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cargo", help="Path to the cargo/rustup executable")
    parser.add_argument("--rustc", help="Defaults to rustc next to cargo")
    parser.add_argument("--cargo-home", help="Cargo cache path; otherwise inherits CARGO_HOME. "
                        "Windows requires a writable short local path such as C:\\nc")
    parser.add_argument("--target", choices=["x86_64-unknown-linux-gnu", "x86_64-pc-windows-msvc"],
                        help="Defaults to this host's native target; cross-compiling cannot pass runtime tests")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/proof-build.json")
    args = parser.parse_args()
    # An interrupted rebuild must not leave an older successful report standing
    # in for the current attempt. Final results replace this marker atomically.
    save_report(args.output, {"schema": "neuro.proof_build.v2", "passed": False,
                              "status": "building", "target": args.target, "stub_used": False})
    try:
        report = build(args)
    except (OSError, ValueError, subprocess.SubprocessError, KeyError) as error:
        report = {"schema": "neuro.proof_build.v2", "passed": False,
                  "error": str(error), "stub_used": False}
    save_report(args.output, report)
    print(json.dumps({"schema": report["schema"], "passed": report["passed"],
                      "report": str(args.output), "error": report.get("error"),
                      "failed_stage": report.get("failure")}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
