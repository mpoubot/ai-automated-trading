#!/usr/bin/env python3
"""Tests for AURA v0.5.3.80 -- Options Exit Engine (O6).

No live Alpaca call anywhere in this file.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aura_v05380_options_exit_engine as EXIT  # noqa: E402

AS_OF = date(2026, 10, 7)
EXPIRY = AS_OF + timedelta(days=30)

SHORT_OCC = f"SPY{EXPIRY.strftime('%y%m%d')}C00450000"
LONG_OCC = f"SPY{EXPIRY.strftime('%y%m%d')}C00455000"

POSITION = {
    "underlying_symbol": "SPY",
    "legs": [
        {"occ_symbol": SHORT_OCC, "side": "SELL"},
        {"occ_symbol": LONG_OCC, "side": "BUY"},
    ],
}


def quote_record(mid: str, status: str = "OK") -> dict:
    mid_d = Decimal(mid)
    delta = Decimal("0.02")
    return {
        "data_status": status,
        "broker_latest_quote": {"bid_price": str(mid_d - delta), "ask_price": str(mid_d + delta)},
    }


def quotes(short_mid="1.55", long_mid="0.05", short_status="OK", long_status="OK") -> dict:
    return {
        SHORT_OCC: quote_record(short_mid, short_status),
        LONG_OCC: quote_record(long_mid, long_status),
    }


@dataclass
class FakeCalendarState:
    status: str
    calendar_by_symbol: dict
    error: str | None = None


# ======================================================================
# normalize_position() -- caller-input validation, fails closed
# ======================================================================

def test_normalize_position_rejects_non_dict():
    with pytest.raises(RuntimeError, match="INVALID_POSITION"):
        EXIT.normalize_position("nope", EXIT._load_options_metadata_module())


def test_normalize_position_rejects_missing_underlying():
    meta = EXIT._load_options_metadata_module()
    with pytest.raises(RuntimeError, match="MISSING_UNDERLYING_SYMBOL"):
        EXIT.normalize_position({"legs": []}, meta)


def test_normalize_position_rejects_wrong_leg_count():
    meta = EXIT._load_options_metadata_module()
    with pytest.raises(RuntimeError, match="V1_SCOPE_REQUIRES_EXACTLY_2_LEGS"):
        EXIT.normalize_position({"underlying_symbol": "SPY", "legs": [POSITION["legs"][0]]}, meta)


def test_normalize_position_rejects_invalid_side():
    meta = EXIT._load_options_metadata_module()
    bad = {"underlying_symbol": "SPY", "legs": [
        {"occ_symbol": SHORT_OCC, "side": "SHORT"}, {"occ_symbol": LONG_OCC, "side": "BUY"},
    ]}
    with pytest.raises(RuntimeError, match="INVALID_LEG_SIDE"):
        EXIT.normalize_position(bad, meta)


def test_normalize_position_rejects_unparseable_occ_symbol():
    meta = EXIT._load_options_metadata_module()
    bad = {"underlying_symbol": "SPY", "legs": [
        {"occ_symbol": "NOT-AN-OCC-SYMBOL", "side": "SELL"}, {"occ_symbol": LONG_OCC, "side": "BUY"},
    ]}
    with pytest.raises(RuntimeError, match="UNPARSEABLE_LEG_OCC_SYMBOL"):
        EXIT.normalize_position(bad, meta)


def test_normalize_position_rejects_underlying_mismatch():
    meta = EXIT._load_options_metadata_module()
    bad = {"underlying_symbol": "QQQ", "legs": POSITION["legs"]}
    with pytest.raises(RuntimeError, match="LEG_UNDERLYING_MISMATCH"):
        EXIT.normalize_position(bad, meta)


def test_normalize_position_rejects_mixed_expiries():
    meta = EXIT._load_options_metadata_module()
    other_expiry_occ = f"SPY{(EXPIRY + timedelta(days=7)).strftime('%y%m%d')}C00455000"
    bad = {"underlying_symbol": "SPY", "legs": [
        {"occ_symbol": SHORT_OCC, "side": "SELL"}, {"occ_symbol": other_expiry_occ, "side": "BUY"},
    ]}
    with pytest.raises(RuntimeError, match="NOT_A_VERTICAL_MIXED_EXPIRIES"):
        EXIT.normalize_position(bad, meta)


def test_normalize_position_rejects_two_short_legs():
    meta = EXIT._load_options_metadata_module()
    bad = {"underlying_symbol": "SPY", "legs": [
        {"occ_symbol": SHORT_OCC, "side": "SELL"}, {"occ_symbol": LONG_OCC, "side": "SELL"},
    ]}
    with pytest.raises(RuntimeError, match="NOT_A_VERTICAL_REQUIRES_ONE_LONG_ONE_SHORT_LEG"):
        EXIT.normalize_position(bad, meta)


def test_normalize_position_happy_path():
    meta = EXIT._load_options_metadata_module()
    normalized = EXIT.normalize_position(POSITION, meta)
    assert normalized["underlying_symbol"] == "SPY"
    assert normalized["expiry"] == EXPIRY
    assert {leg["side"] for leg in normalized["legs"]} == {"BUY", "SELL"}


# ======================================================================
# days_to_expiry() -- pure
# ======================================================================

def test_days_to_expiry():
    assert EXIT.days_to_expiry(EXPIRY, AS_OF) == 30
    assert EXIT.days_to_expiry(EXPIRY, EXPIRY) == 0
    assert EXIT.days_to_expiry(EXPIRY, EXPIRY + timedelta(days=1)) == -1


# ======================================================================
# compute_cost_to_close() -- valuation, fail-closed per-leg
# ======================================================================

def test_compute_cost_to_close_happy_path_matches_entry_shape():
    meta = EXIT._load_options_metadata_module()
    normalized = EXIT.normalize_position(POSITION, meta)
    result = EXIT.compute_cost_to_close(normalized, quotes("1.55", "0.05"))
    assert result["status"] == "OK"
    assert result["cost_to_close"] == Decimal("1.50")


def test_compute_cost_to_close_sign_flips_on_favorable_decay():
    meta = EXIT._load_options_metadata_module()
    normalized = EXIT.normalize_position(POSITION, meta)
    result = EXIT.compute_cost_to_close(normalized, quotes("0.10", "0.05"))
    assert result["cost_to_close"] == Decimal("0.05")


def test_compute_cost_to_close_fails_closed_on_missing_leg_quote():
    meta = EXIT._load_options_metadata_module()
    normalized = EXIT.normalize_position(POSITION, meta)
    result = EXIT.compute_cost_to_close(normalized, {SHORT_OCC: quote_record("1.55")})
    assert result["status"] == "QUOTE_NOT_USABLE"
    assert result["cost_to_close"] is None
    assert result["leg_statuses"][LONG_OCC] == "MISSING_QUOTE_RECORD"


def test_compute_cost_to_close_fails_closed_on_stale_quote():
    meta = EXIT._load_options_metadata_module()
    normalized = EXIT.normalize_position(POSITION, meta)
    result = EXIT.compute_cost_to_close(normalized, quotes(short_status="STALE_QUOTE"))
    assert result["status"] == "QUOTE_NOT_USABLE"
    assert result["leg_statuses"][SHORT_OCC] == "STALE_QUOTE"


def test_compute_cost_to_close_fails_closed_on_crossed_quote():
    meta = EXIT._load_options_metadata_module()
    normalized = EXIT.normalize_position(POSITION, meta)
    bad = quotes()
    bad[SHORT_OCC]["broker_latest_quote"] = {"bid_price": "2.00", "ask_price": "1.00"}
    result = EXIT.compute_cost_to_close(normalized, bad)
    assert result["leg_statuses"][SHORT_OCC] == "INVALID_QUOTE"
    assert result["status"] == "QUOTE_NOT_USABLE"


# ======================================================================
# evaluate_exit() -- the one entry point, full priority-order coverage
# ======================================================================

def test_evaluate_exit_holds_when_nothing_fires():
    r = EXIT.evaluate_exit(POSITION, entry_net_price="-1.50", leg_quotes=quotes(), as_of_date=AS_OF)
    assert r["action"] == "HOLD"
    assert r["trigger"] is None
    assert len(r["triggers_evaluated"]) == 5


def test_evaluate_exit_take_profit_boundary_fires_at_exactly_50_pct():
    r = EXIT.evaluate_exit(
        POSITION, entry_net_price="-1.50", leg_quotes=quotes("0.80", "0.05"), as_of_date=AS_OF,
    )
    assert r["cost_to_close"] == "0.75"
    assert r["action"] == "CLOSE" and r["trigger"] == "TAKE_PROFIT"


def test_evaluate_exit_take_profit_does_not_fire_just_above_boundary():
    r = EXIT.evaluate_exit(
        POSITION, entry_net_price="-1.50", leg_quotes=quotes("0.81", "0.05"), as_of_date=AS_OF,
    )
    assert r["action"] == "HOLD"


def test_evaluate_exit_stop_loss_boundary_fires_at_exactly_200_pct():
    r = EXIT.evaluate_exit(
        POSITION, entry_net_price="-1.50", leg_quotes=quotes("3.08", "0.08"), as_of_date=AS_OF,
    )
    assert r["cost_to_close"] == "3.00"
    assert r["action"] == "CLOSE" and r["trigger"] == "STOP_LOSS"


def test_evaluate_exit_stop_loss_does_not_fire_just_below_boundary():
    r = EXIT.evaluate_exit(
        POSITION, entry_net_price="-1.50", leg_quotes=quotes("3.06", "0.08"), as_of_date=AS_OF,
    )
    assert r["action"] == "HOLD"


def test_evaluate_exit_dte_boundary_fires_at_exactly_3_dte():
    r = EXIT.evaluate_exit(
        POSITION, entry_net_price="-1.50", leg_quotes=quotes(), as_of_date=EXPIRY - timedelta(days=3),
    )
    assert r["dte"] == 3
    assert r["action"] == "CLOSE" and r["trigger"] == "DTE_EXIT"


def test_evaluate_exit_dte_does_not_fire_at_4_dte():
    r = EXIT.evaluate_exit(
        POSITION, entry_net_price="-1.50", leg_quotes=quotes(), as_of_date=EXPIRY - timedelta(days=4),
    )
    assert r["dte"] == 4
    assert r["action"] == "HOLD"


def test_evaluate_exit_kill_switch_beats_everything():
    r = EXIT.evaluate_exit(
        POSITION, entry_net_price="-1.50", leg_quotes=quotes("0.80", "0.05"),
        as_of_date=EXPIRY - timedelta(days=1), kill_switch_engaged=True,
    )
    # TAKE_PROFIT and DTE_EXIT would both fire here too -- KILL_SWITCH must still win.
    assert r["action"] == "CLOSE" and r["trigger"] == "KILL_SWITCH"


def test_evaluate_exit_take_profit_beats_dte_and_earnings():
    cal = FakeCalendarState("OK", {"SPY": (AS_OF,)})
    r = EXIT.evaluate_exit(
        POSITION, entry_net_price="-1.50", leg_quotes=quotes("0.80", "0.05"),
        as_of_date=EXPIRY - timedelta(days=1), earnings_calendar_state=cal,
    )
    assert r["trigger"] == "TAKE_PROFIT"


def test_evaluate_exit_dte_beats_earnings():
    cal = FakeCalendarState("OK", {"SPY": (EXPIRY - timedelta(days=2),)})
    r = EXIT.evaluate_exit(
        POSITION, entry_net_price="-1.50", leg_quotes=quotes(), as_of_date=EXPIRY - timedelta(days=2),
        earnings_calendar_state=cal,
    )
    assert r["trigger"] == "DTE_EXIT"


def test_evaluate_exit_earnings_trigger_fires_within_horizon():
    cal = FakeCalendarState("OK", {"SPY": (AS_OF + timedelta(days=1),)})
    r = EXIT.evaluate_exit(
        POSITION, entry_net_price="-1.50", leg_quotes=quotes(), as_of_date=AS_OF,
        earnings_calendar_state=cal,
    )
    assert r["action"] == "CLOSE" and r["trigger"] == "EARNINGS_EXIT"


def test_evaluate_exit_earnings_trigger_does_not_fire_outside_horizon():
    cal = FakeCalendarState("OK", {"SPY": (AS_OF + timedelta(days=2),)})
    r = EXIT.evaluate_exit(
        POSITION, entry_net_price="-1.50", leg_quotes=quotes(), as_of_date=AS_OF,
        earnings_calendar_state=cal,
    )
    assert r["action"] == "HOLD"


def test_evaluate_exit_earnings_trigger_ignores_other_symbols():
    cal = FakeCalendarState("OK", {"QQQ": (AS_OF,)})
    r = EXIT.evaluate_exit(
        POSITION, entry_net_price="-1.50", leg_quotes=quotes(), as_of_date=AS_OF,
        earnings_calendar_state=cal,
    )
    assert r["action"] == "HOLD"


def test_evaluate_exit_earnings_unavailable_skips_not_forces(caplog=None):
    cal = FakeCalendarState("UNAVAILABLE", {}, error="FMP_RATE_LIMITED")
    r = EXIT.evaluate_exit(
        POSITION, entry_net_price="-1.50", leg_quotes=quotes(), as_of_date=AS_OF,
        earnings_calendar_state=cal,
    )
    assert r["action"] == "HOLD"
    earnings_entry = next(t for t in r["triggers_evaluated"] if t["trigger"] == "EARNINGS_EXIT")
    assert earnings_entry["fired"] is False
    assert "UNAVAILABLE" in earnings_entry["detail"]


def test_evaluate_exit_earnings_unavailable_does_not_block_dte():
    cal = FakeCalendarState("UNAVAILABLE", {}, error="FMP_DOWN")
    r = EXIT.evaluate_exit(
        POSITION, entry_net_price="-1.50", leg_quotes=quotes(), as_of_date=EXPIRY - timedelta(days=1),
        earnings_calendar_state=cal,
    )
    assert r["action"] == "CLOSE" and r["trigger"] == "DTE_EXIT"


def test_evaluate_exit_no_earnings_calendar_state_skips_trigger_cleanly():
    r = EXIT.evaluate_exit(POSITION, entry_net_price="-1.50", leg_quotes=quotes(), as_of_date=AS_OF)
    earnings_entry = next(t for t in r["triggers_evaluated"] if t["trigger"] == "EARNINGS_EXIT")
    assert earnings_entry["fired"] is False


def test_evaluate_exit_missing_quote_skips_tp_sl_but_not_dte():
    r = EXIT.evaluate_exit(
        POSITION, entry_net_price="-1.50",
        leg_quotes={SHORT_OCC: quote_record("1.55", "STALE_QUOTE")},
        as_of_date=EXPIRY - timedelta(days=1),
    )
    assert r["cost_to_close"] is None
    assert r["action"] == "CLOSE" and r["trigger"] == "DTE_EXIT"
    tp_entry = next(t for t in r["triggers_evaluated"] if t["trigger"] == "TAKE_PROFIT")
    sl_entry = next(t for t in r["triggers_evaluated"] if t["trigger"] == "STOP_LOSS")
    assert tp_entry["fired"] is False and "UNAVAILABLE" in tp_entry["detail"]
    assert sl_entry["fired"] is False and "UNAVAILABLE" in sl_entry["detail"]


def test_evaluate_exit_missing_quote_alone_never_forces_close():
    r = EXIT.evaluate_exit(
        POSITION, entry_net_price="-1.50", leg_quotes={}, as_of_date=AS_OF,
    )
    assert r["action"] == "HOLD"


def test_evaluate_exit_rejects_non_negative_entry_price():
    with pytest.raises(RuntimeError, match="V1_SCOPE_REQUIRES_NET_CREDIT_SPREAD"):
        EXIT.evaluate_exit(POSITION, entry_net_price="1.50", leg_quotes=quotes(), as_of_date=AS_OF)


def test_evaluate_exit_rejects_zero_entry_price():
    with pytest.raises(RuntimeError, match="V1_SCOPE_REQUIRES_NET_CREDIT_SPREAD"):
        EXIT.evaluate_exit(POSITION, entry_net_price="0", leg_quotes=quotes(), as_of_date=AS_OF)


def test_evaluate_exit_rejects_invalid_entry_price_type():
    with pytest.raises(RuntimeError, match="INVALID_DECIMAL"):
        EXIT.evaluate_exit(POSITION, entry_net_price="not-a-number", leg_quotes=quotes(), as_of_date=AS_OF)


def test_evaluate_exit_rejects_invalid_as_of_date_type():
    with pytest.raises(RuntimeError, match="INVALID_AS_OF_DATE"):
        EXIT.evaluate_exit(POSITION, entry_net_price="-1.50", leg_quotes=quotes(), as_of_date="2026-10-07")


def test_evaluate_exit_credit_captured_pct_sign_and_magnitude():
    r = EXIT.evaluate_exit(POSITION, entry_net_price="-1.50", leg_quotes=quotes("0.80", "0.05"), as_of_date=AS_OF)
    assert r["credit_captured_pct"] == "0.5"

    r = EXIT.evaluate_exit(POSITION, entry_net_price="-1.50", leg_quotes=quotes("3.08", "0.08"), as_of_date=AS_OF)
    assert Decimal(r["credit_captured_pct"]) == Decimal("-1")


def test_evaluate_exit_output_is_json_serializable_shape():
    import json
    r = EXIT.evaluate_exit(POSITION, entry_net_price="-1.50", leg_quotes=quotes(), as_of_date=AS_OF)
    json.dumps(r)  # raises TypeError if any field (e.g. a bare Decimal) isn't serializable


def test_evaluate_exit_config_override_changes_thresholds():
    custom = {"take_profit_credit_pct": Decimal("0.80"), "stop_loss_credit_pct": Decimal("1.20"),
              "dte_exit_threshold": 10, "earnings_exit_horizon_days": 3}
    r = EXIT.evaluate_exit(
        POSITION, entry_net_price="-1.50", leg_quotes=quotes("1.00", "0.10"), as_of_date=AS_OF, config=custom,
    )
    # cost_to_close = 0.90, which is > 0.5*1.5 default TP but <= 0.80*1.50=1.20 custom TP
    assert r["action"] == "CLOSE" and r["trigger"] == "TAKE_PROFIT"


def test_evaluate_exit_triggers_evaluated_always_has_all_five_in_priority_order():
    r = EXIT.evaluate_exit(POSITION, entry_net_price="-1.50", leg_quotes=quotes(), as_of_date=AS_OF)
    names = [t["trigger"] for t in r["triggers_evaluated"]]
    assert names == list(EXIT.PRIORITY_ORDER)


# ======================================================================
# Structural / disclosure properties
# ======================================================================

def test_network_capable_functions_is_empty():
    assert EXIT.NETWORK_CAPABLE_FUNCTIONS == frozenset()


def test_module_has_no_live_credentials_or_network_imports():
    source = Path(EXIT.__file__).read_text(encoding="utf-8")
    for token in ("requests.", "urlopen", "TradingClient(", "OptionHistoricalDataClient(", "StockHistoricalDataClient("):
        assert token not in source, f"unexpected live/network construction token found: {token}"


def test_kill_switch_default_is_false_never_forces_exit_silently():
    r = EXIT.evaluate_exit(POSITION, entry_net_price="-1.50", leg_quotes=quotes(), as_of_date=AS_OF)
    assert r["action"] == "HOLD"
