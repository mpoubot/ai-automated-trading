"""
Tests for Track A's institutional entry-filter layer. All data is
deterministic, fabricated OHLCV / fixture provider data — no network calls.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from data_feeds import SyntheticFixtureDataFeeds
from liquidity_regime_gate import LiquidityRegimeConfig, evaluate_liquidity_regime
from derivatives_positioning_overlay import (
    DerivativesPositioningConfig,
    evaluate_derivatives_positioning,
)
from macro_regime_overlay import (
    MacroRegimeConfig,
    compute_macro_regime,
    evaluate_macro_gate,
)
from entry_filters import CombinedFilterConfig, evaluate_entry_filters


SYMBOL = "BTC/USDT:USDT"


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _flat_ohlcv(n: int, price: float = 100.0, spread_frac: float = 0.0005,
                 drift_per_bar: float = 0.0) -> pd.DataFrame:
    """n bars of near-flat OHLCV with a tiny deterministic wiggle (so
    pct_change-based std isn't exactly zero) and optional linear drift."""
    idx = np.arange(n)
    closes = price + drift_per_bar * idx + 0.05 * np.sin(idx / 3.0)
    highs = closes * (1 + spread_frac)
    lows = closes * (1 - spread_frac)
    timestamps = pd.date_range("2024-01-01", periods=n, freq="1h")
    return pd.DataFrame({
        "timestamp": timestamps,
        "open": closes,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": np.full(n, 1_000.0),
    })


def _ohlcv_with_tail_vol_spike(n: int, spike_bars: int, price: float = 100.0) -> pd.DataFrame:
    """n bars, flat/tiny-range except the final `spike_bars` bars which get
    a much wider high/low range — drives the Parkinson rolling estimate's
    current value far above its own trailing distribution."""
    df = _flat_ohlcv(n, price=price)
    closes = df["close"].to_numpy()
    spike_start = n - spike_bars
    df.loc[spike_start:, "high"] = closes[spike_start:] * 1.05
    df.loc[spike_start:, "low"] = closes[spike_start:] * 0.95
    return df


def _make_trend_df(n: int, start: float, end: float, scale: float = 1.0) -> pd.DataFrame:
    """Linear close trend from `start` to `end` over n bars, scaled."""
    closes = np.linspace(start, end, n) * scale
    timestamps = pd.date_range("2024-01-01", periods=n, freq="1h")
    return pd.DataFrame({
        "timestamp": timestamps,
        "open": closes,
        "high": closes * 1.001,
        "low": closes * 0.999,
        "close": closes,
        "volume": np.full(n, 1_000.0),
    })


@dataclass
class _FixtureFundingProvider:
    """Minimal FundingRateProvider fixture for derivatives-overlay tests."""

    DATA_SOURCE_LABEL: str = "fixture_funding"
    IS_REAL_MARKET_DATA: bool = False
    rates: dict[str, float | None] = field(default_factory=dict)

    def get_current_funding_rate(self, symbol: str) -> float | None:
        return self.rates.get(symbol)

    def get_funding_rate_history(self, symbol: str, cutoff: datetime) -> pd.DataFrame:
        return pd.DataFrame(columns=["timestamp", "funding_rate"])


# ---------------------------------------------------------------------------
# liquidity_regime_gate.py
# ---------------------------------------------------------------------------

class TestLiquidityRegimeGate:
    def test_blocks_on_thin_depth(self):
        window = _flat_ohlcv(300)
        feeds = SyntheticFixtureDataFeeds(
            depth_notional_usd={(SYMBOL, 50.0): 1_000.0},
            spread_bps={SYMBOL: 5.0},
            reference_mid_price={SYMBOL: float(window["close"].iloc[-1])},
        )
        config = LiquidityRegimeConfig()
        verdict = evaluate_liquidity_regime(
            window, SYMBOL, order_book=feeds, cross_exchange=feeds,
            planned_notional_usd=10_000.0, config=config,
        )
        assert verdict.allowed is False
        assert any("liquidity-impact ratio" in r for r in verdict.reasons)

    def test_blocks_on_wide_spread(self):
        window = _flat_ohlcv(300)
        feeds = SyntheticFixtureDataFeeds(
            depth_notional_usd={(SYMBOL, 50.0): 1_000_000.0},
            spread_bps={SYMBOL: 50.0},  # exceeds BTC's 15bps tier cap
            reference_mid_price={SYMBOL: float(window["close"].iloc[-1])},
        )
        config = LiquidityRegimeConfig()
        verdict = evaluate_liquidity_regime(
            window, SYMBOL, order_book=feeds, cross_exchange=feeds,
            planned_notional_usd=1_000.0, config=config,
        )
        assert verdict.allowed is False
        assert any("spread" in r for r in verdict.reasons)

    def test_blocks_on_vol_regime_spike(self):
        window = _ohlcv_with_tail_vol_spike(n=300, spike_bars=24)
        feeds = SyntheticFixtureDataFeeds(
            depth_notional_usd={(SYMBOL, 50.0): 1_000_000.0},
            spread_bps={SYMBOL: 5.0},
            reference_mid_price={SYMBOL: float(window["close"].iloc[-1])},
        )
        config = LiquidityRegimeConfig()
        verdict = evaluate_liquidity_regime(
            window, SYMBOL, order_book=feeds, cross_exchange=feeds,
            planned_notional_usd=1_000.0, config=config,
        )
        assert verdict.allowed is False
        assert any("realized-vol regime" in r for r in verdict.reasons)
        assert verdict.evidence["vol_percentile"] > config.VOL_PERCENTILE_BLOCK_THRESHOLD

    def test_vol_regime_fails_open_on_insufficient_history(self):
        # Only 25 bars: enough for the cross-exchange return-noise estimate
        # (needs >= MIN_CROSS_EXCHANGE_RETURN_SAMPLES=20) but NOT enough
        # trailing Parkinson samples (needs >= MIN_VOL_HISTORY_SAMPLES+1=31)
        # -- isolates the vol-regime soft fail-open path.
        window = _flat_ohlcv(25, drift_per_bar=0.01)
        feeds = SyntheticFixtureDataFeeds(
            depth_notional_usd={(SYMBOL, 50.0): 1_000_000.0},
            spread_bps={SYMBOL: 5.0},
            reference_mid_price={SYMBOL: float(window["close"].iloc[-1])},
        )
        config = LiquidityRegimeConfig()
        verdict = evaluate_liquidity_regime(
            window, SYMBOL, order_book=feeds, cross_exchange=feeds,
            planned_notional_usd=1_000.0, config=config,
        )
        assert verdict.evidence["vol_percentile"] is None
        assert any("fail-open" in r for r in verdict.reasons)
        # Insufficient history must NOT block on its own.
        assert verdict.allowed is True

    def test_cross_exchange_fails_closed_on_missing_reference(self):
        window = _flat_ohlcv(300)
        feeds = SyntheticFixtureDataFeeds(
            depth_notional_usd={(SYMBOL, 50.0): 1_000_000.0},
            spread_bps={SYMBOL: 5.0},
            reference_mid_price={},  # no reference price available at all
        )
        config = LiquidityRegimeConfig()
        verdict = evaluate_liquidity_regime(
            window, SYMBOL, order_book=feeds, cross_exchange=feeds,
            planned_notional_usd=1_000.0, config=config,
        )
        assert verdict.allowed is False
        assert any("failing CLOSED" in r for r in verdict.reasons)

    def test_cross_exchange_blocks_on_divergence(self):
        window = _flat_ohlcv(300)
        current_price = float(window["close"].iloc[-1])
        feeds = SyntheticFixtureDataFeeds(
            depth_notional_usd={(SYMBOL, 50.0): 1_000_000.0},
            spread_bps={SYMBOL: 5.0},
            reference_mid_price={SYMBOL: current_price * 1.10},  # 10% away
        )
        config = LiquidityRegimeConfig()
        verdict = evaluate_liquidity_regime(
            window, SYMBOL, order_book=feeds, cross_exchange=feeds,
            planned_notional_usd=1_000.0, config=config,
        )
        assert verdict.allowed is False
        assert any("cross-exchange divergence" in r for r in verdict.reasons)

    def test_allows_when_all_checks_pass(self):
        window = _flat_ohlcv(300)
        feeds = SyntheticFixtureDataFeeds(
            depth_notional_usd={(SYMBOL, 50.0): 1_000_000.0},
            spread_bps={SYMBOL: 5.0},
            reference_mid_price={SYMBOL: float(window["close"].iloc[-1])},
        )
        config = LiquidityRegimeConfig()
        verdict = evaluate_liquidity_regime(
            window, SYMBOL, order_book=feeds, cross_exchange=feeds,
            planned_notional_usd=1_000.0, config=config,
        )
        assert verdict.allowed is True


# ---------------------------------------------------------------------------
# derivatives_positioning_overlay.py
# ---------------------------------------------------------------------------

class TestDerivativesPositioningOverlay:
    def test_blocks_on_extreme_funding(self):
        funding = _FixtureFundingProvider(rates={SYMBOL: 0.002})  # crowded-long rate
        feeds = SyntheticFixtureDataFeeds()
        config = DerivativesPositioningConfig()
        verdict = evaluate_derivatives_positioning(
            SYMBOL, "long", funding=funding, open_interest=feeds,
            liquidations=feeds, now=datetime.now(timezone.utc), config=config,
        )
        assert verdict.allowed is False
        assert any("funding extremity" in r for r in verdict.reasons)

    def test_funding_fails_open_when_unavailable(self):
        funding = _FixtureFundingProvider(rates={})  # no rate for this symbol
        feeds = SyntheticFixtureDataFeeds()
        config = DerivativesPositioningConfig()
        verdict = evaluate_derivatives_positioning(
            SYMBOL, "long", funding=funding, open_interest=feeds,
            liquidations=feeds, now=datetime.now(timezone.utc), config=config,
        )
        assert verdict.allowed is True
        assert any("funding rate unavailable" in r for r in verdict.reasons)

    def test_oi_acceleration_tightens_size_without_confirming_price(self):
        funding = _FixtureFundingProvider(rates={SYMBOL: 0.0})
        oi_series = pd.Series(
            [1_000.0, 1_200.0],  # +20% OI over the window
            index=pd.to_datetime(["2024-01-01T00:00:00", "2024-01-01T04:00:00"]),
        )
        feeds = SyntheticFixtureDataFeeds(open_interest_history={SYMBOL: oi_series})
        config = DerivativesPositioningConfig()
        verdict = evaluate_derivatives_positioning(
            SYMBOL, "long", funding=funding, open_interest=feeds,
            liquidations=feeds, now=datetime.now(timezone.utc), config=config,
            price_change_pct=-0.01,  # price flat/falling while OI rises
        )
        assert verdict.allowed is True  # NOT a hard veto
        assert verdict.size_multiplier == pytest.approx(config.OI_ACCEL_SIZE_MULTIPLIER)
        assert any("OI acceleration caution" in r for r in verdict.reasons)

    def test_oi_acceleration_no_action_when_price_confirms(self):
        funding = _FixtureFundingProvider(rates={SYMBOL: 0.0})
        oi_series = pd.Series(
            [1_000.0, 1_200.0],
            index=pd.to_datetime(["2024-01-01T00:00:00", "2024-01-01T04:00:00"]),
        )
        feeds = SyntheticFixtureDataFeeds(open_interest_history={SYMBOL: oi_series})
        config = DerivativesPositioningConfig()
        verdict = evaluate_derivatives_positioning(
            SYMBOL, "long", funding=funding, open_interest=feeds,
            liquidations=feeds, now=datetime.now(timezone.utc), config=config,
            price_change_pct=0.02,  # price rising too -> trend-confirming
        )
        assert verdict.allowed is True
        assert verdict.size_multiplier == 1.0

    def test_liquidation_cooldown_blocks_then_expires(self):
        funding = _FixtureFundingProvider(rates={SYMBOL: 0.0})
        feeds = SyntheticFixtureDataFeeds(
            liquidation_notional_usd={(SYMBOL, 15.0): 10_000_000.0},  # over threshold
        )
        config = DerivativesPositioningConfig()
        cooldown_state: dict[str, datetime] = {}
        t0 = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

        verdict1 = evaluate_derivatives_positioning(
            SYMBOL, "long", funding=funding, open_interest=feeds,
            liquidations=feeds, now=t0, config=config, cooldown_state=cooldown_state,
        )
        assert verdict1.allowed is False
        assert any("liquidation cascade detected" in r for r in verdict1.reasons)
        assert SYMBOL in cooldown_state

        # Still within the 30-minute cooldown, even though the feed itself
        # has gone quiet again -- cooldown state, not the live reading,
        # drives the block here.
        feeds.liquidation_notional_usd[(SYMBOL, 15.0)] = 0.0
        t1 = t0 + timedelta(minutes=10)
        verdict2 = evaluate_derivatives_positioning(
            SYMBOL, "long", funding=funding, open_interest=feeds,
            liquidations=feeds, now=t1, config=config, cooldown_state=cooldown_state,
        )
        assert verdict2.allowed is False
        assert any("cooldown active" in r for r in verdict2.reasons)

        # Cooldown window fully elapsed -> no longer blocked by it.
        t2 = t0 + timedelta(minutes=31)
        verdict3 = evaluate_derivatives_positioning(
            SYMBOL, "long", funding=funding, open_interest=feeds,
            liquidations=feeds, now=t2, config=config, cooldown_state=cooldown_state,
        )
        assert verdict3.allowed is True
        assert SYMBOL not in cooldown_state

    def test_liquidation_feed_error_fails_closed(self):
        class _BrokenLiquidations:
            DATA_SOURCE_LABEL = "broken"
            IS_REAL_MARKET_DATA = False

            def get_liquidation_notional_usd(self, symbol, lookback_minutes):
                raise RuntimeError("feed down")

        funding = _FixtureFundingProvider(rates={SYMBOL: 0.0})
        feeds = SyntheticFixtureDataFeeds()
        config = DerivativesPositioningConfig()
        verdict = evaluate_derivatives_positioning(
            SYMBOL, "long", funding=funding, open_interest=feeds,
            liquidations=_BrokenLiquidations(), now=datetime.now(timezone.utc), config=config,
        )
        assert verdict.allowed is False
        assert any("failing CLOSED" in r for r in verdict.reasons)


# ---------------------------------------------------------------------------
# macro_regime_overlay.py
# ---------------------------------------------------------------------------

def _macro_config(**overrides) -> MacroRegimeConfig:
    base = dict(
        CANDLE_TIMEFRAME_HOURS=1.0,
        EMA_SHORT_DAYS=3.0,
        EMA_LONG_DAYS=5.0,
        ETH_BTC_RATIO_LOOKBACK_DAYS=3.0,
        RELATIVE_STRENGTH_LOOKBACK_DAYS=3.0,
        HISTORY_BUFFER_BARS=5,
    )
    base.update(overrides)
    return MacroRegimeConfig(**base)


def _universe(n_bars: int, btc_trend, eth_scale: float, breadth_positive: int,
              breadth_negative: int) -> dict[str, pd.DataFrame]:
    """btc_trend: (start, end) for BTC's close. eth_scale: ETH = BTC * scale
    (constant scale -> flat ratio -> 'flat-to-rising'). Remaining symbols are
    built relative to BTC's own ratio (end/start) so "positive"/"negative"
    relative strength holds regardless of whether BTC itself is trending up
    or down: `breadth_positive` symbols move 1.5x BTC's ratio (outperform,
    positive RS vs BTC), `breadth_negative` symbols move 0.5x BTC's ratio
    (underperform, negative RS vs BTC).
    """
    btc_start, btc_end = btc_trend
    btc_ratio = btc_end / btc_start
    universe = {
        "BTC/USDT:USDT": _make_trend_df(n_bars, btc_start, btc_end),
        "ETH/USDT:USDT": _make_trend_df(n_bars, btc_start, btc_end, scale=eth_scale),
    }
    for i in range(breadth_positive):
        universe[f"POS{i}/USDT:USDT"] = _make_trend_df(n_bars, 100.0, 100.0 * btc_ratio * 1.5)
    for i in range(breadth_negative):
        universe[f"NEG{i}/USDT:USDT"] = _make_trend_df(n_bars, 100.0, 100.0 * btc_ratio * 0.5)
    return universe


class TestMacroRegimeOverlay:
    def test_risk_on_from_uptrend_and_rising_ratio(self):
        config = _macro_config()
        universe = _universe(150, btc_trend=(100.0, 200.0), eth_scale=1.0,
                              breadth_positive=6, breadth_negative=4)
        state = compute_macro_regime(
            universe, btc_symbol="BTC/USDT:USDT", eth_symbol="ETH/USDT:USDT", config=config
        )
        assert state.regime == "RISK_ON"

    def test_risk_off_from_downtrend(self):
        config = _macro_config()
        universe = _universe(150, btc_trend=(200.0, 100.0), eth_scale=1.0,
                              breadth_positive=3, breadth_negative=7)
        state = compute_macro_regime(
            universe, btc_symbol="BTC/USDT:USDT", eth_symbol="ETH/USDT:USDT", config=config
        )
        assert state.regime == "RISK_OFF"

    def test_unknown_on_insufficient_history(self):
        config = _macro_config()
        universe = _universe(10, btc_trend=(100.0, 110.0), eth_scale=1.0,
                              breadth_positive=1, breadth_negative=1)
        state = compute_macro_regime(
            universe, btc_symbol="BTC/USDT:USDT", eth_symbol="ETH/USDT:USDT", config=config
        )
        assert state.regime == "UNKNOWN"
        gate = evaluate_macro_gate(state, "long", config=config)
        assert gate.allowed is True
        gate_short = evaluate_macro_gate(state, "short", config=config)
        assert gate_short.allowed is True

    def test_risk_off_suppresses_longs_below_breadth_override(self):
        config = _macro_config()
        universe = _universe(150, btc_trend=(200.0, 100.0), eth_scale=1.0,
                              breadth_positive=3, breadth_negative=7)  # breadth 0.3 < 6/11
        state = compute_macro_regime(
            universe, btc_symbol="BTC/USDT:USDT", eth_symbol="ETH/USDT:USDT", config=config
        )
        assert state.regime == "RISK_OFF"
        gate = evaluate_macro_gate(state, "long", config=config)
        assert gate.allowed is False
        gate_short = evaluate_macro_gate(state, "short", config=config)
        assert gate_short.allowed is True  # shorts never suppressed in RISK_OFF

    def test_risk_off_breadth_override_allows_longs(self):
        config = _macro_config()
        universe = _universe(150, btc_trend=(200.0, 100.0), eth_scale=1.0,
                              breadth_positive=6, breadth_negative=4)  # breadth 0.6 >= 6/11
        state = compute_macro_regime(
            universe, btc_symbol="BTC/USDT:USDT", eth_symbol="ETH/USDT:USDT", config=config
        )
        assert state.regime == "RISK_OFF"
        assert state.breadth_pct >= config.BREADTH_OVERRIDE_THRESHOLD
        gate = evaluate_macro_gate(state, "long", config=config)
        assert gate.allowed is True
        assert any("override" in r for r in gate.reasons)

    def test_risk_on_suppresses_shorts_above_mirrored_breadth(self):
        config = _macro_config()
        universe = _universe(150, btc_trend=(100.0, 200.0), eth_scale=1.0,
                              breadth_positive=8, breadth_negative=2)  # high breadth
        state = compute_macro_regime(
            universe, btc_symbol="BTC/USDT:USDT", eth_symbol="ETH/USDT:USDT", config=config
        )
        assert state.regime == "RISK_ON"
        gate = evaluate_macro_gate(state, "short", config=config)
        assert gate.allowed is False
        gate_long = evaluate_macro_gate(state, "long", config=config)
        assert gate_long.allowed is True


# ---------------------------------------------------------------------------
# entry_filters.py — combined, always-evaluate-every-component
# ---------------------------------------------------------------------------

class TestCombinedEntryFilters:
    def _permissive_feeds(self, window: pd.DataFrame) -> SyntheticFixtureDataFeeds:
        return SyntheticFixtureDataFeeds(
            depth_notional_usd={(SYMBOL, 50.0): 1_000_000.0},
            spread_bps={SYMBOL: 5.0},
            reference_mid_price={SYMBOL: float(window["close"].iloc[-1])},
        )

    def test_allows_when_everything_passes(self):
        window = _flat_ohlcv(300)
        universe = {SYMBOL: window}
        feeds = self._permissive_feeds(window)
        funding = _FixtureFundingProvider(rates={SYMBOL: 0.0})
        combined_config = CombinedFilterConfig(
            macro=_macro_config(),  # UNKNOWN (no BTC/ETH symbols) -> neutral allow
        )
        verdict = evaluate_entry_filters(
            window, universe, SYMBOL, "long",
            planned_notional_usd=1_000.0,
            order_book=feeds, cross_exchange=feeds, funding=funding,
            open_interest=feeds, liquidations=feeds,
            now=datetime.now(timezone.utc), config=combined_config,
        )
        assert verdict.allowed is True
        assert set(verdict.component_verdicts) == {
            "liquidity_regime", "derivatives_positioning", "macro_regime",
        }

    def test_combined_never_short_circuits_on_block(self):
        window = _flat_ohlcv(300)
        universe = {SYMBOL: window}
        # Thin depth -> liquidity gate blocks; everything else is permissive.
        feeds = SyntheticFixtureDataFeeds(
            depth_notional_usd={(SYMBOL, 50.0): 1.0},
            spread_bps={SYMBOL: 5.0},
            reference_mid_price={SYMBOL: float(window["close"].iloc[-1])},
        )
        funding = _FixtureFundingProvider(rates={SYMBOL: 0.0})
        combined_config = CombinedFilterConfig(macro=_macro_config())
        verdict = evaluate_entry_filters(
            window, universe, SYMBOL, "long",
            planned_notional_usd=1_000.0,
            order_book=feeds, cross_exchange=feeds, funding=funding,
            open_interest=feeds, liquidations=feeds,
            now=datetime.now(timezone.utc), config=combined_config,
        )
        assert verdict.allowed is False

        # All three component verdicts must be present and FULLY evaluated
        # (not stubbed/skipped) even though liquidity already failed.
        components = verdict.component_verdicts
        assert set(components) == {
            "liquidity_regime", "derivatives_positioning", "macro_regime",
        }
        assert components["liquidity_regime"].allowed is False
        assert components["derivatives_positioning"].allowed is True
        assert components["derivatives_positioning"].evidence["funding_annualized"] == 0.0
        assert components["macro_regime"].allowed is True
        assert any("liquidity_regime" in r for r in verdict.reasons)

    def test_oi_size_multiplier_propagates_to_combined_verdict(self):
        window = _flat_ohlcv(300)
        # Force the trailing few closes to an unambiguous small decline so
        # the OI-acceleration check's internally-derived price_change_pct
        # is deterministically <= 0, regardless of this fixture's sine
        # wiggle (which is otherwise needed elsewhere to keep the
        # cross-exchange noise estimate non-zero).
        tail_closes = np.array([100.02, 100.01, 100.0, 99.99, 99.98])
        tail_idx = window.index[-5:]
        window.loc[tail_idx, "close"] = tail_closes
        window.loc[tail_idx, "open"] = tail_closes
        window.loc[tail_idx, "high"] = tail_closes * 1.0005
        window.loc[tail_idx, "low"] = tail_closes * 0.9995
        universe = {SYMBOL: window}
        feeds = self._permissive_feeds(window)
        feeds.open_interest_history[SYMBOL] = pd.Series(
            [1_000.0, 1_200.0],
            index=pd.to_datetime(["2024-01-01T00:00:00", "2024-01-01T04:00:00"]),
        )
        funding = _FixtureFundingProvider(rates={SYMBOL: 0.0})
        derivatives_config = DerivativesPositioningConfig(OI_ACCEL_WINDOW_HOURS=4.0)
        combined_config = CombinedFilterConfig(
            derivatives=derivatives_config, macro=_macro_config(),
        )
        verdict = evaluate_entry_filters(
            window, universe, SYMBOL, "long",
            planned_notional_usd=1_000.0,
            order_book=feeds, cross_exchange=feeds, funding=funding,
            open_interest=feeds, liquidations=feeds,
            now=datetime.now(timezone.utc), config=combined_config,
        )
        assert verdict.allowed is True
        assert verdict.size_multiplier == pytest.approx(derivatives_config.OI_ACCEL_SIZE_MULTIPLIER)
