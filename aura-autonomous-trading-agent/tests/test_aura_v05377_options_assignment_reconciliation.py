#!/usr/bin/env python3
"""Tests for AURA v0.5.3.77 -- Options Assignment/Exercise Reconciliation
(O4B).

No live Alpaca order or position is required anywhere in this file.
Positions and activities are faked test doubles (SimpleNamespace, duck-
typed exactly like the real alpaca-py Position/NonTradeActivity
objects); only `fetch_broker_activities()`'s construction of the real
`NonTradeActivity` model is exercised against realistic raw dicts, to
prove the parsing path is real, not just assumed.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aura_v05377_options_assignment_reconciliation as R  # noqa: E402

AS_OF = datetime(2026, 10, 6, tzinfo=timezone.utc)
_UID = "11111111-1111-1111-1111-111111111111"


def _pos(symbol, qty, side, asset_class="us_option"):
    return SimpleNamespace(symbol=symbol, qty=qty, side=side, asset_class=asset_class)


def _act(symbol, activity_type, d, status="executed", qty="1", net_amount="0"):
    return SimpleNamespace(
        symbol=symbol, activity_type=activity_type, date=d, status=status,
        qty=qty, net_amount=net_amount,
    )


# ======================================================================
# normalize_expected_position() -- caller-input validation, fails closed
# ======================================================================

def test_normalize_expected_position_valid():
    meta = R._load_options_metadata_module()
    result = R.normalize_expected_position({"occ_symbol": "AAPL261218C00150000", "qty": "1"}, meta)
    assert result["underlying_symbol"] == "AAPL"
    assert result["qty"] == Decimal("1")


def test_normalize_expected_position_not_a_dict():
    meta = R._load_options_metadata_module()
    with pytest.raises(RuntimeError, match="INVALID_EXPECTED_POSITION_ENTRY"):
        R.normalize_expected_position("nope", meta)


def test_normalize_expected_position_missing_occ_symbol():
    meta = R._load_options_metadata_module()
    with pytest.raises(RuntimeError, match="MISSING_OCC_SYMBOL_IN_EXPECTED_POSITION"):
        R.normalize_expected_position({"qty": "1"}, meta)


def test_normalize_expected_position_malformed_occ_symbol():
    meta = R._load_options_metadata_module()
    with pytest.raises(RuntimeError, match="MALFORMED_OCC_SYMBOL"):
        R.normalize_expected_position({"occ_symbol": "NOT_AN_OCC_SYMBOL", "qty": "1"}, meta)


def test_normalize_expected_position_invalid_qty():
    meta = R._load_options_metadata_module()
    with pytest.raises(RuntimeError, match="INVALID_EXPECTED_QTY"):
        R.normalize_expected_position({"occ_symbol": "AAPL261218C00150000", "qty": "nan"}, meta)


def test_normalize_expected_position_allows_negative_and_zero_qty():
    meta = R._load_options_metadata_module()
    short = R.normalize_expected_position({"occ_symbol": "AAPL261218C00150000", "qty": "-2"}, meta)
    assert short["qty"] == Decimal("-2")
    flat = R.normalize_expected_position({"occ_symbol": "AAPL261218C00150000", "qty": "0"}, meta)
    assert flat["qty"] == Decimal("0")


# ======================================================================
# normalize_broker_position() -- broker-side anomaly handling, never
# raises
# ======================================================================

def test_normalize_broker_position_valid_long():
    meta = R._load_options_metadata_module()
    result = R.normalize_broker_position(_pos("AAPL261218C00150000", "1", "long"), meta)
    assert result["qty"] == Decimal("1")
    assert result["underlying_symbol"] == "AAPL"
    assert result["problem"] is None


def test_normalize_broker_position_valid_short():
    meta = R._load_options_metadata_module()
    result = R.normalize_broker_position(_pos("AAPL261218C00150000", "1", "short"), meta)
    assert result["qty"] == Decimal("-1")


def test_normalize_broker_position_non_option_returns_none():
    meta = R._load_options_metadata_module()
    result = R.normalize_broker_position(_pos("SPY", "10", "long", asset_class="us_equity"), meta)
    assert result is None


def test_normalize_broker_position_unparseable_symbol_never_raises():
    meta = R._load_options_metadata_module()
    result = R.normalize_broker_position(_pos("GARBAGE", "1", "long"), meta)
    assert result["problem"].startswith("UNPARSEABLE_BROKER_POSITION_SYMBOL")


def test_normalize_broker_position_unparseable_side_never_raises():
    meta = R._load_options_metadata_module()
    result = R.normalize_broker_position(_pos("AAPL261218C00150000", "1", "sideways"), meta)
    assert result["problem"].startswith("UNPARSEABLE_BROKER_POSITION_QTY_OR_SIDE")


# ======================================================================
# normalize_activity() / index_activities_by_occ_symbol()
# ======================================================================

def test_normalize_activity_filters_non_assignment_codes():
    assert R.normalize_activity(_act("AAPL261218C00150000", "FILL", date(2026, 10, 5))) is None


def test_normalize_activity_filters_canceled_status():
    assert R.normalize_activity(_act("AAPL261218C00150000", "OPASN", date(2026, 10, 5), status="canceled")) is None


@pytest.mark.parametrize("code", ["OPASN", "OPEXC", "OPEXP"])
def test_normalize_activity_accepts_all_three_codes(code):
    result = R.normalize_activity(_act("AAPL261218C00150000", code, date(2026, 10, 5)))
    assert result["activity_type"] == code


def test_index_activities_requires_positive_lookback_days():
    with pytest.raises(RuntimeError, match="INVALID_LOOKBACK_DAYS"):
        R.index_activities_by_occ_symbol([], lookback_days=0, as_of=AS_OF)
    with pytest.raises(RuntimeError, match="INVALID_LOOKBACK_DAYS"):
        R.index_activities_by_occ_symbol([], lookback_days=-5, as_of=AS_OF)
    with pytest.raises(RuntimeError, match="INVALID_LOOKBACK_DAYS"):
        R.index_activities_by_occ_symbol([], lookback_days="10", as_of=AS_OF)


def test_index_activities_excludes_outside_lookback_window():
    activities = [_act("AAPL261218C00150000", "OPASN", date(2026, 9, 1))]
    index = R.index_activities_by_occ_symbol(activities, lookback_days=10, as_of=AS_OF)
    assert index == {}


def test_index_activities_includes_within_lookback_window():
    activities = [_act("AAPL261218C00150000", "OPASN", date(2026, 10, 1))]
    index = R.index_activities_by_occ_symbol(activities, lookback_days=10, as_of=AS_OF)
    assert "AAPL261218C00150000" in index


def test_index_activities_boundary_day_is_included():
    # as_of - 10 days = 2026-09-26, exactly on the cutoff
    activities = [_act("AAPL261218C00150000", "OPASN", date(2026, 9, 26))]
    index = R.index_activities_by_occ_symbol(activities, lookback_days=10, as_of=AS_OF)
    assert "AAPL261218C00150000" in index


# ======================================================================
# reconcile_options_positions() -- the full pure core
# ======================================================================

def test_consistent_book_freezes_nothing():
    expected = [
        {"occ_symbol": "AAPL261218C00150000", "qty": "1"},
        {"occ_symbol": "AAPL261218C00160000", "qty": "-1"},
    ]
    broker = [_pos("AAPL261218C00150000", "1", "long"), _pos("AAPL261218C00160000", "1", "short")]
    result = R.reconcile_options_positions(expected, broker, [], lookback_days=10, as_of=AS_OF)
    assert result["frozen_underlyings"] == []
    assert result["requires_human_attention"] is False


def test_assignment_detected_with_evidence():
    expected = [
        {"occ_symbol": "AAPL261218C00150000", "qty": "1"},
        {"occ_symbol": "AAPL261218C00160000", "qty": "-1"},
    ]
    broker = [_pos("AAPL261218C00150000", "1", "long")]  # short leg gone
    activities = [_act("AAPL261218C00160000", "OPASN", date(2026, 10, 5))]
    result = R.reconcile_options_positions(expected, broker, activities, lookback_days=10, as_of=AS_OF)
    assert result["frozen_underlyings"] == ["AAPL"]
    assert result["requires_human_attention"] is True
    leg = result["underlyings"]["AAPL"]["legs"]["AAPL261218C00160000"]
    assert leg["classification"] == "ASSIGNMENT_OR_EXERCISE_DETECTED"
    assert leg["evidence"][0]["activity_type"] == "OPASN"


def test_unexplained_mismatch_without_evidence():
    expected = [{"occ_symbol": "AAPL261218C00150000", "qty": "1"}]
    broker = []  # position vanished, nothing explains it
    result = R.reconcile_options_positions(expected, broker, [], lookback_days=10, as_of=AS_OF)
    leg = result["underlyings"]["AAPL"]["legs"]["AAPL261218C00150000"]
    assert leg["classification"] == "UNEXPLAINED_POSITION_MISMATCH"
    assert result["requires_human_attention"] is True


def test_mismatch_never_silently_auto_resolved_even_with_symmetric_shape():
    # Both legs of a vertical reduced by the same amount -- the
    # "symmetric partial" the optionwright precedent would auto-adjust.
    # Per Martin's decision, O4B v1 freezes on this too, no exception.
    expected = [
        {"occ_symbol": "AAPL261218C00150000", "qty": "2"},
        {"occ_symbol": "AAPL261218C00160000", "qty": "-2"},
    ]
    broker = [_pos("AAPL261218C00150000", "1", "long"), _pos("AAPL261218C00160000", "1", "short")]
    result = R.reconcile_options_positions(expected, broker, [], lookback_days=10, as_of=AS_OF)
    assert result["frozen_underlyings"] == ["AAPL"]
    for leg in result["underlyings"]["AAPL"]["legs"].values():
        assert leg["classification"] != "CONSISTENT"


def test_unrelated_underlying_not_frozen_by_a_different_underlyings_mismatch():
    expected = [
        {"occ_symbol": "AAPL261218C00150000", "qty": "1"},
        {"occ_symbol": "MSFT261218C00300000", "qty": "1"},
    ]
    broker = [_pos("MSFT261218C00300000", "1", "long")]  # AAPL leg missing
    result = R.reconcile_options_positions(expected, broker, [], lookback_days=10, as_of=AS_OF)
    assert result["frozen_underlyings"] == ["AAPL"]
    assert result["underlyings"]["MSFT"]["status"] == "CONSISTENT"


def test_non_option_broker_positions_are_silently_excluded():
    expected = [{"occ_symbol": "AAPL261218C00150000", "qty": "1"}]
    broker = [_pos("AAPL261218C00150000", "1", "long"), _pos("SPY", "10", "long", asset_class="us_equity")]
    result = R.reconcile_options_positions(expected, broker, [], lookback_days=10, as_of=AS_OF)
    assert "SPY" not in result["underlyings"]
    assert result["frozen_underlyings"] == []


def test_unparseable_broker_position_surfaces_without_aborting_run():
    expected = [{"occ_symbol": "AAPL261218C00150000", "qty": "1"}]
    broker = [_pos("AAPL261218C00150000", "1", "long"), _pos("GARBAGE", "1", "long")]
    result = R.reconcile_options_positions(expected, broker, [], lookback_days=10, as_of=AS_OF)
    assert "AAPL" not in result["frozen_underlyings"]  # the good leg is unaffected
    assert "UNKNOWN" in result["frozen_underlyings"]
    reasons = result["underlyings"]["UNKNOWN"]["freeze_reasons"]
    assert reasons[0]["classification"] == "UNPARSEABLE_BROKER_POSITION"


def test_duplicate_expected_position_fails_closed():
    expected = [
        {"occ_symbol": "AAPL261218C00150000", "qty": "1"},
        {"occ_symbol": "AAPL261218C00150000", "qty": "2"},
    ]
    with pytest.raises(RuntimeError, match="DUPLICATE_EXPECTED_POSITION"):
        R.reconcile_options_positions(expected, [], [], lookback_days=10, as_of=AS_OF)


def test_expected_positions_not_a_list_fails_closed():
    with pytest.raises(RuntimeError, match="EXPECTED_POSITIONS_NOT_A_LIST"):
        R.reconcile_options_positions("nope", [], [], lookback_days=10, as_of=AS_OF)


def test_broker_positions_not_a_list_fails_closed():
    with pytest.raises(RuntimeError, match="BROKER_POSITIONS_NOT_A_LIST"):
        R.reconcile_options_positions([], "nope", [], lookback_days=10, as_of=AS_OF)


def test_empty_expected_and_broker_is_trivially_consistent():
    result = R.reconcile_options_positions([], [], [], lookback_days=10, as_of=AS_OF)
    assert result["frozen_underlyings"] == []
    assert result["underlyings"] == {}


def test_no_remediation_function_exists_anywhere_in_module():
    # The module must never attempt automated remediation -- confirm no
    # cancel/close/submit-shaped function exists at all.
    forbidden_substrings = ("cancel", "close_position", "submit", "unwind", "adjust_book")
    names = [name for name in dir(R) if not name.startswith("_")]
    for name in names:
        lowered = name.lower()
        for forbidden in forbidden_substrings:
            assert forbidden not in lowered, f"found remediation-shaped name: {name}"


# ======================================================================
# Network layer -- fetch_broker_positions / fetch_broker_activities /
# reconcile()
# ======================================================================

class FakeClient:
    def __init__(self, positions, activities_raw):
        self._positions = positions
        self._activities_raw = activities_raw
        self.get_calls = []

    def get_all_positions(self):
        return self._positions

    def get(self, path, data=None, **kwargs):
        self.get_calls.append((path, data))
        return self._activities_raw


def _raw_activity(activity_type, symbol, d="2026-10-05", status="executed"):
    return {
        "id": _UID, "account_id": _UID, "activity_type": activity_type, "date": d,
        "net_amount": "0", "description": "x", "status": status, "symbol": symbol,
        "qty": "1", "price": None, "per_share_amount": None,
    }


def test_fetch_broker_positions_returns_raw_sdk_list():
    client = FakeClient([_pos("AAPL261218C00150000", "1", "long")], [])
    result = R.fetch_broker_positions(client)
    assert result[0].symbol == "AAPL261218C00150000"


def test_fetch_broker_activities_filters_to_assignment_codes_only():
    raw = [
        _raw_activity("OPASN", "AAPL261218C00160000"),
        _raw_activity("FILL", "AAPL261218C00150000"),
    ]
    client = FakeClient([], raw)
    result = R.fetch_broker_activities(client, 10)
    assert len(result) == 1
    assert R._enum_value(result[0].activity_type) == "OPASN"


def test_fetch_broker_activities_sends_after_param():
    client = FakeClient([], [])
    R.fetch_broker_activities(client, 10)
    path, data = client.get_calls[0]
    assert path == "/account/activities"
    assert "after" in data


def test_fetch_broker_activities_requires_positive_lookback_days():
    client = FakeClient([], [])
    with pytest.raises(RuntimeError, match="INVALID_LOOKBACK_DAYS"):
        R.fetch_broker_activities(client, 0)


def test_fetch_broker_activities_rejects_non_list_response():
    client = FakeClient([], {"not": "a list"})
    with pytest.raises(RuntimeError, match="UNEXPECTED_ACTIVITIES_RESPONSE_SHAPE"):
        R.fetch_broker_activities(client, 10)


def test_reconcile_orchestrates_fetch_and_classify():
    broker_positions = [_pos("AAPL261218C00150000", "1", "long")]
    raw_activities = [_raw_activity("OPASN", "AAPL261218C00160000")]
    client = FakeClient(broker_positions, raw_activities)
    expected = [
        {"occ_symbol": "AAPL261218C00150000", "qty": "1"},
        {"occ_symbol": "AAPL261218C00160000", "qty": "-1"},
    ]
    result = R.reconcile(expected, client, lookback_days=10)
    assert result["frozen_underlyings"] == ["AAPL"]
    leg = result["underlyings"]["AAPL"]["legs"]["AAPL261218C00160000"]
    assert leg["classification"] == "ASSIGNMENT_OR_EXERCISE_DETECTED"


def test_reconcile_never_caches_fetches_fresh_every_call():
    broker_positions = [_pos("AAPL261218C00150000", "1", "long")]
    client = FakeClient(broker_positions, [])
    expected = [{"occ_symbol": "AAPL261218C00150000", "qty": "1"}]
    R.reconcile(expected, client, lookback_days=10)
    R.reconcile(expected, client, lookback_days=10)
    assert len(client.get_calls) == 2  # a fresh /account/activities call every time


# ======================================================================
# Network-boundary disclosure -- same "real code, no live call" style
# test .376's own suite uses
# ======================================================================

def test_network_capable_functions_disclosure_is_complete():
    import ast

    source = Path(R.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    module_level_funcs = {
        node.name for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert R.NETWORK_CAPABLE_FUNCTIONS <= module_level_funcs

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name not in R.NETWORK_CAPABLE_FUNCTIONS:
            for inner in ast.walk(node):
                if isinstance(inner, ast.Name) and inner.id == "client":
                    pytest.fail(
                        f"{node.name} references `client` but is not in NETWORK_CAPABLE_FUNCTIONS"
                    )


def test_no_live_trading_client_constructed_outside_main():
    source = Path(R.__file__).read_text(encoding="utf-8")
    assert "from alpaca.trading.client import TradingClient" not in source.split("def main()")[0]


def test_both_network_functions_are_read_only_by_construction():
    # fetch_broker_positions calls only get_all_positions(); fetch_broker_
    # activities calls only get(). Neither references submit_order,
    # cancel_order, or close_position anywhere in their source.
    import inspect

    forbidden = ("submit_order", "cancel_order", "close_position", "close_all_positions")
    for fn in (R.fetch_broker_positions, R.fetch_broker_activities, R.reconcile):
        src = inspect.getsource(fn)
        for name in forbidden:
            assert name not in src, f"{fn.__name__} unexpectedly references {name}"


# ======================================================================
# CLI main()
# ======================================================================

def test_main_requires_credentials(tmp_path, monkeypatch):
    import json

    monkeypatch.delenv("ALPACA_PAPER_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_PAPER_SECRET_KEY", raising=False)

    expected_path = tmp_path / "expected.json"
    output_path = tmp_path / "out.json"
    expected_path.write_text(json.dumps([{"occ_symbol": "AAPL261218C00150000", "qty": "1"}]), encoding="utf-8")

    argv = sys.argv
    sys.argv = [
        "aura_v05377", "--expected-input", str(expected_path),
        "--lookback-days", "10", "--output", str(output_path),
    ]
    try:
        exit_code = R.main()
    finally:
        sys.argv = argv

    assert exit_code == 1
    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert result["status"] == "FAIL_CLOSED"
    assert "MISSING_ALPACA_PAPER_CREDENTIALS" in result["error"]
