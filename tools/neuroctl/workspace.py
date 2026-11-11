"""Source provenance, read-only inventory, and offline native checks."""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import shutil
import sys

from .experiments import run_command

ROOT = Path(__file__).resolve().parents[2]


def read_json(path):
    path = Path(path)
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("JSON input exceeds the 2 MiB control-record limit")
    def reject(value):
        raise ValueError(f"Non-finite JSON value: {value}")
    with path.open(encoding="utf-8") as stream:
        return json.load(stream, parse_constant=reject)


def process_ok(result):
    return (result["returncode"] == 0 and not result["timed_out"]
            and not result["launch_error"] and result["output_complete"])


def source_check(root=ROOT):
    root = Path(root).resolve()
    lock = read_json(root / "upstream" / "sources.lock.json")
    results = []
    for source in lock["sources"]:
        if source.get("imported") is not True:
            continue
        relative = source.get("path")
        if not isinstance(relative, str) or not relative:
            raise ValueError("Imported source lacks its relative path")
        directory = (root / relative).resolve()
        if not directory.is_relative_to(root) or directory == root:
            raise ValueError("Source path must be inside the project")
        row = {"id": source["id"], "path": relative,
               "expected_commit": source["commit"], "passed": False}
        if not directory.is_dir() or not (directory / ".git").exists():
            row["reason"] = "Submodule not initialized: git submodule update --init --recursive"
        else:
            head = run_command(["git", "rev-parse", "HEAD"], cwd=directory,
                               stage="source-check", timeout_s=15)
            clean = run_command(["git", "status", "--porcelain", "--untracked-files=normal"],
                                cwd=directory, stage="source-check", timeout_s=15)
            row["actual_commit"] = head["stdout"].strip() if process_ok(head) else None
            row["clean"] = process_ok(clean) and not clean["stdout"].strip()
            row["passed"] = row["actual_commit"] == source["commit"] and row["clean"]
            if not row["passed"]:
                row["reason"] = "Missing git, wrong pinned commit, or modified baseline checkout"
        results.append(row)
    return {"schema": "neuro.sources.v1", "passed": bool(results) and
            all(row["passed"] for row in results), "sources": results}


def doctor(target="offline", root=ROOT):
    if target not in {"offline", "gpu"}:
        raise ValueError("Unknown inventory target")
    compiler = shutil.which("g++") or shutil.which("c++") or shutil.which("cl")
    found = {name: shutil.which(name) for name in
             ("git", "cmake", "cargo", "nvcc", "nvidia-smi", "ssh")}
    found["cxx"] = compiler
    found["cc"] = shutil.which("gcc") or shutil.which("cc") or shutil.which("cl")
    required = ["git", "cc", "cxx"] if target == "offline" else ["git", "cmake", "cc", "cxx", "nvcc", "nvidia-smi"]
    missing = [name for name in required if not found[name]]
    if sys.version_info < (3, 11):
        missing.append("Python >= 3.11")
    sources = source_check(root)
    result = {"schema": "neuro.inventory.v1", "target": target,
              "platform": platform.system(), "machine": platform.machine(),
              "python": platform.python_version(), "tools": found,
              "missing_required": missing, "sources": sources,
              "gpu_kernel_tested": False, "pool_tested": False,
              "passed": not missing and sources["passed"]}
    if target == "gpu" and found["nvidia-smi"]:
        query = run_command([found["nvidia-smi"],
                             "--query-gpu=name,driver_version,memory.total,compute_cap",
                             "--format=csv,noheader,nounits"],
                            timeout_s=15, stage="inventory")
        result["gpu_inventory"] = query
        result["passed"] = result["passed"] and process_ok(query)
    if target == "gpu" and found["nvcc"]:
        version = run_command([found["nvcc"], "--version"], timeout_s=15, stage="inventory")
        result["cuda_compiler"] = version
        result["passed"] = result["passed"] and process_ok(version)
    return result


def cpu_check(root=ROOT, cxx=None):
    """Build and run actual C++ checks; no full miner, network, or proof stub."""
    root = Path(root).resolve()
    provenance = source_check(root)
    result = {"schema": "neuro.cpu_check.v1", "passed": False,
              "kind": "cpu_component_checks", "sources": provenance,
              "gpu_tested": False, "proof_tested": False, "pool_tested": False,
              "checks": []}
    if not provenance["passed"]:
        result["reason"] = "Pinned upstream source check failed"
        return result
    compiler = cxx or shutil.which("g++")
    if not compiler:
        result["reason"] = "GCC g++ required for the checked LTO math-probe recipe"
        return result
    if os.name == "nt":
        result["reason"] = "Direct CPU recipe is Linux/GCC; use CMake for Windows native checks"
        return result
    cc = shutil.which("gcc")
    if not cc:
        result["reason"] = "GCC C compiler required for the pinned BLAKE3 and Pearl noise components"
        return result
    build = root / "build" / "cpu"
    build.mkdir(parents=True, exist_ok=True)
    upstream = root / "upstream" / "cppminer"
    blake3 = root / "upstream" / "blake3" / "c"
    includes = ["-I", str(upstream / "include"), "-I", str(root / "native" / "include")]
    portable = ["-DBLAKE3_NO_SSE2", "-DBLAKE3_NO_SSE41", "-DBLAKE3_NO_AVX2",
                "-DBLAKE3_NO_AVX512", "-DBLAKE3_USE_NEON=0"]
    c_objects = []
    result["c_component_builds"] = []
    c_sources = [blake3 / name for name in ("blake3.c", "blake3_dispatch.c", "blake3_portable.c")]
    c_sources.append(upstream / "src/common/cp_noise.c")
    for source in c_sources:
        output = build / (source.stem + ".o")
        compiled = run_command([cc, "-std=c11", "-O2", "-ffunction-sections",
                                "-fdata-sections", *portable, *includes,
                                "-I", str(blake3), "-c", str(source), "-o", str(output)],
                               cwd=root, timeout_s=120, stage="build")
        result["c_component_builds"].append({"source": str(source.relative_to(root)), "build": compiled})
        if not process_ok(compiled):
            result["reason"] = "Pinned C component failed to compile"
            return result
        c_objects.append(output)
    pearl_sources = [root / "native/pearl/pearl_input.cpp",
                     upstream / "src/common/cp_job_ctrl.cpp", upstream / "src/common/cp_pool.cpp"]
    fullk_sources = [root / "native/reference/fullk_reference.cpp", root / "native/reference/fullk_warp.cpp"]
    pearl_flags = ["-I", str(root / "native"), "-I", str(blake3), *portable,
                   "-ffunction-sections", "-fdata-sections", "-Wl,--gc-sections"]
    targets = [
        ("cppminer_cli", [upstream / "tests/cp_cli_test.cpp"], []),
        ("cppminer_pool_protocol", [upstream / p for p in [
            "tests/cp_pool_protocol_test.cpp", "src/common/cp_json_frame.cpp",
            "src/common/cp_pool_notify.cpp", "src/common/cp_util.cpp", "src/common/cp_state.cpp"]], []),
        ("cppminer_math_probe", [root / "native/tests/cppminer_math_probe.cpp",
            upstream / "src/cpu/gemm/case33_gemm_xor.cpp",
            upstream / "src/cpu/gemm/case33_cpu_features.cpp"],
            ["-I", str(upstream / "src/cpu/gemm"), "-flto", "-ffunction-sections",
             "-fdata-sections", "-Wl,--gc-sections"]),
        ("mma_layout", [root / "native/tests/mma_layout_test.cpp"], []),
        ("fullk", [root / "native/tests/fullk_test.cpp",
            root / "native/reference/fullk_reference.cpp",
            root / "native/reference/fullk_warp.cpp"], ["-I", str(root / "native")]),
        ("fullk_upstream", [root / "native/tests/fullk_upstream_test.cpp",
            root / "native/reference/fullk_reference.cpp",
            root / "native/reference/fullk_warp.cpp",
            upstream / "src/cpu/gemm/case33_gemm_xor.cpp",
            upstream / "src/cpu/gemm/case33_cpu_features.cpp"],
            ["-I", str(root / "native"), "-I", str(upstream / "src/cpu/gemm"),
             "-flto", "-ffunction-sections", "-fdata-sections", "-Wl,--gc-sections"]),
        ("blake3_vectors", [root / "native/tests/blake3_vectors_test.cpp", *c_objects[:3]],
            ["-I", str(root / "native"), "-I", str(blake3), *portable]),
        ("pearl_alignment", [root / "native/tests/pearl_alignment_test.cpp",
            *pearl_sources[1:], *c_objects], pearl_flags),
        ("pearl_input", [root / "native/tests/pearl_input_test.cpp",
            *pearl_sources, *fullk_sources, *c_objects], pearl_flags),
        ("pearl_upstream", [root / "native/tests/pearl_upstream_test.cpp",
            *pearl_sources, *fullk_sources, *c_objects,
            upstream / "src/cpu/gemm/case33_gemm_xor.cpp",
            upstream / "src/cpu/gemm/case33_cpu_features.cpp"],
            [*pearl_flags, "-I", str(upstream / "src/cpu/gemm"), "-flto"]),
    ]
    for name, sources, extra in targets:
        executable = build / name
        compile_result = run_command([compiler, "-std=c++17", "-O2", "-pthread",
                                      *includes, *extra, *(str(p) for p in sources),
                                      "-o", str(executable)], cwd=root,
                                     timeout_s=120, stage="build")
        entry = {"name": name, "build": compile_result, "passed": False}
        if process_ok(compile_result):
            verification = run_command([str(executable)], cwd=root,
                                       timeout_s=60, stage="verification")
            entry["verification"] = verification
            entry["passed"] = process_ok(verification)
        result["checks"].append(entry)
    result["passed"] = all(check["passed"] for check in result["checks"])
    return result
