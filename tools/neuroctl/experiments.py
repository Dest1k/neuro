"""Bounded local experiments and conservative, evidence-based comparison.

This validates observed-result records; it cannot attest that imported JSON is
truthful. A successful command alone is never a correctness or mining proof.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
from pathlib import Path
import signal
import statistics
import subprocess
import tempfile
import threading
import time
import uuid


def _number(value, *, positive=False):
    try:
        return (type(value) in (int, float) and math.isfinite(value)
                and (value > 0 if positive else value >= 0))
    except OverflowError:
        return False


def _stop_tree(process):
    """Own process group on POSIX; best-effort task tree on Windows."""
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=5, check=False, shell=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if process.poll() is None:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def run_command(argv, *, cwd=None, timeout_s=60, max_log_bytes=65536,
                stage="measurement"):
    """Run argv without a shell; retain only the tail of each output stream.

    Commands must not daemonize or detach from their process group. On Windows,
    taskkill cannot guarantee cleanup after the leader exits or the OS hangs.
    """
    if (not isinstance(argv, (list, tuple)) or not argv
            or any(not isinstance(x, str) for x in argv) or not argv[0]):
        raise ValueError("argv must be a nonempty sequence of strings")
    if not _number(timeout_s, positive=True):
        raise ValueError("timeout_s must be finite and positive")
    if type(max_log_bytes) is not int or max_log_bytes < 1:
        raise ValueError("max_log_bytes must be a positive integer")
    result = {"schema": "neuro.process.v1", "run_id": str(uuid.uuid4()),
              "source": "local-subprocess", "stage": stage, "argv": list(argv),
              "cwd": str(Path(cwd or os.getcwd()).resolve()),
              "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
              "returncode": None, "timed_out": False, "launch_error": None}
    output, totals = {"stdout": bytearray(), "stderr": bytearray()}, {}

    def drain(name, stream):
        total = 0
        try:
            while chunk := stream.read(65536):
                total += len(chunk)
                output[name].extend(chunk)
                del output[name][:-max_log_bytes]
        finally:
            totals[name] = total
            stream.close()

    start = time.monotonic()
    flags = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
             if os.name == "nt" else {"start_new_session": True})
    try:
        process = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                   shell=False, **flags)
    except OSError as exc:
        result["launch_error"] = f"{type(exc).__name__}: {exc}"
        result["output_complete"] = False
    else:
        readers = [threading.Thread(target=drain, args=(name, stream), daemon=True)
                   for name, stream in (("stdout", process.stdout),
                                        ("stderr", process.stderr))]
        for reader in readers:
            reader.start()
        try:
            process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            result["timed_out"] = True
            _stop_tree(process)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        finally:
            # A local experiment owns its descendants, including after success.
            if os.name != "nt":
                _stop_tree(process)
        for reader in readers:
            reader.join(timeout=1)
        result["returncode"] = process.poll()
        result["output_complete"] = all(not reader.is_alive() for reader in readers)
    result["elapsed_s"] = time.monotonic() - start
    for name in ("stdout", "stderr"):
        result[name] = bytes(output[name]).decode("utf-8", errors="replace")
        result[name + "_truncated"] = totals.get(name, 0) > max_log_bytes
    return result


def write_result(path, result):
    """Atomically replace a JSON artifact; preserve an existing unique run ID."""
    document = dict(result)
    document.setdefault("run_id", str(uuid.uuid4()))
    uuid.UUID(document["run_id"])
    encoded = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="." + path.name + ".", delete=False) as f:
            temporary = f.name
            f.write(encoded)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
    return document


def _observed_ok(record, stage):
    if not isinstance(record, dict):
        return False
    try:
        uuid.UUID(record.get("run_id", ""))
        dt.datetime.fromisoformat(record.get("started_at", ""))
    except (ValueError, TypeError, AttributeError):
        return False
    return (record.get("schema") == "neuro.process.v1"
            and record.get("source") == "local-subprocess"
            and record.get("stage") == stage
            and type(record.get("returncode")) is int and record["returncode"] == 0
            and record.get("timed_out") is False
            and record.get("output_complete") is True
            and record.get("launch_error") is None
            and _number(record.get("elapsed_s"), positive=True)
            and isinstance(record.get("argv"), list) and bool(record["argv"])
            and all(isinstance(arg, str) for arg in record["argv"]) and bool(record["argv"][0]))


def evaluate(candidate, baseline, *, max_regression=0.02, max_relative_spread=0.15):
    """Gate evidence, then compare medians; never infer mining profit.

    Three or more repetitions per side are required. Pool samples use accepted
    difficulty / elapsed seconds, not a miner's displayed hashrate.
    """
    if not _number(max_regression) or max_regression >= 1:
        raise ValueError("max_regression must be in [0, 1)")
    if not _number(max_relative_spread):
        raise ValueError("max_relative_spread must be finite and nonnegative")
    result = {"schema": "neuro.comparison.v1", "run_id": str(uuid.uuid4()),
              "passed": False, "reasons": [], "performance_claim": "none"}
    reasons, values, identities = result["reasons"], {}, {}
    measurement_ids = set()
    kinds = {"cpu_smoke", "gpu_kernel", "pool"}
    required_identity = ("hardware", "workload", "profile", "environment", "device_kind")
    for label, record in (("candidate", candidate), ("baseline", baseline)):
        if not isinstance(record, dict) or record.get("schema") != "neuro.experiment.v1":
            reasons.append(f"{label}: missing or unsupported experiment schema")
            continue
        kind, identity = record.get("kind"), record.get("identity", {})
        if not isinstance(kind, str):
            kind = None
        if kind not in kinds or record.get("synthetic") is not False:
            reasons.append(f"{label}: kind must be explicit and synthetic must be false")
        if not isinstance(identity, dict):
            identity = {}
        if any(not isinstance(identity.get(k), str) or not identity[k].strip()
               for k in required_identity) or "power_limit_w" not in identity:
            reasons.append(f"{label}: incomplete comparison identity")
        power = identity.get("power_limit_w")
        if ((kind == "cpu_smoke" and identity.get("device_kind") != "cpu")
                or (kind in {"gpu_kernel", "pool"}
                    and (identity.get("device_kind") != "cuda" or not _number(power, positive=True)))
                or (power is not None and not _number(power, positive=True))):
            reasons.append(f"{label}: invalid device kind or power limit")
        identities[label] = {k: identity.get(k) for k in (*required_identity, "power_limit_w")}
        if kind == "pool":
            for key in ("pool", "difficulty_policy"):
                identities[label][key] = identity.get(key)
                if not isinstance(identity.get(key), str) or not identity[key].strip():
                    reasons.append(f"{label}: missing {key} identity")
        correctness = record.get("correctness", {})
        if not isinstance(correctness, dict):
            correctness = {}
        verifier = correctness.get("verification")
        if (correctness.get("exact") is not True
                or not isinstance(correctness.get("verifier_id"), str)
                or not correctness.get("verifier_id", "").strip()
                or not _observed_ok(verifier, "verification")):
            reasons.append(f"{label}: exact correctness lacks successful observed verification")
        samples, data = record.get("samples"), []
        if not isinstance(samples, list) or len(samples) < 3:
            reasons.append(f"{label}: at least three measured repetitions are required")
            continue
        for index, sample in enumerate(samples):
            if not isinstance(sample, dict):
                reasons.append(f"{label}: invalid sample {index}")
                continue
            observed, elapsed = sample.get("measurement"), sample.get("elapsed_s")
            if not _observed_ok(observed, "measurement") or not _number(elapsed, positive=True):
                reasons.append(f"{label}: sample {index} lacks a successful measurement")
                continue
            run_id = observed["run_id"]
            if run_id in measurement_ids or (isinstance(verifier, dict) and run_id == verifier.get("run_id")):
                reasons.append(f"{label}: duplicated or reused measurement run ID")
            measurement_ids.add(run_id)
            if kind == "pool":
                count, invalid, work = (sample.get(k) for k in
                                        ("accepted_shares", "invalid_shares", "accepted_difficulty"))
                if (type(count) is not int or count <= 0 or type(invalid) is not int
                        or invalid != 0 or not _number(work, positive=True)):
                    reasons.append(f"{label}: sample {index} lacks valid accepted pool work")
                    continue
                rate = work / elapsed
                if not _number(rate, positive=True):
                    reasons.append(f"{label}: sample {index} accepted rate is not finite")
                    continue
                data.append(rate)
            else:
                data.append(elapsed)
        if len(data) == len(samples):
            median = statistics.median(data)
            spread = (max(data) - min(data)) / median
            if spread > max_relative_spread:
                reasons.append(f"{label}: repetition spread {spread:.4g} exceeds limit")
            values[label] = median
    if isinstance(candidate, dict) and isinstance(baseline, dict):
        if candidate.get("kind") != baseline.get("kind"):
            reasons.append("experiment kinds differ")
    if identities.get("candidate") != identities.get("baseline"):
        reasons.append("hardware/workload/profile/environment/power/pool identities differ")
    if reasons or len(values) != 2:
        return result
    pool = candidate["kind"] == "pool"
    ratio = values["candidate"] / values["baseline"]
    if not _number(ratio, positive=True):
        reasons.append("comparison ratio is outside the finite numeric range")
        return result
    regression = (1 - ratio) if pool else (ratio - 1)
    result.update(kind=candidate["kind"], metric="accepted_difficulty_per_second" if pool else "elapsed_s",
                  medians=values, candidate_to_baseline=ratio, regression=regression)
    if regression > max_regression:
        reasons.append(f"regression {regression:.4g} exceeds limit {max_regression:.4g}")
    result["passed"] = not reasons
    result["performance_claim"] = ("cpu_smoke_only" if candidate["kind"] == "cpu_smoke"
                                   else "measured_comparison_only_no_profit_or_significance_claim")
    return result
