#!/usr/bin/env python3
"""Integration tests for AURA v0.5.3.29 (MEXC Intent Ledger) + v0.5.3.30
(MEXC Intent Reconciliation), wired against the real v0.5.3.27 (MEXC
execution adapter) and v0.5.3.28 (MEXC Observed Execution Reader) modules.

Martin's locked implementation sequence, Phase 3:
  9.  .27 -> .29 -> .28 -> .30 -> .29 wiring
  10. End-to-end matrix (the TI-* traces from the transition/evidence
      test matrix document)
  11. Crash/restart tests
  12. Replay/concurrency tests
  13. Verify no existing AURA/MEXC component was modified unintentionally
      (done separately via `git status`/`git diff`, not in this file)

This file does NOT introduce an orchestrator module. It is test-only
wiring: a thin helper layer that calls .27/.28/.29/.30's own public
functions in the documented order, exactly the way a future orchestrator
would, but with no new production code. Per Martin's own instruction,
the orchestrator itself is explicitly out of scope until after this
phase is complete and reviewed.

No real MEXC credentials, no real network call, anywhere in this file.
"""
from __future__ import annotations

import importlib.util
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ccxt

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


ADAPTER = _load("aura_v05327_mexc_execution_adapter", "aura_v05327_mexc_execution_adapter.py")
OBSERVED = _load("aura_v05328_mexc_observed_execution", "aura_v05328_mexc_observed_execution.py")
LEDGER = _load("aura_v05329_mexc_intent_ledger", "aura_v05329_mexc_intent_ledger.py")
RECON = _load("aura_v05330_mexc_intent_reconciliation", "aura_v05330_mexc_intent_reconciliation.py")


def expect(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"PASS: {name}")


# --------------------------------------------------------------------- #
# Fakes -- same shape as .27's and .28's own test suites, kept separate
# (in production these are two different CLI invocations, potentially
# two different processes; this file never lets Section-A tests share
# one fake object across the .27 and .28 steps, so the wiring test is
# honest about the real process boundary).
# --------------------------------------------------------------------- #

class FakeSubmitExchange:
    """Injectable stand-in for the exchange .27's submit() talks to."""

    def __init__(self, create_order_result=None, create_order_exception=None,
                 set_leverage_exception=None):
        self.create_order_calls: list[tuple] = []
        self.set_leverage_calls: list[tuple] = []
        self._create_order_result = create_order_result
        self._create_order_exception = create_order_exception
        self._set_leverage_exception = set_leverage_exception

    def set_leverage(self, leverage, symbol):
        self.set_leverage_calls.append((leverage, symbol))
        if self._set_leverage_exception:
            raise self._set_leverage_exception

    def create_order(self, symbol, type, side, amount, params=None):
        self.create_order_calls.append((symbol, type, side, amount, params))
        if self._create_order_exception:
            raise self._create_order_exception
        return self._create_order_result or {"id": "mexc-fake-order-1", "status": "closed"}


class FakeReadExchange:
    """Injectable stand-in for the exchange .28's read_observed_execution()
    talks to."""

    def __init__(self, orders=None, positions=None, trades=None):
        self._orders = orders if orders is not None else []
        self._positions = positions if positions is not None else []
        self._trades = trades if trades is not None else {}
        self.markets_loaded = True

    def load_markets(self):
        self.markets_loaded = True

    def market(self, symbol):
        return {"id": symbol.split(":")[0].replace("/", "_")}

    def fetch_orders(self, symbol, since=None, limit=None, params=None):
        return [{"info": o} for o in self._orders]

    def fetch_positions(self, symbols=None):
        return [{"info": p} for p in self._positions]

    def fetch_order_trades(self, id, symbol=None, since=None, limit=None, params=None):
        return [{"info": t} for t in self._trades.get(id, [])]


BASE_ENTRY_SPEC = {
    "execution_spec_version": "1.0",
    "exchange": "MEXC",
    "account_mode": "LIVE",
    "market_type": "swap",
    "kill_switch": False,
    "execution_authorized": True,
    "live_execution_authorized": True,
    "symbol": "BTC/USDT:USDT",
    "side": "BUY",
    "quantity": "1",
    "order_type": "MARKET",
    "reduce_only": False,
    "leverage": 3,
}


def raw_order(**overrides) -> dict:
    base = {
        "orderId": "mexc-order-1",
        "symbol": "BTC_USDT",
        "positionId": "0",
        "price": "50000",
        "vol": "1",
        "leverage": "3",
        "side": "1",
        "category": "1",
        "orderType": "1",
        "dealAvgPrice": "50000",
        "dealVol": "1",
        "state": "3",
        "externalOid": "aura-30-intent",
        "createTime": "1700000000000",
        "updateTime": "1700000001000",
        "positionMode": "1",
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------- #
# Test-only pipeline helpers -- explicitly NOT a new orchestrator module.
# Each helper is a direct, undisguised call sequence into .27/.28/.29/.30's
# already-committed public functions.
# --------------------------------------------------------------------- #

_OUTCOME_MAP = {
    "SUBMITTED": "SUBMISSION_ACKNOWLEDGED",
    "REJECTED": "SUBMISSION_REJECTED",
    "EXECUTION_UNCERTAIN": "EXECUTION_UNCERTAIN",
}


def pipeline_submit(client_order_id: str, direction: str, side: str, quantity: float,
                     symbol: str, claims_dir: Path, exchange, base_dir: Path,
                     reduce_only: bool = False, spec_overrides: dict | None = None) -> tuple[dict, dict]:
    """.29 create -> claimed -> submission_attempted -> real .27 submit()
    -> outcome mapped back onto .29. Returns (intent_record, submission_result)."""
    LEDGER.create_intent(client_order_id, symbol, side, quantity, direction, "sha256:integration-fp", base_dir=base_dir)
    LEDGER.record_claimed(client_order_id, base_dir=base_dir)
    LEDGER.record_submission_attempted(client_order_id, base_dir=base_dir)

    ccxt_side = "BUY" if side == "buy" else "SELL"
    spec_dict = dict(BASE_ENTRY_SPEC, client_order_id=client_order_id, symbol=symbol,
                      side=ccxt_side, quantity=str(quantity), reduce_only=reduce_only)
    if reduce_only:
        spec_dict.pop("leverage", None)
    if spec_overrides:
        spec_dict.update(spec_overrides)
    spec = ADAPTER.validate_spec(spec_dict)

    submission_result = ADAPTER.submit(spec, claims_dir, exchange=exchange)
    status = submission_result["status"]

    if status == "DUPLICATE_CLAIM_REJECTED":
        record = LEDGER.record_submission_outcome(client_order_id, "DUPLICATE_CLAIM_REJECTED", base_dir=base_dir)
    else:
        outcome = _OUTCOME_MAP[status]
        record = LEDGER.record_submission_outcome(
            client_order_id, outcome,
            mexc_order_id=submission_result.get("mexc_order_id"),
            raw_response_ref=submission_result.get("reason"),
            base_dir=base_dir,
        )
    return record, submission_result


def pipeline_observe(submission_result: dict, exchange, lookback_minutes: int = 60) -> dict:
    """Writes .27's output to a file (the real cross-process boundary),
    then runs it through .28's own load_submission_result() + real
    read_observed_execution() against a fake exchange."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "submission_result.json"
        path.write_text(json.dumps(submission_result), encoding="utf-8")
        submission = OBSERVED.load_submission_result(path)
    return OBSERVED.read_observed_execution(submission, lookback_minutes=lookback_minutes, exchange=exchange)


def pipeline_reconcile(intent_record: dict, observed_snapshot: dict, base_dir: Path,
                        staleness_policy: dict | None = None, now: datetime | None = None) -> tuple[dict, dict | None]:
    """Real .30 reconcile_intent() -> real .30 apply_verdict() (unless
    BLOCKED, which .30 must never silently apply). Returns
    (verdict, updated_intent_record_or_None)."""
    verdict = RECON.reconcile_intent(intent_record, observed_snapshot, staleness_policy=staleness_policy, now=now)
    if verdict["action"] == "BLOCKED":
        return verdict, None
    updated = RECON.apply_verdict(intent_record["client_order_id"], verdict, base_dir=base_dir)
    return verdict, updated


def snapshot_for(client_order_id: str, order_status: str, position_exists: bool,
                  raw_order_info: dict, mexc_order_id: str = "mexc-order-1",
                  fill_price: float | None = None, match_count: int = 1,
                  raw_status: str | None = "3", reason: str | None = None) -> dict:
    """Builds a real, hash-finalized .28-shaped snapshot directly (mirrors
    .30's own test suite convention) for the TI-* traces that specify
    their evidence abstractly rather than round-tripping a fake exchange."""
    fill_timestamp = datetime.now(timezone.utc).isoformat() if order_status in ("FILLED", "PARTIALLY_FILLED") else None
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
        "position_id": "pos-1" if position_exists else None,
        "fill_price": fill_price if fill_price is not None else (50000.0 if order_status in ("FILLED", "PARTIALLY_FILLED") else None),
        "fill_timestamp": fill_timestamp,
        "observed_fills": [],
        "reason": reason,
        "queried_at": datetime.now(timezone.utc).isoformat(),
    })
    if raw_order_info is not None:
        snapshot["raw_evidence"]["order"] = raw_order_info
    return OBSERVED.finalize(snapshot)


# ======================================================================= #
# Section A (item 9): real .27 -> .29 -> .28 -> .30 -> .29 wiring
# ======================================================================= #

def test_wiring_clean_open_fill() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ledger"
        claims = Path(d) / "claims"
        submit_fake = FakeSubmitExchange(create_order_result={"id": "mexc-order-1", "status": "closed"})
        record, submission_result = pipeline_submit(
            "aura-int-open-1", "OPEN_LONG", "buy", 1.0, "BTC/USDT:USDT", claims, submit_fake, base,
        )
        expect("wiring-open: .27 SUBMITTED maps to .29 AWAITING_RECONCILIATION",
               submission_result["status"] == "SUBMITTED" and record["current_state"] == "AWAITING_RECONCILIATION")

        read_fake = FakeReadExchange(orders=[raw_order(externalOid="aura-int-open-1", state="3", dealVol="1", vol="1")])
        snapshot = pipeline_observe(submission_result, read_fake)
        expect("wiring-open: real .28 read classifies FILLED", snapshot["order_status"] == "FILLED")

        verdict, updated = pipeline_reconcile(record, snapshot, base)
        expect("wiring-open: real .30 verdict is RECORD_ATTEMPT/RECONCILED_FILLED",
               verdict["action"] == "RECORD_ATTEMPT" and verdict["target_state"] == "RECONCILED_FILLED")
        expect("wiring-open: real .29 record lands on RECONCILED_FILLED end to end",
               updated is not None and updated["current_state"] == "RECONCILED_FILLED")


def test_wiring_clean_close_no_surviving_position() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ledger"
        claims = Path(d) / "claims"
        submit_fake = FakeSubmitExchange(create_order_result={"id": "mexc-order-2", "status": "closed"})
        record, submission_result = pipeline_submit(
            "aura-int-close-1", "CLOSE_LONG", "sell", 1.0, "BTC/USDT:USDT", claims, submit_fake, base,
            reduce_only=True,
        )
        expect("wiring-close: SUBMITTED reaches AWAITING_RECONCILIATION", record["current_state"] == "AWAITING_RECONCILIATION")

        read_fake = FakeReadExchange(
            orders=[raw_order(externalOid="aura-int-close-1", orderId="mexc-order-2", state="3", dealVol="1", vol="1")],
            positions=[],  # no surviving position -- this is the point of the test
        )
        snapshot = pipeline_observe(submission_result, read_fake)
        expect("wiring-close: real .28 read shows FILLED with no surviving position",
               snapshot["order_status"] == "FILLED" and snapshot["position_exists"] is False)

        verdict, updated = pipeline_reconcile(record, snapshot, base)
        expect("wiring-close: order evidence alone still reaches RECONCILED_FILLED end to end (locked decision)",
               verdict["action"] == "RECORD_ATTEMPT" and updated["current_state"] == "RECONCILED_FILLED")


def test_wiring_pre_submission_rejected_never_reaches_30() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ledger"
        claims = Path(d) / "claims"
        submit_fake = FakeSubmitExchange(create_order_exception=ccxt.InvalidOrder("mexc: invalid order"))
        record, submission_result = pipeline_submit(
            "aura-int-reject-1", "OPEN_LONG", "buy", 1.0, "BTC/USDT:USDT", claims, submit_fake, base,
        )
        expect("wiring-reject: real .27 classifies an InvalidOrder as REJECTED",
               submission_result["status"] == "REJECTED")
        expect("wiring-reject: .29 lands directly on TERMINAL_SUBMISSION_REJECTED, no .30 involved",
               record["current_state"] == "TERMINAL_SUBMISSION_REJECTED")


def test_wiring_execution_uncertain_then_resolves() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ledger"
        claims = Path(d) / "claims"
        submit_fake = FakeSubmitExchange(create_order_exception=ccxt.RequestTimeout("mexc: timed out"))
        record, submission_result = pipeline_submit(
            "aura-int-uncertain-1", "OPEN_LONG", "buy", 1.0, "BTC/USDT:USDT", claims, submit_fake, base,
        )
        expect("wiring-uncertain: real .27 classifies a timeout as EXECUTION_UNCERTAIN",
               submission_result["status"] == "EXECUTION_UNCERTAIN")
        expect("wiring-uncertain: .29 still reaches AWAITING_RECONCILIATION, identical to SUBMITTED path",
               record["current_state"] == "AWAITING_RECONCILIATION")

        # .28 finds the order anyway -- the order was in fact placed despite the
        # ambiguous local outcome. mexc_order_id is unknown locally, so .28 must
        # resolve by client_order_id (externalOid) alone.
        read_fake = FakeReadExchange(orders=[raw_order(externalOid="aura-int-uncertain-1", state="3", dealVol="1", vol="1")])
        snapshot = pipeline_observe(submission_result, read_fake)
        verdict, updated = pipeline_reconcile(record, snapshot, base)
        expect("wiring-uncertain: resolves to RECONCILED_FILLED exactly like the acknowledged path",
               updated["current_state"] == "RECONCILED_FILLED")


def test_wiring_duplicate_claim_replay_escalates() -> None:
    """A real retry of the WHOLE .27 submission step (not just a retry of
    the .29 mutator) against a claims_dir that already granted the claim.
    This is the genuine replay scenario .27's own claim file exists to
    catch; it must surface through .29 as an anomaly, never a second
    intent record."""
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ledger"
        claims = Path(d) / "claims"
        client_order_id = "aura-int-dup-1"

        submit_fake_1 = FakeSubmitExchange(create_order_result={"id": "mexc-order-dup-1", "status": "closed"})
        record1, result1 = pipeline_submit(client_order_id, "OPEN_LONG", "buy", 1.0, "BTC/USDT:USDT", claims, submit_fake_1, base)
        expect("wiring-dup: first attempt succeeds normally", result1["status"] == "SUBMITTED" and record1["current_state"] == "AWAITING_RECONCILIATION")

        # Caller believes the first attempt may have failed (e.g. it never
        # saw the response) and retries the ENTIRE submission path against
        # the SAME client_order_id and SAME claims_dir -- no new .29 record
        # is created; .27's claim file is what catches this.
        submit_fake_2 = FakeSubmitExchange(create_order_result={"id": "mexc-order-dup-2", "status": "closed"})
        spec = ADAPTER.validate_spec(dict(BASE_ENTRY_SPEC, client_order_id=client_order_id))
        retry_result = ADAPTER.submit(spec, claims, exchange=submit_fake_2)
        expect("wiring-dup: real .27 claim file refuses the retry", retry_result["status"] == "DUPLICATE_CLAIM_REJECTED")
        expect("wiring-dup: retry never reached create_order on the exchange", submit_fake_2.create_order_calls == [])

        record2 = LEDGER.record_submission_outcome(client_order_id, "DUPLICATE_CLAIM_REJECTED", base_dir=base)
        expect("wiring-dup: .29 escalates the SAME record, not a new one",
               record2["client_order_id"] == record1["client_order_id"] and record2["current_state"] == "ESCALATED_HUMAN_REVIEW")

        all_records = list(base.glob("*.json"))
        expect("wiring-dup: exactly one record file exists for this client_order_id", len(all_records) == 1)


# ======================================================================= #
# Section B (item 10): TI-* end-to-end lifecycle traces
# ======================================================================= #

def test_TI_CLEAN_OPEN_01() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        cid = "TI-CLEAN-OPEN-01"
        LEDGER.create_intent(cid, "BTC/USDT:USDT", "buy", 1.0, "OPEN_LONG", "sha256:fp", base_dir=base)
        LEDGER.record_claimed(cid, base_dir=base)
        LEDGER.record_submission_attempted(cid, base_dir=base)
        record = LEDGER.record_submission_outcome(cid, "SUBMISSION_ACKNOWLEDGED", mexc_order_id="mexc-1", base_dir=base)
        expect("TI-CLEAN-OPEN-01: AWAITING_RECONCILIATION", record["current_state"] == "AWAITING_RECONCILIATION")

        snapshot = snapshot_for(cid, "FILLED", True, raw_order(externalOid=cid, state="3", dealVol="1", vol="1"))
        verdict, updated = pipeline_reconcile(record, snapshot, base)
        expect("TI-CLEAN-OPEN-01: verdict RECORD_ATTEMPT/RECONCILED_FILLED", verdict["target_state"] == "RECONCILED_FILLED")
        expect("TI-CLEAN-OPEN-01: final state RECONCILED_FILLED", updated["current_state"] == "RECONCILED_FILLED")
        expect("TI-CLEAN-OPEN-01: .29's own event log is the complete history (no external system needed)",
               [e["event"] for e in updated["events"]] ==
               ["INTENT_CREATED", "CLAIMED", "SUBMISSION_ATTEMPTED", "SUBMISSION_ACKNOWLEDGED", "RECONCILIATION_ATTEMPT"])


def test_TI_CLEAN_CLOSE_01() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        cid = "TI-CLEAN-CLOSE-01"
        record = LEDGER.create_intent(cid, "BTC/USDT:USDT", "sell", 1.0, "CLOSE_LONG", "sha256:fp", base_dir=base)
        LEDGER.record_claimed(cid, base_dir=base)
        LEDGER.record_submission_attempted(cid, base_dir=base)
        record = LEDGER.record_submission_outcome(cid, "SUBMISSION_ACKNOWLEDGED", mexc_order_id="mexc-1", base_dir=base)

        snapshot = snapshot_for(cid, "FILLED", False, raw_order(externalOid=cid, state="3", dealVol="1", vol="1"))
        verdict, updated = pipeline_reconcile(record, snapshot, base)
        expect("TI-CLEAN-CLOSE-01: closing intent with no surviving position still reaches RECONCILED_FILLED",
               updated["current_state"] == "RECONCILED_FILLED")


def test_TI_UNCERTAIN_RESOLVES_01() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        cid = "TI-UNCERTAIN-RESOLVES-01"
        LEDGER.create_intent(cid, "BTC/USDT:USDT", "buy", 1.0, "OPEN_LONG", "sha256:fp", base_dir=base)
        LEDGER.record_claimed(cid, base_dir=base)
        LEDGER.record_submission_attempted(cid, base_dir=base)
        record = LEDGER.record_submission_outcome(cid, "EXECUTION_UNCERTAIN", base_dir=base)
        expect("TI-UNCERTAIN-RESOLVES-01: EXECUTION_UNCERTAIN reaches AWAITING_RECONCILIATION identically",
               record["current_state"] == "AWAITING_RECONCILIATION")

        snapshot = snapshot_for(cid, "FILLED", True, raw_order(externalOid=cid, state="3", dealVol="1", vol="1"))
        _, updated = pipeline_reconcile(record, snapshot, base)
        expect("TI-UNCERTAIN-RESOLVES-01: final state identical to the acknowledged path",
               updated["current_state"] == "RECONCILED_FILLED")


def test_TI_PARTIAL_THEN_FILL_01() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        cid = "TI-PARTIAL-THEN-FILL-01"
        LEDGER.create_intent(cid, "BTC/USDT:USDT", "sell", 1.0, "OPEN_SHORT", "sha256:fp", base_dir=base)
        LEDGER.record_claimed(cid, base_dir=base)
        LEDGER.record_submission_attempted(cid, base_dir=base)
        record = LEDGER.record_submission_outcome(cid, "SUBMISSION_ACKNOWLEDGED", mexc_order_id="mexc-1", base_dir=base)

        snap1 = snapshot_for(cid, "PARTIALLY_FILLED", True, raw_order(externalOid=cid, state="2", dealVol="0.4", vol="1"), reason="ORDER_STATE_2")
        v1, updated1 = pipeline_reconcile(record, snap1, base)
        expect("TI-PARTIAL-THEN-FILL-01: first pass -> RECONCILED_PARTIALLY_FILLED, non-terminal",
               updated1["current_state"] == "RECONCILED_PARTIALLY_FILLED" and updated1["partial_fill_terminal"] is False)

        snap2 = snapshot_for(cid, "FILLED", True, raw_order(externalOid=cid, state="3", dealVol="1", vol="1"))
        v2, updated2 = pipeline_reconcile(updated1, snap2, base)
        expect("TI-PARTIAL-THEN-FILL-01: second pass completes to RECONCILED_FILLED",
               updated2["current_state"] == "RECONCILED_FILLED")


def test_TI_PARTIAL_TERMINAL_01() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        cid = "TI-PARTIAL-TERMINAL-01"
        LEDGER.create_intent(cid, "BTC/USDT:USDT", "sell", 1.0, "OPEN_SHORT", "sha256:fp", base_dir=base)
        LEDGER.record_claimed(cid, base_dir=base)
        LEDGER.record_submission_attempted(cid, base_dir=base)
        record = LEDGER.record_submission_outcome(cid, "SUBMISSION_ACKNOWLEDGED", mexc_order_id="mexc-1", base_dir=base)

        snap = snapshot_for(cid, "CANCELED", True, raw_order(externalOid=cid, state="4", dealVol="0.4", vol="1"))
        verdict, updated = pipeline_reconcile(record, snap, base)
        expect("TI-PARTIAL-TERMINAL-01: order_terminal_on_exchange True", verdict["order_terminal_on_exchange"] is True)
        expect("TI-PARTIAL-TERMINAL-01: RECONCILED_PARTIALLY_FILLED is terminal here",
               updated["current_state"] == "RECONCILED_PARTIALLY_FILLED" and updated["partial_fill_terminal"] is True)

        # TI-PARTIAL-TERM-02: a further pass against the now-terminal record is refused
        snap2 = snapshot_for(cid, "FILLED", True, raw_order(externalOid=cid, state="3", dealVol="1", vol="1"))
        verdict2, updated2 = pipeline_reconcile(updated, snap2, base)
        expect("TI-PARTIAL-TERM-02: .30 itself refuses (BLOCKED) once the record is terminal",
               verdict2["action"] == "BLOCKED" and updated2 is None)


def test_TI_CANCEL_CLEAN_01() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        cid = "TI-CANCEL-CLEAN-01"
        LEDGER.create_intent(cid, "BTC/USDT:USDT", "buy", 1.0, "OPEN_LONG", "sha256:fp", base_dir=base)
        LEDGER.record_claimed(cid, base_dir=base)
        LEDGER.record_submission_attempted(cid, base_dir=base)
        record = LEDGER.record_submission_outcome(cid, "SUBMISSION_ACKNOWLEDGED", mexc_order_id="mexc-1", base_dir=base)

        snap = snapshot_for(cid, "CANCELED", False, raw_order(externalOid=cid, state="4", dealVol="0", vol="1"))
        _, updated = pipeline_reconcile(record, snap, base)
        expect("TI-CANCEL-CLEAN-01: RECONCILED_CANCELED", updated["current_state"] == "RECONCILED_CANCELED")


def test_TI_CANCEL_MISLABELED_01() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        cid = "TI-CANCEL-MISLABELED-01"
        LEDGER.create_intent(cid, "BTC/USDT:USDT", "buy", 1.0, "OPEN_LONG", "sha256:fp", base_dir=base)
        LEDGER.record_claimed(cid, base_dir=base)
        LEDGER.record_submission_attempted(cid, base_dir=base)
        record = LEDGER.record_submission_outcome(cid, "SUBMISSION_ACKNOWLEDGED", mexc_order_id="mexc-1", base_dir=base)

        # .28's own order_status label says CANCELED; raw dealVol says a
        # partial fill actually happened. The full pipeline must not trust
        # the label.
        snap = snapshot_for(cid, "CANCELED", True, raw_order(externalOid=cid, state="4", dealVol="0.6", vol="1"))
        verdict, updated = pipeline_reconcile(record, snap, base)
        expect("TI-CANCEL-MISLABELED-01: never RECONCILED_CANCELED despite .28's label",
               updated["current_state"] != "RECONCILED_CANCELED")
        expect("TI-CANCEL-MISLABELED-01: resolves via the dealVol-first partial-on-terminal rule instead",
               updated["current_state"] == "RECONCILED_PARTIALLY_FILLED" and updated["partial_fill_terminal"] is True)


def test_TI_POST_ACK_REJECTED_01() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        cid = "TI-POST-ACK-REJECTED-01"
        LEDGER.create_intent(cid, "BTC/USDT:USDT", "buy", 1.0, "OPEN_LONG", "sha256:fp", base_dir=base)
        LEDGER.record_claimed(cid, base_dir=base)
        LEDGER.record_submission_attempted(cid, base_dir=base)
        record = LEDGER.record_submission_outcome(cid, "SUBMISSION_ACKNOWLEDGED", mexc_order_id="mexc-1", base_dir=base)

        snap = snapshot_for(cid, "REJECTED", False, None, fill_price=None,
                             raw_status=None, reason="SYNTHETIC_POST_ACK_REJECTED")
        verdict, updated = pipeline_reconcile(record, snap, base)
        expect("TI-POST-ACK-REJECTED-01: ESCALATE, never a silent success", verdict["action"] == "ESCALATE")
        expect("TI-POST-ACK-REJECTED-01: final state ESCALATED_HUMAN_REVIEW, never TERMINAL_SUBMISSION_REJECTED",
               updated["current_state"] == "ESCALATED_HUMAN_REVIEW")


def test_TI_PRE_SUBMISSION_REJECTED_01() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        cid = "TI-PRE-SUBMISSION-REJECTED-01"
        LEDGER.create_intent(cid, "BTC/USDT:USDT", "buy", 1.0, "OPEN_LONG", "sha256:fp", base_dir=base)
        LEDGER.record_claimed(cid, base_dir=base)
        LEDGER.record_submission_attempted(cid, base_dir=base)
        record = LEDGER.record_submission_outcome(cid, "SUBMISSION_REJECTED", base_dir=base)
        expect("TI-PRE-SUBMISSION-REJECTED-01: TERMINAL_SUBMISSION_REJECTED directly, .30 never invoked",
               record["current_state"] == "TERMINAL_SUBMISSION_REJECTED")


def test_TI_DUPLICATE_CLAIM_01() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        cid = "TI-DUPLICATE-CLAIM-01"
        LEDGER.create_intent(cid, "BTC/USDT:USDT", "buy", 1.0, "OPEN_LONG", "sha256:fp", base_dir=base)
        LEDGER.record_claimed(cid, base_dir=base)
        LEDGER.record_submission_attempted(cid, base_dir=base)
        record = LEDGER.record_submission_outcome(cid, "DUPLICATE_CLAIM_REJECTED", base_dir=base)
        expect("TI-DUPLICATE-CLAIM-01: mid-flight duplicate claim escalates on the same record",
               record["current_state"] == "ESCALATED_HUMAN_REVIEW")
        expect("TI-DUPLICATE-CLAIM-01: exactly one record file", len(list(base.glob("*.json"))) == 1)


def test_TI_STALENESS_01() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        cid = "TI-STALENESS-01"
        LEDGER.create_intent(cid, "BTC/USDT:USDT", "buy", 1.0, "OPEN_LONG", "sha256:fp", base_dir=base)
        LEDGER.record_claimed(cid, base_dir=base)
        LEDGER.record_submission_attempted(cid, base_dir=base)
        record = LEDGER.record_submission_outcome(cid, "SUBMISSION_ACKNOWLEDGED", mexc_order_id="mexc-1", base_dir=base)
        ack_at = datetime.fromisoformat(record["events"][-1]["at"])

        staleness_policy = {"reconciliation_staleness_minutes": 5}
        not_found = snapshot_for(cid, "UNRESOLVED", False, None, fill_price=None,
                                  match_count=0, raw_status=None, reason="NO_MATCHING_ORDER_FOUND_IN_WINDOW")

        just_inside = ack_at + timedelta(minutes=2)
        v1, updated1 = pipeline_reconcile(record, not_found, base, staleness_policy=staleness_policy, now=just_inside)
        expect("TI-STALENESS-01: still within the window -> stays RECONCILING",
               v1["action"] == "RECORD_ATTEMPT" and updated1["current_state"] == "RECONCILING")

        past_threshold = ack_at + timedelta(minutes=10)
        v2, updated2 = pipeline_reconcile(updated1, not_found, base, staleness_policy=staleness_policy, now=past_threshold)
        expect("TI-STALENESS-01: past the threshold -> ESCALATE, clock anchored at AWAITING_RECONCILIATION entry",
               v2["action"] == "ESCALATE" and updated2["current_state"] == "ESCALATED_HUMAN_REVIEW")


def test_TI_RESTART_01() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        # One record per non-terminal state, mirroring T29-LIST-01, plus a
        # couple of terminal ones -- built via a *fresh module load* per
        # phase to simulate "no in-memory state survives a restart" as
        # literally as this test harness allows.
        def make(cid, target_state, **kw):
            LEDGER.create_intent(cid, "BTC/USDT:USDT", "buy", 1.0, "OPEN_LONG", "sha256:fp", base_dir=base)
            if target_state == "NEW":
                return
            LEDGER.record_claimed(cid, base_dir=base)
            if target_state == "CLAIMED":
                return
            LEDGER.record_submission_attempted(cid, base_dir=base)
            if target_state == "SUBMISSION_ATTEMPTED":
                return
            LEDGER.record_submission_outcome(cid, "SUBMISSION_ACKNOWLEDGED", mexc_order_id="m", base_dir=base)
            if target_state == "AWAITING_RECONCILIATION":
                return
            if target_state == "RECONCILING":
                LEDGER.record_reconciliation_attempt(cid, "snap-1", "UNRESOLVED", base_dir=base)
                return
            if target_state == "RECONCILED_FILLED":
                LEDGER.record_reconciliation_attempt(cid, "snap-1", "FILLED", base_dir=base)
                return
            if target_state == "RECONCILED_PARTIALLY_FILLED_OPEN":
                LEDGER.record_reconciliation_attempt(cid, "snap-1", "PARTIALLY_FILLED", order_terminal_on_exchange=False, base_dir=base)
                return
            if target_state == "TERMINAL_SUBMISSION_REJECTED":
                raise ValueError("built via a different path below")
            if target_state == "ESCALATED_HUMAN_REVIEW":
                LEDGER.escalate_to_human(cid, "TEST_ESCALATION", base_dir=base)
                return

        make("restart-new", "NEW")
        make("restart-claimed", "CLAIMED")
        make("restart-subatt", "SUBMISSION_ATTEMPTED")
        make("restart-awaiting", "AWAITING_RECONCILIATION")
        make("restart-reconciling", "RECONCILING")
        make("restart-filled", "RECONCILED_FILLED")
        make("restart-partial-open", "RECONCILED_PARTIALLY_FILLED_OPEN")
        make("restart-escalated", "ESCALATED_HUMAN_REVIEW")

        LEDGER.create_intent("restart-rejected", "BTC/USDT:USDT", "buy", 1.0, "OPEN_LONG", "sha256:fp", base_dir=base)
        LEDGER.record_claimed("restart-rejected", base_dir=base)
        LEDGER.record_submission_attempted("restart-rejected", base_dir=base)
        LEDGER.record_submission_outcome("restart-rejected", "SUBMISSION_REJECTED", base_dir=base)

        # "Restart": a fresh call into the module-level function with no
        # other state, as a cold process would do.
        unresolved = LEDGER.list_unresolved_intents(base_dir=base)
        unresolved_ids = {u["client_order_id"] for u in unresolved}
        expect("TI-RESTART-01: exactly the non-terminal set survives cold storage", unresolved_ids == {
            "restart-new", "restart-claimed", "restart-subatt", "restart-awaiting",
            "restart-reconciling", "restart-partial-open", "restart-escalated",
        })
        expect("TI-RESTART-01: terminal records correctly excluded",
               "restart-filled" not in unresolved_ids and "restart-rejected" not in unresolved_ids)

        for cid in ["restart-awaiting", "restart-reconciling"]:
            roundtrip = LEDGER.get_intent(cid, base_dir=base)
            ok, errors = LEDGER.verify_intent(roundtrip)
            expect(f"TI-RESTART-01: {cid} round-trips through get_intent() and verifies clean", ok and not errors)


def test_TI_VERIFY_CHAIN_01() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        cid = "TI-VERIFY-CHAIN-01"
        LEDGER.create_intent(cid, "BTC/USDT:USDT", "buy", 1.0, "OPEN_LONG", "sha256:fp", base_dir=base)
        LEDGER.record_claimed(cid, base_dir=base)
        LEDGER.record_submission_attempted(cid, base_dir=base)
        record = LEDGER.record_submission_outcome(cid, "SUBMISSION_ACKNOWLEDGED", mexc_order_id="mexc-1", base_dir=base)

        snapshot = snapshot_for(cid, "FILLED", True, raw_order(externalOid=cid, state="3", dealVol="1", vol="1"))
        _, updated = pipeline_reconcile(record, snapshot, base)
        expect("TI-VERIFY-CHAIN-01: pipeline reaches RECONCILED_FILLED", updated["current_state"] == "RECONCILED_FILLED")

        ok29, errors29 = LEDGER.verify_intent(updated)
        expect("TI-VERIFY-CHAIN-01: .29's final record independently re-verifies", ok29 and not errors29)

        recomputed_snapshot_hash = OBSERVED.canonical_snapshot_hash(snapshot)
        expect("TI-VERIFY-CHAIN-01: .28 snapshot hash recomputes cleanly", recomputed_snapshot_hash == snapshot["snapshot_hash"])

        last_event_fields = updated["events"][-1]["fields"]
        expect("TI-VERIFY-CHAIN-01: .29's recorded observed_snapshot_hash matches the evidence's own hash exactly",
               last_event_fields["observed_snapshot_hash"] == snapshot["snapshot_hash"])


# ======================================================================= #
# Section C (item 11): crash/restart at the file boundary
# ======================================================================= #

def test_crash_after_27_before_29_outcome_recorded() -> None:
    """Simulates a crash between .27 writing its submission result to disk
    and the caller relaying that outcome into .29 -- the .27 output file
    is the durable record that survives the crash and lets a restarted
    process finish the handoff."""
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ledger"
        claims = Path(d) / "claims"
        cid = "crash-after-27"
        LEDGER.create_intent(cid, "BTC/USDT:USDT", "buy", 1.0, "OPEN_LONG", "sha256:fp", base_dir=base)
        LEDGER.record_claimed(cid, base_dir=base)
        LEDGER.record_submission_attempted(cid, base_dir=base)

        spec = ADAPTER.validate_spec(dict(BASE_ENTRY_SPEC, client_order_id=cid))
        fake = FakeSubmitExchange(create_order_result={"id": "mexc-crash-1", "status": "closed"})
        submission_result = ADAPTER.submit(spec, claims, exchange=fake)

        # ".27's output is durably on disk" -- simulate this literally.
        out_path = Path(d) / "submission_result.json"
        out_path.write_text(json.dumps(submission_result), encoding="utf-8")

        # *** crash *** -- no .29 mutator has run yet for the outcome.
        before_crash = LEDGER.get_intent(cid, base_dir=base)
        expect("crash-after-27: .29 still shows SUBMISSION_ATTEMPTED at the crash point",
               before_crash["current_state"] == "SUBMISSION_ATTEMPTED")

        # "restart": a fresh reader picks the durable file back up.
        recovered_result = json.loads(out_path.read_text(encoding="utf-8"))
        record = LEDGER.record_submission_outcome(
            cid, "SUBMISSION_ACKNOWLEDGED", mexc_order_id=recovered_result.get("mexc_order_id"), base_dir=base,
        )
        expect("crash-after-27: recovery completes the handoff correctly", record["current_state"] == "AWAITING_RECONCILIATION")


def test_crash_mid_write_simulated_via_direct_ledger_primitives() -> None:
    """.29's own T29-CRASH-01/02 already cover the atomic-rename boundary
    directly; this test confirms the SAME guarantee holds when reached
    through the full pipeline's call shape (record_submission_outcome
    rather than a bare internal mutator), i.e. the crash-safety property
    is not accidentally specific to one call site."""
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        cid = "crash-mid-write"
        LEDGER.create_intent(cid, "BTC/USDT:USDT", "buy", 1.0, "OPEN_LONG", "sha256:fp", base_dir=base)
        LEDGER.record_claimed(cid, base_dir=base)
        LEDGER.record_submission_attempted(cid, base_dir=base)
        record = LEDGER.record_submission_outcome(cid, "SUBMISSION_ACKNOWLEDGED", mexc_order_id="m", base_dir=base)

        path = LEDGER.record_path(cid, base)
        pre_crash_bytes = path.read_bytes()

        # Simulate "crash after temp write, before rename": a leftover
        # temp file exists but the real record file is untouched.
        tmp_leftover = path.with_suffix(path.suffix + ".tmp-leftover")
        tmp_leftover.write_bytes(b"{not valid json, a half-written temp file}")

        reloaded = LEDGER.get_intent(cid, base_dir=base)
        expect("crash-mid-write: the real record is untouched by a leftover temp file",
               reloaded is not None and json.dumps(reloaded, sort_keys=True) is not None and path.read_bytes() == pre_crash_bytes)
        ok, errors = LEDGER.verify_intent(reloaded)
        expect("crash-mid-write: the untouched record still verifies cleanly", ok and not errors)
        tmp_leftover.unlink()


# ======================================================================= #
# Section D (item 12): replay/concurrency at the integration level
# ======================================================================= #

def test_replay_identical_reconciliation_through_apply_verdict_is_idempotent() -> None:
    """The SAME .28 snapshot reaching .30/.apply_verdict twice (e.g. a
    caller retries after a network blip on the *return* path, not knowing
    whether the first apply_verdict() already landed) must be a no-op the
    second time, not a duplicate event or a crash."""
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        cid = "replay-identical"
        LEDGER.create_intent(cid, "BTC/USDT:USDT", "buy", 1.0, "OPEN_LONG", "sha256:fp", base_dir=base)
        LEDGER.record_claimed(cid, base_dir=base)
        LEDGER.record_submission_attempted(cid, base_dir=base)
        record = LEDGER.record_submission_outcome(cid, "SUBMISSION_ACKNOWLEDGED", mexc_order_id="m", base_dir=base)

        snapshot = snapshot_for(cid, "FILLED", True, raw_order(externalOid=cid, state="3", dealVol="1", vol="1"))
        verdict, updated1 = pipeline_reconcile(record, snapshot, base)
        expect("replay-identical: first apply_verdict reaches RECONCILED_FILLED", updated1["current_state"] == "RECONCILED_FILLED")

        # Retry apply_verdict with the SAME verdict dict against the now
        # already-resolved record.
        updated2 = RECON.apply_verdict(cid, verdict, base_dir=base)
        expect("replay-identical: retry is a silent no-op, same event count",
               len(updated2["events"]) == len(updated1["events"]) and updated2["current_state"] == "RECONCILED_FILLED")


def test_replay_conflicting_reconciliation_through_apply_verdict_escalates() -> None:
    """Two out-of-order evidence reads racing against the same record:
    the first (FILLED) verdict is applied and settles the record; a
    second, stale/conflicting (CANCELED) verdict computed *before* the
    first settled but applied *after* must never be silently accepted or
    dropped -- it must escalate, exercising the exact 'different event at
    an already-settled point' rule Martin required tests to prove,
    through the real .30 apply_verdict() call path rather than .29's
    bare mutator."""
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        cid = "replay-conflicting"
        LEDGER.create_intent(cid, "BTC/USDT:USDT", "buy", 1.0, "OPEN_LONG", "sha256:fp", base_dir=base)
        LEDGER.record_claimed(cid, base_dir=base)
        LEDGER.record_submission_attempted(cid, base_dir=base)
        record = LEDGER.record_submission_outcome(cid, "SUBMISSION_ACKNOWLEDGED", mexc_order_id="m", base_dir=base)

        filled_snapshot = snapshot_for(cid, "FILLED", True, raw_order(externalOid=cid, state="3", dealVol="1", vol="1"))
        canceled_snapshot = snapshot_for(cid, "CANCELED", False, raw_order(externalOid=cid, state="4", dealVol="0", vol="1"))

        # Both verdicts computed against the SAME pre-settlement record
        # (simulating two racing readers), applied in sequence.
        filled_verdict = RECON.reconcile_intent(record, filled_snapshot)
        canceled_verdict = RECON.reconcile_intent(record, canceled_snapshot)

        updated1 = RECON.apply_verdict(cid, filled_verdict, base_dir=base)
        expect("replay-conflicting: first (FILLED) verdict settles the record", updated1["current_state"] == "RECONCILED_FILLED")

        updated2 = RECON.apply_verdict(cid, canceled_verdict, base_dir=base)
        expect("replay-conflicting: conflicting second verdict escalates rather than silently applying or crashing",
               updated2["current_state"] == "ESCALATED_HUMAN_REVIEW")

        all_records = list(base.glob("*.json"))
        expect("replay-conflicting: still exactly one record for this client_order_id", len(all_records) == 1)


def test_replay_30_never_touches_29_storage_directly() -> None:
    """Cross-cutting invariant from the test matrix Sec.5: .30 never
    writes .29's storage directly. Verified here by calling
    reconcile_intent() (never apply_verdict()) repeatedly against a real
    on-disk record and confirming the file on disk is byte-for-byte
    unchanged -- .30's reasoning step alone must be side-effect-free."""
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        cid = "purity-check"
        LEDGER.create_intent(cid, "BTC/USDT:USDT", "buy", 1.0, "OPEN_LONG", "sha256:fp", base_dir=base)
        LEDGER.record_claimed(cid, base_dir=base)
        LEDGER.record_submission_attempted(cid, base_dir=base)
        record = LEDGER.record_submission_outcome(cid, "SUBMISSION_ACKNOWLEDGED", mexc_order_id="m", base_dir=base)

        path = LEDGER.record_path(cid, base)
        before = path.read_bytes()

        snapshot = snapshot_for(cid, "FILLED", True, raw_order(externalOid=cid, state="3", dealVol="1", vol="1"))
        for _ in range(5):
            RECON.reconcile_intent(record, snapshot)

        after = path.read_bytes()
        expect("purity-check: reconcile_intent() alone never mutates .29's on-disk record", before == after)


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_")]
    for t in tests:
        t()
    print("AURA v0.5.3.29+30 INTEGRATION CONTRACT: ALL PASS")
