#!/usr/bin/env python3
"""Bounded Linux -> Windows SSH entry point for four fixed offline actions.

The checkout on Windows must already exist. This does not install software,
guess a key-descriptor format, enrol host keys, or upload the repository.
Private profile/key paths and SSH argv are deliberately absent from reports.
"""
from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import tempfile
import uuid

from neuroctl.experiments import run_command
from candidate_manifest import CandidateError, canonical_bytes, create_manifest, validate_manifest

ROOT = Path(__file__).resolve().parents[1]
ACTIONS = ("inventory", "build-probes", "run-cpu", "run-cuda")
DEFAULT_TIMEOUTS = {"inventory": 90, "build-probes": 1800,
                    "run-cpu": 300, "run-cuda": 900}
MAX_CAPTURE = 262144


class RemoteError(ValueError):
    """A bounded input/transport failure, with no secret-bearing diagnostic."""


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RemoteError("duplicate JSON field")
        result[key] = value
    return result


def _json(text: str):
    # Bound nesting before the platform JSON decoder (its recursion limits vary).
    depth = 0
    quoted = escaped = False
    for character in text:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in "[{":
            depth += 1
            if depth > 16:
                raise RemoteError("JSON nesting exceeds the worker profile")
        elif character in "]}":
            depth -= 1
    def constant(_):
        raise RemoteError("non-finite JSON number")
    try:
        result = json.loads(text, object_pairs_hook=_object, parse_constant=constant)
    except (ValueError, RecursionError) as exc:
        raise RemoteError("invalid JSON object") from exc
    if not isinstance(result, dict):
        raise RemoteError("expected a JSON object")
    return result


def _integer(value, minimum, maximum, name):
    if type(value) is not int or not minimum <= value <= maximum:
        raise RemoteError(f"invalid integer field: {name}")
    return value


def _local_file(value, name):
    if (not isinstance(value, str) or not value or len(value) > 4096
            or any(ord(c) < 32 for c in value)):
        raise RemoteError(f"invalid path field: {name}")
    path = Path(value).expanduser().absolute()
    # Metadata only. Never open, parse, hash, print or copy the private key.
    if not path.is_file():
        raise RemoteError(f"existing file required for {name}")
    return path


def _windows_path(value, *, transfer=False):
    if (not isinstance(value, str) or not 4 <= len(value) <= 200
            or any(ord(c) < 32 for c in value)):
        raise RemoteError("invalid Windows path")
    path = PureWindowsPath(value)
    if (not path.is_absolute() or re.fullmatch(r"[A-Za-z]:", path.drive) is None
            or ".." in path.parts or value.endswith(("/", "\\"))):
        raise RemoteError("explicit Windows drive directory required")
    normalized = path.as_posix()
    # Keep SFTP filenames simple. Repository paths travel in structured JSON,
    # so spaces/quotes there never become a command or scp remote expression.
    if transfer and re.fullmatch(r"[A-Za-z]:/[A-Za-z0-9._/-]+", normalized) is None:
        raise RemoteError("remote_work requires a simple ASCII drive path without spaces")
    return normalized


@dataclass(frozen=True, repr=False)
class Profile:
    host: str
    user: str
    port: int
    identity: Path
    known_hosts: Path
    remote_repo: str
    remote_work: str
    timeouts: dict
    max_output_bytes: int


def load_profile(path: Path) -> Profile:
    try:
        if not path.is_file() or path.stat().st_size > 16384:
            raise RemoteError("private profile is too large")
        with path.open(encoding="utf-8") as stream:
            text = stream.read(16385)
        if len(text) > 16384:
            raise RemoteError("private profile is too large")
        value = _json(text)
    except (OSError, UnicodeError) as exc:
        raise RemoteError("cannot read private JSON profile") from exc
    fields = {"schema_version", "host", "user", "port", "identity_file",
              "known_hosts", "remote_repo", "remote_work", "timeout_s", "max_output_bytes"}
    if set(value) != fields:
        raise RemoteError("private profile has missing or unknown fields")
    _integer(value["schema_version"], 1, 1, "schema_version")
    if (not isinstance(value["host"], str)
            or re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?",
                            value["host"]) is None):
        raise RemoteError("host must be a plain DNS name or IPv4 address")
    if (not isinstance(value["user"], str)
            or re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}", value["user"]) is None):
        raise RemoteError("user must be a plain local Windows account name")
    limits = value["timeout_s"]
    if not isinstance(limits, dict) or set(limits) != set(ACTIONS):
        raise RemoteError("timeout_s must specify every fixed action")
    for action, seconds in limits.items():
        _integer(seconds, 1, 3600, "timeout_s." + action)
    return Profile(value["host"], value["user"],
                   _integer(value["port"], 1, 65535, "port"),
                   _local_file(value["identity_file"], "identity_file"),
                   _local_file(value["known_hosts"], "known_hosts"),
                   _windows_path(value["remote_repo"]),
                   _windows_path(value["remote_work"], transfer=True), limits,
                   _integer(value["max_output_bytes"], 1024, MAX_CAPTURE, "max_output_bytes"))


def ps_literal(value: str) -> str:
    if "\0" in value:
        raise RemoteError("NUL in PowerShell value")
    return "'" + value.replace("'", "''") + "'"


def encoded_command(script: str) -> list[str]:
    wrapped = ("$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue';"
               "try {" + script + "} catch {"
               "[Console]::Error.WriteLine('remote operation failed');exit 2}")
    encoded = base64.b64encode(wrapped.encode("utf-16-le")).decode("ascii")
    return ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded]


def ssh_options(profile: Profile) -> list[str]:
    # -F prevents accidental ProxyCommand/RemoteCommand/forwarding from another
    # profile. Trust is exclusively the existing, explicitly named host-key file.
    known_hosts = str(profile.known_hosts).replace("\\", "\\\\").replace('"', '\\"')
    return ["-F", "/dev/null", "-i", str(profile.identity),
            "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", f'UserKnownHostsFile="{known_hosts}"',
            "-o", "GlobalKnownHostsFile=/dev/null", "-o", "UpdateHostKeys=no",
            "-o", "VerifyHostKeyDNS=no", "-o", "IdentitiesOnly=yes",
            "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
            "-o", "ForwardAgent=no", "-o", "ClearAllForwardings=yes",
            "-o", "PermitLocalCommand=no", "-o", "ConnectTimeout=10",
            "-o", "ConnectionAttempts=1", "-o", "ServerAliveInterval=5",
            "-o", "ServerAliveCountMax=2"]


def make_request(profile: Profile, action: str, device: int, request_id: str, *, candidate=None) -> dict:
    if action not in ACTIONS:
        raise RemoteError("unsupported action")
    _integer(device, 0, 15, "device")
    if action != "run-cuda" and device != 0:
        raise RemoteError("device is only meaningful for run-cuda")
    if re.fullmatch(r"[0-9a-f]{32}", request_id) is None:
        raise RemoteError("invalid request identity")
    files = {"worker_sha256": ROOT / "windows/worker.ps1",
             "runner_sha256": ROOT / "windows/BoundedProcess.cs",
             "entrypoint_sha256": ROOT / "windows/ci.ps1",
             "manifest_tool_sha256": ROOT / "tools/candidate_manifest.py"}
    try:
        hashes = {key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in files.items()}
    except OSError as exc:
        raise RemoteError("local worker source is missing") from exc
    if action != "inventory":
        try:
            candidate = validate_manifest(candidate if candidate is not None else create_manifest(ROOT))
        except CandidateError as exc:
            raise RemoteError("local candidate identity could not be established") from exc
        source = {row["path"]: row["sha256"] for row in candidate["files"]}
        if any(source.get(path.relative_to(ROOT).as_posix()) != hashes[key] for key, path in files.items()):
            raise RemoteError("local worker files changed after candidate capture")
    elif candidate is not None:
        raise RemoteError("inventory does not execute a candidate")
    return {"schema_version": 2, "request_id": request_id, "action": action,
            "repo_root": profile.remote_repo, "device": device,
            "timeout_s": profile.timeouts[action], "max_output_bytes": profile.max_output_bytes,
            "candidate_sha256": candidate["candidate_sha256"] if candidate else None,
            "manifest_sha256": hashlib.sha256(canonical_bytes(candidate) + b"\n").hexdigest() if candidate else None,
            **hashes}


def validate_report(report: dict, request: dict) -> dict:
    fields = {"schema", "request_id", "action", "worker_sha256", "runner_sha256",
              "entrypoint_sha256", "entrypoint_actual_sha256", "status",
              "manifest_tool_sha256", "manifest_sha256", "candidate_sha256",
              "candidate_before_sha256", "candidate_after_sha256",
              "exit_code", "timed_out", "output_complete", "duration_ms", "process_id",
              "stdout_bytes", "stderr_bytes", "stdout_truncated", "stderr_truncated",
              "stdout", "stderr", "error"}
    if set(report) != fields or report["schema"] != "neuro.remote.worker.v2":
        raise RemoteError("unexpected worker report schema")
    for field in ("request_id", "action", "worker_sha256", "runner_sha256", "entrypoint_sha256",
                  "manifest_tool_sha256", "manifest_sha256", "candidate_sha256"):
        if report[field] != request[field]:
            raise RemoteError("worker report is not for this request and staged code")
    actual_entry = report["entrypoint_actual_sha256"]
    if actual_entry is not None and (not isinstance(actual_entry, str)
                                    or re.fullmatch(r"[0-9a-f]{64}", actual_entry) is None):
        raise RemoteError("invalid remote entrypoint hash")
    if (request["action"] != "inventory" and actual_entry != request["entrypoint_sha256"]
            and report["process_id"] != 0):
        raise RemoteError("fixed action ran without the requested entrypoint source")
    for field in ("candidate_before_sha256", "candidate_after_sha256"):
        value = report[field]
        if value is not None and (not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None):
            raise RemoteError("invalid actual candidate identity")
        if request["action"] == "inventory" and value is not None:
            raise RemoteError("inventory cannot claim candidate verification")
    if request["action"] != "inventory":
        if report["process_id"] != 0 and report["candidate_before_sha256"] != request["candidate_sha256"]:
            raise RemoteError("fixed action ran without the requested candidate source")
        if report["status"] == "passed" and report["candidate_after_sha256"] != request["candidate_sha256"]:
            raise RemoteError("candidate source changed or was not verified after the action")
        if report["candidate_after_sha256"] != request["candidate_sha256"] and report["status"] != "worker_error":
            raise RemoteError("a missing or changed final candidate must invalidate the action result")
    _integer(report["exit_code"], -(2 ** 31), 2 ** 31 - 1, "exit_code")
    _integer(report["duration_ms"], 0, request["timeout_s"] * 1000 + 30000, "duration_ms")
    _integer(report["process_id"], 0, 2 ** 32 - 1, "process_id")
    for field in ("timed_out", "output_complete", "stdout_truncated", "stderr_truncated"):
        if type(report[field]) is not bool:
            raise RemoteError("worker report flags must be booleans")
    for stream in ("stdout", "stderr"):
        total = _integer(report[stream + "_bytes"], 0, 2 ** 63 - 1, stream + "_bytes")
        if not isinstance(report[stream], str) or len(report[stream]) > request["max_output_bytes"]:
            raise RemoteError("worker output exceeds requested capture bound")
        if len(report[stream]) > total:
            raise RemoteError("worker output exceeds observed byte count")
        if report[stream + "_truncated"] != (total > request["max_output_bytes"]):
            raise RemoteError("inconsistent worker output accounting")
    if not isinstance(report["error"], str) or len(report["error"]) > 4096:
        raise RemoteError("invalid worker error")
    if report["process_id"] == 0 and not report["error"]:
        raise RemoteError("worker claims an outcome without a child process")
    expected = "failed"
    if report["error"] or not report["output_complete"]:
        expected = "worker_error"
    elif report["timed_out"]:
        expected = "timeout"
    elif report["stdout_truncated"] or report["stderr_truncated"]:
        expected = "output_limit"
    elif report["exit_code"] == 0 and report["process_id"] > 0:
        expected = "passed"
    if report["status"] != expected:
        raise RemoteError("worker status contradicts process outcome")
    return report


def _require_linux_controller():
    if os.name != "posix":
        raise RemoteError("the SSH controller currently runs on Linux/POSIX")


def dispatch(profile: Profile, action: str, device: int = 0, *, runner=run_command) -> dict:
    _require_linux_controller()
    ssh, scp = shutil.which("ssh"), shutil.which("scp")
    if not ssh or not scp:
        raise RemoteError("OpenSSH ssh and scp are required")
    request_id = uuid.uuid4().hex
    try:
        candidate = create_manifest(ROOT) if action != "inventory" else None
    except CandidateError as exc:
        raise RemoteError("local candidate identity could not be established") from exc
    request = make_request(profile, action, device, request_id, candidate=candidate)
    report = {"schema": "neuro.remote.dispatch.v2", "request_id": request_id,
              "action": action, "status": "not_started", "remote_execution": "not_started",
              "candidate_sha256": request["candidate_sha256"], "transport": [], "worker": None}

    def invoke(stage, argv, timeout, cap=8192):
        result = runner(argv, timeout_s=timeout, max_log_bytes=cap, stage=stage)
        # Never persist argv/cwd/SSH diagnostic text: these can contain private
        # host/user/key references. Runtime child output is in the worker report.
        report["transport"].append({"stage": stage, "returncode": result["returncode"],
                                    "timed_out": result["timed_out"],
                                    "output_complete": result["output_complete"],
                                    "launch_failed": bool(result["launch_error"])})
        good = (not result["timed_out"] and not result["launch_error"]
                and result["output_complete"] and result["returncode"] == 0
                and not result["stdout_truncated"] and not result["stderr_truncated"])
        return good, result

    ok, version = invoke("client_version", [ssh, "-V"], 10)
    match = re.search(r"OpenSSH_(\d+)\.(\d+)", version["stdout"] + version["stderr"])
    if not ok or not match or int(match[1]) < 9:
        report["status"] = "unsupported_ssh_client"
        return report
    options = ssh_options(profile)
    destination = f"{profile.user}@{profile.host}"
    ssh_base = [ssh, *options, "-p", str(profile.port), "-n", "-T", "--", destination]
    remote_dir = profile.remote_work + "/neuro-" + request_id
    remote_request = remote_dir + "/request.json"
    remote_report = remote_dir + "/report.json"
    create = (f"$p={ps_literal(remote_dir)};"
              "if([IO.Directory]::Exists($p)){throw 'run directory already exists'};"
              "[IO.Directory]::CreateDirectory($p)|Out-Null")
    ok, _ = invoke("prepare", [*ssh_base, *encoded_command(create)], 30)
    if not ok:
        report["status"] = "transport_error"
        return report
    with tempfile.TemporaryDirectory(prefix="neuro-worker-") as temporary:
        request_path = Path(temporary) / "request.json"
        request_path.write_text(json.dumps(request, separators=(",", ":")) + "\n", encoding="utf-8")
        candidate_paths = []
        if candidate is not None:
            manifest_path = Path(temporary) / "candidate.json"
            manifest_path.write_bytes(canonical_bytes(candidate) + b"\n")
            candidate_paths.append(str(manifest_path))
        # Modern scp uses SFTP. No -O fallback and no shell metacharacters in
        # remote transfer paths. Only our worker/request files are staged.
        upload = [scp, *options, "-S", ssh, "-P", str(profile.port), "-B",
                  str(ROOT / "windows/worker.ps1"), str(ROOT / "windows/BoundedProcess.cs"),
                  str(ROOT / "tools/candidate_manifest.py"), *candidate_paths,
                  str(request_path), destination + ":" + remote_dir + "/"]
        ok, _ = invoke("upload", upload, 60)
        if not ok:
            report["status"] = "transport_error"
            return report
        launch = (f"& {ps_literal(remote_dir + '/worker.ps1')}"
                  f" -RequestPath {ps_literal(remote_request)} -ReportPath {ps_literal(remote_report)};"
                  "exit $LASTEXITCODE")
        report["remote_execution"] = "unknown"
        launch_ok, launched = invoke("execute", [*ssh_base, *encoded_command(launch)],
                                     request["timeout_s"] + 100)
        # Fetch even after a lost SSH connection, but never re-execute the work.
        # Reading through SSH enforces a local capture cap as well as remote size
        # check; a hostile/partial report cannot cause an unbounded scp download.
        report_cap = 12 * request["max_output_bytes"] + 16384
        read = (f"$p={ps_literal(remote_report)};$f=Get-Item -LiteralPath $p;"
                f"if($f.PSIsContainer -or $f.Length -gt {report_cap}){{throw 'invalid report size'}};"
                "[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false);"
                "[Console]::Out.Write([IO.File]::ReadAllText($p))")
        fetch_ok, fetched = invoke("report", [*ssh_base, *encoded_command(read)], 30, report_cap)
    if not fetch_ok:
        report["status"] = "unknown"
        return report
    try:
        worker = validate_report(_json(fetched["stdout"]), request)
    except RemoteError:
        report["status"] = "invalid_report"
        return report
    report["remote_execution"] = "confirmed"
    report["worker"] = worker
    expected_exit = {"passed": 0, "failed": 1, "worker_error": 2, "output_limit": 3, "timeout": 124}
    transport_consistent = (not launched["timed_out"] and not launched["launch_error"]
                            and launched["output_complete"]
                            and launched["returncode"] == expected_exit[worker["status"]])
    report["status"] = worker["status"] if transport_consistent else "transport_error"
    if worker["status"] == "passed" and not launch_ok:
        report["status"] = "transport_error"
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("--profile", type=Path, required=True,
                        help="explicit private JSON profile; not the owner's unparsed key descriptor")
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/remote-worker.json")
    args = parser.parse_args(argv)
    try:
        report = dispatch(load_profile(args.profile), args.action, args.device)
    except (RemoteError, OSError) as exc:
        # The category is actionable without exposing a private path/value.
        message = str(exc) if isinstance(exc, RemoteError) else "local I/O failure"
        report = {"schema": "neuro.remote.dispatch.v2", "action": args.action,
                  "status": "invalid_input", "remote_execution": "not_started", "error": message}
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError:
        print(json.dumps({"action": args.action, "status": "report_write_error"}))
        return 2
    print(json.dumps({"action": args.action, "status": report["status"], "output": str(args.output)}))
    return 0 if report["status"] == "passed" else 124 if report["status"] == "timeout" else 1


if __name__ == "__main__":
    raise SystemExit(main())
