#!/usr/bin/env python3
"""Contract + unit tests for AURA v0.5.3.28 MEXC Observed Execution Reader.

Mirrors v0.5.3.27's test style: CLI-subprocess tests for the
no-network-call path (input validation, the --query-live double-gate),
and direct-import unit tests against an injected FakeExchange for the
actual read/classification logic — a fake is required here too, since
subprocess testing can't inject a fake exchange without a real network
attempt.

No real MEXC credentials, no real network call, anywhere in this file.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "aura_v05328_mexc_observed_execution.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("aura_v05328_mexc_observed_execution", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


MOD = _load_module()


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_submission_result(**overrides) -> dict:
    base = {
        "adapter_version": "AURA v0.5.3.27",
        "status": "SUBMITTED",
        "client_order_id": "aura-test-05328-001",
        "symbol": "BTC/USDT:USDT",
        "side": "BUY",
        "reduce_only": False,
        "mexc_order_id": "mexc-order-1",
        "raw_response": {"id": "mexc-order-1"},
        "observed_at": iso_now(),
        "live": True,
    }
    base.update(overrides)
    return base


def raw_order(**overrides) -> dict:
    """A raw MEXC swap order info dict (the shape resolve_order() reads
    from order['info']), matching the ccxt source's own documented shape."""
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
        "externalOid": "aura-test-05328-001",
        "createTime": "1700000000000",
        "updateTime": "1700000001000",
        "positionMode": "1",
    }
    base.update(overrides)
    return base


class FakeExchange:
    """Injectable stand-in for ccxt.mexc — no network I/O. Every method
    returns pre-canned data or records what it was asked to fetch.

    markets_loaded models the real ccxt constraint the code review found:
    market() raises ExchangeError if markets haven't been loaded (ccxt
    does NOT auto-load there), while fetch_orders/fetch_positions/
    fetch_order_trades each self-guard with their own internal
    load_markets() call, matching ccxt/mexc.py's actual behavior exactly
    (verified by direct source reading, not assumed). Defaults to True so
    every existing test's fake reflects a fully-initialized exchange, the
    same state build_exchange() now guarantees via its own load_markets()
    call (Fix 1) -- markets_loaded=False is for the regression test that
    exercises this constraint directly."""

    def __init__(self, orders=None, positions=None, trades=None, markets_loaded=True):
        self._orders = orders if orders is not None else []
        self._positions = positions if positions is not None else []
        self._trades = trades if trades is not None else {}
        self.markets_loaded = markets_loaded
        self.load_markets_calls = 0
        self.fetch_orders_calls: list[tuple] = []
        self.fetch_positions_calls: list[tuple] = []
        self.fetch_order_trades_calls: list[tuple] = []

    def load_markets(self):
        self.load_markets_calls += 1
        self.markets_loaded = True

    def market(self, symbol):
        if not self.markets_loaded:
            import ccxt
            raise ccxt.ExchangeError("mexc markets not loaded")
        return {"id": symbol.split(":")[0].replace("/", "_")}

    def fetch_orders(self, symbol, since=None, limit=None, params=None):
        self.markets_loaded = True  # mirrors ccxt/mexc.py's own internal guard
        self.fetch_orders_calls.append((symbol, since))
        return [{"info": o} for o in self._orders]

    def fetch_positions(self, symbols=None):
        self.markets_loaded = True  # mirrors ccxt/mexc.py's own internal guard
        self.fetch_positions_calls.append(tuple(symbols) if symbols else None)
        return [{"info": p} for p in self._positions]

    def fetch_order_trades(self, id, symbol=None, since=None, limit=None, params=None):
        self.markets_loaded = True  # mirrors ccxt/mexc.py's own internal guard
        self.fetch_order_trades_calls.append((id, symbol))
        return [{"info": t} for t in self._trades.get(id, [])]


def expect(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"PASS: {name}")


# --------------------------------------------------------------------- #
# CLI-level contract tests: no network call, input validation, gating
# --------------------------------------------------------------------- #

def run_cli(submission: dict, *extra: str) -> tuple[int, dict]:
    with tempfile.TemporaryDirectory() as d:
        inp = Path(d) / "submission.json"
        out = Path(d) / "out.json"
        inp.write_text(json.dumps(submission), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(MODULE_PATH), "--submission-input", str(inp), "--output", str(out), *extra],
            cwd=ROOT, text=True, capture_output=True,
        )
        return proc.returncode, json.loads(out.read_text(encoding="utf-8"))


def test_cli_contract() -> None:
    # 1. valid submission result, default invocation (no --query-live): validates only
    rc, result = run_cli(make_submission_result())
    expect("1. default invocation validates without querying MEXC",
           rc == 0 and result["snapshot_status"] == "VALIDATED_NO_QUERY")
    expect("1. no order_status is asserted without a query", result["order_status"] is None)

    # 2. wrong adapter_version in submission result
    rc, result = run_cli(make_submission_result(adapter_version="AURA v0.5.3.99"))
    expect("2. wrong source adapter_version fails closed",
           rc == 1 and result["snapshot_status"] == "BLOCKED"
           and "WRONG_SUBMISSION_SOURCE_VERSION" in result["reason"])

    # 3. a DUPLICATE_CLAIM_REJECTED submission has nothing to observe
    rc, result = run_cli(make_submission_result(status="DUPLICATE_CLAIM_REJECTED"))
    expect("3. duplicate-claim submission result refused",
           rc == 1 and "NOTHING_TO_OBSERVE_FOR_STATUS" in result["reason"])

    # 4. missing client_order_id
    bad = make_submission_result()
    bad.pop("client_order_id")
    rc, result = run_cli(bad)
    expect("4. missing client_order_id fails closed",
           rc == 1 and "MISSING_CLIENT_ORDER_ID_IN_SUBMISSION_RESULT" in result["reason"])

    # 5. missing symbol
    bad = make_submission_result()
    bad.pop("symbol")
    rc, result = run_cli(bad)
    expect("5. missing symbol fails closed",
           rc == 1 and "MISSING_SYMBOL_IN_SUBMISSION_RESULT" in result["reason"])

    # 6. missing observed_at (needed to bound the lookback window)
    bad = make_submission_result()
    bad.pop("observed_at")
    rc, result = run_cli(bad)
    expect("6. missing observed_at fails closed",
           rc == 1 and "MISSING_OBSERVED_AT_IN_SUBMISSION_RESULT" in result["reason"])

    # 7. --query-live without the env-var double-gate still fails closed,
    # regardless of MEXC_API_KEY/SECRET or network availability.
    rc, result = run_cli(make_submission_result(), "--query-live")
    expect("7. --query-live without AURA_MEXC_OBSERVATION_ENABLED fails closed",
           rc == 1 and "LIVE_OBSERVATION_QUERY_NOT_ENABLED" in result["reason"])

    # 8. REJECTED submission is still a valid thing to (later) observe —
    # refusing only DUPLICATE_CLAIM_REJECTED/FAIL_CLOSED/BLOCKED/VALIDATED_NO_SUBMISSION
    rc, result = run_cli(make_submission_result(status="REJECTED"))
    expect("8. a REJECTED submission result validates (nothing to refuse)",
           rc == 0 and result["snapshot_status"] == "VALIDATED_NO_QUERY")

    # 9. EXECUTION_UNCERTAIN submission is exactly the motivating case
    rc, result = run_cli(make_submission_result(status="EXECUTION_UNCERTAIN"))
    expect("9. an EXECUTION_UNCERTAIN submission result validates",
           rc == 0 and result["snapshot_status"] == "VALIDATED_NO_QUERY")

    # 10. every BLOCKED-shaped output still carries a verifiable snapshot_hash
    bad = make_submission_result()
    bad.pop("client_order_id")
    rc, result = run_cli(bad)
    expect("10. a BLOCKED snapshot still carries a snapshot_hash",
           isinstance(result.get("snapshot_hash"), str) and len(result["snapshot_hash"]) == 64)


# --------------------------------------------------------------------- #
# Direct unit tests: read_observed_execution() against a fake exchange
# --------------------------------------------------------------------- #

def _parse(payload: dict) -> dict:
    """Mirrors load_submission_result()'s parsing without requiring a file
    on disk, for direct unit tests."""
    submitted_dt = datetime.fromisoformat(payload["observed_at"].replace("Z", "+00:00"))
    if submitted_dt.tzinfo is None:
        submitted_dt = submitted_dt.replace(tzinfo=timezone.utc)
    return {
        "client_order_id": payload["client_order_id"],
        "symbol": payload["symbol"],
        "submitted_at": payload["observed_at"],
        "submitted_at_dt": submitted_dt,
        "submission_status": payload["status"],
        "submission_mexc_order_id": payload.get("mexc_order_id"),
    }


def test_no_match_is_unresolved() -> None:
    """11. No order in the lookback window matches the client_order_id ->
    UNRESOLVED, never REJECTED. Absence isn't evidence of rejection."""
    submission = _parse(make_submission_result())
    fake = FakeExchange(orders=[], positions=[])
    snapshot = MOD.read_observed_execution(submission, exchange=fake)
    expect("11a. zero matches classifies as UNRESOLVED", snapshot["order_status"] == "UNRESOLVED")
    expect("11b. match_count is 0", snapshot["match_count"] == 0)
    expect("11c. reason names the absence", snapshot["reason"] == "NO_MATCHING_ORDER_FOUND_IN_WINDOW")


def test_multiple_matches_is_conflict() -> None:
    """12. More than one order in the window carries the same externalOid
    -> UNRESOLVED (conflict), never silently pick one."""
    submission = _parse(make_submission_result())
    fake = FakeExchange(orders=[raw_order(orderId="a"), raw_order(orderId="b")])
    snapshot = MOD.read_observed_execution(submission, exchange=fake)
    expect("12a. multiple matches classifies as UNRESOLVED", snapshot["order_status"] == "UNRESOLVED")
    expect("12b. match_count is 2", snapshot["match_count"] == 2)
    expect("12c. reason names the conflict", snapshot["reason"] == "MULTIPLE_MATCHING_ORDERS_FOUND")


def test_open_no_fill_is_pending() -> None:
    """13. Single match, raw state '2' (open), dealVol 0 -> PENDING."""
    submission = _parse(make_submission_result())
    fake = FakeExchange(orders=[raw_order(state="2", dealVol="0")])
    snapshot = MOD.read_observed_execution(submission, exchange=fake)
    expect("13a. open + no fill classifies as PENDING", snapshot["order_status"] == "PENDING")
    expect("13b. raw_status recorded", snapshot["raw_status"] == "2")
    expect("13c. no fill price asserted", snapshot["fill_price"] is None)


def test_open_partial_fill_is_partially_filled() -> None:
    """14. Single match, raw state '2' (open), 0 < dealVol < vol -> PARTIALLY_FILLED."""
    submission = _parse(make_submission_result())
    fake = FakeExchange(orders=[raw_order(state="2", vol="1", dealVol="0.4")])
    snapshot = MOD.read_observed_execution(submission, exchange=fake)
    expect("14. open + partial fill classifies as PARTIALLY_FILLED",
           snapshot["order_status"] == "PARTIALLY_FILLED")


def test_closed_full_fill_is_filled_with_evidence() -> None:
    """15. Single match, raw state '3' (closed), dealVol == vol -> FILLED,
    with fill_price/fill_timestamp populated from the order's own
    dealAvgPrice/updateTime, and observed_fills populated from trade
    evidence — never a value this module computed itself."""
    submission = _parse(make_submission_result())
    order = raw_order(state="3", vol="1", dealVol="1", dealAvgPrice="51000", updateTime="1700000002000")
    fake = FakeExchange(
        orders=[order],
        trades={"mexc-order-1": [{"id": "t1", "price": "51000", "vol": "1", "fee": "0.5", "timestamp": "1700000002000"}]},
    )
    snapshot = MOD.read_observed_execution(submission, exchange=fake)
    expect("15a. closed + full fill classifies as FILLED", snapshot["order_status"] == "FILLED")
    expect("15b. fill_price comes from dealAvgPrice", snapshot["fill_price"] == 51000.0)
    expect("15c. fill_timestamp is populated", snapshot["fill_timestamp"] is not None)
    expect("15d. observed_fills carries the trade evidence", len(snapshot["observed_fills"]) == 1)
    expect("15e. fetch_order_trades was called with the resolved MEXC order id",
           fake.fetch_order_trades_calls == [("mexc-order-1", "BTC/USDT:USDT")])
    expect("15f. raw_evidence['trades'] carries the verbatim raw trade dict, not just the curated subset",
           snapshot["raw_evidence"]["trades"] == [
               {"id": "t1", "price": "51000", "vol": "1", "fee": "0.5", "timestamp": "1700000002000"}
           ])


def test_canceled() -> None:
    """16. Single match, raw state '4' -> CANCELED."""
    submission = _parse(make_submission_result())
    fake = FakeExchange(orders=[raw_order(state="4")])
    snapshot = MOD.read_observed_execution(submission, exchange=fake)
    expect("16. raw state 4 classifies as CANCELED", snapshot["order_status"] == "CANCELED")


def test_unmapped_raw_state_is_unresolved() -> None:
    """17. Raw states '1' and '5' -- unmapped even by ccxt itself -- must
    never be guessed into a recognized status."""
    submission = _parse(make_submission_result())
    for unmapped in ("1", "5", "99"):
        fake = FakeExchange(orders=[raw_order(state=unmapped)])
        snapshot = MOD.read_observed_execution(submission, exchange=fake)
        expect(f"17. unmapped raw state '{unmapped}' classifies as UNRESOLVED, never guessed",
               snapshot["order_status"] == "UNRESOLVED")


def test_closed_without_matching_volume_is_unresolved() -> None:
    """18. Raw state '3' (closed) but dealVol != vol -- inconsistent
    evidence, must not be reported as FILLED."""
    submission = _parse(make_submission_result())
    fake = FakeExchange(orders=[raw_order(state="3", vol="1", dealVol="0.5")])
    snapshot = MOD.read_observed_execution(submission, exchange=fake)
    expect("18. closed with mismatched volumes classifies as UNRESOLVED, not FILLED",
           snapshot["order_status"] == "UNRESOLVED")


def test_position_exists_and_absent() -> None:
    """19. position_exists/position_id come from fetch_positions(), matched
    on the raw market id, present only when holdVol > 0."""
    submission = _parse(make_submission_result())

    fake_present = FakeExchange(
        orders=[raw_order(state="3", vol="1", dealVol="1")],
        positions=[{"symbol": "BTC_USDT", "positionId": "778", "holdVol": "1"}],
        trades={"mexc-order-1": []},
    )
    snapshot = MOD.read_observed_execution(submission, exchange=fake_present)
    expect("19a. an open position is reported present", snapshot["position_exists"] is True)
    expect("19b. position_id is a string", snapshot["position_id"] == "778")

    fake_absent = FakeExchange(orders=[raw_order(state="4")], positions=[])
    snapshot2 = MOD.read_observed_execution(submission, exchange=fake_absent)
    expect("19c. absence from fetch_positions() reports position_exists=False",
           snapshot2["position_exists"] is False)
    expect("19d. position_id is None when absent", snapshot2["position_id"] is None)


def test_fill_status_requires_substantiating_evidence() -> None:
    """20. A FILLED/PARTIALLY_FILLED classification with no usable
    dealAvgPrice/updateTime must fail closed to UNRESOLVED rather than
    report a fill AURA can't substantiate."""
    submission = _parse(make_submission_result())
    fake = FakeExchange(orders=[raw_order(state="3", vol="1", dealVol="1", dealAvgPrice="0", updateTime=None)])
    snapshot = MOD.read_observed_execution(submission, exchange=fake)
    expect("20. FILLED without a substantiating price/timestamp falls back to UNRESOLVED",
           snapshot["order_status"] == "UNRESOLVED")
    expect("20b. reason names the inconsistency",
           snapshot["reason"] == "FILL_STATUS_WITHOUT_SUBSTANTIATING_PRICE_OR_TIMESTAMP")


def test_snapshot_hash_is_verifiable() -> None:
    """21. Every emitted snapshot's snapshot_hash matches a recomputation
    of the same canonical fields — the same discipline as every other
    stage in the chain."""
    submission = _parse(make_submission_result())
    fake = FakeExchange(orders=[raw_order(state="4")])
    snapshot = MOD.read_observed_execution(submission, exchange=fake)
    recomputed = MOD.canonical_snapshot_hash(snapshot)
    expect("21. snapshot_hash matches an independent recomputation",
           snapshot["snapshot_hash"] == recomputed)


def test_lookback_window_bounds_the_query() -> None:
    """22. fetch_orders() is called with a `since` timestamp that is
    exactly (submitted_at - lookback_minutes) earlier, confirming the
    lookback window is actually applied, not just documented."""
    submitted_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    submission = _parse(make_submission_result(observed_at=submitted_at.isoformat()))
    fake = FakeExchange(orders=[raw_order(state="4")])
    MOD.read_observed_execution(submission, lookback_minutes=30, exchange=fake)
    expected_since_ms = int((submitted_at - timedelta(minutes=30)).timestamp() * 1000)
    expect("22. fetch_orders since= matches submitted_at - lookback_minutes",
           fake.fetch_orders_calls[0][1] == expected_since_ms)


# --------------------------------------------------------------------- #
# Regression tests added after the 2026-09-09 code review (Fix 1/2/3)
# --------------------------------------------------------------------- #

def test_market_without_load_markets_raises_then_succeeds() -> None:
    """23. Reproduces the exact defect the code review found and the
    original 22 tests missed: ccxt's real market() raises ExchangeError
    if markets haven't been loaded (it does NOT auto-load) -- resolve_
    position() calling exchange.market() directly used to only work
    because resolve_order()'s fetch_orders() call happened to load markets
    first. This proves that constraint is real (23a), and that
    build_exchange() (Fix 1) now closes it at the source by loading
    markets unconditionally, independent of call order (23b/23c)."""
    import ccxt

    # 23a. resolve_position() on a fresh, not-yet-loaded exchange raises --
    # confirming the underlying ccxt constraint the fake now models
    # accurately (it did not before this fix; all 22 original tests would
    # have passed even with this defect present).
    fake = FakeExchange(positions=[{"symbol": "BTC_USDT", "positionId": "1", "holdVol": "1"}],
                         markets_loaded=False)
    raised = False
    try:
        MOD.resolve_position(fake, "BTC/USDT:USDT")
    except ccxt.ExchangeError:
        raised = True
    expect("23a. resolve_position() on unloaded markets raises ExchangeError, matching real ccxt",
           raised)

    # 23b. after load_markets(), the same call succeeds.
    fake.load_markets()
    result = MOD.resolve_position(fake, "BTC/USDT:USDT")
    expect("23b. resolve_position() succeeds once markets are loaded",
           result["position_exists"] is True)

    # 23c. build_exchange() itself now calls load_markets() unconditionally
    # -- verified against the real construction path, not the FakeExchange,
    # by swapping in a network-free stand-in for ccxt.mexc for one call.
    class FakeMexcClass:
        def __init__(self, config):
            self.config = config
            self.load_markets_called = False

        def load_markets(self):
            self.load_markets_called = True

    original_mexc = ccxt.mexc
    ccxt.mexc = FakeMexcClass
    try:
        exchange = MOD.build_exchange("key", "secret")
        expect("23c. build_exchange() calls load_markets() before returning, independent of call order",
               exchange.load_markets_called is True)
    finally:
        ccxt.mexc = original_mexc


def test_raw_trade_evidence_survives_unchanged() -> None:
    """24. Raw trade evidence must be preserved verbatim, not just the
    curated observed_fills subset (code review finding #6) -- including
    fields curate_fills() does not carry forward, proving nothing is lost
    in translation."""
    submission = _parse(make_submission_result())
    raw_trade = {
        "id": "t99", "symbol": "BTC_USDT", "side": "1", "vol": "1", "price": "51000",
        "feeCurrency": "USDT", "fee": "0.51", "timestamp": "1700000002000", "profit": "12.5",
        "category": "1", "orderId": "mexc-order-1", "positionMode": "1", "taker": True,
    }
    order = raw_order(state="3", vol="1", dealVol="1", dealAvgPrice="51000", updateTime="1700000002000")
    fake = FakeExchange(orders=[order], trades={"mexc-order-1": [raw_trade]})
    snapshot = MOD.read_observed_execution(submission, exchange=fake)

    expect("24a. raw_evidence['trades'] preserves the full raw trade dict, byte-for-byte",
           snapshot["raw_evidence"]["trades"] == [raw_trade])
    expect("24b. fields curate_fills() drops (symbol, side, profit, taker, ...) still exist in raw_evidence",
           snapshot["raw_evidence"]["trades"][0]["profit"] == "12.5"
           and snapshot["raw_evidence"]["trades"][0]["taker"] is True)
    expect("24c. observed_fills remains the curated subset, unaffected by the raw preservation",
           snapshot["observed_fills"] == [
               {"trade_id": "t99", "price": "51000", "vol": "1", "fee": "0.51", "timestamp": "1700000002000"}
           ])


def test_status_allowlist_enforced_at_finalize() -> None:
    """25. finalize() must defensively normalize any OBSERVED snapshot
    carrying an order_status outside ALLOWED_ORDER_STATUSES to UNRESOLVED
    (code review finding: the allowlist existed but was never actively
    checked). Exercised by calling finalize() directly with a hand-crafted
    unrecognized value, since classify_order_status() never produces one
    by construction today -- this is defense in depth against a FUTURE
    ccxt/MEXC change or an internal bug, not today's normal flow."""
    bogus = MOD.base_snapshot()
    bogus["snapshot_status"] = "OBSERVED"
    bogus["order_status"] = "SOME_FUTURE_MEXC_STATUS_AURA_DOES_NOT_KNOW"
    bogus["fill_price"] = 12345.0
    bogus["fill_timestamp"] = "2026-01-01T00:00:00+00:00"
    result = MOD.finalize(bogus)
    expect("25a. an unrecognized order_status is forced to UNRESOLVED",
           result["order_status"] == "UNRESOLVED")
    expect("25b. fill_price is cleared alongside the forced downgrade",
           result["fill_price"] is None)
    expect("25c. fill_timestamp is cleared alongside the forced downgrade",
           result["fill_timestamp"] is None)
    expect("25d. the original unrecognized value is preserved for audit, never silently dropped",
           result["raw_evidence"]["unrecognized_order_status"] == "SOME_FUTURE_MEXC_STATUS_AURA_DOES_NOT_KNOW")

    # A recognized value must pass through unchanged -- the check is a
    # floor, not a rewrite of legitimate output.
    ok = MOD.base_snapshot()
    ok["snapshot_status"] = "OBSERVED"
    ok["order_status"] = "FILLED"
    ok["fill_price"] = 100.0
    ok["fill_timestamp"] = "2026-01-01T00:00:00+00:00"
    result_ok = MOD.finalize(ok)
    expect("25e. a recognized order_status passes through finalize() unchanged",
           result_ok["order_status"] == "FILLED" and result_ok["fill_price"] == 100.0)

    # snapshot_status != "OBSERVED" (e.g. VALIDATED_NO_QUERY/BLOCKED) must
    # never be forced into UNRESOLVED just for having order_status=None.
    not_observed = MOD.base_snapshot()
    not_observed["snapshot_status"] = "VALIDATED_NO_QUERY"
    result_no = MOD.finalize(not_observed)
    expect("25f. a non-OBSERVED snapshot's order_status=None is left alone, not forced to UNRESOLVED",
           result_no["order_status"] is None)


def main() -> int:
    test_cli_contract()
    test_no_match_is_unresolved()
    test_multiple_matches_is_conflict()
    test_open_no_fill_is_pending()
    test_open_partial_fill_is_partially_filled()
    test_closed_full_fill_is_filled_with_evidence()
    test_canceled()
    test_unmapped_raw_state_is_unresolved()
    test_closed_without_matching_volume_is_unresolved()
    test_position_exists_and_absent()
    test_fill_status_requires_substantiating_evidence()
    test_snapshot_hash_is_verifiable()
    test_lookback_window_bounds_the_query()
    test_market_without_load_markets_raises_then_succeeds()
    test_raw_trade_evidence_survives_unchanged()
    test_status_allowlist_enforced_at_finalize()
    print("AURA v0.5.3.28 CONTRACT: ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
