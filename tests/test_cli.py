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

    def test_duplicate_keys_and_deep_nesting_become_unresolved_receipts(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            common = ("budget", "--db", str(base / "budget.sqlite3"))
            self.assertEqual(self.invoke(*common, "reserve", "--campaign", "cli-campaign",
                                         "--session", "cli-session", "--request", "one").returncode, 0)
            self.assertEqual(self.invoke(*common, "begin", "--request", "one").returncode, 0)
            receipts = {
                "duplicate.json": '{"usage": {"prompt_tokens": 1000000, "completion_tokens": 10, '
                                  '"prompt_tokens": 100}}',
                "deep.json": "[" * 20000 + "0" + "]" * 20000,
            }
            for name, text in receipts.items():
                receipt = base / name
                receipt.write_text(text, encoding="utf-8")
                failed = self.invoke(*common, "reconcile", "--request", "one", "--usage", str(receipt))
                self.assertEqual(failed.returncode, 1, failed.stderr)
                self.assertNotIn("Traceback", failed.stderr)
                data = json.loads(failed.stdout)
                self.assertEqual(data["state"], "UNRESOLVED")
                if name == "duplicate.json":
                    self.assertTrue(data["receipt_read_error"])
                else:
                    # Some Python parsers accept this depth; reconciliation must
                    # still reject the non-usage document and retain the hold.
                    self.assertFalse(data["reconciled"])
                self.assertEqual(data["held_cost_usd"], "0.007408")
            blocked = self.invoke(*common, "reserve", "--campaign", "cli-campaign",
                                  "--session", "cli-session", "--request", "two")
            self.assertEqual(blocked.returncode, 2)
            settled = self.invoke(*common, "reconcile", "--request", "one",
                                  "--usage", "examples/usage-openai-uncached.json")
            self.assertEqual(settled.returncode, 0, settled.stderr)
            self.assertEqual(json.loads(settled.stdout)["state"], "SETTLED")

    def test_cost_command_rejects_duplicate_keys_without_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.json"
            path.write_text('{"prompt_tokens": 100, "completion_tokens": 1, "prompt_tokens": 200}',
                            encoding="utf-8")
            result = self.invoke("cost", "--usage", str(path), "--model", "openai/gpt-6.1-sol")
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("Duplicate JSON object key", result.stderr)

    def test_empty_command_is_an_error(self):
        result = self.invoke("run")
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
