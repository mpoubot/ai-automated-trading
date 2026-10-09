"""
Cross-sectional crypto momentum / BTC-ETH macro regime overlay.

Unlike liquidity_regime_gate.py and derivatives_positioning_overlay.py,
this overlay needs NO new data feed — it is computed entirely from OHLCV
candles the caller already has for the whole universe (the same `window`
DataFrames backtester.py/live_bot.py already fetch per symbol), which is
why its functions take a `dict[symbol, DataFrame]` for the universe rather
than any provider Protocol from data_feeds.py.

Inputs, OBSERVED vs DERIVED:
  OBSERVED: each symbol's OHLCV `close` series in `universe_bars`.
  DERIVED (computed here): BTC's 50d/200d EMA (via core.indicators.ema,
    REUSED rather than reimplemented), the ETH/BTC price ratio trend, each
    symbol's 20d relative strength vs BTC, and `breadth_pct`.

FAIL-OPEN / neutral by design: `compute_macro_regime` returns regime
"UNKNOWN" whenever there isn't enough trailing history to evaluate BTC's
EMAs or the ETH/BTC ratio (missing symbol, short history, etc.), and
`evaluate_macro_gate` always ALLOWS both sides on "UNKNOWN". This overlay
is a BREADTH/SENTIMENT read, not a safety gate — insufficient data here
means "we don't know the regime," not "trading is unsafe," so it must
never silently block all trading just because history is thin (e.g. early
in a backtest, or a newly added symbol).
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from core import indicators as ind


@dataclass(frozen=True, slots=True)
class MacroRegimeConfig:
    """Thresholds/windows for the macro regime overlay. Defaults are
    PROPOSED starting points (standard 50d/200d trend-following convention
    plus a majority-of-universe breadth override), not yet empirically
    validated for this specific 11-pair universe.
    """

    CANDLE_TIMEFRAME_HOURS: float = 1.0  # matches mexc_bot/config.py TIMEFRAME="1h"
    EMA_SHORT_DAYS: float = 50.0
    EMA_LONG_DAYS: float = 200.0
    ETH_BTC_RATIO_LOOKBACK_DAYS: float = 20.0
    RELATIVE_STRENGTH_LOOKBACK_DAYS: float = 20.0
    HISTORY_BUFFER_BARS: int = 5  # extra bars beyond the longest EMA before trusting it
    # 6/11 ~= 0.5454... — "a clear majority of the 11-pair universe already
    # outperforming BTC" overrides the regime suppression below.
    BREADTH_OVERRIDE_THRESHOLD: float = 6.0 / 11.0

    def bars_per_day(self) -> float:
        return 24.0 / self.CANDLE_TIMEFRAME_HOURS


@dataclass(frozen=True, slots=True)
class MacroRegimeState:
    regime: str  # "RISK_ON" | "RISK_OFF" | "UNKNOWN"
    breadth_pct: float
    evidence: dict


@dataclass(frozen=True, slots=True)
class MacroRegimeVerdict:
    allowed: bool
    reasons: tuple[str, ...]
    evidence: dict


def compute_macro_regime(
    universe_bars: dict[str, pd.DataFrame],
    *,
    btc_symbol: str,
    eth_symbol: str,
    config: MacroRegimeConfig,
) -> MacroRegimeState:
    """RISK_ON when BTC trades above both its 50d and 200d EMA AND the
    ETH/BTC price ratio is flat-to-rising over the lookback window;
    RISK_OFF otherwise; UNKNOWN if there isn't enough trailing history for
    BTC or ETH to evaluate either leg (fail-open/neutral — see module
    docstring).

    `breadth_pct` is the fraction of the (non-BTC) universe with positive
    trailing relative strength vs BTC over `RELATIVE_STRENGTH_LOOKBACK_DAYS`.
    """
    evidence: dict = {}
    bars_per_day = config.bars_per_day()
    ema_short_bars = max(2, round(config.EMA_SHORT_DAYS * bars_per_day))
    ema_long_bars = max(2, round(config.EMA_LONG_DAYS * bars_per_day))
    required_bars = ema_long_bars + config.HISTORY_BUFFER_BARS

    btc_df = universe_bars.get(btc_symbol)
    if btc_df is None or len(btc_df) < required_bars:
        evidence["reason"] = (
            f"BTC history insufficient ({0 if btc_df is None else len(btc_df)} bars, "
            f"need >= {required_bars})"
        )
        return MacroRegimeState(regime="UNKNOWN", breadth_pct=0.0, evidence=evidence)

    eth_df = universe_bars.get(eth_symbol)
    ratio_lookback_bars = max(1, round(config.ETH_BTC_RATIO_LOOKBACK_DAYS * bars_per_day))
    if eth_df is None or len(eth_df) < required_bars or len(eth_df) <= ratio_lookback_bars:
        evidence["reason"] = "ETH history insufficient to evaluate ETH/BTC ratio leg"
        return MacroRegimeState(regime="UNKNOWN", breadth_pct=0.0, evidence=evidence)

    btc_close = btc_df["close"]
    btc_ema_short = float(ind.ema(btc_close, ema_short_bars).iloc[-1])
    btc_ema_long = float(ind.ema(btc_close, ema_long_bars).iloc[-1])
    btc_price = float(btc_close.iloc[-1])
    btc_above_emas = btc_price > btc_ema_short and btc_price > btc_ema_long
    evidence["btc_price"] = btc_price
    evidence["btc_ema_short"] = btc_ema_short
    evidence["btc_ema_long"] = btc_ema_long
    evidence["btc_above_emas"] = btc_above_emas

    # Align ETH/BTC on shared index positions (both fetched on the same
    # TIMEFRAME, same convention as everywhere else in this codebase) and
    # compare the ratio now vs `ratio_lookback_bars` candles ago.
    n = min(len(btc_close), len(eth_df["close"]))
    ratio = eth_df["close"].iloc[:n].reset_index(drop=True) / btc_close.iloc[:n].reset_index(drop=True)
    ratio_now = float(ratio.iloc[-1])
    ratio_then = float(ratio.iloc[-1 - ratio_lookback_bars])
    flat_to_rising = ratio_now >= ratio_then
    evidence["eth_btc_ratio_now"] = ratio_now
    evidence["eth_btc_ratio_then"] = ratio_then
    evidence["eth_btc_flat_to_rising"] = flat_to_rising

    regime = "RISK_ON" if (btc_above_emas and flat_to_rising) else "RISK_OFF"

    # --- Breadth: fraction of non-BTC universe with positive RS vs BTC ---
    rs_lookback_bars = max(1, round(config.RELATIVE_STRENGTH_LOOKBACK_DAYS * bars_per_day))
    btc_rs_then = float(btc_close.iloc[-1 - rs_lookback_bars]) if len(btc_close) > rs_lookback_bars else None
    evaluated = 0
    positive = 0
    per_symbol_rs: dict[str, float] = {}
    if btc_rs_then:
        for sym, df in universe_bars.items():
            if sym == btc_symbol:
                continue
            if df is None or len(df) <= rs_lookback_bars:
                continue
            price_now = float(df["close"].iloc[-1])
            price_then = float(df["close"].iloc[-1 - rs_lookback_bars])
            if price_then == 0 or btc_rs_then == 0:
                continue
            rs = (price_now / price_then) / (btc_price / btc_rs_then) - 1.0
            per_symbol_rs[sym] = rs
            evaluated += 1
            if rs > 0:
                positive += 1

    breadth_pct = (positive / evaluated) if evaluated > 0 else 0.0
    evidence["breadth_evaluated_count"] = evaluated
    evidence["breadth_positive_count"] = positive
    evidence["per_symbol_relative_strength"] = per_symbol_rs

    return MacroRegimeState(regime=regime, breadth_pct=breadth_pct, evidence=evidence)


def evaluate_macro_gate(
    regime_state: MacroRegimeState,
    side: str,
    *,
    config: MacroRegimeConfig,
) -> MacroRegimeVerdict:
    """Suppress new longs in RISK_OFF (unless breadth_pct meets the
    override threshold), suppress new shorts in RISK_ON (mirror image).
    UNKNOWN always allows both sides — neutral/fail-open, see module
    docstring for why.
    """
    reasons: list[str] = []
    evidence = {"regime": regime_state.regime, "breadth_pct": regime_state.breadth_pct}

    if regime_state.regime == "UNKNOWN":
        reasons.append("macro regime UNKNOWN — neutral/fail-open, both sides allowed")
        return MacroRegimeVerdict(allowed=True, reasons=tuple(reasons), evidence=evidence)

    if regime_state.regime == "RISK_OFF":
        if side == "long":
            if regime_state.breadth_pct >= config.BREADTH_OVERRIDE_THRESHOLD:
                reasons.append(
                    f"RISK_OFF but breadth {regime_state.breadth_pct:.1%} >= override "
                    f"threshold {config.BREADTH_OVERRIDE_THRESHOLD:.1%} — long allowed"
                )
                return MacroRegimeVerdict(allowed=True, reasons=tuple(reasons), evidence=evidence)
            reasons.append(
                f"RISK_OFF regime suppresses new longs (breadth "
                f"{regime_state.breadth_pct:.1%} < override threshold "
                f"{config.BREADTH_OVERRIDE_THRESHOLD:.1%})"
            )
            return MacroRegimeVerdict(allowed=False, reasons=tuple(reasons), evidence=evidence)
        reasons.append("RISK_OFF regime does not suppress shorts")
        return MacroRegimeVerdict(allowed=True, reasons=tuple(reasons), evidence=evidence)

    # regime_state.regime == "RISK_ON"
    if side == "short":
        # Mirror of the RISK_OFF override: a very LOW breadth reading means
        # leadership is narrow even though BTC itself is technically
        # RISK_ON, which weakens confidence in the regime read enough to
        # allow shorts through anyway.
        mirrored_override = 1.0 - config.BREADTH_OVERRIDE_THRESHOLD
        if regime_state.breadth_pct <= mirrored_override:
            reasons.append(
                f"RISK_ON but breadth {regime_state.breadth_pct:.1%} <= mirrored override "
                f"threshold {mirrored_override:.1%} (narrow leadership) — short allowed"
            )
            return MacroRegimeVerdict(allowed=True, reasons=tuple(reasons), evidence=evidence)
        reasons.append(
            f"RISK_ON regime suppresses new shorts (breadth "
            f"{regime_state.breadth_pct:.1%} > mirrored override threshold {mirrored_override:.1%})"
        )
        return MacroRegimeVerdict(allowed=False, reasons=tuple(reasons), evidence=evidence)
    reasons.append("RISK_ON regime does not suppress longs")
    return MacroRegimeVerdict(allowed=True, reasons=tuple(reasons), evidence=evidence)
