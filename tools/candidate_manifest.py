#!/usr/bin/env python3
"""Exact public-checkout identity for prepositioned Linux/Windows candidates.

This is a standalone staged verifier: only Python's standard library and Git
are required. It never transfers source contents or hashes private profiles.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import threading

SCHEMA = "neuro.candidate.manifest.v1"
SCOPE = "public_checkout_exact_bytes_v1"
MAX_RECORD = 2 * 1024 * 1024
MAX_FILES = 4096
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
GENERATED_ROOTS = {".git", ".venv", "build", "artifacts", "runs", "_work", "start", "dist"}
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
HEX40 = re.compile(r"[0-9a-f]{40}\Z")


class CandidateError(ValueError):
    """A failed identity boundary. Messages omit private filenames/content."""


def canonical_bytes(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def _path(value: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 512
            or any(ord(c) < 32 or ord(c) > 126 for c in value)
            or any(c in value for c in '\\<>:"|?*')):
        raise CandidateError("Candidate path is not portable between Linux and Windows")
    parts = value.split("/")
    if any(p in ("", ".", "..") or p.lower() == ".git" or p.endswith((" ", "."))
           or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", p)
           for p in parts):
        raise CandidateError("Candidate contains an unsafe relative path")
    return value


def _private(path: str) -> bool:
    name = PurePosixPath(path).name.lower()
    return (name == ".env" or name.startswith(".env.") and name != ".env.example"
            or name.endswith((".key", ".pem", ".pfx", ".local.json"))
            or any("credentials" in part.lower() for part in path.split("/"))
            or ("token" in name and name.endswith(".txt"))
            or path.lower() == "configs/local.json")


def _generated(path: str) -> bool:
    parts = path.split("/")
    return (parts[0] in GENERATED_ROOTS or "__pycache__" in parts
            or path.endswith((".pyc", ".pyo")))


def _git(root: Path, *arguments: str) -> bytes:
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("GIT_"):
            del env[key]
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"})
    output = bytearray()
    read_errors = []
    try:
        process = subprocess.Popen(["git", "--no-optional-locks", "-c", "core.fsmonitor=false",
                                    "-C", str(root), *arguments], stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, env=env)
        def read():
            try:
                output.extend(process.stdout.read(MAX_RECORD + 1))
            except OSError:
                read_errors.append(True)
            finally:
                process.stdout.close()
        thread = threading.Thread(target=read, daemon=True)
        thread.start()
        try:
            code = process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            raise CandidateError("Git metadata command exceeded its bound")
        finally:
            thread.join(timeout=5)
        if thread.is_alive() or read_errors or code != 0 or len(output) > MAX_RECORD:
            raise CandidateError("Git metadata is unavailable or exceeds its bound")
        return bytes(output)
    except OSError as exc:
        raise CandidateError("Git is required for candidate identity") from exc


def _safe_node(root: Path, relative: str, *, permit_leaf_link=False) -> Path:
    current = root
    parts = relative.split("/")
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = current.lstat()
        except OSError as exc:
            raise CandidateError("Candidate file is missing or inaccessible") from exc
        reparse = bool(getattr(info, "st_file_attributes", 0) & 0x400)
        if stat.S_ISLNK(info.st_mode) or reparse:
            if not (permit_leaf_link and index == len(parts) - 1 and current.is_symlink()):
                raise CandidateError("Candidate symlink or reparse point is not allowed")
        elif index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise CandidateError("Candidate ancestor is not a directory")
    return current


def _bytes(root: Path, relative: str, *, git_link=False) -> bytes:
    path = _safe_node(root, relative, permit_leaf_link=git_link)
    before = path.lstat()
    if git_link and path.is_symlink():
        raw = os.readlink(path).encode("utf-8")
    else:
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
            raise CandidateError("Candidate file type or size is outside the bound")
        try:
            with path.open("rb") as stream:
                raw = stream.read(MAX_FILE_BYTES + 1)
        except OSError as exc:
            raise CandidateError("Candidate file cannot be read") from exc
    after = path.lstat()
    if (len(raw) > MAX_FILE_BYTES or (before.st_size, before.st_mtime_ns, before.st_ino)
            != (after.st_size, after.st_mtime_ns, after.st_ino)):
        raise CandidateError("Candidate file changed while being read")
    return raw


def _link_target(root: Path, relative: str, raw: bytes, module: str):
    # Pinned BLAKE3 license links are represented as link-text files by Git for
    # Windows without symlink privilege. Bind the same exact link text in both.
    try:
        target = raw.decode("ascii")
    except UnicodeError as exc:
        raise CandidateError("Pinned link target is not portable") from exc
    if not target or "\\" in target or ":" in target or target.startswith("/"):
        raise CandidateError("Pinned link target is unsafe")
    resolved = (root / relative).parent.joinpath(target).resolve()
    if not resolved.is_relative_to((root / module).resolve()) or not resolved.is_file():
        raise CandidateError("Pinned link must resolve inside its source module")


def _load(path: Path) -> dict:
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_RECORD + 1)
        if len(raw) > MAX_RECORD:
            raise CandidateError("Candidate JSON exceeds its bound")
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise CandidateError("Duplicate candidate JSON field")
                result[key] = value
            return result
        def constant(_):
            raise CandidateError("Non-finite candidate JSON number")
        result = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
        if not isinstance(result, dict):
            raise CandidateError("Candidate JSON must be an object")
        return result
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, CandidateError):
            raise
        raise CandidateError("Candidate JSON cannot be read") from exc


def _inventory(root: Path):
    top = _git(root, "rev-parse", "--show-toplevel").decode("utf-8").strip()
    if Path(top).resolve() != root:
        raise CandidateError("Candidate root must be its own Git checkout")
    tracked = {}
    for record in _git(root, "ls-files", "--stage", "-z").split(b"\0"):
        if not record:
            continue
        head, name = record.split(b"\t", 1)
        mode, oid, stage = head.decode("ascii").split()
        name = _path(name.decode("utf-8"))
        if stage != "0" or name in tracked:
            raise CandidateError("Candidate contains unresolved index entries")
        if _private(name) or _generated(name):
            raise CandidateError("Private or generated files must not be tracked candidate inputs")
        tracked[name] = (mode, oid)
    if len({name.casefold() for name in tracked}) != len(tracked):
        raise CandidateError("Candidate index has case-colliding paths")
    # A new ignored header can still affect a compiler. Inventory all public
    # checkout files rather than trusting .gitignore as a source exclusion list.
    modules = {path for path, (mode, _) in tracked.items() if mode == "160000"}
    names = set(modules)
    pending = [(root, "")]
    scanned_nodes = 0
    while pending:
        directory, prefix = pending.pop()
        with os.scandir(directory) as entries:
            for item in entries:
                scanned_nodes += 1
                if scanned_nodes > MAX_FILES * 2:
                    raise CandidateError("Candidate directory inventory exceeds its bound")
                name = prefix + item.name
                if _private(name) or _generated(name) or name in modules:
                    continue
                _path(name)
                info = item.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise CandidateError("Root candidate symlink or reparse point is unsupported")
                if stat.S_ISDIR(info.st_mode):
                    pending.append((Path(item.path), name + "/"))
                elif stat.S_ISREG(info.st_mode):
                    names.add(name)
                    if len(names) > MAX_FILES:
                        raise CandidateError("Candidate has too many files")
                else:
                    raise CandidateError("Candidate contains a special filesystem node")
    if len(names) > MAX_FILES or len({x.casefold() for x in names}) != len(names):
        raise CandidateError("Candidate has too many files or case-colliding paths")
    return tracked, sorted(names)


def create_manifest(root: Path) -> dict:
    root = Path(root).resolve()
    tracked, names = _inventory(root)
    lock_path = _safe_node(root, "upstream/sources.lock.json")
    lock = _load(lock_path)
    if lock.get("schema_version") != 1 or not isinstance(lock.get("sources"), list):
        raise CandidateError("Invalid pinned source lock")
    pins = []
    for source in lock["sources"]:
        if not isinstance(source, dict):
            raise CandidateError("Invalid pinned source record")
        if source.get("imported") is not True:
            continue
        path = _path(source.get("path"))
        commit = source.get("commit")
        if not path.startswith("upstream/") or not isinstance(commit, str) or not HEX40.fullmatch(commit):
            raise CandidateError("Invalid pinned source identity")
        pins.append({"path": path, "commit": commit})
    pins.sort(key=lambda row: row["path"])
    if len({p["path"] for p in pins}) != len(pins):
        raise CandidateError("Duplicate pinned source")
    gitlinks = {path: oid for path, (mode, oid) in tracked.items() if mode == "160000"}
    if gitlinks != {p["path"]: p["commit"] for p in pins}:
        raise CandidateError("Pinned sources and candidate gitlinks differ")
    rows = []
    total = 0
    def append(path, raw, kind="file"):
        nonlocal total
        total += len(raw)
        if len(rows) >= MAX_FILES or total > MAX_TOTAL_BYTES:
            raise CandidateError("Candidate source exceeds its total bound")
        rows.append({"path": path, "kind": kind, "bytes": len(raw),
                     "sha256": hashlib.sha256(raw).hexdigest()})
    for name in names:
        if name in gitlinks:
            continue
        if name in tracked and tracked[name][0] not in ("100644", "100755"):
            raise CandidateError("Root candidate links or special index modes are unsupported")
        # An unstaged deletion is a legitimate dirty candidate; the target must
        # have the same absence. Added, ignored-in-index and modified files bind.
        if name in tracked and not (root / name).exists() and not (root / name).is_symlink():
            continue
        append(name, _bytes(root, name))
    for pin in pins:
        module = _safe_node(root, pin["path"])
        if Path(_git(module, "rev-parse", "--show-toplevel").decode("utf-8").strip()).resolve() != module.resolve():
            raise CandidateError("Pinned source Git metadata points at another worktree")
        if _git(module, "rev-parse", "HEAD").decode("ascii").strip() != pin["commit"]:
            raise CandidateError("Pinned source HEAD differs")
        if _git(module, "status", "--porcelain", "--untracked-files=all", "--ignored"):
            raise CandidateError("Pinned source checkout is modified")
        for record in _git(module, "ls-tree", "-rz", "--full-tree", "HEAD").split(b"\0"):
            if not record:
                continue
            head, name = record.split(b"\t", 1)
            mode, kind, oid = head.decode("ascii").split()
            if kind != "blob" or mode not in ("100644", "100755", "120000"):
                raise CandidateError("Nested pinned submodule or special source mode is unsupported")
            name = _path(pin["path"] + "/" + name.decode("utf-8"))
            raw = _bytes(root, name, git_link=mode == "120000")
            actual = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
            if actual != oid:
                raise CandidateError("Pinned source bytes differ from the committed blob")
            if mode == "120000":
                _link_target(root, name, raw, pin["path"])
            append(name, raw, "git-symlink" if mode == "120000" else "file")
    rows.sort(key=lambda row: row["path"])
    if len({row["path"].casefold() for row in rows}) != len(rows):
        raise CandidateError("Candidate has case-colliding source paths")
    if _inventory(root) != (tracked, names):
        raise CandidateError("Candidate inventory changed during hashing")
    payload = {"schema": SCHEMA, "scope": SCOPE, "files": rows, "submodules": pins}
    result = {**payload, "candidate_sha256": hashlib.sha256(canonical_bytes(payload)).hexdigest()}
    if len(canonical_bytes(result)) > MAX_RECORD:
        raise CandidateError("Candidate manifest exceeds its bound")
    return result


def validate_manifest(value: dict) -> dict:
    if (not isinstance(value, dict) or set(value) != {"schema", "scope", "files", "submodules", "candidate_sha256"}
            or value["schema"] != SCHEMA or value["scope"] != SCOPE
            or not isinstance(value["candidate_sha256"], str) or not HEX64.fullmatch(value["candidate_sha256"])
            or not isinstance(value["files"], list) or not 1 <= len(value["files"]) <= MAX_FILES
            or not isinstance(value["submodules"], list) or len(value["submodules"]) > 16):
        raise CandidateError("Invalid candidate manifest schema")
    previous = ""
    folded = set()
    total = 0
    for row in value["files"]:
        if not isinstance(row, dict) or set(row) != {"path", "kind", "bytes", "sha256"}:
            raise CandidateError("Invalid candidate file record")
        path = _path(row["path"])
        if (path <= previous or path.casefold() in folded or _private(path) or _generated(path)
                or row["kind"] not in ("file", "git-symlink")
                or type(row["bytes"]) is not int or not 0 <= row["bytes"] <= MAX_FILE_BYTES
                or not isinstance(row["sha256"], str) or not HEX64.fullmatch(row["sha256"])):
            raise CandidateError("Invalid candidate file identity/order")
        previous = path
        folded.add(path.casefold())
        total += row["bytes"]
    previous = ""
    for row in value["submodules"]:
        if (not isinstance(row, dict) or set(row) != {"path", "commit"}
                or _path(row["path"]) <= previous or not row["path"].startswith("upstream/")
                or not isinstance(row["commit"], str) or not HEX40.fullmatch(row["commit"])):
            raise CandidateError("Invalid candidate pinned source record")
        previous = row["path"]
    payload = {k: value[k] for k in ("schema", "scope", "files", "submodules")}
    if total > MAX_TOTAL_BYTES or hashlib.sha256(canonical_bytes(payload)).hexdigest() != value["candidate_sha256"]:
        raise CandidateError("Candidate manifest digest does not match its contents")
    return value


def verify(root: Path, manifest: dict, expected: str) -> dict:
    validate_manifest(manifest)
    if expected != manifest["candidate_sha256"]:
        raise CandidateError("Candidate manifest does not belong to this request")
    actual = create_manifest(root)
    passed = actual == manifest
    expected_files = {row['path']: row for row in manifest['files']}
    actual_files = {row['path']: row for row in actual['files']}
    mismatched = sorted(path for path in expected_files.keys() | actual_files.keys()
                        if expected_files.get(path) != actual_files.get(path))
    return {"schema": "neuro.candidate.verification.v1", "passed": passed,
            "candidate_sha256": expected, "actual_sha256": actual["candidate_sha256"],
            "files": len(actual["files"]), "mismatched_count": len(mismatched),
            "mismatched_paths": mismatched[:5], "error": None if passed else "candidate_mismatch"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "fingerprint", "verify"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--expected")
    args = parser.parse_args(argv)
    try:
        if args.action == "verify":
            if args.manifest is None or args.expected is None:
                raise CandidateError("Verification needs a manifest and expected identity")
            result = verify(args.root, _load(args.manifest), args.expected)
        else:
            value = create_manifest(args.root)
            if args.action == "create":
                if args.output is None:
                    raise CandidateError("Creation needs an output path outside public candidate inputs")
                target = args.output.resolve()
                root = args.root.resolve()
                if target.is_relative_to(root):
                    relative = target.relative_to(root).as_posix()
                    if relative.split('/')[0] not in {"artifacts", "build", "runs", "_work", "dist"}:
                        raise CandidateError("Manifest output must be outside public candidate inputs")
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_bytes(canonical_bytes(value) + b"\n")
            result = {"schema": "neuro.candidate.fingerprint.v1", "passed": True,
                      "candidate_sha256": value["candidate_sha256"], "files": len(value["files"]),
                      "bytes": sum(row["bytes"] for row in value["files"]), "submodules": len(value["submodules"])}
    except (CandidateError, OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        result = {"schema": "neuro.candidate.verification.v1", "passed": False,
                  "candidate_sha256": args.expected, "actual_sha256": None,
                  "error": str(exc) if isinstance(exc, CandidateError) else "candidate_io_or_schema_failure"}
    print(json.dumps(result, separators=(",", ":")))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
