#!/usr/bin/env python3
"""Tests for aura_v05342_mexc_crypto_signal_research.py (v0.5.3.42 --
MEXC crypto short-side signal research).

Unit tests use synthetic OHLC/funding data (no network, no credentials).
One integration test at the bottom exercises the real pinned dataset
committed under AURA_CRYPTO/ end-to-end through the actual CLI entry
point, matching this repo's convention for real-path regression coverage
(see e.g. tests/test_aura_v05340_pre_submission_revalidation.py's E2E
tests through the real .38 orchestration).
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
SCRIPT_PATH = ROOT / "aura_v05342_mexc_crypto_signal_research.py"

spec = importlib.util.spec_from_file_location("aura_v05342_mexc_crypto_signal_research", SCRIPT_PATH)
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)


def make_bars(rows: list[dict], *, symbol: str = "TEST_USDT", start: str = "2026-01-01T00:00:00Z") -> pd.DataFrame:
    """rows: list of {open, high, low, close}, one per hourly bar starting
    at `start`. Adds timestamp/symbol/volume columns matching bars_1h.csv's
    schema (REQUIRED_BAR_COLUMNS)."""
    base = pd.Timestamp(start)
    out = []
    for i, r in enumerate(rows):
        out.append({
            "timestamp": base + pd.Timedelta(hours=i),
            "symbol": symbol,
            "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"],
            "volume": 1.0,
        })
    return pd.DataFrame(out)


def make_funding(events: list[tuple[str, str, float]]) -> pd.DataFrame:
    """events: list of (symbol, iso_timestamp, funding_rate). Always carries
    the symbol/settle_time/funding_rate columns even when empty, matching
    what a real (header-having) funding_history.csv read would produce --
    an empty list->DataFrame([]) with no columns is a test-only artifact,
    not something load_pinned_dataset can ever actually hand to callers."""
    if not events:
        return pd.DataFrame(columns=["symbol", "settle_time", "funding_rate"])
    return pd.DataFrame(
        [{"symbol": s, "settle_time": pd.Timestamp(t), "funding_rate": r} for s, t, r in events]
    )


# ------------------------------------------------------------------------
# funding_cost_pct
# ------------------------------------------------------------------------

def test_funding_cost_long_pays_on_positive_rate():
    funding = make_funding([("BTC_USDT", "2026-01-01T04:00:00Z", 0.0001)])
    cost = M.funding_cost_pct(
        funding, "BTC_USDT", pd.Timestamp("2026-01-01T00:00:00Z"), pd.Timestamp("2026-01-01T08:00:00Z"), "LONG"
    )
    assert cost == pytest.approx(0.01)  # 0.0001 * 100


def test_funding_cost_short_receives_on_positive_rate():
    funding = make_funding([("BTC_USDT", "2026-01-01T04:00:00Z", 0.0001)])
    cost = M.funding_cost_pct(
        funding, "BTC_USDT", pd.Timestamp("2026-01-01T00:00:00Z"), pd.Timestamp("2026-01-01T08:00:00Z"), "SHORT"
    )
    assert cost == pytest.approx(-0.01)  # short receives -> negative cost


def test_funding_cost_short_pays_on_negative_rate():
    funding = make_funding([("BTC_USDT", "2026-01-01T04:00:00Z", -0.0002)])
    cost = M.funding_cost_pct(
        funding, "BTC_USDT", pd.Timestamp("2026-01-01T00:00:00Z"), pd.Timestamp("2026-01-01T08:00:00Z"), "SHORT"
    )
    assert cost == pytest.approx(0.02)


def test_funding_cost_window_excludes_entry_includes_exit():
    # Event exactly at entry_ts must be excluded; event exactly at exit_ts must be included.
    funding = make_funding([
        ("BTC_USDT", "2026-01-01T00:00:00Z", 0.001),  # at entry -- excluded
        ("BTC_USDT", "2026-01-01T08:00:00Z", 0.0005),  # at exit -- included
    ])
    cost = M.funding_cost_pct(
        funding, "BTC_USDT", pd.Timestamp("2026-01-01T00:00:00Z"), pd.Timestamp("2026-01-01T08:00:00Z"), "LONG"
    )
    assert cost == pytest.approx(0.05)  # only the 0.0005 event counted


def test_funding_cost_zero_when_no_events_in_window():
    funding = make_funding([("BTC_USDT", "2026-01-05T00:00:00Z", 0.0005)])
    cost = M.funding_cost_pct(
        funding, "BTC_USDT", pd.Timestamp("2026-01-01T00:00:00Z"), pd.Timestamp("2026-01-01T08:00:00Z"), "LONG"
    )
    assert cost == 0.0


def test_funding_cost_ignores_other_symbols():
    funding = make_funding([("ETH_USDT", "2026-01-01T04:00:00Z", 0.01)])
    cost = M.funding_cost_pct(
        funding, "BTC_USDT", pd.Timestamp("2026-01-01T00:00:00Z"), pd.Timestamp("2026-01-01T08:00:00Z"), "LONG"
    )
    assert cost == 0.0


# ------------------------------------------------------------------------
# simulate_trade_mexc -- next-bar-open fill
# ------------------------------------------------------------------------

def test_fill_is_next_bar_open_not_signal_bar_close():
    # Signal bar (not itself in g_full) closes just before bar 0. Fill must
    # be bar 0's OPEN (101), never the signal bar's close.
    bars = make_bars([
        {"open": 101, "high": 102, "low": 100.5, "close": 101.5},
        {"open": 101.5, "high": 102, "low": 101, "close": 101.8},
    ])
    funding = make_funding([])
    signal_ts = bars.iloc[0]["timestamp"] - pd.Timedelta(hours=1)
    result = M.simulate_trade_mexc(bars, funding, "TEST_USDT", signal_ts, "LONG", 50.0, 50.0, 10, 0.0)
    assert result["entry_ts"] == bars.iloc[0]["timestamp"]
    assert result["entry_price"] == pytest.approx(101.0)


def test_no_data_after_signal_when_signal_is_last_bar():
    bars = make_bars([{"open": 100, "high": 101, "low": 99, "close": 100}])
    signal_ts = bars.iloc[0]["timestamp"] + pd.Timedelta(hours=5)  # nothing after this
    result = M.simulate_trade_mexc(bars, make_funding([]), "TEST_USDT", signal_ts, "LONG", 2.0, 4.0, 10, 0.0)
    assert result["exit_reason"] == "NO_DATA_AFTER_SIGNAL"
    assert result["exit_ts"] == signal_ts  # never blocks a later entry
    assert result["entry_price"] is None


def test_long_trailing_stop_no_lookahead():
    # Fill at bar0 open=101. Bar0 rallies to 110 -> running_extreme=110,
    # stop trails to 110*0.98=107.8. Bar1's low breaches 107.8 -> STOP,
    # using the extreme established BEFORE bar1, not bar1's own high.
    bars = make_bars([
        {"open": 101, "high": 110, "low": 100.5, "close": 109},
        {"open": 108, "high": 109, "low": 107.0, "close": 108},
    ])
    signal_ts = bars.iloc[0]["timestamp"] - pd.Timedelta(hours=1)
    result = M.simulate_trade_mexc(bars, make_funding([]), "TEST_USDT", signal_ts, "LONG", 2.0, None, 10, 0.0)
    assert result["exit_reason"] == "STOP"
    assert result["bars_held"] == 2
    entry_price = 101.0
    assert result["gross_return_pct"] == pytest.approx(100.0 * (107.8 / entry_price - 1.0))


def test_short_direction_pnl_sign_and_mfe_mae():
    # SHORT: entry at bar0 open=100. Price falls to 90 (favorable for
    # short) then bounces to 95 for exit via TIMEOUT.
    bars = make_bars([
        {"open": 100, "high": 100.5, "low": 90, "close": 92},
        {"open": 92, "high": 95, "low": 91, "close": 95},
    ])
    signal_ts = bars.iloc[0]["timestamp"] - pd.Timedelta(hours=1)
    result = M.simulate_trade_mexc(bars, make_funding([]), "TEST_USDT", signal_ts, "SHORT", 50.0, 50.0, 2, 0.0)
    assert result["exit_reason"] == "TIMEOUT"
    entry_price = 100.0
    exit_price = 95.0
    assert result["gross_return_pct"] == pytest.approx(100.0 * (entry_price / exit_price - 1.0))
    assert result["gross_return_pct"] > 0  # short profits when price falls net of the bounce
    # MFE should reflect the best (lowest) price seen (90), MAE the worst (highest, 95 post-bounce... but running_extreme tracks min for SHORT)
    assert result["mfe_pct"] == pytest.approx(100.0 * (entry_price / 90.0 - 1.0))


def test_net_return_subtracts_fee_slippage_and_funding():
    bars = make_bars([{"open": 100, "high": 100.5, "low": 99.8, "close": 100.2} for _ in range(3)])
    funding = make_funding([("TEST_USDT", "2026-01-01T02:00:00Z", 0.0005)])
    signal_ts = bars.iloc[0]["timestamp"] - pd.Timedelta(hours=1)
    result = M.simulate_trade_mexc(bars, funding, "TEST_USDT", signal_ts, "LONG", 50.0, 50.0, 3, 0.06)
    assert result["exit_reason"] == "TIMEOUT"
    assert result["fee_slippage_cost_pct"] == pytest.approx(0.06)
    assert result["funding_cost_pct"] == pytest.approx(0.05)
    assert result["net_return_pct"] == pytest.approx(
        result["gross_return_pct"] - 0.06 - 0.05
    )


# ------------------------------------------------------------------------
# simulate_trades_for_entries_mexc -- sequential single-position account
# ------------------------------------------------------------------------

def test_overlapping_entries_are_skipped_not_stacked():
    bars = make_bars([{"open": 100, "high": 100.2, "low": 99.9, "close": 100.1} for _ in range(20)])
    entries = pd.DataFrame([
        {
            "timestamp": bars.iloc[0]["timestamp"] - pd.Timedelta(hours=1),
            "trend_bear": True, "bar2_positive": True, "atr14_pct": 0.1,
        },
        {
            # Fires while the first trade (long-held, wide stop/target -> TIMEOUT at bar 10) is still open.
            "timestamp": bars.iloc[2]["timestamp"] - pd.Timedelta(hours=1),
            "trend_bear": True, "bar2_positive": True, "atr14_pct": 0.1,
        },
    ])
    trades, skipped = M.simulate_trades_for_entries_mexc(
        bars, make_funding([]), "TEST_USDT", entries, "LONG", (50.0, 50.0), 10, 0.0
    )
    assert skipped == 1
    assert len(trades) == 1


# ------------------------------------------------------------------------
# load_pinned_dataset -- fail-closed behavior
# ------------------------------------------------------------------------

def test_load_pinned_dataset_missing_file_fails_closed(tmp_path):
    with pytest.raises(FileNotFoundError):
        M.load_pinned_dataset(tmp_path)


def test_load_pinned_dataset_malformed_columns_fails_closed(tmp_path):
    pd.DataFrame({"a": [1]}).to_csv(tmp_path / "bars_1h.csv", index=False)
    pd.DataFrame({"a": [1]}).to_csv(tmp_path / "funding_history.csv", index=False)
    with pytest.raises(ValueError):
        M.load_pinned_dataset(tmp_path)


def test_load_pinned_dataset_manifest_hash_matches_recompute(tmp_path):
    bars = make_bars([{"open": 1, "high": 1, "low": 1, "close": 1} for _ in range(3)], symbol="BTC_USDT")
    bars.to_csv(tmp_path / "bars_1h.csv", index=False)
    funding = make_funding([("BTC_USDT", "2026-01-01T00:00:00Z", 0.0001)])
    funding.to_csv(tmp_path / "funding_history.csv", index=False)

    _, _, manifest = M.load_pinned_dataset(tmp_path)
    assert manifest["bars_sha256"] == M._sha256_file(tmp_path / "bars_1h.csv")
    assert manifest["funding_sha256"] == M._sha256_file(tmp_path / "funding_history.csv")
    assert manifest["symbols"]["BTC_USDT"]["bars_n"] == 3


# ------------------------------------------------------------------------
# Determinism
# ------------------------------------------------------------------------

def test_bootstrap_reproducible_with_same_seed():
    vals = np.array([0.5, -0.2, 0.3, 0.1, -0.4, 0.6, 0.2, -0.1])
    boot1 = M.V2.bootstrap_mean_ci(vals, n_boot=500, alpha=0.1, rng=np.random.default_rng(42))
    boot2 = M.V2.bootstrap_mean_ci(vals, n_boot=500, alpha=0.1, rng=np.random.default_rng(42))
    assert boot1 == boot2


# ------------------------------------------------------------------------
# Same-signal-path discipline: this script must never touch .23's
# allowlists or .39's promotion-gate API.
# ------------------------------------------------------------------------

def test_module_never_imports_or_calls_execution_or_promotion_modules():
    # The module docstring legitimately NAMES .23's allowlists and .39's
    # registry in prose (to document that they are untouched) -- what must
    # actually never happen is importing/loading/calling those modules as
    # code. Check only the code, not the leading module docstring.
    source = SCRIPT_PATH.read_text()
    code_only = source.split('"""', 2)[-1]  # drop the module docstring
    assert "aura_v05323_execution_specification_builder" not in code_only
    assert "aura_v05339_strategy_registry" not in code_only
    assert "record_evidence(" not in code_only
    assert ".record_evidence" not in code_only
    # Confirm _load_module is only ever called with the two research modules
    # this script is designed to reuse -- never an execution-spine module.
    import re
    loaded = re.findall(r'_load_module\("[^"]+",\s*"([^"]+)"\)', code_only)
    assert loaded == ["aura_exit_policy_backtest.py"]


def test_result_field_is_preliminary_verdict_not_verdict():
    # Guards against ever accidentally naming the field "verdict" the way
    # EPB/V2 do, which could be mistaken downstream for a .39 gate outcome.
    bars = make_bars([{"open": 100, "high": 100.1, "low": 99.9, "close": 100} for _ in range(5)])
    funding = make_funding([])
    results, _ = M.backtest_symbol_mexc(
        "TEST_USDT", bars, funding,
        n_boot=100, alpha=0.1, k_folds=4, max_hold_hours=5, fee_slippage_pct=0.0,
        grid=M.EPB.policy_grid((2.0,), (4.0,)), rng=np.random.default_rng(1),
    )
    for r in results:
        assert "preliminary_verdict" in r
        assert "verdict" not in r


# ------------------------------------------------------------------------
# End-to-end integration: real pinned dataset, real CLI entry point.
# ------------------------------------------------------------------------

REAL_DATA_DIR = ROOT / "AURA_CRYPTO"


@pytest.mark.skipif(not (REAL_DATA_DIR / "bars_1h.csv").is_file(), reason="pinned AURA_CRYPTO dataset not present")
def test_e2e_real_pinned_dataset_via_cli(tmp_path):
    outdir = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--outdir", str(outdir), "--n-boot", "200"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    assert "PRELIMINARY_NOT_VALIDATED" in proc.stdout

    summary = pd.read_csv(outdir / "mexc_crypto_signal_summary.csv")
    trades = pd.read_csv(outdir / "mexc_crypto_signal_trades.csv")
    report = (outdir / "mexc_crypto_signal_report.txt").read_text()

    assert set(summary["symbol"].unique()) <= {"BTC_USDT", "ETH_USDT"}
    assert set(summary["candidate"].unique()) == {"FROZEN", "MIRROR"}
    assert "preliminary_verdict" in summary.columns
    assert "verdict" not in summary.columns

    if len(trades):
        assert trades["net_return_pct"].notna().all()
        assert set(trades["direction"].unique()) <= {"LONG", "SHORT"}
        # LONG funding cost and SHORT funding cost must never be identical in
        # sign pattern across a nontrivial sample (sanity check on the sign fix).
        long_funding = trades.loc[trades["direction"] == "LONG", "funding_cost_pct"]
        short_funding = trades.loc[trades["direction"] == "SHORT", "funding_cost_pct"]
        if len(long_funding) and len(short_funding):
            assert not (long_funding.mean() == pytest.approx(short_funding.mean()) and long_funding.mean() != 0)

    assert "PRELIMINARY_NOT_VALIDATED" in report
    assert "permutation" in report.lower()
    assert "multiple-testing" in report.lower()


@pytest.mark.skipif(not (REAL_DATA_DIR / "bars_1h.csv").is_file(), reason="pinned AURA_CRYPTO dataset not present")
def test_e2e_reproducible_across_two_runs(tmp_path):
    out1, out2 = tmp_path / "out1", tmp_path / "out2"
    for outdir in (out1, out2):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--outdir", str(outdir), "--n-boot", "200", "--seed", "7"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=180,
        )
        assert proc.returncode == 0, proc.stderr

    s1 = pd.read_csv(out1 / "mexc_crypto_signal_summary.csv")
    s2 = pd.read_csv(out2 / "mexc_crypto_signal_summary.csv")
    pd.testing.assert_frame_equal(s1, s2)


@pytest.mark.skipif(not (REAL_DATA_DIR / "bars_1h.csv").is_file(), reason="pinned AURA_CRYPTO dataset not present")
def test_e2e_dataset_manifest_matches_committed_files():
    bars_df, funding_df, manifest = M.load_pinned_dataset(REAL_DATA_DIR)
    assert manifest["bars_sha256"] == M._sha256_file(REAL_DATA_DIR / "bars_1h.csv")
    assert manifest["funding_sha256"] == M._sha256_file(REAL_DATA_DIR / "funding_history.csv")
    assert "BTC_USDT" in manifest["symbols"]
    assert "ETH_USDT" in manifest["symbols"]
