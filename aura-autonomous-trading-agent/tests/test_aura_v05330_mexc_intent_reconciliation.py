#!/usr/bin/env python3
"""Contract tests for AURA v0.5.3.30 MEXC Intent Reconciliation.

Covers the transition/evidence test matrix's Sec.3 (T30-* evidence-
matrix cases) directly against reconcile_intent(), using real v0.5.3.29
intent records (built through .29's own mutators, never hand-
constructed) and real v0.5.3.28-shaped snapshots (finalized through
.28's own finalize()/hashing, so the hash-verification path is
exercised honestly rather than faked).

No MEXC, no network, no ccxt.
"""
from __future__ import annotations

import importlib.util
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


LEDGER = _load("aura_v05329_mexc_intent_ledger", "aura_v05329_mexc_intent_ledger.py")
OBSERVED = _load("aura_v05328_mexc_observed_execution", "aura_v05328_mexc_observed_execution.py")
RECON = _load("aura_v05330_mexc_intent_reconciliation", "aura_v05330_mexc_intent_reconciliation.py")


def expect(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"PASS: {name}")


# --------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------- #

def make_intent(base_dir: Path, state: str = "AWAITING_RECONCILIATION", client_order_id: str = "aura-30-intent",
                 direction: str = "OPEN_LONG", partial_terminal: bool | None = None) -> dict:
    LEDGER.create_intent(client_order_id, "BTC/USDT:USDT", "buy", 1.0, direction, "sha256:fp", base_dir=base_dir)
    if state == "NEW":
        return LEDGER.get_intent(client_order_id, base_dir)
    LEDGER.record_claimed(client_order_id, base_dir=base_dir)
    LEDGER.record_submission_attempted(client_order_id, base_dir=base_dir)
    if state == "SUBMISSION_ATTEMPTED":
        return LEDGER.get_intent(client_order_id, base_dir)
    LEDGER.record_submission_outcome(client_order_id, "SUBMISSION_ACKNOWLEDGED", mexc_order_id="mexc-1", base_dir=base_dir)
    if state == "AWAITING_RECONCILIATION":
        return LEDGER.get_intent(client_order_id, base_dir)
    if state == "RECONCILING":
        return LEDGER.record_reconciliation_attempt(client_order_id, "prior-snap", "UNRESOLVED", base_dir=base_dir)
    if state == "RECONCILED_FILLED":
        return LEDGER.record_reconciliation_attempt(client_order_id, "prior-snap", "FILLED", base_dir=base_dir)
    if state == "RECONCILED_PARTIALLY_FILLED":
        return LEDGER.record_reconciliation_attempt(
            client_order_id, "prior-snap", "PARTIALLY_FILLED", order_terminal_on_exchange=partial_terminal, base_dir=base_dir
        )
    raise ValueError(state)


def raw_order(**overrides) -> dict:
    base = {
        "orderId": "mexc-order-1",
        "symbol": "BTC_USDT",
        "positionId": "0",
        "price": "50000",
        "vol": "1",
        "dealAvgPrice": "50000",
        "dealVol": "1",
        "state": "3",
        "externalOid": "aura-30-intent",
        "createTime": "1700000000000",
        "updateTime": "1700000001000",
    }
    base.update(overrides)
    return base


def make_snapshot(client_order_id: str = "aura-30-intent", order_status: str = "FILLED",
                   position_exists: bool = True, position_id: str | None = "pos-1",
                   fill_price: float | None = 50000.0, fill_timestamp: str | None = None,
                   match_count: int | None = 1, raw_status: str | None = "3",
                   raw_order_info: dict | None = None, reason: str | None = None,
                   mexc_order_id: str | None = "mexc-order-1") -> dict:
    if fill_timestamp is None and order_status in ("FILLED", "PARTIALLY_FILLED"):
        fill_timestamp = datetime.now(timezone.utc).isoformat()
    snapshot = OBSERVED.base_snapshot()
    snapshot.update({
        "snapshot_status": "OBSERVED",
        "client_order_id": client_order_id,
        "symbol": "BTC/USDT:USDT",
        "mexc_order_id": mexc_order_id,
        "order_status": order_status,
        "raw_status": raw_status,
        "match_count": match_count,
        "position_exists": position_exists,
        "position_id": position_id,
        "fill_price": fill_price,
        "fill_timestamp": fill_timestamp,
        "observed_fills": [],
        "reason": reason,
        "queried_at": datetime.now(timezone.utc).isoformat(),
    })
    if raw_order_info is not None:
        snapshot["raw_evidence"]["order"] = raw_order_info
    return OBSERVED.finalize(snapshot)


# --------------------------------------------------------------------- #
# T30-FILL-* : order evidence alone proves execution
# --------------------------------------------------------------------- #

def test_fill_open_with_position() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        intent = make_intent(base, "AWAITING_RECONCILIATION", direction="OPEN_LONG")
        snap = make_snapshot(order_status="FILLED", position_exists=True, raw_order_info=raw_order(state="3", dealVol="1", vol="1"))
        v = RECON.reconcile_intent(intent, snap)
        expect("T30-FILL-01: RECORD_ATTEMPT/RECONCILED_FILLED", v["action"] == "RECORD_ATTEMPT" and v["target_state"] == "RECONCILED_FILLED")


def test_fill_close_without_position() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        intent = make_intent(base, "AWAITING_RECONCILIATION", direction="CLOSE_LONG")
        snap = make_snapshot(order_status="FILLED", position_exists=False, raw_order_info=raw_order(state="3", dealVol="1", vol="1"))
        v = RECON.reconcile_intent(intent, snap)
        expect(
            "T30-FILL-02: close intent, no surviving position, STILL RECONCILED_FILLED (order evidence alone suffices)",
            v["action"] == "RECORD_ATTEMPT" and v["target_state"] == "RECONCILED_FILLED",
        )


def test_fill_open_without_position_still_fills() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        intent = make_intent(base, "AWAITING_RECONCILIATION", direction="OPEN_LONG")
        snap = make_snapshot(order_status="FILLED", position_exists=False, raw_order_info=raw_order(state="3", dealVol="1", vol="1"))
        v = RECON.reconcile_intent(intent, snap)
        expect(
            "T30-FILL-03: inconsistent OPEN+no-position still FILLED, not downgraded to conflict",
            v["action"] == "RECORD_ATTEMPT" and v["target_state"] == "RECONCILED_FILLED",
        )
        expect("T30-FILL-03: evidence records the position_exists context", v["evidence"]["position_exists"] is False)


# --------------------------------------------------------------------- #
# T30-PARTIAL-* : the dealVol-first, terminal-vs-open refinement
# --------------------------------------------------------------------- #

def test_partial_on_open_order() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        intent = make_intent(base, "AWAITING_RECONCILIATION")
        snap = make_snapshot(order_status="PARTIALLY_FILLED", raw_status="2", raw_order_info=raw_order(state="2", dealVol="0.4", vol="1"))
        v = RECON.reconcile_intent(intent, snap)
        expect(
            "T30-PARTIAL-OPEN-01: non-terminal, order_terminal_on_exchange False",
            v["action"] == "RECORD_ATTEMPT" and v["target_state"] == "RECONCILED_PARTIALLY_FILLED" and v["order_terminal_on_exchange"] is False,
        )


def test_partial_on_terminal_order() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        intent = make_intent(base, "AWAITING_RECONCILIATION")
        snap = make_snapshot(order_status="UNRESOLVED", raw_status="4", reason="ORDER_STATE_4",
                              raw_order_info=raw_order(state="4", dealVol="0.4", vol="1"))
        v = RECON.reconcile_intent(intent, snap)
        expect(
            "T30-PARTIAL-TERM-01: derived from raw evidence even though .28 said UNRESOLVED, terminal True",
            v["action"] == "RECORD_ATTEMPT" and v["target_state"] == "RECONCILED_PARTIALLY_FILLED" and v["order_terminal_on_exchange"] is True,
        )


def test_cancel_mislabeling_regression() -> None:
    """T30-CANCEL-02: .28 labeled it CANCELED, but raw dealVol shows a
    partial fill -- .30 must NOT trust the CANCELED label."""
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        intent = make_intent(base, "AWAITING_RECONCILIATION")
        snap = make_snapshot(order_status="CANCELED", raw_status="4",
                              raw_order_info=raw_order(state="4", dealVol="0.6", vol="1"))
        v = RECON.reconcile_intent(intent, snap)
        expect(
            "T30-CANCEL-02: never trusts a mislabeled CANCELED when raw dealVol shows a partial fill",
            v["action"] == "RECORD_ATTEMPT" and v["target_state"] == "RECONCILED_PARTIALLY_FILLED",
        )


def test_cancel_clean() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        intent = make_intent(base, "AWAITING_RECONCILIATION")
        snap = make_snapshot(order_status="CANCELED", position_exists=False, raw_status="4",
                              raw_order_info=raw_order(state="4", dealVol="0", vol="1"))
        v = RECON.reconcile_intent(intent, snap)
        expect("T30-CANCEL-01: clean cancel", v["action"] == "RECORD_ATTEMPT" and v["target_state"] == "RECONCILED_CANCELED")


# --------------------------------------------------------------------- #
# T30-PENDING / REJECTED / UNRESOLVED-*
# --------------------------------------------------------------------- #

def test_pending_stays_reconciling() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        intent = make_intent(base, "AWAITING_RECONCILIATION")
        snap = make_snapshot(order_status="PENDING", position_exists=False, fill_price=None, fill_timestamp=None,
                              raw_status="2", raw_order_info=raw_order(state="2", dealVol="0", vol="1"))
        v = RECON.reconcile_intent(intent, snap)
        expect("T30-PENDING-01: RECORD_ATTEMPT/RECONCILING", v["action"] == "RECORD_ATTEMPT" and v["target_state"] == "RECONCILING")


def test_post_ack_rejected_escalates_never_terminal_submission_rejected() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        intent = make_intent(base, "AWAITING_RECONCILIATION")
        snap = make_snapshot(order_status="REJECTED", position_exists=False, fill_price=None, fill_timestamp=None, raw_status=None)
        v = RECON.reconcile_intent(intent, snap)
        expect("T30-REJECTED-01: ESCALATE, never RECORD_ATTEMPT", v["action"] == "ESCALATE")


def test_multiple_matches_escalates() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        intent = make_intent(base, "AWAITING_RECONCILIATION")
        snap = make_snapshot(order_status="UNRESOLVED", match_count=2, position_exists=False,
                              fill_price=None, fill_timestamp=None, raw_status=None,
                              reason="MULTIPLE_MATCHING_ORDERS_FOUND")
        v = RECON.reconcile_intent(intent, snap)
        expect("T30-MULTI-01: ESCALATE immediately", v["action"] == "ESCALATE" and v["reason"] == "MULTIPLE_MATCHING_ORDERS_FOUND")


def test_not_found_stays_reconciling_without_staleness() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        intent = make_intent(base, "AWAITING_RECONCILIATION")
        snap = make_snapshot(order_status="UNRESOLVED", match_count=0, position_exists=False,
                              fill_price=None, fill_timestamp=None, raw_status=None,
                              reason="NO_MATCHING_ORDER_FOUND_IN_WINDOW")
        v = RECON.reconcile_intent(intent, snap)
        expect("T30-NOTFOUND-01: RECORD_ATTEMPT/RECONCILING", v["action"] == "RECORD_ATTEMPT" and v["target_state"] == "RECONCILING")


def test_not_found_escalates_once_stale() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        intent = make_intent(base, "AWAITING_RECONCILIATION")
        snap = make_snapshot(order_status="UNRESOLVED", match_count=0, position_exists=False,
                              fill_price=None, fill_timestamp=None, raw_status=None,
                              reason="NO_MATCHING_ORDER_FOUND_IN_WINDOW")
        far_future = datetime.now(timezone.utc) + timedelta(minutes=1000)
        v = RECON.reconcile_intent(intent, snap, staleness_policy={"reconciliation_staleness_minutes": 5}, now=far_future)
        expect("T30-NOTFOUND-02: ESCALATE once staleness threshold exceeded", v["action"] == "ESCALATE" and "STALENESS" in v["reason"])


def test_staleness_clock_starts_at_awaiting_reconciliation_entry() -> None:
    """The clock must start at the AWAITING_RECONCILIATION entry event's
    OWN timestamp, not at this call's wall-clock time -- a record built
    long ago and only now reconciled for the first time must escalate
    immediately if it's already past the threshold, not get a fresh
    grace period."""
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        intent = make_intent(base, "AWAITING_RECONCILIATION")
        ack_event = next(e for e in intent["events"] if e["event"] == "SUBMISSION_ACKNOWLEDGED")
        expect("staleness fixture: ack event present", ack_event is not None)
        snap = make_snapshot(order_status="UNRESOLVED", match_count=0, position_exists=False,
                              fill_price=None, fill_timestamp=None, raw_status=None,
                              reason="NO_MATCHING_ORDER_FOUND_IN_WINDOW")
        just_past_threshold = RECON._parse_iso(ack_event["at"]) + timedelta(minutes=11)
        v = RECON.reconcile_intent(intent, snap, staleness_policy={"reconciliation_staleness_minutes": 10}, now=just_past_threshold)
        expect("staleness: escalates using the ack event's own timestamp as the clock start", v["action"] == "ESCALATE")


def test_unmapped_raw_state_stays_reconciling() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        intent = make_intent(base, "AWAITING_RECONCILIATION")
        snap = make_snapshot(order_status="UNRESOLVED", match_count=1, position_exists=False,
                              fill_price=None, fill_timestamp=None, raw_status="1",
                              reason="ORDER_STATE_1", raw_order_info=raw_order(state="1", dealVol="0", vol="1"))
        v = RECON.reconcile_intent(intent, snap)
        expect("T30-UNMAPPED-01: RECORD_ATTEMPT/RECONCILING", v["action"] == "RECORD_ATTEMPT" and v["target_state"] == "RECONCILING")


def test_inconsistent_fill_evidence_stays_reconciling() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        intent = make_intent(base, "AWAITING_RECONCILIATION")
        snap = make_snapshot(order_status="UNRESOLVED", match_count=1, position_exists=False,
                              fill_price=None, fill_timestamp=None, raw_status="3",
                              reason="FILL_STATUS_WITHOUT_SUBSTANTIATING_PRICE_OR_TIMESTAMP")
        v = RECON.reconcile_intent(intent, snap)
        expect("T30-INCONSISTENT-01: RECORD_ATTEMPT/RECONCILING", v["action"] == "RECORD_ATTEMPT" and v["target_state"] == "RECONCILING")


def test_unrecognized_status_escalates() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        intent = make_intent(base, "AWAITING_RECONCILIATION")
        snap = make_snapshot(order_status="UNRESOLVED", match_count=1, position_exists=False,
                              fill_price=None, fill_timestamp=None, raw_status=None,
                              reason="UNRECOGNIZED_ORDER_STATUS_VALUE")
        v = RECON.reconcile_intent(intent, snap)
        expect("T30-UNRECOGNIZED-01: ESCALATE immediately", v["action"] == "ESCALATE")


# --------------------------------------------------------------------- #
# verification / precondition failures
# --------------------------------------------------------------------- #

def test_tampered_intent_record_blocks() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        intent = make_intent(base, "AWAITING_RECONCILIATION")
        intent = dict(intent)
        intent["record_hash"] = "not-the-real-hash"
        snap = make_snapshot()
        v = RECON.reconcile_intent(intent, snap)
        expect("T30-VERIFY-01: BLOCKED on tampered intent record", v["action"] == "BLOCKED" and "INTENT_VERIFICATION_FAILED" in v["reason"])


def test_tampered_snapshot_blocks() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        intent = make_intent(base, "AWAITING_RECONCILIATION")
        snap = make_snapshot()
        snap = dict(snap)
        snap["snapshot_hash"] = "not-the-real-hash"
        v = RECON.reconcile_intent(intent, snap)
        expect("T30-VERIFY-02: BLOCKED on tampered snapshot", v["action"] == "BLOCKED" and "SNAPSHOT_VERIFICATION_FAILED" in v["reason"])


def test_precondition_violation_blocks() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        intent = make_intent(base, "SUBMISSION_ATTEMPTED")
        snap = make_snapshot()
        v = RECON.reconcile_intent(intent, snap)
        expect("T30-PRECONDITION-01: BLOCKED, not silently processed", v["action"] == "BLOCKED" and "PRECONDITION_VIOLATION" in v["reason"])


# --------------------------------------------------------------------- #
# apply_verdict -- the one place this module calls .29's mutators
# --------------------------------------------------------------------- #

def test_apply_verdict_record_attempt() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        intent = make_intent(base, "AWAITING_RECONCILIATION")
        snap = make_snapshot(order_status="FILLED", position_exists=True, raw_order_info=raw_order(state="3", dealVol="1", vol="1"))
        v = RECON.reconcile_intent(intent, snap)
        expect("apply_verdict: verdict carries a non-empty observed_snapshot_hash before it reaches .29",
               bool(v.get("observed_snapshot_hash")) and v["observed_snapshot_hash"] == snap["snapshot_hash"])
        updated = RECON.apply_verdict(intent["client_order_id"], v, base_dir=base)
        expect("apply_verdict: RECORD_ATTEMPT reaches RECONCILED_FILLED via .29's own mutator", updated["current_state"] == "RECONCILED_FILLED")
        persisted_fields = updated["events"][-1]["fields"]
        expect("apply_verdict: the persisted RECONCILIATION_ATTEMPT event stores the real snapshot hash, not None",
               persisted_fields.get("observed_snapshot_hash") == snap["snapshot_hash"])


def test_apply_verdict_escalate() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        intent = make_intent(base, "AWAITING_RECONCILIATION")
        snap = make_snapshot(order_status="REJECTED", position_exists=False, fill_price=None, fill_timestamp=None, raw_status=None)
        v = RECON.reconcile_intent(intent, snap)
        updated = RECON.apply_verdict(intent["client_order_id"], v, base_dir=base)
        expect("apply_verdict: ESCALATE reaches ESCALATED_HUMAN_REVIEW via .29's own mutator", updated["current_state"] == "ESCALATED_HUMAN_REVIEW")


def test_apply_verdict_blocked_raises() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        try:
            RECON.apply_verdict("whatever", {"action": "BLOCKED", "reason": "x"}, base_dir=base)
        except RuntimeError:
            print("PASS: apply_verdict: BLOCKED verdict raises rather than being silently applied")
            return
        raise AssertionError("apply_verdict: BLOCKED verdict should raise")


def main() -> int:
    test_fill_open_with_position()
    test_fill_close_without_position()
    test_fill_open_without_position_still_fills()
    test_partial_on_open_order()
    test_partial_on_terminal_order()
    test_cancel_mislabeling_regression()
    test_cancel_clean()
    test_pending_stays_reconciling()
    test_post_ack_rejected_escalates_never_terminal_submission_rejected()
    test_multiple_matches_escalates()
    test_not_found_stays_reconciling_without_staleness()
    test_not_found_escalates_once_stale()
    test_staleness_clock_starts_at_awaiting_reconciliation_entry()
    test_unmapped_raw_state_stays_reconciling()
    test_inconsistent_fill_evidence_stays_reconciling()
    test_unrecognized_status_escalates()
    test_tampered_intent_record_blocks()
    test_tampered_snapshot_blocks()
    test_precondition_violation_blocks()
    test_apply_verdict_record_attempt()
    test_apply_verdict_escalate()
    test_apply_verdict_blocked_raises()
    print("AURA v0.5.3.30 CONTRACT: ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
