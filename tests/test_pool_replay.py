"""Independent peer refusal/bounds/identity tests; no miner or network needed."""
import base64
import gzip
import os
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from replay_pool import (Job, JSONLines, PeerLedger, ReplayError, backend_arguments,
                         decode_proof, require_backend_execution,
                         require_official_acceptance, stop_child, strict_object)


class StrictProtocolTests(unittest.TestCase):
    def test_json_ambiguity_and_non_objects_are_rejected(self):
        for raw in (b'{"id":1,"id":2}', b'{"params":{"id":1,"id":2}}',
                    b'{"id":NaN}', b'{"id":Infinity}', b'{"id":1e400}',
                    b'[]', b'true', b'{}{}', b'\xff'):
            with self.subTest(raw=raw), self.assertRaises(ReplayError):
                strict_object(raw)

    def test_fragmented_and_coalesced_frames_keep_identity(self):
        reader = JSONLines(64)
        self.assertEqual(reader.feed(b'{"id":'), [])
        self.assertEqual(reader.feed(b'1}\n{"id":2}\n{"id"'), [{"id": 1}, {"id": 2}])
        self.assertEqual(reader.feed(b':3}\n'), [{"id": 3}])
        reader.finish()

    def test_unterminated_and_oversized_frames_fail(self):
        for raw in (b"{" + b" " * 40, b"{" + b" " * 40 + b"}\n"):
            with self.subTest(raw=raw), self.assertRaises(ReplayError):
                JSONLines(32).feed(raw)
        reader = JSONLines()
        reader.feed(b'{"id":1')
        with self.assertRaises(ReplayError):
            reader.finish()

    def test_deeply_nested_bounded_json_is_a_reported_failure(self):
        nested = b'{"x":' + b'[' * 20000 + b'0' + b']' * 20000 + b'}'
        self.assertLess(len(nested), 65536)
        with self.assertRaises(ReplayError):
            strict_object(nested, limit=65536)


class CompressionTests(unittest.TestCase):
    def test_real_single_member_and_plain_payloads(self):
        raw = bytes(range(256)) * 5
        zipped = gzip.compress(raw, mtime=0)
        self.assertEqual(decode_proof(base64.b64encode(zipped).decode(), gzip_negotiated=True), (raw, zipped))
        self.assertEqual(decode_proof(base64.b64encode(raw).decode(), gzip_negotiated=False), (raw, raw))

    def test_negotiation_mismatch_is_not_silently_autodetected(self):
        for raw, negotiated in ((gzip.compress(b"plain"), False), (b"plain", True)):
            with self.subTest(negotiated=negotiated), self.assertRaises(ReplayError):
                decode_proof(base64.b64encode(raw).decode(), gzip_negotiated=negotiated)

    def test_expansion_is_bounded_before_gzip_completion(self):
        encoded = base64.b64encode(gzip.compress(b"A" * 100000)).decode()
        with self.assertRaisesRegex(ReplayError, "expanded byte bound"):
            decode_proof(encoded, gzip_negotiated=True, max_raw=1024)

    def test_truncation_trailing_members_and_corrupt_crc_are_rejected(self):
        valid = gzip.compress(b"fixture", mtime=0)
        bad_crc = bytearray(valid)
        bad_crc[-8] ^= 1
        for wire in (valid[:-1], valid + b"junk", valid + valid, bytes(bad_crc)):
            with self.subTest(wire=wire), self.assertRaises(ReplayError):
                decode_proof(base64.b64encode(wire).decode(), gzip_negotiated=True)

    def test_invalid_base64_empty_and_plain_bound(self):
        for text in ("", "%%%", "YWJj\n", "é"):
            with self.subTest(text=text), self.assertRaises(ReplayError):
                decode_proof(text, gzip_negotiated=False)
        with self.assertRaises(ReplayError):
            decode_proof(base64.b64encode(b"12345").decode(), gzip_negotiated=False, max_raw=4)


class PeerIdentityTests(unittest.TestCase):
    def setUp(self):
        self.ledger = PeerLedger()
        self.connection = self.ledger.begin(1)
        self.authorization = {"jsonrpc": "2.0", "id": 1, "method": "mining.authorize", "params": {
            "wallet": "offline-wallet", "worker": "replay-worker", "agent": "neuro-replay",
            "password": "x", "type": "v2"}}
        self.job = Job("same-id", "00" * 76)

    def authorize(self):
        self.ledger.authorize(self.connection, self.authorization)
        self.connection.authorized = True

    def submit(self, request_id=2, job_id="same-id", hs=0):
        return {"jsonrpc": "2.0", "id": request_id, "method": "mining.submit",
                "params": {"job_id": job_id, "plain_proof": "AA==", "hs": hs}}

    def test_no_submit_before_authorization_and_job(self):
        with self.assertRaises(ReplayError):
            self.ledger.submission(self.connection, self.submit())
        self.authorize()
        with self.assertRaises(ReplayError):
            self.ledger.submission(self.connection, self.submit())

    def test_target_cert_and_header_tail_are_distinct_jobs(self):
        self.authorize()
        variants = [self.job,
                    Job("same-id", "00" * 75 + "01"),
                    Job("same-id", "00" * 75 + "01", cert_version=2),
                    Job("same-id", "00" * 75 + "01", target_hex="01" * 32, cert_version=2)]
        revisions = [self.ledger.issue(self.connection, job).revision for job in variants]
        self.assertEqual(revisions, [1, 2, 3, 4])
        self.assertEqual(self.ledger.issue(self.connection, variants[-1]).revision, 4)
        _, issued, _ = self.ledger.submission(self.connection, self.submit())
        self.assertEqual(issued.job, variants[-1])

    def test_wrong_job_reused_id_and_boolean_id_fail(self):
        self.authorize()
        self.ledger.issue(self.connection, self.job)
        self.ledger.submission(self.connection, self.submit())
        for message in (self.submit(), self.submit(3, "stale"), self.submit(True)):
            with self.subTest(message=message), self.assertRaises(ReplayError):
                self.ledger.submission(self.connection, message)

    def test_connection_epoch_separates_identical_ids_and_jobs(self):
        self.authorize()
        first = self.ledger.issue(self.connection, self.job)
        self.ledger.submission(self.connection, self.submit())
        self.connection.closed = True
        other = self.ledger.begin(2)
        self.ledger.authorize(other, self.authorization)
        other.authorized = True
        second = self.ledger.issue(other, self.job)
        self.ledger.submission(other, self.submit())
        self.assertNotEqual(first.connection, second.connection)
        with self.assertRaises(ReplayError):
            self.ledger.submission(self.connection, self.submit(4))

    def test_only_finite_numeric_hashrate(self):
        self.authorize()
        self.ledger.issue(self.connection, self.job)
        for index, value in enumerate((True, -1, "1", float("nan"), float("inf"), 2 ** 4096), 2):
            with self.subTest(value=value), self.assertRaises(ReplayError):
                self.ledger.submission(self.connection, self.submit(index, hs=value))


class OfficialResponseTests(unittest.TestCase):
    def test_only_complete_expected_verifier_acceptance_counts(self):
        process = {"timed_out": False, "launch_error": None, "output_complete": True, "returncode": 0}
        payload = {"verified": True, "stage": "verified", "upstream_commit": "pinned"}
        require_official_acceptance(process, payload, "pinned")
        for change in ({"returncode": 1}, {"timed_out": True}, {"launch_error": "failed"},
                       {"output_complete": False}):
            with self.subTest(change=change), self.assertRaises(ReplayError):
                require_official_acceptance({**process, **change}, payload, "pinned")
        for change in ({"verified": "true"}, {"verified": 1}, {"verified": False},
                       {"stage": "verification_panic"}, {"upstream_commit": "other"}):
            with self.subTest(change=change), self.assertRaises(ReplayError):
                require_official_acceptance(process, {**payload, **change}, "pinned")


class CleanupTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "The observed replay profile is Linux")
    def test_ignored_termination_escalates_and_reaps_owned_child(self):
        code = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(10)"
        child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                                 start_new_session=True)
        try:
            self.assertEqual(child.stdout.readline(), b"ready\n")
            stop_child(child, grace=0.1)
            self.assertIsNotNone(child.returncode)
            stop_child(child, grace=0.1)  # An already reaped child is harmless.
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=1)
            child.stdout.close()


class BackendEvidenceTests(unittest.TestCase):
    def test_backend_selection_is_explicit_and_device_is_cuda_only(self):
        self.assertEqual(backend_arguments("cpu", None), ["--backend", "cpu"])
        self.assertEqual(backend_arguments("cuda", None), ["--backend", "cuda", "--device", "0"])
        self.assertEqual(backend_arguments("cuda", 15), ["--backend", "cuda", "--device", "15"])
        for backend, device in (("cpu", 0), ("cuda", -1), ("cuda", 16), ("cuda", True),
                                ("cuda", 1.5), ("other", None)):
            with self.subTest(backend=backend, device=device), self.assertRaises(ReplayError):
                backend_arguments(backend, device)

    def test_selected_backend_requires_observed_execution_not_just_a_label(self):
        for backend in ("cpu", "cuda"):
            observed = {"backend": backend, "cpu_execution": backend == "cpu", "gpu_execution": backend == "cuda"}
            require_backend_execution(observed, backend)
            for change in ({"backend": "other"}, {"cpu_execution": False, "gpu_execution": False},
                           {"cpu_execution": True, "gpu_execution": True}):
                with self.subTest(backend=backend, change=change), self.assertRaises(ReplayError):
                    require_backend_execution({**observed, **change}, backend)


if __name__ == "__main__":
    unittest.main()
