#!/usr/bin/env python3
"""
AURA v0.5.3.40 -- Pre-Submission Revalidation + Unified Execution Audit Trail

Built against the approved design document (AURA_v0.5.3.40_Design_Document
_2026-09-12.md) and the pre-implementation investigation
(AURA_v0.5.3.40_Pre_Implementation_Investigation_2026-09-12.md), both
produced before any line of this module was written.

============================================================================
WHAT THIS MODULE IS
============================================================================
Two cohesive halves, built together because every revalidation outcome is
also an audit event:

1. REVALIDATION: a new, read-only, venue-neutral market-state check --
   fetch a fresh price immediately before submission, compare it against
   the price the decision was actually made against (reference_price,
   added to v0.5.3.33 alongside this module), and fail closed on drift,
   staleness, or a fetch failure. This is Martin's approved Option B: a
   genuine live-price check, not a TTL/guardrail-only revalidation. It
   does NOT guarantee the execution price -- it bounds a market-state
   check immediately before submission (design doc Section 3.4).

2. AUDIT TRAIL: a venue-neutral, event-sourced, hash-verified record per
   client_order_id, covering DECISION -> AUTHORIZED -> REVALIDATED ->
   CONSUMED -> SUBMISSION_ATTEMPTED -> OUTCOME for both MEXC and Alpaca.
   Storage/locking/hashing conventions are copied from v0.5.3.29/.39, not
   reinvented: O_CREAT|O_EXCL advisory locks (WITH the parent-directory
   creation fix v0.5.3.39 discovered the hard way -- baked in here from
   the start, not rediscovered), atomic tmp-then-os.replace() writes,
   append-only events, derive_state() replay, self-verifying record_hash.

   For MEXC's SUBMISSION_ATTEMPTED/OUTCOME events specifically, this
   module does NOT re-derive or duplicate what v0.5.3.29 already owns --
   it records a REFERENCE to v0.5.3.29's own record_hash/current_state
   (design doc Section 2.2). v0.5.3.29 remains the sole authority for
   what actually happened on the MEXC broker side; this trail can never
   disagree with it, because it never tries to independently decide.

============================================================================
WHAT THIS MODULE DELIBERATELY DOES NOT DO (non-goals, design doc Sec.11)
============================================================================
- No AI/ML anywhere in the revalidation decision. Drift/staleness
  thresholds are deterministic config values, same discipline as
  v0.5.3.39's promotion criteria.
- No broker/API functionality beyond the new read-only price query. This
  module never places, cancels, or modifies an order.
- No Alpaca reconciliation spine (Martin's explicit instruction #3 on the
  .40 GO). An Alpaca EXECUTION_UNCERTAIN outcome recorded here has no
  automated resolution path -- see record_outcome()'s docstring.
- No change to v0.5.3.29's INTENT_CREATED->CLAIMED->SUBMISSION_ATTEMPTED
  ->... spine, and no new v0.5.3.29 transition. The pre-implementation
  investigation found the existing NEW-state-is-a-safe-resting-state
  design already handles a pre-claim revalidation rejection correctly --
  nothing needed changing there.
- Never releases/deletes a claim. This module has no concept of
  "unclaim" at all -- it only ever appends CONSUMED once, like
  v0.5.3.32/.37's claim() never having a release function.

============================================================================
THE RACE, NOT HAND-WAVED (design doc Section 5)
============================================================================
The gap between "price observed fresh" and "order actually reaches the
venue" cannot be reduced to zero without holding a lock across the price
fetch and the network call to the broker -- architecturally heavy, and not
what Option B asked for. Instead: every REVALIDATED event carries the
venue's own quote timestamp (price_checked_at derived from it), every
CONSUMED/SUBMISSION_ATTEMPTED event carries its own timestamp, so the gap
is always reconstructable from the audit trail, never invisible. A hard
ceiling (check_revalidation_to_submission_ceiling(), called by the caller
immediately before the actual submit() call) fails closed
(REVALIDATION_EXPIRED_BEFORE_SUBMISSION) if too much time elapsed between
the price check and the submission attempt -- covering pathological
scheduling delays, not just the expected-fast path. The claim, if already
granted, is NEVER released by this check; it only blocks the submit call
itself from proceeding.

============================================================================
reference_price PROVENANCE (pre-implementation investigation finding)
============================================================================
v0.5.3.25 (position sizing) was traced and found NOT to be a usable source
for this field -- it feeds the old, unrelated v0.5.3.12-.23 Alpaca-crypto-
spot chain, not this v0.5.3.33-.38/.40 track (MEXC futures / Alpaca
stock-ETF), confirmed by grep (zero callers of .25 anywhere in .33-.40)
and by v0.5.3.33's own docstring (section D). reference_price is
therefore designed exactly like quantity already is on the canonical
spec: caller-supplied, validated, never fetched or invented by this
module or by v0.5.3.33. When it is absent for a market order,
revalidate_market_state() below fails closed
(NO_REFERENCE_PRICE_AVAILABLE) rather than skipping the check or
inventing a value.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

VERSION = "AURA v0.5.3.40"
ENGINE = "EXECUTION_AUDIT_TRAIL"
SCHEMA_VERSION = "1.0"

VENUES = frozenset({"MEXC", "ALPACA"})
ASSET_CLASSES = frozenset({"CRYPTO_FUTURES", "CRYPTO_SPOT", "STOCK", "ETF"})

EVENT_TYPES = frozenset(
    {"DECISION", "AUTHORIZED", "REVALIDATED", "CONSUMED", "SUBMISSION_ATTEMPTED", "OUTCOME", "RESOLVED"}
)

# RESOLVED was added in v0.5.3.45 (Alpaca EXECUTION_UNCERTAIN resolution
# authority) -- see that module's docstring for the full rationale. It is
# legal exactly once, only immediately after any OUTCOME:* state, and only
# for ALPACA records (enforced in record_resolved() below, not here --
# derive_state() only knows about event sequences, never about a record's
# other fields). Adding a new event type to an already-shipped state
# machine is additive only: every existing legal sequence (DECISION ->
# ... -> OUTCOME:*, terminal) is completely unaffected because RESOLVED
# did not exist as a legal follow-on before this change, so no prior
# record's derived state can change. MEXC's OUTCOME:* records are never
# expected to receive a RESOLVED event -- MEXC's own .29/.30 remain sole
# authority for that side (record_resolved() enforces this explicitly).

DEFAULT_AUDIT_BASE_DIR = Path("regime_output/execution_audit_trail/records")

# Deterministic config defaults -- all overridable per call, never a
# silent hidden default a caller can't see or reason about. No field
# here is ever inferred from market conditions "intelligently" -- fixed
# thresholds only, per the no-AI/ML non-goal.
DEFAULT_MAX_QUOTE_AGE_SECONDS = 10.0
DEFAULT_MAX_PRICE_DRIFT_BPS = 50.0
DEFAULT_MAX_REVALIDATION_TO_SUBMISSION_SECONDS = 5.0

# Ordinary clock skew between our machine and a venue's own clock, or
# between our machine and now_dt supplied by a caller/test -- not a
# revalidation policy knob, just tolerance for imprecise clocks. A quote
# claiming to be more than this far in the future is still rejected.
CLOCK_SKEW_TOLERANCE_SECONDS = 2.0


class AuditTrailError(Exception):
    """Raised for structural/storage failures in the audit trail half
    (corrupt record, illegal event sequence, lock timeout, record
    already exists). Distinct from RevalidationRejected, which is the
    expected, structured outcome of a revalidation check failing."""


class IllegalTransitionError(Exception):
    pass


class RevalidationRejected(Exception):
    """Raised by revalidate_market_state() and
    check_revalidation_to_submission_ceiling() with .reason set to one
    of the documented failure-mode codes (module docstring, and design
    doc Section 9). Callers catch this, map .reason/.detail onto their
    own existing rejection shape (.31/.36's _reject()), and always write
    a REVALIDATED audit event either way -- this module never returns a
    differently-shaped success vs failure result; success returns a
    dict, failure always raises."""

    def __init__(self, reason: str, detail: Any = None):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}:{detail}")


# --------------------------------------------------------------------- #
# shared helpers -- identical convention to every other AURA module
# --------------------------------------------------------------------- #

def stable_json(obj: Any) -> str:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(text: Any) -> datetime | None:
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _decimal_positive(value: Any, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise RevalidationRejected("INVALID_REFERENCE_PRICE", f"{field}={value!r}")
    if not number.is_finite() or number <= 0:
        raise RevalidationRejected("INVALID_REFERENCE_PRICE", f"{field}={value!r}")
    return number


# ============================================================================
# PART 1 -- live price fetch (venue-neutral abstraction, read-only)
# ============================================================================
# Both fetch_* functions take a caller-supplied client (a real ccxt.mexc
# instance / real Alpaca data client in production, a fake test double in
# every test here) -- same injectable-client convention .27's
# build_exchange()/exchange param and .35's alpaca_client param already
# use throughout this repo. Neither function constructs a real network
# client itself, and neither ever falls back to a cached/assumed price on
# any failure -- every error propagates to the caller as an exception,
# which revalidate_market_state() converts into PRICE_FETCH_FAILED.

class PriceFetchError(Exception):
    pass


def fetch_mexc_price(exchange: Any, symbol: str) -> dict[str, Any]:
    """exchange.fetch_ticker(symbol) -- standard ccxt method, same
    exchange object .27.submit() is handed. Returns
    {"price": Decimal, "quote_observed_at": iso str}."""
    ticker = exchange.fetch_ticker(symbol)
    price = ticker.get("last")
    if price is None:
        price = ticker.get("close")
    if price is None:
        raise PriceFetchError(f"MEXC_TICKER_MISSING_PRICE:{symbol}")
    ts_ms = ticker.get("timestamp")
    if ts_ms is None:
        raise PriceFetchError(f"MEXC_TICKER_MISSING_TIMESTAMP:{symbol}")
    observed_at = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
    try:
        price_decimal = Decimal(str(price))
    except (InvalidOperation, TypeError, ValueError):
        raise PriceFetchError(f"MEXC_TICKER_INVALID_PRICE:{symbol}:{price!r}")
    if not price_decimal.is_finite() or price_decimal <= 0:
        raise PriceFetchError(f"MEXC_TICKER_INVALID_PRICE:{symbol}:{price!r}")
    return {"price": price_decimal, "quote_observed_at": observed_at.isoformat()}


def fetch_alpaca_price(data_client: Any, symbol: str) -> dict[str, Any]:
    """data_client.get_stock_latest_quote(request) -- standard
    alpaca-py market-data method. Uses the bid/ask midpoint as the
    reference "current price" -- read-only, no order touched. Imported
    lazily (not at module load time), same convention as .35, so this
    module stays importable/testable without alpaca-py's data submodule
    necessarily being configured."""
    from alpaca.data.requests import StockLatestQuoteRequest

    request = StockLatestQuoteRequest(symbol_or_symbols=symbol)
    result = data_client.get_stock_latest_quote(request)
    quote = result[symbol] if isinstance(result, dict) else result

    bid = getattr(quote, "bid_price", None)
    ask = getattr(quote, "ask_price", None)
    ts = getattr(quote, "timestamp", None)
    if bid is None or ask is None:
        raise PriceFetchError(f"ALPACA_QUOTE_MISSING_BID_OR_ASK:{symbol}")
    try:
        bid_decimal = Decimal(str(bid))
        ask_decimal = Decimal(str(ask))
    except (InvalidOperation, TypeError, ValueError):
        raise PriceFetchError(f"ALPACA_QUOTE_INVALID_BID_OR_ASK:{symbol}")
    if bid_decimal <= 0 or ask_decimal <= 0:
        raise PriceFetchError(f"ALPACA_QUOTE_INVALID_BID_OR_ASK:{symbol}")
    if ts is None:
        raise PriceFetchError(f"ALPACA_QUOTE_MISSING_TIMESTAMP:{symbol}")
    observed_at = ts if isinstance(ts, str) else ts.isoformat()
    mid = (bid_decimal + ask_decimal) / Decimal(2)
    return {"price": mid, "quote_observed_at": observed_at}


# ============================================================================
# PART 2 -- the combined revalidation gate (price half only -- TTL and
# guardrail checks remain owned by .31/.36 and are unchanged; this is the
# new check they call in addition, per design doc Section 4)
# ============================================================================

def compute_drift_bps(fresh_price: Decimal, reference_price: Decimal) -> Decimal:
    return abs(fresh_price - reference_price) / reference_price * Decimal(10000)


def revalidate_market_state(
    *,
    reference_price: Any,
    price_fetch_fn: Callable[[], dict[str, Any]],
    max_quote_age_seconds: float = DEFAULT_MAX_QUOTE_AGE_SECONDS,
    max_price_drift_bps: float = DEFAULT_MAX_PRICE_DRIFT_BPS,
    now_dt: datetime | None = None,
) -> dict[str, Any]:
    """Pure-ish check (its only side effect is whatever price_fetch_fn
    itself does -- one read-only network call): fetch a fresh price,
    check its own venue-reported freshness, compute drift against
    reference_price, and either return a PASSED result dict or raise
    RevalidationRejected. Always call this and always record its
    outcome (pass or fail) to the audit trail -- this function has no
    side effects on the audit trail itself; the caller does that,
    exactly the same separation .31/.36's existing
    revalidate_before_submission() already keeps as a pure check.

    price_fetch_fn takes no arguments -- the caller closes over
    whatever venue client + symbol are needed (e.g.
    `lambda: fetch_mexc_price(exchange, symbol)`), keeping this
    function itself venue-neutral.

    "Now", for freshness purposes, is evaluated AFTER price_fetch_fn()
    returns (unless now_dt is explicitly supplied, e.g. by a test) --
    freshness is "how old is this quote as of the moment I'm about to
    act on it," which is only knowable once the fetch has actually
    completed. Evaluating it beforehand would make age_seconds spuriously
    negative purely from call-ordering, not from any real staleness."""
    if reference_price is None:
        raise RevalidationRejected("NO_REFERENCE_PRICE_AVAILABLE")
    reference_price_decimal = _decimal_positive(reference_price, "reference_price")

    try:
        quote = price_fetch_fn()
    except RevalidationRejected:
        raise
    except Exception as exc:  # noqa: BLE001 -- classified, never silently swallowed
        raise RevalidationRejected("PRICE_FETCH_FAILED", f"{type(exc).__name__}:{exc}")

    moment = now_dt or datetime.now(timezone.utc)

    fresh_price = quote.get("price") if isinstance(quote, dict) else None
    quote_observed_at_raw = quote.get("quote_observed_at") if isinstance(quote, dict) else None
    if not isinstance(fresh_price, Decimal) or not fresh_price.is_finite() or fresh_price <= 0:
        raise RevalidationRejected("PRICE_FETCH_FAILED", "INVALID_PRICE_RETURNED")

    quote_observed_at = _parse_iso(quote_observed_at_raw)
    if quote_observed_at is None:
        raise RevalidationRejected("PRICE_FETCH_FAILED", "INVALID_QUOTE_TIMESTAMP")

    age_seconds = (moment - quote_observed_at).total_seconds()
    if age_seconds < -CLOCK_SKEW_TOLERANCE_SECONDS:
        # A quote timestamped meaningfully in the future relative to our
        # own clock is itself untrustworthy -- fail closed rather than
        # let it pass a max-age check by virtue of a negative age. A
        # small tolerance absorbs ordinary clock skew between our clock
        # and the venue's, not genuine future-dated data.
        raise RevalidationRejected("PRICE_DATA_STALE", f"FUTURE_TIMESTAMP:{age_seconds:.3f}")
    age_seconds = max(age_seconds, 0.0)
    if age_seconds > max_quote_age_seconds:
        raise RevalidationRejected("PRICE_DATA_STALE", f"AGE_SECONDS={age_seconds:.3f}")

    drift_bps = compute_drift_bps(fresh_price, reference_price_decimal)
    if drift_bps > Decimal(str(max_price_drift_bps)):
        raise RevalidationRejected(
            "PRICE_DRIFT_EXCEEDED",
            f"drift_bps={drift_bps}:fresh={fresh_price}:reference={reference_price_decimal}",
        )

    return {
        "status": "REVALIDATION_PASSED",
        "fresh_price": str(fresh_price),
        "reference_price": str(reference_price_decimal),
        "drift_bps": str(drift_bps),
        "quote_observed_at": quote_observed_at.isoformat(),
        "price_checked_at": moment.isoformat(),
        "max_quote_age_seconds": max_quote_age_seconds,
        "max_price_drift_bps": max_price_drift_bps,
    }


def check_revalidation_to_submission_ceiling(
    *,
    price_checked_at: str,
    max_seconds: float = DEFAULT_MAX_REVALIDATION_TO_SUBMISSION_SECONDS,
    now_dt: datetime | None = None,
) -> None:
    """Fail-closed backstop for the residual claim-to-submit race
    (design doc Section 5) -- call this immediately before the actual
    broker/exchange submit() call, after the claim has already been
    granted. Raises RevalidationRejected(
    'REVALIDATION_EXPIRED_BEFORE_SUBMISSION', ...) if too much time has
    elapsed since the price was checked; does NOT release or affect the
    claim itself in any way -- per the no-claim-release invariant, the
    claim stays consumed regardless of what this check decides. Returns
    None (no news is good news) on success."""
    moment = now_dt or datetime.now(timezone.utc)
    checked_at = _parse_iso(price_checked_at)
    if checked_at is None:
        raise RevalidationRejected(
            "REVALIDATION_EXPIRED_BEFORE_SUBMISSION", "UNPARSEABLE_PRICE_CHECKED_AT"
        )
    elapsed = (moment - checked_at).total_seconds()
    if elapsed > max_seconds:
        raise RevalidationRejected(
            "REVALIDATION_EXPIRED_BEFORE_SUBMISSION", f"elapsed_seconds={elapsed:.3f}"
        )


# ============================================================================
# PART 3 -- unified execution audit trail (event-sourced, hash-verified,
# venue-neutral). Storage/locking/hashing pattern copied from v0.5.3.29/
# .39, not reinvented.
# ============================================================================

def _record_path(client_order_id: str, base_dir: Path | None = None) -> Path:
    base = base_dir or DEFAULT_AUDIT_BASE_DIR
    return Path(base) / f"{client_order_id}.json"


def _lock_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".lock")


class _FileLock:
    """Advisory O_CREAT|O_EXCL lock. Creates the lock file's parent
    directory FIRST, before attempting to acquire the lock -- this is
    the exact fix v0.5.3.39 had to discover the hard way, via a manual
    fresh-directory CLI smoke test that pytest's tmp_path fixture
    masked (tmp_path always pre-creates its directory). Baked in here
    from the start rather than rediscovered."""

    def __init__(self, path: Path, timeout_seconds: float = 5.0):
        self._lock_path = _lock_path(path)
        self._timeout = timeout_seconds
        self._fd: int | None = None

    def __enter__(self) -> "_FileLock":
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self._timeout
        while True:
            try:
                self._fd = os.open(str(self._lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise AuditTrailError(f"LOCK_TIMEOUT:{self._lock_path}")
                time.sleep(0.02)

    def __exit__(self, *exc_info: Any) -> None:
        if self._fd is not None:
            os.close(self._fd)
        try:
            os.unlink(self._lock_path)
        except FileNotFoundError:
            pass


def _write_record_atomic(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, ensure_ascii=False, allow_nan=False)
        f.write("\n")
    os.replace(tmp_path, path)


def _load_record_raw(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise AuditTrailError(f"AUDIT_RECORD_NOT_FOUND:{path.stem}")
    with path.open("r", encoding="utf-8") as f:
        record = json.load(f)
    if not isinstance(record, dict):
        raise AuditTrailError(f"AUDIT_RECORD_NOT_OBJECT:{path.stem}")
    return record


def _canonical_record_hash_fields(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": record.get("schema_version"),
        "engine": record.get("engine"),
        "agent_version": record.get("agent_version"),
        "client_order_id": record.get("client_order_id"),
        "venue": record.get("venue"),
        "asset_class": record.get("asset_class"),
        "events": record.get("events"),
    }


def canonical_record_hash(record: dict[str, Any]) -> str:
    return sha256_text(stable_json(_canonical_record_hash_fields(record)))


def _after_revalidated(fields: dict[str, Any]) -> str:
    return "REVALIDATED" if fields.get("passed") else "REVALIDATION_FAILED"


def derive_state(events: list[dict[str, Any]]) -> str:
    """Pure function: replay events from scratch. Legal sequence:
    DECISION -> (AUTHORIZED)? -> REVALIDATED -> CONSUMED ->
    (SUBMISSION_ATTEMPTED -> OUTCOME)? -- AUTHORIZED only appears on a
    real authorize() success (design doc Section 2.2: a rejected
    authorization was never real, so it is never recorded as an event
    at all -- there is no AUTHORIZATION_REJECTED event type). A failed
    REVALIDATED or a failed CONSUMED is terminal. Never trusts a stored
    current_state -- this IS the definition of current_state, same
    discipline as v0.5.3.29/.39."""
    state = "UNCREATED"
    for ev in events:
        etype = ev.get("event")
        fields = ev.get("fields", {}) or {}

        if state == "UNCREATED":
            if etype != "DECISION":
                raise IllegalTransitionError(f"FIRST_EVENT_NOT_DECISION:{etype}")
            state = "DECISION"
            continue

        if state == "DECISION":
            if etype == "AUTHORIZED":
                state = "AUTHORIZED"
                continue
            if etype == "REVALIDATED":
                state = _after_revalidated(fields)
                continue
            raise IllegalTransitionError(f"ILLEGAL_FROM_DECISION:{etype}")

        if state == "AUTHORIZED":
            if etype != "REVALIDATED":
                raise IllegalTransitionError(f"ILLEGAL_FROM_AUTHORIZED:{etype}")
            state = _after_revalidated(fields)
            continue

        if state == "REVALIDATED":
            if etype != "CONSUMED":
                raise IllegalTransitionError(f"ILLEGAL_FROM_REVALIDATED:{etype}")
            state = "CONSUMED" if fields.get("granted") else "CONSUMPTION_FAILED"
            continue

        if state == "CONSUMED":
            if etype != "SUBMISSION_ATTEMPTED":
                raise IllegalTransitionError(f"ILLEGAL_FROM_CONSUMED:{etype}")
            state = "SUBMISSION_ATTEMPTED"
            continue

        if state == "SUBMISSION_ATTEMPTED":
            if etype != "OUTCOME":
                raise IllegalTransitionError(f"ILLEGAL_FROM_SUBMISSION_ATTEMPTED:{etype}")
            state = f"OUTCOME:{fields.get('outcome')}"
            continue

        if state.startswith("OUTCOME:"):
            # The only event legal after OUTCOME:* is a single RESOLVED
            # (v0.5.3.45) -- everything else about this state remains
            # exactly as terminal as before this change.
            if etype == "RESOLVED":
                state = f"RESOLVED:{fields.get('resolution')}"
                continue
            raise IllegalTransitionError(f"EVENT_AFTER_TERMINAL:{state}:{etype}")

        # REVALIDATION_FAILED, CONSUMPTION_FAILED, or any RESOLVED:* state
        # -- all terminal, nothing legal follows.
        raise IllegalTransitionError(f"EVENT_AFTER_TERMINAL:{state}:{etype}")

    return state


def verify_record(record: dict[str, Any]) -> tuple[bool, list[str]]:
    """Recomputes record_hash and checks the event sequence is a legal
    path through the state machine, same two-part discipline as
    v0.5.3.29's verify_intent() / v0.5.3.39's verify_record()."""
    errors: list[str] = []
    if record.get("engine") != ENGINE:
        errors.append("WRONG_ENGINE")
    if record.get("schema_version") != SCHEMA_VERSION:
        errors.append("WRONG_SCHEMA_VERSION")

    supplied_hash = record.get("record_hash")
    if not isinstance(supplied_hash, str) or not supplied_hash:
        errors.append("MISSING_RECORD_HASH")
    else:
        try:
            calculated = canonical_record_hash(record)
        except (TypeError, ValueError):
            errors.append("RECORD_NOT_HASHABLE")
        else:
            if calculated != supplied_hash:
                errors.append("RECORD_HASH_MISMATCH")

    events = record.get("events")
    if not isinstance(events, list) or not events:
        errors.append("MISSING_OR_EMPTY_EVENTS")
        return False, errors

    try:
        derived_state = derive_state(events)
    except IllegalTransitionError as exc:
        errors.append(f"ILLEGAL_EVENT_SEQUENCE:{exc}")
        return False, errors

    if record.get("current_state") != derived_state:
        errors.append(
            f"CURRENT_STATE_DRIFT:stored={record.get('current_state')!r}:derived={derived_state!r}"
        )

    return len(errors) == 0, errors


def _finalize(record: dict[str, Any]) -> dict[str, Any]:
    record["current_state"] = derive_state(record["events"])
    record["updated_at"] = now()
    record["record_hash"] = canonical_record_hash(record)
    return record


def get_record(client_order_id: str, base_dir: Path | None = None) -> dict[str, Any] | None:
    """Returns None if no record exists yet (a genuine absence, not an
    error -- matching .29.get_intent()'s convention); raises
    AuditTrailError if a record exists but fails verify_record() (fail
    closed on corruption, never return a partially-trusted record)."""
    path = _record_path(client_order_id, base_dir)
    if not path.exists():
        return None
    record = _load_record_raw(path)
    ok, errors = verify_record(record)
    if not ok:
        raise AuditTrailError(f"AUDIT_RECORD_VERIFICATION_FAILED:{client_order_id}:{','.join(errors)}")
    return record


def record_decision(
    client_order_id: str,
    *,
    venue: str,
    asset_class: str,
    symbol: str,
    spec_fingerprint: str,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    """Creates the record -- a hard refusal on an existing
    client_order_id (AUDIT_RECORD_ALREADY_EXISTS), same discipline as
    v0.5.3.29.create_intent(): a caller distinguishing its own
    crash-retry from a genuine collision does so by catching this and
    calling get_record() itself, exactly the pattern v0.5.3.38 already
    uses for v0.5.3.29."""
    if venue not in VENUES:
        raise AuditTrailError(f"INVALID_VENUE:{venue!r}")
    if asset_class not in ASSET_CLASSES:
        raise AuditTrailError(f"INVALID_ASSET_CLASS:{asset_class!r}")
    if not isinstance(symbol, str) or not symbol.strip():
        raise AuditTrailError("MISSING_SYMBOL")
    if not isinstance(spec_fingerprint, str) or not spec_fingerprint.strip():
        raise AuditTrailError("MISSING_SPEC_FINGERPRINT")

    path = _record_path(client_order_id, base_dir)
    if path.exists():
        raise AuditTrailError(f"AUDIT_RECORD_ALREADY_EXISTS:{client_order_id}")

    with _FileLock(path):
        if path.exists():
            raise AuditTrailError(f"AUDIT_RECORD_ALREADY_EXISTS:{client_order_id}")
        created_at = now()
        record: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "engine": ENGINE,
            "agent_version": VERSION,
            "client_order_id": client_order_id,
            "venue": venue,
            "asset_class": asset_class,
            "symbol": symbol,
            "created_at": created_at,
            "events": [
                {
                    "event": "DECISION",
                    "recorded_at": created_at,
                    "fields": {"spec_fingerprint": spec_fingerprint},
                }
            ],
        }
        record = _finalize(record)
        _write_record_atomic(path, record)
        return record


def _append_event(
    client_order_id: str, event_type: str, fields: dict[str, Any], base_dir: Path | None = None
) -> dict[str, Any]:
    if event_type not in EVENT_TYPES:
        raise AuditTrailError(f"INVALID_EVENT_TYPE:{event_type!r}")
    path = _record_path(client_order_id, base_dir)
    with _FileLock(path):
        record = _load_record_raw(path)
        ok, errors = verify_record(record)
        if not ok:
            raise AuditTrailError(
                f"AUDIT_RECORD_VERIFICATION_FAILED:{client_order_id}:{','.join(errors)}"
            )
        record["events"].append(
            {"event": event_type, "recorded_at": now(), "fields": fields}
        )
        # derive_state() (inside _finalize -> canonical hash path is
        # fine, but the legality check itself must happen explicitly
        # here so an illegal append never gets written to disk at all.
        derive_state(record["events"])
        record = _finalize(record)
        _write_record_atomic(path, record)
        return record


def record_authorized(client_order_id: str, base_dir: Path | None = None) -> dict[str, Any]:
    """Only ever called on a genuine AUTHORIZED outcome from .31/.36's
    authorize() -- a rejected authorization is never recorded (design
    doc Section 2.2: it was never real)."""
    return _append_event(client_order_id, "AUTHORIZED", {}, base_dir)


def record_revalidated(
    client_order_id: str,
    *,
    passed: bool,
    reason: str | None = None,
    detail: Any = None,
    price_result: dict[str, Any] | None = None,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    """Always called, pass or fail -- this is the one audit event every
    execution attempt that reaches revalidation will have, regardless
    of outcome."""
    fields: dict[str, Any] = {"passed": bool(passed)}
    if not passed:
        fields["reason"] = reason
        fields["detail"] = str(detail) if detail is not None else None
    if price_result:
        fields.update(price_result)
    return _append_event(client_order_id, "REVALIDATED", fields, base_dir)


def record_consumed(
    client_order_id: str, *, granted: bool, reason: str | None = None, base_dir: Path | None = None
) -> dict[str, Any]:
    """granted=False covers both ALREADY_CLAIMED (lost the race) and
    any other claim-layer refusal -- always written, win or lose, so
    the audit trail can distinguish 'never attempted the claim' from
    'attempted and lost.'"""
    fields: dict[str, Any] = {"granted": bool(granted)}
    if not granted:
        fields["reason"] = reason
    return _append_event(client_order_id, "CONSUMED", fields, base_dir)


def record_submission_attempted(client_order_id: str, base_dir: Path | None = None) -> dict[str, Any]:
    return _append_event(client_order_id, "SUBMISSION_ATTEMPTED", {}, base_dir)


def record_outcome(
    client_order_id: str,
    *,
    outcome: str,
    detail: Any = None,
    mexc_intent_record_hash: str | None = None,
    mexc_intent_current_state: str | None = None,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    """For MEXC: pass mexc_intent_record_hash/mexc_intent_current_state
    so this event is a REFERENCE into v0.5.3.29's own record rather than
    an independently-derived opinion about what happened (design doc
    Section 2.2) -- v0.5.3.29/.30 remain sole authority for MEXC fill/
    reconciliation truth. For Alpaca, leave both None -- this event IS
    the authoritative record there (no v0.5.3.29-equivalent exists).

    An outcome of EXECUTION_UNCERTAIN recorded here has NO automated
    resolution path for Alpaca (Martin's explicit instruction #3: the
    missing Alpaca reconciliation spine is out of scope for v0.5.3.40).
    Nothing in this module treats it as self-resolving -- no retry, no
    poll, no inferred outcome. This event's only job is to make the gap
    queryable ("an OUTCOME:EXECUTION_UNCERTAIN or a CONSUMED with no
    following SUBMISSION_ATTEMPTED/OUTCOME within N minutes"), not to
    close it."""
    fields: dict[str, Any] = {"outcome": outcome, "detail": str(detail) if detail is not None else None}
    if mexc_intent_record_hash is not None:
        fields["mexc_intent_record_hash"] = mexc_intent_record_hash
    if mexc_intent_current_state is not None:
        fields["mexc_intent_current_state"] = mexc_intent_current_state
    return _append_event(client_order_id, "OUTCOME", fields, base_dir)


RESOLUTIONS = frozenset(
    {"RESOLVED_FILLED", "RESOLVED_PARTIALLY_FILLED", "RESOLVED_CANCELED",
     "RESOLVED_REJECTED", "ESCALATED_HUMAN_REVIEW"}
)


def record_resolved(
    client_order_id: str,
    *,
    resolution: str,
    detail: Any = None,
    proven_failed: bool | None = None,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    """v0.5.3.45 (Alpaca EXECUTION_UNCERTAIN resolution authority). Only
    ever called on an ALPACA record -- MEXC's own .29/.30 remain sole
    authority for MEXC fill/reconciliation truth (module docstring,
    PART 3 preamble); calling this on a MEXC record would create exactly
    the second, independently-derived opinion this module's design has
    always refused to produce, so it is refused here, hard, rather than
    silently allowed.

    `resolution` must be one of RESOLUTIONS -- this module invents
    nothing: it is either one of the four definitive terminal fills/
    cancellations/rejections .45's evidence matrix can prove, or
    ESCALATED_HUMAN_REVIEW when the evidence cannot be resolved
    automatically. There is no RESOLVED_STILL_PENDING -- a still-pending
    outcome writes nothing here at all (the record simply stays at
    OUTCOME:* until a later resolution pass has something new to say),
    matching this module's "only write what actually changed" audit
    convention elsewhere.

    `proven_failed`, when not None, is evidence-only metadata (did this
    resolution prove the order never executed at all) -- this module
    takes no action on it. It exists so a value that is genuinely known
    is not thrown away, per the same disclosure discipline as every
    other field in this trail; a future, separately-approved capability
    may read it, but it grants no retry/resubmission authority here."""
    if resolution not in RESOLUTIONS:
        raise AuditTrailError(f"INVALID_RESOLUTION:{resolution!r}")

    path = _record_path(client_order_id, base_dir)
    with _FileLock(path):
        record = _load_record_raw(path)
        ok, errors = verify_record(record)
        if not ok:
            raise AuditTrailError(
                f"AUDIT_RECORD_VERIFICATION_FAILED:{client_order_id}:{','.join(errors)}"
            )
        if record.get("venue") != "ALPACA":
            raise AuditTrailError(
                f"RESOLVED_EVENT_NOT_PERMITTED_FOR_VENUE:{record.get('venue')!r}:"
                "MEXC records are resolved solely by .29/.30, never by this event"
            )
        fields: dict[str, Any] = {"resolution": resolution}
        if detail is not None:
            fields["detail"] = str(detail)
        if proven_failed is not None:
            fields["proven_failed"] = bool(proven_failed)
        record["events"].append({"event": "RESOLVED", "recorded_at": now(), "fields": fields})
        derive_state(record["events"])
        record = _finalize(record)
        _write_record_atomic(path, record)
        return record


# --------------------------------------------------------------------- #
# CLI -- for manual inspection/smoke-testing, mirroring every other AURA
# module's convention. Not the primary integration path (that's .31/
# .36/.38 calling the functions above directly).
# --------------------------------------------------------------------- #

def _cli_show(args: argparse.Namespace) -> int:
    record = get_record(args.client_order_id, base_dir=args.audit_dir)
    if record is None:
        print(f"NO AUDIT RECORD FOR {args.client_order_id}")
        return 1
    print(json.dumps(record, indent=2))
    return 0


def _cli_decision(args: argparse.Namespace) -> int:
    record = record_decision(
        args.client_order_id,
        venue=args.venue,
        asset_class=args.asset_class,
        symbol=args.symbol,
        spec_fingerprint=args.spec_fingerprint,
        base_dir=args.audit_dir,
    )
    print(f"current_state       : {record['current_state']}")
    print(f"record_hash         : {record['record_hash']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-dir", type=Path, default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    p_show = sub.add_parser("show")
    p_show.add_argument("--client-order-id", required=True)

    p_decision = sub.add_parser("decision")
    p_decision.add_argument("--client-order-id", required=True)
    p_decision.add_argument("--venue", required=True, choices=sorted(VENUES))
    p_decision.add_argument("--asset-class", required=True, choices=sorted(ASSET_CLASSES))
    p_decision.add_argument("--symbol", required=True)
    p_decision.add_argument("--spec-fingerprint", required=True)

    args = parser.parse_args()
    if args.command == "show":
        return _cli_show(args)
    if args.command == "decision":
        return _cli_decision(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
