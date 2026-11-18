"""Refuse misleading recovery evidence; real subprocess tests use the CLI gate."""
import copy
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from replay_recovery import (OUTCOMES, Phase, RecoveryPhase, check_outcomes,
                             independent_case)
from replay_pool import Job, ReplayError, read_peer

ROOT = Path(__file__).resolve().parents[1]


def journal_report(**counts):
    outcomes = dict.fromkeys(OUTCOMES, 0)
    outcomes.update(counts)
    return {"journal": {"enabled": True, "outcomes": outcomes,
                         "attempts_consumed": sum(outcomes.values())}}


class PeerSocketTests(unittest.TestCase):
    def test_aborted_peer_is_the_end_of_the_connection(self):
        class Reset:
            def recv(self, _size):
                raise ConnectionResetError(10054, "reset")

        class Abort:
            def recv(self, _size):
                raise ConnectionAbortedError(10053, "abort")

        class Open:
            def recv(self, _size):
                return b"ok"

        self.assertEqual(read_peer(Reset()), b"")
        self.assertEqual(read_peer(Abort()), b"")
        self.assertEqual(read_peer(Open()), b"ok")


class DurableOutcomeEvidenceTests(unittest.TestCase):
    def test_uncertain_send_cannot_be_relabelled_accepted_or_forgotten(self):
        expected = {"uncertain": 1, "accepted": 1}
        check_outcomes(journal_report(**expected), expected)
        for counts in ({"accepted": 2}, {"accepted": 1}, {"interrupted": 1, "accepted": 1}):
            with self.subTest(counts=counts), self.assertRaises(ReplayError):
                check_outcomes(journal_report(**counts), expected)

    def test_unknown_states_boolean_counts_and_inconsistent_totals_are_refused(self):
        original = journal_report(uncertain=1)
        variants = []
        changed = copy.deepcopy(original)
        changed["journal"]["outcomes"]["pending"] = 0
        variants.append(changed)
        changed = copy.deepcopy(original)
        changed["journal"]["outcomes"]["uncertain"] = True
        variants.append(changed)
        changed = copy.deepcopy(original)
        del changed["journal"]["outcomes"]["uncertain"]
        variants.append(changed)
        changed = copy.deepcopy(original)
        changed["journal"]["attempts_consumed"] = 0
        variants.append(changed)
        for report in variants:
            with self.subTest(report=report), self.assertRaises(ReplayError):
                check_outcomes(report, {"uncertain": 1})


class IndependentRecoveryPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = json.loads((ROOT / "native/fixtures/attempt_manifest.json").read_text())
        cls.job = Job("same-id", cls.fixtures["cases"][0]["header_hex"])

    def test_plan_is_keyed_by_actual_header_and_attempt_not_job_label(self):
        first, _ = independent_case(self.fixtures, self.job, 0)
        second, _ = independent_case(self.fixtures, self.job, 1)
        self.assertNotEqual(first["digests"]["commitment_a"], second["digests"]["commitment_a"])
        with self.assertRaises(ReplayError):
            independent_case(self.fixtures, Job("same-id", "00" * 76), 0)
        with self.assertRaises(ReplayError):
            independent_case(self.fixtures, self.job, 62)  # Not a committed independent case.

    def test_other_target_cannot_borrow_a_known_easy_target_winner(self):
        with self.assertRaises(ReplayError):
            independent_case(self.fixtures, Job("same-id", self.job.header_hex,
                                              target_hex="00" * 32), 0)
        delayed = self.fixtures["miss_then_found"]
        job = Job("same-id", self.job.header_hex, target_hex=delayed["target_be_hex"])
        case, tile = independent_case(self.fixtures, job, 7)
        self.assertEqual(case["index"], 7)
        self.assertEqual((tile["row"], tile["col"]), (112, 0))
        with self.assertRaises(ReplayError):
            independent_case(self.fixtures, job, 0)


class RecoveryReportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        fixtures = json.loads((ROOT / "native/fixtures/attempt_manifest.json").read_text())
        job = Job("same-id", fixtures["cases"][0]["header_hex"])
        phase = Phase("resume", job, (1,), (), {"uncertain": 1, "miss": 1},
                      limit=2, stop_on_limit=True)
        self.runner = RecoveryPhase(phase, journal=Path(self.temporary.name) / "journal",
                                    native=Path("unused"), reference=Path("unused"), commit="unused",
                                    fixture={"header_hex": job.header_hex, "seed_hex": fixtures["root_seed_hex"]},
                                    work=Path(self.temporary.name) / "phase", deadline_ms=1000,
                                    attempt_fixtures=fixtures)
        connection = self.runner.ledger.begin(1)
        connection.authorized = True
        self.runner.ledger.issue(connection, job)
        self.runner.events = [{"event": "authorized"},
                              {"event": "journal_bound", "work_id": 1, "next_attempt": 1},
                              {"event": "attempt_started", "index": 1}]
        self.report = journal_report(uncertain=1, miss=1)
        self.report.update(schema="neuro.pool_client.v1", completed=True, reason="attempt_limit",
                           root_seed_hex=fixtures["root_seed_hex"],
                           attempt_seed_policy="neuro/zero-b/row-seed/v1", attempt_limit_per_generation=2,
                           backend="cpu", cpu_execution=True, gpu_execution=False,
                           counters={"submissions": 0, "accepted": 0, "rejected": 0, "pending_lost": 0, "errors": 0})
        self.report["journal"].update(recovered=True, work_count=1,
                                       works=[{"work_id": 1, "next_attempt": 2, "limit": 2}])

    def tearDown(self):
        self.runner.selector.close()
        self.temporary.cleanup()

    def test_valid_bounded_recovery_requires_fresh_authorization(self):
        self.runner.validate_phase(self.report, 0)
        self.runner.events = [event for event in self.runner.events if event["event"] != "authorized"]
        with self.assertRaisesRegex(ReplayError, "freshly authorize"):
            self.runner.validate_phase(self.report, 0)

    def test_skipped_or_repeated_attempt_labels_and_rolled_back_next_index_fail(self):
        for indices in ([0], [1, 1], [2]):
            with self.subTest(indices=indices):
                self.runner.events = [event for event in self.runner.events if event["event"] != "attempt_started"]
                self.runner.events += [{"event": "attempt_started", "index": index} for index in indices]
                with self.assertRaises(ReplayError):
                    self.runner.validate_phase(self.report, 0)
        self.runner.events = [event for event in self.runner.events if event["event"] != "attempt_started"]
        self.runner.events.append({"event": "attempt_started", "index": 1})
        self.report["journal"]["works"][0]["next_attempt"] = 1
        with self.assertRaisesRegex(ReplayError, "Durable attempt position"):
            self.runner.validate_phase(self.report, 0)

    def test_final_report_cannot_erase_prior_uncertainty_or_change_seed(self):
        for change in ({"root_seed_hex": "00" * 32}, {"attempt_seed_policy": "other"}):
            with self.subTest(change=change), self.assertRaises(ReplayError):
                self.runner.validate_phase({**self.report, **change}, 0)
        self.report["journal"]["outcomes"]["uncertain"] = 0
        self.report["journal"]["attempts_consumed"] = 1
        with self.assertRaises(ReplayError):
            self.runner.validate_phase(self.report, 0)

    def test_exit_86_at_another_stage_or_attempt_does_not_satisfy_crash_gate(self):
        self.runner.phase = replace(self.runner.phase, crash_point="after_proof", crash_index=1)
        crash = {"event": "crash_injected", "point": "after_proof", "index": 1,
                 "job_id": self.runner.phase.job.job_id}
        self.runner.events.append(crash)
        self.runner.validate_phase(None, 86)
        for changed in ({"point": "before_compute"}, {"index": 0}, {"job_id": "different-work"}):
            with self.subTest(changed=changed):
                self.runner.events[-1] = {**crash, **changed}
                with self.assertRaisesRegex(ReplayError, "requested abrupt-exit"):
                    self.runner.validate_phase(None, 86)


if __name__ == "__main__":
    unittest.main()
