"""
Volatility & liquidity regime gate.

Blocks (or allows, with evidence) a candidate entry based on whether the
CURRENT market microstructure/regime can safely absorb the planned trade.
All four checks below are evaluated every time and NEVER short-circuited —
every component's evidence is recorded even once an earlier check has
already decided the verdict, so the full picture is always available for
audit (same "always evaluate every component" philosophy this project uses
elsewhere for combined enforcement checks; see entry_filters.py).

Inputs, OBSERVED vs DERIVED:
  OBSERVED (read directly from a provider, not computed here):
    - order_book.get_depth_notional_usd / get_top_of_book_spread_bps
    - cross_exchange.get_reference_mid_price
    - window's raw OHLCV (high/low/close) — these are observed candles,
      not something this module fetches itself
  DERIVED (computed in this module from the OBSERVED inputs above):
    - liquidity-impact ratio (depth / planned_notional_usd)
    - Parkinson realized-volatility estimate and its trailing percentile
    - cross-exchange divergence z-score

FAIL-OPEN vs FAIL-CLOSED — deliberate, check-by-check (see inline comments
at each check for the specific reasoning):
  - Liquidity-impact ratio:  hard safety check -> fails CLOSED (blocks) on
    thin depth, and also fails CLOSED if `planned_notional_usd` is missing
    or non-positive (a caller bug should never silently pass).
  - Spread cap:              hard safety check -> fails CLOSED on a wide
    spread; if the provider can't produce a spread reading at all, that is
    treated as a thin/no-market signal and also fails CLOSED.
  - Realized-vol regime:     a SOFT regime read, not a hard safety gate.
    Fails OPEN (does not block) when there isn't enough trailing history to
    build a meaningful percentile — insufficient history is a sampling
    problem, not evidence that conditions are actually unsafe. The reason
    is still recorded so the gap is visible in evidence/audit.
  - Cross-exchange divergence: a hard DATA-QUALITY gate, not a regime read
    — if the reference venue has no usable price (provider returns None or
    raises) we cannot verify MEXC's own price isn't a stale/bad outlier, so
    this fails CLOSED (blocks) specifically because we are deliberately
    choosing "can't verify" == "treat as unsafe" for this one check, unlike
    the vol-regime check above.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from data_feeds import CrossExchangeReferenceProvider, OrderBookDepthProvider


@dataclass(frozen=True, slots=True)
class LiquidityRegimeConfig:
    """Thresholds for the liquidity/volatility regime gate.

    All defaults below are PROPOSED institutional-standard starting points,
    not empirically fit to this strategy or validated against live MEXC
    microstructure data — they are explicitly meant to be reviewed/recalibrated
    before this gate ever touches real capital, not blindly trusted.
    """

    # --- Liquidity-impact ratio ---
    # "Depth within DEPTH_BAND_BPS of mid must be at least
    # MIN_LIQUIDITY_IMPACT_RATIO times the planned trade notional."
    # 3.0x is a common conservative institutional starting point for a
    # single-clip market order against visible depth.
    DEPTH_BAND_BPS: float = 50.0
    MIN_LIQUIDITY_IMPACT_RATIO: float = 3.0

    # --- Spread cap, tiered by base asset (dict config, not per-symbol
    # if/else branches) ---
    SPREAD_CAP_BPS_BY_BASE: dict[str, float] = field(
        default_factory=lambda: {"BTC": 15.0, "ETH": 15.0}
    )
    SPREAD_CAP_BPS_DEFAULT: float = 25.0  # the other 9 pairs in the 11-pair universe

    # --- Realized-volatility regime (Parkinson estimator) ---
    CANDLE_TIMEFRAME_HOURS: float = 1.0  # matches mexc_bot/config.py TIMEFRAME="1h"
    VOL_LOOKBACK_HOURS: float = 24.0     # trailing window the Parkinson estimate covers
    VOL_HISTORY_LOOKBACK_DAYS: float = 90.0  # trailing distribution window
    MIN_VOL_HISTORY_SAMPLES: int = 30    # below this, fail OPEN on this check only (soft signal)
    VOL_PERCENTILE_BLOCK_THRESHOLD: float = 95.0

    # --- Cross-exchange divergence ---
    CROSS_EXCHANGE_DIVERGENCE_Z_BLOCK: float = 3.0
    CROSS_EXCHANGE_RETURN_HISTORY_BARS: int = 200  # bars used to estimate normal bar-to-bar noise
    MIN_CROSS_EXCHANGE_RETURN_SAMPLES: int = 20

    def spread_cap_bps_for_symbol(self, symbol: str) -> float:
        """'BTC/USDT:USDT' -> 'BTC' -> tiered cap, else SPREAD_CAP_BPS_DEFAULT."""
        base = symbol.split("/")[0].upper() if "/" in symbol else symbol.upper()
        return self.SPREAD_CAP_BPS_BY_BASE.get(base, self.SPREAD_CAP_BPS_DEFAULT)


@dataclass(frozen=True, slots=True)
class LiquidityRegimeVerdict:
    allowed: bool
    reasons: tuple[str, ...]
    evidence: dict


def _parkinson_vol_series(window: pd.DataFrame, bars_per_window: int) -> pd.Series:
    """Rolling Parkinson realized-volatility estimate using high/low, one
    value per bar, each covering the trailing `bars_per_window` candles.

    sigma_P^2 = (1 / (4 * ln(2) * n)) * sum(ln(high_i / low_i)^2)

    NOT annualized — this module only ever uses it for RELATIVE percentile
    ranking against its own trailing distribution, so the scale cancels out
    and annualizing would add nothing but a chance to get the scaling
    factor wrong. This estimator is NOT in core/indicators.py (that module
    only has close-based ATR/RSI/EMA/MACD/ADX), so it is implemented here,
    pure pandas/numpy, matching this project's existing style for
    indicator-style helpers.
    """
    log_hl = np.log(window["high"] / window["low"])
    park_factor = 1.0 / (4.0 * np.log(2.0))
    mean_sq = (log_hl ** 2).rolling(window=bars_per_window).mean()
    return np.sqrt(mean_sq * park_factor)


def evaluate_liquidity_regime(
    window: pd.DataFrame,
    symbol: str,
    *,
    order_book: OrderBookDepthProvider,
    cross_exchange: CrossExchangeReferenceProvider,
    planned_notional_usd: float,
    config: LiquidityRegimeConfig,
) -> LiquidityRegimeVerdict:
    """Evaluate all four liquidity/volatility regime checks for `symbol`
    against the candidate trade's `planned_notional_usd`. `window` is the
    same OHLCV DataFrame slice the strategy/backtester already has
    (`df.iloc[:i+1]` in the backtester, the full fetched `df` live) — see
    this project's backtester.py::simulate_symbol and
    live_bot.py::_process_symbol for where it comes from.

    Every check below runs unconditionally; `allowed` is the AND of all of
    them, and `reasons`/`evidence` always reflect every check, blocking or
    not — see the module docstring for why (audit visibility, no
    short-circuiting).
    """
    reasons: list[str] = []
    evidence: dict = {}

    # --- Check 1: liquidity-impact ratio (hard safety check -> fails CLOSED) ---
    if planned_notional_usd is None or planned_notional_usd <= 0:
        liquidity_ok = False
        reasons.append("invalid planned_notional_usd (<=0) — failing closed")
        evidence["liquidity_impact_ratio"] = None
    else:
        depth_usd = order_book.get_depth_notional_usd(symbol, config.DEPTH_BAND_BPS)
        ratio = depth_usd / planned_notional_usd
        evidence["depth_notional_usd"] = depth_usd
        evidence["liquidity_impact_ratio"] = ratio
        liquidity_ok = ratio >= config.MIN_LIQUIDITY_IMPACT_RATIO
        if not liquidity_ok:
            reasons.append(
                f"liquidity-impact ratio {ratio:.2f} < required "
                f"{config.MIN_LIQUIDITY_IMPACT_RATIO:.2f} "
                f"(depth ${depth_usd:,.0f} within {config.DEPTH_BAND_BPS:.0f}bps "
                f"vs planned ${planned_notional_usd:,.0f})"
            )

    # --- Check 2: spread cap (hard safety check -> fails CLOSED) ---
    spread_bps = order_book.get_top_of_book_spread_bps(symbol)
    cap_bps = config.spread_cap_bps_for_symbol(symbol)
    evidence["spread_bps"] = spread_bps
    evidence["spread_cap_bps"] = cap_bps
    spread_ok = spread_bps is not None and spread_bps <= cap_bps
    if not spread_ok:
        reasons.append(f"spread {spread_bps}bps exceeds cap {cap_bps:.1f}bps for {symbol}")

    # --- Check 3: realized-vol regime (SOFT -> fails OPEN on insufficient history) ---
    bars_per_hour = 1.0 / config.CANDLE_TIMEFRAME_HOURS
    vol_window_bars = max(2, round(config.VOL_LOOKBACK_HOURS * bars_per_hour))
    history_bars_cap = max(
        vol_window_bars + config.MIN_VOL_HISTORY_SAMPLES,
        round(config.VOL_HISTORY_LOOKBACK_DAYS * 24.0 * bars_per_hour),
    )
    recent = window.iloc[-history_bars_cap:] if len(window) > history_bars_cap else window
    park_series = _parkinson_vol_series(recent, vol_window_bars).dropna()

    vol_ok = True  # fail OPEN by default when we can't evaluate this check
    if len(park_series) < config.MIN_VOL_HISTORY_SAMPLES + 1:
        reasons.append(
            f"vol-regime check skipped (fail-open): only {len(park_series)} trailing "
            f"Parkinson samples, need >= {config.MIN_VOL_HISTORY_SAMPLES + 1} — soft "
            "regime signal, insufficient history does not block"
        )
        evidence["vol_percentile"] = None
    else:
        current_vol = park_series.iloc[-1]
        historical = park_series.iloc[:-1]
        percentile = float((historical < current_vol).mean() * 100.0)
        evidence["vol_percentile"] = percentile
        evidence["current_parkinson_vol"] = float(current_vol)
        vol_ok = percentile <= config.VOL_PERCENTILE_BLOCK_THRESHOLD
        if not vol_ok:
            reasons.append(
                f"realized-vol regime at {percentile:.1f}th percentile of trailing "
                f"{len(historical)} samples, exceeds block threshold "
                f"{config.VOL_PERCENTILE_BLOCK_THRESHOLD:.1f}"
            )

    # --- Check 4: cross-exchange divergence (hard DATA-QUALITY gate -> fails CLOSED) ---
    try:
        reference_price = cross_exchange.get_reference_mid_price(symbol)
    except Exception as exc:  # noqa: BLE001 - deliberately broad: any failure -> fail closed
        reference_price = None
        evidence["cross_exchange_error"] = repr(exc)

    current_price = float(window["close"].iloc[-1])
    if reference_price is None or reference_price <= 0:
        cross_ok = False
        reasons.append(
            "cross-exchange reference price unavailable — failing CLOSED "
            "(cannot verify MEXC price isn't a stale/bad outlier)"
        )
        evidence["cross_exchange_z"] = None
    else:
        returns_bps = window["close"].pct_change().iloc[-config.CROSS_EXCHANGE_RETURN_HISTORY_BARS:] * 10_000.0
        returns_bps = returns_bps.dropna()
        std_bps = float(returns_bps.std(ddof=1)) if len(returns_bps) >= config.MIN_CROSS_EXCHANGE_RETURN_SAMPLES else 0.0
        divergence_bps = (current_price - reference_price) / reference_price * 10_000.0
        evidence["reference_price"] = reference_price
        evidence["divergence_bps"] = divergence_bps

        if std_bps <= 0.0:
            # Not enough own-price history to know what "normal" bar-to-bar
            # noise looks like for this symbol -> cannot verify the
            # divergence is within-normal-noise, so fail CLOSED (same
            # reasoning as a missing reference price: "can't verify" means
            # "treat as unsafe" for this specific hard data-quality gate).
            cross_ok = False
            reasons.append(
                "cross-exchange z-score unavailable (insufficient own-price "
                "history to estimate normal bar noise) — failing CLOSED"
            )
            evidence["cross_exchange_z"] = None
        else:
            z = divergence_bps / std_bps
            evidence["cross_exchange_z"] = z
            cross_ok = abs(z) <= config.CROSS_EXCHANGE_DIVERGENCE_Z_BLOCK
            if not cross_ok:
                reasons.append(
                    f"cross-exchange divergence z={z:.2f} exceeds block threshold "
                    f"{config.CROSS_EXCHANGE_DIVERGENCE_Z_BLOCK:.2f} "
                    f"(MEXC={current_price}, reference={reference_price})"
                )

    allowed = liquidity_ok and spread_ok and vol_ok and cross_ok
    if allowed:
        reasons.append("all liquidity/volatility regime checks passed")

    return LiquidityRegimeVerdict(allowed=allowed, reasons=tuple(reasons), evidence=evidence)
