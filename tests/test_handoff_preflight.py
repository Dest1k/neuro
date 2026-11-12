"""Preflight must not turn presence/version information into execution evidence."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import handoff_preflight as preflight


class PreflightTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="neuro-preflight-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "README.md").write_bytes(b"")
        for relative in ("configs/proof-build.json", "rust/pearl-proof-check/Cargo.lock",
                         "upstream/cppminer/rust/cp-proof-ffi/Cargo.lock"):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, path)
        self.config = json.loads((self.root / "configs/proof-build.json").read_text())
        self.calls = []
        self.versions = {
            "cmake": "cmake version 3.31.6", "ctest": "ctest version 3.31.6",
            "cargo": "cargo " + self.config["cargo_version"] + " (fixture)",
            "rustc": "\n".join(("rustc " + self.config["rust_toolchain"],
                                   "release: " + self.config["rust_toolchain"],
                                   "commit-hash: " + self.config["rustc_commit"],
                                   "host: x86_64-unknown-linux-gnu")),
            "nvcc": "Cuda compilation tools, release 13.5, V13.5.37",
            "architectures": "sm_80\nsm_120\n", "fixture_binding": preflight.EMPTY_BLAKE3}
        patches = [
            patch.object(preflight.platform, "system", return_value="Linux"),
            patch.object(preflight.platform, "machine", return_value="x86_64"),
            patch.object(preflight, "source_check", return_value={"passed": True}),
            patch.object(preflight, "create_manifest", return_value={"candidate_sha256": "a" * 64, "files": []}),
            patch.object(preflight, "executable", side_effect=lambda value: "/fixture/" + Path(value).name if value else None),
            patch.object(preflight, "rust_binary", side_effect=lambda name, *a, **k: "/fixture/" + name),
            patch.object(preflight, "probe", side_effect=self.probe),
            patch.object(preflight.importlib.metadata, "version", return_value="1.0.11"),
            patch.dict(os.environ, {key: "" for key in preflight.RUST_OVERRIDES})]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def probe(self, path, arguments, root):
        self.calls.append((path, arguments))
        name = Path(path).name if path else "missing"
        if arguments[:1] == ["-c"]:
            name = "fixture_binding"
        text = self.versions.get("architectures" if arguments == ["--list-gpu-code"] else name, "fixture version")
        return {"path": path, "available": path is not None,
                "probe": {"stdout": text, "stderr": ""}}

    def inspect(self, *arguments):
        return preflight.inspect(preflight.parser().parse_args(arguments), self.root)

    def test_basic_never_invokes_rust_cuda_ssh_or_model_and_does_not_claim_execution(self):
        result = self.inspect()
        self.assertTrue(result["passed"], result["missing"])
        self.assertEqual({Path(path).name for path, _ in self.calls},
                         {"git", "cc", "c++", "cmake", "ctest", "make"})
        self.assertTrue(all(arguments == ["--version"] for _, arguments in self.calls))
        self.assertTrue(all(value is False or value == 0 for value in result["limits"].values()))

    def test_old_cmake_and_mismatched_ctest_are_separate_blockers(self):
        self.versions["cmake"] = "cmake version 3.23.9"
        result = self.inspect()
        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["cmake_compatible"])
        self.assertFalse(result["checks"]["ctest_matches_cmake"])

    def test_same_rust_release_with_wrong_commit_or_host_is_not_the_pin(self):
        original = self.versions["rustc"]
        for old, new in ((self.config["rustc_commit"], "0" * 40),
                         ("x86_64-unknown-linux-gnu", "x86_64-pc-windows-msvc")):
            with self.subTest(value=new):
                self.versions["rustc"] = original.replace(old, new)
                result = self.inspect("--stage", "proof")
                self.assertFalse(result["passed"])
                self.assertFalse(result["checks"]["rust_pin"])

    def test_rust_environment_blocks_without_disclosing_values(self):
        marker = "PRIVATE_TEST_MARKER"
        with patch.dict(os.environ, {"RUSTC_WRAPPER": marker}):
            result = self.inspect("--stage", "proof")
        self.assertFalse(result["passed"])
        self.assertEqual(result["checks"]["rust_override_names"], ["RUSTC_WRAPPER"])
        self.assertNotIn(marker, json.dumps(result))

    def test_lock_tampering_fails_without_modifying_or_repairing_it(self):
        path = self.root / "rust/pearl-proof-check/Cargo.lock"
        tampered = path.read_bytes() + b"\n# unexpected\n"
        path.write_bytes(tampered)
        result = self.inspect("--stage", "proof")
        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["reference_lock_sha256"])
        self.assertEqual(path.read_bytes(), tampered)

    def test_active_workflow_and_nonempty_readme_are_not_silently_repaired(self):
        workflow = self.root / ".github/workflows/ci.yml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text("name: forbidden\n")
        (self.root / "README.md").write_bytes(b"changed")
        result = self.inspect()
        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["empty_readme"])
        self.assertFalse(result["checks"]["github_actions_disabled"])
        self.assertTrue(workflow.is_file())

    def test_cuda_version_without_exact_target_is_insufficient(self):
        self.versions["architectures"] = "sm_90\nsm_120a\n"
        result = self.inspect("--stage", "cuda-build")
        self.assertFalse(result["passed"])
        self.assertTrue(result["checks"]["cuda_toolkit_minimum"])
        self.assertFalse(result["checks"]["cuda_sm120_compiler"])
        self.assertTrue(all(args in (["--version"], ["-Vv"], ["--list-gpu-code"]) for _, args in self.calls))

    def test_fixture_package_is_optional_and_install_hash_is_never_inferred(self):
        with patch.object(preflight.importlib.metadata, "version", return_value="0.4.1"):
            self.assertTrue(self.inspect()["passed"])
            self.assertFalse(self.inspect("--require-fixtures")["passed"])
        result = self.inspect("--stage", "proof", "--cargo-home", str(self.root / "absent"))
        self.assertTrue(result["passed"], result["missing"])
        self.assertFalse(result["fixtures"]["wheel_hash_verified"])
        self.assertFalse(result["limits"]["cargo_dependencies_materialized"])
        self.assertFalse((self.root / "absent").exists())

    def test_matching_package_metadata_cannot_hide_a_broken_extension_or_non_cpython(self):
        self.versions["fixture_binding"] = "broken-extension"
        self.assertFalse(self.inspect("--require-fixtures")["passed"])
        self.versions["fixture_binding"] = preflight.EMPTY_BLAKE3
        self.assertTrue(self.inspect("--require-fixtures")["passed"])
        with patch.object(preflight.platform, "python_implementation", return_value="PyPy"):
            self.assertFalse(self.inspect("--require-fixtures")["passed"])

    def test_existing_file_cannot_be_a_cargo_download_cache(self):
        cache = self.root / "occupied"
        cache.write_bytes(b"leave unchanged")
        result = self.inspect("--stage", "proof", "--cargo-home", str(cache))
        self.assertFalse(result["passed"])
        self.assertEqual(cache.read_bytes(), b"leave unchanged")

    def test_windows_is_not_mislabeled_as_verified_linux_readiness(self):
        with patch.object(preflight.platform, "system", return_value="Windows"):
            result = self.inspect()
        self.assertFalse(result["passed"])
        self.assertEqual(self.calls, [])
        self.assertFalse(result["limits"]["windows_execution"])


@unittest.skipIf(os.name == "nt", "Linux VM resolver, not a Windows preflight")
class RustShimResolutionTests(unittest.TestCase):
    def test_missing_pinned_toolchain_never_executes_rustup_proxy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "would-have-installed"
            shim = root / "rustup"
            shim.write_text("#!/bin/sh\ntouch '" + str(marker) + "'\n")
            shim.chmod(0o755)
            cargo = root / "cargo"
            os.link(shim, cargo)
            rust_home = root / "toolchains-not-installed"
            with patch.dict(os.environ, {"RUSTUP_HOME": str(rust_home)}):
                selected = preflight.rust_binary("cargo", str(cargo), "1.99.1", "x86_64-unknown-linux-gnu")
            self.assertIsNone(selected)
            self.assertFalse(marker.exists())
            self.assertFalse(rust_home.exists())

    def test_installed_toolchain_is_resolved_without_running_proxy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shim = root / "rustup"
            shim.write_text("#!/bin/sh\nexit 99\n")
            shim.chmod(0o755)
            cargo = root / "cargo"
            cargo.symlink_to(shim.name)
            installed = root / "rust-home/toolchains/1.99.1-x86_64-unknown-linux-gnu/bin/cargo"
            installed.parent.mkdir(parents=True)
            installed.write_text("#!/bin/sh\nexit 0\n")
            installed.chmod(0o755)
            with patch.dict(os.environ, {"RUSTUP_HOME": str(root / "rust-home")}):
                selected = preflight.rust_binary("cargo", str(cargo), "1.99.1", "x86_64-unknown-linux-gnu")
            self.assertEqual(selected, str(installed))


if __name__ == "__main__":
    unittest.main()
