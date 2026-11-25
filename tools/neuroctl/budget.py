"""Durable local request reservations; no provider, SDK, or DSH calls.

The caller must route every billable attempt through reserve -> begin_dispatch
and enforce the reserved token limits on the real request. An in-flight call
never expires into free money. Final usage is priced without cache discounts;
missing usage holds its reservation and stops further dispatch in that campaign.
This is a local accounting boundary, not proof of a provider's actual debit.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping

from .cost import ModelPricing, PricingError, Usage, UsageError, estimate_cost, load_pricing, normalize_usage


class BudgetError(ValueError):
    """Invalid policy, conflicting operation, or an unusable ledger."""


class BudgetDenied(BudgetError):
    """A request cannot be reserved or dispatched within the local policy."""


def _amount(value: Any, name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise BudgetError(f"{name} must be an exact decimal string or integer")
    try:
        number = Decimal(value)
    except (ValueError, InvalidOperation) as exc:
        raise BudgetError(f"{name} is not a decimal amount") from exc
    if not number.is_finite() or number < 0 or (positive and number == 0):
        raise BudgetError(f"{name} must be finite and {'positive' if positive else 'non-negative'}")
    return number


def _text(value: Decimal) -> str:
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def _sum(values) -> Decimal:
    """Sum stored decimal strings exactly, independently of global precision."""
    numbers = [Decimal(value) for value in values]
    if not numbers:
        return Decimal(0)
    lowest = min(value.as_tuple().exponent for value in numbers)
    highest = max(value.adjusted() for value in numbers)
    with localcontext() as context:
        context.prec = max(50, highest - lowest + len(str(len(numbers))) + 4)
        return sum(numbers, Decimal(0))


def _count(value: Any, name: str, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0) or value > 2**63 - 1:
        raise BudgetError(f"{name} must be a bounded {'positive' if positive else 'non-negative'} integer")
    return value


def _identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        raise BudgetError(f"{name} must be a short ASCII identifier, not a prompt or a credential")
    return value


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class BudgetPolicy:
    executor: str = "minimax/minimax-m3"
    input_budget_tokens: int = 64_000
    output_budget_tokens: int = 8_000
    maximum_calls_per_work_session: int = 200
    work_session_budget_usd: Decimal = Decimal("5.00")
    initial_campaign_budget_usd: Decimal = Decimal("25.00")
    fallback_model: str = "openai/gpt-6.1-sol"
    fallback_enabled: bool = False
    fallback_maximum_calls_per_work_session: int = 3
    fallback_work_session_budget_usd: Decimal = Decimal("0.50")

    def __post_init__(self) -> None:
        for name in ("executor", "fallback_model"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value) > 256:
                raise BudgetError(f"{name} must be an exact model route")
        if self.executor == self.fallback_model:
            raise BudgetError("Executor and fallback must be distinct routes")
        if type(self.fallback_enabled) is not bool:
            raise BudgetError("fallback_enabled must be a boolean")
        for name in ("input_budget_tokens", "output_budget_tokens", "maximum_calls_per_work_session",
                     "fallback_maximum_calls_per_work_session"):
            _count(getattr(self, name), name, positive=True)
        for name in ("work_session_budget_usd", "initial_campaign_budget_usd", "fallback_work_session_budget_usd"):
            object.__setattr__(self, name, _amount(getattr(self, name), name, positive=True))
        if self.work_session_budget_usd > self.initial_campaign_budget_usd:
            raise BudgetError("Session budget cannot exceed the campaign budget")
        if self.fallback_work_session_budget_usd > self.work_session_budget_usd:
            raise BudgetError("Fallback budget must fit inside the session budget")
        if self.fallback_maximum_calls_per_work_session > self.maximum_calls_per_work_session:
            raise BudgetError("Fallback call limit must fit inside the session call limit")

    @classmethod
    def from_project(cls, path: str | Path) -> "BudgetPolicy":
        try:
            document = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(document, dict) or document.get("schema_version") != 1:
                raise BudgetError("Unsupported project configuration")
            model = document["model_policy"]
            if not isinstance(model, dict):
                raise BudgetError("model_policy must be an object")
            fallback = model["fallback"]
            if not isinstance(fallback, dict):
                raise BudgetError("fallback must be an object")
            if model.get("unknown_cache_discount") != "none" or model.get("budget_cache_discount") != "none":
                raise BudgetError("This budget boundary requires cache discounts to be disabled")
            return cls(
                executor=model["executor"], input_budget_tokens=model["input_budget_tokens"],
                output_budget_tokens=model["output_budget_tokens"],
                maximum_calls_per_work_session=model["maximum_calls_per_work_session"],
                work_session_budget_usd=model["work_session_budget_usd"],
                initial_campaign_budget_usd=model["initial_campaign_budget_usd"],
                fallback_model=fallback["model"], fallback_enabled=fallback["enabled"],
                fallback_maximum_calls_per_work_session=fallback["maximum_calls_per_work_session"],
                fallback_work_session_budget_usd=fallback["work_session_budget_usd"],
            )
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise BudgetError("Cannot read the complete project budget policy") from exc


def _pricing_document(pricing: ModelPricing) -> dict[str, Any]:
    return {key: _text(value) if isinstance(value, Decimal) else value
            for key, value in asdict(pricing).items()}


def _budget_pricing(pricing: ModelPricing) -> ModelPricing:
    # No speculative read/write discount. A known write surcharge is retained;
    # an unpriced write class remains unknown and must fail reconciliation.
    fields = {}
    for name in ("cache_read_per_million", "cache_write_per_million",
                 "cache_write_5m_per_million", "cache_write_1h_per_million"):
        rate = getattr(pricing, name)
        fields[name] = max(pricing.input_per_million, rate) if rate is not None else None
    fields["cache_read_per_million"] = fields["cache_read_per_million"] or pricing.input_per_million
    return replace(pricing, **fields)


def _ordinary_cost(input_tokens: int, output_tokens: int, pricing: ModelPricing) -> Decimal:
    usage = Usage("budget", input_tokens, output_tokens, input_tokens)
    return Decimal(estimate_cost(usage, pricing)["total_cost_usd"])


def _reserve_cost(input_tokens: int, output_tokens: int, pricing: ModelPricing) -> Decimal:
    # The adapter must not request an unpriced write/TTL class. Among the priced
    # classes, all input could be charged at the most expensive known rate.
    rates = [pricing.input_per_million]
    rates.extend(rate for rate in (
        pricing.cache_read_per_million, pricing.cache_write_per_million,
        pricing.cache_write_5m_per_million, pricing.cache_write_1h_per_million,
    ) if rate is not None)
    return _ordinary_cost(input_tokens, output_tokens, replace(pricing, input_per_million=max(rates)))


class BudgetLedger:
    """Cross-process accounting on a local SQLite file with full synchronization.

    Reopen the same file, campaign and session IDs after a restart. Limits and
    rates are pinned in the ledger; edits cannot silently reprice past calls or
    expand an active budget. Only the explicit fallback-enabled switch may
    change, while its fixed sublimits and the overall balances remain intact.
    """

    def __init__(self, db_path: str | Path, policy: BudgetPolicy,
                 prices: Mapping[str, ModelPricing]) -> None:
        if str(db_path) == ":memory:":
            raise BudgetError("A durable file is required, not an in-memory ledger")
        if not isinstance(policy, BudgetPolicy):
            raise BudgetError("policy must be a BudgetPolicy")
        self.path = Path(db_path).resolve()
        self.policy = policy
        self.prices = {}
        for model in (policy.executor, policy.fallback_model):
            pricing = prices.get(model)
            if not isinstance(pricing, ModelPricing) or pricing.model_id != model:
                raise BudgetError("Exact pricing for executor and fallback is required")
            self.prices[model] = pricing
        policy_record = {key: _text(value) if isinstance(value, Decimal) else value
                         for key, value in asdict(policy).items() if key != "fallback_enabled"}
        self.configuration = {"policy": policy_record, "prices": {
            model: _pricing_document(pricing) for model, pricing in self.prices.items()
        }, "budget_cache_discount": "none"}
        self.configuration_json = _json(self.configuration)
        self.configuration_sha256 = hashlib.sha256(self.configuration_json.encode()).hexdigest()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and "budget_meta" not in tables:
                raise BudgetError("Existing database is not a neuro budget ledger")
            connection.execute("CREATE TABLE IF NOT EXISTS budget_meta (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute("""CREATE TABLE IF NOT EXISTS budget_sessions (
                session_id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL)""")
            connection.execute("""CREATE TABLE IF NOT EXISTS budget_reservations (
                reservation_id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL,
                session_id TEXT NOT NULL REFERENCES budget_sessions(session_id),
                model_id TEXT NOT NULL, role TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('RESERVED','IN_FLIGHT','UNRESOLVED','SETTLED','CANCELLED')),
                max_input_tokens INTEGER NOT NULL, max_output_tokens INTEGER NOT NULL,
                reserved_cost_usd TEXT NOT NULL, held_cost_usd TEXT NOT NULL,
                settled_cost_usd TEXT, quote_cost_usd TEXT, usage_json TEXT,
                error_code TEXT, overrun INTEGER NOT NULL DEFAULT 0,
                created_utc TEXT NOT NULL, updated_utc TEXT NOT NULL)""")
            connection.execute("CREATE INDEX IF NOT EXISTS budget_campaign ON budget_reservations(campaign_id)")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(budget_reservations)")}
            for name in ("observed_input_tokens", "observed_output_tokens"):
                if name not in columns:
                    # Additive migration: older journals keep every row and gain
                    # an unknown (NULL) observation floor.
                    connection.execute(f"ALTER TABLE budget_reservations ADD COLUMN {name} INTEGER")
            stored = dict(connection.execute("SELECT name,value FROM budget_meta"))
            if not stored:
                connection.executemany("INSERT INTO budget_meta(name,value) VALUES (?,?)", (
                    ("schema", "neuro.budget.v1"), ("configuration", self.configuration_json)))
            elif stored.get("schema") != "neuro.budget.v1" or stored.get("configuration") != self.configuration_json:
                raise BudgetError("Ledger policy/prices differ; keep the existing budget history and reconcile explicitly")
            self._restore_stored_observation_floors(connection)

    @staticmethod
    def _restore_stored_observation_floors(connection) -> None:
        def unique_keys(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate key in stored usage")
                result[key] = value
            return result

        rows = list(connection.execute("""SELECT reservation_id,state,usage_json,
            observed_input_tokens,observed_output_tokens FROM budget_reservations
            WHERE usage_json IS NOT NULL"""))
        for row in rows:
            try:
                usage = json.loads(row["usage_json"], object_pairs_hook=unique_keys)
                if not isinstance(usage, dict):
                    raise ValueError("stored usage is not an object")
                counters = []
                for name in ("input_tokens", "output_tokens"):
                    value = usage.get(name)
                    if type(value) is not int or value < 0:
                        raise ValueError("stored usage has no reliable counter floor")
                    counters.append(min(value, 2**63 - 1))
            except (TypeError, ValueError, RecursionError):
                if row["state"] == "IN_FLIGHT":
                    connection.execute("""UPDATE budget_reservations SET state='UNRESOLVED',
                        error_code='invalid_stored_usage',updated_utc=? WHERE reservation_id=?""",
                        (_now(), row["reservation_id"]))
                continue
            restored = tuple(max(row[name] or 0, value) for name, value in zip(
                ("observed_input_tokens", "observed_output_tokens"), counters))
            if restored != (row["observed_input_tokens"], row["observed_output_tokens"]):
                connection.execute("""UPDATE budget_reservations SET observed_input_tokens=?,
                    observed_output_tokens=?,updated_utc=? WHERE reservation_id=?""",
                    (*restored, _now(), row["reservation_id"]))

    @classmethod
    def from_project(cls, db_path: str | Path, project_path: str | Path,
                     prices_path: str | Path) -> "BudgetLedger":
        policy = BudgetPolicy.from_project(project_path)
        prices = {model: load_pricing(prices_path, model) for model in (policy.executor, policy.fallback_model)}
        return cls(db_path, policy, prices)

    @contextmanager
    def _transaction(self):
        connection = None
        try:
            connection = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except sqlite3.Error as exc:
            if connection is not None:
                connection.rollback()
            raise BudgetError("Budget ledger transaction failed; no dispatch permission granted") from exc
        except BaseException:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _find(connection, reservation_id: str):
        row = connection.execute("SELECT * FROM budget_reservations WHERE reservation_id=?", (reservation_id,)).fetchone()
        if row is None:
            raise BudgetError("Unknown reservation_id")
        return row

    @staticmethod
    def _totals(rows, limit: Decimal) -> dict[str, Any]:
        active = [row for row in rows if row["state"] != "CANCELLED"]
        spent = _sum(row["settled_cost_usd"] for row in active if row["state"] == "SETTLED")
        held = _sum(row["held_cost_usd"] for row in active if row["state"] != "SETTLED")
        committed = _sum((spent, held))
        unresolved = sum(row["state"] == "UNRESOLVED" for row in active)
        overruns = sum(bool(row["overrun"]) for row in active)
        reasons = []
        if unresolved:
            reasons.append("unresolved_usage")
        if overruns:
            reasons.append("request_bounds_exceeded")
        if committed > limit:
            reasons.append("budget_exceeded")
        return {"limit_usd": _text(limit), "settled_cost_usd": _text(spent),
                "held_cost_usd": _text(held), "committed_cost_usd": _text(committed),
                "remaining_usd": _text(_sum((limit, committed.copy_negate()))),
                "reserved_calls": len(active), "dispatched_calls": sum(
                    row["state"] in ("IN_FLIGHT", "UNRESOLVED", "SETTLED") for row in active),
                "unresolved_calls": unresolved, "overrun_calls": overruns,
                "blocked": bool(reasons), "blocking_reasons": reasons}

    def _status(self, connection, campaign_id: str, session_id: str | None) -> dict[str, Any]:
        rows = list(connection.execute("SELECT * FROM budget_reservations WHERE campaign_id=?", (campaign_id,)))
        result = {"schema": "neuro.budget_status.v1", "campaign_id": campaign_id,
                  "configuration_sha256": self.configuration_sha256,
                  "executor": self.policy.executor, "budget_cache_discount": "none",
                  "billing_verified": False, "dsh_adapter_connected": False,
                  "campaign": self._totals(rows, self.policy.initial_campaign_budget_usd)}
        if session_id is not None:
            session = connection.execute("SELECT campaign_id FROM budget_sessions WHERE session_id=?", (session_id,)).fetchone()
            if session is not None and session[0] != campaign_id:
                raise BudgetError("session_id already belongs to a different campaign")
            session_rows = [row for row in rows if row["session_id"] == session_id]
            session_totals = self._totals(session_rows, self.policy.work_session_budget_usd)
            session_totals["maximum_calls"] = self.policy.maximum_calls_per_work_session
            fallback = self._totals([row for row in session_rows if row["role"] == "fallback"],
                                    self.policy.fallback_work_session_budget_usd)
            fallback.update(model=self.policy.fallback_model, enabled=self.policy.fallback_enabled,
                            maximum_calls=self.policy.fallback_maximum_calls_per_work_session)
            result.update(session_id=session_id, session=session_totals, fallback=fallback)
        return result

    @staticmethod
    def _record(row, *, dispatch_permitted: bool = False) -> dict[str, Any]:
        result = dict(row)
        usage_json = result.pop("usage_json")
        result.update(schema="neuro.budget_reservation.v1", overrun=bool(row["overrun"]),
                      usage=json.loads(usage_json) if usage_json is not None else None,
                      dispatch_permitted=dispatch_permitted, reconciled=row["state"] == "SETTLED",
                      budget_cache_discount="none", billing_verified=False, dsh_adapter_connected=False)
        return result

    def status(self, *, campaign_id: str, session_id: str | None = None) -> dict[str, Any]:
        _identifier(campaign_id, "campaign_id")
        if session_id is not None:
            _identifier(session_id, "session_id")
        with self._transaction() as connection:
            return self._status(connection, campaign_id, session_id)

    def reserve(self, *, campaign_id: str, session_id: str, reservation_id: str,
                model_id: str | None = None, max_input_tokens: int | None = None,
                max_output_tokens: int | None = None) -> dict[str, Any]:
        for name, value in (("campaign_id", campaign_id), ("session_id", session_id), ("reservation_id", reservation_id)):
            _identifier(value, name)
        model_id = self.policy.executor if model_id is None else model_id
        if model_id not in self.prices:
            raise BudgetDenied("Model is outside the executor/fallback policy")
        role = "executor" if model_id == self.policy.executor else "fallback"
        if role == "fallback" and not self.policy.fallback_enabled:
            raise BudgetDenied("Fallback is disabled; it requires explicit bounded configuration")
        max_input_tokens = self.policy.input_budget_tokens if max_input_tokens is None else max_input_tokens
        max_output_tokens = self.policy.output_budget_tokens if max_output_tokens is None else max_output_tokens
        _count(max_input_tokens, "max_input_tokens")
        _count(max_output_tokens, "max_output_tokens")
        if max_input_tokens + max_output_tokens == 0:
            raise BudgetDenied("A dispatch reservation needs a non-zero token bound")
        if max_input_tokens > self.policy.input_budget_tokens or max_output_tokens > self.policy.output_budget_tokens:
            raise BudgetDenied("Requested token bounds exceed the configured per-call limits")
        reserved = _reserve_cost(max_input_tokens, max_output_tokens, self.prices[model_id])
        with self._transaction() as connection:
            previous = connection.execute("SELECT * FROM budget_reservations WHERE reservation_id=?", (reservation_id,)).fetchone()
            if previous is not None:
                intent = (campaign_id, session_id, model_id, max_input_tokens, max_output_tokens)
                actual = tuple(previous[name] for name in (
                    "campaign_id", "session_id", "model_id", "max_input_tokens", "max_output_tokens"))
                if actual != intent:
                    raise BudgetError("reservation_id already represents a different request")
                return self._record(previous)
            status = self._status(connection, campaign_id, session_id)
            if status["campaign"]["blocked"]:
                raise BudgetDenied("Campaign is blocked by unresolved usage or a request/budget overrun")
            for scope in ("campaign", "session") + (("fallback",) if role == "fallback" else ()):
                totals = status[scope]
                if _sum((totals["committed_cost_usd"], reserved)) > Decimal(totals["limit_usd"]):
                    raise BudgetDenied(f"{scope} budget cannot cover the full reservation")
                if "maximum_calls" in totals and totals["reserved_calls"] >= totals["maximum_calls"]:
                    raise BudgetDenied(f"{scope} call limit reached")
            connection.execute("INSERT OR IGNORE INTO budget_sessions(session_id,campaign_id) VALUES (?,?)", (session_id, campaign_id))
            stamp = _now()
            connection.execute("""INSERT INTO budget_reservations
                (reservation_id,campaign_id,session_id,model_id,role,state,max_input_tokens,max_output_tokens,
                 reserved_cost_usd,held_cost_usd,created_utc,updated_utc)
                VALUES (?,?,?,?,?,'RESERVED',?,?,?,?,?,?)""",
                (reservation_id, campaign_id, session_id, model_id, role, max_input_tokens, max_output_tokens,
                 _text(reserved), _text(reserved), stamp, stamp))
            return self._record(self._find(connection, reservation_id))

    def begin_dispatch(self, reservation_id: str) -> dict[str, Any]:
        _identifier(reservation_id, "reservation_id")
        with self._transaction() as connection:
            row = self._find(connection, reservation_id)
            if row["state"] != "RESERVED":
                raise BudgetError("Reservation is not available for a new dispatch; paid retries need a new ID")
            if row["role"] == "fallback" and not self.policy.fallback_enabled:
                raise BudgetDenied("Fallback is disabled")
            status = self._status(connection, row["campaign_id"], row["session_id"])
            if status["campaign"]["blocked"]:
                raise BudgetDenied("Campaign is blocked by unresolved usage or a request/budget overrun")
            connection.execute("UPDATE budget_reservations SET state='IN_FLIGHT',updated_utc=? WHERE reservation_id=?", (_now(), reservation_id))
            return self._record(self._find(connection, reservation_id), dispatch_permitted=True)

    def cancel_undispatched(self, reservation_id: str) -> dict[str, Any]:
        _identifier(reservation_id, "reservation_id")
        with self._transaction() as connection:
            row = self._find(connection, reservation_id)
            if row["state"] == "CANCELLED":
                return self._record(row)
            if row["state"] != "RESERVED":
                raise BudgetError("A dispatched or uncertain request cannot be cancelled into free budget")
            connection.execute("""UPDATE budget_reservations SET state='CANCELLED',held_cost_usd='0',updated_utc=?
                WHERE reservation_id=?""", (_now(), reservation_id))
            return self._record(self._find(connection, reservation_id))

    @staticmethod
    def _observation_floors(row, usage):
        """Confirmed lower bounds of observed input/output counters, never lower."""
        stored_input = row["observed_input_tokens"]
        stored_output = row["observed_output_tokens"]
        if usage is None:
            return stored_input, stored_output
        limit = 2**63 - 1
        return (max(stored_input or 0, min(usage.input_tokens, limit)),
                max(stored_output or 0, min(usage.output_tokens, limit)))

    @staticmethod
    def _below_observed_floor(row, usage) -> bool:
        return (usage.input_tokens < (row["observed_input_tokens"] or 0)
                or usage.output_tokens < (row["observed_output_tokens"] or 0))

    def _held_floor(self, usage: Usage, model_id: str) -> Decimal | None:
        try:
            return _ordinary_cost(usage.input_tokens, usage.output_tokens, self.prices[model_id])
        except (ArithmeticError, PricingError, TypeError, ValueError):
            # A counter too large to price cannot lower the existing hold.
            return None

    def reconcile(self, reservation_id: str, payload: Mapping[str, Any] | None,
                  api_format: str = "auto") -> dict[str, Any]:
        _identifier(reservation_id, "reservation_id")
        with self._transaction() as connection:
            row = self._find(connection, reservation_id)
            if row["state"] in ("RESERVED", "CANCELLED"):
                raise BudgetError("Only a dispatched request can be reconciled")
            usage = None
            reason = None
            settled = None
            quote = None
            try:
                if payload is None:
                    reason = "missing_final_usage"
                else:
                    usage = normalize_usage(payload, api_format)
                    if not usage.accounting_complete:
                        reason = "incomplete_final_usage"
                    else:
                        pricing = self.prices[row["model_id"]]
                        settled = Decimal(estimate_cost(usage, _budget_pricing(pricing))["total_cost_usd"])
                        quote = estimate_cost(usage, pricing)["total_cost_usd"]
                        if self._below_observed_floor(row, usage):
                            # Fewer counters than an earlier reconcile already
                            # observed cannot be the authoritative final usage.
                            reason = "contradicts_observed_floor"
                            settled = None
            except UsageError:
                reason = "invalid_final_usage"
            except PricingError:
                reason = "unpriced_cache_write_class"
            except (ArithmeticError, TypeError, ValueError, RecursionError):
                # An input that cannot be represented or priced is still
                # uncertainty; never lose the durable hold over it.
                reason = "invalid_final_usage"
            try:
                usage_json = _json(asdict(usage)) if usage is not None else None
            except (TypeError, ValueError, RecursionError):
                usage_json = None
                reason = "invalid_final_usage"
                settled = None
                quote = None
            observed_input, observed_output = self._observation_floors(row, usage)
            if row["state"] == "SETTLED":
                if reason is None and usage_json == row["usage_json"]:
                    if (row["observed_input_tokens"], row["observed_output_tokens"]) != (
                            observed_input, observed_output):
                        # Replays of older journals also backfill the floors.
                        connection.execute("""UPDATE budget_reservations
                            SET observed_input_tokens=?,observed_output_tokens=?,updated_utc=?
                            WHERE reservation_id=?""",
                            (observed_input, observed_output, _now(), reservation_id))
                        row = self._find(connection, reservation_id)
                    return self._record(row)
                raise BudgetError("Conflicting final usage for an already settled reservation")
            beyond_tokens = usage is not None and (
                usage.input_tokens > row["max_input_tokens"] or usage.output_tokens > row["max_output_tokens"])
            overrun = bool(row["overrun"]) or beyond_tokens
            if reason is not None:
                held = Decimal(row["held_cost_usd"])
                if usage is not None:
                    # An incomplete response may already reveal more than was
                    # reserved. Retain that observed ordinary-price floor too.
                    floor = self._held_floor(usage, row["model_id"])
                    if floor is not None:
                        held = max(held, floor)
                connection.execute("""UPDATE budget_reservations SET state='UNRESOLVED',held_cost_usd=?,
                    usage_json=COALESCE(?,usage_json),error_code=?,overrun=?,
                    observed_input_tokens=?,observed_output_tokens=?,updated_utc=?
                    WHERE reservation_id=?""",
                    (_text(held), usage_json, reason, int(overrun),
                     observed_input, observed_output, _now(), reservation_id))
            else:
                overrun = overrun or settled > Decimal(row["reserved_cost_usd"])
                connection.execute("""UPDATE budget_reservations SET state='SETTLED',held_cost_usd='0',
                    settled_cost_usd=?,quote_cost_usd=?,usage_json=?,error_code=?,overrun=?,
                    observed_input_tokens=?,observed_output_tokens=?,updated_utc=?
                    WHERE reservation_id=?""", (_text(settled), quote, usage_json,
                    "request_bounds_exceeded" if overrun else None, int(overrun),
                    observed_input, observed_output, _now(), reservation_id))
            return self._record(self._find(connection, reservation_id))
