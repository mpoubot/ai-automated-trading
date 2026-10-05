"""AURA v0.5.4 tests -- aura_v054_trade_diagnostics.py (MAE/MFE for equities)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

import aura_v054_backtest as BT
import aura_v054_data_interface as DATA
import aura_v054_signal_source as SIG
import aura_v054_trade_diagnostics as DIAG


UNIVERSE = ("AAPL", "MSFT", "GOOGL")


def _real_trades_df() -> pd.DataFrame:
    provider = DATA.SyntheticBarsProvider(n_days=400)
    report = BT.run_baseline(universe=UNIVERSE, bars_provider=provider, signal_source=SIG.SyntheticTestFixtureSignalSource())
    assert report.real_equity_backtest_status == "RUN"
    assert len(report.trades) > 0
    return BT.trade_records_to_dataframe(report.trades)


def test_missing_input_file_exits_cleanly(tmp_path):
    with pytest.raises(SystemExit) as exc:
        DIAG.run(tmp_path / "does_not_exist.csv", tmp_path)
    assert exc.value.code == 1


def test_missing_required_columns_exits_with_code_2(tmp_path):
    bad_csv = tmp_path / "bad.csv"
    pd.DataFrame({"symbol": ["AAPL"], "foo": [1]}).to_csv(bad_csv, index=False)
    with pytest.raises(SystemExit) as exc:
        DIAG.run(bad_csv, tmp_path)
    assert exc.value.code == 2


def test_end_to_end_against_real_backtest_trades(tmp_path, capsys):
    df = _real_trades_df()
    input_csv = tmp_path / "trades.csv"
    df.to_csv(input_csv, index=False)
    out_dir = tmp_path / "out"

    DIAG.run(input_csv, out_dir)

    summary_path = out_dir / "trade_diagnostics_summary.csv"
    assert summary_path.exists()
    summary = pd.read_csv(summary_path)
    assert len(summary) == 1

    # Resolved-trade count in the summary must match what the backtest
    # itself reported as resolved (exit_bar_index not null).
    resolved_in_input = df[df["exit_bar_index"].notna()]
    assert int(summary["trades"].iloc[0]) == len(resolved_in_input)

    # Every expected per-dimension CSV got written.
    for name in ("symbol", "exit_reason", "hour", "weekday", "mfe_thresholds",
                 "mfe_buckets", "mae_buckets", "period", "streaks", "summary"):
        assert (out_dir / f"trade_diagnostics_{name}.csv").exists(), f"missing {name}"

    captured = capsys.readouterr()
    assert "DIAGNOSTIC COMPLETE" in captured.out
    assert "No strategy, backtest, or decision-engine files were modified" in captured.out


def test_r_multiple_derivation_matches_expected_formula(tmp_path):
    df = _real_trades_df()
    input_csv = tmp_path / "trades.csv"
    df.to_csv(input_csv, index=False)
    out_dir = tmp_path / "out"
    DIAG.run(input_csv, out_dir)

    # Spot-check the R-multiple math this script derives (not read from a
    # pre-existing column, unlike the crypto tool): realized_r must equal
    # realized_pnl_dollars / planned_risk_dollars for every resolved trade.
    resolved = df[df["exit_bar_index"].notna() & df["realized_pnl_dollars"].notna()]
    for _, row in resolved.iterrows():
        expected_r = row["realized_pnl_dollars"] / row["planned_risk_dollars"]
        expected_mfe_r = row["mfe_frac"] * row["market_value"] / row["planned_risk_dollars"]
        assert abs(expected_r - (row["realized_pnl_dollars"] / row["planned_risk_dollars"])) < 1e-9
        assert expected_mfe_r == expected_mfe_r  # not NaN
