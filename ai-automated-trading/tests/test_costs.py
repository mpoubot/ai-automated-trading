"""
tests/test_costs.py

CostModel: fee/slippage arithmetic, and funding_rate_at()'s real-vs-fallback
behavior (correctness of the lookup itself; funding accrual mechanics and
the "unavailable funding must be recorded, not ignored" requirement are
covered in tests/test_funding.py).
"""
import pandas as pd

from core.costs import CostModel


def test_fee_and_slippage_arithmetic():
    cm = CostModel(fee_pct=0.001, slippage_pct=0.0005)
    assert cm.fee(price=100.0, quantity=2.0) == 100.0 * 2.0 * 0.001
    assert cm.slippage(price=100.0, quantity=2.0) == 100.0 * 2.0 * 0.0005


def test_funding_rate_at_uses_fallback_when_no_funding_df():
    cm = CostModel(funding_rate_fallback=0.00025, funding_df=None)
    rate, is_real = cm.funding_rate_at(pd.Timestamp("2025-01-01"))
    assert rate == 0.00025
    assert is_real is False


def test_funding_rate_at_uses_fallback_when_funding_df_empty():
    cm = CostModel(funding_rate_fallback=0.00025, funding_df=pd.DataFrame(columns=["timestamp", "funding_rate"]))
    rate, is_real = cm.funding_rate_at(pd.Timestamp("2025-01-01"))
    assert rate == 0.00025
    assert is_real is False


def test_funding_rate_at_uses_fallback_before_first_real_row():
    funding_df = pd.DataFrame({
        "timestamp": [pd.Timestamp("2025-03-01"), pd.Timestamp("2025-03-02")],
        "funding_rate": [0.0001, 0.0002],
    })
    cm = CostModel(funding_rate_fallback=0.00025, funding_df=funding_df)
    rate, is_real = cm.funding_rate_at(pd.Timestamp("2025-01-01"))  # before any real row
    assert rate == 0.00025
    assert is_real is False


def test_funding_rate_at_returns_most_recent_real_rate():
    funding_df = pd.DataFrame({
        "timestamp": [pd.Timestamp("2025-03-01"), pd.Timestamp("2025-03-02"), pd.Timestamp("2025-03-03")],
        "funding_rate": [0.0001, 0.0002, 0.0003],
    })
    cm = CostModel(funding_df=funding_df)
    rate, is_real = cm.funding_rate_at(pd.Timestamp("2025-03-02 12:00:00"))
    assert rate == 0.0002  # most recent at-or-before, not the last row overall
    assert is_real is True


def test_funding_pnl_sign_convention_long_pays_positive_rate():
    funding_df = pd.DataFrame({"timestamp": [pd.Timestamp("2025-03-01")], "funding_rate": [0.001]})
    cm = CostModel(funding_df=funding_df)
    pnl, is_real = cm.funding_pnl("long", notional=1000.0, ts=pd.Timestamp("2025-03-02"), intervals=1)
    assert pnl < 0  # long pays when rate is positive
    assert is_real is True


def test_funding_pnl_sign_convention_short_receives_positive_rate():
    funding_df = pd.DataFrame({"timestamp": [pd.Timestamp("2025-03-01")], "funding_rate": [0.001]})
    cm = CostModel(funding_df=funding_df)
    pnl, is_real = cm.funding_pnl("short", notional=1000.0, ts=pd.Timestamp("2025-03-02"), intervals=1)
    assert pnl > 0  # short receives when rate is positive
