import contextlib
import io
import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from neuroctl.experiments import evaluate, run_command
import record_gpu_baseline as baseline


ROOT = Path(__file__).resolve().parents[1]
PROBE_SCHEMA = (
    '{"schema":"neuro.fullk_probe.v1","status":"PASS","exact":true,'
    '"gpu_execution_observed":true,"cases":1,"launches":2,"k":4096,'
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


def panel_line(elapsed):
    payload = json.loads(timing_line(elapsed))
    payload.update(case=baseline.PANEL_CASE, rows=baseline.PANEL_ROWS,
                   cols=baseline.PANEL_COLS, seed=baseline.PANEL_SEED)
    return json.dumps(payload)


PANEL_PROBE = json.dumps({
    "schema": "neuro.fullk_probe.v1", "status": "PASS", "exact": True,
    "gpu_execution_observed": True, "case": baseline.PANEL_CASE,
    "rows": baseline.PANEL_ROWS, "cols": baseline.PANEL_COLS,
    "seed": baseline.PANEL_SEED, "cases": 1, "launches": 2, "k": baseline.K,
    "mining_evidence": False, "hashrate": None,
})


def probe_record(stage, line, code=0):
    """A real local process that prints the probe line and exits with `code`."""
    script = f"import sys;print({line!r});sys.exit({code})"
    return run_command([sys.executable, "-c", script], stage=stage, timeout_s=20)


class PublicationGateTests(unittest.TestCase):
    """The recorder may publish only after successful, complete observations."""

    def run_record(self, records, *, samples=3, panel=False, existing=None):
        with tempfile.TemporaryDirectory() as directory:
            native = Path(directory) / "fullk_probe.exe"
            native.write_bytes(b"stand-in; _probe is mocked")
            output = Path(directory) / "baseline.json"
            if existing is not None:
                output.write_bytes(existing)
            queue = list(records)
            document, failed = None, None
            printed = io.StringIO()
            with mock.patch.object(baseline, "_probe",
                                   side_effect=lambda *a, **k: queue.pop(0)), \
                    mock.patch.object(baseline, "query_gpu",
                                      return_value=("fixture GPU", "000.00", 115.0, "[N/A]")), \
                    mock.patch.object(baseline, "owner_miner_running", return_value=False), \
                    contextlib.redirect_stdout(printed):
                try:
                    document = baseline.record(native, 0, samples, output, panel=panel)
                except baseline.BaselineError as exc:
                    failed = exc
            content = output.read_bytes() if output.exists() else None
            return document, content, printed.getvalue(), failed

    def gold_records(self, elapsed=(0.0100, 0.0102, 0.0099), verification_code=0,
                     failure_index=None):
        records = [probe_record("verification", PROBE_SCHEMA, verification_code)]
        for index, value in enumerate(elapsed):
            code = 3 if index == failure_index else 0
            records.append(probe_record("measurement", timing_line(value), code))
        return records

    def panel_records(self, elapsed=(0.20, 0.21, 0.19), failure_index=None):
        records = [probe_record("verification", PANEL_PROBE)]
        for index, value in enumerate(elapsed):
            code = 2 if index == failure_index else 0
            records.append(probe_record("measurement", panel_line(value), code))
        return records

    def expect_failure(self, records, **options):
        document, content, printed, failed = self.run_record(records, **options)
        self.assertIsNone(document)
        self.assertIsInstance(failed, baseline.BaselineError)
        self.assertNotIn("passed", printed)
        return content

    def test_pass_stdout_from_a_failed_verification_process_is_not_published(self):
        content = self.expect_failure(self.gold_records(verification_code=5))
        self.assertIsNone(content)

    def test_pass_stdout_from_a_failed_measurement_process_is_not_published(self):
        content = self.expect_failure(self.gold_records(failure_index=1))
        self.assertIsNone(content)

    def test_panel_mode_failure_is_rejected_too(self):
        self.expect_failure(self.panel_records(failure_index=2), panel=True)

    def test_incomplete_process_evidence_is_rejected(self):
        for change in ({"returncode": True}, {"returncode": False},
                       {"timed_out": True}, {"output_complete": False},
                       {"launch_error": "OSError: boom"}, {"stdout_truncated": True},
                       {"stage": "measurement"}, {"run_id": "not-a-uuid"}):
            records = self.gold_records()
            broken = dict(records[0])
            broken.update(change)
            records[0] = broken
            with self.subTest(change=change):
                self.expect_failure(records)

    def test_existing_artifact_survives_a_failed_run(self):
        original = b'{"schema": "neuro.experiment.v1", "kept": true}\n'
        content = self.expect_failure(self.gold_records(verification_code=7),
                                      existing=original)
        self.assertEqual(content, original)

    def test_reused_observation_ids_are_not_independent(self):
        records = self.gold_records()
        reused = [records[0]]
        for _ in range(3):
            copy = dict(records[1])
            copy["run_id"] = str(uuid.UUID(records[1]["run_id"])).upper()
            reused.append(copy)
        with self.assertRaises(baseline.BaselineError):
            baseline.assemble_experiment(
                hardware="h", environment="e", power_limit_w=115,
                verification=reused[0], samples=[(0.01, item) for item in reused[1:]])
        shared = dict(records[1])
        shared["run_id"] = records[0]["run_id"]
        shared["stage"] = "measurement"
        document, _, _, failed = self.run_record(
            [records[0], shared, records[1], records[2]])
        self.assertIsNone(document)
        self.assertIsInstance(failed, baseline.BaselineError)

    def test_strict_json_boundary(self):
        duplicate = ('{"schema":"neuro.fullk_probe.v1","status":"PASS","exact":true,'
                     '"gpu_execution_observed":true,"cases":1,"launches":2,"k":4096,'
                     '"cases":1,"mining_evidence":false,"hashrate":null}')
        with self.assertRaises(baseline.BaselineError):
            baseline.parse_verification(duplicate)
        nan_line = timing_line(0.01).replace('"elapsed_s": 0.01',
                                             '"elapsed_s": NaN')
        with self.assertRaises(baseline.BaselineError):
            baseline.parse_timing(nan_line)
        huge = json.loads(timing_line(0.01))
        huge["elapsed_s"] = 10 ** 400
        with self.assertRaises(baseline.BaselineError):
            baseline.parse_timing(json.dumps(huge))
        payload = json.loads(timing_line(0.01))
        for change in ({"rows": True}, {"cols": 48.0}, {"launches": True},
                       {"elapsed_s": True}, {"elapsed_s": "0.01"},
                       {"elapsed_s": 0}, {"elapsed_s": -1}):
            broken = dict(payload)
            broken.update(change)
            with self.subTest(change=change):
                with self.assertRaises(baseline.BaselineError):
                    baseline.parse_timing(json.dumps(broken))
        with self.assertRaises(baseline.BaselineError):
            baseline.parse_timing(timing_line(0.01) + "\n" + timing_line(0.02))

    def test_mode_mismatch_is_rejected_in_both_directions(self):
        with self.assertRaises(baseline.BaselineError):
            baseline.parse_verification(PANEL_PROBE)
        with self.assertRaises(baseline.BaselineError):
            baseline.parse_panel_verification(PROBE_SCHEMA)
        gold_with_shape = json.loads(PROBE_SCHEMA)
        gold_with_shape["rows"] = baseline.PANEL_ROWS
        with self.assertRaises(baseline.BaselineError):
            baseline.parse_verification(json.dumps(gold_with_shape))

    def test_cli_failure_and_success_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            native = Path(directory) / "fullk_probe.exe"
            native.write_bytes(b"stand-in")
            output = Path(directory) / "baseline.json"
            out, err = io.StringIO(), io.StringIO()
            failure = baseline.BaselineError("verification process did not complete successfully")
            with mock.patch.object(baseline, "record", side_effect=failure), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = baseline.main(["--native", str(native), "--output", str(output)])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(err.getvalue())["passed"], False)
            self.assertIn("error", json.loads(err.getvalue()))
            self.assertFalse(output.exists())
            with mock.patch.object(baseline, "record", return_value={}), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                code = baseline.main(["--native", str(native), "--output", str(output)])
            self.assertEqual(code, 0)

    def test_successful_gold_record_stays_evaluator_compatible(self):
        first, content, printed, failed = self.run_record(self.gold_records())
        self.assertIsNone(failed, failed)
        self.assertIn('"passed": true', printed)
        self.assertEqual(first["schema"], "neuro.experiment.v1")
        self.assertIn("fullk gold", first["identity"]["workload"])
        self.assertEqual(first["samples"][0]["elapsed_s"], 0.0100)
        measurement = first["samples"][0]["measurement"]
        self.assertNotEqual(measurement["elapsed_s"], 0.0100)
        second, _, _, failed = self.run_record(
            self.gold_records(elapsed=(0.0101, 0.0099, 0.0102)))
        self.assertIsNone(failed, failed)
        verdict = evaluate(second, first)
        self.assertTrue(verdict["passed"], verdict["reasons"])
        self.assertEqual(json.loads(content)["correctness"]["exact"], True)

    def test_successful_panel_record_keeps_its_own_workload(self):
        document, _, printed, failed = self.run_record(self.panel_records(), panel=True)
        self.assertIsNone(failed, failed)
        self.assertIn('"passed": true', printed)
        self.assertEqual(document["correctness"]["verifier_id"], baseline.PANEL_VERIFIER_ID)
        self.assertIn("reference", document["identity"]["workload"])
        self.assertEqual(document["samples"][1]["elapsed_s"], 0.21)


if __name__ == "__main__":
    unittest.main()
