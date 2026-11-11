"""Real temporary Git checkouts exercise cross-host source identity boundaries."""
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import candidate_manifest as candidate


def git(root, *args):
    return subprocess.check_output(["git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
                                    "-c", "core.autocrlf=false", "-C", str(root), *args], stderr=subprocess.DEVNULL).decode().strip()


def init(root):
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q")
    git(root, "config", "core.autocrlf", "false")
    git(root, "config", "core.ignorecase", "false")


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="neuro-candidate-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "candidate with spaces"
        init(self.root)
        self.write("upstream/sources.lock.json", json.dumps({"schema_version": 1, "sources": []}))
        self.write("native/kernel.cu", "// exact source\n")
        self.write(".gitignore", "/artifacts/\n*.local.json\n")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "fixture")

    def write(self, name, value):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(value.encode() if isinstance(value, str) else value)
        return p

    def manifest(self):
        return candidate.create_manifest(self.root)

    def pin(self, *, link=False):
        module = self.root / "upstream/donor"
        init(module)
        (module / "value.c").write_bytes(b"int value = 42;\n")
        if link:
            # Git for Windows may represent symlinks as plain link-text files.
            (module / "alias").write_bytes(b"value.c")
        git(module, "add", ".")
        if link:
            oid = git(module, "hash-object", "alias")
            git(module, "update-index", "--cacheinfo", "120000," + oid + ",alias")
            git(module, "config", "core.symlinks", "false")
        git(module, "commit", "-qm", "donor")
        commit = git(module, "rev-parse", "HEAD")
        git(self.root, "update-index", "--add", "--cacheinfo", "160000," + commit + ",upstream/donor")
        self.write("upstream/sources.lock.json", json.dumps({"schema_version": 1, "sources": [
            {"id": "donor", "path": "upstream/donor", "commit": commit, "imported": True}]}))
        return module

    def test_exact_canonical_bytes_are_order_independent_ascii(self):
        self.assertEqual(candidate.canonical_bytes({"z": [2, 1], "a": "\u0410"}),
                         b'{"a":"\\u0410","z":[2,1]}')
        original = self.manifest()
        self.assertEqual(candidate.validate_manifest(original), original)
        with self.assertRaises(ValueError):
            candidate.canonical_bytes({"x": float("nan")})

    def test_relocated_checkout_has_identical_identity_despite_absolute_path_and_mtime(self):
        original = self.manifest()
        relocated = Path(self.temp.name) / "another checkout"
        shutil.copytree(self.root, relocated)
        os.utime(relocated / "native/kernel.cu", (1, 1))
        self.assertEqual(candidate.create_manifest(relocated), original)

    def test_dirty_tracked_source_changes_identity_without_needing_commit(self):
        original = self.manifest()
        self.write("native/kernel.cu", "// candidate change\n")
        current = self.manifest()
        self.assertNotEqual(current["candidate_sha256"], original["candidate_sha256"])
        result = candidate.verify(self.root, original, original["candidate_sha256"])
        self.assertFalse(result["passed"])
        self.assertEqual(result["actual_sha256"], current["candidate_sha256"])

    def test_untracked_and_even_ignored_source_are_bound(self):
        self.write(".gitignore", "*.cu\n/artifacts/\n")
        original = self.manifest()
        self.write("native/new_ignored.cu", "// compiler can include this\n")
        current = self.manifest()
        self.assertNotEqual(original["candidate_sha256"], current["candidate_sha256"])
        self.assertIn("native/new_ignored.cu", [row["path"] for row in current["files"]])

    def test_unstaged_deletion_is_bound_and_remote_extra_file_is_detected(self):
        before = self.manifest()
        (self.root / "native/kernel.cu").unlink()
        deleted = self.manifest()
        self.assertNotEqual(before["candidate_sha256"], deleted["candidate_sha256"])
        self.write("native/kernel.cu", "// exact source\n")
        self.assertFalse(candidate.verify(self.root, deleted, deleted["candidate_sha256"])["passed"])

    def test_generated_output_and_private_profiles_are_not_read_or_fingerprinted(self):
        before = self.manifest()
        private = self.write("configs/gpu-owner.local.json", "PRIVATE_OWNER_CONFIGURATION")
        self.write("configs/local.json", "PRIVATE_ENDPOINT")
        self.write("artifacts/proof.json", "runtime output")
        self.write("build/new.o", "object")
        opened = Path.open
        def guard(path, *args, **kwargs):
            if path == private:
                raise AssertionError("private profile contents were opened")
            return opened(path, *args, **kwargs)
        with patch.object(Path, "open", guard):
            self.assertEqual(self.manifest(), before)

    def test_force_tracked_private_or_generated_file_is_rejected_before_hashing(self):
        for name in ("configs/gpu-owner.local.json", "artifacts/run.json"):
            with self.subTest(name=name):
                self.write(name, "secret or generated")
                git(self.root, "add", "-f", name)
                with self.assertRaises(candidate.CandidateError):
                    self.manifest()
                git(self.root, "rm", "--cached", "-q", name)

    def test_root_symlink_cannot_escape_or_hide_candidate_inputs(self):
        target = Path(self.temp.name) / "outside.cu"
        target.write_bytes(b"outside")
        try:
            (self.root / "native/alias.cu").symlink_to(target)
        except OSError:
            self.skipTest("creating a symlink requires Windows developer mode or privilege")
        with self.assertRaises(candidate.CandidateError):
            self.manifest()

    def test_case_colliding_index_is_rejected_even_on_case_insensitive_target(self):
        oid = git(self.root, "hash-object", "native/kernel.cu")
        git(self.root, "update-index", "--add", "--cacheinfo", "100644," + oid + ",native/Kernel.cu")
        with self.assertRaises(candidate.CandidateError):
            self.manifest()

    def test_root_git_environment_cannot_redirect_inventory(self):
        before = self.manifest()
        with patch.dict(os.environ, {"GIT_DIR": str(Path(self.temp.name) / "missing"),
                                     "GIT_WORK_TREE": str(Path(self.temp.name) / "foreign")}):
            self.assertEqual(self.manifest(), before)

    def test_pinned_module_actual_bytes_and_commit_are_in_manifest(self):
        module = self.pin()
        value = self.manifest()
        self.assertEqual(value["submodules"], [{"path": "upstream/donor", "commit": git(module, "rev-parse", "HEAD")}])
        row = next(row for row in value["files"] if row["path"] == "upstream/donor/value.c")
        self.assertEqual(row["sha256"], hashlib.sha256(b"int value = 42;\n").hexdigest())

    def test_modified_pinned_source_is_rejected_even_when_assume_unchanged(self):
        module = self.pin()
        git(module, "update-index", "--assume-unchanged", "value.c")
        (module / "value.c").write_bytes(b"int value = 43;\n")
        self.assertEqual(git(module, "status", "--porcelain"), "")
        with self.assertRaises(candidate.CandidateError):
            self.manifest()

    def test_pinned_untracked_file_is_rejected(self):
        module = self.pin()
        (module / "extra.h").write_bytes(b"changed compiler include")
        with self.assertRaises(candidate.CandidateError):
            self.manifest()

    def test_ignored_extra_header_inside_pinned_module_is_rejected(self):
        module = self.pin()
        (module / ".git/info/exclude").write_text("hidden.h\n")
        (module / "hidden.h").write_bytes(b"compiler input hidden from normal git status")
        self.assertEqual(git(module, "status", "--porcelain", "--untracked-files=all"), "")
        with self.assertRaises(candidate.CandidateError):
            self.manifest()

    def test_different_pinned_head_is_rejected(self):
        module = self.pin()
        git(module, "commit", "--allow-empty", "-qm", "foreign head")
        with self.assertRaises(candidate.CandidateError):
            self.manifest()

    def test_source_lock_and_index_gitlink_must_agree(self):
        self.pin()
        self.write("upstream/sources.lock.json", json.dumps({"schema_version": 1, "sources": []}))
        with self.assertRaises(candidate.CandidateError):
            self.manifest()

    def test_pinned_link_text_has_portable_semantics(self):
        module = self.pin(link=True)
        text_manifest = self.manifest()
        row = next(row for row in text_manifest["files"] if row["path"].endswith("/alias"))
        self.assertEqual(row["kind"], "git-symlink")
        if os.name != "nt":
            (module / "alias").unlink()
            (module / "alias").symlink_to("value.c")
            git(module, "config", "core.symlinks", "true")
            self.assertEqual(self.manifest(), text_manifest)

    def test_pinned_link_cannot_resolve_outside_module(self):
        module = self.pin(link=True)
        (module / "alias").write_bytes(b"../../native/kernel.cu")
        git(module, "add", "alias")
        git(module, "commit", "-qm", "unsafe link")
        commit = git(module, "rev-parse", "HEAD")
        git(self.root, "update-index", "--cacheinfo", "160000," + commit + ",upstream/donor")
        self.write("upstream/sources.lock.json", json.dumps({"schema_version": 1, "sources": [
            {"path": "upstream/donor", "commit": commit, "imported": True}]}))
        with self.assertRaises(candidate.CandidateError):
            self.manifest()

    def test_bad_paths_and_forged_or_incomplete_manifest_rejected(self):
        original = self.manifest()
        for name in ("../escape", "C:/escape", "native\\file", "native/NUL.txt", "native/ends.", "native/line\n"):
            with self.subTest(name=name), self.assertRaises(candidate.CandidateError):
                candidate._path(name)
        for change in ({"candidate_sha256": "0" * 64}, {"files": []}, {"unknown": True}):
            with self.subTest(change=change), self.assertRaises(candidate.CandidateError):
                candidate.validate_manifest({**original, **change})
        with self.assertRaises(candidate.CandidateError):
            candidate.verify(self.root, original, "0" * 64)
        duplicate = copy.deepcopy(original)
        duplicate["files"].append(duplicate["files"][0])
        with self.assertRaises(candidate.CandidateError):
            candidate.validate_manifest(duplicate)

    def test_cli_persists_manifest_and_verifies_exact_requested_identity(self):
        path = self.root / "artifacts/candidate.json"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(candidate.main(["create", "--root", str(self.root), "--output", str(path)]), 0)
        value = json.loads(path.read_text())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(candidate.main(["verify", "--root", str(self.root), "--manifest", str(path),
                                             "--expected", value["candidate_sha256"]]), 0)
        self.write("native/kernel.cu", "changed\n")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(candidate.main(["verify", "--root", str(self.root), "--manifest", str(path),
                                             "--expected", value["candidate_sha256"]]), 1)

    def test_manifest_cannot_write_itself_into_public_source_or_git_metadata(self):
        for name in ("candidate.json", ".git/candidate.json"):
            target = self.root / name
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(candidate.main(["create", "--root", str(self.root), "--output", str(target)]), 1)
            self.assertFalse(target.exists())

    def test_duplicate_or_oversize_json_fails_closed(self):
        path = Path(self.temp.name) / "input.json"
        for text in ('{"files":[],"files":[]}', '{"x":NaN}', " " * (candidate.MAX_RECORD + 1)):
            path.write_text(text)
            with self.assertRaises(candidate.CandidateError):
                candidate._load(path)


if __name__ == "__main__":
    unittest.main()
