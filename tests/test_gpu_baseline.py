import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from neuroctl.experiments import evaluate, run_command
import record_gpu_baseline as baseline


ROOT = Path(__file__).resolve().parents[1]
PROBE_SCHEMA = (
    '{"schema":"neuro.fullk_probe.v1","status":"PASS","exact":true,'
    '"gpu_execution_observed":true,"cases":1,"launches":2,'
    '"mining_evidence":false,"hashrate":null}'
)


def timing_line(elapsed):
    return json.dumps({
        "schema": "neuro.fullk_kernel_time.v1", "status": "PASS", "exact": True,
        "gpu_execution_observed": True, "case": baseline.CASE,
        "rows": baseline.ROWS, "cols": baseline.COLS, "k": baseline.K,
        "launches": baseline.LAUNCHES, "interval": baseline.INTERVAL,
        "elapsed_s": elapsed, "mining_evidence": False, "hashrate": None,
    })


def emit(stage, line):
    record = run_command([sys.executable, "-c", f"print({line!r})"], stage=stage, timeout_s=20)
    if record["returncode"] != 0 or not record["output_complete"]:
        raise AssertionError("fixture process failed")
    return record


class BaselineParserTests(unittest.TestCase):
    def test_interval_constant_is_the_probe_source_string(self):
        source = (ROOT / "native/cuda/fullk_probe.cu").read_text(encoding="utf-8")
        self.assertIn(f'"{baseline.INTERVAL}"', source)
        self.assertIn("time-launches must be an integer from 8 to 64", source)

    def test_power_line_and_process_count(self):
        name, driver, watts, token = baseline.parse_power_watts(
            "NVIDIA GeForce RTX 5080 Laptop GPU, 617.14, [N/A], 115.00\n")
        self.assertEqual(name, "NVIDIA GeForce RTX 5080 Laptop GPU")
        self.assertEqual(driver, "617.14")
        self.assertEqual(watts, 115.0)
        self.assertEqual(token, "[N/A]")
        with self.assertRaises(baseline.BaselineError):
            baseline.parse_power_watts("GPU, driver, [N/A], [N/A]\n")
        with self.assertRaises(baseline.BaselineError):
            baseline.parse_power_watts("one\ntwo\n")
        self.assertEqual(baseline.count_image('INFO: No tasks\n', "SRBMiner-MULTI.exe"), 0)
        self.assertEqual(baseline.count_image('"SRBMiner-MULTI.exe","123"\n', "SRBMiner-MULTI.exe"), 1)

    def test_rejects_a_mismatched_or_mining_sample(self):
        with self.assertRaises(baseline.BaselineError):
            baseline.parse_verification(PROBE_SCHEMA.replace('"cases":1', '"cases":6'))
        payload = json.loads(timing_line(0.01))
        payload["hashrate"] = 1
        with self.assertRaises(baseline.BaselineError):
            baseline.parse_timing(json.dumps(payload))
        with self.assertRaises(baseline.BaselineError):
            baseline.parse_timing(timing_line(0))

    def test_three_real_processes_assemble_a_comparable_record(self):
        verification = emit("verification", PROBE_SCHEMA)
        baseline.parse_verification(verification["stdout"])
        samples = []
        other = []
        for elapsed in (0.010, 0.0104, 0.0098):
            record = emit("measurement", timing_line(elapsed))
            samples.append((baseline.parse_timing(record["stdout"]), record))
        for elapsed in (0.0101, 0.0102, 0.0099):
            record = emit("measurement", timing_line(elapsed))
            other.append((baseline.parse_timing(record["stdout"]), record))
        other_verification = emit("verification", PROBE_SCHEMA)
        first = baseline.assemble_experiment(
            hardware="parser-fixture", environment="parser-fixture", power_limit_w=140,
            verification=verification, samples=samples)
        second = baseline.assemble_experiment(
            hardware="parser-fixture", environment="parser-fixture", power_limit_w=140,
            verification=other_verification, samples=other)
        verdict = evaluate(second, first)
        self.assertTrue(verdict["passed"], verdict["reasons"])
        self.assertEqual(verdict["performance_claim"],
                         "measured_comparison_only_no_profit_or_significance_claim")
        self.assertFalse(first["synthetic"])
        self.assertNotIn("commit", first["identity"]["environment"])

    def test_panel_contract_is_pinned_in_the_probe_and_recorder(self):
        source = (ROOT / "native/cuda/fullk_probe.cu").read_text(encoding="utf-8")
        self.assertIn("panel rows and cols must be multiples of 128 in [128, 1024]", source)
        self.assertIn("panel mode does not take a gold fixture", source)
        self.assertIn("0x12345678u", source)
        self.assertIn(baseline.PANEL_CASE, source)
        self.assertEqual(baseline.PANEL_ROWS, 1024)
        self.assertEqual(baseline.PANEL_COLS, 1024)
        self.assertEqual(baseline.PANEL_SEED, 0x12345678)

    def test_panel_samples_assemble_a_separate_experiment(self):
        line = json.dumps({
            "schema": "neuro.fullk_probe.v1", "status": "PASS", "exact": True,
            "gpu_execution_observed": True, "case": baseline.PANEL_CASE,
            "rows": baseline.PANEL_ROWS, "cols": baseline.PANEL_COLS,
            "seed": baseline.PANEL_SEED, "cases": 1, "launches": 2, "k": baseline.K,
            "mining_evidence": False, "hashrate": None,
        })
        verification = emit("verification", line)
        baseline.parse_panel_verification(verification["stdout"])
        with self.assertRaises(baseline.BaselineError):
            baseline.parse_panel_verification(PROBE_SCHEMA)
        samples = []
        for elapsed in (0.20, 0.21, 0.19):
            payload = json.loads(timing_line(elapsed))
            payload.update(case=baseline.PANEL_CASE, rows=baseline.PANEL_ROWS,
                           cols=baseline.PANEL_COLS, seed=baseline.PANEL_SEED)
            record = emit("measurement", json.dumps(payload))
            samples.append((baseline.parse_timing(
                record["stdout"], case=baseline.PANEL_CASE, rows=baseline.PANEL_ROWS,
                cols=baseline.PANEL_COLS, seed=baseline.PANEL_SEED), record))
        missing_seed = json.loads(timing_line(0.20))
        missing_seed.update(case=baseline.PANEL_CASE, rows=baseline.PANEL_ROWS,
                            cols=baseline.PANEL_COLS)
        with self.assertRaises(baseline.BaselineError):
            baseline.parse_timing(
                json.dumps(missing_seed), case=baseline.PANEL_CASE,
                rows=baseline.PANEL_ROWS, cols=baseline.PANEL_COLS, seed=baseline.PANEL_SEED)
        experiment = baseline.assemble_experiment(
            hardware="parser-fixture", environment="parser-fixture", power_limit_w=115,
            verification=verification, samples=samples,
            workload="fullk reference panel", verifier_id=baseline.PANEL_VERIFIER_ID)
        self.assertEqual(experiment["correctness"]["verifier_id"], baseline.PANEL_VERIFIER_ID)
        self.assertNotEqual(experiment["identity"]["workload"],
                            baseline.assemble_experiment(
                                hardware="parser-fixture", environment="parser-fixture",
                                power_limit_w=115, verification=verification,
                                samples=samples)["identity"]["workload"])


if __name__ == "__main__":
    unittest.main()
