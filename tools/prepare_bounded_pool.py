#!/usr/bin/env python3
"""Apply the bounded-host patch to an owned copy of the pinned donor source.

The reference checkout is read-only. No protocol serializer, arithmetic or proof
implementation is replaced. The legacy asynchronous reader is excluded at
compile time because neuro has its own bounded event loop.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "upstream/cppminer/src/common/cp_pool.cpp"
PATCH = ROOT / "patches/cppminer-bounded-pool.patch"
SOURCE_COMMIT = "5b68506af5e6433ee5c25aa9e6fbb00b537590aa"
SOURCE_SHA256 = "da85ab64d00d696e63504254645c2bca0936e6ff523faae9e94f9ce8a473fe3e"
PATCH_SHA256 = "d43e18030c9178aadbc299d2243e327063ec68f08598b366efc31c4f65afec4d"
OUTPUT_SHA256 = "98ca31d7e4e5552a626df7534f542f28521cc3b81b79efec8c8ec607c7e4f48f"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def prepare(output_dir: Path) -> dict:
    directory = output_dir.resolve()
    if (not directory.is_relative_to((ROOT / "build").resolve())
            or directory == (ROOT / "build").resolve()
            or directory.is_relative_to((ROOT / "upstream").resolve())):
        raise ValueError("Generated donor copy must be in its own directory under project build/")
    source, patch = SOURCE.read_bytes(), PATCH.read_bytes()
    if digest(source) != SOURCE_SHA256 or digest(patch) != PATCH_SHA256:
        raise ValueError("Donor source or bounded-host patch differs from the reviewed byte hashes")
    output = directory / "src/common/cp_pool.cpp"
    if output.is_symlink() or not output.resolve().is_relative_to(directory):
        raise ValueError("Generated donor source must not escape its owned output directory")
    reused = output.is_file() and digest(output.read_bytes()) == OUTPUT_SHA256
    if not reused:
        output.parent.mkdir(parents=True, exist_ok=True)
        # A fresh temporary directory makes patching independent of existing
        # build files and prevents Git from using the project's index/worktree.
        with tempfile.TemporaryDirectory(prefix="pool-patch-", dir=directory) as temporary:
            work = Path(temporary)
            candidate = work / "src/common/cp_pool.cpp"
            candidate.parent.mkdir(parents=True)
            candidate.write_bytes(source)
            env = os.environ.copy()
            for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
                env.pop(key, None)
            env["GIT_CEILING_DIRECTORIES"] = str(directory)
            command = ["git", "-c", "core.autocrlf=false", "apply", "--whitespace=error-all"]
            for args in ([*command, "--check", str(PATCH)], [*command, str(PATCH)]):
                result = subprocess.run(args, cwd=work, env=env, capture_output=True,
                                        timeout=15, check=False)
                if result.returncode:
                    detail = result.stderr.decode("utf-8", errors="replace")[-4096:]
                    raise ValueError("The reviewed donor patch did not apply cleanly: " + detail)
            if digest(candidate.read_bytes()) != OUTPUT_SHA256:
                raise ValueError("Patched donor source differs from its reviewed output hash")
            os.replace(candidate, output)
    if SOURCE.read_bytes() != source:
        raise ValueError("Reference source changed during preparation")
    report = {"schema": "neuro.bounded_pool_source.v1", "passed": True,
              "source_commit": SOURCE_COMMIT, "source_sha256": SOURCE_SHA256,
              "patch_sha256": PATCH_SHA256, "output_sha256": OUTPUT_SHA256,
              "output": output.relative_to(ROOT).as_posix(), "reused": reused,
              "excluded_component": "legacy_async_reader",
              "reference_modified": False, "proof_stub_used": False}
    (directory / "source.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = prepare(args.output_dir)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        report = {"schema": "neuro.bounded_pool_source.v1", "passed": False, "error": str(error)}
    print(json.dumps(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
