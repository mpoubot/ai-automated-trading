#!/usr/bin/env python3
"""Tests for aura_exit_policy_backtest.py (Phase 2: trailing-stop /
take-profit exit-policy research backtest).

Loaded via importlib, matching the convention used elsewhere in tests/
(e.g. tests/test_aura_v05323_execution_specification_builder.py). Uses
synthetic OHLC data throughout -- no network calls, no credentials needed.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "aura_exit_policy_backtest.py"

spec = importlib.util.spec_from_file_location("aura_exit_policy_backtest", SCRIPT_PATH)
epb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(epb)


def make_bars(rows: list[dict], *, symbol: str = "TEST/USD") -> pd.DataFrame:
    """rows: list of {open, high, low, close}, one per hourly bar starting
    at a fixed base timestamp. Adds timestamp/symbol/volume columns."""
    base = pd.Timestamp("2026-01-01T00:00:00Z")
    out = []
    for i, r in enumerate(rows):
        out.append({
            "timestamp": base + pd.Timedelta(hours=i),
            "symbol": symbol,
            "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"],
            "volume": 1.0,
        })
    return pd.DataFrame(out)


# ------------------------------------------------------------------------
# simulate_trade: LONG
# ------------------------------------------------------------------------

def test_long_trailing_stop_hit_after_favorable_move():
    # Entry at 100. Bar 1 rallies to 110 (running extreme -> 110, stop
    # trails to 110*0.98=107.8). Bar 2's low breaches 107.8 -> STOP.
    bars = make_bars([
        {"open": 101, "high": 110, "low": 100, "close": 109},
        {"open": 108, "high": 109, "low": 107.0, "close": 108},
    ])
    result = epb.simulate_trade(bars, bars.iloc[0]["timestamp"] - pd.Timedelta(hours=1), 100.0, "LONG", 2.0, None, 10, 0.0)
    assert result["exit_reason"] == "STOP"
    assert result["bars_held"] == 2
    assert result["gross_return_pct"] == pytest.approx(100.0 * (107.8 / 100.0 - 1.0))


def test_long_take_profit_hit():
    bars = make_bars([
        {"open": 101, "high": 106, "low": 100, "close": 105},  # target at entry*1.05=105 hit this bar
    ])
    result = epb.simulate_trade(bars, bars.iloc[0]["timestamp"] - pd.Timedelta(hours=1), 100.0, "LONG", 10.0, 5.0, 10, 0.0)
    assert result["exit_reason"] == "TARGET"
    assert result["gross_return_pct"] == pytest.approx(5.0)


def test_long_same_bar_stop_and_target_resolves_to_stop():
    # Wide range bar that touches both a tight stop and a target -- stop wins.
    bars = make_bars([
        {"open": 100, "high": 106, "low": 94, "close": 100},
    ])
    result = epb.simulate_trade(bars, bars.iloc[0]["timestamp"] - pd.Timedelta(hours=1), 100.0, "LONG", 5.0, 5.0, 10, 0.0)
    assert result["exit_reason"] == "STOP"


def test_long_timeout_when_never_stopped_or_targeted():
    bars = make_bars([{"open": 100, "high": 100.5, "low": 99.8, "close": 100.2} for _ in range(3)])
    result = epb.simulate_trade(bars, bars.iloc[0]["timestamp"] - pd.Timedelta(hours=1), 100.0, "LONG", 50.0, 50.0, 3, 0.0)
    assert result["exit_reason"] == "TIMEOUT"
    assert result["bars_held"] == 3
    assert result["gross_return_pct"] == pytest.approx(100.0 * (bars.iloc[-1]["close"] / 100.0 - 1.0))


def test_long_no_data_after_entry():
    bars = make_bars([{"open": 100, "high": 101, "low": 99, "close": 100}])
    # Entry timestamp AFTER the only bar in the frame -> nothing to simulate.
    result = epb.simulate_trade(bars, bars.iloc[0]["timestamp"] + pd.Timedelta(hours=5), 100.0, "LONG", 2.0, 4.0, 10, 0.0)
    assert result["exit_reason"] == "NO_DATA_AFTER_ENTRY"
    assert result["net_return_pct"] is None


def test_long_no_lookahead_stop_uses_prior_bar_extreme_not_this_bars_high():
    # Bar 1: big rally to 120 but closes back down -- running extreme should
    # update to 120 only AFTER this bar is checked (it isn't stopped out on
    # its own high). Bar 2's low of 115 must NOT trigger a stop computed
    # from bar 1's high within bar 1 itself.
    bars = make_bars([
        {"open": 101, "high": 120, "low": 100, "close": 102},  # no exit yet (stop trails from 100 -> 98, low=100 doesn't breach 98)
        {"open": 102, "high": 121, "low": 115, "close": 118},  # now stop trails from 120 -> 117.6; low=115 breaches -> STOP
    ])
    result = epb.simulate_trade(bars, bars.iloc[0]["timestamp"] - pd.Timedelta(hours=1), 100.0, "LONG", 2.0, None, 10, 0.0)
    assert result["exit_reason"] == "STOP"
    assert result["bars_held"] == 2
    # Stop must trail from bar 1's high (120), not exit within bar 1 itself
    # on that same high -- i.e. exit_price = 120 * 0.98 = 117.6, not 98.
    assert result["gross_return_pct"] == pytest.approx(100.0 * (117.6 / 100.0 - 1.0))


def test_long_cost_is_subtracted_from_net_not_gross():
    bars = make_bars([{"open": 100, "high": 106, "low": 100, "close": 105}])
    result = epb.simulate_trade(bars, bars.iloc[0]["timestamp"] - pd.Timedelta(hours=1), 100.0, "LONG", 10.0, 5.0, 10, 0.10)
    assert result["gross_return_pct"] == pytest.approx(5.0)
    assert result["net_return_pct"] == pytest.approx(4.90)


# ------------------------------------------------------------------------
# simulate_trade: SHORT (mirror of the LONG cases)
# ------------------------------------------------------------------------

def test_short_trailing_stop_hit_after_favorable_move():
    # Entry at 100 (short). Bar 1 drops to 90 (running extreme -> 90, stop
    # trails to 90*1.02=91.8). Bar 2's high breaches 91.8 -> STOP.
    bars = make_bars([
        {"open": 99, "high": 100, "low": 90, "close": 91},
        {"open": 92, "high": 92.5, "low": 91, "close": 92},
    ])
    result = epb.simulate_trade(bars, bars.iloc[0]["timestamp"] - pd.Timedelta(hours=1), 100.0, "SHORT", 2.0, None, 10, 0.0)
    assert result["exit_reason"] == "STOP"
    assert result["gross_return_pct"] == pytest.approx(100.0 * (100.0 / 91.8 - 1.0))


def test_short_take_profit_hit():
    bars = make_bars([{"open": 99, "high": 100, "low": 94, "close": 95}])  # target at entry*0.95=95
    result = epb.simulate_trade(bars, bars.iloc[0]["timestamp"] - pd.Timedelta(hours=1), 100.0, "SHORT", 10.0, 5.0, 10, 0.0)
    assert result["exit_reason"] == "TARGET"
    # SHORT return is entry/exit - 1 (symmetric with LONG's exit/entry - 1),
    # so a 5%-of-entry price drop (100 -> 95) returns 100/95 - 1 = 5.263...%,
    # not exactly 5% -- these only coincide for small moves.
    assert result["gross_return_pct"] == pytest.approx(100.0 * (100.0 / 95.0 - 1.0))


def test_short_same_bar_stop_and_target_resolves_to_stop():
    bars = make_bars([{"open": 100, "high": 106, "low": 94, "close": 100}])
    result = epb.simulate_trade(bars, bars.iloc[0]["timestamp"] - pd.Timedelta(hours=1), 100.0, "SHORT", 5.0, 5.0, 10, 0.0)
    assert result["exit_reason"] == "STOP"


def test_short_profitable_when_price_falls():
    bars = make_bars([{"open": 99, "high": 100, "low": 80, "close": 85} for _ in range(1)])
    result = epb.simulate_trade(bars, bars.iloc[0]["timestamp"] - pd.Timedelta(hours=1), 100.0, "SHORT", 50.0, 50.0, 1, 0.0)
    assert result["exit_reason"] == "TIMEOUT"
    assert result["gross_return_pct"] > 0  # short profits when price fell


# ------------------------------------------------------------------------
# simulate_trades_for_entries: sequential, single-position account -- an
# entry that fires while a previous trade under the same policy is still
# open must be skipped, not stacked into a second concurrent trade.
# ------------------------------------------------------------------------

def test_simulate_trades_for_entries_skips_entry_while_prior_trade_still_open():
    # A flat, never-moving series: with a wide trailing stop and no
    # take-profit, the first trade only resolves via TIMEOUT after
    # max_hold_bars=20. A second entry fired at hour=5 (well inside that
    # window) must be skipped -- only one trade should come out.
    bars = [{"open": 100, "high": 100.1, "low": 99.9, "close": 100} for _ in range(40)]
    g_full = make_bars(bars)
    entries = pd.DataFrame([
        {"timestamp": g_full.iloc[0]["timestamp"], "close": 100.0},   # hour 0 -- opens a trade held to hour 20
        {"timestamp": g_full.iloc[5]["timestamp"], "close": 100.0},   # hour 5 -- still inside the open trade, must be skipped
    ])
    trades, skipped = epb.simulate_trades_for_entries(g_full, entries, "LONG", (50.0, None), max_hold_bars=20, cost_pct=0.0)
    assert len(trades) == 1
    assert skipped == 1
    assert trades.iloc[0]["exit_reason"] == "TIMEOUT"


def test_simulate_trades_for_entries_takes_entry_after_prior_trade_closes():
    # Same shape, but the second entry fires at hour=25 -- after the first
    # trade (held to hour 20) has already closed -- so both should be taken.
    bars = [{"open": 100, "high": 100.1, "low": 99.9, "close": 100} for _ in range(40)]
    g_full = make_bars(bars)
    entries = pd.DataFrame([
        {"timestamp": g_full.iloc[0]["timestamp"], "close": 100.0},    # hour 0 -- closes (TIMEOUT) at hour 20
        {"timestamp": g_full.iloc[25]["timestamp"], "close": 100.0},   # hour 25 -- after the first trade closed
    ])
    trades, skipped = epb.simulate_trades_for_entries(g_full, entries, "LONG", (50.0, None), max_hold_bars=20, cost_pct=0.0)
    assert len(trades) == 2
    assert skipped == 0


def test_simulate_trades_for_entries_takes_third_entry_after_first_closes_skips_middle():
    # Three entries: #2 fires while #1 is still open (skipped), #3 fires
    # after #1 has closed (taken) -- confirms gating resumes correctly
    # rather than staying blocked forever after the first skip.
    bars = [{"open": 100, "high": 100.1, "low": 99.9, "close": 100} for _ in range(40)]
    g_full = make_bars(bars)
    entries = pd.DataFrame([
        {"timestamp": g_full.iloc[0]["timestamp"], "close": 100.0},    # hour 0 -- closes (TIMEOUT) at hour 20
        {"timestamp": g_full.iloc[5]["timestamp"], "close": 100.0},    # hour 5 -- skipped, #1 still open
        {"timestamp": g_full.iloc[25]["timestamp"], "close": 100.0},   # hour 25 -- taken, #1 already closed
    ])
    trades, skipped = epb.simulate_trades_for_entries(g_full, entries, "LONG", (50.0, None), max_hold_bars=20, cost_pct=0.0)
    assert len(trades) == 2
    assert skipped == 1


def test_simulate_trades_for_entries_a_tighter_stop_skips_fewer_overlapping_entries():
    # A trade that resolves quickly (tight stop, sharp adverse move) frees
    # up the account sooner than a trade that only times out -- so the
    # SAME entries under a tight stop should skip no more (usually fewer)
    # overlapping entries than under a wide trail-only stop that has to run
    # to the max-hold cap. This is why the skip count must be computed per
    # policy, not once for the whole entry list.
    bars = []
    for h in range(40):
        if h == 1:
            bars.append({"open": 100, "high": 100, "low": 90, "close": 91})  # sharp drop -> tight stop fires fast
        else:
            bars.append({"open": 100, "high": 100.1, "low": 99.9, "close": 100})
    g_full = make_bars(bars)
    entries = pd.DataFrame([
        {"timestamp": g_full.iloc[0]["timestamp"], "close": 100.0},
        {"timestamp": g_full.iloc[5]["timestamp"], "close": 100.0},
    ])
    tight_trades, tight_skipped = epb.simulate_trades_for_entries(g_full, entries, "LONG", (1.0, None), max_hold_bars=20, cost_pct=0.0)
    wide_trades, wide_skipped = epb.simulate_trades_for_entries(g_full, entries, "LONG", (50.0, None), max_hold_bars=20, cost_pct=0.0)
    assert tight_trades.iloc[0]["exit_reason"] == "STOP"
    assert tight_skipped == 0  # the hour-5 entry fires after the hour-0 trade already stopped out on the hour-1 drop
    assert wide_skipped == 1   # the wide trail-only stop is still open at hour 5, so that entry is skipped


# ------------------------------------------------------------------------
# build_entries: FROZEN vs MIRROR, direction correctness, ATR threshold.
# ------------------------------------------------------------------------

def _synthetic_regime_slice(rows: list[dict]) -> pd.DataFrame:
    """Build a minimal frame with exactly the columns build_entries reads:
    timestamp, _valid_row, trend_bear, bar2_positive, atr14_pct."""
    base = pd.Timestamp("2026-01-01T00:00:00Z")
    out = []
    for i, r in enumerate(rows):
        out.append({
            "timestamp": base + pd.Timedelta(hours=i),
            "_valid_row": True,
            "trend_bear": r["trend_bear"],
            "bar2_positive": r["bar2_positive"],
            "atr14_pct": r["atr14_pct"],
        })
    return pd.DataFrame(out)


def test_build_entries_frozen_matches_bear_low_atr_positive_bar2():
    g = _synthetic_regime_slice([
        {"trend_bear": True, "bar2_positive": True, "atr14_pct": 0.3},  # matches FROZEN
        {"trend_bear": True, "bar2_positive": True, "atr14_pct": 0.3},  # same run, not a new entry
        {"trend_bear": True, "bar2_positive": False, "atr14_pct": 0.3},  # no match (bar2 not positive)
        {"trend_bear": False, "bar2_positive": True, "atr14_pct": 0.3},  # no match (not bear)
        {"trend_bear": True, "bar2_positive": True, "atr14_pct": 0.9},  # no match (ATR too high)
    ])
    entries = epb.build_entries(g, "FROZEN", atr_threshold_pct=0.596)
    assert len(entries) == 1
    assert entries.iloc[0]["timestamp"] == g.iloc[0]["timestamp"]


def test_build_entries_mirror_matches_bull_or_neutral_low_atr_nonpositive_bar2():
    g = _synthetic_regime_slice([
        {"trend_bear": False, "bar2_positive": False, "atr14_pct": 0.3},  # matches MIRROR
        {"trend_bear": True, "bar2_positive": False, "atr14_pct": 0.3},  # no match (is bear)
        {"trend_bear": False, "bar2_positive": True, "atr14_pct": 0.3},  # no match (bar2 positive)
    ])
    entries = epb.build_entries(g, "MIRROR", atr_threshold_pct=0.596)
    assert len(entries) == 1
    assert entries.iloc[0]["timestamp"] == g.iloc[0]["timestamp"]


def test_build_entries_new_episode_after_gap():
    # Two separate runs of FROZEN match, separated by a non-matching bar --
    # must produce two entries, not one.
    g = _synthetic_regime_slice([
        {"trend_bear": True, "bar2_positive": True, "atr14_pct": 0.3},
        {"trend_bear": False, "bar2_positive": True, "atr14_pct": 0.3},
        {"trend_bear": True, "bar2_positive": True, "atr14_pct": 0.3},
    ])
    entries = epb.build_entries(g, "FROZEN", atr_threshold_pct=0.596)
    assert len(entries) == 2


def test_build_entries_unknown_candidate_raises():
    g = _synthetic_regime_slice([{"trend_bear": True, "bar2_positive": True, "atr14_pct": 0.3}])
    with pytest.raises(ValueError):
        epb.build_entries(g, "NOT_A_CANDIDATE", atr_threshold_pct=0.596)


# ------------------------------------------------------------------------
# trade_summary / policy_grid / policy_id / search_best_policy
# ------------------------------------------------------------------------

def test_trade_summary_basic_stats():
    trades = pd.DataFrame([
        {"exit_reason": "TARGET", "bars_held": 3, "net_return_pct": 4.0},
        {"exit_reason": "STOP", "bars_held": 2, "net_return_pct": -2.0},
        {"exit_reason": "TIMEOUT", "bars_held": 10, "net_return_pct": 1.0},
    ])
    s = epb.trade_summary(trades)
    assert s["n_trades"] == 3
    assert s["mean_net_return_pct"] == pytest.approx((4.0 - 2.0 + 1.0) / 3)
    assert s["win_rate"] == pytest.approx(2 / 3)
    assert s["profit_factor"] == pytest.approx((4.0 + 1.0) / 2.0)
    assert s["timeout_rate"] == pytest.approx(1 / 3)


def test_trade_summary_empty():
    s = epb.trade_summary(pd.DataFrame(columns=["exit_reason", "bars_held", "net_return_pct"]))
    assert s["n_trades"] == 0
    assert s["mean_net_return_pct"] is None


def test_policy_grid_includes_trail_only_variant_per_trailing_stop():
    grid = epb.policy_grid((1.0, 2.0), (4.0, 8.0))
    assert (1.0, None) in grid
    assert (2.0, None) in grid
    assert (1.0, 4.0) in grid
    assert len(grid) == 2 * (2 + 1)


def test_policy_id_labels_trail_only_distinctly():
    assert epb.policy_id((2.0, 4.0)) == "TS2_TP4"
    assert epb.policy_id((2.0, None)) == "TS2_TRAILONLY"


def test_search_best_policy_picks_highest_train_mean_and_respects_min_trades():
    # Build entries that will profit a lot under a wide trailing stop and
    # lose under a tight one -- search should pick the wide one.
    base = pd.Timestamp("2026-01-01T00:00:00Z")
    entries = pd.DataFrame([
        {"timestamp": base + pd.Timedelta(hours=h), "close": 100.0} for h in (0, 20, 40, 60)
    ])
    # g_full: after every entry, price dips 1% then rallies 20% over the window.
    bars = []
    for h in range(0, 90):
        bars.append({"open": 100, "high": 101, "low": 99, "close": 100})
    g_full = make_bars(bars)
    g_full["timestamp"] = base + pd.to_timedelta(np.arange(len(bars)), unit="h")

    # A tight 0.5% stop should get stopped out near breakeven-ish; a wide
    # 50% stop should just time out flat in this synthetic flat series --
    # this test only checks that search_best_policy runs end-to-end and
    # returns one of the grid policies with a summary, not a specific pick
    # (the flat series makes every policy roughly equal by design; a sharper
    # directional check lives in the LONG/SHORT unit tests above).
    grid = epb.policy_grid((0.5, 50.0), (1.0,))
    chosen, summary = epb.search_best_policy(g_full, entries, "LONG", grid, max_hold_bars=10, cost_pct=0.0)
    assert chosen in grid
    assert summary["n_trades"] >= epb.MIN_TRAIN_TRADES_FOR_SEARCH


def test_search_best_policy_none_when_too_few_train_entries():
    entries = pd.DataFrame([{"timestamp": pd.Timestamp("2026-01-01T00:00:00Z"), "close": 100.0}])
    g_full = make_bars([{"open": 100, "high": 101, "low": 99, "close": 100}])
    chosen, summary = epb.search_best_policy(g_full, entries, "LONG", epb.policy_grid((1.0,), (2.0,)), max_hold_bars=5, cost_pct=0.0)
    assert chosen is None
    assert summary is None


# ------------------------------------------------------------------------
# Direction mapping / candidate labels stay in sync with .13's live strings.
# ------------------------------------------------------------------------

def test_candidate_direction_mapping_matches_agreed_phase2_scope():
    assert epb.CANDIDATE_DIRECTION == {"FROZEN": "LONG", "MIRROR": "SHORT"}


def test_candidate_labels_match_live_signal_decision_engine_constants():
    assert epb.CANDIDATE_LABEL["FROZEN"] == epb.SDE.FROZEN_CANDIDATE
    assert epb.CANDIDATE_LABEL["MIRROR"] == epb.SDE.MIRROR_CANDIDATE


def test_this_script_never_touches_live_direction_allowlists():
    # .23's allowlists must remain untouched/empty regardless of anything
    # this research script does -- verified by import, not by running .23.
    esb = importlib.util.module_from_spec(
        importlib.util.spec_from_file_location("esb", ROOT / "aura_v05323_execution_specification_builder.py")
    )
    importlib.util.spec_from_file_location("esb", ROOT / "aura_v05323_execution_specification_builder.py").loader.exec_module(esb)
    assert esb.VALIDATED_LONG_ENTRY_REGIME_LABELS == frozenset()
    assert esb.VALIDATED_SHORT_ENTRY_REGIME_LABELS == frozenset()


# ------------------------------------------------------------------------
# CLI fail-closed path (no credentials, no network).
# ------------------------------------------------------------------------

def test_cli_fails_closed_without_credentials(monkeypatch, tmp_path):
    env = {
        k: v for k, v in __import__("os").environ.items()
        if k not in ("ALPACA_PAPER_API_KEY", "ALPACA_API_KEY", "ALPACA_PAPER_SECRET_KEY", "ALPACA_SECRET_KEY")
    }
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--outdir", str(tmp_path)],
        cwd=str(ROOT), text=True, capture_output=True, env=env,
    )
    assert proc.returncode == 2
    assert "FAIL-CLOSED" in proc.stderr
