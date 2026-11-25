"""Local accounting risks: concurrent spending, crashes, missing usage, and retries.

All usage is synthetic. No SDK, model endpoint, credential, or DSH is involved.
"""

from contextlib import closing
from dataclasses import replace
from decimal import Decimal, localcontext
import multiprocessing
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from neuroctl.budget import BudgetDenied, BudgetError, BudgetLedger, BudgetPolicy  # noqa: E402
from neuroctl.cost import load_pricing  # noqa: E402


def prices():
    return {model: load_pricing(ROOT / "configs/model-prices.json", model)
            for model in ("minimax/minimax-m3", "openai/gpt-6.1-sol")}


def race_reserve(path, policy, start, output, index):
    try:
        ledger = BudgetLedger(path, policy, prices())
        if not start.wait(10):
            raise RuntimeError("test start timeout")
        ledger.reserve(campaign_id="same-campaign", session_id=f"session-{index}",
                       reservation_id=f"request-{index}")
        output.put("reserved")
    except BudgetDenied:
        output.put("denied")
    except Exception as exc:
        output.put(type(exc).__name__)


def race_begin(path, policy, start, output):
    try:
        ledger = BudgetLedger(path, policy, prices())
        if not start.wait(10):
            raise RuntimeError("test start timeout")
        result = ledger.begin_dispatch("same-request")
        output.put("permitted" if result["dispatch_permitted"] is True else "invalid")
    except BudgetError:
        output.put("denied")
    except Exception as exc:
        output.put(type(exc).__name__)


def exit_after_dispatch(path, policy):
    ledger = BudgetLedger(path, policy, prices())
    ledger.begin_dispatch("crash-request")
    # Simulate abrupt worker death: no finally, cleanup, or release callback.
    os._exit(0)


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "budget.sqlite3"
        self.policy = BudgetPolicy()
        self.ledger = BudgetLedger(self.path, self.policy, prices())

    def tearDown(self):
        self.directory.cleanup()

    def reserve(self, name="r1", **kwargs):
        fields = dict(campaign_id="campaign", session_id="session", reservation_id=name)
        fields.update(kwargs)
        return self.ledger.reserve(**fields)

    def dispatch(self, name="r1", **kwargs):
        self.reserve(name, **kwargs)
        return self.ledger.begin_dispatch(name)

    def status(self):
        return self.ledger.status(campaign_id="campaign", session_id="session")

    def make_ledger(self, **changes):
        policy = replace(self.policy, **changes)
        path = Path(self.directory.name) / "custom.sqlite3"
        return BudgetLedger(path, policy, prices()), policy, path

    def join_children(self, processes):
        deadline = time.monotonic() + 20
        try:
            for process in processes:
                process.join(max(0, deadline - time.monotonic()))
            self.assertTrue(all(not process.is_alive() for process in processes), "test workers did not terminate")
            self.assertEqual([process.exitcode for process in processes], [0] * len(processes))
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(2)
                if process.is_alive():
                    process.kill()
                    process.join(2)

    def test_project_selects_minimax_with_quoted_rates_and_disabled_fallback(self):
        ledger = BudgetLedger.from_project(self.path, ROOT / "configs/project.json", ROOT / "configs/model-prices.json")
        result = ledger.reserve(campaign_id="campaign", session_id="session", reservation_id="max-call")
        self.assertEqual(result["model_id"], "minimax/minimax-m3")
        self.assertEqual(result["reserved_cost_usd"], "0.007408")
        self.assertFalse(result["dispatch_permitted"])
        status = self.status()
        self.assertEqual(status["session"]["limit_usd"], "5")
        self.assertEqual(status["campaign"]["limit_usd"], "25")
        self.assertEqual(status["session"]["maximum_calls"], 200)
        self.assertFalse(status["fallback"]["enabled"])
        self.assertFalse(status["billing_verified"])
        self.assertFalse(status["dsh_adapter_connected"])

    def test_cache_hit_is_observed_but_does_not_release_a_speculative_discount(self):
        self.dispatch()
        marker = "DO_NOT_STORE_RESPONSE_TEXT_OR_SECRET_8b43f6"
        result = self.ledger.reconcile("r1", {"choices": [{"text": marker}], "usage": {
            "prompt_tokens": 64000, "completion_tokens": 2000,
            "prompt_tokens_details": {"cached_tokens": 57600},
        }}, "openai")
        self.assertEqual(result["settled_cost_usd"], "0.005548")
        self.assertEqual(result["quote_cost_usd"], "0.00199984")
        self.assertEqual(result["usage"]["cache_read_tokens"], 57600)
        self.assertEqual(result["held_cost_usd"], "0")
        self.assertFalse(result["overrun"])
        self.assertNotIn(marker.encode(), self.path.read_bytes())
        self.assertEqual(self.status()["session"]["committed_cost_usd"], "0.005548")

    def test_openai_unknown_cache_still_has_complete_full_input_and_can_settle(self):
        self.dispatch(max_input_tokens=1000, max_output_tokens=10)
        result = self.ledger.reconcile("r1", {"prompt_tokens": 1000, "completion_tokens": 10}, "openai")
        self.assertEqual(result["state"], "SETTLED")
        self.assertEqual(result["usage"]["cache_status"], "UNKNOWN")
        self.assertEqual(result["settled_cost_usd"], "0.0000801")

    def test_anthropic_exclusive_input_is_counted_with_read_and_write_once(self):
        self.dispatch(max_input_tokens=1000, max_output_tokens=10)
        result = self.ledger.reconcile("r1", {"input_tokens": 100, "output_tokens": 10,
            "cache_read_input_tokens": 800, "cache_creation_input_tokens": 100}, "anthropic")
        self.assertEqual(result["usage"]["input_tokens"], 1000)
        self.assertEqual(result["settled_cost_usd"], "0.0000801")
        self.assertEqual(result["quote_cost_usd"], "0.00003082")

    def test_missing_usage_holds_money_blocks_new_dispatch_and_can_be_reconciled_later(self):
        self.dispatch("lost")
        self.reserve("waiting")
        result = self.ledger.reconcile("lost", None)
        self.assertEqual(result["state"], "UNRESOLVED")
        self.assertFalse(result["reconciled"])
        self.assertIsNone(result["settled_cost_usd"])
        self.assertEqual(result["held_cost_usd"], "0.007408")
        with self.assertRaises(BudgetDenied):
            self.reserve("next")
        with self.assertRaises(BudgetDenied):
            self.ledger.begin_dispatch("waiting")
        with self.assertRaises(BudgetError):
            self.ledger.cancel_undispatched("lost")
        self.ledger.reconcile("lost", {"prompt_tokens": 1000, "completion_tokens": 10}, "openai")
        self.assertFalse(self.status()["campaign"]["blocked"])
        self.assertTrue(self.ledger.begin_dispatch("waiting")["dispatch_permitted"])

    def test_incomplete_anthropic_zero_is_not_a_zero_bill(self):
        self.dispatch()
        result = self.ledger.reconcile("r1", {"input_tokens": 0, "output_tokens": 0}, "anthropic")
        self.assertEqual(result["error_code"], "incomplete_final_usage")
        self.assertEqual(result["held_cost_usd"], "0.007408")
        self.assertIsNone(result["settled_cost_usd"])
        self.assertTrue(self.status()["campaign"]["blocked"])

    def test_invalid_counters_never_release_any_of_several_inflight_reservations(self):
        bad_values = [-1, True, 1.5, None, "100"]
        for index in range(len(bad_values)):
            self.dispatch(f"r{index}")
        for index, bad in enumerate(bad_values):
            result = self.ledger.reconcile(f"r{index}", {"prompt_tokens": bad, "completion_tokens": 0}, "openai")
            self.assertEqual(result["error_code"], "invalid_final_usage")
        self.assertEqual(self.status()["campaign"]["held_cost_usd"], "0.03704")
        self.assertEqual(self.status()["campaign"]["unresolved_calls"], 5)

    def test_unknown_cache_write_ttl_is_unresolved_not_a_guessed_one_times_price(self):
        self.dispatch()
        result = self.ledger.reconcile("r1", {
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 100,
            "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 100},
        }, "anthropic")
        self.assertEqual(result["error_code"], "unpriced_cache_write_class")
        self.assertEqual(result["held_cost_usd"], "0.007408")
        self.assertIsNone(result["settled_cost_usd"])

    def test_partial_stream_is_not_a_final_bill(self):
        self.dispatch()
        result = self.ledger.reconcile("r1", {"object": "chat.completion.chunk", "usage": {
            "prompt_tokens": 1000, "completion_tokens": 10}}, "openai")
        self.assertEqual(result["state"], "UNRESOLVED")
        self.assertEqual(result["held_cost_usd"], "0.007408")

    def test_identical_reserve_and_final_usage_are_idempotent_but_begin_is_single_use(self):
        first = self.reserve()
        self.assertEqual(self.reserve(), first)
        self.assertEqual(self.status()["session"]["reserved_calls"], 1)
        with self.assertRaises(BudgetError):
            self.reserve(max_input_tokens=1000)
        self.assertTrue(self.ledger.begin_dispatch("r1")["dispatch_permitted"])
        with self.assertRaises(BudgetError):
            self.ledger.begin_dispatch("r1")
        payload = {"prompt_tokens": 1000, "completion_tokens": 10}
        settled = self.ledger.reconcile("r1", payload, "openai")
        self.assertEqual(self.ledger.reconcile("r1", {"usage": payload}, "openai"), settled)
        with self.assertRaises(BudgetError):
            self.ledger.reconcile("r1", {"prompt_tokens": 1000, "completion_tokens": 11}, "openai")
        self.assertEqual(self.status()["session"]["settled_cost_usd"], "0.0000801")
        self.assertFalse(self.reserve()["dispatch_permitted"])

    def test_cancel_only_releases_an_undispatched_call_and_never_reuses_its_id(self):
        self.reserve()
        self.assertEqual(self.ledger.cancel_undispatched("r1")["state"], "CANCELLED")
        self.assertEqual(self.ledger.cancel_undispatched("r1")["held_cost_usd"], "0")
        self.assertEqual(self.status()["session"]["reserved_calls"], 0)
        with self.assertRaises(BudgetError):
            self.ledger.begin_dispatch("r1")
        with self.assertRaises(BudgetError):
            self.ledger.reconcile("r1", {"prompt_tokens": 0, "completion_tokens": 0}, "openai")
        self.dispatch("new-paid-attempt")
        with self.assertRaises(BudgetError):
            self.ledger.cancel_undispatched("new-paid-attempt")

    def test_dispatched_call_survives_abrupt_worker_exit_and_reopening(self):
        self.reserve("crash-request")
        process = multiprocessing.get_context("spawn").Process(target=exit_after_dispatch, args=(str(self.path), self.policy))
        process.start()
        self.join_children([process])
        restarted = BudgetLedger(self.path, self.policy, prices())
        self.assertEqual(restarted.status(campaign_id="campaign")["campaign"]["held_cost_usd"], "0.007408")
        with self.assertRaises(BudgetError):
            restarted.begin_dispatch("crash-request")
        with self.assertRaises(BudgetError):
            restarted.cancel_undispatched("crash-request")
        self.assertEqual(restarted.reconcile("crash-request", None)["state"], "UNRESOLVED")

    def test_concurrent_processes_cannot_oversubscribe_shared_campaign(self):
        ledger, policy, path = self.make_ledger(work_session_budget_usd="0.014816", initial_campaign_budget_usd="0.014816",
                                              fallback_work_session_budget_usd="0.001")
        context = multiprocessing.get_context("spawn")
        start, output = context.Event(), context.Queue()
        processes = [context.Process(target=race_reserve, args=(str(path), policy, start, output, index)) for index in range(6)]
        for process in processes:
            process.start()
        start.set()
        self.join_children(processes)
        results = [output.get(timeout=2) for _ in processes]
        output.close()
        output.join_thread()
        self.assertEqual(sorted(results), ["denied"] * 4 + ["reserved"] * 2)
        total = ledger.status(campaign_id="same-campaign")["campaign"]
        self.assertEqual(total["committed_cost_usd"], "0.014816")
        self.assertEqual(total["reserved_calls"], 2)

    def test_concurrent_begin_grants_exactly_one_local_dispatch_permission(self):
        self.reserve("same-request")
        context = multiprocessing.get_context("spawn")
        start, output = context.Event(), context.Queue()
        processes = [context.Process(target=race_begin, args=(str(self.path), self.policy, start, output)) for _ in range(4)]
        for process in processes:
            process.start()
        start.set()
        self.join_children(processes)
        results = [output.get(timeout=2) for _ in processes]
        output.close()
        output.join_thread()
        self.assertEqual(sorted(results), ["denied"] * 3 + ["permitted"])
        self.assertEqual(self.status()["session"]["dispatched_calls"], 1)

    def test_session_limit_and_campaign_limit_apply_independently_without_partial_insert(self):
        ledger, _, _ = self.make_ledger(work_session_budget_usd="0.01", initial_campaign_budget_usd="0.02",
                                        fallback_work_session_budget_usd="0.001")
        ledger.reserve(campaign_id="c", session_id="a", reservation_id="one")
        with self.assertRaises(BudgetDenied):
            ledger.reserve(campaign_id="c", session_id="a", reservation_id="session-denied")
        ledger.reserve(campaign_id="c", session_id="b", reservation_id="two")
        with self.assertRaises(BudgetDenied):
            ledger.reserve(campaign_id="c", session_id="fresh-session", reservation_id="campaign-denied")
        self.assertEqual(ledger.status(campaign_id="c")["campaign"]["reserved_calls"], 2)
        self.assertEqual(ledger.status(campaign_id="c", session_id="fresh-session")["session"]["reserved_calls"], 0)

    def test_fallback_requires_explicit_enablement_and_shares_existing_limits(self):
        self.reserve("minimax")
        with self.assertRaises(BudgetDenied):
            self.reserve("sol", model_id="openai/gpt-6.1-sol")
        enabled = BudgetLedger(self.path, replace(self.policy, fallback_enabled=True), prices())
        for index in range(3):
            enabled.reserve(campaign_id="campaign", session_id="session", reservation_id=f"sol-{index}",
                            model_id="openai/gpt-6.1-sol")
        with self.assertRaises(BudgetDenied):
            enabled.reserve(campaign_id="campaign", session_id="session", reservation_id="sol-four",
                            model_id="openai/gpt-6.1-sol")
        status = enabled.status(campaign_id="campaign", session_id="session")
        self.assertEqual(status["fallback"]["reserved_calls"], 3)
        self.assertEqual(status["fallback"]["committed_cost_usd"], "0.087624")
        self.assertEqual(status["session"]["committed_cost_usd"], "0.095032")
        with self.assertRaises(BudgetDenied):
            self.ledger.begin_dispatch("sol-0")

    def test_fallback_dollar_limit_is_not_only_a_call_counter(self):
        ledger, _, _ = self.make_ledger(fallback_enabled=True, fallback_work_session_budget_usd="0.04")
        ledger.reserve(campaign_id="c", session_id="s", reservation_id="one", model_id="openai/gpt-6.1-sol")
        with self.assertRaises(BudgetDenied):
            ledger.reserve(campaign_id="c", session_id="s", reservation_id="two", model_id="openai/gpt-6.1-sol")
        self.assertEqual(ledger.status(campaign_id="c", session_id="s")["fallback"]["reserved_calls"], 1)

    def test_real_overrun_is_recorded_above_caps_and_stops_future_and_reserved_dispatch(self):
        self.dispatch("bad-provider")
        self.reserve("waiting")
        result = self.ledger.reconcile("bad-provider", {"prompt_tokens": 400_000_000, "completion_tokens": 0}, "openai")
        self.assertEqual(result["settled_cost_usd"], "30.8")
        self.assertTrue(result["overrun"])
        self.assertTrue(result["reconciled"])
        status = self.status()
        self.assertEqual(status["campaign"]["remaining_usd"], "-5.807408")
        self.assertIn("budget_exceeded", status["campaign"]["blocking_reasons"])
        with self.assertRaises(BudgetDenied):
            self.reserve("next")
        with self.assertRaises(BudgetDenied):
            self.ledger.begin_dispatch("waiting")
        self.assertEqual(self.status()["campaign"]["settled_cost_usd"], "30.8")

    def test_token_bound_violation_stops_even_when_smaller_input_keeps_cost_below_hold(self):
        self.dispatch(max_input_tokens=64000, max_output_tokens=1)
        result = self.ledger.reconcile("r1", {"prompt_tokens": 0, "completion_tokens": 2}, "openai")
        self.assertLess(Decimal(result["settled_cost_usd"]), Decimal(result["reserved_cost_usd"]))
        self.assertTrue(result["overrun"])
        with self.assertRaises(BudgetDenied):
            self.reserve("next")

    def test_incomplete_usage_can_raise_hold_to_observed_floor_but_cannot_release_it(self):
        self.dispatch()
        result = self.ledger.reconcile("r1", {"input_tokens": 100_000, "output_tokens": 0}, "anthropic")
        self.assertEqual(result["held_cost_usd"], "0.0077")
        self.assertTrue(result["overrun"])
        self.assertIsNone(result["settled_cost_usd"])
        self.assertEqual(self.ledger.reconcile("r1", None)["held_cost_usd"], "0.0077")

    def test_decimal_totals_preserve_tiny_remainder_under_small_global_precision(self):
        cap = "0.014816000000000000000000000000000000001"
        ledger, _, _ = self.make_ledger(work_session_budget_usd=cap, initial_campaign_budget_usd=cap,
                                        fallback_work_session_budget_usd="0.001")
        with localcontext() as context:
            context.prec = 6
            ledger.reserve(campaign_id="c", session_id="s", reservation_id="one")
            ledger.reserve(campaign_id="c", session_id="s", reservation_id="two")
            status = ledger.status(campaign_id="c", session_id="s")
        self.assertEqual(status["campaign"]["committed_cost_usd"], "0.014816")
        self.assertEqual(Decimal(status["campaign"]["remaining_usd"]), Decimal("1e-39"))

    def test_limits_prices_and_session_identity_cannot_be_reset_by_reopening(self):
        self.reserve()
        with self.assertRaises(BudgetError):
            BudgetLedger(self.path, replace(self.policy, initial_campaign_budget_usd="30"), prices())
        changed = prices()
        changed[self.policy.executor] = replace(changed[self.policy.executor], input_per_million="0.001")
        with self.assertRaises(BudgetError):
            BudgetLedger(self.path, self.policy, changed)
        with self.assertRaises(BudgetError):
            self.reserve("other-campaign", campaign_id="new-campaign")
        self.assertEqual(self.status()["campaign"]["held_cost_usd"], "0.007408")

    def test_call_cap_includes_zero_token_completed_requests_and_each_paid_retry(self):
        ledger, _, _ = self.make_ledger(maximum_calls_per_work_session=2, fallback_maximum_calls_per_work_session=1)
        for name in ("attempt", "paid-retry"):
            ledger.reserve(campaign_id="c", session_id="s", reservation_id=name)
            ledger.begin_dispatch(name)
            ledger.reconcile(name, {"prompt_tokens": 0, "completion_tokens": 0}, "openai")
        self.assertEqual(ledger.status(campaign_id="c", session_id="s")["session"]["settled_cost_usd"], "0")
        with self.assertRaises(BudgetDenied):
            ledger.reserve(campaign_id="c", session_id="s", reservation_id="third")

    def test_invalid_request_or_policy_never_creates_a_dispatchable_reservation(self):
        for fields in ({"max_input_tokens": 64001}, {"max_output_tokens": 8001},
                       {"max_input_tokens": True}, {"max_output_tokens": -1},
                       {"max_input_tokens": 0, "max_output_tokens": 0},
                       {"model_id": "unpriced/route"}, {"reservation_id": "not an opaque id"}):
            with self.subTest(fields=fields), self.assertRaises(BudgetError):
                self.reserve(**fields)
        self.assertEqual(self.status()["campaign"]["reserved_calls"], 0)
        with self.assertRaises(BudgetError):
            BudgetPolicy(work_session_budget_usd=5.0)
        with self.assertRaises(BudgetError):
            BudgetLedger(":memory:", self.policy, prices())

    def test_existing_unrelated_database_is_not_repurposed(self):
        path = Path(self.directory.name) / "other.sqlite3"
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("CREATE TABLE unrelated (value TEXT)")
            connection.execute("INSERT INTO unrelated VALUES ('keep')")
        with self.assertRaises(BudgetError):
            BudgetLedger(path, self.policy, prices())
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute("SELECT value FROM unrelated").fetchone()[0], "keep")


    def test_wrapper_and_top_level_conflict_is_durable_unresolved(self):
        self.dispatch()
        result = self.ledger.reconcile("r1", {
            "usage": {"prompt_tokens": 100, "completion_tokens": 10},
            "prompt_tokens": 1_000_000, "completion_tokens": 10,
        }, "openai")
        self.assertEqual(result["state"], "UNRESOLVED")
        self.assertEqual(result["error_code"], "invalid_final_usage")
        self.assertEqual(result["held_cost_usd"], "0.007408")
        restarted = BudgetLedger(self.path, self.policy, prices())
        self.assertEqual(restarted.status(campaign_id="campaign")["campaign"]["blocking_reasons"],
                         ["unresolved_usage"])
        with self.assertRaises(BudgetDenied):
            restarted.reserve(campaign_id="campaign", session_id="session", reservation_id="blocked")

    def test_counter_too_large_for_the_representation_is_unresolved_not_a_crash(self):
        self.dispatch()
        result = self.ledger.reconcile("r1", {"prompt_tokens": 10 ** 5000, "completion_tokens": 0}, "openai")
        self.assertEqual(result["state"], "UNRESOLVED")
        self.assertEqual(result["error_code"], "invalid_final_usage")
        self.assertTrue(self.status()["campaign"]["blocked"])

    def test_observed_counters_are_floors_and_contradiction_is_not_settled(self):
        self.dispatch()
        first = self.ledger.reconcile("r1", {"input_tokens": 200_000, "output_tokens": 50}, "anthropic")
        self.assertEqual(first["state"], "UNRESOLVED")
        self.assertEqual(first["observed_input_tokens"], 200_000)
        self.assertEqual(first["observed_output_tokens"], 50)
        contradictory = self.ledger.reconcile("r1", {
            "input_tokens": 100, "output_tokens": 50,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        }, "anthropic")
        self.assertEqual(contradictory["state"], "UNRESOLVED")
        self.assertEqual(contradictory["error_code"], "contradicts_observed_floor")
        self.assertGreaterEqual(Decimal(contradictory["held_cost_usd"]),
                                Decimal(first["held_cost_usd"]))
        confirmed = self.ledger.reconcile("r1", {
            "input_tokens": 200_000, "output_tokens": 50,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        }, "anthropic")
        self.assertEqual(confirmed["state"], "SETTLED")
        self.assertEqual(confirmed["settled_cost_usd"], "0.0154155")
        self.assertEqual(confirmed["observed_input_tokens"], 200_000)
        self.assertEqual(confirmed["observed_output_tokens"], 50)
        replay = self.ledger.reconcile("r1", {
            "input_tokens": 200_000, "output_tokens": 50,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        }, "anthropic")
        self.assertEqual(replay["settled_cost_usd"], "0.0154155")
        self.assertEqual(replay["observed_input_tokens"], 200_000)

    def test_older_journal_without_floor_columns_is_upgraded_in_place(self):
        if sqlite3.sqlite_version_info < (3, 35, 0):
            self.skipTest("DROP COLUMN requires SQLite 3.35 or newer")
        self.dispatch()
        self.ledger.reconcile("r1", {"prompt_tokens": 1000, "completion_tokens": 10}, "openai")
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("ALTER TABLE budget_reservations DROP COLUMN observed_input_tokens")
            connection.execute("ALTER TABLE budget_reservations DROP COLUMN observed_output_tokens")
        reopened = BudgetLedger(self.path, self.policy, prices())
        self.assertEqual(reopened.status(campaign_id="campaign")["campaign"]["settled_cost_usd"],
                         "0.0000801")
        replay = reopened.reconcile("r1", {"prompt_tokens": 1000, "completion_tokens": 10}, "openai")
        self.assertEqual(replay["state"], "SETTLED")
        self.assertEqual(replay["observed_input_tokens"], 1000)
        self.assertEqual(replay["observed_output_tokens"], 10)

    def test_legacy_unresolved_usage_restores_floors_without_losing_history(self):
        if sqlite3.sqlite_version_info < (3, 35, 0):
            self.skipTest("DROP COLUMN requires SQLite 3.35 or newer")
        self.dispatch("old-settled")
        self.ledger.reconcile("old-settled", {
            "prompt_tokens": 1000, "completion_tokens": 10,
        }, "openai")
        self.dispatch()
        first = self.ledger.reconcile("r1", {
            "input_tokens": 60_000, "output_tokens": 100,
        }, "anthropic")
        self.assertEqual(first["state"], "UNRESOLVED")
        configuration = self.ledger.configuration_sha256
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            old_settled = dict(connection.execute("""SELECT state,settled_cost_usd,held_cost_usd,usage_json
                FROM budget_reservations WHERE reservation_id='old-settled'""").fetchone())
            connection.execute("ALTER TABLE budget_reservations DROP COLUMN observed_input_tokens")
            connection.execute("ALTER TABLE budget_reservations DROP COLUMN observed_output_tokens")
        reopened = BudgetLedger(self.path, self.policy, prices())
        self.assertEqual(reopened.configuration_sha256, configuration)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory = sqlite3.Row
            restored = dict(connection.execute("""SELECT state,held_cost_usd,observed_input_tokens,
                observed_output_tokens FROM budget_reservations WHERE reservation_id='r1'""").fetchone())
            preserved = dict(connection.execute("""SELECT state,settled_cost_usd,held_cost_usd,usage_json
                FROM budget_reservations WHERE reservation_id='old-settled'""").fetchone())
        self.assertEqual(restored, {"state": "UNRESOLVED", "held_cost_usd": "0.007408",
                                    "observed_input_tokens": 60_000, "observed_output_tokens": 100})
        self.assertEqual(preserved, old_settled)
        smaller = reopened.reconcile("r1", {
            "input_tokens": 100, "output_tokens": 100,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        }, "anthropic")
        self.assertEqual(smaller["state"], "UNRESOLVED")
        self.assertEqual(smaller["error_code"], "contradicts_observed_floor")
        self.assertEqual(smaller["held_cost_usd"], "0.007408")
        reopened = BudgetLedger(self.path, self.policy, prices())
        settled = reopened.reconcile("r1", {
            "input_tokens": 60_000, "output_tokens": 100,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        }, "anthropic")
        self.assertEqual(settled["state"], "SETTLED")
        self.assertEqual(settled["settled_cost_usd"], "0.004651")
        self.assertEqual(settled["held_cost_usd"], "0")
        self.assertEqual(settled["observed_input_tokens"], 60_000)
        self.assertEqual(settled["observed_output_tokens"], 100)

    def test_usage_serialization_failure_is_durable_and_overrun_is_sticky(self):
        self.dispatch()
        payload = {"prompt_tokens": 100_000, "completion_tokens": 10}
        with mock.patch("neuroctl.budget._json", side_effect=ValueError("injected usage serialization failure")):
            result = self.ledger.reconcile("r1", payload, "openai")
        self.assertEqual(result["state"], "UNRESOLVED")
        self.assertEqual(result["error_code"], "invalid_final_usage")
        self.assertEqual(result["held_cost_usd"], "0.0077031")
        self.assertIsNone(result["settled_cost_usd"])
        self.assertTrue(result["overrun"])
        reopened = BudgetLedger(self.path, self.policy, prices())
        with closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory = sqlite3.Row
            persisted = dict(connection.execute("""SELECT state,error_code,held_cost_usd,
                settled_cost_usd,overrun FROM budget_reservations WHERE reservation_id='r1'""").fetchone())
        self.assertEqual(persisted, {"state": "UNRESOLVED", "error_code": "invalid_final_usage",
                                     "held_cost_usd": "0.0077031", "settled_cost_usd": None, "overrun": 1})
        self.assertTrue(reopened.status(campaign_id="campaign")["campaign"]["blocked"])
        with self.assertRaises(BudgetDenied):
            reopened.reserve(campaign_id="campaign", session_id="session", reservation_id="blocked")
        with self.assertRaises(BudgetError):
            reopened.begin_dispatch("r1")
        settled = reopened.reconcile("r1", payload, "openai")
        self.assertEqual(settled["state"], "SETTLED")
        self.assertEqual(settled["settled_cost_usd"], "0.0077031")
        self.assertTrue(settled["overrun"])
        self.assertEqual(settled["error_code"], "request_bounds_exceeded")
        self.assertTrue(reopened.status(campaign_id="campaign")["campaign"]["blocked"])


if __name__ == "__main__":
    unittest.main()
