import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def invoke(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "tools/neuroctl.py"), *args],
                              cwd=ROOT, capture_output=True, text=True, timeout=15)

    def test_cost_keeps_unknown_cache_and_exact_price(self):
        result = self.invoke("cost", "--usage", "examples/usage-openai-uncached.json",
                             "--model", "openai/gpt-6.1-sol")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["total_cost_usd"], "0.02079")
        self.assertEqual(data["cache_status"], "UNKNOWN")
        self.assertFalse(data["billing_verified"])

    def test_incomplete_usage_cannot_satisfy_a_budget_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.json"
            path.write_text(json.dumps({"input_tokens": 1000, "output_tokens": 100}), encoding="utf-8")
            result = self.invoke("cost", "--usage", str(path), "--format", "anthropic",
                                 "--model", "anthropic/claude-sonnet-5")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(json.loads(result.stdout)["accounting_complete"])

    def test_missing_usage_file_is_an_error(self):
        result = self.invoke("cost", "--usage", "no-such-usage.json", "--model", "openai/gpt-6.1-sol")
        self.assertEqual(result.returncode, 2)
        self.assertIn("error", json.loads(result.stderr))

    def test_dispatched_budget_with_unreadable_receipt_blocks_until_reconciled(self):
        with tempfile.TemporaryDirectory() as directory:
            common = ("budget", "--db", str(Path(directory) / "budget.sqlite3"))
            reserve = self.invoke(*common, "reserve", "--campaign", "cli-campaign",
                                  "--session", "cli-session", "--request", "one")
            self.assertEqual(reserve.returncode, 0, reserve.stderr)
            self.assertEqual(self.invoke(*common, "begin", "--request", "one").returncode, 0)
            failed = self.invoke(*common, "reconcile", "--request", "one", "--usage", str(Path(directory) / "missing.json"))
            self.assertEqual(failed.returncode, 1, failed.stderr)
            self.assertEqual(json.loads(failed.stdout)["state"], "UNRESOLVED")
            self.assertTrue(json.loads(failed.stdout)["receipt_read_error"])
            blocked = self.invoke(*common, "reserve", "--campaign", "cli-campaign",
                                  "--session", "cli-session", "--request", "two")
            self.assertEqual(blocked.returncode, 2)
            settled = self.invoke(*common, "reconcile", "--request", "one", "--usage", "examples/usage-openai-uncached.json")
            self.assertEqual(settled.returncode, 0, settled.stderr)
            status = self.invoke(*common, "status", "--campaign", "cli-campaign")
            self.assertFalse(json.loads(status.stdout)["campaign"]["blocked"])

    def test_synthetic_example_never_becomes_evidence(self):
        result = self.invoke("compare", "--candidate", "examples/experiment-result.json",
                             "--baseline", "examples/experiment-result.json")
        self.assertEqual(result.returncode, 1)
        self.assertFalse(json.loads(result.stdout)["passed"])

    def test_actual_command_failure_propagates(self):
        result = self.invoke("run", "--", sys.executable, "-c", "raise SystemExit(7)")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["returncode"], 7)

    def test_empty_command_is_an_error(self):
        result = self.invoke("run")
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
