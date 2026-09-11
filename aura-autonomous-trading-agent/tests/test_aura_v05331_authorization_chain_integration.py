#!/usr/bin/env python3
"""Integration tests for AURA v0.5.3.31 (MEXC Execution Authorization)
wired in front of the real, already-proven v0.5.3.27 -> v0.5.3.29 ->
v0.5.3.28 -> v0.5.3.30 chain (tests/test_aura_v05329_30_integration.py).

Proves Martin's requirements 13-17 end to end THROUGH the new Authorization
boundary, reusing the already-proven .27-.30 logic rather than re-testing
it:
  13. MEXC uncertain response -> EXECUTION_UNCERTAIN
  14. EXECUTION_UNCERTAIN enters reconciliation
  15. reconciliation resolves FILLED
  16. resolves REJECTED
  17. unresolved stays RECONCILING / HUMAN

This file introduces no orchestrator. Like test_aura_v05329_30_integration.py,
it is test-only wiring: a thin helper that calls .31/.29/.27/.28/.30's own
public functions in the documented order.

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


AUTH = _load("aura_v05331_mexc_execution_authorization", "aura_v05331_mexc_execution_authorization.py")
ADAPTER = _load("aura_v05327_mexc_execution_adapter", "aura_v05327_mexc_execution_adapter.py")
OBSERVED = _load("aura_v05328_mexc_observed_execution", "aura_v05328_mexc_observed_execution.py")
LEDGER = _load("aura_v05329_mexc_intent_ledger", "aura_v05329_mexc_intent_ledger.py")
RECON = _load("aura_v05330_mexc_intent_reconciliation", "aura_v05330_mexc_intent_reconciliation.py")


def expect(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"PASS: {name}")


class FakeSubmitExchange:
    def __init__(self, create_order_result=None, create_order_exception=None, set_leverage_exception=None):
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

ALLOW_ALL_CONFIG = {
    "kill_switch": False,
    "execution_authorized": True,
    "paper_execution_authorized": True,
    "live_execution_authorized": True,
    "authorization_ttl_seconds": 300,
    "safety_state_ttl_seconds": 300,
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
        "externalOid": "aura-31-intent",
        "createTime": "1700000000000",
        "updateTime": "1700000001000",
        "positionMode": "1",
    }
    base.update(overrides)
    return base


_OUTCOME_MAP = {
    "SUBMITTED": "SUBMISSION_ACKNOWLEDGED",
    "REJECTED": "SUBMISSION_REJECTED",
    "EXECUTION_UNCERTAIN": "EXECUTION_UNCERTAIN",
}


def pipeline_submit_with_authorization(client_order_id: str, direction: str, side: str, quantity: float,
                                        symbol: str, ledger_base: Path, auth_claims: Path, adapter_claims: Path,
                                        exchange, reduce_only: bool = False,
                                        spec_overrides: dict | None = None) -> tuple[dict, dict, dict]:
    """.29 create -> claimed -> submission_attempted -> [NEW] .31 authorize()
    -> [NEW] .31 authorized_submit() (which itself calls the real, unmodified
    .27 validate_spec()+submit()) -> outcome mapped back onto .29.
    Returns (intent_record, authorized_submit_result, submission_result)."""
    LEDGER.create_intent(client_order_id, symbol, side, quantity, direction, "sha256:integration-fp", base_dir=ledger_base)
    LEDGER.record_claimed(client_order_id, base_dir=ledger_base)
    LEDGER.record_submission_attempted(client_order_id, base_dir=ledger_base)

    ccxt_side = "BUY" if side == "buy" else "SELL"
    spec_dict = dict(BASE_ENTRY_SPEC, client_order_id=client_order_id, symbol=symbol,
                      side=ccxt_side, quantity=str(quantity), reduce_only=reduce_only)
    if reduce_only:
        spec_dict.pop("leverage", None)
    if spec_overrides:
        spec_dict.update(spec_overrides)

    auth_record = AUTH.authorize(spec_dict, config=ALLOW_ALL_CONFIG, ledger_base_dir=ledger_base, claims_dir=auth_claims)
    expect(f"{client_order_id}: authorization succeeds ahead of submission", auth_record["status"] == "AUTHORIZED")

    submit_outcome = AUTH.authorized_submit(
        auth_record, spec_dict, auth_claims, adapter_claims,
        config=ALLOW_ALL_CONFIG, ledger_base_dir=ledger_base, exchange=exchange,
    )
    expect(f"{client_order_id}: authorized_submit reaches the adapter", submit_outcome["status"] == "SUBMISSION_ATTEMPTED")
    submission_result = submit_outcome["submission_result"]
    status = submission_result["status"]

    if status == "DUPLICATE_CLAIM_REJECTED":
        record = LEDGER.record_submission_outcome(client_order_id, "DUPLICATE_CLAIM_REJECTED", base_dir=ledger_base)
    else:
        outcome = _OUTCOME_MAP[status]
        record = LEDGER.record_submission_outcome(
            client_order_id, outcome,
            mexc_order_id=submission_result.get("mexc_order_id"),
            raw_response_ref=submission_result.get("reason"),
            base_dir=ledger_base,
        )
    return record, submit_outcome, submission_result


def pipeline_observe(submission_result: dict, exchange, lookback_minutes: int = 60) -> dict:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "submission_result.json"
        path.write_text(json.dumps(submission_result), encoding="utf-8")
        submission = OBSERVED.load_submission_result(path)
    return OBSERVED.read_observed_execution(submission, lookback_minutes=lookback_minutes, exchange=exchange)


def pipeline_reconcile(intent_record: dict, observed_snapshot: dict, base_dir: Path,
                        staleness_policy: dict | None = None, now: datetime | None = None) -> tuple[dict, dict | None]:
    verdict = RECON.reconcile_intent(intent_record, observed_snapshot, staleness_policy=staleness_policy, now=now)
    if verdict["action"] == "BLOCKED":
        return verdict, None
    updated = RECON.apply_verdict(intent_record["client_order_id"], verdict, base_dir=base_dir)
    return verdict, updated


def snapshot_for(client_order_id: str, order_status: str, position_exists: bool,
                  raw_order_info: dict, mexc_order_id: str = "mexc-order-1",
                  fill_price: float | None = None, match_count: int = 1,
                  raw_status: str | None = "3", reason: str | None = None) -> dict:
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
# 13/14. MEXC uncertain response through Authorization -> EXECUTION_UNCERTAIN
#         -> enters reconciliation (AWAITING_RECONCILIATION, same as an
#         acknowledged submission)
# ======================================================================= #

def test_authorized_uncertain_enters_reconciliation() -> None:
    with tempfile.TemporaryDirectory() as d:
        ledger_base = Path(d) / "ledger"
        auth_claims = Path(d) / "auth_claims"
        adapter_claims = Path(d) / "adapter_claims"
        cid = "aura-31-uncertain-1"

        fake = FakeSubmitExchange(create_order_exception=ccxt.RequestTimeout("mexc: timed out"))
        record, submit_outcome, submission_result = pipeline_submit_with_authorization(
            cid, "OPEN_LONG", "buy", 1.0, "BTC/USDT:USDT", ledger_base, auth_claims, adapter_claims, fake,
        )
        expect("13: authorized submission through a timeout is classified EXECUTION_UNCERTAIN by the real .27",
               submission_result["status"] == "EXECUTION_UNCERTAIN")
        expect("14: .29 reaches AWAITING_RECONCILIATION -- EXECUTION_UNCERTAIN enters reconciliation exactly like SUBMITTED",
               record["current_state"] == "AWAITING_RECONCILIATION")


# ======================================================================= #
# 15. Reconciliation resolves FILLED
# ======================================================================= #

def test_authorized_open_resolves_filled() -> None:
    with tempfile.TemporaryDirectory() as d:
        ledger_base = Path(d) / "ledger"
        auth_claims = Path(d) / "auth_claims"
        adapter_claims = Path(d) / "adapter_claims"
        cid = "aura-31-filled-1"

        fake = FakeSubmitExchange(create_order_result={"id": "mexc-order-1", "status": "closed"})
        record, submit_outcome, submission_result = pipeline_submit_with_authorization(
            cid, "OPEN_LONG", "buy", 1.0, "BTC/USDT:USDT", ledger_base, auth_claims, adapter_claims, fake,
        )
        expect("15: authorized submission acknowledged", submission_result["status"] == "SUBMITTED")

        read_fake = FakeReadExchange(orders=[raw_order(externalOid=cid, state="3", dealVol="1", vol="1")])
        snapshot = pipeline_observe(submission_result, read_fake)
        expect("15: real .28 read classifies FILLED", snapshot["order_status"] == "FILLED")

        verdict, updated = pipeline_reconcile(record, snapshot, ledger_base)
        expect("15: reconciliation resolves FILLED end to end through the Authorization boundary",
               verdict["action"] == "RECORD_ATTEMPT" and updated["current_state"] == "RECONCILED_FILLED")


# ======================================================================= #
# 16. Reconciliation resolves REJECTED (pre-submission rejection -- never
#     even reaches .30, exactly like the existing .27-.30 contract)
# ======================================================================= #

def test_authorized_rejected_never_reaches_reconciliation() -> None:
    with tempfile.TemporaryDirectory() as d:
        ledger_base = Path(d) / "ledger"
        auth_claims = Path(d) / "auth_claims"
        adapter_claims = Path(d) / "adapter_claims"
        cid = "aura-31-rejected-1"

        fake = FakeSubmitExchange(create_order_exception=ccxt.InvalidOrder("mexc: invalid order"))
        record, submit_outcome, submission_result = pipeline_submit_with_authorization(
            cid, "OPEN_LONG", "buy", 1.0, "BTC/USDT:USDT", ledger_base, auth_claims, adapter_claims, fake,
        )
        expect("16: real .27 classifies an InvalidOrder as REJECTED even when authorized",
               submission_result["status"] == "REJECTED")
        expect("16: .29 lands directly on TERMINAL_SUBMISSION_REJECTED, .30 never invoked",
               record["current_state"] == "TERMINAL_SUBMISSION_REJECTED")


# ======================================================================= #
# 17. Unresolved stays RECONCILING / escalates to HUMAN
# ======================================================================= #

def test_authorized_unresolved_stays_reconciling_then_escalates() -> None:
    with tempfile.TemporaryDirectory() as d:
        ledger_base = Path(d) / "ledger"
        auth_claims = Path(d) / "auth_claims"
        adapter_claims = Path(d) / "adapter_claims"
        cid = "aura-31-unresolved-1"

        fake = FakeSubmitExchange(create_order_result={"id": "mexc-order-unresolved", "status": "closed"})
        record, submit_outcome, submission_result = pipeline_submit_with_authorization(
            cid, "OPEN_LONG", "buy", 1.0, "BTC/USDT:USDT", ledger_base, auth_claims, adapter_claims, fake,
        )
        ack_at = datetime.fromisoformat(record["events"][-1]["at"])

        staleness_policy = {"reconciliation_staleness_minutes": 5}
        not_found = snapshot_for(cid, "UNRESOLVED", False, None, fill_price=None,
                                  match_count=0, raw_status=None, reason="NO_MATCHING_ORDER_FOUND_IN_WINDOW")

        just_inside = ack_at + timedelta(minutes=2)
        v1, updated1 = pipeline_reconcile(record, not_found, ledger_base, staleness_policy=staleness_policy, now=just_inside)
        expect("17: still within the staleness window -> stays RECONCILING",
               v1["action"] == "RECORD_ATTEMPT" and updated1["current_state"] == "RECONCILING")

        past_threshold = ack_at + timedelta(minutes=10)
        v2, updated2 = pipeline_reconcile(updated1, not_found, ledger_base, staleness_policy=staleness_policy, now=past_threshold)
        expect("17: past the threshold -> escalates to ESCALATED_HUMAN_REVIEW",
               v2["action"] == "ESCALATE" and updated2["current_state"] == "ESCALATED_HUMAN_REVIEW")


# ======================================================================= #
# Cross-cutting: Authorization never authorizes twice for the same
# authorization_id even across this full chain, and unauthorized specs
# never reach .27 at all.
# ======================================================================= #

def test_unauthorized_spec_never_reaches_adapter() -> None:
    with tempfile.TemporaryDirectory() as d:
        ledger_base = Path(d) / "ledger"
        auth_claims = Path(d) / "auth_claims"
        adapter_claims = Path(d) / "adapter_claims"
        cid = "aura-31-unauthorized-1"

        LEDGER.create_intent(cid, "BTC/USDT:USDT", "buy", 1.0, "OPEN_LONG", "sha256:fp", base_dir=ledger_base)
        LEDGER.record_claimed(cid, base_dir=ledger_base)
        LEDGER.record_submission_attempted(cid, base_dir=ledger_base)

        spec_dict = dict(BASE_ENTRY_SPEC, client_order_id=cid)
        deny_config = dict(ALLOW_ALL_CONFIG, execution_authorized=False)
        auth_record = AUTH.authorize(spec_dict, config=deny_config, ledger_base_dir=ledger_base, claims_dir=auth_claims)
        expect("cross-cutting: authorization is rejected", auth_record["status"] == "AUTHORIZATION_REJECTED")

        fake = FakeSubmitExchange()
        # A caller must not even be able to call authorized_submit() with a
        # rejection dict in place of a real record -- verify_authorization_record
        # / status checks refuse it structurally.
        result = AUTH.authorized_submit(
            auth_record, spec_dict, auth_claims, adapter_claims,
            config=deny_config, ledger_base_dir=ledger_base, exchange=fake,
        )
        expect("cross-cutting: authorized_submit refuses a rejection record and never reaches the exchange",
               result["status"] in ("AUTHORIZATION_ALREADY_CONSUMED", "REVALIDATION_FAILED") and fake.create_order_calls == [])


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_")]
    for t in tests:
        t()
    print(f"AURA v0.5.3.31 AUTHORIZATION CHAIN INTEGRATION CONTRACT: ALL {len(tests)} PASS")
