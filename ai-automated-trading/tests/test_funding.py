"""
tests/test_funding.py

Funding handling end-to-end: the backtest engine actually charges/credits
funding on open positions at the configured interval, funding is never
silently skipped (falls back to a documented constant when no real rate is
available, and that fact is recorded on the event), and
core/dataset.py::load_funding()'s coverage-gap metadata is what
research/run_first_experiment.py threads into each ResultRecord's
funding_data_available / funding_coverage_gap_vs_ohlcv fields (the concrete
requirement: "where funding history is unavailable... the result must
explicitly record that limitation").
"""
import pandas as pd

from core.backtest_engine import ExitConfig, run_backtest
from core.costs import CostModel
from core.indicators import atr as compute_atr
from core.instrument import Instrument
from core.risk import RiskConfig
from strategies.pipeline_fixture import PipelineValidationFixture
from tests._fixtures import make_synthetic_funding, make_synthetic_ohlcv


def test_funding_events_emitted_for_long_held_position():
    # Long warmup + a long holding period (no exit before the time stop) so
    # at least one 8h funding interval is guaranteed to elapse.
    ohlcv = make_synthetic_ohlcv(num_bars=100, seed=21, drift=0.0, vol=0.0005)
    ohlcv["atr"] = compute_atr(ohlcv, period=14)
    funding_df = make_synthetic_funding(ohlcv, rate=0.0002, interval_hours=8)
    strategy = PipelineValidationFixture(interval_bars=50)  # one entry, near the start
    inst = Instrument.mexc_swap("BTC")
    cost_model = CostModel(funding_df=funding_df, funding_interval_hours=8.0)

    result = run_backtest(strategy, inst, ohlcv, cost_model, RiskConfig(), 500.0, ExitConfig(time_stop_bars=48))

    funding_events = [e for e in result.event_log if e["type"] == "funding"]
    assert funding_events, "expected at least one funding charge on a long-held position"
    assert all(e["funding_rate_is_real"] for e in funding_events)


def test_funding_falls_back_but_flags_non_real_rate():
    ohlcv = make_synthetic_ohlcv(num_bars=100, seed=22, drift=0.0, vol=0.0005)
    ohlcv["atr"] = compute_atr(ohlcv, period=14)
    strategy = PipelineValidationFixture(interval_bars=50)
    inst = Instrument.mexc_swap("BTC")
    # No funding_df at all -> every funding charge must use the fallback and
    # be flagged funding_rate_is_real=False, never silently treated as real.
    cost_model = CostModel(funding_df=None, funding_rate_fallback=0.00013, funding_interval_hours=8.0)

    result = run_backtest(strategy, inst, ohlcv, cost_model, RiskConfig(), 500.0, ExitConfig(time_stop_bars=48))

    funding_events = [e for e in result.event_log if e["type"] == "funding"]
    assert funding_events, "expected funding charges even without real funding data (fallback path)"
    assert all(e["funding_rate_is_real"] is False for e in funding_events)


def test_funding_is_never_silently_zeroed_when_position_held_across_interval():
    ohlcv = make_synthetic_ohlcv(num_bars=100, seed=23, drift=0.0, vol=0.0005)
    ohlcv["atr"] = compute_atr(ohlcv, period=14)
    funding_df = make_synthetic_funding(ohlcv, rate=0.0005, interval_hours=8)
    strategy = PipelineValidationFixture(interval_bars=50)
    inst = Instrument.mexc_swap("BTC")
    cost_model = CostModel(funding_df=funding_df, funding_interval_hours=8.0)

    result = run_backtest(strategy, inst, ohlcv, cost_model, RiskConfig(), 500.0, ExitConfig(time_stop_bars=48))
    funding_events = [e for e in result.event_log if e["type"] == "funding"]
    assert funding_events
    assert any(abs(e["pnl"]) > 0 for e in funding_events), "funding charge computed as exactly zero is suspicious for a non-zero rate"


def test_dataset_funding_coverage_gap_is_recorded_not_dropped(dataset_root):
    """End-to-end with the real dataset: BTC's funding history is known
    (per manifest.json) to start after its OHLCV window -- confirm this
    survives into load_funding()'s metadata."""
    from core import dataset as ds
    handle = ds.open_dataset(dataset_root)
    _df, meta = ds.load_funding(handle, Instrument.mexc_swap("BTC"))
    assert meta["funding_coverage_gap_vs_ohlcv"] is True
    assert meta["funding_coverage_start"] is not None
