"""
tests/test_signal_generation.py

PipelineValidationFixture's signal generation: deterministic bar-index
gating, no signal during ATR warmup, no signal off-schedule, and correct
Signal contents on-schedule -- including that it NEVER emits a "short"
signal (its docstring's own claim: "no market view, no trading logic").
"""
from core.indicators import atr as compute_atr
from core.instrument import Instrument
from strategies.pipeline_fixture import PipelineValidationFixture
from tests._fixtures import make_synthetic_ohlcv


def _prep(num_bars=200, interval_bars=10, atr_period=14, seed=1):
    ohlcv = make_synthetic_ohlcv(num_bars=num_bars, seed=seed)
    ohlcv["atr"] = compute_atr(ohlcv, period=atr_period)
    strat = PipelineValidationFixture(interval_bars=interval_bars, atr_warmup_period=atr_period)
    inst = Instrument.mexc_swap("BTC")
    return ohlcv, strat, inst


def test_no_signal_before_atr_warmup():
    ohlcv, strat, inst = _prep(num_bars=50, interval_bars=1, atr_period=14)
    # bar_index 0 is divisible by interval_bars=1 every bar, but ATR (Wilder,
    # ewm min_periods=atr_period) is NaN for the first (atr_period - 1) rows
    # -- evaluate() must return None there. The (atr_period)-th row (index
    # atr_period - 1) is the first with a valid ATR value.
    for i in range(13):
        sig = strat.evaluate(ohlcv.iloc[: i + 1], inst)
        assert sig is None, f"expected no signal at warmup bar {i} (ATR still NaN)"
    # And the first bar with a valid ATR (index 13) should be able to signal.
    sig = strat.evaluate(ohlcv.iloc[:14], inst)
    assert sig is not None, "expected a signal once ATR warmup completes (bar_index=13)"


def test_signal_only_on_scheduled_bars():
    ohlcv, strat, inst = _prep(num_bars=200, interval_bars=10, atr_period=14)
    on_schedule_hits = 0
    off_schedule_hits = 0
    for i in range(len(ohlcv)):
        window = ohlcv.iloc[: i + 1]
        sig = strat.evaluate(window, inst)
        bar_index = len(window) - 1
        if sig is not None:
            assert bar_index % 10 == 0, f"signal fired off-schedule at bar_index={bar_index}"
            on_schedule_hits += 1
        elif bar_index % 10 == 0 and bar_index >= 14:
            # scheduled bar past warmup but no signal -- should not happen
            off_schedule_hits += 1
    assert on_schedule_hits > 0, "expected at least one signal in 200 synthetic bars"
    assert off_schedule_hits == 0


def test_signal_is_always_long_never_short():
    ohlcv, strat, inst = _prep(num_bars=300, interval_bars=15, atr_period=14, seed=7)
    directions = set()
    for i in range(len(ohlcv)):
        sig = strat.evaluate(ohlcv.iloc[: i + 1], inst)
        if sig is not None:
            directions.add(sig.direction)
    assert directions == {"long"}, f"fixture must be long-only (no market view), got {directions}"


def test_signal_carries_atr_and_timestamp():
    ohlcv, strat, inst = _prep(num_bars=100, interval_bars=20, atr_period=14, seed=3)
    window = ohlcv.iloc[:20]  # bar_index 19 -- not on schedule; use a definitely-scheduled one
    window = ohlcv.iloc[:21]  # bar_index 20 -- on schedule (20 % 20 == 0), past warmup
    sig = strat.evaluate(window, inst)
    assert sig is not None
    assert sig.atr_at_signal is not None and sig.atr_at_signal > 0
    assert sig.timestamp == ohlcv.iloc[20]["timestamp"]
    assert "PIPELINE VALIDATION FIXTURE" in sig.reason
