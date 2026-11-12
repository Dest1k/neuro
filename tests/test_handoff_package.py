"""Real Git and ZIP integration tests for the portable handoff boundary."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "tools"))
import package_handoff as handoff


def git(root, *args):
    return handoff.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                       *args, cwd=root)


def write(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def fixture(root):
    root.mkdir()
    git(root, "init", "-q")
    write(root, "README.md", "")
    write(root, "LICENSE", "Root fixture license\n")
    write(root, "AGENTS.md", "Use the local owner environment. No Actions.\n")
    write(root, "docs/architecture.md", "Fixture architecture\n")
    write(root, "docs/iterations/09.md", "Iteration 9 fixture\n")
    write(root, "docs/incidents/2026-11-12-github-suspension.md", "No Actions after restore.\n")
    write(root, "native/source.cpp", "int answer() { return 42; }\n")
    write(root, ".gitignore", (PROJECT / ".gitignore").read_text() + "\n*.ignored.cpp\n")
    write(root, "state/current.json", json.dumps({
        "preparation_iteration": 9,
        "execution_policy": {"github_actions": handoff.POLICY,
            "active_workflow_files_expected": 0, "applies_after_checkpoint_restore": True,
            "account_remote_or_repository_change_reenables_actions": False}}))
    for name in ("candidate_manifest.py", "package_handoff.py", "neuroctl.py"):
        target = root / "tools" / name
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(PROJECT / "tools" / name, target)
    shutil.copytree(PROJECT / "tools/neuroctl", root / "tools/neuroctl",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    sources = []
    gitmodules = []
    for name, origin in sorted(handoff.PUBLIC_MODULES.items()):
        module = root / name
        module.mkdir(parents=True)
        git(module, "init", "-q")
        write(module, "LICENSE", "Pinned fixture license\n")
        write(module, "source.c", "int donor = 1;\n")
        write(module, "license-link", "LICENSE")
        git(module, "add", ".")
        link = git(module, "hash-object", "license-link")
        git(module, "update-index", "--cacheinfo", "120000," + link + ",license-link")
        git(module, "config", "core.symlinks", "false")
        git(module, "commit", "-qm", "Pinned source")
        pin = git(module, "rev-parse", "HEAD")
        boundaries = [pin]
        if name.endswith("blake3"):
            git(module, "checkout", "--orphan", "extra-shallow-root")
            write(module, "source.c", "int donor = 2;\n")
            git(module, "add", ".")
            git(module, "commit", "-qm", "Second shallow boundary")
            boundaries.append(git(module, "rev-parse", "HEAD"))
            git(module, "checkout", "--detach", pin)
        write(module, ".git/shallow", "\n".join(sorted(boundaries)) + "\n")
        sources.append({"id": name.split("/")[-1], "path": name,
                        "commit": pin, "repository": origin, "imported": True})
        gitmodules.append(f'[submodule "{name}"]\n\tpath = {name}\n\turl = {origin}\n')
        git(root, "update-index", "--add", "--cacheinfo", "160000," + pin + "," + name)
    write(root, ".gitmodules", "".join(gitmodules))
    write(root, "upstream/sources.lock.json", json.dumps({"schema_version": 1, "sources": sources}))
    git(root, "add", ".")
    git(root, "commit", "-qm", "Portable source fixture")
    return git(root, "rev-parse", "HEAD")


class HandoffPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared = tempfile.TemporaryDirectory(prefix="neuro-handoff-fixtures-")
        cls.base = Path(cls.shared.name)
        cls.source = cls.base / "source checkout"
        cls.commit = fixture(cls.source)
        cls.fingerprint = handoff.candidate(cls.source)["candidate_sha256"]
        # These are deliberately untracked/ignored and must never be copied.
        for name in ("configs/local.json", "configs/remote.local.json", "private.key",
                     "api-token.txt", "build/cache.bin", "artifacts/model-budget.sqlite3",
                     "runs/search.journal", "_work/old/lease.json"):
            write(cls.source, name, "MUST_NOT_SHIP_PRIVATE_OR_RUNTIME\n")
        git(cls.source, "config", "http.extraHeader", "Authorization: MUST_NOT_SHIP_CONFIG")
        git(cls.source, "config", "credential.helper", "MUST_NOT_SHIP_HELPER")
        git(cls.source, "config", "remote.origin.url", "https://secret.invalid/PRIVATE_REMOTE")
        write(cls.source, ".git/hooks/post-checkout", "#!/bin/sh\nexit 99\n")
        (cls.source / ".git/hooks/post-checkout").chmod(0o755)
        write(cls.source, ".git/info/exclude", "MUST_NOT_SHIP_INFO\n")
        write(cls.source, ".git/logs/private-note", "MUST_NOT_SHIP_REFLOG\n")
        cls.archive = cls.base / "synthetic-handoff.zip"
        cls.result = handoff.build(cls.source, cls.commit, cls.fingerprint, cls.archive)

    @classmethod
    def tearDownClass(cls):
        cls.shared.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="neuro-handoff-case-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.extract("relocated checkout")

    def extract(self, name):
        target = self.base / name
        target.mkdir(parents=True)
        with zipfile.ZipFile(self.archive) as archive:
            archive.extractall(target)
        return target

    def changed_source(self):
        target = self.base / "builder source"
        shutil.copytree(self.source, target)
        return target

    def rewrite_manifest(self, transform):
        path = self.root / handoff.META / "MANIFEST.json"
        manifest = json.loads(path.read_text())
        transform(manifest)
        path.write_text(json.dumps(manifest), encoding="ascii")
        (path.parent / "MANIFEST.sha256").write_text(handoff.sha256(path) + "  MANIFEST.json\n")

    def test_flat_archive_and_two_relocations_keep_exact_source_identity(self):
        second = self.extract("another path with spaces/start")
        for root in (self.root, second):
            with self.subTest(root=root):
                before = {name: handoff.sha256(path) for name, path in handoff.regular_files(root).items()}
                result = handoff.verify(root)
                self.assertTrue(result["passed"])
                self.assertTrue(result["source_check"]["passed"])
                self.assertEqual(result["candidate_sha256"], self.fingerprint)
                self.assertEqual(result["commit"], self.commit)
                self.assertFalse(result["network_used"])
                self.assertFalse(result["iteration10_launch_archive"])
                after = {name: handoff.sha256(path) for name, path in handoff.regular_files(root).items()}
                self.assertEqual(before, after, "verification must not refresh Git indexes")
        self.assertTrue((self.root / "AGENTS.md").is_file())
        self.assertTrue((self.root / "tools/neuroctl.py").is_file())
        self.assertEqual((self.root / "README.md").stat().st_size, 0)

    def test_embedded_standalone_verifier_works_from_unrelated_cwd(self):
        result = subprocess.run([sys.executable, "-B", "-s", "-E",
            str(self.root / handoff.META / "verify.py"), "verify", "--root", str(self.root)],
            cwd=self.base, env=handoff.clean_env(), capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)["passed"])

    def test_private_runtime_config_and_paths_are_absent(self):
        with zipfile.ZipFile(self.archive) as archive:
            for item in archive.infolist():
                self.assertFalse(item.is_dir())
                self.assertEqual((item.external_attr >> 16) & 0o170000, 0o100000)
                if item.filename not in handoff.META_FILES:
                    self.assertFalse(handoff.private_or_generated(item.filename), item.filename)
                if item.filename.endswith("/.git/config") or item.filename == ".git/config":
                    self.assertEqual(archive.read(item).decode(), handoff.CONFIG)
                self.assertNotIn(b"MUST_NOT_SHIP", archive.read(item), item.filename)
        for relative in (".", *handoff.PUBLIC_MODULES):
            root = self.root / relative
            self.assertEqual(git(root, "remote"), "")
            self.assertFalse((root / ".git/hooks").exists())
            self.assertFalse((root / ".git/logs").exists())
            self.assertFalse((root / ".git/objects/info/alternates").exists())
            self.assertEqual(git(root, "config", "core.symlinks"), "false")
            self.assertEqual(git(root, "config", "core.filemode"), "false")
        self.assertEqual(handoff.candidate(self.source)["candidate_sha256"], self.fingerprint)

    def test_pinned_symlink_text_licenses_and_both_shallow_boundaries_survive(self):
        manifest = handoff.read_json(self.root / handoff.META / "MANIFEST.json")
        self.assertEqual(manifest["root"]["original_origin"], handoff.ROOT_ORIGIN)
        for module in manifest["modules"]:
            checkout = self.root / module["path"]
            link = checkout / "license-link"
            self.assertFalse(link.is_symlink())
            self.assertEqual(link.read_bytes(), b"LICENSE")
            self.assertEqual((checkout / "LICENSE").read_bytes(), b"Pinned fixture license\n")
            self.assertEqual(sorted((checkout / ".git/shallow").read_text().splitlines()),
                             module["shallow_boundaries"])
            self.assertEqual(len(module["shallow_boundaries"]), 2 if module["path"].endswith("blake3") else 1)
            self.assertEqual(git(checkout, "status", "--porcelain"), "")

    def test_source_tamper_is_detected_before_git_or_candidate_execution(self):
        write(self.root, "native/source.cpp", "tampered\n")
        with patch.object(handoff, "git", side_effect=AssertionError("Git must not run")):
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                handoff.verify(self.root)

    def test_addition_and_deletion_are_detected(self):
        extra = write(self.root, "private.key", "never read me")
        with self.assertRaisesRegex(ValueError, "additional or missing"):
            handoff.verify(self.root)
        extra.unlink()
        (self.root / "native/source.cpp").unlink()
        with self.assertRaisesRegex(ValueError, "additional or missing"):
            handoff.verify(self.root)

    def test_git_metadata_tamper_is_detected(self):
        write(self.root, ".git/config", handoff.CONFIG + "[credential]\n\thelper = changed\n")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            handoff.verify(self.root)

    def test_changed_metadata_remains_forbidden_when_manifest_is_recalculated(self):
        path = write(self.root, ".git/config", handoff.CONFIG + "[core]\n\tworktree = /elsewhere\n")
        def change(manifest):
            row = next(row for row in manifest["files"] if row["path"] == ".git/config")
            row.update(bytes=path.stat().st_size, sha256=handoff.sha256(path))
        self.rewrite_manifest(change)
        with self.assertRaisesRegex(ValueError, "sanitized portable profile"):
            handoff.verify(self.root)

    def test_manifest_checksum_and_duplicate_records_are_rejected(self):
        path = self.root / handoff.META / "MANIFEST.json"
        original = path.read_bytes()
        path.write_bytes(original + b" ")
        with self.assertRaisesRegex(ValueError, "manifest checksum mismatch"):
            handoff.verify(self.root)
        path.write_bytes(original)
        self.rewrite_manifest(lambda value: value["files"].append(value["files"][0]))
        with self.assertRaisesRegex(ValueError, "file record"):
            handoff.verify(self.root)

    def test_unsafe_manifest_path_and_link_are_rejected(self):
        self.rewrite_manifest(lambda value: value["files"][0].update(path="../outside"))
        with self.assertRaisesRegex(ValueError, "Unsafe handoff path"):
            handoff.verify(self.root)
        if os.name != "nt":
            root = self.extract("symlink case")
            (root / "escape").symlink_to(self.base)
            with self.assertRaisesRegex(ValueError, "link, special node"):
                handoff.verify(root)

    def test_builder_refuses_dirty_or_untracked_source(self):
        source = self.changed_source()
        write(source, "native/source.cpp", "changed\n")
        for name in (None, "extra.cpp"):
            if name:
                git(source, "checkout", "--", "native/source.cpp")
                write(source, name, "extra\n")
            with self.assertRaisesRegex(ValueError, "clean committed"):
                handoff.build(source, self.commit, self.fingerprint, self.base / "rejected.zip")
            self.assertFalse((self.base / "rejected.zip").exists())

    def test_ignored_public_source_cannot_disappear_from_package(self):
        source = self.changed_source()
        write(source, "native/extra.ignored.cpp", "compiler input\n")
        self.assertEqual(git(source, "status", "--porcelain"), "")
        identity = handoff.candidate(source)["candidate_sha256"]
        with self.assertRaisesRegex(ValueError, "Clean staged sources differ"):
            handoff.build(source, self.commit, identity, self.base / "rejected.zip")
        self.assertFalse((self.base / "rejected.zip").exists())

    def test_readme_and_actions_policy_are_build_gates(self):
        for name, contents in (("README.md", "must stay empty\n"),
                               (".github/workflows/ci.yaml", "name: prohibited\n")):
            with self.subTest(name=name):
                source = self.base / ("readme source" if name == "README.md" else "actions source")
                shutil.copytree(self.source, source)
                write(source, name, contents)
                git(source, "add", name)
                git(source, "commit", "-qm", "Forbidden fixture change")
                commit = git(source, "rev-parse", "HEAD")
                identity = handoff.candidate(source)["candidate_sha256"]
                with self.assertRaisesRegex(ValueError, "README.md|Actions workflows"):
                    handoff.build(source, commit, identity, self.base / "rejected.zip")

    def test_wrong_identity_output_location_and_overwrite_are_refused(self):
        with self.assertRaisesRegex(ValueError, "current HEAD"):
            handoff.build(self.source, "0" * 40, self.fingerprint, self.base / "wrong.zip")
        with self.assertRaisesRegex(ValueError, "requested fingerprint"):
            handoff.build(self.source, self.commit, "0" * 64, self.base / "wrong.zip")
        with self.assertRaisesRegex(ValueError, "new ZIP path outside"):
            handoff.build(self.source, self.commit, self.fingerprint, self.source / "inside.zip")
        before = handoff.sha256(self.archive)
        with self.assertRaisesRegex(ValueError, "new ZIP path outside"):
            handoff.build(self.source, self.commit, self.fingerprint, self.archive)
        self.assertEqual(handoff.sha256(self.archive), before)

    def test_environment_git_redirection_does_not_change_verification(self):
        with patch.dict(os.environ, {"GIT_DIR": str(self.base / "foreign"),
                                     "GIT_WORK_TREE": str(self.base / "elsewhere")}):
            self.assertTrue(handoff.verify(self.root)["passed"])


if __name__ == "__main__":
    unittest.main()
