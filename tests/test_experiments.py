import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from neuroctl.experiments import evaluate, run_command, write_result


def observed(stage):
    """A test fixture, not a claimed real GPU observation."""
    return {"schema": "neuro.process.v1", "run_id": str(uuid.uuid4()),
            "source": "local-subprocess", "stage": stage, "argv": ["test-fixture"],
            "started_at": "2026-11-11T00:00:00+00:00",
            "returncode": 0, "timed_out": False, "launch_error": None,
            "output_complete": True, "elapsed_s": 1.0}


def experiment(times=(1.0, 1.01, 0.99), kind="gpu_kernel"):
    result = {"schema": "neuro.experiment.v1", "kind": kind, "synthetic": False,
              "identity": {"hardware": "fixture-device", "workload": "fixture-vector-set",
                           "profile": "stock-fixture", "environment": "fixture-toolchain",
                           "device_kind": "cpu" if kind == "cpu_smoke" else "cuda",
                           "power_limit_w": None if kind == "cpu_smoke" else 140},
              "correctness": {"exact": True, "verifier_id": "independent-fixture-oracle",
                              "verification": observed("verification")},
              "samples": [{"elapsed_s": t, "measurement": observed("measurement")} for t in times]}
    if kind == "pool":
        result["identity"].update(pool="fixture-pool", difficulty_policy="fixed-fixture")
        for sample in result["samples"]:
            sample.update(accepted_shares=10, invalid_shares=0, accepted_difficulty=100.0)
    return result


class RunnerTests(unittest.TestCase):
    def test_success_failure_and_literal_arguments(self):
        literal = "$(touch forbidden); echo unsafe"
        result = run_command([sys.executable, "-c", "import sys; print(sys.argv[1])", literal])
        self.assertEqual(result["returncode"], 0)
        self.assertIn(literal, result["stdout"])
        self.assertFalse(result["timed_out"])
        failure = run_command([sys.executable, "-c", "raise SystemExit(7)"])
        self.assertEqual(failure["returncode"], 7)
        self.assertNotEqual(result["run_id"], failure["run_id"])

    def test_bounded_streams_and_launch_error(self):
        result = run_command([sys.executable, "-c",
                              "import sys; print('a'*200000); print('b'*200000,file=sys.stderr)"],
                             max_log_bytes=128)
        for stream in ("stdout", "stderr"):
            self.assertLessEqual(len(result[stream]), 128)
            self.assertTrue(result[stream + "_truncated"])
        missing = run_command([str(Path(tempfile.gettempdir()) / str(uuid.uuid4()))])
        self.assertIsNone(missing["returncode"])
        self.assertIsNotNone(missing["launch_error"])

    def test_timeout(self):
        result = run_command([sys.executable, "-c", "import time; time.sleep(10)"], timeout_s=0.1)
        self.assertTrue(result["timed_out"])
        self.assertNotEqual(result["returncode"], 0)
        self.assertLess(result["elapsed_s"], 4)

    @unittest.skipIf(os.name == "nt", "POSIX group cleanup; Windows needs a physical platform test")
    def test_timeout_kills_child_process(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "child-survived"
            child = "import pathlib,time,sys; time.sleep(.8); pathlib.Path(sys.argv[1]).touch()"
            parent = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]); time.sleep(10)"
            result = run_command([sys.executable, "-c", parent, child, str(marker)], timeout_s=0.25)
            self.assertTrue(result["timed_out"])
            time.sleep(0.8)
            self.assertFalse(marker.exists())

    def test_atomic_json_and_invalid_input(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested/result.json"
            first = write_result(path, {"result": 1})
            second = write_result(path, {"result": 2})
            self.assertNotEqual(first["run_id"], second["run_id"])
            self.assertEqual(json.loads(path.read_text()), second)
            with self.assertRaises(ValueError):
                write_result(path, {"result": float("nan")})
            self.assertEqual(json.loads(path.read_text()), second)
        with self.assertRaises(ValueError):
            run_command("echo unsafe")
        with self.assertRaises(ValueError):
            run_command([sys.executable], timeout_s=0)


class EvaluationTests(unittest.TestCase):
    def test_pass_regression_and_stability(self):
        baseline = experiment()
        self.assertTrue(evaluate(experiment((.95, .96, .94)), baseline)["passed"])
        for times in ((1.1, 1.11, 1.09), (1.0, 1.0, 10.0)):
            self.assertFalse(evaluate(experiment(times), baseline)["passed"])

    def test_missing_correctness_failed_command_and_nonfinite(self):
        baseline = experiment()
        for alteration in ("exact", "verification", "command", "nonfinite", "reused", "synthetic", "kind"):
            with self.subTest(alteration=alteration):
                candidate = experiment()
                if alteration == "exact":
                    candidate["correctness"]["exact"] = "PASS"
                elif alteration == "verification":
                    candidate["correctness"]["verification"] = {"status": "PASS"}
                elif alteration == "command":
                    candidate["samples"][0]["measurement"]["timed_out"] = True
                elif alteration == "nonfinite":
                    candidate["samples"][0]["elapsed_s"] = float("nan")
                elif alteration == "reused":
                    candidate["samples"][1]["measurement"] = candidate["samples"][0]["measurement"]
                elif alteration == "synthetic":
                    candidate["synthetic"] = True
                else:
                    candidate["kind"] = ["gpu_kernel"]
                self.assertFalse(evaluate(candidate, baseline)["passed"])
        self.assertFalse(evaluate({}, baseline)["passed"])
        self.assertFalse(evaluate(experiment((1.0,)), baseline)["passed"])
        self.assertFalse(evaluate(baseline, baseline)["passed"])

    def test_comparability(self):
        baseline = experiment()
        for key in ("hardware", "workload", "profile", "environment", "power_limit_w"):
            candidate = experiment()
            candidate["identity"][key] = 175 if key == "power_limit_w" else "different"
            self.assertFalse(evaluate(candidate, baseline)["passed"], key)
        candidate = experiment()
        del candidate["identity"]["power_limit_w"]
        self.assertFalse(evaluate(candidate, baseline)["passed"])

    def test_pool_uses_accepted_work_and_rejects_invalid_shares(self):
        baseline = experiment(kind="pool")
        candidate = experiment((.9, .91, .89), kind="pool")
        for sample in candidate["samples"]:
            sample["raw_hashrate_ths"] = 0.001
        result = evaluate(candidate, baseline)
        self.assertTrue(result["passed"])
        self.assertEqual(result["metric"], "accepted_difficulty_per_second")
        candidate["samples"][0]["invalid_shares"] = 1
        self.assertFalse(evaluate(candidate, baseline)["passed"])
        del candidate["samples"][0]["accepted_difficulty"]
        candidate["samples"][0]["raw_hashrate_ths"] = 100000
        self.assertFalse(evaluate(candidate, baseline)["passed"])
        candidate = experiment((1e-308, 1e-308, 1e-308), kind="pool")
        for sample in candidate["samples"]:
            sample["accepted_difficulty"] = 1e308
        self.assertFalse(evaluate(candidate, baseline)["passed"])

    def test_cpu_smoke_cannot_be_gpu_or_mining_evidence(self):
        result = evaluate(experiment(kind="cpu_smoke"), experiment(kind="cpu_smoke"))
        self.assertTrue(result["passed"])
        self.assertEqual(result["performance_claim"], "cpu_smoke_only")
        candidate = experiment(kind="cpu_smoke")
        candidate["kind"] = "gpu_kernel"
        self.assertFalse(evaluate(candidate, experiment())["passed"])


if __name__ == "__main__":
    unittest.main()
