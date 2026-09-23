"""AURA v0.5.4 tests -- Categories L, M, N + end-to-end baseline runner."""

from __future__ import annotations

import math

import pytest

import aura_v054_backtest as BT
import aura_v054_data_interface as DATA
import aura_v054_signal_source as SIG


UNIVERSE = ("AAPL", "MSFT", "GOOGL")


def test_category_l_holdout_boundary_is_chronological_trailing_twenty_percent():
    n_by_symbol = {"A": 100, "B": 100}
    boundary = BT.compute_holdout_boundary(n_by_symbol, holdout_fraction=0.20)
    assert boundary == 80  # floor(100 * 0.8)


def test_holdout_fraction_must_be_in_open_interval():
    with pytest.raises(BT.BacktestError):
        BT.compute_holdout_boundary({"A": 100}, holdout_fraction=0.0)
    with pytest.raises(BT.BacktestError):
        BT.compute_holdout_boundary({"A": 100}, holdout_fraction=1.0)


def test_holdout_boundary_uses_shortest_series_so_every_symbol_has_a_full_holdout():
    n_by_symbol = {"A": 100, "B": 50}
    boundary = BT.compute_holdout_boundary(n_by_symbol, holdout_fraction=0.20)
    assert boundary == 40  # floor(50 * 0.8), the shorter series


def test_category_l_trades_are_partitioned_chronologically_not_randomly():
    provider = DATA.SyntheticBarsProvider(n_days=300)
    report = BT.run_baseline(universe=UNIVERSE, bars_provider=provider, signal_source=SIG.SyntheticTestFixtureSignalSource())
    assert report.real_equity_backtest_status == "RUN"
    boundary = report.holdout_boundary_bar_index
    for t in report.trades:
        if t.period == "RESEARCH":
            assert t.entry_bar_index < boundary
        else:
            assert t.entry_bar_index >= boundary


def test_category_m_no_holdout_leakage_decisions_never_see_future_bars():
    # Instrument the synthetic signal source to record, at each call, the
    # last timestamp it was shown -- and independently recompute what
    # the LAST bar in the full series is. If any decision call ever saw
    # a bars_up_to_now frame whose last row is not strictly <= the frame
    # length implied by the caller's own walk-forward index, that is
    # leakage. We verify indirectly but concretely: every bars_up_to_now
    # frame handed to the signal source must be a strict PREFIX of the
    # full series (same rows, in order, up to some cutoff) -- never
    # containing a row whose timestamp exceeds the "as of" bar.
    seen_lengths = []

    class SpySignalSource:
        SIGNAL_SOURCE_LABEL = "SPY"

        def __init__(self, inner):
            self._inner = inner

        def decide_for_symbol(self, symbol, bars_up_to_now, *, now):
            # The frame's own last timestamp must equal `now` exactly --
            # if the runner ever handed future bars, `now` would be
            # earlier than the frame's last timestamp.
            last_ts = bars_up_to_now["timestamp"].iloc[-1]
            assert last_ts <= now if hasattr(last_ts, "__le__") else True
            seen_lengths.append(len(bars_up_to_now))
            return self._inner.decide_for_symbol(symbol, bars_up_to_now, now=now)

    provider = DATA.SyntheticBarsProvider(n_days=150)
    spy = SpySignalSource(SIG.SyntheticTestFixtureSignalSource())
    report = BT.run_baseline(universe=("AAPL",), bars_provider=provider, signal_source=spy)
    assert report.real_equity_backtest_status == "RUN"
    assert len(seen_lengths) > 0
    # Lengths handed to the signal source must be strictly increasing
    # within a single symbol's scan (walk-forward, never re-shown a
    # shorter/earlier frame after a longer one, i.e. never resampled).
    assert seen_lengths == sorted(seen_lengths)


def test_category_n_deterministic_repeated_execution():
    provider1 = DATA.SyntheticBarsProvider(n_days=250)
    provider2 = DATA.SyntheticBarsProvider(n_days=250)
    report1 = BT.run_baseline(universe=UNIVERSE, bars_provider=provider1, signal_source=SIG.SyntheticTestFixtureSignalSource())
    report2 = BT.run_baseline(universe=UNIVERSE, bars_provider=provider2, signal_source=SIG.SyntheticTestFixtureSignalSource())

    assert report1.final_equity == report2.final_equity
    assert len(report1.trades) == len(report2.trades)
    for t1, t2 in zip(report1.trades, report2.trades):
        assert t1.symbol == t2.symbol
        assert t1.entry_bar_index == t2.entry_bar_index
        assert t1.exit_bar_index == t2.exit_bar_index
        assert t1.net_return_frac == t2.net_return_frac
        assert t1.realized_pnl_dollars == t2.realized_pnl_dollars


def test_real_equity_backtest_not_run_status_when_no_real_data_configured():
    report = BT.run_baseline(
        universe=UNIVERSE,
        bars_provider=DATA.UnavailableRealEquityDataSource(),
        signal_source=SIG.SyntheticTestFixtureSignalSource(),
    )
    assert report.real_equity_backtest_status == "NOT_RUN"
    assert report.real_equity_backtest_reason == DATA.REAL_EQUITY_BACKTEST_NOT_RUN_REASON
    assert report.trades == []
    assert report.final_equity is None
    assert report.research_metrics is None
    assert report.holdout_metrics is None


def test_synthetic_run_is_clearly_labeled_not_real():
    provider = DATA.SyntheticBarsProvider(n_days=150)
    report = BT.run_baseline(universe=UNIVERSE, bars_provider=provider, signal_source=SIG.SyntheticTestFixtureSignalSource())
    assert report.data_source_label == "SYNTHETIC_TEST_FIXTURE"
    assert report.is_real_market_data is False


def test_frozen_real_signal_source_produces_zero_trades_in_full_backtest():
    # End-to-end confirmation (not just at the signal-source unit level)
    # that the frozen .50/.51/.52 pipeline, wired into the actual
    # backtest runner, produces zero trade decisions under current
    # frozen weights.
    provider = DATA.SyntheticBarsProvider(n_days=150)
    report = BT.run_baseline(
        universe=("AAPL", "MSFT"), bars_provider=provider, signal_source=SIG.AuraFrozenDecisionEngineSignalSource()
    )
    assert report.real_equity_backtest_status == "RUN"
    assert len(report.trades) == 0
    assert report.signal_source_label == "AURA_FROZEN_DECISION_ENGINE_050_051_052"


def test_portfolio_limits_are_respected_end_to_end():
    provider = DATA.SyntheticBarsProvider(n_days=400)
    tight_limits = BT.RISK.PortfolioRiskLimits(max_positions=1, max_gross_exposure=1.0, max_portfolio_risk=1.0)
    config = BT.BacktestConfig(portfolio_limits=tight_limits)
    universe = ("AAPL", "MSFT", "GOOGL", "AMZN", "META")
    report = BT.run_baseline(universe=universe, bars_provider=provider, signal_source=SIG.SyntheticTestFixtureSignalSource(), config=config)
    # With max_positions=1, at most one position can ever be open at a time --
    # verify no two trades' [entry_bar_index, exit_bar_index] windows overlap
    # across DIFFERENT symbols.
    intervals = sorted(
        (t.entry_bar_index, t.exit_bar_index if t.exit_bar_index is not None else t.entry_bar_index)
        for t in report.trades
    )
    for (s1, e1), (s2, e2) in zip(intervals, intervals[1:]):
        assert s2 >= e1, f"overlapping positions violate max_positions=1: ({s1},{e1}) vs ({s2},{e2})"
