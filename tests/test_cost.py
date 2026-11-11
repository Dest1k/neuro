"""Accounting tests focus on actual billing risks, not provider connectivity."""

from decimal import Decimal
import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from neuroctl.cost import (  # noqa: E402
    ModelPricing, PricingError, UsageError, estimate_cost, load_pricing, normalize_usage,
)


class CostTests(unittest.TestCase):
    def pricing(self, **kwargs):
        fields = dict(
            model_id="test/exact-route", input_per_million=Decimal("1"),
            output_per_million=Decimal("5"), cache_read_per_million=Decimal("0.1"),
            cache_write_per_million=Decimal("1.25"),
            cache_write_5m_per_million=Decimal("1.25"),
            cache_write_1h_per_million=Decimal("2"),
        )
        fields.update(kwargs)
        return ModelPricing(**fields)

    def test_openai_total_contains_cached_tokens_no_double_count(self):
        usage = normalize_usage({"usage": {
            "prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100,
            "prompt_tokens_details": {"cached_tokens": 900},
            "completion_tokens_details": {"reasoning_tokens": 80},
        }}, "openai")
        result = estimate_cost(usage, self.pricing())
        self.assertEqual(usage.uncached_input_tokens, 100)
        self.assertEqual(result["total_cost_usd"], "0.00069")
        self.assertEqual(result["billable_tokens"]["output"], 100)

    def test_openai_responses_input_alias(self):
        usage = normalize_usage({
            "input_tokens": 1000, "output_tokens": 100,
            "input_tokens_details": {"cached_tokens": 900},
        }, "auto")
        self.assertEqual(estimate_cost(usage, self.pricing())["total_cost_usd"], "0.00069")

    def test_missing_openai_cache_metadata_charged_as_full_input(self):
        usage = normalize_usage({"prompt_tokens": 1000, "completion_tokens": 100}, "openai")
        result = estimate_cost(usage, self.pricing())
        self.assertEqual(result["cache_status"], "UNKNOWN")
        self.assertTrue(result["conservative"])
        self.assertEqual(result["total_cost_usd"], "0.0015")

    def test_anthropic_input_excludes_cache_read_and_write(self):
        usage = normalize_usage({
            "input_tokens": 100, "output_tokens": 100,
            "cache_read_input_tokens": 800, "cache_creation_input_tokens": 100,
        }, "anthropic")
        self.assertEqual(usage.input_tokens, 1000)
        self.assertEqual(estimate_cost(usage, self.pricing())["total_cost_usd"], "0.000805")

    def test_missing_anthropic_metadata_not_a_false_complete_bill(self):
        usage = normalize_usage({"input_tokens": 1000, "output_tokens": 100}, "anthropic")
        result = estimate_cost(usage, self.pricing())
        self.assertEqual(result["total_cost_usd"], "0.0015")
        self.assertFalse(result["accounting_complete"])
        self.assertFalse(result["billing_verified"])
        self.assertEqual(result["cache_status"], "UNKNOWN")

    def test_partial_anthropic_metadata_grants_no_read_discount(self):
        usage = normalize_usage({
            "input_tokens": 100, "output_tokens": 100, "cache_read_input_tokens": 900,
        }, "anthropic")
        result = estimate_cost(usage, self.pricing())
        self.assertEqual(result["total_cost_usd"], "0.0015")
        self.assertFalse(result["accounting_complete"])

    def test_ttl_writes_are_separate_not_added_to_total_twice(self):
        usage = normalize_usage({
            "input_tokens": 100, "output_tokens": 0,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 300,
            "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 200},
        }, "anthropic")
        self.assertEqual(usage.input_tokens, 400)
        result = estimate_cost(usage, self.pricing())
        self.assertEqual(result["billable_tokens"]["cache_write"], 0)
        self.assertEqual(result["total_cost_usd"], "0.000625")

    def test_complete_ttl_breakdown_can_supply_missing_write_total(self):
        usage = normalize_usage({
            "input_tokens": 100, "output_tokens": 0, "cache_read_input_tokens": 0,
            "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 200},
        }, "anthropic")
        self.assertEqual(usage.cache_write_tokens, 300)
        self.assertTrue(usage.accounting_complete)

    def test_unknown_cache_read_price_uses_full_rate(self):
        usage = normalize_usage({
            "input_tokens": 1000, "output_tokens": 100,
            "input_tokens_details": {"cached_tokens": 900},
        }, "openai")
        result = estimate_cost(usage, self.pricing(cache_read_per_million=None))
        self.assertEqual(result["total_cost_usd"], "0.0015")
        self.assertTrue(result["conservative"])

    def test_unknown_write_rate_fails_closed(self):
        usage = normalize_usage({
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 100,
        }, "anthropic")
        with self.assertRaises(PricingError):
            estimate_cost(usage, self.pricing(cache_write_per_million=None))

    def test_generic_write_price_does_not_invent_one_hour_price(self):
        usage = normalize_usage({
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 100,
            "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 100},
        }, "anthropic")
        with self.assertRaises(PricingError):
            estimate_cost(usage, self.pricing(cache_write_1h_per_million=None))

    def test_rejects_negative_boolean_float_string_and_null_counters(self):
        for bad in (-1, True, 1.0, "1", None):
            with self.subTest(bad=bad), self.assertRaises(UsageError):
                normalize_usage({"prompt_tokens": bad, "completion_tokens": 0}, "openai")

    def test_negative_reasoning_detail_is_rejected_even_without_double_counting(self):
        with self.assertRaises(UsageError):
            normalize_usage({
                "prompt_tokens": 100, "completion_tokens": 10,
                "completion_tokens_details": {"reasoning_tokens": -1},
            }, "openai")

    def test_rejects_cache_larger_than_total(self):
        with self.assertRaises(UsageError):
            normalize_usage({
                "prompt_tokens": 100, "completion_tokens": 0,
                "prompt_tokens_details": {"cached_tokens": 101},
            }, "openai")

    def test_rejects_conflicting_aliases_and_total(self):
        for payload in (
            {"prompt_tokens": 100, "input_tokens": 200, "completion_tokens": 0},
            {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 100},
        ):
            with self.subTest(payload=payload), self.assertRaises(UsageError):
                normalize_usage(payload, "openai")

    def test_rejects_inconsistent_ttl_totals(self):
        with self.assertRaises(UsageError):
            normalize_usage({
                "input_tokens": 0, "output_tokens": 0,
                "cache_read_input_tokens": 0, "cache_creation_input_tokens": 300,
                "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 100},
            }, "anthropic")

    def test_rejects_unknown_positive_write_ttl(self):
        with self.assertRaises(UsageError):
            normalize_usage({
                "input_tokens": 0, "output_tokens": 0,
                "cache_read_input_tokens": 0, "cache_creation_input_tokens": 100,
                "cache_creation": {"ephemeral_24h_input_tokens": 100},
            }, "anthropic")

    def test_ambiguous_format_requires_user_choice(self):
        with self.assertRaises(UsageError):
            normalize_usage({"input_tokens": 100, "output_tokens": 10}, "auto")

    def test_stream_fragment_rejected(self):
        with self.assertRaises(UsageError):
            normalize_usage({
                "type": "message_delta", "usage": {"output_tokens": 20},
            }, "anthropic")

    def test_router_write_premium_is_not_silently_ignored(self):
        with self.assertRaises(UsageError):
            normalize_usage({
                "prompt_tokens": 100, "completion_tokens": 0,
                "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 100},
            }, "openai")

    def test_top_level_router_write_aliases_are_not_silently_ignored(self):
        for name in ("cache_write_tokens", "cache_creation_tokens"):
            with self.subTest(counter=name), self.assertRaises(UsageError):
                normalize_usage({
                    "prompt_tokens": 100, "completion_tokens": 0, name: 100,
                }, "openai")

    def test_reasoning_detail_cannot_exceed_reported_output(self):
        for name in ("completion_tokens_details", "output_tokens_details"):
            with self.subTest(details=name), self.assertRaises(UsageError):
                normalize_usage({
                    "prompt_tokens": 100, "completion_tokens": 10,
                    name: {"reasoning_tokens": 100},
                }, "openai")

    def test_mixed_format_ttl_write_premium_is_not_ignored(self):
        with self.assertRaises(UsageError):
            normalize_usage({
                "prompt_tokens": 100, "completion_tokens": 0,
                "prompt_tokens_details": {"cached_tokens": 0},
                "cache_creation": {"ephemeral_5m_input_tokens": 100},
            }, "openai")

    def test_decimal_math_exact_and_json_serializable(self):
        usage = normalize_usage({"prompt_tokens": 3, "completion_tokens": 7}, "openai")
        result = estimate_cost(usage, self.pricing(
            input_per_million=Decimal("0.281"), output_per_million=Decimal("1.403"),
        ))
        self.assertEqual(result["total_cost_usd"], "0.000010664")
        self.assertIsInstance(json.dumps(result), str)

    def test_decimal_addition_preserves_widely_separated_places(self):
        usage = normalize_usage({"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}, "openai")
        result = estimate_cost(usage, self.pricing(
            input_per_million=Decimal("1E+40"), output_per_million=Decimal("1E-40"),
        ))
        self.assertEqual(result["total_cost_usd"], "1" + "0" * 40 + "." + "0" * 39 + "1")

    def test_rejects_invalid_prices(self):
        for bad in (0.1, True, "-1", "NaN", "Infinity"):
            with self.subTest(bad=bad), self.assertRaises(PricingError):
                self.pricing(input_per_million=bad)

    def test_manifest_exact_routes_and_source(self):
        path = ROOT / "configs" / "model-prices.json"
        price = load_pricing(path, "anthropic/claude-sonnet-5")
        self.assertEqual(price.cache_write_per_million, Decimal("0.4278"))
        self.assertFalse(price.route_verified)
        self.assertEqual(price.source, "user_table_2026-11-11")
        self.assertIsNone(price.cache_write_1h_per_million)
        with self.assertRaises(PricingError):
            load_pricing(path, "sonnet-5")

    def test_user_scenario_sol_no_cache_discount(self):
        usage = normalize_usage({
            "input_tokens": 6_400_000, "output_tokens": 200_000,
            "input_tokens_details": {"cached_tokens": 5_760_000},
        }, "openai")
        price = load_pricing(ROOT / "configs" / "model-prices.json", "openai/gpt-6.1-sol")
        self.assertEqual(estimate_cost(usage, price)["total_cost_usd"], "2.079")

    def test_user_scenario_m3_cache_discount(self):
        usage = normalize_usage({
            "input_tokens": 6_400_000, "output_tokens": 200_000,
            "input_tokens_details": {"cached_tokens": 5_760_000},
        }, "openai")
        price = load_pricing(ROOT / "configs" / "model-prices.json", "minimax/minimax-m3")
        self.assertEqual(estimate_cost(usage, price)["total_cost_usd"], "0.199984")


if __name__ == "__main__":
    unittest.main()
