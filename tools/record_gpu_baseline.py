#!/usr/bin/env python3
"""Record one full-K CUDA-event baseline on an already leased GPU.

The interval is a fixed batch of queued launches measured with CUDA events.
Host transfers and the compare stay outside that interval. The default workload
is the 32x48 gold fixture. `--panel` times the separate 1024x1024 reference
panel. This does not stop SRBMiner, open a pool, or report a hashrate. Run it
only after the existing GPU lease has stopped the owner miner.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from neuroctl.experiments import run_command, write_result  # noqa: E402

CASE = "coordinate_mix"
ROWS = 32
COLS = 48
K = 4096
LAUNCHES = 64
INTERVAL = "cuda_events_around_queued_launches_excluding_transfers"
VERIFIER_ID = "committed native/fixtures/fullk_golden.hpp coordinate_mix"
PROFILE = "existing power.limit and clocks; no overclock; no TDR change"
# Largest shape the current kernel and Pearl input contract both accept.
PANEL_CASE = "reference_coordinate_mix"
PANEL_ROWS = 1024
PANEL_COLS = 1024
PANEL_SEED = 0x12345678
PANEL_VERIFIER_ID = "native/reference/fullk_reference.cpp coordinate_mix seed 305419896"


class BaselineError(RuntimeError):
    pass


def parse_json_line(stdout: str) -> dict:
    lines = [line for line in stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise BaselineError("probe must emit one JSON line")
    try:
        payload = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise BaselineError("probe JSON is unreadable") from exc
    if not isinstance(payload, dict):
        raise BaselineError("probe JSON must be an object")
    return payload


def _reject_mining_claim(payload: dict) -> None:
    if payload.get("mining_evidence") is not False or payload.get("hashrate") is not None:
        raise BaselineError("probe claimed mining or a hashrate")


def _exact_probe(payload: dict) -> None:
    if (payload.get("schema") != "neuro.fullk_probe.v1" or payload.get("status") != "PASS"
            or payload.get("exact") is not True or payload.get("gpu_execution_observed") is not True
            or payload.get("cases") != 1 or payload.get("launches") != 2):
        raise BaselineError("verification was not one exact GPU fixture")
    _reject_mining_claim(payload)


def parse_verification(stdout: str) -> dict:
    payload = parse_json_line(stdout)
    _exact_probe(payload)
    return payload


def parse_panel_verification(stdout: str) -> dict:
    payload = parse_json_line(stdout)
    _exact_probe(payload)
    if (payload.get("case") != PANEL_CASE or payload.get("rows") != PANEL_ROWS
            or payload.get("cols") != PANEL_COLS or payload.get("seed") != PANEL_SEED
            or payload.get("k") != K):
        raise BaselineError("verification was not the reference panel")
    return payload


def parse_timing(stdout: str, *, case: str = CASE, rows: int = ROWS, cols: int = COLS,
                 launches: int = LAUNCHES, seed: int | None = None) -> float:
    payload = parse_json_line(stdout)
    elapsed = payload.get("elapsed_s")
    if (payload.get("schema") != "neuro.fullk_kernel_time.v1" or payload.get("status") != "PASS"
            or payload.get("exact") is not True or payload.get("gpu_execution_observed") is not True
            or payload.get("case") != case or payload.get("rows") != rows or payload.get("cols") != cols
            or payload.get("k") != K or payload.get("launches") != launches
            or payload.get("interval") != INTERVAL
            or (seed is not None and payload.get("seed") != seed)):
        raise BaselineError("timing sample does not match the full-K baseline contract")
    _reject_mining_claim(payload)
    if (isinstance(elapsed, bool) or not isinstance(elapsed, (int, float))
            or not math.isfinite(elapsed) or elapsed <= 0):
        raise BaselineError("timing sample has no positive finite elapsed_s")
    return float(elapsed)


def parse_power_watts(text: str) -> tuple[str, str, float, str]:
    """Return name, driver, enforced watts, and the raw power.limit token.

    Laptop nvidia-smi can report power.limit as [N/A] while still enforcing a
    positive cap. The experiment uses that enforced cap and keeps the raw token.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise BaselineError("nvidia-smi must report exactly one GPU line")
    fields = [field.strip() for field in lines[0].split(",")]
    if len(fields) != 4 or not fields[0] or not fields[1] or not fields[2]:
        raise BaselineError("nvidia-smi GPU identity line is incomplete")
    try:
        watts = float(fields[3])
    except ValueError as exc:
        raise BaselineError("nvidia-smi enforced.power.limit is not a number") from exc
    if not math.isfinite(watts) or watts <= 0:
        raise BaselineError("nvidia-smi enforced.power.limit must be positive")
    return fields[0], fields[1], watts, fields[2]


def count_image(tasklist_csv: str, image: str) -> int:
    return sum(1 for line in tasklist_csv.splitlines() if image in line)


def query_gpu(device: int) -> tuple[str, str, float]:
    try:
        completed = subprocess.run(
            ["nvidia-smi", f"--id={device}",
             "--query-gpu=name,driver_version,power.limit,enforced.power.limit",
             "--format=csv,noheader,nounits"],
            check=False, capture_output=True, text=True, timeout=20, shell=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BaselineError("nvidia-smi query failed") from exc
    if completed.returncode != 0:
        raise BaselineError("nvidia-smi query failed")
    return parse_power_watts(completed.stdout)


def owner_miner_running() -> bool:
    if os.name != "nt":
        return False
    try:
        completed = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq SRBMiner-MULTI.exe", "/FO", "CSV", "/NH"],
            check=False, capture_output=True, text=True, timeout=20, shell=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BaselineError("could not inspect the owner miner process") from exc
    if completed.returncode != 0:
        raise BaselineError("could not inspect the owner miner process")
    return count_image(completed.stdout, "SRBMiner-MULTI.exe") != 0


def assemble_experiment(*, hardware: str, environment: str, power_limit_w: float,
                        verification: dict, samples: list[tuple[float, dict]],
                        workload: str | None = None, verifier_id: str | None = None) -> dict:
    if (not isinstance(hardware, str) or not hardware.strip()
            or not isinstance(environment, str) or not environment.strip()):
        raise BaselineError("hardware and environment must be nonempty")
    if (isinstance(power_limit_w, bool) or not isinstance(power_limit_w, (int, float))
            or not math.isfinite(power_limit_w) or power_limit_w <= 0):
        raise BaselineError("power limit must be positive")
    if len(samples) < 3:
        raise BaselineError("at least three measured repetitions are required")
    if workload is None:
        workload = (f"fullk gold {CASE} {ROWS}x{COLS} k={K} keep_c "
                    f"launches_per_sample={LAUNCHES} interval={INTERVAL}")
    if verifier_id is None:
        verifier_id = VERIFIER_ID
    return {
        "schema": "neuro.experiment.v1",
        "kind": "gpu_kernel",
        "synthetic": False,
        "identity": {
            "hardware": hardware,
            "workload": workload,
            "profile": PROFILE,
            "environment": environment,
            "device_kind": "cuda",
            "power_limit_w": power_limit_w,
        },
        "correctness": {
            "exact": True,
            "verifier_id": verifier_id,
            "verification": verification,
        },
        "samples": [{"elapsed_s": elapsed, "measurement": record} for elapsed, record in samples],
    }


def _probe(native: Path, device: int, stage: str, extra: list[str], timeout_s: int) -> dict:
    argv = [str(native), "--device", str(device), *extra]
    return run_command(argv, cwd=ROOT, timeout_s=timeout_s, stage=stage)


def record(native: Path, device: int, samples: int, output: Path, *, panel: bool = False) -> dict:
    if not native.is_file():
        raise BaselineError("full-K probe executable is missing")
    if owner_miner_running():
        raise BaselineError("SRBMiner-MULTI.exe is running; acquire the GPU lease first")
    name, driver, watts, limit_token = query_gpu(device)
    environment = (f"nvidia-smi driver {driver}; power.limit {limit_token}; "
                   f"enforced.power.limit {watts:g} W; os {platform.system()} {platform.release()}")
    if panel:
        prefix = ["--panel-rows", str(PANEL_ROWS), "--panel-cols", str(PANEL_COLS)]
        workload = (f"fullk reference {PANEL_CASE} {PANEL_ROWS}x{PANEL_COLS} k={K} "
                    f"seed={PANEL_SEED} keep_c launches_per_sample={LAUNCHES} interval={INTERVAL}")
        verifier_id = PANEL_VERIFIER_ID
        timeout_s = 180
        schema = "neuro.gpu_panel_baseline.v1"
    else:
        prefix = ["--case", CASE]
        workload = None
        verifier_id = None
        timeout_s = 60
        schema = "neuro.gpu_baseline.v1"
    verification = _probe(native, device, "verification", prefix, timeout_s)
    if panel:
        parse_panel_verification(verification.get("stdout", ""))
    else:
        parse_verification(verification.get("stdout", ""))
    measured: list[tuple[float, dict]] = []
    for _ in range(samples):
        observation = _probe(
            native, device, "measurement", [*prefix, "--time-launches", str(LAUNCHES)], timeout_s)
        elapsed = parse_timing(
            observation.get("stdout", ""),
            **({} if not panel else {"case": PANEL_CASE, "rows": PANEL_ROWS,
                                     "cols": PANEL_COLS, "seed": PANEL_SEED}))
        measured.append((elapsed, observation))
    end_name, end_driver, end_watts, end_token = query_gpu(device)
    if (end_name, end_driver, end_watts, end_token) != (name, driver, watts, limit_token):
        raise BaselineError("GPU name, driver, or power.limit changed during the baseline")
    experiment = assemble_experiment(
        hardware=name, environment=environment, power_limit_w=watts,
        verification=verification, samples=measured, workload=workload, verifier_id=verifier_id)
    document = write_result(output, experiment)
    elapsed = [item[0] for item in measured]
    ordered = sorted(elapsed)
    median = ordered[len(ordered) // 2] if len(ordered) % 2 else (ordered[len(ordered) // 2 - 1] + ordered[len(ordered) // 2]) / 2
    spread = (max(elapsed) - min(elapsed)) / median
    print(json.dumps({
        "schema": schema, "passed": True, "samples": len(elapsed),
        "median_elapsed_s": median, "relative_spread": spread,
        "spread_exceeds_comparison_limit": spread > 0.15,
        "output": str(output),
    }, allow_nan=False))
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument("--samples", type=int, default=3)
    default_output = ROOT / "artifacts" / "gpu-baseline.json"
    parser.add_argument("--output", type=Path, default=default_output)
    parser.add_argument("--panel", action="store_true",
                        help="time the 1024x1024 reference panel instead of the 32x48 gold fixture")
    args = parser.parse_args(argv)
    if not 0 <= args.device <= 15:
        raise BaselineError("device must be from 0 to 15")
    if not 3 <= args.samples <= 7:
        raise BaselineError("samples must be from 3 to 7")
    output = args.output
    if args.panel and output == default_output:
        output = ROOT / "artifacts" / "gpu-panel-baseline.json"
    schema = "neuro.gpu_panel_baseline.v1" if args.panel else "neuro.gpu_baseline.v1"
    try:
        record(args.native, args.device, args.samples, output, panel=args.panel)
    except BaselineError as exc:
        print(json.dumps({"schema": schema, "passed": False, "error": str(exc)},
                         allow_nan=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
