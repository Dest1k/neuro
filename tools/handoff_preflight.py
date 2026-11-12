#!/usr/bin/env python3
"""Read-only prerequisite check for the Linux handoff VM; never installs tools.

Version probes are bounded local commands. This is not a build, a CUDA driver
query, a DSH adapter, or authorization to run a miner. Windows uses ci.ps1 and
the existing remote inventory instead of pretending Linux inventory covers it.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import sys

from candidate_manifest import create_manifest
from neuroctl.experiments import run_command, write_result
from neuroctl.workspace import process_ok, read_json, source_check

ROOT = Path(__file__).resolve().parents[1]
RUST_OVERRIDES = ("RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "RUSTC_WRAPPER",
                  "RUSTC_WORKSPACE_WRAPPER")
# Empty-input hash from the pinned official BLAKE3 test_vectors/test_vectors.json.
EMPTY_BLAKE3 = "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262"


def executable(value: str | None) -> str | None:
    found = shutil.which(value) if value else None
    return str(Path(found).absolute()) if found else None


def rust_binary(name: str, requested: str | None, pin: str, target: str,
                sibling: str | None = None) -> str | None:
    """Resolve installed rustup binaries without ever starting a rustup shim.

    A version request through a shim may materialize a missing toolchain. Use
    the already installed native toolchain instead; a missing pin stays missing.
    Explicit non-shim binaries are still checked against release/commit/host.
    """
    selected = executable(requested) if requested else (
        executable(str(Path(sibling).with_name(name))) if sibling else executable(name))
    if requested and selected is None:
        return None
    if selected:
        path = Path(selected)
        shim = path.resolve().name == "rustup"
        # Rustup also installs hard-linked proxies. Do not run a proxy merely
        # because its path did not resolve through a symbolic link.
        neighbour = path.with_name("rustup")
        if neighbour.is_file():
            shim = shim or os.path.samefile(path, neighbour)
        if not shim:
            return selected
    toolchains = Path(os.environ.get("RUSTUP_HOME", str(Path.home() / ".rustup")))
    direct = toolchains / "toolchains" / f"{pin}-{target}" / "bin" / name
    resolved = executable(str(direct))
    if resolved and Path(resolved).resolve().name != "rustup":
        return resolved
    return None


def probe(path: str | None, arguments: list[str], root: Path) -> dict:
    if path is None:
        return {"path": None, "available": False, "reason": "Executable not found"}
    result = run_command([path, *arguments], cwd=root, timeout_s=10,
                         max_log_bytes=8192, stage="inventory")
    okay = (process_ok(result) and not result["stdout_truncated"]
            and not result["stderr_truncated"])
    return {"path": path, "available": okay, "probe": result}


def version(row: dict, label: str) -> tuple[int, int, int] | None:
    if not row["available"]:
        return None
    match = re.search(r"\b" + re.escape(label) + r" version (\d+)\.(\d+)\.(\d+)",
                      row["probe"]["stdout"])
    return tuple(map(int, match.groups())) if match else None


def inspect(args, root: Path = ROOT) -> dict:
    root = Path(root).resolve()
    missing = []
    result = {"schema": "neuro.handoff_preflight.v1", "stage": args.stage,
              "passed": False, "kind": "local_prerequisites_not_build_evidence",
              "root": str(root), "platform": platform.system(),
              "machine": platform.machine(), "python": platform.python_version(),
              "checks": {}, "tools": {}, "missing": missing,
              "limits": {"compiler_compatibility_tested": False,
                         "proof_built": False, "cargo_dependencies_materialized": False,
                         "gpu_execution": False, "windows_execution": False,
                         "owner_ssh_connected": False, "srbminer_stopped": False,
                         "dsh_transport_connected": False, "provider_billing_verified": False,
                         "paid_model_calls": 0, "external_pool_connection": False}}
    checks, tools = result["checks"], result["tools"]
    if result["platform"] != "Linux" or result["machine"].lower() not in {"x86_64", "amd64"}:
        missing.append("This preflight covers the Linux x86_64 VM; use windows/ci.ps1 on Windows")
        return result
    minimum = (3, 11) if args.stage == "basic" else (3, 11, 8)
    checks["python_supported"] = tuple(sys.version_info[:3]) >= minimum
    if not checks["python_supported"]:
        missing.append("Python >= " + ".".join(map(str, minimum)))
    checks["empty_readme"] = (root / "README.md").is_file() and (root / "README.md").stat().st_size == 0
    workflows = root / ".github/workflows"
    checks["github_actions_disabled"] = not workflows.exists() or not any(
        path.suffix.lower() in {".yaml", ".yml"} for path in workflows.rglob("*"))
    if not checks["empty_readme"]:
        missing.append("README.md must be present and exactly zero bytes")
    if not checks["github_actions_disabled"]:
        missing.append("Remove active root GitHub Actions workflows; this project runs locally")
    checks["sources"] = source_check(root)
    if not checks["sources"]["passed"]:
        missing.append("Pinned sources missing, modified, or at the wrong commit")
    try:
        candidate = create_manifest(root)
        checks["candidate_sha256"] = candidate["candidate_sha256"]
        checks["candidate_files"] = len(candidate["files"])
    except (ValueError, OSError) as exc:
        checks["candidate_error"] = str(exc)
        missing.append("Exact public source identity is unavailable")

    cmake = executable(args.cmake or "cmake")
    ctest = executable(args.ctest or (str(Path(cmake).with_name("ctest")) if cmake else "ctest"))
    recipes = {
        "git": (executable("git"), ["--version"]),
        "cc": (executable(args.cc or os.environ.get("CC") or "cc"), ["--version"]),
        "cxx": (executable(args.cxx or os.environ.get("CXX") or "c++"), ["--version"]),
        "cmake": (cmake, ["--version"]), "ctest": (ctest, ["--version"]),
        "build_tool": (executable(args.build_tool or
                                  ("ninja" if args.generator == "Ninja" else "make")), ["--version"])}
    result["generator"] = args.generator
    for name, (path, arguments) in recipes.items():
        tools[name] = probe(path, arguments, root)
        if not tools[name]["available"]:
            missing.append("Required local tool: " + name)
    cmake_version = version(tools["cmake"], "cmake")
    ctest_version = version(tools["ctest"], "ctest")
    checks["cmake_compatible"] = cmake_version is not None and cmake_version >= (3, 24, 0)
    checks["ctest_matches_cmake"] = cmake_version is not None and ctest_version == cmake_version
    if not checks["cmake_compatible"]:
        missing.append("CMake >= 3.24")
    if not checks["ctest_matches_cmake"]:
        missing.append("CTest from the same CMake installation/version")

    try:
        fixture_version = importlib.metadata.version("blake3")
    except importlib.metadata.PackageNotFoundError:
        fixture_version = None
    result["fixtures"] = {"required": args.require_fixtures, "blake3_version": fixture_version,
                          "locked_version": "1.0.11", "wheel_hash_verified": False,
                          "runtime_check_passed": None}
    if args.require_fixtures:
        compatible = (platform.python_implementation() == "CPython"
                      and sys.version_info[:2] in {(3, 11), (3, 12)} and fixture_version == "1.0.11")
        if compatible:
            tools["fixture_binding"] = probe(sys.executable,
                ["-c", "import blake3; print(blake3.blake3(b'').hexdigest())"], root)
            compatible = (tools["fixture_binding"]["available"] and
                          tools["fixture_binding"]["probe"]["stdout"].strip() == EMPTY_BLAKE3)
        result["fixtures"]["runtime_check_passed"] = compatible
        if not compatible:
            missing.append("Fixture oracles need CPython 3.11/3.12 and working hash-locked blake3 1.0.11")

    if args.stage in {"proof", "cuda-build"}:
        config = read_json(root / "configs/proof-build.json")
        target = "x86_64-unknown-linux-gnu"
        cargo = rust_binary("cargo", args.cargo, config["rust_toolchain"], target)
        rustc = rust_binary("rustc", args.rustc, config["rust_toolchain"], target, sibling=cargo)
        tools["cargo"] = probe(cargo, ["--version"], root)
        tools["rustc"] = probe(rustc, ["-Vv"], root)
        metadata = {}
        if tools["rustc"]["available"]:
            metadata = dict(line.split(": ", 1) for line in tools["rustc"]["probe"]["stdout"].splitlines()
                            if ": " in line)
        checks["rust_pin"] = (metadata.get("release") == config["rust_toolchain"]
                              and metadata.get("commit-hash") == config["rustc_commit"]
                              and metadata.get("host") == target)
        checks["cargo_pin"] = (tools["cargo"]["available"] and tools["cargo"]["probe"]["stdout"].startswith(
            f"cargo {config['cargo_version']} "))
        if not checks["rust_pin"] or not checks["cargo_pin"]:
            missing.append("Already installed native Rust/Cargo with the exact configs/proof-build.json pin")
        overrides = [name for name in RUST_OVERRIDES if os.environ.get(name)]
        checks["rust_override_names"] = overrides  # Never serialize environment values.
        if overrides:
            missing.append("Unset Rust compiler flags/wrappers before the pinned proof build")
        for name, relative in (("donor_lock_sha256", "upstream/cppminer/rust/cp-proof-ffi/Cargo.lock"),
                               ("reference_lock_sha256", "rust/pearl-proof-check/Cargo.lock")):
            path = root / relative
            checks[name] = path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == config[name]
            if not checks[name]:
                missing.append("Pinned lock hash mismatch: " + relative)
        cargo_home = args.cargo_home or os.environ.get("CARGO_HOME")
        result["cargo_home"] = {"path": cargo_home, "selected_by": "argument" if args.cargo_home else "environment_or_default",
                                "cache_completeness_checked": False}
        if cargo_home:
            cache = Path(cargo_home).expanduser()
            if cache.exists() and not cache.is_dir():
                missing.append("CARGO_HOME is an existing non-directory; select a cache directory")
        result["proof_build_arguments"] = ["--cargo", cargo, "--rustc", rustc] if cargo and rustc else []
        if cargo_home:
            result["proof_build_arguments"] += ["--cargo-home", cargo_home]

    if args.stage == "cuda-build":
        nvcc = executable(args.nvcc or "nvcc")
        tools["nvcc"] = probe(nvcc, ["--version"], root)
        tools["cuda_architectures"] = probe(nvcc, ["--list-gpu-code"], root)
        cuda_text = tools["nvcc"].get("probe", {}).get("stdout", "")
        match = re.search(r"release (\d+)\.(\d+)", cuda_text)
        checks["cuda_toolkit_minimum"] = bool(tools["nvcc"]["available"] and match
                                             and tuple(map(int, match.groups())) >= (12, 8))
        checks["cuda_sm120_compiler"] = bool(tools["cuda_architectures"]["available"] and
            re.search(r"\bsm_120\b", tools["cuda_architectures"].get("probe", {}).get("stdout", "")))
        if not checks["cuda_toolkit_minimum"] or not checks["cuda_sm120_compiler"]:
            missing.append("CUDA toolkit >= 12.8 advertising sm_120 (driver/GPU still untested)")
    result["passed"] = not missing
    return result


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--stage", choices=["basic", "proof", "cuda-build"], default="basic")
    for name in ("cmake", "ctest", "cc", "cxx", "build-tool", "cargo", "rustc", "cargo-home", "nvcc"):
        cli.add_argument("--" + name, help="Explicit local path; no shell command or installation")
    cli.add_argument("--generator", choices=["Unix Makefiles", "Ninja"], default="Unix Makefiles")
    cli.add_argument("--require-fixtures", action="store_true")
    cli.add_argument("--output", type=Path)
    return cli


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = inspect(args)
        if args.output:
            result = write_result(args.output, result)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0 if result["passed"] else 1
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"passed": False, "error": str(exc), "error_type": type(exc).__name__}),
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
