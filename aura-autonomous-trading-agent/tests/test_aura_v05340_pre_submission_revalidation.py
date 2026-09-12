#!/usr/bin/env python3
"""Unit + end-to-end tests for AURA v0.5.3.40 (Pre-Submission Revalidation +
Unified Execution Audit Trail), covering Martin's explicit .40 test list:

  1. reference_price provenance proven end-to-end (the "most important
     subtlety" -- decision price -> canonical .33 reference_price ->
     authorization -> fresh venue price -> drift calculation -> claim ->
     submission -> audit trail), for BOTH venues, through the REAL .38
     orchestration -- not just checking the field exists.
  2. stale quote fails closed.
  3. price drift fails closed.
  4. missing reference_price fails closed (the fail-closed NO_REFERENCE_
     PRICE_AVAILABLE behavior Martin explicitly required be preserved).
  5. revalidation passes -> claim -> submit, full audit trail chain
     DECISION -> AUTHORIZED -> REVALIDATED -> CONSUMED -> SUBMISSION_
     ATTEMPTED -> OUTCOME, both venues.
  6. claim remains consumed when the revalidation-to-submission ceiling
     expires.
  7. concurrent claim: exactly one winner, with the audit trail wired.
  8. no orphan .29 NEW intent after a pre-claim revalidation rejection --
     also the regression test for a genuine bug found and fixed during
     this same verification pass (see test_mexc_pre_claim_rejection_
     leaves_intent_at_new_and_retryable's docstring).
  9. existing .29 historical state machine remains fully compatible --
     a .40-wired MEXC submission produces the identical .29 ledger shape
     (AWAITING_RECONCILIATION) as .38's own pre-.40 test suite.

Runs everything through the REAL v0.5.3.38 orchestration
(supervise_mexc_futures_execution / supervise_alpaca_equity_execution),
not the .40 module in isolation, plus a smaller set of direct unit tests
against .40's own revalidate_market_state()/check_revalidation_to_
submission_ceiling()/audit-trail primitives for fast, precise coverage of
each individual failure mode.

No real credentials, no network call, anywhere in this file -- every
price_fetch_fn is a local fake closure, every exchange/client is an
injected fake, matching the convention already established by every
other test file in this chain.

============================================================================
RESOLVED ISSUE (found and fixed during this verification pass -- see the
.40 completion report): .31.authorize()/.36.authorize() used to call
.40's record_authorized() UNCONDITIONALLY whenever audit_dir was
supplied, with no handling for the case where a client_order_id's audit
record was already past the point where an AUTHORIZED event is legal to
append (derive_state()'s single-shot DECISION -> (AUTHORIZED)? ->
REVALIDATED -> CONSUMED -> ... state machine treats a failed REVALIDATED,
or any state at or beyond CONSUMED, as terminal). This meant a genuine
retry of a decision whose first attempt's audit trail already reached a
terminal state, or two concurrent authorize() calls racing on the SAME
client_order_id, raised an UNHANDLED IllegalTransitionError instead of
failing closed cleanly. Found via test_mexc_pre_claim_rejection_leaves_
intent_at_new_and_retryable's retry step and test_mexc_concurrent_
attempts_with_audit_trail_exactly_one_wins's race. Reported to Martin
(several designs were reasonable -- not guessed at); resolved per his
explicit decision with best-effort audit writes:
_audit_write_best_effort() in .31/.36/.38 swallows ONLY
IllegalTransitionError (never AuditTrailError -- corruption/tamper
detection still propagates) at every .40 record_*() call site. Both
tests above prove the fix, not just document the finding.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import aura_v05329_mexc_intent_ledger as LEDGER
import aura_v05333_canonical_execution_specification as CANON
import aura_v05338_common_execution_supervisor as SUP
import aura_v05340_pre_submission_revalidation as V40

SIGNAL_TS = "2026-09-12T12:00:00+00:00"


def expect(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"PASS: {name}")


# ======================================================================= #
# Fixtures / builders
# ======================================================================= #

def make_mexc_spec(*, direction="OPEN_LONG", symbol="BTC/USDT:USDT", quantity="0.01", leverage=3,
                    decision_id="DEC-M1", strategy_id="STRAT-M", strategy_version="v1.0",
                    signal_timestamp=SIGNAL_TS, reference_price=None) -> dict:
    kwargs = dict(
        asset_class="CRYPTO_FUTURES", venue="MEXC", direction=direction, symbol=symbol,
        quantity=quantity, decision_id=decision_id, strategy_id=strategy_id,
        strategy_version=strategy_version, signal_timestamp=signal_timestamp,
        order_type="MARKET", source_kind="DETERMINISTIC_SIGNAL",
    )
    if direction in ("OPEN_LONG", "OPEN_SHORT"):
        kwargs["leverage"] = leverage
    if reference_price is not None:
        kwargs["reference_price"] = reference_price
    return CANON.build_canonical_execution_specification(**kwargs)


def make_alpaca_spec(*, asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL", quantity="10",
                      decision_id="DEC-A1", strategy_id="STRAT-A", strategy_version="v1.0",
                      signal_timestamp=SIGNAL_TS, reference_price=None) -> dict:
    kwargs = dict(
        asset_class=asset_class, venue="ALPACA", direction=direction, symbol=symbol,
        quantity=quantity, decision_id=decision_id, strategy_id=strategy_id,
        strategy_version=strategy_version, signal_timestamp=signal_timestamp,
        order_type="MARKET", source_kind="DETERMINISTIC_SIGNAL",
    )
    if reference_price is not None:
        kwargs["reference_price"] = reference_price
    return CANON.build_canonical_execution_specification(**kwargs)


def make_alpaca_asset(*, symbol="AAPL", shortable=True, easy_to_borrow=True) -> dict:
    return {
        "symbol": symbol, "asset_class": "us_equity", "exchange": "NASDAQ", "status": "active",
        "tradable": True, "shortable": shortable, "easy_to_borrow": easy_to_borrow,
        "fractionable": True, "marginable": True,
        "min_order_size": None, "min_trade_increment": None, "price_increment": None,
    }


def mexc_auth_config(**overrides) -> dict:
    config = {
        "kill_switch": False, "execution_authorized": True, "paper_execution_authorized": True,
        "live_execution_authorized": True, "authorization_ttl_seconds": 60, "safety_state_ttl_seconds": 60,
    }
    config.update(overrides)
    return config


def alpaca_auth_config(**overrides) -> dict:
    config = {
        "kill_switch": False, "execution_authorized": True, "paper_execution_authorized": True,
        "authorization_ttl_seconds": 60, "safety_state_ttl_seconds": 60,
    }
    config.update(overrides)
    return config


def sup_open(**overrides) -> dict:
    config = {"kill_switch": False}
    config.update(overrides)
    return config


class Dirs:
    def __init__(self, tmp_path: Path):
        self.mexc_auth = tmp_path / "mexc-auth-claims"
        self.mexc_adapter = tmp_path / "mexc-adapter-claims"
        self.mexc_ledger = tmp_path / "mexc-ledger"
        self.alpaca_auth = tmp_path / "alpaca-auth-claims"
        self.alpaca_supervisor = tmp_path / "alpaca-supervisor-claims"
        self.audit = tmp_path / "audit-trail"

    def mexc_kwargs(self) -> dict:
        return dict(auth_claims_dir=self.mexc_auth, adapter_claims_dir=self.mexc_adapter,
                    ledger_base_dir=self.mexc_ledger)

    def alpaca_kwargs(self) -> dict:
        return dict(auth_claims_dir=self.alpaca_auth, supervisor_claims_dir=self.alpaca_supervisor)


class FakeSubmissionExchange:
    def __init__(self, create_order_result=None, create_order_exception=None):
        self.create_order_calls: list[tuple] = []
        self.set_leverage_calls: list[tuple] = []
        self._create_order_result = create_order_result
        self._create_order_exception = create_order_exception

    def set_leverage(self, leverage, symbol):
        self.set_leverage_calls.append((leverage, symbol))

    def create_order(self, symbol, type, side, amount, params=None):
        self.create_order_calls.append((symbol, type, side, amount, params))
        if self._create_order_exception:
            raise self._create_order_exception
        return self._create_order_result or {"id": "mexc-fake-order-1", "status": "closed"}


class FakeOrder:
    def __init__(self, order_id, status):
        self.id = order_id
        self.status = status


class FakeAlpacaClient:
    def __init__(self, existing_order=None, not_found_exc=Exception):
        self._existing = existing_order
        self._not_found_exc = not_found_exc
        self.submit_calls: list = []

    def get_order_by_client_id(self, client_order_id):
        if self._existing is None:
            raise self._not_found_exc("order not found")
        return self._existing

    def submit_order(self, order_data):
        self.submit_calls.append(order_data)
        return FakeOrder(order_id="broker-123", status="new")


def fixed_price_fetch_fn(price: str, quote_observed_at: str | None = None, calls: list | None = None):
    """A price_fetch_fn closure returning a FIXED price with a FRESH
    (or explicitly overridden) quote timestamp -- the standard fake used
    throughout this file in place of a real ccxt/alpaca-data client."""
    def _fetch():
        if calls is not None:
            calls.append(True)
        observed = quote_observed_at or datetime.now(timezone.utc).isoformat()
        return {"price": Decimal(price), "quote_observed_at": observed}
    return _fetch


# ======================================================================= #
# 1. Direct unit tests -- revalidate_market_state() failure modes
# ======================================================================= #

def test_unit_missing_reference_price_fails_closed() -> None:
    try:
        V40.revalidate_market_state(reference_price=None, price_fetch_fn=fixed_price_fetch_fn("50000"))
        raise AssertionError("expected RevalidationRejected")
    except V40.RevalidationRejected as exc:
        expect("1: missing reference_price fails closed with NO_REFERENCE_PRICE_AVAILABLE",
               exc.reason == "NO_REFERENCE_PRICE_AVAILABLE")


def test_unit_invalid_reference_price_fails_closed() -> None:
    for bad in ("not-a-number", "-5", "0"):
        try:
            V40.revalidate_market_state(reference_price=bad, price_fetch_fn=fixed_price_fetch_fn("50000"))
            raise AssertionError(f"expected RevalidationRejected for {bad!r}")
        except V40.RevalidationRejected as exc:
            expect(f"2: invalid reference_price {bad!r} fails closed with INVALID_REFERENCE_PRICE",
                   exc.reason == "INVALID_REFERENCE_PRICE")


def test_unit_stale_quote_fails_closed() -> None:
    stale_ts = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    try:
        V40.revalidate_market_state(
            reference_price="50000", price_fetch_fn=fixed_price_fetch_fn("50010", quote_observed_at=stale_ts),
            max_quote_age_seconds=10.0,
        )
        raise AssertionError("expected RevalidationRejected")
    except V40.RevalidationRejected as exc:
        expect("3: a 30s-old quote against a 10s max age fails closed with PRICE_DATA_STALE",
               exc.reason == "PRICE_DATA_STALE")


def test_unit_price_drift_fails_closed() -> None:
    # reference 50000, fresh 51000 -> drift = 1000/50000 * 10000 = 200 bps
    try:
        V40.revalidate_market_state(
            reference_price="50000", price_fetch_fn=fixed_price_fetch_fn("51000"),
            max_price_drift_bps=50.0,
        )
        raise AssertionError("expected RevalidationRejected")
    except V40.RevalidationRejected as exc:
        expect("4: 200bps drift against a 50bps max fails closed with PRICE_DRIFT_EXCEEDED",
               exc.reason == "PRICE_DRIFT_EXCEEDED")
        expect("4: detail names the actual computed drift", "drift_bps=200" in exc.detail)


def test_unit_price_fetch_exception_fails_closed() -> None:
    def failing_fetch():
        raise ConnectionError("ticker timeout")
    try:
        V40.revalidate_market_state(reference_price="50000", price_fetch_fn=failing_fetch)
        raise AssertionError("expected RevalidationRejected")
    except V40.RevalidationRejected as exc:
        expect("5: a price_fetch_fn exception is classified, never silently swallowed",
               exc.reason == "PRICE_FETCH_FAILED" and "ConnectionError" in str(exc.detail))


def test_unit_passing_revalidation_returns_expected_fields() -> None:
    result = V40.revalidate_market_state(
        reference_price="50000", price_fetch_fn=fixed_price_fetch_fn("50010"),
        max_price_drift_bps=50.0,
    )
    expect("6: PASSED status", result["status"] == "REVALIDATION_PASSED")
    expect("6: reference_price echoed exactly", result["reference_price"] == "50000")
    expect("6: fresh_price echoed exactly", result["fresh_price"] == "50010")
    expect("6: drift_bps computed correctly (10/50000*10000=2)", Decimal(result["drift_bps"]) == Decimal("2"))


# ======================================================================= #
# 2. Direct unit tests -- check_revalidation_to_submission_ceiling()
# ======================================================================= #

def test_unit_ceiling_passes_within_budget() -> None:
    checked = datetime(2026, 9, 12, 12, 0, 0, tzinfo=timezone.utc)
    later = checked + timedelta(seconds=3)
    V40.check_revalidation_to_submission_ceiling(
        price_checked_at=checked.isoformat(), max_seconds=5.0, now_dt=later,
    )  # must not raise
    print("PASS: 7: ceiling check passes when elapsed (3s) is within the 5s budget")


def test_unit_ceiling_fires_when_elapsed_exceeds_budget() -> None:
    checked = datetime(2026, 9, 12, 12, 0, 0, tzinfo=timezone.utc)
    later = checked + timedelta(seconds=8)
    try:
        V40.check_revalidation_to_submission_ceiling(
            price_checked_at=checked.isoformat(), max_seconds=5.0, now_dt=later,
        )
        raise AssertionError("expected RevalidationRejected")
    except V40.RevalidationRejected as exc:
        expect("8: an 8s gap against a 5s budget fails closed with REVALIDATION_EXPIRED_BEFORE_SUBMISSION",
               exc.reason == "REVALIDATION_EXPIRED_BEFORE_SUBMISSION")


# ======================================================================= #
# 3. Direct unit tests -- audit trail primitives
# ======================================================================= #

def test_unit_record_decision_is_hard_refused_on_duplicate(tmp_path) -> None:
    V40.record_decision("COID-DUP", venue="MEXC", asset_class="CRYPTO_FUTURES", symbol="BTC/USDT:USDT",
                         spec_fingerprint="fp-1", base_dir=tmp_path)
    try:
        V40.record_decision("COID-DUP", venue="MEXC", asset_class="CRYPTO_FUTURES", symbol="BTC/USDT:USDT",
                             spec_fingerprint="fp-1", base_dir=tmp_path)
        raise AssertionError("expected AuditTrailError")
    except V40.AuditTrailError as exc:
        expect("9: a second record_decision() for the same client_order_id is hard-refused",
               "AUDIT_RECORD_ALREADY_EXISTS" in str(exc))


def test_unit_illegal_event_sequence_rejected(tmp_path) -> None:
    V40.record_decision("COID-SEQ", venue="MEXC", asset_class="CRYPTO_FUTURES", symbol="BTC/USDT:USDT",
                         spec_fingerprint="fp-1", base_dir=tmp_path)
    try:
        # CONSUMED before REVALIDATED is illegal. derive_state() raises
        # IllegalTransitionError directly -- _append_event() does not
        # wrap it in AuditTrailError, so that is what a caller must catch.
        V40.record_consumed("COID-SEQ", granted=True, base_dir=tmp_path)
        raise AssertionError("expected IllegalTransitionError")
    except V40.IllegalTransitionError as exc:
        expect("10: CONSUMED before REVALIDATED is rejected as an illegal transition",
               "ILLEGAL_FROM_DECISION" in str(exc))


def test_unit_tampered_record_hash_detected(tmp_path) -> None:
    V40.record_decision("COID-TAMPER", venue="MEXC", asset_class="CRYPTO_FUTURES", symbol="BTC/USDT:USDT",
                         spec_fingerprint="fp-1", base_dir=tmp_path)
    path = V40._record_path("COID-TAMPER", tmp_path)
    import json
    raw = json.loads(path.read_text())
    raw["events"][0]["fields"]["spec_fingerprint"] = "fp-TAMPERED"
    path.write_text(json.dumps(raw))
    try:
        V40.get_record("COID-TAMPER", base_dir=tmp_path)
        raise AssertionError("expected AuditTrailError")
    except V40.AuditTrailError as exc:
        expect("11: a tampered record fails its own hash self-verification",
               "VERIFICATION_FAILED" in str(exc))


# ======================================================================= #
# 4. End-to-end reference_price provenance -- the "most important
#    subtlety" (Martin's own words). Real .38 orchestration, both venues.
# ======================================================================= #

def test_e2e_mexc_reference_price_provenance_full_chain(tmp_path) -> None:
    """Proves the full chain end-to-end, not just that the field exists:
    decision price (what a caller supplies as reference_price at spec-
    build time, simulating .25-equivalent decision/sizing output) ->
    canonical .33 reference_price (echoed unchanged on the built spec) ->
    .31.authorize() (record issued against that spec) -> a genuinely
    DIFFERENT fresh venue price (from the fake MEXC ticker) -> drift_bps
    computed from exactly those two numbers -> claim only after
    revalidation passes -> submission -> the persisted .40 audit record
    on disk carries the exact reference_price/fresh_price/drift_bps that
    were actually used, tying the whole chain together."""
    d = Dirs(tmp_path)
    decision_price = "50000.00"
    fresh_venue_price = "50015.00"  # drift = 15/50000*10000 = 3 bps -- well within default 50bps
    spec = make_mexc_spec(decision_id="DEC-PROVENANCE-M", reference_price=decision_price)

    expect("12a: the canonical spec echoes reference_price exactly as the caller supplied it",
           spec["reference_price"] == decision_price)

    fetch_calls: list = []
    fake_exchange = FakeSubmissionExchange(create_order_result={"id": "mexc-prov-1", "status": "open"})
    r = SUP.supervise_mexc_futures_execution(
        spec, auth_config=mexc_auth_config(), attempt_submission=True, exchange=fake_exchange,
        supervisor_config=sup_open(), audit_dir=d.audit,
        price_fetch_fn=fixed_price_fetch_fn(fresh_venue_price, calls=fetch_calls),
        **d.mexc_kwargs(),
    )
    expect("12b: the fresh-price fetch function was actually called (not bypassed)", len(fetch_calls) == 1)
    expect("12c: submission SUBMITTED", r["status"] == "SUBMITTED")

    record = V40.get_record(spec["client_order_id"], base_dir=d.audit)
    expect("12d: audit record exists", record is not None)
    events_by_type = {}
    for ev in record["events"]:
        events_by_type.setdefault(ev["event"], []).append(ev)
    expect("12e: full chain present: DECISION, AUTHORIZED, REVALIDATED, CONSUMED, SUBMISSION_ATTEMPTED, OUTCOME",
           set(events_by_type) == {"DECISION", "AUTHORIZED", "REVALIDATED", "CONSUMED",
                                    "SUBMISSION_ATTEMPTED", "OUTCOME"})
    revalidated = events_by_type["REVALIDATED"][0]["fields"]
    expect("12f: the audit trail's recorded reference_price matches the ORIGINAL decision price exactly",
           revalidated["reference_price"] == decision_price)
    expect("12g: the audit trail's recorded fresh_price matches the FAKE VENUE'S price exactly",
           revalidated["fresh_price"] == fresh_venue_price)
    expected_drift = (Decimal(fresh_venue_price) - Decimal(decision_price)).copy_abs() / Decimal(decision_price) * Decimal(10000)
    expect("12h: drift_bps in the audit trail matches the independently-computed value",
           Decimal(revalidated["drift_bps"]) == expected_drift)
    expect("12i: CONSUMED only happened after REVALIDATED (state machine order, not just presence)",
           record["current_state"].startswith("OUTCOME:"))
    # .31.authorized_submit() records the OUTCOME event using .27's OWN
    # raw adapter status string ("SUBMITTED"), not .29's separately-
    # mapped ledger outcome vocabulary ("SUBMISSION_ACKNOWLEDGED") -- the
    # .40 audit trail and the .29 ledger deliberately use two different,
    # independently-defined outcome vocabularies for the same event.
    expect("12j: OUTCOME event's outcome matches .27's own raw adapter status",
           events_by_type["OUTCOME"][0]["fields"]["outcome"] == "SUBMITTED")


def test_e2e_alpaca_reference_price_provenance_full_chain(tmp_path) -> None:
    d = Dirs(tmp_path)
    decision_price = "190.00"
    fresh_venue_price = "190.05"  # drift = 0.05/190*10000 ~= 2.6 bps
    spec = make_alpaca_spec(decision_id="DEC-PROVENANCE-A", reference_price=decision_price)
    asset = make_alpaca_asset()

    expect("13a: the canonical spec echoes reference_price exactly as the caller supplied it",
           spec["reference_price"] == decision_price)

    fetch_calls: list = []
    client = FakeAlpacaClient()
    r = SUP.supervise_alpaca_equity_execution(
        spec, asset, auth_config=alpaca_auth_config(), attempt_submission=True, alpaca_client=client,
        supervisor_config=sup_open(), audit_dir=d.audit,
        price_fetch_fn=fixed_price_fetch_fn(fresh_venue_price, calls=fetch_calls),
        **d.alpaca_kwargs(),
    )
    expect("13b: the fresh-price fetch function was actually called", len(fetch_calls) == 1)
    expect("13c: submission SUBMITTED", r["status"] == "SUBMITTED")
    expect("13d: exactly one real .35 submit_order call happened", len(client.submit_calls) == 1)

    record = V40.get_record(spec["client_order_id"], base_dir=d.audit)
    events_by_type = {}
    for ev in record["events"]:
        events_by_type.setdefault(ev["event"], []).append(ev)
    expect("13e: full chain present for Alpaca too: DECISION..OUTCOME, written partly by .36 (REVALIDATED/"
           "CONSUMED) and partly by .38 (SUBMISSION_ATTEMPTED/OUTCOME, since .36 never calls .35.submit())",
           set(events_by_type) == {"DECISION", "AUTHORIZED", "REVALIDATED", "CONSUMED",
                                    "SUBMISSION_ATTEMPTED", "OUTCOME"})
    revalidated = events_by_type["REVALIDATED"][0]["fields"]
    expect("13f: recorded reference_price matches the original decision price exactly",
           revalidated["reference_price"] == decision_price)
    expect("13g: recorded fresh_price matches the fake venue's price exactly",
           revalidated["fresh_price"] == fresh_venue_price)
    expect("13h: OUTCOME reflects the real Alpaca submission status",
           events_by_type["OUTCOME"][0]["fields"]["outcome"] == "SUBMITTED")


# ======================================================================= #
# 5. End-to-end fail-closed paths, both venues
# ======================================================================= #

def test_e2e_mexc_stale_quote_fails_closed_and_blocks_submission(tmp_path) -> None:
    d = Dirs(tmp_path)
    spec = make_mexc_spec(decision_id="DEC-STALE-M", reference_price="50000")
    stale_ts = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    fake_exchange = FakeSubmissionExchange()
    r = SUP.supervise_mexc_futures_execution(
        spec, auth_config=mexc_auth_config(), attempt_submission=True, exchange=fake_exchange,
        supervisor_config=sup_open(), audit_dir=d.audit,
        price_fetch_fn=fixed_price_fetch_fn("50010", quote_observed_at=stale_ts),
        **d.mexc_kwargs(),
    )
    expect("14a: a stale quote blocks the MEXC submission", r["status"] == "BLOCKED")
    expect("14b: the exchange was never called", fake_exchange.create_order_calls == [])
    intent = LEDGER.get_intent(spec["client_order_id"], base_dir=d.mexc_ledger)
    expect("14c: the .29 intent stays at NEW (nothing was ever claimed)",
           intent is not None and intent["current_state"] == "NEW")
    record = V40.get_record(spec["client_order_id"], base_dir=d.audit)
    revalidated = [e for e in record["events"] if e["event"] == "REVALIDATED"][0]
    expect("14d: the audit trail records a FAILED revalidation, not silently dropped",
           revalidated["fields"]["passed"] is False and revalidated["fields"]["reason"] == "PRICE_DATA_STALE")


def test_e2e_alpaca_price_drift_fails_closed_and_blocks_submission(tmp_path) -> None:
    d = Dirs(tmp_path)
    spec = make_alpaca_spec(decision_id="DEC-DRIFT-A", reference_price="100.00")
    asset = make_alpaca_asset()
    client = FakeAlpacaClient()
    r = SUP.supervise_alpaca_equity_execution(
        spec, asset, auth_config=alpaca_auth_config(), attempt_submission=True, alpaca_client=client,
        supervisor_config=sup_open(), audit_dir=d.audit,
        price_fetch_fn=fixed_price_fetch_fn("110.00"),  # 1000 bps drift, default max is 50 bps
        **d.alpaca_kwargs(),
    )
    expect("15a: excessive drift blocks the Alpaca submission", r["status"] == "BLOCKED")
    expect("15b: .35.submit() was never called", client.submit_calls == [])
    record = V40.get_record(spec["client_order_id"], base_dir=d.audit)
    revalidated = [e for e in record["events"] if e["event"] == "REVALIDATED"][0]
    expect("15c: the audit trail records the drift rejection with reason PRICE_DRIFT_EXCEEDED",
           revalidated["fields"]["passed"] is False and revalidated["fields"]["reason"] == "PRICE_DRIFT_EXCEEDED")
    expect("15d: no CONSUMED event was ever written (nothing was claimed)",
           not any(e["event"] == "CONSUMED" for e in record["events"]))


def test_e2e_mexc_missing_reference_price_fails_closed(tmp_path) -> None:
    """The exact behavior Martin required be PRESERVED, not replaced: a
    market order with no trustworthy decision-time price available must
    fail closed (NO_REFERENCE_PRICE_AVAILABLE), never invent or backfill
    one."""
    d = Dirs(tmp_path)
    spec = make_mexc_spec(decision_id="DEC-NOPRICE-M")  # no reference_price supplied
    expect("16a: the canonical spec genuinely has no reference_price", spec.get("reference_price") is None)
    fake_exchange = FakeSubmissionExchange()
    r = SUP.supervise_mexc_futures_execution(
        spec, auth_config=mexc_auth_config(), attempt_submission=True, exchange=fake_exchange,
        supervisor_config=sup_open(), audit_dir=d.audit,
        price_fetch_fn=fixed_price_fetch_fn("50000"),  # a fetch fn IS supplied -- opt-in check is engaged
        **d.mexc_kwargs(),
    )
    expect("16b: BLOCKED, never invented/backfilled a reference price", r["status"] == "BLOCKED")
    expect("16c: reason is exactly NO_REFERENCE_PRICE_AVAILABLE", r["reason"] == "NO_REFERENCE_PRICE_AVAILABLE")
    expect("16d: the exchange was never called", fake_exchange.create_order_calls == [])
    intent = LEDGER.get_intent(spec["client_order_id"], base_dir=d.mexc_ledger)
    expect("16e: the .29 intent stays at NEW, still retryable (e.g. once a real price becomes available)",
           intent["current_state"] == "NEW")


def test_e2e_alpaca_missing_reference_price_fails_closed(tmp_path) -> None:
    d = Dirs(tmp_path)
    spec = make_alpaca_spec(decision_id="DEC-NOPRICE-A")
    asset = make_alpaca_asset()
    client = FakeAlpacaClient()
    r = SUP.supervise_alpaca_equity_execution(
        spec, asset, auth_config=alpaca_auth_config(), attempt_submission=True, alpaca_client=client,
        supervisor_config=sup_open(), audit_dir=d.audit,
        price_fetch_fn=fixed_price_fetch_fn("190.00"),
        **d.alpaca_kwargs(),
    )
    expect("17a: BLOCKED on the Alpaca path too", r["status"] == "BLOCKED")
    expect("17b: reason is exactly NO_REFERENCE_PRICE_AVAILABLE", r["reason"] == "NO_REFERENCE_PRICE_AVAILABLE")
    expect("17c: .35.submit() was never called", client.submit_calls == [])


def test_e2e_without_price_fetch_fn_behavior_is_byte_for_byte_unchanged(tmp_path) -> None:
    """The additive/opt-in guarantee: a spec with NO reference_price and
    NO price_fetch_fn supplied must succeed exactly as it did before
    v0.5.3.40 -- the price check is never silently engaged."""
    d = Dirs(tmp_path)
    spec = make_mexc_spec(decision_id="DEC-NOOPTIN-M")  # no reference_price
    fake_exchange = FakeSubmissionExchange()
    r = SUP.supervise_mexc_futures_execution(
        spec, auth_config=mexc_auth_config(), attempt_submission=True, exchange=fake_exchange,
        supervisor_config=sup_open(), **d.mexc_kwargs(),  # no price_fetch_fn, no audit_dir
    )
    expect("18: with price_fetch_fn=None (default), a spec with no reference_price still submits normally",
           r["status"] == "SUBMITTED")


# ======================================================================= #
# 6. Claim remains consumed when the revalidation-to-submission ceiling
#    expires
# ======================================================================= #

def test_e2e_mexc_ceiling_expiry_leaves_claim_permanently_consumed(tmp_path) -> None:
    """The ceiling (check_revalidation_to_submission_ceiling()) is
    evaluated against the SAME frozen `moment` the whole atomic
    authorized_submit() call uses (by design -- see that function's own
    docstring), so a real, non-zero elapsed gap cannot be manufactured
    within a single synchronous call in a test. This test instead uses a
    deliberately absurd NEGATIVE max_revalidation_to_submission_seconds
    to deterministically exercise the fail-closed branch -- a
    synthetic trigger, documented as such, standing in for what a real
    pathological scheduling delay would produce in production. The
    property under test is what happens AFTER the ceiling fires: the
    authorization_id claim, already granted, must remain permanently
    consumed -- proven by a retry with a healthy config still being
    reported as ALREADY_CONSUMED, never a fresh attempt."""
    d = Dirs(tmp_path)
    spec = make_mexc_spec(decision_id="DEC-CEILING-M", reference_price="50000")
    fake_exchange = FakeSubmissionExchange()
    r = SUP.supervise_mexc_futures_execution(
        spec, auth_config=mexc_auth_config(), attempt_submission=True, exchange=fake_exchange,
        supervisor_config=sup_open(), audit_dir=d.audit,
        price_fetch_fn=fixed_price_fetch_fn("50000"),
        max_revalidation_to_submission_seconds=-1.0,
        **d.mexc_kwargs(),
    )
    expect("19a: the ceiling check blocks the submission", r["status"] == "BLOCKED")
    expect("19b: the exchange itself was never reached", fake_exchange.create_order_calls == [])
    intent = LEDGER.get_intent(spec["client_order_id"], base_dir=d.mexc_ledger)
    expect("19c: the .29 intent was moved to CLAIMED-derived escalation, NOT left at NEW -- the claim really "
           "was granted before the ceiling fired", intent["current_state"] == "ESCALATED_HUMAN_REVIEW")

    fake_retry = FakeSubmissionExchange()
    retry = SUP.supervise_mexc_futures_execution(
        spec, auth_config=mexc_auth_config(), attempt_submission=True, exchange=fake_retry,
        supervisor_config=sup_open(), **d.mexc_kwargs(),  # healthy config, no ceiling override this time
    )
    expect("19d: a retry of the SAME decision is blocked as an existing (escalated) intent, "
           "never silently resubmitted", retry["status"] == "BLOCKED" and retry["stage"] == "EXISTING_INTENT")
    expect("19e: the retry never reached the exchange", fake_retry.create_order_calls == [])


def test_e2e_alpaca_ceiling_expiry_leaves_claim_permanently_consumed(tmp_path) -> None:
    d = Dirs(tmp_path)
    spec = make_alpaca_spec(decision_id="DEC-CEILING-A", reference_price="190.00")
    asset = make_alpaca_asset()
    client = FakeAlpacaClient()
    r = SUP.supervise_alpaca_equity_execution(
        spec, asset, auth_config=alpaca_auth_config(), attempt_submission=True, alpaca_client=client,
        supervisor_config=sup_open(), audit_dir=d.audit,
        price_fetch_fn=fixed_price_fetch_fn("190.00"),
        max_revalidation_to_submission_seconds=-1.0,
        **d.alpaca_kwargs(),
    )
    expect("20a: the ceiling check blocks the Alpaca submission", r["status"] == "BLOCKED")
    expect("20b: .35.submit() was never called", client.submit_calls == [])
    expect("20c: the .37 authorization_id claim underneath remains permanently consumed",
           list(d.alpaca_auth.glob("*")) != [])

    client2 = FakeAlpacaClient()
    retry = SUP.supervise_alpaca_equity_execution(
        spec, asset, auth_config=alpaca_auth_config(), attempt_submission=True, alpaca_client=client2,
        supervisor_config=sup_open(), **d.alpaca_kwargs(),  # healthy config, no ceiling override
    )
    expect("20d: a retry is blocked by the Supervisor-level client_order_id claim (never released either)",
           retry["status"] == "BLOCKED")
    expect("20e: the retry never reached the broker", client2.submit_calls == [])


# ======================================================================= #
# 7. No orphan .29 NEW intent after a pre-claim revalidation rejection --
#    also the regression test for a genuine bug found and fixed during
#    this verification pass.
# ======================================================================= #

def test_mexc_pre_claim_rejection_leaves_intent_at_new_and_retryable(tmp_path) -> None:
    """Regression test for a real bug found (and fixed) while wiring
    .38 to .40 this session: since .31.authorized_submit() was reordered
    to revalidate BEFORE claiming (an earlier, already-approved v0.5.3.40
    change), a "REVALIDATION_FAILED" status from that function became
    ambiguous -- it now covers both 'nothing was ever claimed' (pre-claim
    rejection) and 'a claim WAS granted, then the post-claim ceiling
    check blocked it'. .38's own consumption-mapping code, written before
    that reordering, still unconditionally treated ANY non-ALREADY_
    CONSUMED status as proof a claim had been granted, and called
    .29.record_claimed() regardless -- which would have incorrectly
    transitioned (and then escalated) a .29 intent for a decision that
    was never actually claimed at all, directly undermining the entire
    point of the v0.5.3.40 reordering. Fixed by having
    .31.authorized_submit() return an explicit `claim_granted` boolean
    and having .38 consult it. This test proves the fix: a pre-claim
    price-drift rejection must leave the .29 intent at NEW (not CLAIMED
    or ESCALATED_HUMAN_REVIEW), and a subsequent retry with a passing
    price must complete normally.

    The retry at step 21e also exercises a SECOND bug found during this
    same verification pass: with audit_dir wired, .31.authorize() used
    to call .40.record_authorized() unconditionally, which crashed with
    an unhandled IllegalTransitionError once the FIRST attempt's audit
    record had already reached a terminal REVALIDATION_FAILED state.
    Reported to Martin (not guessed at, since several designs were
    reasonable); resolved per his explicit decision with best-effort
    audit writes (_audit_write_best_effort() in .31/.36/.38, swallowing
    only IllegalTransitionError). This test's retry step would still
    raise today if that fix were reverted."""
    d = Dirs(tmp_path)
    spec = make_mexc_spec(decision_id="DEC-NOORPHAN-M", reference_price="50000")
    fake_exchange_1 = FakeSubmissionExchange()
    blocked = SUP.supervise_mexc_futures_execution(
        spec, auth_config=mexc_auth_config(), attempt_submission=True, exchange=fake_exchange_1,
        supervisor_config=sup_open(), audit_dir=d.audit,
        price_fetch_fn=fixed_price_fetch_fn("60000"),  # 20000bps drift -- rejected pre-claim
        **d.mexc_kwargs(),
    )
    expect("21a: the pre-claim drift rejection blocks the submission", blocked["status"] == "BLOCKED")
    expect("21b: the exchange was never reached", fake_exchange_1.create_order_calls == [])

    intent = LEDGER.get_intent(spec["client_order_id"], base_dir=d.mexc_ledger)
    expect("21c: THE FIX: the .29 intent stays at NEW -- no phantom claim was ever recorded",
           intent is not None and intent["current_state"] == "NEW")
    expect("21d: no .32 authorization claim file was created either (nothing claimed anywhere)",
           not d.mexc_auth.exists() or not list(d.mexc_auth.glob("*")))

    fake_exchange_2 = FakeSubmissionExchange(create_order_result={"id": "mexc-retry-ok", "status": "open"})
    retry = SUP.supervise_mexc_futures_execution(
        spec, auth_config=mexc_auth_config(), attempt_submission=True, exchange=fake_exchange_2,
        supervisor_config=sup_open(), audit_dir=d.audit,
        price_fetch_fn=fixed_price_fetch_fn("50005"),  # small, passing drift this time
        **d.mexc_kwargs(),
    )
    expect("21e: a genuine retry with a healthy price completes normally, all the way to SUBMITTED",
           retry["status"] == "SUBMITTED")
    intent2 = LEDGER.get_intent(spec["client_order_id"], base_dir=d.mexc_ledger)
    expect("21f: the .29 intent now correctly reflects a real submission (AWAITING_RECONCILIATION)",
           intent2["current_state"] == "AWAITING_RECONCILIATION")


# ======================================================================= #
# 8. Existing .29 historical state machine remains fully compatible
# ======================================================================= #

def test_mexc_29_ledger_shape_unchanged_by_40_wiring(tmp_path) -> None:
    """A .40-wired MEXC submission (price_fetch_fn + audit_dir supplied)
    must produce the EXACT SAME .29 ledger shape as a plain, non-.40
    submission (.38's own pre-.40 test suite,
    test_mexc_full_submission_path_lands_at_awaiting_reconciliation) --
    proving .29 itself was never touched or reinterpreted by this
    milestone, only orchestrated around, exactly as designed."""
    d = Dirs(tmp_path)
    spec = make_mexc_spec(decision_id="DEC-29COMPAT-M", reference_price="50000")
    fake = FakeSubmissionExchange(create_order_result={"id": "mexc-order-compat", "status": "open"})
    r = SUP.supervise_mexc_futures_execution(
        spec, auth_config=mexc_auth_config(), attempt_submission=True, exchange=fake,
        supervisor_config=sup_open(), audit_dir=d.audit,
        price_fetch_fn=fixed_price_fetch_fn("50000"),
        **d.mexc_kwargs(),
    )
    expect("22a: SUBMITTED, identically to the non-.40 path", r["status"] == "SUBMITTED")
    intent = LEDGER.get_intent(spec["client_order_id"], base_dir=d.mexc_ledger)
    expect("22b: .29 lands at AWAITING_RECONCILIATION -- the same terminal-of-this-phase state as always",
           intent["current_state"] == "AWAITING_RECONCILIATION")
    event_names = [e["event"] for e in intent["events"]]
    expect("22c: .29's own event vocabulary is completely unchanged (INTENT_CREATED/CLAIMED/"
           "SUBMISSION_ATTEMPTED/SUBMISSION_ACKNOWLEDGED, no new event types introduced)",
           set(event_names) <= {"INTENT_CREATED", "CLAIMED", "SUBMISSION_ATTEMPTED",
                                 "SUBMISSION_ACKNOWLEDGED", "EXECUTION_UNCERTAIN"})


# ======================================================================= #
# 9. Concurrency -- exactly one winner, with the audit trail wired
# ======================================================================= #

def test_mexc_concurrent_attempts_with_audit_trail_exactly_one_wins(tmp_path) -> None:
    """Also proves the fix for a second bug found during this same
    verification pass: before best-effort audit writes
    (_audit_write_best_effort(), Martin's explicit .40 decision),
    concurrent authorize() calls racing on the SAME client_order_id would
    have every LOSING call's .40.record_authorized() call raise an
    unhandled IllegalTransitionError straight out of
    supervise_mexc_futures_execution() -- exceptions are captured
    explicitly below (never silently swallowed by the test itself) and
    asserted to be empty, so this test would fail loudly again if that
    fix were reverted, rather than misleadingly "passing" on the strength
    of the single genuine winner alone."""
    d = Dirs(tmp_path)
    spec = make_mexc_spec(decision_id="DEC-CONCURRENT-40-M", reference_price="50000")
    fakes: list[FakeSubmissionExchange] = []
    results: list[dict] = []
    exceptions: list[BaseException] = []
    lock = threading.Lock()

    def attempt():
        fake = FakeSubmissionExchange()
        try:
            r = SUP.supervise_mexc_futures_execution(
                spec, auth_config=mexc_auth_config(), attempt_submission=True, exchange=fake,
                supervisor_config=sup_open(), audit_dir=d.audit,
                price_fetch_fn=fixed_price_fetch_fn("50002"),
                **d.mexc_kwargs(),
            )
        except Exception as exc:  # noqa: BLE001 -- deliberately captured, not swallowed silently
            with lock:
                exceptions.append(exc)
            return
        with lock:
            fakes.append(fake)
            results.append(r)

    threads = [threading.Thread(target=attempt) for _ in range(15)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    expect("23a: EVERY concurrent attempt completes without raising -- a losing attempt must be a "
           "clean, reported BLOCKED/ALREADY_CONSUMED result, never an unhandled exception",
           exceptions == [])

    total_create_order_calls = sum(len(f.create_order_calls) for f in fakes)
    expect("23b: exactly ONE real create_order call across 15 concurrent .40-audited attempts",
           total_create_order_calls == 1)
    submitted = [r for r in results if r["status"] == "SUBMITTED"]
    expect("23c: exactly one attempt reports SUBMITTED", len(submitted) == 1)

    record = V40.get_record(spec["client_order_id"], base_dir=d.audit)
    expect("23d: the audit trail itself stayed internally consistent under concurrency "
           "(a single coherent record, not corrupted by concurrent writers)",
           V40.verify_record(record)[0] is True)
    consumed_events = [e for e in record["events"] if e["event"] == "CONSUMED"]
    granted_count = sum(1 for e in consumed_events if e["fields"]["granted"] is True)
    expect("23e: the audit trail shows exactly one granted CONSUMED event (never more than one)",
           granted_count == 1)


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_")]
    for t in tests:
        t()
    print(f"AURA v0.5.3.40 UNIT+E2E TEST CONTRACT: ALL {len(tests)} PASS")
