#!/usr/bin/env python3
"""Exercise durable search recovery through fresh native processes and real TCP.

This is an offline, bounded correctness gate. The peer owns the jobs and expected
attempts, and verifies every received proof using independent fixtures and the
separately pinned official Pearl verifier. Process interruption is observed;
filesystem/power-loss durability and physical GPU execution are separate gates.
"""
from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import selectors
import socket
import struct
import subprocess
import tempfile
import threading
import time

from replay_pool import (
    ROOT, TARGET, MAX_JSON_FRAME, Job, ReplayCase, ReplayError, Scenario,
    backend_arguments, decode_proof, encode_message, inspect_proof,
    require, require_attempt_submission, require_backend_execution,
    require_official_acceptance, run_command, sha256, stop_child, strict_object,
)


SCENARIOS = (
    "crash_before_compute", "crash_after_proof", "crash_before_send",
    "crash_after_ack", "received_without_ack", "encoding_change_restart", "rejected_then_restart",
    "miss_prefix_restart", "changed_header_return", "changed_target_return",
    "exhausted_restart", "corrupt_journal", "torn_journal", "report_path_alias", "exclusive_writer",
)
OUTCOMES = frozenset(("running", "found", "miss", "cancelled", "error", "interrupted",
                      "intent", "accepted", "rejected", "uncertain", "discarded"))


def check_outcomes(report: dict, expected: dict[str, int]) -> None:
    """An uncertain send must not silently become an accepted or forgotten send."""
    journal = report.get("journal")
    require(type(journal) is dict and journal.get("enabled") is True,
            "Native report omitted the enabled durable journal")
    actual = journal.get("outcomes")
    require(type(actual) is dict and set(actual) == OUTCOMES,
            "Native journal omitted or added an unknown outcome")
    require(all(type(value) is int and value >= 0 for value in actual.values()),
            "Journal outcomes must be nonnegative integer counts")
    require(set(expected) <= OUTCOMES, "Test expectation contains an unknown outcome")
    for name in OUTCOMES:
        require(actual[name] == expected.get(name, 0),
                f"Durable outcome {name}: {actual[name]} != {expected.get(name, 0)}")
    require(journal.get("attempts_consumed") == sum(actual.values()),
            "Consumed attempts differ from the durable outcome inventory")


def independent_case(fixtures: dict, job: Job, index: int) -> tuple[dict, dict]:
    """Use peer-owned plans, never a native event label, to identify a proof."""
    case = next((item for item in fixtures["cases"]
                 if item["index"] == index and item["header_hex"] == job.header_hex), None)
    require(case is not None, "No independent fixture for this issued work and attempt")
    if job.target_hex == TARGET:
        tile = case["first_easy_winner"]
    else:
        delayed = fixtures["miss_then_found"]
        require(job.target_hex == delayed["target_be_hex"] and
                index == delayed["winning_attempt"],
                "No independent winning tile for this issued target")
        tile = delayed
    return case, tile


@dataclass(frozen=True)
class Phase:
    name: str
    job: Job
    started: tuple[int, ...]
    wire_attempts: tuple[int, ...]
    outcomes: dict[str, int] | None = None
    gzip: bool = True
    limit: int = 8
    crash_point: str | None = None
    crash_index: int = 0
    acknowledgement: str = "accept"  # accept, reject, or kill_after_receive
    preflight_error: bool = False
    report_alias: str | None = None
    lock_contender: bool = False
    stop_on_limit: bool = False


class RecoveryPhase(ReplayCase):
    """One fresh native process; its listener and journal outlive this object."""

    def __init__(self, phase: Phase, *, journal: Path, **kwargs):
        super().__init__(Scenario(phase.name, attempts=phase.limit, gzip=phase.gzip), **kwargs)
        self.phase, self.journal = phase, journal
        self.initial_job = phase.job
        self.contender: dict | None = None
        self.killed_after_receive = False
        self.argv: list[str] = []

    def on_event(self, event: dict) -> None:
        require(type(event.get("event")) is str, "Native event has no event name")
        self.events.append(event)

    def handle_authorize(self, context, message: dict) -> None:
        request_id = self.ledger.authorize(context, message)
        if self.phase.lock_contender:
            # The first process owns the journal and is still waiting for the
            # authorization response. The contender must fail before networking.
            contender_report = self.work / "contender-report.json"
            argv = list(self.argv)
            argv[argv.index("--report") + 1] = str(contender_report)
            self.contender = run_command(argv, cwd=ROOT, timeout_s=2,
                                         max_log_bytes=16384, stage="journal-lock-contender")
            require(self.contender.get("returncode") == 2 and
                    self.contender.get("output_complete") is True and
                    not self.contender.get("timed_out") and
                    not self.contender.get("launch_error"),
                    "A concurrent journal writer did not promptly refuse")
            output = self.contender.get("stdout", "") + self.contender.get("stderr", "")
            require("journal" in output.lower() and
                    ("lock" in output.lower() or "exclusive journal file" in output.lower()) and
                    '"event":"connected"' not in output,
                    "The contender failed for another reason or connected to the peer")
        response = {"jsonrpc": "2.0", "id": request_id, "result": True}
        if self.phase.gzip:
            response["type"] = "v2"

        def delivered():
            context.authorized, context.gzip = True, self.phase.gzip
            self.ledger.issue(context, self.initial_job)

        self.send(context, encode_message(response) + encode_message(self.initial_job.notify()),
                  delivered=delivered)

    def handle_submit(self, context, message: dict) -> None:
        request_id, issued, encoded = self.ledger.submission(context, message)
        ordinal = len(self.proofs)
        require(ordinal < len(self.phase.wire_attempts),
                "Unexpected extra/repeated wire submission after recovery")
        expected_index = self.phase.wire_attempts[ordinal]
        expected, tile = independent_case(self.attempt_fixtures, issued.job, expected_index)
        raw, wire = decode_proof(encoded, gzip_negotiated=context.gzip)
        decoded = inspect_proof(raw)
        require_attempt_submission(decoded, expected, tile)
        proof_file = self.work / f"wire-{ordinal}-attempt-{expected_index}.b64"
        proof_file.write_bytes(base64.b64encode(raw))
        process = run_command([str(self.reference), "--header-hex", issued.job.header_hex,
                               "--proof-base64-file", str(proof_file), "--cert-version",
                               str(issued.job.cert_version), "--nbits", issued.job.nbits],
                              cwd=ROOT, timeout_s=15, max_log_bytes=16384,
                              stage="recovered-wire-official-verification")
        verified = strict_object(process["stdout"].encode(), limit=16384)
        require_official_acceptance(process, verified, self.commit)
        record = {"id": request_id, "job_id": issued.job.job_id,
                  "header_sha256": sha256(bytes.fromhex(issued.job.header_hex)),
                  "target_be_hex": issued.job.target_hex,
                  "expected_attempt_index": expected_index,
                  "attempt_identity_independently_checked": True,
                  "proof_signal_commitment_a": decoded["a"]["root_hex"],
                  "proof_sha256": sha256(raw), "wire_sha256": sha256(wire),
                  "raw_bytes": len(raw), "wire_bytes": len(wire), "gzip": context.gzip,
                  "official_verifier": verified,
                  "synthetic_peer_accepted": None}
        self.proofs.append(record)
        if self.phase.acknowledgement == "kill_after_receive":
            # Proof receipt and verification are already observed on the peer.
            # No ACK is sent, so restart must preserve uncertainty even though
            # the local peer knows that this particular share reached it.
            self.killed_after_receive = True
            stop_child(self.process)
            self.close(context)
            return
        accepted = self.phase.acknowledgement == "accept"
        require(accepted or self.phase.acknowledgement == "reject", "Invalid acknowledgement plan")
        record["synthetic_peer_accepted"] = accepted
        reply = {"jsonrpc": "2.0", "id": request_id, "result": accepted}
        if not accepted:
            reply["error"] = {"code": 23, "message": "synthetic recovery test rejection"}
        self.send(context, encode_message(reply))

    def validate_phase(self, report: dict | None, returncode: int) -> None:
        started = [item.get("index") for item in self.events if item["event"] == "attempt_started"]
        require(started == list(self.phase.started),
                f"Recovery started attempts {started}, expected {list(self.phase.started)}")
        require([item["expected_attempt_index"] for item in self.proofs] == list(self.phase.wire_attempts),
                "Recovery wire proofs differ from the independent peer plan")
        require(len(self.ledger.connections) == (0 if self.phase.preflight_error else 1),
                "Unexpected connection count or networking before journal validation")
        if self.phase.preflight_error:
            require(returncode == 2 and any(item["event"] == "fatal_error" for item in self.events),
                    "Unsafe or damaged journal did not fail before opening a connection")
            return
        require(len(self.ledger.issued) == 1 and len([
            item for item in self.events if item["event"] == "authorized"]) == 1,
            "Recovery did not freshly authorize and receive its work")
        bound = [item for item in self.events if item["event"] == "journal_bound"]
        require(len(bound) == 1 and type(bound[0].get("work_id")) is int and
                type(bound[0].get("next_attempt")) is int,
                "Recovery did not report its bound work and saved attempt index")
        if self.phase.started:
            require(bound[0]["next_attempt"] == self.phase.started[0],
                    "First dispatched attempt differs from the durable restart position")
        if self.phase.crash_point:
            crashes = [item for item in self.events if item["event"] == "crash_injected"]
            require(returncode == 86 and len(crashes) == 1 and
                    crashes[0].get("point") == self.phase.crash_point and
                    crashes[0].get("index") == self.phase.crash_index and
                    crashes[0].get("job_id") == self.phase.job.job_id,
                "Native process did not reach the requested abrupt-exit fault point")
            require(report is None, "An abrupt fault unexpectedly produced a normal completion report")
            return
        if self.phase.acknowledgement == "kill_after_receive":
            require(self.killed_after_receive and returncode != 0 and report is None,
                    "Native process did not stop after observed receipt without acknowledgement")
            require(not any(item["event"] == "share_response" for item in self.events),
                    "An unacknowledged wire share received an unexpected response")
            return
        require(type(report) is dict and returncode == 0 and report.get("completed") is True,
                "Recovery did not finish its bounded normal phase")
        require(report.get("schema") == "neuro.pool_client.v1" and
                report.get("root_seed_hex") == self.fixture["seed_hex"] and
                report.get("attempt_seed_policy") == "neuro/zero-b/row-seed/v1" and
                report.get("attempt_limit_per_generation") == self.phase.limit,
                "Recovered report omitted or changed the reproducible search contract")
        expected_reason = "attempt_limit" if self.phase.stop_on_limit else "submission_limit"
        require(report.get("reason") == expected_reason, "Unexpected recovery completion reason")
        if self.phase.started:
            require_backend_execution(report, self.backend)
        else:
            require(report.get("cpu_execution") is False and report.get("gpu_execution") is False,
                    "An exhausted recovered job performed unexpected arithmetic")
        counters = report.get("counters", {})
        expected_accepted = len(self.proofs) if self.phase.acknowledgement == "accept" else 0
        expected_rejected = len(self.proofs) if self.phase.acknowledgement == "reject" else 0
        require(counters.get("submissions") == len(self.proofs) and
                counters.get("accepted") == expected_accepted and
                counters.get("rejected") == expected_rejected and
                counters.get("pending_lost") == 0 and counters.get("errors") == 0,
                "Per-process response counters differ from actual peer replies")
        require(self.phase.outcomes is not None, "Normal phase lacks durable outcome expectations")
        check_outcomes(report, self.phase.outcomes)
        works = report["journal"].get("works")
        require(type(works) is list and report["journal"].get("work_count") == len(works),
                "Durable work inventory is missing or inconsistent")
        current = [work for work in works if work.get("work_id") == bound[0]["work_id"]]
        require(len(current) == 1 and current[0].get("next_attempt") ==
                (self.phase.started[-1] + 1 if self.phase.started else bound[0]["next_attempt"]) and
                current[0].get("limit") == self.phase.limit,
                "Durable attempt position or bound was lost")
        require(report["journal"].get("recovered") is (self.phase.name != "first"),
                "Journal opening did not distinguish a fresh file from recovery")

    def run_on_listener(self, listener: socket.socket) -> dict:
        started = time.monotonic()
        result = {"name": self.phase.name, "passed": False}
        self.selector.register(listener, selectors.EVENT_READ, None)
        self.native_report.unlink(missing_ok=True)
        requested_report = self.native_report
        if self.phase.report_alias == "journal":
            requested_report = self.journal
        elif self.phase.report_alias == "temporary":
            require(self.journal.name.endswith(".tmp"), "Temporary-alias fixture needs a .tmp journal")
            requested_report = self.journal.with_name(self.journal.name[:-4])
        else:
            require(self.phase.report_alias is None, "Unknown report alias fixture")
        self.argv = [str(self.native), "--port", str(listener.getsockname()[1]),
                     "--report", str(requested_report), "--deadline-ms", str(self.deadline_ms),
                     "--max-connections", "1", "--max-submissions", str(max(1, len(self.phase.wire_attempts))),
                     "--max-attempts", str(self.phase.limit), "--rows", "128", "--cols", "128",
                     "--seed-hex", self.fixture["seed_hex"], "--journal", str(self.journal),
                     *self.backend_argv]
        if self.phase.stop_on_limit:
            self.argv += ["--stop-on-attempt-limit", "1"]
        if self.phase.crash_point:
            self.argv += ["--crash-point", self.phase.crash_point,
                          "--crash-attempt-index", str(self.phase.crash_index)]
        env = os.environ.copy()
        env["RAYON_NUM_THREADS"] = "2"
        try:
            flags = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                     if os.name == "nt" else {"start_new_session": True})
            self.process = subprocess.Popen(self.argv, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **flags)
            self.event_thread = threading.Thread(target=self.capture_output, daemon=True)
            self.event_thread.start()
            deadline = started + self.deadline_ms / 1000 + 6
            while True:
                self.drain_events()
                if (self.process.poll() is not None and self.stdout_finished and
                        all(context.closed for context in self.ledger.connections.values())):
                    # A failed process could connect and exit before this
                    # selector first notices it. Do not call that "no TCP".
                    try:
                        unexpected, _address = listener.accept()
                    except BlockingIOError:
                        break
                    unexpected.close()
                    raise ReplayError("Unexpected queued connection from a finished process")
                require(time.monotonic() < deadline, "Recovery phase exceeded its process deadline")
                for selected, _ in self.selector.select(0.02):
                    if selected.data is None:
                        sock, address = listener.accept()
                        if address[0] != "127.0.0.1" or self.ledger.connections:
                            sock.close()
                            raise ReplayError("Unexpected peer or extra native connection")
                        sock.setblocking(False)
                        context = self.ledger.begin(1, sock)
                        self.selector.register(sock, selectors.EVENT_READ, context)
                    else:
                        context = selected.data
                        if context.closed:
                            continue
                        data = context.sock.recv(65536)
                        if not data:
                            context.reader.finish()
                            self.close(context)
                            continue
                        for message in context.reader.feed(data):
                            if message.get("method") == "mining.authorize":
                                self.handle_authorize(context, message)
                            elif message.get("method") == "mining.submit":
                                self.handle_submit(context, message)
                            else:
                                raise ReplayError("Unexpected client wire method")
            self.drain_events()
            report = None
            if self.native_report.is_file():
                require(self.native_report.stat().st_size <= MAX_JSON_FRAME, "Native report is oversized")
                report = strict_object(self.native_report.read_bytes())
                result["native_report"] = report
            self.validate_phase(report, self.process.returncode)
            result["passed"] = True
        except (OSError, ValueError, KeyError, struct.error, ReplayError, subprocess.SubprocessError) as error:
            result["error"] = str(error)
            result["native_log_tail"] = bytes(self.log_tail).decode("utf-8", errors="replace")
        finally:
            if self.process is not None and self.process.poll() is None:
                try:
                    stop_child(self.process)
                except (OSError, subprocess.TimeoutExpired) as error:
                    result.update(passed=False, cleanup_error=str(error))
            for context in self.ledger.connections.values():
                self.close(context)
            self.selector.unregister(listener)
            self.selector.close()
            if self.event_thread is not None:
                self.event_thread.join(timeout=1)
            if self.process is not None and self.process.stdout is not None and (
                    self.event_thread is None or not self.event_thread.is_alive()):
                self.process.stdout.close()
        result.update(native_returncode=None if self.process is None else self.process.returncode,
                      elapsed_seconds=round(time.monotonic() - started, 3), events=self.events,
                      submitted_proofs=self.proofs, connections=len(self.ledger.connections))
        if self.contender is not None:
            result["contender"] = self.contender
        return result


def scenario_phases(name: str, job: Job, fixtures: dict) -> list[Phase]:
    crashes = {"crash_before_compute": "before_compute", "crash_after_proof": "after_proof",
               "crash_before_send": "after_send_intent", "crash_after_ack": "after_ack"}
    if name in crashes:
        first_wire = (0,) if name == "crash_after_ack" else ()
        old = "accepted" if name == "crash_after_ack" else (
            "uncertain" if name == "crash_before_send" else "interrupted")
        outcomes = {old: 1}
        outcomes["accepted"] = outcomes.get("accepted", 0) + 1
        return [Phase("first", job, (0,), first_wire, crash_point=crashes[name]),
                Phase("resume", job, (1,), (1,), outcomes)]
    if name == "received_without_ack":
        return [Phase("first", job, (0,), (0,), acknowledgement="kill_after_receive"),
                Phase("resume", job, (1,), (1,), {"uncertain": 1, "accepted": 1})]
    if name == "encoding_change_restart":
        return [Phase("first", job, (0,), (0,), acknowledgement="kill_after_receive"),
                Phase("resume", job, (1,), (1,), {"uncertain": 1, "accepted": 1}, gzip=False)]
    if name == "rejected_then_restart":
        return [Phase("first", job, (0,), (0,), {"rejected": 1}, acknowledgement="reject"),
                Phase("resume", job, (1,), (1,), {"rejected": 1, "accepted": 1})]
    if name == "miss_prefix_restart":
        gold = fixtures["miss_then_found"]
        job = replace(job, target_hex=gold["target_be_hex"], nbits=gold["nbits"])
        return [Phase("first", job, (0, 1, 2, 3), (), crash_point="before_compute", crash_index=3),
                Phase("resume", job, (4, 5, 6, 7), (7,), {"miss": 6, "interrupted": 1, "accepted": 1})]
    if name == "changed_header_return":
        replacement = next(case for case in fixtures["cases"] if case["replacement_header"])
        changed = replace(job, header_hex=replacement["header_hex"])
        return [Phase("first", job, (0,), (0,), {"accepted": 1}),
                Phase("changed", changed, (0,), (0,), {"accepted": 2}),
                Phase("return", job, (1,), (1,), {"accepted": 3})]
    if name == "changed_target_return":
        changed = replace(job, target_hex="00" * 32, nbits="0x00000000")
        return [Phase("first", job, (0,), (0,), {"accepted": 1}),
                Phase("changed", changed, (0,), (), {"accepted": 1, "miss": 1}, limit=1, stop_on_limit=True),
                Phase("return", job, (1,), (1,), {"accepted": 2, "miss": 1})]
    if name == "exhausted_restart":
        job = replace(job, target_hex="00" * 32, nbits="0x00000000")
        return [Phase("first", job, (0, 1), (), {"miss": 2}, limit=2, stop_on_limit=True),
                Phase("resume", job, (), (), {"miss": 2}, limit=2, stop_on_limit=True)]
    if name in ("corrupt_journal", "torn_journal"):
        return [Phase("first", job, (0,), (0,), {"accepted": 1}),
                Phase("damaged", job, (), (), preflight_error=True)]
    if name == "report_path_alias":
        return [Phase("first", job, (0,), (0,), {"accepted": 1}),
                Phase("direct-alias", job, (), (), preflight_error=True, report_alias="journal"),
                Phase("temporary-alias", job, (), (), preflight_error=True, report_alias="temporary")]
    if name == "exclusive_writer":
        return [Phase("first", job, (0,), (0,), {"accepted": 1}, lock_contender=True),
                Phase("resume", job, (1,), (1,), {"accepted": 2})]
    raise ReplayError("Unknown recovery scenario")


def replay(args) -> dict:
    backend_arguments(args.backend, args.device)
    native, reference = args.native.resolve(), args.reference.resolve()
    config_path = ROOT / "configs/proof-build.json"
    config = json.loads(config_path.read_text())
    build = json.loads((ROOT / "artifacts/proof-build.json").read_text())
    require(build.get("passed") is True and
            sha256(reference.read_bytes()) == build["artifacts"]["reference_checker"]["sha256"] and
            build.get("config_sha256") == sha256(config_path.read_bytes()),
            "Official checker/configuration does not match the successful pinned proof build")
    proof_path = ROOT / "native/fixtures/proof_manifest.json"
    proof_fixtures = json.loads(proof_path.read_text())
    fixture = next(item for item in proof_fixtures["cases"] if item["name"] == "v3_zero_b")
    attempt_path = ROOT / "native/fixtures/attempt_manifest.json"
    attempt_fixtures = json.loads(attempt_path.read_text())
    require(attempt_fixtures.get("schema") == "neuro.attempt_fixtures.v1" and
            attempt_fixtures.get("root_seed_hex") == fixture["seed_hex"] and
            attempt_fixtures.get("historical_attempt0_exact") is True,
            "Unexpected independent attempt fixture manifest")
    require(all(sha256((ROOT / name).read_bytes()) == digest
                for name, digest in attempt_fixtures["sources_sha256"].items()) and
            sha256((ROOT / "native/fixtures/attempt_golden.hpp").read_bytes()) == attempt_fixtures["header_sha256"],
            "Independent attempt fixture sources changed")
    require(native.is_file() and os.access(native, os.X_OK), "Native replay client is not executable")
    args.work_dir.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=args.work_dir.resolve()))
    results = []
    for name in SCENARIOS:
        if args.scenario and name not in args.scenario:
            continue
        work = run_dir / name
        work.mkdir()
        journal = work / ("search.journal.tmp" if name == "report_path_alias" else "search.journal")
        job = Job("recovery-same-id", fixture["header_hex"])
        result = {"name": name, "passed": True, "phases": []}
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind(("127.0.0.1", 0))
            listener.listen(2)
            listener.setblocking(False)
            damaged = None
            for number, phase in enumerate(scenario_phases(name, job, attempt_fixtures)):
                if phase.preflight_error and name in ("corrupt_journal", "torn_journal"):
                    raw = bytearray(journal.read_bytes())
                    require(len(raw) > 32, "Valid journal is too short for a corruption fixture")
                    if name == "corrupt_journal":
                        raw[len(raw) // 2] ^= 0x40
                    else:
                        del raw[-7:]
                    damaged = bytes(raw)
                    journal.write_bytes(damaged)
                if phase.report_alias:
                    damaged = journal.read_bytes()
                runner = RecoveryPhase(phase, journal=journal, native=native, reference=reference,
                                       commit=config["reference_commit"], fixture=fixture,
                                       work=work / f"{number}-{phase.name}", deadline_ms=args.deadline_ms,
                                       backend=args.backend, device=args.device, attempt_fixtures=attempt_fixtures)
                observed = runner.run_on_listener(listener)
                result["phases"].append(observed)
                if damaged is not None:
                    require(journal.read_bytes() == damaged,
                            "A damaged journal was changed/reset by refused recovery")
                if not observed["passed"]:
                    result["passed"] = False
                    break
            # Every normally finished restart must allocate a distinct durable
            # connection epoch, even though JSON-RPC request IDs can start over.
            connections = [item["connection"] for phase in result["phases"]
                           for item in phase["events"] if item["event"] == "connected"]
            require(len(connections) == len(set(connections)), "Connection identity was reused across processes")
            if name == "encoding_change_restart" and result["passed"]:
                first, resumed = result["phases"]
                bindings = [[event for event in phase["events"] if event["event"] == "journal_bound"]
                            for phase in (first, resumed)]
                require(len(bindings[0]) == len(bindings[1]) == 1 and
                        bindings[0][0]["work_id"] == bindings[1][0]["work_id"] and
                        resumed["native_report"]["journal"].get("work_count") == 1 and
                        resumed["native_report"]["journal"]["works"][0].get("last_gzip") is False,
                        "A transport-only encoding change created a fresh search identity")
                old_proof, new_proof = first["submitted_proofs"][0], resumed["submitted_proofs"][0]
                require(old_proof["gzip"] is True and new_proof["gzip"] is False and
                        old_proof["wire_sha256"] != old_proof["proof_sha256"] and
                        new_proof["wire_sha256"] == new_proof["proof_sha256"] and
                        old_proof["proof_sha256"] != new_proof["proof_sha256"],
                        "Encoding change did not preserve progress while changing the actual wire format")
        except (OSError, ValueError, KeyError, ReplayError) as error:
            result.update(passed=False, error=str(error))
        finally:
            listener.close()
        results.append(result)
        print(json.dumps({"scenario": name, "passed": result["passed"],
                          "phases": len(result["phases"]), "error": result.get("error")}), flush=True)
    phases = [phase for result in results for phase in result["phases"]]
    return {"schema": "neuro.pool_recovery.v1", "passed": bool(results) and all(r["passed"] for r in results),
            "transport": "loopback_tcp_fresh_process_restarts", "backend": args.backend,
            "device": (0 if args.device is None else args.device) if args.backend == "cuda" else None,
            "scenarios_run": len(results), "process_phases_run": len(phases), "scenarios": results,
            "submitted_proofs_checked": sum(len(phase["submitted_proofs"]) for phase in phases),
            "official_reference_commit": config["reference_commit"],
            "attempt_fixture_manifest_sha256": sha256(attempt_path.read_bytes()),
            "proof_fixture_manifest_sha256": sha256(proof_path.read_bytes()),
            "binaries": {"native_sha256": sha256(native.read_bytes()), "official_sha256": sha256(reference.read_bytes())},
            "work_dir": str(run_dir), "external_pool_connection": False, "live_pool_acceptance": False,
            "model_api_calls": False, "power_loss_tested": False,
            "cpu_execution": any(phase.get("native_report", {}).get("cpu_execution") is True for phase in phases),
            "gpu_execution": any(phase.get("native_report", {}).get("gpu_execution") is True for phase in phases),
            "windows_execution": os.name == "nt" and any("native_report" in phase for phase in phases)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    native_dir = "build/windows" if os.name == "nt" else "build/proof-cmake"
    target = "x86_64-pc-windows-msvc" if os.name == "nt" else "x86_64-unknown-linux-gnu"
    suffix = ".exe" if os.name == "nt" else ""
    parser.add_argument("--native", type=Path, default=ROOT / native_dir / ("neuro_pool_replay_client" + suffix))
    parser.add_argument("--reference", type=Path, default=ROOT / "build/proof-target" / target /
                        "release" / ("pearl-proof-check" + suffix))
    parser.add_argument("--work-dir", type=Path, default=ROOT / "artifacts/pool-recovery")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/pool-recovery.json")
    parser.add_argument("--scenario", action="append", choices=SCENARIOS)
    parser.add_argument("--deadline-ms", type=int, default=8000)
    parser.add_argument("--backend", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--device", type=int)
    args = parser.parse_args()
    try:
        require(1000 <= args.deadline_ms <= 30000, "Native deadline must be between 1000 and 30000 ms")
        report = replay(args)
    except (OSError, ValueError, KeyError, StopIteration, ReplayError) as error:
        report = {"schema": "neuro.pool_recovery.v1", "passed": False, "error": str(error)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: report.get(key) for key in
                      ("schema", "passed", "scenarios_run", "process_phases_run", "submitted_proofs_checked", "error")}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
