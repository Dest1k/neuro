#!/usr/bin/env python3
"""Build or verify a flat, offline neuro handoff checkout (Python 3.11 + Git)."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile

SCHEMA = "neuro.handoff.package.v1"
META = "_work/handoff"
ROOT_ORIGIN = "https://github.com/alinescafs3mp-afk/neuro.git"
PUBLIC_MODULES = {
    "upstream/cppminer": "https://github.com/1640675651/CPPminer.git",
    "upstream/blake3": "https://github.com/BLAKE3-team/BLAKE3.git",
}
POLICY = "disabled_for_all_project_workloads_including_self_hosted_runners"
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
MAX_FILES = 10000
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
CONFIG = ("[core]\n\trepositoryformatversion = 0\n\tbare = false\n"
          "\tautocrlf = false\n\teol = lf\n\tsymlinks = false\n"
          "\tfilemode = false\n\tlogAllRefUpdates = false\n")
GENERATED = {"build", "artifacts", "runs", ".venv", "start", "dist", "_work"}
META_FILES = {META + "/" + name for name in
              ("MANIFEST.json", "MANIFEST.sha256", "candidate.json", "verify.py")}


def sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def clean_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith(("GIT_", "PYTHON"))}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
                "GIT_ALLOW_PROTOCOL": "file", "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_ATTR_NOSYSTEM": "1", "GIT_OPTIONAL_LOCKS": "0",
                "LC_ALL": "C", "LANG": "C"})
    return env


def run(argv: list[str], *, cwd: Path | None = None) -> str:
    result = subprocess.run(argv, cwd=cwd, env=clean_env(), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=120, check=False)
    if result.returncode:
        # Keep errors useful without echoing local Git configuration or commands.
        raise RuntimeError(f"{Path(argv[0]).name} failed ({result.returncode}): "
                           + result.stderr.decode("utf-8", "replace")[-2000:])
    if len(result.stdout) > 4 * 1024 * 1024 or len(result.stderr) > 4 * 1024 * 1024:
        raise ValueError("Command output exceeds handoff bound")
    return result.stdout.decode("utf-8").strip()


def git(*args: str, cwd: Path | None = None) -> str:
    return run(["git", "--no-optional-locks", "-c", "core.autocrlf=false",
                "-c", "core.eol=lf", "-c", "core.hooksPath=" + os.devnull,
                "-c", "core.fsmonitor=false", "-c", "core.logAllRefUpdates=false",
                "-c", "protocol.allow=never", "-c", "protocol.file.allow=always",
                *map(str, args)], cwd=cwd)


def read_json(path: Path) -> dict:
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("Handoff JSON exceeds bound")
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("Duplicate handoff JSON field")
            value[key] = item
        return value
    def constant(_):
        raise ValueError("Non-finite handoff JSON number")
    value = json.loads(path.read_bytes(), object_pairs_hook=pairs, parse_constant=constant)
    if not isinstance(value, dict):
        raise ValueError("Handoff JSON must be an object")
    return value


def safe_path(value: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 512
            or any(ord(c) < 32 or ord(c) > 126 for c in value)
            or any(c in value for c in '\\<>:"|?*')):
        raise ValueError("Non-portable handoff path")
    if any(part in ("", ".", "..") or part.endswith((" ", "."))
           or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part)
           for part in value.split("/")):
        raise ValueError("Unsafe handoff path")
    return value


def regular_files(root: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    folded = set()
    nodes = 0
    total = 0
    for directory, directories, names in os.walk(root, followlinks=False):
        for name in directories + names:
            nodes += 1
            path = Path(directory) / name
            info = path.lstat()
            relative = safe_path(path.relative_to(root).as_posix())
            if (nodes > MAX_FILES * 2 or stat.S_ISLNK(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & 0x400
                    or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))):
                raise ValueError("Handoff contains a link, special node, or excessive inventory")
            if relative.casefold() in folded:
                raise ValueError("Case-colliding handoff paths")
            folded.add(relative.casefold())
        for name in names:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            size = path.stat().st_size
            files[relative] = path
            total += size
            if len(files) > MAX_FILES or size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                raise ValueError("Handoff exceeds size bound")
    return files


def private_or_generated(name: str) -> bool:
    leaf = PurePosixPath(name).name.lower()
    return (name.split("/")[0] in GENERATED or "__pycache__" in name.split("/")
            or name.endswith((".pyc", ".pyo"))
            or leaf == ".env" or leaf.startswith(".env.") and leaf != ".env.example"
            or leaf.endswith((".key", ".pem", ".pfx", ".local.json"))
            or any("credentials" in part.lower() for part in name.split("/"))
            or "token" in leaf and leaf.endswith(".txt")
            or name.lower() == "configs/local.json")


def assert_policy(root: Path) -> int:
    readme = root / "README.md"
    if readme.is_symlink() or not readme.is_file() or readme.stat().st_size != 0:
        raise ValueError("README.md must remain present and zero bytes")
    workflows = root / ".github/workflows"
    if (root / ".github").is_symlink() or workflows.is_symlink():
        raise ValueError("Workflow directory must not be a link")
    if workflows.exists() and (not workflows.is_dir() or any(
            path.is_symlink() or path.suffix.lower() in {".yml", ".yaml"}
            for path in workflows.rglob("*"))):
        raise ValueError("Root GitHub Actions workflows must remain absent")
    state = read_json(root / "state/current.json")
    iteration = state.get("preparation_iteration")
    if type(iteration) is not int or iteration not in (9, 10):
        raise ValueError("Flat handoff requires iteration 9 rehearsal or iteration 10 state")
    policy = state.get("execution_policy", {})
    if (policy.get("github_actions") != POLICY
            or policy.get("active_workflow_files_expected") != 0
            or policy.get("applies_after_checkpoint_restore") is not True
            or policy.get("account_remote_or_repository_change_reenables_actions") is not False):
        raise ValueError("Post-incident execution policy is missing or changed")
    for name in ("AGENTS.md", "LICENSE", "docs/architecture.md",
                 f"docs/iterations/{iteration:02}.md",
                 "docs/incidents/2026-11-12-github-suspension.md"):
        if not (root / name).is_file():
            raise ValueError("Required handoff instruction or license is missing: " + name)
    return iteration


def pins(root: Path) -> list[dict]:
    lock = read_json(root / "upstream/sources.lock.json")
    values = [source for source in lock["sources"] if source.get("imported") is True]
    if (len(values) != 2 or {source.get("path") for source in values} != set(PUBLIC_MODULES)):
        raise ValueError("Expected the two pinned upstream sources")
    for source in values:
        name, commit = source["path"], source.get("commit", "")
        if source.get("repository") != PUBLIC_MODULES[name] or not HEX40.fullmatch(commit):
            raise ValueError("Unexpected upstream provenance or pin")
        if git("ls-tree", "HEAD", "--", name, cwd=root) != f"160000 commit {commit}\t{name}":
            raise ValueError("Source lock and committed gitlink differ")
    return sorted(values, key=lambda source: source["path"])


def candidate(root: Path, output: Path | None = None) -> dict:
    argv = [sys.executable, "-B", "-s", "-E", str(root / "tools/candidate_manifest.py"),
            "create" if output else "fingerprint", "--root", str(root)]
    if output is not None:
        argv.extend(["--output", str(output)])
    result = json.loads(run(argv, cwd=root))
    if result.get("passed") is not True:
        raise ValueError("Candidate identity failed")
    return result


def assert_current(root: Path, commit: str, expected: str) -> int:
    if git("rev-parse", "HEAD", cwd=root) != commit:
        raise ValueError("Requested commit is not the current HEAD")
    if git("status", "--porcelain", "--untracked-files=all", cwd=root):
        raise ValueError("Handoff requires a clean committed source checkout")
    if candidate(root)["candidate_sha256"] != expected:
        raise ValueError("Source candidate differs from the requested fingerprint")
    return assert_policy(root)


def remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def snapshot_module(source: Path, target: Path, pin: str, template: Path) -> list[str]:
    if git("rev-parse", "HEAD", cwd=source) != pin:
        raise ValueError("Upstream HEAD differs from source pin")
    if git("status", "--porcelain", "--untracked-files=all", "--ignored", cwd=source):
        raise ValueError("Upstream checkout is dirty")
    source_git = Path(git("rev-parse", "--absolute-git-dir", cwd=source))
    boundaries = sorted((source_git / "shallow").read_text(encoding="ascii").splitlines())
    if not boundaries or any(not HEX40.fullmatch(value) for value in boundaries):
        raise ValueError("Expected bounded shallow upstream history")
    git("clone", "--bare", "--local", "--no-hardlinks", "--template=" + str(template),
        "--", str(source), str(target))
    if sorted((target / "shallow").read_text(encoding="ascii").splitlines()) != boundaries:
        raise ValueError("Upstream shallow boundaries changed")
    retained = []
    for line in git("--git-dir=" + str(target), "for-each-ref", "--format=%(refname) %(objectname)").splitlines():
        ref, oid = line.split(" ")
        if not ref.startswith(("refs/heads/", "refs/tags/")) or not HEX40.fullmatch(oid):
            raise ValueError("Unexpected upstream reference")
        retained.append((ref, oid))
    if any((target / "objects/info" / name).exists() for name in ("alternates", "http-alternates")):
        raise ValueError("External object stores are not portable")
    if list((target / "objects").rglob("*.promisor")):
        raise ValueError("Partial clones cannot be offline handoffs")
    for child in list(target.iterdir()):
        if child.name not in {"objects", "shallow"}:
            remove(child)
    if (target / "objects/info").exists():
        remove(target / "objects/info")
    for marker in (target / "objects/pack").glob("*.keep"):
        marker.unlink()
    (target / "refs/heads").mkdir(parents=True)
    (target / "refs/heads/handoff").write_text(pin + "\n", encoding="ascii", newline="\n")
    (target / "HEAD").write_text("ref: refs/heads/handoff\n", encoding="ascii", newline="\n")
    (target / "config").write_text("[core]\n\trepositoryformatversion = 0\n\tbare = true\n",
                                    encoding="ascii", newline="\n")
    for ref, oid in retained:
        git("--git-dir=" + str(target), "update-ref", ref, oid)
    git("--git-dir=" + str(target), "fsck", "--full", "--no-reflogs", "--no-dangling")
    return boundaries


def sanitize_checkout(root: Path) -> None:
    metadata = root / ".git"
    if not metadata.is_dir() or metadata.is_symlink():
        raise ValueError("Handoff requires a self-contained .git directory")
    for path in list(metadata.iterdir()):
        if path.name not in {"HEAD", "config", "index", "objects", "refs", "packed-refs", "shallow"}:
            remove(path)
    if (metadata / "objects/info").exists():
        remove(metadata / "objects/info")
    for path in (metadata / "objects/pack").glob("*.keep"):
        path.unlink()
    (metadata / "config").write_text(CONFIG, encoding="ascii", newline="\n")
    # No remote-tracking refs or checkout paths are needed in a portable handoff.
    for ref in git("for-each-ref", "--format=%(refname)", cwd=root).splitlines():
        if not ref.startswith(("refs/heads/", "refs/tags/")):
            git("update-ref", "--no-deref", "-d", ref, cwd=root)
    if (metadata / "refs/remotes").exists():
        remove(metadata / "refs/remotes")


def check_git(root: Path, commit: str, boundaries: list[str] | None) -> None:
    metadata = root / ".git"
    if not metadata.is_dir() or metadata.is_symlink():
        raise ValueError("Git metadata is not an embedded directory")
    if (metadata / "config").read_text(encoding="ascii") != CONFIG:
        raise ValueError("Git configuration is not the sanitized portable profile")
    for name, path in regular_files(metadata).items():
        allowed = (name in {"HEAD", "config", "index", "packed-refs", "shallow"}
                   or name.startswith(("refs/heads/", "refs/tags/"))
                   or re.fullmatch(r"objects/[0-9a-f]{2}/[0-9a-f]{38}", name)
                   or re.fullmatch(r"objects/pack/pack-[0-9a-f]{40}\.(pack|idx|rev|bitmap)", name))
        if not allowed or path.stat().st_nlink != 1:
            raise ValueError("Unexpected or externally shared Git metadata: " + name)
    if Path(git("rev-parse", "--show-toplevel", cwd=root)).resolve() != root.resolve():
        raise ValueError("Git metadata points at a different worktree")
    if git("rev-parse", "HEAD", cwd=root) != commit or git("remote", cwd=root):
        raise ValueError("Git identity changed or an active remote is present")
    if git("status", "--porcelain", "--untracked-files=all", cwd=root):
        raise ValueError("Extracted checkout is dirty")
    shallow = metadata / "shallow"
    actual = sorted(shallow.read_text(encoding="ascii").splitlines()) if shallow.exists() else None
    if actual != boundaries:
        raise ValueError("Shallow history boundary changed")
    git("fsck", "--full", "--no-reflogs", "--no-dangling", cwd=root)


def verify(root: Path) -> dict:
    root = root.resolve(strict=True)
    actual = regular_files(root)  # Reject links before reading metadata or executing Git.
    manifest_name, checksum_name = META + "/MANIFEST.json", META + "/MANIFEST.sha256"
    if manifest_name not in actual or checksum_name not in actual:
        raise ValueError("Handoff manifest is missing")
    expected_line = sha256(actual[manifest_name]) + "  MANIFEST.json\n"
    if actual[checksum_name].read_text(encoding="ascii") != expected_line:
        raise ValueError("Handoff manifest checksum mismatch")
    manifest = read_json(actual[manifest_name])
    if (manifest.get("schema") != SCHEMA or manifest.get("flat_checkout") is not True
            or manifest.get("execution_policy") != POLICY
            or manifest.get("iteration") not in (9, 10)
            or manifest.get("iteration10_launch_archive") != (manifest.get("iteration") == 10)):
        raise ValueError("Invalid handoff schema or execution policy")
    entries = manifest.get("files")
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_FILES:
        raise ValueError("Invalid handoff inventory")
    recorded = {}
    for row in entries:
        name = safe_path(row["path"])
        if (name in recorded or type(row.get("bytes")) is not int
                or not 0 <= row["bytes"] <= MAX_FILE_BYTES
                or not HEX64.fullmatch(row.get("sha256", ""))):
            raise ValueError("Invalid handoff file record")
        recorded[name] = row
    if set(actual) != set(recorded) | {manifest_name, checksum_name}:
        raise ValueError("Handoff has additional or missing files")
    for name, row in recorded.items():
        if actual[name].stat().st_size != row["bytes"] or sha256(actual[name]) != row["sha256"]:
            raise ValueError("Handoff file checksum mismatch: " + name)
        if name not in META_FILES and private_or_generated(name):
            raise ValueError("Private or generated files are not handoff payload")
    if not META_FILES.issubset(actual):
        raise ValueError("Required standalone verifier or candidate manifest is missing")
    identity = manifest.get("root", {})
    commit, expected = identity.get("commit", ""), manifest.get("candidate_sha256", "")
    if (not HEX40.fullmatch(commit) or not HEX64.fullmatch(expected)
            or identity.get("original_origin") != ROOT_ORIGIN
            or identity.get("origin_usable") is not False
            or identity.get("active_remotes") != []):
        raise ValueError("Invalid root source identity")
    if assert_policy(root) != manifest["iteration"]:
        raise ValueError("Handoff iteration differs from project state")
    check_git(root, commit, None)
    if int(git("rev-list", "--count", "HEAD", cwd=root)) != identity.get("reachable_commit_count"):
        raise ValueError("Root reachable history changed")
    modules = manifest.get("modules")
    lock = pins(root)
    if not isinstance(modules, list) or len(modules) != 2:
        raise ValueError("Invalid module inventory")
    for source, module in zip(lock, modules):
        boundaries = module.get("shallow_boundaries")
        if (module.get("path") != source["path"] or module.get("commit") != source["commit"]
                or module.get("origin") != PUBLIC_MODULES[source["path"]]
                or not isinstance(boundaries, list) or not boundaries
                or any(not isinstance(value, str) or not HEX40.fullmatch(value) for value in boundaries)):
            raise ValueError("Module provenance or shallow history is invalid")
        check_git(root / source["path"], source["commit"], boundaries)
    source_check = json.loads(run([sys.executable, "-B", "-s", "-E",
                                  str(root / "tools/neuroctl.py"), "source-check"], cwd=root))
    if source_check.get("passed") is not True:
        raise ValueError("Pinned source check failed")
    fingerprint = candidate(root)
    candidate_record = read_json(root / META / "candidate.json")
    if fingerprint["candidate_sha256"] != expected or candidate_record.get("candidate_sha256") != expected:
        raise ValueError("Source fingerprint changed after extraction")
    candidate_check = json.loads(run([sys.executable, "-B", "-s", "-E",
        str(root / "tools/candidate_manifest.py"), "verify", "--root", str(root),
        "--manifest", str(root / META / "candidate.json"), "--expected", expected], cwd=root))
    if candidate_check.get("passed") is not True:
        raise ValueError("Full candidate inventory changed")
    return {"schema": "neuro.handoff.verification.v1", "passed": True,
            "iteration": manifest["iteration"], "commit": commit,
            "candidate_sha256": expected, "payload_files": len(actual),
            "source_check": source_check, "active_remotes": [], "network_used": False,
            "iteration10_launch_archive": manifest["iteration10_launch_archive"]}


def build(root: Path, commit: str, expected: str, output: Path) -> dict:
    root = root.resolve(strict=True)
    output = output.absolute()
    if not HEX40.fullmatch(commit) or not HEX64.fullmatch(expected):
        raise ValueError("Full commit SHA-1 and candidate SHA-256 are required")
    if (output.suffix.lower() != ".zip" or output.resolve().is_relative_to(root)
            or os.path.lexists(output)):
        raise ValueError("Output must be a new ZIP path outside the source checkout")
    iteration = assert_current(root, commit, expected)
    if git("rev-parse", "--is-shallow-repository", cwd=root) != "false":
        raise ValueError("Root source history must be complete")
    source_pins = pins(root)
    if Path(__file__).read_bytes() != (root / "tools/package_handoff.py").read_bytes():
        raise ValueError("Use the packager committed in the requested source checkout")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="neuro-handoff-", dir=output.parent) as temporary:
        work = Path(temporary)
        template = work / "empty-template"
        template.mkdir()
        bundle = work / "root.bundle"
        git("bundle", "create", str(bundle), "HEAD", cwd=root)
        git("bundle", "verify", str(bundle), cwd=root)
        package = work / "checkout"
        git("clone", "--no-checkout", "--template=" + str(template), "--", str(bundle), str(package))
        sanitize_checkout(package)
        git("checkout", "-B", f"handoff-iteration-{iteration:02}", commit, cwd=package)
        modules = []
        for source in source_pins:
            snapshot = work / (source["path"].split("/")[-1] + ".git")
            boundaries = snapshot_module(root / source["path"], snapshot, source["commit"], template)
            target = package / source["path"]
            git("clone", "--local", "--no-hardlinks", "--no-checkout", "--template=" + str(template),
                "--", str(snapshot), str(target))
            sanitize_checkout(target)
            git("checkout", "--detach", source["commit"], cwd=target)
            sanitize_checkout(target)
            modules.append({"path": source["path"], "commit": source["commit"],
                            "origin": PUBLIC_MODULES[source["path"]], "shallow_boundaries": boundaries})
        sanitize_checkout(package)
        meta = package / META
        meta.mkdir(parents=True)
        actual_candidate = candidate(package, meta / "candidate.json")
        if actual_candidate["candidate_sha256"] != expected:
            raise ValueError("Clean staged sources differ from requested candidate (including ignored public files)")
        shutil.copyfile(package / "tools/package_handoff.py", meta / "verify.py")
        manifest = {"schema": SCHEMA, "flat_checkout": True, "iteration": iteration,
            "iteration10_launch_archive": iteration == 10, "execution_policy": POLICY,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "root": {"commit": commit, "original_origin": ROOT_ORIGIN,
                     "origin_usable": False, "active_remotes": [],
                     "reachable_commit_count": int(git("rev-list", "--count", "HEAD", cwd=root))},
            "candidate_sha256": expected, "candidate_files": actual_candidate["files"],
            "candidate_bytes": actual_candidate["bytes"], "modules": modules,
            "files": [{"path": name, "bytes": path.stat().st_size, "sha256": sha256(path)}
                      for name, path in sorted(regular_files(package).items())]}
        manifest_path = meta / "MANIFEST.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                                 encoding="ascii", newline="\n")
        (meta / "MANIFEST.sha256").write_text(sha256(manifest_path) + "  MANIFEST.json\n",
                                             encoding="ascii", newline="\n")
        verification = verify(package)
        assert_current(root, commit, expected)
        # Write a complete temporary ZIP first; publish exclusively, never overwrite.
        staged_zip = work / "handoff.zip"
        with zipfile.ZipFile(staged_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for name, path in sorted(regular_files(package).items()):
                entry = zipfile.ZipInfo(name, (2026, 11, 12, 0, 0, 0))
                entry.create_system = 3
                entry.external_attr = 0o100644 << 16
                entry.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(entry, path.read_bytes())
        with staged_zip.open("rb") as source, output.open("xb") as destination:
            shutil.copyfileobj(source, destination)
    return {"schema": "neuro.handoff.build.v1", "passed": True,
            "archive": str(output), "archive_sha256": sha256(output),
            "archive_bytes": output.stat().st_size, "commit": commit,
            "candidate_sha256": expected, "iteration": iteration,
            "iteration10_launch_archive": iteration == 10,
            "staging_verification": verification, "relocation_tested": False, "network_used": False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    builder = actions.add_parser("build", help="Build from a clean committed checkout; no network")
    builder.add_argument("--root", required=True, type=Path)
    builder.add_argument("--commit", required=True)
    builder.add_argument("--candidate-sha256", required=True)
    builder.add_argument("--output", required=True, type=Path)
    checker = actions.add_parser("verify", help="Verify an untouched extracted handoff before use")
    checker.add_argument("--root", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if sys.version_info < (3, 11):
            raise ValueError("Python 3.11 or newer is required")
        result = (build(args.root, args.commit, args.candidate_sha256, args.output)
                  if args.action == "build" else verify(args.root))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as exc:
        result = {"schema": "neuro.handoff." + args.action + ".v1", "passed": False, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
