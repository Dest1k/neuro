#!/usr/bin/env python3
"""Exercise the real native host/proof path against a finite loopback TCP peer.

The wire dialect comes from pinned CPPminer (LuckyPool/Kryptex mining.*).
Every submitted proof is checked by the separately pinned official Pearl
verifier. Synthetic peer replies are not evidence of live pool acceptance.
"""
from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass, field, replace
import hashlib
import heapq
import json
import math
import os
from pathlib import Path
import queue
import selectors
import signal
import socket
import struct
import subprocess
import threading
import time
from typing import Callable
import zlib

from check_pearl_proofs import inspect_proof
from neuroctl.experiments import run_command

ROOT = Path(__file__).resolve().parents[1]
MAX_RAW_PROOF = 1024 * 1024
MAX_ENCODED_PROOF = 4 * ((MAX_RAW_PROOF + 2) // 3)
MAX_JSON_FRAME = 2 * 1024 * 1024
MAX_EVENT_LINE = 64 * 1024
MAX_NATIVE_LOG = 512 * 1024
EVENT_PREFIX = b"NEURO_EVENT "
TARGET = (1 << 235).to_bytes(32, "big").hex()
HALF_TARGET = (1 << 234).to_bytes(32, "big").hex()


class ReplayError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ReplayError(message)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def strict_object(raw: bytes, *, limit: int = MAX_JSON_FRAME) -> dict:
    require(len(raw) <= limit, "JSON frame exceeds its byte bound")

    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "Duplicate JSON object member")
            result[key] = value
        return result

    def constant(_value):
        raise ReplayError("Non-finite JSON number")

    def finite_float(text):
        value = float(text)
        require(math.isfinite(value), "Non-finite JSON number")
        return value

    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                            parse_constant=constant, parse_float=finite_float)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise ReplayError("Invalid UTF-8/JSON frame or excessive nesting") from error
    require(type(result) is dict, "JSON-RPC frame must be an object")
    return result


class JSONLines:
    """Bound the incoming request stream independently of the native framer."""

    def __init__(self, limit: int = MAX_JSON_FRAME):
        self.buffer = bytearray()
        self.limit = limit

    def feed(self, data: bytes) -> list[dict]:
        self.buffer.extend(data)
        result = []
        while True:
            end = self.buffer.find(b"\n")
            if end < 0:
                require(len(self.buffer) <= self.limit, "Unterminated JSON frame exceeds its byte bound")
                return result
            require(end <= self.limit, "JSON frame exceeds its byte bound")
            line = bytes(self.buffer[:end])
            del self.buffer[:end + 1]
            require(bool(line.strip()), "Empty JSON-RPC request")
            result.append(strict_object(line, limit=self.limit))

    def finish(self) -> None:
        require(not self.buffer.strip(), "Connection ended with a partial JSON request")


def decode_proof(encoded: str, *, gzip_negotiated: bool,
                 max_raw: int = MAX_RAW_PROOF) -> tuple[bytes, bytes]:
    require(type(encoded) is str and bool(encoded), "plain_proof must be a nonempty string")
    require(len(encoded) <= MAX_ENCODED_PROOF, "Encoded proof exceeds its byte bound")
    try:
        wire = base64.b64decode(encoded, validate=True)
    except (ValueError, UnicodeError) as error:
        raise ReplayError("Invalid proof base64") from error
    require(len(wire) <= MAX_RAW_PROOF, "Decoded wire proof exceeds its byte bound")
    if not gzip_negotiated:
        require(not wire.startswith(b"\x1f\x8b"), "Gzip proof sent without negotiated v2")
        require(len(wire) <= max_raw, "Plain proof exceeds its byte bound")
        return wire, wire
    require(wire.startswith(b"\x1f\x8b"), "Negotiated v2 requires a gzip proof")
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        raw = decoder.decompress(wire, max_raw + 1)
    except zlib.error as error:
        raise ReplayError("Invalid gzip proof") from error
    require(len(raw) <= max_raw and not decoder.unconsumed_tail,
            "Gzip proof exceeds its expanded byte bound")
    require(decoder.eof, "Truncated gzip proof")
    require(not decoder.unused_data, "Trailing bytes or multiple gzip members")
    require(bool(raw), "Empty proof")
    return raw, wire


@dataclass(frozen=True)
class Job:
    job_id: str
    header_hex: str
    target_hex: str = TARGET
    cert_version: int = 3
    nbits: str = "0x1e080000"

    def notify(self) -> dict:
        return {"jsonrpc": "2.0", "method": "mining.notify", "params": {
            "job_id": self.job_id, "header": self.header_hex,
            "target": self.target_hex, "cert_version": self.cert_version}}


@dataclass(frozen=True)
class IssuedJob:
    connection: int
    revision: int
    job: Job


@dataclass
class Connection:
    epoch: int
    sock: socket.socket | None = None
    reader: JSONLines = field(default_factory=JSONLines)
    requests: set[int] = field(default_factory=set)
    authorized: bool = False
    gzip: bool = False
    current: IssuedJob | None = None
    closed: bool = False


class PeerLedger:
    """Server-owned jobs and request IDs; it never trusts client proof labels."""

    def __init__(self):
        self.connections: dict[int, Connection] = {}
        self.issued: list[IssuedJob] = []

    def begin(self, epoch: int, sock: socket.socket | None = None) -> Connection:
        require(type(epoch) is int and epoch > 0 and epoch not in self.connections,
                "Invalid or reused connection epoch")
        context = Connection(epoch, sock)
        self.connections[epoch] = context
        return context

    def request(self, context: Connection, message: dict, method: str) -> tuple[int, dict]:
        require(not context.closed, "Request on a closed connection")
        require(set(message) == {"jsonrpc", "id", "method", "params"},
                "Unexpected JSON-RPC request envelope")
        require(message["jsonrpc"] == "2.0" and message["method"] == method,
                "Unexpected JSON-RPC version or method")
        request_id = message["id"]
        require(type(request_id) is int and 0 < request_id <= 0x7fffffff,
                "Request id must be a positive signed 32-bit integer")
        require(request_id not in context.requests, "Reused request id within one connection")
        require(type(message["params"]) is dict, "Request params must be an object")
        context.requests.add(request_id)
        return request_id, message["params"]

    def authorize(self, context: Connection, message: dict) -> int:
        require(not context.authorized and not context.requests, "Unexpected repeated authorization")
        request_id, params = self.request(context, message, "mining.authorize")
        require(params == {"wallet": "offline-wallet", "worker": "replay-worker",
                           "agent": "neuro-replay", "password": "x", "type": "v2"},
                "Replay authorization must use the fixed synthetic credentials")
        return request_id

    def issue(self, context: Connection, job: Job) -> IssuedJob:
        require(context.authorized and not context.closed, "Job issued before authorization or after close")
        if context.current and context.current.job == job:
            return context.current
        revision = 1 + sum(item.connection == context.epoch for item in self.issued)
        context.current = IssuedJob(context.epoch, revision, job)
        self.issued.append(context.current)
        return context.current

    def submission(self, context: Connection, message: dict) -> tuple[int, IssuedJob, str]:
        require(context.authorized and context.current is not None,
                "Submit arrived before an authorized job")
        request_id, params = self.request(context, message, "mining.submit")
        require(set(params) == {"job_id", "plain_proof", "hs"}, "Unexpected submit params")
        require(type(params["job_id"]) is str and params["job_id"] == context.current.job.job_id,
                "Submit does not refer to the current server job")
        hs = params["hs"]
        try:
            finite = type(hs) in (int, float) and math.isfinite(hs)
        except OverflowError:
            finite = False
        require(finite and hs >= 0,
                "Submit hs must be a finite nonnegative number")
        require(type(params["plain_proof"]) is str, "plain_proof must be a string")
        return request_id, context.current, params["plain_proof"]


def require_official_acceptance(process: dict, result: dict, commit: str) -> None:
    require(not process.get("timed_out") and not process.get("launch_error") and
            process.get("output_complete") is True, "Official verifier process did not finish completely")
    require(process.get("returncode") == 0 and result.get("verified") is True and
            result.get("stage") == "verified" and result.get("upstream_commit") == commit,
            "Submitted proof was not accepted by the pinned official verifier")


def backend_arguments(backend: str, device: int | None) -> list[str]:
    require(backend in ("cpu", "cuda"), "Backend must be cpu or cuda")
    if backend == "cpu":
        require(device is None, "--device is only valid with --backend cuda")
        return ["--backend", "cpu"]
    device = 0 if device is None else device
    require(type(device) is int and 0 <= device <= 15, "CUDA device must be an integer from 0 through 15")
    return ["--backend", "cuda", "--device", str(device)]


def require_backend_execution(report: dict, backend: str) -> None:
    require(report.get("backend") == backend and
            report.get("cpu_execution") is (backend == "cpu") and
            report.get("gpu_execution") is (backend == "cuda"),
            "Replay did not observe successful execution of the explicitly selected backend")


@dataclass(frozen=True)
class Scenario:
    name: str
    gzip: bool = True
    cert_version: int = 3
    pause_stage: str | None = None
    connections: int = 1
    submissions: int = 1


SCENARIOS = (
    Scenario("gzip_happy"),
    Scenario("plain_happy", gzip=False, cert_version=2),
    Scenario("wrong_id_then_reject", submissions=2),
    Scenario("stale_response", submissions=2),
    Scenario("duplicate_notify"),
    Scenario("cancel_before_compute", pause_stage="before_compute"),
    Scenario("cancel_after_compute_header", pause_stage="after_compute"),
    Scenario("cancel_after_proof_cert", pause_stage="after_proof"),
    Scenario("cancel_after_compute_target", pause_stage="after_compute"),
    Scenario("reconnect_after_verify", pause_stage="after_verify", connections=2),
    Scenario("malformed_notify_recovery"),
    Scenario("malformed_frame_reconnect", connections=2),
    Scenario("authorize_rejected_reconnect", connections=2),
    Scenario("unacknowledged_submit"),
    Scenario("ack_timeout"),
)


def encode_message(message: dict) -> bytes:
    return json.dumps(message, separators=(",", ":"), allow_nan=False).encode() + b"\n"


def stop_child(process: subprocess.Popen, *, grace: float = 1.0) -> None:
    """Stop only the session created for this native client, with two bounds."""
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=grace)


class ReplayCase:
    def __init__(self, scenario: Scenario, *, native: Path, reference: Path,
                 commit: str, fixture: dict, work: Path, deadline_ms: int,
                 backend: str = "cpu", device: int | None = None):
        self.scenario, self.native, self.reference, self.commit = scenario, native, reference, commit
        self.fixture, self.work, self.deadline_ms = fixture, work, deadline_ms
        self.backend = backend
        self.backend_argv = backend_arguments(backend, device)
        self.work.mkdir(parents=True, exist_ok=True)
        self.ledger = PeerLedger()
        self.events: list[dict] = []
        self.proofs: list[dict] = []
        self.event_queue: queue.Queue = queue.Queue()
        self.scheduled: list[tuple[float, int, Callable]] = []
        self.sequence = 0
        self.checkpoints = 0
        self.continuation_sent = False
        self.stdout_finished = False
        self.log_tail = bytearray()
        self.selector = selectors.DefaultSelector()
        self.process: subprocess.Popen | None = None
        self.event_thread: threading.Thread | None = None
        self.initial_job = Job(f"{scenario.name}-job", fixture["header_hex"],
                               cert_version=scenario.cert_version)
        self.native_report = self.work / "native-report.json"

    def later(self, delay: float, callback: Callable) -> None:
        self.sequence += 1
        heapq.heappush(self.scheduled, (time.monotonic() + delay, self.sequence, callback))

    def send(self, context: Connection, payload: bytes, *, delivered: Callable | None = None,
             fragmented: bool = False) -> None:
        require(not context.closed and context.sock is not None, "Cannot send to a closed peer")

        def write(data: bytes):
            require(not context.closed and context.sock is not None, "Peer closed during a response")
            context.sock.settimeout(1.0)
            try:
                context.sock.sendall(data)
            finally:
                context.sock.setblocking(False)

        if fragmented:
            write(payload[:11])

            def rest():
                write(payload[11:])
                if delivered:
                    delivered()
            self.later(0.025, rest)
        else:
            write(payload)
            if delivered:
                delivered()

    def send_job(self, context: Connection, job: Job) -> None:
        self.send(context, encode_message(job.notify()),
                  delivered=lambda: self.ledger.issue(context, job))

    def close(self, context: Connection) -> None:
        if context.closed:
            return
        context.closed = True
        if context.sock is not None:
            try:
                self.selector.unregister(context.sock)
            except (KeyError, OSError, ValueError):
                pass
            try:
                context.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            context.sock.close()

    def handle_authorize(self, context: Connection, message: dict) -> None:
        request_id = self.ledger.authorize(context, message)
        name = self.scenario.name
        if name == "authorize_rejected_reconnect" and context.epoch == 1:
            self.send(context, encode_message({"jsonrpc": "2.0", "id": request_id,
                      "result": False, "error": {"code": 24, "message": "synthetic authorization rejection"}}))
            return
        use_gzip = self.scenario.gzip and not (name == "reconnect_after_verify" and context.epoch == 2)
        response = {"jsonrpc": "2.0", "id": request_id, "result": True}
        if use_gzip:
            response["type"] = "v2"
        messages = encode_message(response)
        jobs = []
        if name == "malformed_frame_reconnect" and context.epoch == 1:
            messages += b"{]\n"
        else:
            if name == "gzip_happy":
                messages += encode_message({"jsonrpc": "2.0", "method": "mining.set_difficulty", "params": [32]})
            if name == "malformed_notify_recovery":
                bad_cert = replace(self.initial_job, job_id="bad-cert", cert_version=99).notify()
                bad_header = replace(self.initial_job, job_id="bad-header", header_hex=self.initial_job.header_hex[:-2]).notify()
                no_id = self.initial_job.notify()
                del no_id["params"]["job_id"]
                messages += b"".join(encode_message(value) for value in (bad_cert, bad_header, no_id))
            jobs.append(self.initial_job)
            messages += encode_message(self.initial_job.notify())
            if name == "duplicate_notify":
                jobs.append(self.initial_job)
                messages += encode_message(self.initial_job.notify())

        def delivered():
            context.authorized, context.gzip = True, use_gzip
            for job in jobs:
                self.ledger.issue(context, job)

        self.send(context, messages, delivered=delivered, fragmented=name == "gzip_happy")

    def handle_submit(self, context: Connection, message: dict) -> None:
        request_id, issued, encoded = self.ledger.submission(context, message)
        raw, wire = decode_proof(encoded, gzip_negotiated=context.gzip)
        decoded = inspect_proof(raw)
        require(decoded["m"] == 128 and decoded["n"] == 128, "Proof dimensions differ from the replay profile")
        proof_file = self.work / f"proof-c{context.epoch}-id{request_id}.b64"
        proof_file.write_bytes(base64.b64encode(raw))
        process = run_command([str(self.reference), "--header-hex", issued.job.header_hex,
                               "--proof-base64-file", str(proof_file), "--cert-version",
                               str(issued.job.cert_version), "--nbits", issued.job.nbits],
                              cwd=ROOT, timeout_s=15, max_log_bytes=16384,
                              stage="loopback-submitted-proof-verification")
        result = strict_object(process["stdout"].encode(), limit=16384)
        planned_rejection = self.scenario.submissions == 2 and not self.proofs
        no_ack = self.scenario.name in ("unacknowledged_submit", "ack_timeout")
        record = {"connection": context.epoch, "id": request_id, "revision": issued.revision,
                  "job_id": issued.job.job_id, "cert_version": issued.job.cert_version,
                  "nbits": issued.job.nbits, "header_sha256": sha256(bytes.fromhex(issued.job.header_hex)),
                  "target_be_hex": issued.job.target_hex, "gzip": context.gzip,
                  "wire_bytes": len(wire), "raw_bytes": len(raw),
                  "wire_sha256": sha256(wire), "proof_sha256": sha256(raw),
                  "official_verifier": result, "official_process_returncode": process["returncode"],
                  "synthetic_peer_accepted": None}
        self.proofs.append(record)
        require_official_acceptance(process, result, self.commit)
        if no_ack:
            if self.scenario.name == "unacknowledged_submit":
                self.close(context)
            # ack_timeout deliberately keeps this socket open without sending
            # a reply; only the native pending-response watchdog may end it.
            return
        record["synthetic_peer_accepted"] = not planned_rejection
        reply = {"jsonrpc": "2.0", "id": request_id, "result": not planned_rejection}
        if planned_rejection:
            stale = self.scenario.name == "stale_response"
            reply["error"] = {"code": 21 if stale else 23,
                              "message": "synthetic stale share" if stale else "synthetic policy rejection"}
        payload = encode_message(reply)
        if self.scenario.name == "wrong_id_then_reject" and planned_rejection:
            payload = encode_message({"jsonrpc": "2.0", "id": request_id + 1000000,
                                      "result": True}) + payload
        self.send(context, payload)

    def on_event(self, event: dict) -> None:
        require(type(event.get("event")) is str, "Native event has no event name")
        self.events.append(event)
        if event["event"] == "checkpoint":
            self.checkpoints += 1
            require(self.checkpoints == 1 and event.get("stage") == self.scenario.pause_stage and
                    event.get("job_id") == self.initial_job.job_id,
                    "Unexpected native checkpoint")
            context = self.ledger.connections[event["connection"]]
            if self.scenario.name == "reconnect_after_verify":
                self.close(context)
                return
            job = self.initial_job
            if self.scenario.name == "cancel_before_compute":
                job = replace(job, job_id=job.job_id + "-replacement")
            elif self.scenario.name == "cancel_after_compute_header":
                header = bytearray.fromhex(job.header_hex)
                header[75] ^= 1
                job = replace(job, header_hex=header.hex())
            elif self.scenario.name == "cancel_after_proof_cert":
                job = replace(job, cert_version=2)
            elif self.scenario.name == "cancel_after_compute_target":
                job = replace(job, target_hex=HALF_TARGET, nbits="0x1e040000")
            else:
                raise ReplayError("Checkpoint in a scenario without a cancellation action")
            self.send_job(context, job)
        if (event["event"] == "share_response" and self.scenario.submissions == 2 and
                not self.continuation_sent and self.proofs and event.get("id") == self.proofs[0]["id"]):
            require(event.get("accepted") is False, "Native client counted a planned rejection as accepted")
            context = self.ledger.connections[event["connection"]]
            self.continuation_sent = True
            self.send_job(context, replace(self.initial_job, job_id=self.initial_job.job_id + "-next"))

    def capture_output(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        total = 0
        try:
            with (self.work / "native.log").open("wb") as log:
                while True:
                    line = self.process.stdout.readline(MAX_EVENT_LINE + 1)
                    if not line:
                        break
                    total += len(line)
                    require(len(line) <= MAX_EVENT_LINE and total <= MAX_NATIVE_LOG,
                            "Native output exceeded its byte bound")
                    log.write(line)
                    self.log_tail.extend(line)
                    if len(self.log_tail) > 4096:
                        del self.log_tail[:-4096]
                    if line.startswith(EVENT_PREFIX):
                        self.event_queue.put(("event", strict_object(line[len(EVENT_PREFIX):], limit=MAX_EVENT_LINE)))
        except Exception as error:
            # A background reader failure must reach the main case verdict;
            # a thread traceback followed only by EOF could otherwise hide it.
            self.event_queue.put(("error", f"{type(error).__name__}: {error}"))
        finally:
            self.event_queue.put(("end", None))

    def drain_events(self) -> None:
        while True:
            try:
                kind, value = self.event_queue.get_nowait()
            except queue.Empty:
                return
            if kind == "event":
                self.on_event(value)
            elif kind == "error":
                raise ReplayError(value)
            else:
                self.stdout_finished = True

    def validate_result(self, report: dict, returncode: int) -> None:
        no_ack = self.scenario.name in ("unacknowledged_submit", "ack_timeout")
        require(report.get("schema") == "neuro.pool_client.v1", "Unexpected native report schema")
        if no_ack:
            require(returncode == 1 and report.get("completed") is False and
                    report.get("reason") == "connection_limit",
                    "Unacknowledged submission was incorrectly counted as a completed goal")
        else:
            require(returncode == 0 and report.get("completed") is True and
                    report.get("reason") == "submission_limit",
                    "Native client did not finish its bounded submission/response goal")
        require_backend_execution(report, self.backend)
        counters = report.get("counters")
        require(type(counters) is dict, "Native report has no counters")
        require(all(type(value) is int and value >= 0 for value in counters.values()),
                "Native counters must be nonnegative integers")
        expected = {"connections": self.scenario.connections, "submissions": self.scenario.submissions,
                    "accepted": 0 if no_ack else 1, "rejected": self.scenario.submissions - 1,
                    "pending_lost": int(no_ack), "errors": 0,
                    "duplicates": int(self.scenario.name == "duplicate_notify"),
                    "unmatched_responses": int(self.scenario.name == "wrong_id_then_reject"),
                    "cancelled": int(self.scenario.pause_stage is not None),
                    "checkpoints": int(self.scenario.pause_stage is not None),
                    "jobs_started": len(self.ledger.issued)}
        for key, value in expected.items():
            require(counters.get(key) == value, f"Unexpected native counter {key}: {counters.get(key)} != {value}")
        if self.scenario.name == "malformed_notify_recovery":
            require(counters.get("protocol_errors", 0) >= 3, "Malformed notifications were not rejected")
        if self.scenario.name == "malformed_frame_reconnect":
            require(any(event["event"] == "disconnected" and event.get("connection") == 1 and
                        event.get("reason") == "connection_lost_or_bad_frame" for event in self.events),
                    "Malformed frame did not cause an observed framing disconnect")
        if self.scenario.name == "ack_timeout":
            require(any(event["event"] == "disconnected" and event.get("connection") == 1 and
                        event.get("reason") == "submit_response_timeout" for event in self.events),
                    "Open connection with no ACK did not trigger the native response watchdog")
        require(len(self.proofs) == self.scenario.submissions, "Unexpected number of wire submissions")
        require(len(self.ledger.connections) == self.scenario.connections, "Unexpected TCP connection count")
        require(self.checkpoints == int(self.scenario.pause_stage is not None), "Missing cancellation checkpoint")

        generations = {}
        for epoch in self.ledger.connections:
            sent = [item for item in self.ledger.issued if item.connection == epoch]
            received = [event for event in self.events if event["event"] == "job_received" and event.get("connection") == epoch]
            require(len(sent) == len(received), "Native accepted a different number of jobs than the peer issued")
            for issued, event in zip(sent, received):
                require(event.get("job_id") == issued.job.job_id and
                        type(event.get("generation")) is int and event["generation"] > 0,
                        "Native job event does not match the server ledger")
                generations[epoch, issued.revision] = event["generation"]
            require(len({event["generation"] for event in received}) == len(received), "Reused generation for changed work")
        submitted = [event for event in self.events if event["event"] == "submitted"]
        responses = [event for event in self.events if event["event"] == "share_response"]
        require(len(submitted) == len(self.proofs) and len(responses) == (0 if no_ack else len(self.proofs)),
                "Wire submissions and native submit/response events differ")
        for proof in self.proofs:
            matches = [event for event in submitted if event.get("id") == proof["id"] and event.get("connection") == proof["connection"]]
            replies = [event for event in responses if event.get("id") == proof["id"] and event.get("connection") == proof["connection"]]
            require(len(matches) == 1 and len(replies) == (0 if no_ack else 1),
                    "Missing or duplicate native submit/response identity")
            require(matches[0].get("generation") == generations[proof["connection"], proof["revision"]] and
                    matches[0].get("job_id") == proof["job_id"], "Stale generation reached the wire")
            if no_ack:
                unresolved = [event for event in self.events if event["event"] == "share_unresolved" and
                              event.get("id") == proof["id"] and event.get("connection") == proof["connection"]]
                require(len(unresolved) == 1, "Lost submission did not remain explicitly unresolved")
            else:
                require(type(replies[0].get("accepted")) is bool and
                        replies[0]["accepted"] == proof["synthetic_peer_accepted"], "Submit reply was misclassified")
        if self.scenario.pause_stage:
            checkpoint = next(event for event in self.events if event["event"] == "checkpoint")
            require(any(event["event"] == "attempt_finished" and event.get("status") == "cancelled" and
                        event.get("generation") == checkpoint["generation"] for event in self.events),
                    "The paused old generation did not finish as cancelled")

    def run(self) -> dict:
        started = time.monotonic()
        result = {"name": self.scenario.name, "passed": False}
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(2)
        listener.setblocking(False)
        self.selector.register(listener, selectors.EVENT_READ, None)
        self.native_report.unlink(missing_ok=True)
        argv = [str(self.native), "--port", str(listener.getsockname()[1]),
                "--report", str(self.native_report), "--deadline-ms", str(self.deadline_ms),
                "--max-connections", str(self.scenario.connections),
                "--max-submissions", str(self.scenario.submissions), "--rows", "128", "--cols", "128",
                "--seed-hex", self.fixture["seed_hex"], *self.backend_argv]
        if self.scenario.pause_stage:
            argv += ["--pause-stage", self.scenario.pause_stage, "--pause-job-id", self.initial_job.job_id]
        env = os.environ.copy()
        env["RAYON_NUM_THREADS"] = "2"
        try:
            self.process = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                            start_new_session=True)
            self.event_thread = threading.Thread(target=self.capture_output, daemon=True)
            self.event_thread.start()
            deadline = started + self.deadline_ms / 1000 + 6
            while True:
                self.drain_events()
                now = time.monotonic()
                while self.scheduled and self.scheduled[0][0] <= now:
                    _, _, callback = heapq.heappop(self.scheduled)
                    callback()
                if (self.process.poll() is not None and self.stdout_finished and
                        all(context.closed for context in self.ledger.connections.values())):
                    break
                require(now < deadline, "Loopback replay exceeded its bounded deadline")
                for selected, _ in self.selector.select(0.02):
                    if selected.data is None:
                        sock, address = listener.accept()
                        require(address[0] == "127.0.0.1", "Unexpected non-loopback peer")
                        epoch = len(self.ledger.connections) + 1
                        require(epoch <= self.scenario.connections, "Native client exceeded the allowed reconnect count")
                        sock.setblocking(False)
                        context = self.ledger.begin(epoch, sock)
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
            require(self.native_report.is_file() and self.native_report.stat().st_size <= MAX_JSON_FRAME,
                    "Native client did not write its bounded report")
            report = strict_object(self.native_report.read_bytes())
            self.validate_result(report, self.process.returncode)
            result.update(passed=True, native_report=report)
        except (OSError, ValueError, KeyError, struct.error, ReplayError, subprocess.SubprocessError) as error:
            result["error"] = str(error)
            result["native_log_tail"] = bytes(self.log_tail).decode("utf-8", errors="replace")
            if self.native_report.is_file() and self.native_report.stat().st_size <= MAX_JSON_FRAME:
                try:
                    result["native_report"] = strict_object(self.native_report.read_bytes())
                except ReplayError:
                    pass
        finally:
            if self.process is not None and self.process.poll() is None:
                try:
                    stop_child(self.process)
                except (OSError, subprocess.TimeoutExpired) as error:
                    result["passed"] = False
                    result["cleanup_error"] = str(error)
            for context in self.ledger.connections.values():
                self.close(context)
            self.selector.unregister(listener)
            listener.close()
            self.selector.close()
            if self.event_thread is not None:
                self.event_thread.join(timeout=1)
            if (self.process is not None and self.process.stdout is not None and
                    (self.event_thread is None or not self.event_thread.is_alive())):
                self.process.stdout.close()
            result["native_returncode"] = None if self.process is None else self.process.returncode
        result.update(elapsed_seconds=round(time.monotonic() - started, 3),
                      events=self.events, submitted_proofs=self.proofs,
                      issued_jobs=[{"connection": item.connection, "revision": item.revision,
                                    "job_id": item.job.job_id, "cert_version": item.job.cert_version,
                                    "nbits": item.job.nbits,
                                    "header_sha256": sha256(bytes.fromhex(item.job.header_hex)),
                                    "target_be_hex": item.job.target_hex} for item in self.ledger.issued])
        return result


def replay(args) -> dict:
    backend_arguments(args.backend, args.device)
    native, reference = args.native.resolve(), args.reference.resolve()
    config = json.loads((ROOT / "configs/proof-build.json").read_text())
    build = json.loads((ROOT / "artifacts/proof-build.json").read_text())
    require(build.get("passed") is True and
            sha256(reference.read_bytes()) == build["artifacts"]["reference_checker"]["sha256"],
            "Official checker does not match the successful pinned proof build")
    require(build.get("config_sha256") == sha256((ROOT / "configs/proof-build.json").read_bytes()),
            "Proof build configuration changed")
    fixture_path = ROOT / "native/fixtures/proof_manifest.json"
    fixtures = json.loads(fixture_path.read_text())
    fixture = next(item for item in fixtures["cases"] if item["name"] == "v3_zero_b")
    require(fixtures["pool_target_be_hex"] == TARGET and fixtures["pool_target_nbits"] == "0x1e080000" and
            fixture["shape"] == {"rows": 128, "cols": 128, "inner": 4096}, "Unexpected independent replay fixture")
    require(len(bytes.fromhex(fixture["header_hex"])) == 76 and len(bytes.fromhex(fixture["seed_hex"])) == 32,
            "Unexpected independent header/seed length")
    require(native.is_file() and os.access(native, os.X_OK), "Native replay client is not executable")
    os.environ["RAYON_NUM_THREADS"] = "2"
    selected = [item for item in SCENARIOS if not args.scenario or item.name in args.scenario]
    results = []
    for scenario in selected:
        case = ReplayCase(scenario, native=native, reference=reference,
                          commit=config["reference_commit"], fixture=fixture,
                          work=args.work_dir.resolve() / scenario.name, deadline_ms=args.deadline_ms,
                          backend=args.backend, device=args.device)
        result = case.run()
        results.append(result)
        print(json.dumps({"scenario": result["name"], "passed": result["passed"],
                          "proofs": len(result["submitted_proofs"]), "error": result.get("error")}), flush=True)
    return {"schema": "neuro.pool_replay.v1", "passed": all(item["passed"] for item in results),
            "transport": "loopback_tcp", "protocol": "CPPminer_LuckyPool_Kryptex_dialect",
            "backend": args.backend, "device": (0 if args.device is None else args.device) if args.backend == "cuda" else None,
            "scenarios_run": len(results), "scenarios": results,
            "submitted_proofs_checked": sum(len(item["submitted_proofs"]) for item in results),
            "official_reference_commit": config["reference_commit"],
            "fixture_manifest_sha256": sha256(fixture_path.read_bytes()),
            "binaries": {"native_sha256": sha256(native.read_bytes()), "official_sha256": sha256(reference.read_bytes())},
            "external_pool_connection": False, "live_pool_acceptance": False,
            "model_api_calls": False,
            "cpu_execution": any(item.get("native_report", {}).get("cpu_execution") is True for item in results),
            "gpu_execution": any(item.get("native_report", {}).get("gpu_execution") is True for item in results),
            "windows_execution": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", type=Path, default=ROOT / "build/proof-cmake/neuro_pool_replay_client")
    parser.add_argument("--reference", type=Path, default=ROOT /
                        "build/proof-target/x86_64-unknown-linux-gnu/release/pearl-proof-check")
    parser.add_argument("--work-dir", type=Path, default=ROOT / "artifacts/pool-replay")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/pool-replay.json")
    parser.add_argument("--scenario", action="append", choices=[item.name for item in SCENARIOS])
    parser.add_argument("--deadline-ms", type=int, default=8000)
    parser.add_argument("--backend", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--device", type=int, help="CUDA device index, 0..15; default 0 when CUDA is explicitly selected")
    args = parser.parse_args()
    try:
        require(1000 <= args.deadline_ms <= 30000, "Native deadline must be between 1000 and 30000 ms")
        report = replay(args)
    except (OSError, ValueError, KeyError, StopIteration, ReplayError) as error:
        report = {"schema": "neuro.pool_replay.v1", "passed": False, "error": str(error)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: report.get(key) for key in
                      ("schema", "passed", "scenarios_run", "submitted_proofs_checked", "error")}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
