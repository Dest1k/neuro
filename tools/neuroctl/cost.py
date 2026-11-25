"""Offline token accounting for a single, final API usage snapshot.

This module never contacts a provider.  Rates are assumptions supplied by the
caller, not confirmation that a route exists or that its invoices use them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, localcontext
import json
from pathlib import Path
from typing import Any, Mapping


MILLION = Decimal(1_000_000)


class UsageError(ValueError):
    """Usage is malformed, contradictory, or ambiguous."""


class PricingError(ValueError):
    """A price is invalid or a reported charge cannot be priced safely."""


def _counter(value: Any, name: str) -> int:
    # bool is an int subclass; API counters must still reject it.
    if type(value) is not int or value < 0:
        raise UsageError(f"{name} must be a non-negative integer")
    return value


def _decimal(value: Any, name: str) -> Decimal:
    # Do not import binary-float rounding into currency accounting.
    if isinstance(value, bool) or not isinstance(value, (Decimal, str, int)):
        raise PricingError(f"{name} must be a Decimal, decimal string, or integer")
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise PricingError(f"{name} is not a decimal amount") from exc
    if not result.is_finite() or result < 0:
        raise PricingError(f"{name} must be finite and non-negative")
    return result


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise UsageError(f"{name} must be an object")
    return value


def _validate_token_counters(data: Mapping[str, Any], prefix: str = "usage") -> None:
    """Also reject malformed detail counters that are not separately billed."""
    for name, value in data.items():
        if not isinstance(name, str):
            raise UsageError("Usage object keys must be strings")
        location = f"{prefix}.{name}"
        if name.endswith("_tokens"):
            _counter(value, location)
        elif isinstance(value, Mapping):
            _validate_token_counters(value, location)


def _required_alias(data: Mapping[str, Any], names: tuple[str, ...]) -> int:
    present = [(name, _counter(data[name], name)) for name in names if name in data]
    if not present:
        raise UsageError(f"Missing required counter: {' or '.join(names)}")
    if any(value != present[0][1] for _, value in present[1:]):
        raise UsageError(f"Conflicting counters: {' and '.join(names)}")
    return present[0][1]


# A final provider document names one usage source: either a wrapper object
# ("usage") or bare counters.  Duplicating counters at both levels is legal
# only while both copies agree; otherwise the document has no single meaning.
_TOP_LEVEL_COUNTERS = (
    "prompt_tokens", "input_tokens", "completion_tokens", "output_tokens",
    "total_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
    "cache_write_tokens", "cache_creation_tokens", "cache_creation",
    "prompt_tokens_details", "input_tokens_details",
    "completion_tokens_details", "output_tokens_details",
)
_COUNTER_ALIASES = {
    "prompt_tokens": "input_tokens", "input_tokens": "prompt_tokens",
    "completion_tokens": "output_tokens", "output_tokens": "completion_tokens",
}


def _counter_counterpart(name: str, wrapper: Mapping[str, Any]) -> str | None:
    for key in (name, _COUNTER_ALIASES.get(name)):
        if key is not None and key in wrapper:
            return key
    return None


def _same_counter(wrapper_value: Any, top_value: Any, where: str) -> None:
    if isinstance(wrapper_value, Mapping) or isinstance(top_value, Mapping):
        if not (isinstance(wrapper_value, Mapping) and isinstance(top_value, Mapping)):
            raise UsageError(f"Conflicting {where} between the top level and usage object")
        for key, value in top_value.items():
            if key not in wrapper_value:
                raise UsageError(
                    f"Ambiguous usage: top-level {where}.{key} has no usage counterpart")
            _same_counter(wrapper_value[key], value, f"{where}.{key}")
        return
    # bool is an int subclass; a boolean copy is never a valid token count.
    if type(wrapper_value) is not type(top_value) or wrapper_value != top_value:
        raise UsageError(f"Conflicting {where} between the top level and usage object")


def _reject_duplicated_counters(payload: Mapping[str, Any], wrapper: Mapping[str, Any]) -> None:
    for name in _TOP_LEVEL_COUNTERS:
        if name not in payload:
            continue
        counterpart = _counter_counterpart(name, wrapper)
        if counterpart is None:
            raise UsageError(
                f"Ambiguous usage: top-level {name} has no usage counterpart")
        _same_counter(wrapper[counterpart], payload[name], name)


@dataclass(frozen=True)
class ModelPricing:
    model_id: str
    input_per_million: Decimal
    output_per_million: Decimal
    cache_read_per_million: Decimal | None = None
    cache_write_per_million: Decimal | None = None
    cache_write_5m_per_million: Decimal | None = None
    cache_write_1h_per_million: Decimal | None = None
    currency: str = "USD"
    source: str = "user_table_2026-11-11"
    route_verified: bool = False
    context_window_tokens: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.model_id, str) or not self.model_id.strip():
            raise PricingError("model_id must be a non-empty exact route identifier")
        if self.currency != "USD":
            raise PricingError("Only USD pricing is supported")
        if type(self.route_verified) is not bool:
            raise PricingError("route_verified must be a boolean")
        for name in (
            "input_per_million", "output_per_million", "cache_read_per_million",
            "cache_write_per_million", "cache_write_5m_per_million",
            "cache_write_1h_per_million",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _decimal(value, name))
            elif name in ("input_per_million", "output_per_million"):
                raise PricingError(f"{name} is required")
        if self.context_window_tokens is not None:
            _counter(self.context_window_tokens, "context_window_tokens")


@dataclass(frozen=True)
class Usage:
    api_format: str
    input_tokens: int
    output_tokens: int
    uncached_input_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    cache_status: str = "UNKNOWN"
    accounting_complete: bool = True
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        for name in (
            "input_tokens", "output_tokens", "uncached_input_tokens",
            "cache_read_tokens", "cache_write_tokens", "cache_write_5m_tokens",
            "cache_write_1h_tokens",
        ):
            _counter(getattr(self, name), name)
        if self.input_tokens != (
            self.uncached_input_tokens + self.cache_read_tokens + self.cache_write_tokens
        ):
            raise UsageError("Input total must equal uncached + cache-read + cache-write")
        if self.cache_write_5m_tokens + self.cache_write_1h_tokens > self.cache_write_tokens:
            raise UsageError("Cache write TTL breakdown exceeds cache write total")
        if self.cache_status not in ("KNOWN", "UNKNOWN"):
            raise UsageError("cache_status must be KNOWN or UNKNOWN")
        if type(self.accounting_complete) is not bool:
            raise UsageError("accounting_complete must be a boolean")


def normalize_usage(payload: Mapping[str, Any], api_format: str = "auto") -> Usage:
    """Normalize a final response or its usage object without guessing discounts.

    OpenAI prompt/input totals include cached input. Anthropic input_tokens
    excludes cache reads and writes. Missing Anthropic cache counters make the
    observed total incomplete: an estimate is returned, but it is explicitly
    unsuitable as a spending ceiling. Do not pass streaming event fragments.
    """
    payload = _mapping(payload, "payload")
    if payload.get("type") in (
        "message_start", "message_delta", "content_block_delta", "response.created",
        "response.in_progress", "response.output_text.delta",
    ) or payload.get("object") == "chat.completion.chunk":
        raise UsageError("Use one complete final usage snapshot, not a streaming fragment")
    data = _mapping(payload["usage"], "usage") if "usage" in payload else payload
    if "usage" in payload:
        _reject_duplicated_counters(payload, data)
    _validate_token_counters(data)
    if not isinstance(api_format, str):
        raise UsageError("api_format must be a string")
    fmt = api_format.lower().replace("-", "_")
    if fmt == "auto":
        has_openai = any(k in data for k in (
            "prompt_tokens", "prompt_tokens_details", "input_tokens_details",
        ))
        has_anthropic = any(k in data for k in (
            "cache_read_input_tokens", "cache_creation_input_tokens", "cache_creation",
        ))
        if has_openai and has_anthropic:
            raise UsageError("Mixed usage formats require explicit api_format")
        if has_openai:
            fmt = "openai"
        elif has_anthropic:
            fmt = "anthropic"
        else:
            raise UsageError("Cannot infer usage format; specify openai or anthropic")

    if fmt in ("openai", "openai_chat", "openai_responses"):
        total = _required_alias(data, ("prompt_tokens", "input_tokens"))
        output = _required_alias(data, ("completion_tokens", "output_tokens"))
        if "total_tokens" in data:
            reported_total = _counter(data["total_tokens"], "total_tokens")
            if reported_total != total + output:
                raise UsageError("total_tokens does not equal input + output")
        for write_name in ("cache_write_tokens", "cache_creation_tokens"):
            if write_name in data and _counter(data[write_name], write_name):
                raise UsageError("Router cache-write counters require a documented usage adapter")
        for field_name in ("completion_tokens_details", "output_tokens_details"):
            if field_name not in data or data[field_name] is None:
                continue
            details = _mapping(data[field_name], field_name)
            if ("reasoning_tokens" in details
                    and _counter(details["reasoning_tokens"], f"{field_name}.reasoning_tokens") > output):
                raise UsageError("reasoning_tokens exceeds output total")
        cache_values = []
        for field_name in ("prompt_tokens_details", "input_tokens_details"):
            if field_name not in data or data[field_name] is None:
                continue
            details = _mapping(data[field_name], field_name)
            if "cached_tokens" in details:
                cache_values.append(_counter(details["cached_tokens"], f"{field_name}.cached_tokens"))
            # These are not standard OpenAI counters. Silently ignoring them
            # could omit a router's cache-write premium.
            for write_name in ("cache_write_tokens", "cache_creation_tokens", "cache_creation_input_tokens"):
                if write_name in details:
                    value = _counter(details[write_name], f"{field_name}.{write_name}")
                    if value:
                        raise UsageError("Router cache-write details require a documented usage adapter")
        if any(v != cache_values[0] for v in cache_values[1:]):
            raise UsageError("Conflicting cached_tokens counters")
        for key in ("cache_creation_input_tokens", "cache_read_input_tokens"):
            if key in data:
                value = _counter(data[key], key)
                if key == "cache_creation_input_tokens" and value:
                    raise UsageError("Mixed-format cache writes require a documented usage adapter")
                if key == "cache_read_input_tokens" and (not cache_values or value != cache_values[0]):
                    raise UsageError("Mixed-format cache reads disagree with OpenAI cache details")
        if "cache_creation" in data and data["cache_creation"] is not None:
            creation = _mapping(data["cache_creation"], "cache_creation")
            if any(value for key, value in creation.items() if key.endswith("_input_tokens")):
                raise UsageError("Mixed-format cache-write TTLs require a documented usage adapter")
        cached = cache_values[0] if cache_values else 0
        if cached > total:
            raise UsageError("cached_tokens exceeds input total")
        warnings = () if cache_values else ("cache_metadata_missing_charge_full_input",)
        return Usage(
            api_format="openai", input_tokens=total, output_tokens=output,
            uncached_input_tokens=total - cached, cache_read_tokens=cached,
            cache_status="KNOWN" if cache_values else "UNKNOWN",
            accounting_complete=True, warnings=warnings,
        )

    if fmt != "anthropic":
        raise UsageError("api_format must be openai, openai_chat, openai_responses, anthropic, or auto")
    if any(k in data for k in ("prompt_tokens", "prompt_tokens_details", "input_tokens_details")):
        raise UsageError("OpenAI totals cannot be interpreted as Anthropic uncached input")
    uncached = _required_alias(data, ("input_tokens",))
    output = _required_alias(data, ("output_tokens",))
    has_read = "cache_read_input_tokens" in data
    read = _counter(data["cache_read_input_tokens"], "cache_read_input_tokens") if has_read else 0
    has_write = "cache_creation_input_tokens" in data
    write = _counter(data["cache_creation_input_tokens"], "cache_creation_input_tokens") if has_write else 0
    write_5m = write_1h = 0
    has_complete_ttl_breakdown = False
    if "cache_creation" in data and data["cache_creation"] is not None:
        creation = _mapping(data["cache_creation"], "cache_creation")
        for key in creation:
            if key.endswith("_input_tokens"):
                _counter(creation[key], f"cache_creation.{key}")
        if "ephemeral_5m_input_tokens" in creation:
            write_5m = _counter(creation["ephemeral_5m_input_tokens"], "cache_creation.ephemeral_5m_input_tokens")
        if "ephemeral_1h_input_tokens" in creation:
            write_1h = _counter(creation["ephemeral_1h_input_tokens"], "cache_creation.ephemeral_1h_input_tokens")
        unknown_ttl_tokens = sum(
            value for key, value in creation.items()
            if key.endswith("_input_tokens")
            and key not in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")
        )
        if unknown_ttl_tokens:
            raise UsageError("Unknown cache-write TTL class; supply a documented usage adapter")
        has_complete_ttl_breakdown = all(k in creation for k in (
            "ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens",
        ))
        if not has_write:
            if has_complete_ttl_breakdown:
                write = write_5m + write_1h
                has_write = True
            elif write_5m or write_1h:
                # Count all observed writes, but do not claim the total is complete.
                write = write_5m + write_1h
        if write_5m + write_1h > write:
            raise UsageError("Cache write TTL breakdown exceeds cache creation total")
        if has_write and has_complete_ttl_breakdown and write_5m + write_1h != write:
            raise UsageError("Complete cache-write TTL breakdown must equal cache creation total")
    complete = has_read and has_write
    total = uncached + read + write
    warnings = () if complete else (
        "anthropic_cache_metadata_incomplete_observed_total_only",
        "unknown_cache_no_read_discount",
    )
    return Usage(
        api_format="anthropic", input_tokens=total, output_tokens=output,
        uncached_input_tokens=uncached, cache_read_tokens=read,
        cache_write_tokens=write, cache_write_5m_tokens=write_5m,
        cache_write_1h_tokens=write_1h,
        cache_status="KNOWN" if complete else "UNKNOWN",
        accounting_complete=complete, warnings=warnings,
    )


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def estimate_cost(usage: Usage, pricing: ModelPricing) -> dict[str, Any]:
    """Return a JSON-serializable USD estimate; unknown write prices fail closed.

    Missing cache metadata never grants a discount. A generic cache-write rate
    prices only unspecified-TTL writes, never an explicitly reported 1h/5m tier.
    Output tokens must already include chargeable reasoning; detail counters are
    not added again because OpenAI/Anthropic output totals already include them.
    """
    if not isinstance(usage, Usage) or not isinstance(pricing, ModelPricing):
        raise TypeError("estimate_cost expects Usage and ModelPricing")
    assumptions = list(usage.warnings)
    read_rate = pricing.cache_read_per_million
    conservative = usage.cache_status == "UNKNOWN"
    if usage.cache_status == "UNKNOWN":
        read_rate = pricing.input_per_million
    elif read_rate is None:
        read_rate = pricing.input_per_million
        if usage.cache_read_tokens:
            assumptions.append("cache_read_rate_unknown_charge_regular_input")
            conservative = True
    generic_writes = usage.cache_write_tokens - usage.cache_write_5m_tokens - usage.cache_write_1h_tokens
    entries = {
        "uncached_input": (usage.uncached_input_tokens, pricing.input_per_million),
        "cache_read": (usage.cache_read_tokens, read_rate),
        "cache_write": (generic_writes, pricing.cache_write_per_million),
        "cache_write_5m": (usage.cache_write_5m_tokens, pricing.cache_write_5m_per_million),
        "cache_write_1h": (usage.cache_write_1h_tokens, pricing.cache_write_1h_per_million),
        "output": (usage.output_tokens, pricing.output_per_million),
    }
    for name, (tokens, rate) in entries.items():
        if tokens and rate is None:
            raise PricingError(f"Missing {name} price for {tokens} reported tokens; refusing a guessed cost")
    # Decimal's global default is 28 digits. Raise local precision from operand
    # sizes so even large integer counters do not silently round the bill.
    priced_entries = [(tokens, rate) for tokens, rate in entries.values() if rate is not None]
    lowest_exponent = min(rate.as_tuple().exponent - 6 for _, rate in priced_entries)
    highest_position = max(
        len(str(tokens)) + rate.adjusted() - 6 for tokens, rate in priced_entries
    )
    precision = max(50, highest_position - lowest_exponent + 20)
    with localcontext() as context:
        context.prec = precision
        amounts = {
            name: Decimal(tokens) * rate / MILLION if rate is not None else Decimal(0)
            for name, (tokens, rate) in entries.items()
        }
        total = sum(amounts.values(), Decimal(0))
    if not pricing.route_verified:
        assumptions.append("route_and_billing_not_verified_prices_are_user_supplied")
    if not usage.accounting_complete:
        assumptions.append("not_a_spending_ceiling_missing_provider_counters")
    return {
        "model_id": pricing.model_id,
        "currency": "USD",
        "price_source": pricing.source,
        "route_verified": pricing.route_verified,
        "api_format": usage.api_format,
        "cache_status": usage.cache_status,
        "accounting_complete": usage.accounting_complete,
        "conservative": conservative,
        # Offline arithmetic is never proof of a provider's actual debit.
        "billing_verified": False,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "billable_tokens": {name: tokens for name, (tokens, _) in entries.items()},
        "rates_usd_per_million": {
            name: _decimal_text(rate) if rate is not None else None
            for name, (_, rate) in entries.items()
        },
        "cost_usd": {name: _decimal_text(value) for name, value in amounts.items()},
        "total_cost_usd": _decimal_text(total),
        "assumptions": assumptions,
    }


def load_pricing(path: str | Path, model_id: str) -> ModelPricing:
    """Load an exact, case-sensitive route from the user-provided price manifest."""
    try:
        with Path(path).open("r", encoding="utf-8") as stream:
            document = json.load(stream, parse_float=Decimal)
    except (OSError, json.JSONDecodeError) as exc:
        raise PricingError("Cannot read pricing manifest") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise PricingError("Unsupported pricing manifest schema")
    models = document.get("models")
    if not isinstance(models, dict) or model_id not in models:
        raise PricingError(f"No price for exact model route {model_id!r}")
    record = models[model_id]
    if not isinstance(record, dict):
        raise PricingError("Model pricing record must be an object")
    fields = (
        "input_per_million", "output_per_million", "cache_read_per_million",
        "cache_write_per_million", "cache_write_5m_per_million",
        "cache_write_1h_per_million", "context_window_tokens",
    )
    if any(name not in record for name in ("input_per_million", "output_per_million")):
        raise PricingError("Model input and output prices are required")
    return ModelPricing(
        model_id=model_id,
        **{name: record[name] for name in fields if name in record},
        currency=document.get("currency", "USD"),
        source=document.get("source_id", "unspecified"),
        route_verified=record.get("route_verified", False),
    )
