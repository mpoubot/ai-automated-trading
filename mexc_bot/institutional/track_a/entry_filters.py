"""
Combined entry-filter entry point: ANDs the liquidity/volatility regime
gate, the derivatives positioning overlay, and the macro regime overlay
into a single verdict for one candidate entry.

Every component gate below is called UNCONDITIONALLY, every time — never
short-circuited, even once an earlier component has already decided the
combined verdict is a block. This matches this project's existing
philosophy for combined enforcement checks: "always evaluate every
component for full audit visibility" (compare core/risk_manager.py's
CircuitBreaker, which records state/reason even once halted, rather than
stopping evaluation the instant one condition trips). Concretely here: all
three of `component_verdicts["liquidity_regime"]`,
`component_verdicts["derivatives_positioning"]` and
`component_verdicts["macro_regime"]` are ALWAYS present in the result, so a
human or an automated audit can always see every component's full reasons/
evidence, not just whichever one happened to block first.

================================================================================
EXACT ONE-LINE INTEGRATION (quoted, NOT applied — this module does not edit
backtester.py or live_bot.py).

backtester.py::simulate_symbol, right after `sig = strat.evaluate_signal(window)`
(see the quoted insertion point in the Track A task spec / backtester.py:218):

    sig = strat.evaluate_signal(window)
    if sig["signal"] is not None and entry_filters.evaluate_entry_filters(
            window, universe_bars, symbol, sig["signal"],
            planned_notional_usd=equity_tracker["equity"] * cfg.RISK_PER_TRADE_PCT * cfg.MAX_LEVERAGE,
            order_book=order_book, cross_exchange=cross_exchange, funding=funding,
            open_interest=open_interest, liquidations=liquidations,
            now=row["timestamp"], config=filter_config).allowed:
        ...  # existing plan/open_trade logic, unchanged

live_bot.py::_process_symbol, right after `strat.passes_universe_filter(...)`
and before `sig = strat.evaluate_signal(df)` (see live_bot.py:194-198):

    if not strat.passes_universe_filter(df, candidate["quote_volume_24h"],
                                         candidate["listing_age_days"]):
        return
    sig = strat.evaluate_signal(df)
    if sig["signal"] is not None and not entry_filters.evaluate_entry_filters(
            df, self.universe_bars, symbol, sig["signal"],
            planned_notional_usd=equity * cfg.RISK_PER_TRADE_PCT * cfg.MAX_LEVERAGE,
            order_book=order_book, cross_exchange=cross_exchange, funding=funding,
            open_interest=open_interest, liquidations=liquidations,
            now=datetime.now(timezone.utc), config=filter_config).allowed:
        return
================================================================================
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

from data_feeds import (
    CrossExchangeReferenceProvider,
    FundingRateProvider,
    LiquidationFeedProvider,
    OpenInterestProvider,
    OrderBookDepthProvider,
    SpotPriceProvider,
)
from derivatives_positioning_overlay import (
    DerivativesPositioningConfig,
    evaluate_derivatives_positioning,
)
from liquidity_regime_gate import LiquidityRegimeConfig, evaluate_liquidity_regime
from macro_regime_overlay import MacroRegimeConfig, compute_macro_regime, evaluate_macro_gate


@dataclass(frozen=True, slots=True)
class CombinedFilterConfig:
    """Bundles the three component gates' configs so callers pass one
    object. Each sub-config keeps its own documented, independently
    reviewable defaults (see liquidity_regime_gate.py,
    derivatives_positioning_overlay.py, macro_regime_overlay.py)."""

    liquidity: LiquidityRegimeConfig = field(default_factory=LiquidityRegimeConfig)
    derivatives: DerivativesPositioningConfig = field(default_factory=DerivativesPositioningConfig)
    macro: MacroRegimeConfig = field(default_factory=MacroRegimeConfig)
    btc_symbol: str = "BTC/USDT:USDT"
    eth_symbol: str = "ETH/USDT:USDT"


@dataclass(frozen=True, slots=True)
class CombinedFilterVerdict:
    allowed: bool
    reasons: tuple[str, ...]
    component_verdicts: dict[str, Any]
    size_multiplier: float = 1.0


def _price_change_pct_over_window(window: pd.DataFrame, lookback_hours: float,
                                   candle_timeframe_hours: float) -> float | None:
    """DERIVED: % change in `window`'s close over the trailing
    `lookback_hours`, at `candle_timeframe_hours`-per-candle spacing. Feeds
    the derivatives overlay's OI-acceleration check (see that module's
    docstring for why it takes this as a plain float rather than its own
    OHLCV dependency). None if there isn't enough history."""
    bars = max(1, round(lookback_hours / candle_timeframe_hours))
    if len(window) <= bars:
        return None
    price_now = float(window["close"].iloc[-1])
    price_then = float(window["close"].iloc[-1 - bars])
    if price_then == 0:
        return None
    return (price_now - price_then) / price_then


def evaluate_entry_filters(
    window: pd.DataFrame,
    universe_bars: dict[str, pd.DataFrame],
    symbol: str,
    side: str,
    *,
    planned_notional_usd: float,
    order_book: OrderBookDepthProvider,
    cross_exchange: CrossExchangeReferenceProvider,
    funding: FundingRateProvider,
    open_interest: OpenInterestProvider,
    liquidations: LiquidationFeedProvider,
    now: datetime,
    spot: SpotPriceProvider | None = None,
    config: CombinedFilterConfig | None = None,
    cooldown_state: dict[str, datetime] | None = None,
) -> CombinedFilterVerdict:
    """Single combined entry point other code would call right after
    `strat.evaluate_signal(...)` returns a non-None signal — see the
    module docstring above for the exact one-line integration in both
    backtester.py and live_bot.py (quoted there, not applied here).

    `window` is the candidate symbol's own OHLCV slice (same one
    `strat.evaluate_signal` was just called on). `universe_bars` is a
    dict of {symbol: OHLCV DataFrame} for the WHOLE traded universe,
    needed only by the macro regime overlay's cross-sectional breadth
    check. All three component gates are evaluated unconditionally (see
    module docstring) and ANDed into `allowed`.
    """
    config = config or CombinedFilterConfig()

    liquidity_verdict = evaluate_liquidity_regime(
        window,
        symbol,
        order_book=order_book,
        cross_exchange=cross_exchange,
        planned_notional_usd=planned_notional_usd,
        config=config.liquidity,
    )

    price_change_pct = _price_change_pct_over_window(
        window,
        config.derivatives.OI_ACCEL_WINDOW_HOURS,
        config.liquidity.CANDLE_TIMEFRAME_HOURS,
    )
    perp_price = float(window["close"].iloc[-1]) if len(window) else None

    derivatives_verdict = evaluate_derivatives_positioning(
        symbol,
        side,
        funding=funding,
        open_interest=open_interest,
        liquidations=liquidations,
        now=now,
        config=config.derivatives,
        spot=spot,
        perp_price=perp_price,
        price_change_pct=price_change_pct,
        cooldown_state=cooldown_state,
    )

    regime_state = compute_macro_regime(
        universe_bars,
        btc_symbol=config.btc_symbol,
        eth_symbol=config.eth_symbol,
        config=config.macro,
    )
    macro_verdict = evaluate_macro_gate(regime_state, side, config=config.macro)

    component_verdicts: dict[str, Any] = {
        "liquidity_regime": liquidity_verdict,
        "derivatives_positioning": derivatives_verdict,
        "macro_regime": macro_verdict,
    }

    allowed = liquidity_verdict.allowed and derivatives_verdict.allowed and macro_verdict.allowed
    reasons: list[str] = []
    reasons.extend(f"[liquidity_regime] {r}" for r in liquidity_verdict.reasons)
    reasons.extend(f"[derivatives_positioning] {r}" for r in derivatives_verdict.reasons)
    reasons.extend(f"[macro_regime] {r}" for r in macro_verdict.reasons)

    return CombinedFilterVerdict(
        allowed=allowed,
        reasons=tuple(reasons),
        component_verdicts=component_verdicts,
        size_multiplier=derivatives_verdict.size_multiplier,
    )
