import pandas as pd

from src.alpaca_research.strategy import FrozenStrategy


def test_bootstrap_strategy_is_hold_only():
    bars = pd.DataFrame({"close": [100.0, 101.0]}, index=pd.date_range("2026-01-01", periods=2, freq="h"))
    signal = FrozenStrategy(version="BOOTSTRAP").evaluate(bars, "BTC/USD")
    assert signal.action == "HOLD"
    assert signal.strategy_version == "BOOTSTRAP"
