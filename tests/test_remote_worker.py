"""Controller tests use synthetic SSH results; Win32 execution is tested in CI."""
import base64
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import remote_worker as remote


def process_result(**changes):
    value = {"returncode": 0, "timed_out": False, "launch_error": None,
             "output_complete": True, "stdout": "", "stderr": "",
             "stdout_truncated": False, "stderr_truncated": False}
    value.update(changes)
    return value


def worker_result(request, **changes):
    value = {"schema": "neuro.remote.worker.v1", "request_id": request["request_id"],
             "action": request["action"], "worker_sha256": request["worker_sha256"],
             "runner_sha256": request["runner_sha256"], "entrypoint_sha256": request["entrypoint_sha256"],
             "entrypoint_actual_sha256": request["entrypoint_sha256"], "status": "passed",
             "exit_code": 0, "timed_out": False, "output_complete": True,
             "duration_ms": 120, "process_id": 1234, "stdout_bytes": 2,
             "stderr_bytes": 0, "stdout_truncated": False, "stderr_truncated": False,
             "stdout": "ok", "stderr": "", "error": ""}
    value.update(changes)
    return value


class FakeTransport:
    def __init__(self, *, results=None, worker=None, raw_report=None):
        self.results = results or {}
        self.worker = worker or {}
        self.raw_report = raw_report
        self.calls = []
        self.request = None

    def __call__(self, argv, *, timeout_s, max_log_bytes, stage):
        self.calls.append((stage, list(argv), timeout_s, max_log_bytes))
        if stage == "upload":
            self.request = json.loads(Path(argv[-2]).read_text(encoding="utf-8"))
        if stage in self.results:
            return process_result(**self.results[stage])
        if stage == "client_version":
            return process_result(stderr="OpenSSH_9.8p1, OpenSSL synthetic\n")
        if stage == "report":
            text = self.raw_report
            if text is None:
                text = json.dumps(worker_result(self.request, **self.worker))
            return process_result(stdout=text)
        return process_result()


class RemoteWorkerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="neuro-remote-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.key = self.root / "private key reference"
        self.key.write_text("KEY_CONTENT_MUST_NOT_BE_OPENED", encoding="utf-8")
        self.known_hosts = self.root / "existing known hosts"
        self.known_hosts.write_text("synthetic-host-key-metadata", encoding="utf-8")
        self.values = {"schema_version": 1, "host": "private-test.example.invalid",
                       "user": "PRIVATE_ACCOUNT", "port": 2222,
                       "identity_file": str(self.key), "known_hosts": str(self.known_hosts),
                       "remote_repo": "C:/repo with spaces/neuro",
                       "remote_work": "C:/neuro-worker",
                       "timeout_s": dict(remote.DEFAULT_TIMEOUTS), "max_output_bytes": 4096}
        self.profile_file = self.root / "private-profile.json"
        self.write_profile()
        self.profile = remote.load_profile(self.profile_file)

    def write_profile(self):
        self.profile_file.write_text(json.dumps(self.values), encoding="utf-8")

    def dispatch(self, fake, action="inventory", device=0):
        with patch.object(remote, "_require_linux_controller"), \
                patch.object(remote.shutil, "which", side_effect=lambda name: "/usr/bin/" + name):
            return remote.dispatch(self.profile, action, device, runner=fake)

    def test_profile_only_stats_private_key(self):
        original = Path.open
        opened = []

        def guarded(path, *args, **kwargs):
            opened.append(path)
            if path == self.key:
                raise AssertionError("private key was opened")
            return original(path, *args, **kwargs)

        with patch.object(Path, "open", guarded):
            profile = remote.load_profile(self.profile_file)
        self.assertEqual(profile.identity, self.key)
        self.assertEqual(opened, [self.profile_file])

    def test_missing_identity_or_known_hosts_fails_before_transport(self):
        for field in ("identity_file", "known_hosts"):
            with self.subTest(field=field):
                values = dict(self.values)
                values[field] = str(self.root / "missing")
                self.profile_file.write_text(json.dumps(values), encoding="utf-8")
                with self.assertRaises(remote.RemoteError):
                    remote.load_profile(self.profile_file)

    def test_private_profile_strict_schema_and_bounds(self):
        for changes in ({"host": "-oProxyCommand=malicious"}, {"host": "host;echo bad"},
                        {"user": "someone@else"}, {"user": "account\ncommand"},
                        {"port": True}, {"port": 65536}, {"schema_version": 2},
                        {"max_output_bytes": 1000000}, {"max_output_bytes": False},
                        {"timeout_s": {"inventory": 90}},
                        {"private_key": "DO_NOT_ACCEPT_KEY_CONTENT"}):
            with self.subTest(changes=changes):
                self.profile_file.write_text(json.dumps({**self.values, **changes}), encoding="utf-8")
                with self.assertRaises(remote.RemoteError):
                    remote.load_profile(self.profile_file)

    def test_duplicate_nonfinite_deep_and_large_json_rejected(self):
        for value in ('{"a":1,"a":2}', '{"x":NaN}', '{"x":' + '[' * 2000 + '0' + ']' * 2000 + '}'):
            with self.subTest(value=value[:20]):
                with self.assertRaises(remote.RemoteError):
                    remote._json(value)
        self.profile_file.write_text(" " * 16385, encoding="utf-8")
        with self.assertRaises(remote.RemoteError):
            remote.load_profile(self.profile_file)

    def test_windows_path_restrictions(self):
        for path in ("relative/path", "C:relative", "//server/share", "C:/../escape", "C:/folder\nname"):
            with self.subTest(path=path):
                with self.assertRaises(remote.RemoteError):
                    remote._windows_path(path)
        for path in ("C:/work space", "C:/work&command", "C:/work'quote"):
            with self.assertRaises(remote.RemoteError):
                remote._windows_path(path, transfer=True)
        self.assertEqual(remote._windows_path("C:\\repo with spaces\\neuro"), "C:/repo with spaces/neuro")

    def test_encoding_preserves_literal_quotes_without_shell(self):
        value = "C:/owner's folder/$data; literal"
        encoded = remote.encoded_command("$x=" + remote.ps_literal(value) + ";")
        self.assertEqual(encoded[-2], "-EncodedCommand")
        text = base64.b64decode(encoded[-1], validate=True).decode("utf-16-le")
        self.assertIn("$x='C:/owner''s folder/$data; literal';", text)
        self.assertNotIn(value, encoded)

    def test_ssh_requires_existing_host_key_and_no_agent_forwarding(self):
        options = remote.ssh_options(self.profile)
        for required in ("StrictHostKeyChecking=yes", "BatchMode=yes", "IdentitiesOnly=yes",
                         "ForwardAgent=no", "PasswordAuthentication=no", "UpdateHostKeys=no",
                         "ClearAllForwardings=yes", "PermitLocalCommand=no"):
            self.assertIn(required, options)
        self.assertEqual(options[:2], ["-F", "/dev/null"])
        self.assertIn(str(self.key), options)
        self.assertIn('UserKnownHostsFile="' + str(self.known_hosts) + '"', options)
        self.assertNotIn("StrictHostKeyChecking=no", options)

    def test_request_has_no_private_connection_fields(self):
        request = remote.make_request(self.profile, "run-cuda", 2, "a" * 32)
        self.assertEqual(request["device"], 2)
        self.assertEqual(request["repo_root"], self.profile.remote_repo)
        text = json.dumps(request)
        for private in (str(self.key), str(self.known_hosts), self.profile.host, self.profile.user):
            self.assertNotIn(private, text)
        for action, device in (("arbitrary", 0), ("inventory", 1), ("run-cuda", True), ("run-cuda", 32)):
            with self.assertRaises(remote.RemoteError):
                remote.make_request(self.profile, action, device, "a" * 32)

    def test_success_realistic_argv_and_no_private_report_data(self):
        fake = FakeTransport()
        result = self.dispatch(fake)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["remote_execution"], "confirmed")
        self.assertEqual([call[0] for call in fake.calls], ["client_version", "prepare", "upload", "execute", "report"])
        for _, argv, timeout, cap in fake.calls:
            self.assertIsInstance(argv, list)
            self.assertTrue(all(isinstance(part, str) for part in argv))
            self.assertGreater(timeout, 0)
            self.assertGreater(cap, 0)
            self.assertNotIn("-O", argv)
        uploaded = fake.calls[2][1]
        self.assertEqual(uploaded[0], "/usr/bin/scp")
        self.assertIn("-S", uploaded)
        self.assertEqual(fake.calls[3][2], self.profile.timeouts["inventory"] + 30)
        text = json.dumps(result)
        for private in (str(self.key), str(self.known_hosts), self.profile.host, self.profile.user,
                        "KEY_CONTENT_MUST_NOT_BE_OPENED"):
            self.assertNotIn(private, text)

    def test_unsupported_client_never_connects(self):
        fake = FakeTransport(results={"client_version": {"stderr": "OpenSSH_8.9p1"}})
        result = self.dispatch(fake)
        self.assertEqual(result["status"], "unsupported_ssh_client")
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(result["remote_execution"], "not_started")

    def test_failed_upload_never_starts_work(self):
        fake = FakeTransport(results={"upload": {"returncode": 255, "stderr": "private diagnostic"}})
        result = self.dispatch(fake)
        self.assertEqual(result["status"], "transport_error")
        self.assertEqual(result["remote_execution"], "not_started")
        self.assertNotIn("execute", [item[0] for item in fake.calls])
        self.assertNotIn("private diagnostic", json.dumps(result))

    def test_ssh_timeout_is_unknown_without_remote_report_not_success(self):
        fake = FakeTransport(results={"execute": {"timed_out": True, "returncode": -9},
                                      "report": {"returncode": 255}})
        result = self.dispatch(fake)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["remote_execution"], "unknown")
        self.assertEqual([item[0] for item in fake.calls].count("execute"), 1)

    def test_reported_success_after_transport_failure_is_not_dispatch_success(self):
        fake = FakeTransport(results={"execute": {"timed_out": True, "returncode": -9}})
        result = self.dispatch(fake)
        self.assertEqual(result["status"], "transport_error")
        self.assertEqual(result["remote_execution"], "confirmed")
        self.assertEqual(result["worker"]["status"], "passed")

    def test_real_child_failure_timeout_and_output_limit_are_distinct(self):
        cases = [(1, {"status": "failed", "exit_code": 7}),
                 (124, {"status": "timeout", "exit_code": 124, "timed_out": True}),
                 (3, {"status": "output_limit", "stdout_bytes": 5000, "stdout_truncated": True}),
                 (2, {"status": "worker_error", "error": "Win32Exception: CreateProcess", "exit_code": -1})]
        for code, worker in cases:
            with self.subTest(status=worker["status"]):
                result = self.dispatch(FakeTransport(results={"execute": {"returncode": code}}, worker=worker))
                self.assertEqual(result["status"], worker["status"])
                self.assertEqual(result["remote_execution"], "confirmed")

    def test_stale_or_mismatched_code_report_is_rejected(self):
        for field, value in (("request_id", "b" * 32), ("action", "run-cuda"),
                             ("worker_sha256", "0" * 64), ("runner_sha256", "1" * 64),
                             ("entrypoint_sha256", "2" * 64)):
            with self.subTest(field=field):
                result = self.dispatch(FakeTransport(worker={field: value}))
                self.assertEqual(result["status"], "invalid_report")
                self.assertIsNone(result["worker"])

    def test_entrypoint_mismatch_refuses_fixed_action_before_child(self):
        worker = {"status": "worker_error", "exit_code": -1, "process_id": 0,
                  "output_complete": False, "error": "entrypoint mismatch",
                  "entrypoint_actual_sha256": "0" * 64}
        result = self.dispatch(FakeTransport(results={"execute": {"returncode": 2}}, worker=worker), "run-cpu")
        self.assertEqual(result["status"], "worker_error")
        self.assertEqual(result["worker"]["process_id"], 0)
        result = self.dispatch(FakeTransport(worker={"entrypoint_actual_sha256": "0" * 64}), "run-cpu")
        self.assertEqual(result["status"], "invalid_report")

    def test_inventory_can_report_missing_entrypoint_without_running_it(self):
        result = self.dispatch(FakeTransport(worker={"entrypoint_actual_sha256": None}))
        self.assertEqual(result["status"], "passed")
        self.assertIsNone(result["worker"]["entrypoint_actual_sha256"])

    def test_inconsistent_pass_flags_bytes_and_process_are_rejected(self):
        for change in ({"exit_code": 7}, {"timed_out": True}, {"output_complete": False},
                       {"stdout_bytes": 0}, {"stdout_truncated": True}, {"process_id": 0},
                       {"error": "failed"}, {"duration_ms": -1}, {"exit_code": True},
                       {"stdout": "x" * 4097, "stdout_bytes": 4097}, {"unknown": True}):
            with self.subTest(change=list(change)):
                result = self.dispatch(FakeTransport(worker=change))
                self.assertEqual(result["status"], "invalid_report")

    def test_malformed_remote_json_and_capture_truncation_fail_closed(self):
        for raw in ("{}", '{"a":1,"a":2}', "[", '{"x":' + '[' * 2000 + '0' + ']' * 2000 + '}'):
            with self.subTest(raw=raw[:20]):
                result = self.dispatch(FakeTransport(raw_report=raw))
                self.assertEqual(result["status"], "invalid_report")
        result = self.dispatch(FakeTransport(results={"report": {"stdout_truncated": True}}))
        self.assertEqual(result["status"], "unknown")

    def test_cli_missing_profile_writes_failure_without_connecting(self):
        output = self.root / "report.json"
        with patch.object(remote, "dispatch") as dispatch, contextlib.redirect_stdout(io.StringIO()):
            code = remote.main(["inventory", "--profile", str(self.root / "missing"), "--output", str(output)])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["status"], "invalid_input")
        dispatch.assert_not_called()

    def test_cli_exit_codes_follow_observed_outcome(self):
        for status, expected in (("passed", 0), ("failed", 1), ("unknown", 1), ("timeout", 124)):
            with self.subTest(status=status), patch.object(remote, "dispatch", return_value={"status": status}), \
                    contextlib.redirect_stdout(io.StringIO()):
                code = remote.main(["inventory", "--profile", str(self.profile_file),
                                    "--output", str(self.root / "result.json")])
                self.assertEqual(code, expected)


if __name__ == "__main__":
    unittest.main()
