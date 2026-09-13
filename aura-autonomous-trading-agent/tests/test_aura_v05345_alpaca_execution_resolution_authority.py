#!/usr/bin/env python3
"""Contract + unit tests for AURA v0.5.3.45 Alpaca Execution Resolution
Authority.

Builds real `.40` audit records (via `.40`'s own record_decision/
record_authorized/record_revalidated/record_consumed/
record_submission_attempted/record_outcome, not stand-ins) and resolves
them through `.45`'s real functions. Uses injectable fake Alpaca order
query clients (no real network call, no real credentials, matching this
repo's established convention).
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIT_PATH = ROOT / "aura_v05340_pre_submission_revalidation.py"
RES_PATH = ROOT / "aura_v05345_alpaca_execution_resolution_authority.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


AUDIT = _load("aura_v05340_pre_submission_revalidation", AUDIT_PATH)
RES = _load("aura_v05345_alpaca_execution_resolution_authority", RES_PATH)

NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)


class FakeOrder:
    def __init__(self, status, filled_qty=None, qty=None):
        self.status = status  # plain string, mimics OrderStatus.value being read via getattr(status, "value", status)
        self.filled_qty = filled_qty
        self.qty = qty


class FakeAPIError(Exception):
    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__(f"APIError:{status_code}")


class FakeAlpacaClient:
    """Injectable stand-in for alpaca-py's TradingClient -- only the one
    method .45 actually calls."""

    def __init__(self, order=None, raise_status_code=None, raise_other=False):
        self._order = order
        self._raise_status_code = raise_status_code
        self._raise_other = raise_other
        self.calls: list[str] = []

    def get_order_by_client_id(self, client_id):
        self.calls.append(client_id)
        if self._raise_other:
            raise ConnectionError("simulated network failure")
        if self._raise_status_code is not None:
            raise FakeAPIError(self._raise_status_code)
        return self._order


def _new_alpaca_record(base_dir, coid="aura-test-45", outcome="EXECUTION_UNCERTAIN", outcome_recorded_at=None):
    AUDIT.record_decision(coid, venue="ALPACA", asset_class="STOCK", symbol="AAPL", spec_fingerprint="fp1", base_dir=base_dir)
    AUDIT.record_authorized(coid, base_dir=base_dir)
    AUDIT.record_revalidated(coid, passed=True, price_result={
        "fresh_price": "150", "reference_price": "150", "drift_bps": "0",
        "quote_observed_at": NOW.isoformat(), "price_checked_at": NOW.isoformat(),
        "max_quote_age_seconds": 10, "max_price_drift_bps": 50,
    }, base_dir=base_dir)
    AUDIT.record_consumed(coid, granted=True, base_dir=base_dir)
    AUDIT.record_submission_attempted(coid, base_dir=base_dir)
    record = AUDIT.record_outcome(coid, outcome=outcome, detail="test", base_dir=base_dir)
    if outcome_recorded_at is not None:
        # Directly rewrite the OUTCOME event's timestamp for staleness tests
        # -- a controlled, explicit test fixture, not a hack around .40's
        # own write path (the record is still re-verified/re-hashed after).
        for ev in record["events"]:
            if ev["event"] == "OUTCOME":
                ev["recorded_at"] = outcome_recorded_at
        record = AUDIT._finalize(record)
        AUDIT._write_record_atomic(AUDIT._record_path(coid, base_dir), record)
    return coid, record


# ---------------------------------------------------------------------------
# classify_alpaca_order_state -- the evidence matrix.
# ---------------------------------------------------------------------------

def test_classify_filled():
    assert RES.classify_alpaca_order_state({"found": True, "status": "filled", "filled_qty": 10, "qty": 10}) == ("FILLED", "STATUS_FILLED")


def test_classify_rejected():
    assert RES.classify_alpaca_order_state({"found": True, "status": "rejected", "filled_qty": None, "qty": 10}) == ("REJECTED", "STATUS_REJECTED")


def test_classify_canceled_zero_fill():
    bucket, reason = RES.classify_alpaca_order_state({"found": True, "status": "canceled", "filled_qty": 0, "qty": 10})
    assert bucket == "CANCELED"


def test_classify_canceled_with_partial_fill_is_terminal_partial():
    bucket, reason = RES.classify_alpaca_order_state({"found": True, "status": "canceled", "filled_qty": 4, "qty": 10})
    assert bucket == "PARTIALLY_FILLED_TERMINAL"


def test_classify_expired_zero_fill_is_canceled_bucket():
    bucket, _ = RES.classify_alpaca_order_state({"found": True, "status": "expired", "filled_qty": None, "qty": 10})
    assert bucket == "CANCELED"


def test_classify_partially_filled_still_open():
    bucket, _ = RES.classify_alpaca_order_state({"found": True, "status": "partially_filled", "filled_qty": 4, "qty": 10})
    assert bucket == "PARTIALLY_FILLED_OPEN"


def test_classify_still_open_statuses():
    for status in ["new", "pending_new", "accepted", "accepted_for_bidding", "pending_cancel",
                    "pending_replace", "calculated", "held", "stopped", "suspended", "pending_review", "done_for_day"]:
        bucket, _ = RES.classify_alpaca_order_state({"found": True, "status": status, "filled_qty": None, "qty": 10})
        assert bucket == "STILL_OPEN", status


def test_classify_not_found():
    assert RES.classify_alpaca_order_state({"found": False}) == ("NOT_FOUND", "ORDER_NOT_FOUND_AT_BROKER")


def test_classify_replaced_is_unhandled():
    bucket, _ = RES.classify_alpaca_order_state({"found": True, "status": "replaced", "filled_qty": None, "qty": 10})
    assert bucket == "UNHANDLED"


def test_classify_unrecognized_status_never_guessed():
    bucket, reason = RES.classify_alpaca_order_state({"found": True, "status": "some_future_status", "filled_qty": None, "qty": 10})
    assert bucket == "UNRECOGNIZED"
    assert "some_future_status" in reason


# ---------------------------------------------------------------------------
# fetch_alpaca_order_state -- query layer, injectable client.
# ---------------------------------------------------------------------------

def test_fetch_order_state_success():
    client = FakeAlpacaClient(order=FakeOrder(status="filled", filled_qty="10", qty="10"))
    result = RES.fetch_alpaca_order_state(client, "coid-1")
    assert result == {"found": True, "status": "filled", "filled_qty": Decimal("10"), "qty": Decimal("10")}


def test_fetch_order_state_404_is_not_found():
    client = FakeAlpacaClient(raise_status_code=404)
    result = RES.fetch_alpaca_order_state(client, "coid-1")
    assert result == {"found": False}


def test_fetch_order_state_other_error_raises_query_error_never_silently_not_found():
    client = FakeAlpacaClient(raise_status_code=500)
    try:
        RES.fetch_alpaca_order_state(client, "coid-1")
        assert False, "expected ResolutionQueryError"
    except RES.ResolutionQueryError:
        pass


def test_fetch_order_state_network_error_raises_query_error():
    client = FakeAlpacaClient(raise_other=True)
    try:
        RES.fetch_alpaca_order_state(client, "coid-1")
        assert False, "expected ResolutionQueryError"
    except RES.ResolutionQueryError:
        pass


def test_fetch_order_state_missing_status_raises():
    client = FakeAlpacaClient(order=FakeOrder(status=None))
    try:
        RES.fetch_alpaca_order_state(client, "coid-1")
        assert False, "expected ResolutionQueryError"
    except RES.ResolutionQueryError:
        pass


def test_fetch_order_state_enum_like_status_reads_value():
    class EnumLike:
        value = "filled"
    client = FakeAlpacaClient(order=FakeOrder(status=EnumLike(), filled_qty="10", qty="10"))
    result = RES.fetch_alpaca_order_state(client, "coid-1")
    assert result["status"] == "filled"


# ---------------------------------------------------------------------------
# resolve_alpaca_execution -- both a clean SUBMITTED and an
# EXECUTION_UNCERTAIN outcome go through the same pipeline (Martin's
# explicit "mirror MEXC" scoping answer).
# ---------------------------------------------------------------------------

def test_resolve_execution_uncertain_outcome_to_filled():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        coid, record = _new_alpaca_record(base, outcome="EXECUTION_UNCERTAIN")
        verdict = RES.resolve_alpaca_execution(record, {"found": True, "status": "filled", "filled_qty": 10, "qty": 10}, now=NOW)
        assert verdict["action"] == "RESOLVE"
        assert verdict["resolution"] == "RESOLVED_FILLED"
        assert verdict["proven_failed"] is False


def test_resolve_clean_submitted_outcome_to_filled_same_pipeline():
    """A clean SUBMITTED acknowledgment (not EXECUTION_UNCERTAIN) is
    resolved through the exact same function -- per Martin's explicit
    scoping decision to mirror MEXC's identical treatment of
    SUBMISSION_ACKNOWLEDGED and EXECUTION_UNCERTAIN."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        coid, record = _new_alpaca_record(base, outcome="SUBMITTED")
        verdict = RES.resolve_alpaca_execution(record, {"found": True, "status": "filled", "filled_qty": 10, "qty": 10}, now=NOW)
        assert verdict["action"] == "RESOLVE"
        assert verdict["resolution"] == "RESOLVED_FILLED"


def test_resolve_to_rejected_marks_proven_failed():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        coid, record = _new_alpaca_record(base)
        verdict = RES.resolve_alpaca_execution(record, {"found": True, "status": "rejected", "filled_qty": None, "qty": 10}, now=NOW)
        assert verdict["action"] == "RESOLVE"
        assert verdict["resolution"] == "RESOLVED_REJECTED"
        assert verdict["proven_failed"] is True


def test_resolve_to_canceled_marks_proven_failed():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        coid, record = _new_alpaca_record(base)
        verdict = RES.resolve_alpaca_execution(record, {"found": True, "status": "canceled", "filled_qty": 0, "qty": 10}, now=NOW)
        assert verdict["action"] == "RESOLVE"
        assert verdict["resolution"] == "RESOLVED_CANCELED"
        assert verdict["proven_failed"] is True


def test_resolve_to_partially_filled_terminal_not_proven_failed():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        coid, record = _new_alpaca_record(base)
        verdict = RES.resolve_alpaca_execution(record, {"found": True, "status": "canceled", "filled_qty": 4, "qty": 10}, now=NOW)
        assert verdict["resolution"] == "RESOLVED_PARTIALLY_FILLED"
        assert verdict["proven_failed"] is False


def test_resolve_partially_filled_open_stays_pending():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        coid, record = _new_alpaca_record(base)
        verdict = RES.resolve_alpaca_execution(record, {"found": True, "status": "partially_filled", "filled_qty": 4, "qty": 10}, now=NOW)
        assert verdict["action"] == "STAY_PENDING"


def test_resolve_still_open_stays_pending_when_not_stale():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        coid, record = _new_alpaca_record(base, outcome_recorded_at=NOW.isoformat())
        verdict = RES.resolve_alpaca_execution(record, {"found": True, "status": "new", "filled_qty": None, "qty": 10},
                                                 staleness_policy={"resolution_staleness_minutes": 60}, now=NOW)
        assert verdict["action"] == "STAY_PENDING"


def test_resolve_still_open_escalates_when_stale():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        old_ts = (NOW - timedelta(minutes=120)).isoformat()
        coid, record = _new_alpaca_record(base, outcome_recorded_at=old_ts)
        verdict = RES.resolve_alpaca_execution(record, {"found": True, "status": "new", "filled_qty": None, "qty": 10},
                                                 staleness_policy={"resolution_staleness_minutes": 60}, now=NOW)
        assert verdict["action"] == "ESCALATE"
        assert "STALENESS_THRESHOLD_EXCEEDED" in verdict["reason"]


def test_resolve_not_found_stays_pending_conservatively():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        coid, record = _new_alpaca_record(base)
        verdict = RES.resolve_alpaca_execution(record, {"found": False}, now=NOW)
        assert verdict["action"] == "STAY_PENDING"


def test_resolve_not_found_escalates_when_stale():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        old_ts = (NOW - timedelta(minutes=120)).isoformat()
        coid, record = _new_alpaca_record(base, outcome_recorded_at=old_ts)
        verdict = RES.resolve_alpaca_execution(record, {"found": False},
                                                 staleness_policy={"resolution_staleness_minutes": 60}, now=NOW)
        assert verdict["action"] == "ESCALATE"


def test_resolve_replaced_escalates():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        coid, record = _new_alpaca_record(base)
        verdict = RES.resolve_alpaca_execution(record, {"found": True, "status": "replaced", "filled_qty": None, "qty": 10}, now=NOW)
        assert verdict["action"] == "ESCALATE"


def test_resolve_unrecognized_status_escalates_never_guesses():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        coid, record = _new_alpaca_record(base)
        verdict = RES.resolve_alpaca_execution(record, {"found": True, "status": "totally_new_status", "filled_qty": None, "qty": 10}, now=NOW)
        assert verdict["action"] == "ESCALATE"


def test_resolve_blocked_on_non_alpaca_record():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        AUDIT.record_decision("mexc-coid-1", venue="MEXC", asset_class="CRYPTO_FUTURES", symbol="BTC_USDT", spec_fingerprint="fp1", base_dir=base)
        record = AUDIT.get_record("mexc-coid-1", base_dir=base)
        verdict = RES.resolve_alpaca_execution(record, {"found": True, "status": "filled", "filled_qty": 10, "qty": 10}, now=NOW)
        assert verdict["action"] == "BLOCKED"
        assert "NOT_AN_ALPACA_RECORD" in verdict["reason"]


def test_resolve_blocked_on_record_not_yet_at_outcome():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        AUDIT.record_decision("coid-early", venue="ALPACA", asset_class="STOCK", symbol="AAPL", spec_fingerprint="fp1", base_dir=base)
        record = AUDIT.get_record("coid-early", base_dir=base)
        verdict = RES.resolve_alpaca_execution(record, {"found": True, "status": "filled", "filled_qty": 10, "qty": 10}, now=NOW)
        assert verdict["action"] == "BLOCKED"
        assert "PRECONDITION_VIOLATION" in verdict["reason"]


def test_resolve_blocked_on_tampered_record():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        coid, record = _new_alpaca_record(base)
        tampered = dict(record)
        tampered["record_hash"] = "0" * 64
        verdict = RES.resolve_alpaca_execution(tampered, {"found": True, "status": "filled", "filled_qty": 10, "qty": 10}, now=NOW)
        assert verdict["action"] == "BLOCKED"
        assert "VERIFICATION_FAILED" in verdict["reason"]


# ---------------------------------------------------------------------------
# apply_verdict -- the sole writer into .40's storage.
# ---------------------------------------------------------------------------

def test_apply_verdict_resolve_writes_resolved_event():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        coid, record = _new_alpaca_record(base)
        verdict = RES.resolve_alpaca_execution(record, {"found": True, "status": "filled", "filled_qty": 10, "qty": 10}, now=NOW)
        resolved = RES.apply_verdict(coid, verdict, base_dir=base)
        assert resolved["current_state"] == "RESOLVED:RESOLVED_FILLED"
        ok, errors = AUDIT.verify_record(resolved)
        assert ok, errors


def test_apply_verdict_escalate_writes_escalated_human_review():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        coid, record = _new_alpaca_record(base)
        verdict = RES.resolve_alpaca_execution(record, {"found": True, "status": "replaced", "filled_qty": None, "qty": 10}, now=NOW)
        resolved = RES.apply_verdict(coid, verdict, base_dir=base)
        assert resolved["current_state"] == "RESOLVED:ESCALATED_HUMAN_REVIEW"


def test_apply_verdict_stay_pending_writes_nothing():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        coid, record = _new_alpaca_record(base)
        verdict = RES.resolve_alpaca_execution(record, {"found": True, "status": "new", "filled_qty": None, "qty": 10}, now=NOW)
        result = RES.apply_verdict(coid, verdict, base_dir=base)
        assert result is None
        unchanged = AUDIT.get_record(coid, base_dir=base)
        assert unchanged["current_state"] == "OUTCOME:EXECUTION_UNCERTAIN"


def test_apply_verdict_blocked_writes_nothing():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        coid, record = _new_alpaca_record(base)
        verdict = {"action": "BLOCKED", "reason": "whatever"}
        result = RES.apply_verdict(coid, verdict, base_dir=base)
        assert result is None


def test_record_resolved_refuses_mexc_records():
    """The hard safety rail: .45 must never write an independent
    resolution opinion onto a MEXC record -- .29/.30 remain sole
    authority there."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        AUDIT.record_decision("mexc-coid-2", venue="MEXC", asset_class="CRYPTO_FUTURES", symbol="BTC_USDT", spec_fingerprint="fp1", base_dir=base)
        try:
            AUDIT.record_resolved("mexc-coid-2", resolution="RESOLVED_FILLED", base_dir=base)
            assert False, "expected AuditTrailError"
        except AUDIT.AuditTrailError as exc:
            assert "RESOLVED_EVENT_NOT_PERMITTED_FOR_VENUE" in str(exc)


def test_record_resolved_rejects_invalid_resolution_value():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        coid, record = _new_alpaca_record(base)
        try:
            AUDIT.record_resolved(coid, resolution="MADE_UP_RESOLUTION", base_dir=base)
            assert False, "expected AuditTrailError"
        except AUDIT.AuditTrailError as exc:
            assert "INVALID_RESOLUTION" in str(exc)


def test_resolved_state_is_terminal_second_resolution_rejected():
    """Once RESOLVED, .40's state machine must refuse a second RESOLVED
    event -- a resolution decision, once made, is final."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        coid, record = _new_alpaca_record(base)
        verdict = RES.resolve_alpaca_execution(record, {"found": True, "status": "filled", "filled_qty": 10, "qty": 10}, now=NOW)
        RES.apply_verdict(coid, verdict, base_dir=base)
        try:
            AUDIT.record_resolved(coid, resolution="RESOLVED_CANCELED", base_dir=base)
            assert False, "expected an error rejecting the second RESOLVED event"
        except (AUDIT.AuditTrailError, AUDIT.IllegalTransitionError) as exc:
            assert "EVENT_AFTER_TERMINAL" in str(exc)


# ---------------------------------------------------------------------------
# Determinism / never auto-retries or submits (static + behavioral guard).
# ---------------------------------------------------------------------------

def test_same_inputs_produce_identical_verdict():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        coid, record = _new_alpaca_record(base)
        order_state = {"found": True, "status": "filled", "filled_qty": 10, "qty": 10}
        v1 = RES.resolve_alpaca_execution(record, order_state, now=NOW)
        v2 = RES.resolve_alpaca_execution(record, order_state, now=NOW)
        assert v1 == v2


def test_module_never_submits_or_cancels_or_retries_an_order():
    source = RES_PATH.read_text(encoding="utf-8")
    code_only = source.split('"""', 2)[-1]
    forbidden = ["submit_order(", "cancel_order(", ".submit(", "replace_order("]
    for token in forbidden:
        assert token not in code_only, f"resolution-authority module must not reference {token!r}"
