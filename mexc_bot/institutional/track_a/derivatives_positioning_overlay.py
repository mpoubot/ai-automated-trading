"""
Derivatives positioning overlay: funding extremity, perp/spot basis,
open-interest acceleration, and liquidation-cascade cooldown.

Inputs, OBSERVED vs DERIVED:
  OBSERVED:
    - funding.get_current_funding_rate / get_funding_rate_history
    - open_interest.get_open_interest / get_open_interest_history
    - liquidations.get_liquidation_notional_usd
    - spot.get_spot_price (optional — see basis check below)
    - `perp_price` / `price_change_pct` (caller-supplied, OBSERVED values
      the caller already has from its own OHLCV window — see each check's
      docstring for why this module takes them as plain floats rather than
      re-deriving them from a window it doesn't otherwise need)
  DERIVED (computed here from the OBSERVED inputs above):
    - annualized funding rate
    - basis percentage (perp vs spot)
    - OI percentage change over the acceleration window

FAIL-OPEN vs FAIL-CLOSED, check by check:
  - Funding extremity:   a crowded-positioning SENTIMENT signal, not a hard
    safety gate. Fails OPEN (does not block) when the funding rate is
    unavailable — absence of data here just means "we don't know the
    crowding direction," not "conditions are unsafe."
  - Basis (perp vs spot): same family as funding (a sentiment/crowding
    signal). Fails OPEN when no `spot` provider was supplied, or when it
    returns no usable price — basis is an ADDITIONAL confirmation on top of
    funding, not itself a required safety gate, so its absence should never
    silently block every entry.
  - OI acceleration:      explicitly NOT a hard veto by design — the signal
    (rising OI without rising price) is ambiguous on its own, so this check
    can only ever TIGHTEN size (`size_multiplier = OI_ACCEL_SIZE_MULTIPLIER`),
    never set `allowed = False`. It fails OPEN (multiplier stays 1.0) when
    OI history or the caller-supplied price-change figure is unavailable.
  - Liquidation cascade:  a hard safety gate -> fails CLOSED. A cascade is
    direct evidence of acute, abnormal market stress, so unlike the checks
    above, an unreadable liquidation feed is treated defensively too: see
    inline comment at that check for the exact behavior.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from data_feeds import FundingRateProvider, LiquidationFeedProvider, OpenInterestProvider, SpotPriceProvider


@dataclass(frozen=True, slots=True)
class DerivativesPositioningConfig:
    """Thresholds for the derivatives positioning overlay. Defaults are
    PROPOSED institutional-standard starting points, explicitly not yet
    validated against live MEXC derivatives data — review before this gate
    ever touches real capital.
    """

    # --- Funding extremity ---
    FUNDING_INTERVAL_HOURS: float = 8.0  # matches mexc_bot/config.py FUNDING_INTERVAL_HOURS
    FUNDING_ANNUALIZED_BLOCK_THRESHOLD: float = 1.0  # |annualized rate| > 100%/yr

    # --- Basis (perp vs spot) ---
    BASIS_BLOCK_THRESHOLD_PCT: float = 0.02  # |basis| > 2% blocks crowded-direction entries

    # --- OI acceleration (never a hard veto — size-tightening only) ---
    OI_ACCEL_WINDOW_HOURS: float = 4.0
    OI_RISING_MIN_PCT: float = 0.05        # >= 5% OI increase counted as "rising"
    PRICE_RISING_MIN_PCT: float = 0.0      # > 0% price change counted as "rising"
    OI_ACCEL_SIZE_MULTIPLIER: float = 0.5

    # --- Liquidation-cascade cooldown ---
    LIQUIDATION_LOOKBACK_MINUTES: float = 15.0
    LIQUIDATION_NOTIONAL_BLOCK_USD: float = 5_000_000.0
    LIQUIDATION_COOLDOWN_MINUTES: float = 30.0


@dataclass(frozen=True, slots=True)
class DerivativesPositioningVerdict:
    allowed: bool
    reasons: tuple[str, ...]
    evidence: dict
    size_multiplier: float = 1.0


def evaluate_derivatives_positioning(
    symbol: str,
    side: str,
    *,
    funding: FundingRateProvider,
    open_interest: OpenInterestProvider,
    liquidations: LiquidationFeedProvider,
    now: datetime,
    config: DerivativesPositioningConfig,
    spot: SpotPriceProvider | None = None,
    perp_price: float | None = None,
    price_change_pct: float | None = None,
    cooldown_state: dict[str, datetime] | None = None,
) -> DerivativesPositioningVerdict:
    """Evaluate funding extremity, basis, OI acceleration and the
    liquidation-cascade cooldown for a candidate `side` ("long"/"short")
    entry on `symbol`. All checks run unconditionally (no short-circuiting)
    — see entry_filters.py for why this project always evaluates every
    component.

    `perp_price` and `price_change_pct` are plain floats the CALLER already
    has from its own OHLCV window (e.g. entry_filters.py, which holds
    `window`) — this module intentionally takes them as scalars rather than
    a DataFrame so it has no OHLCV dependency of its own; `perp_price` is
    used only for the basis check and `price_change_pct` only for the OI
    acceleration check. Both default to None, in which case that one check
    fails OPEN (basis) or stays at multiplier 1.0 (OI acceleration) — see
    module docstring.

    `cooldown_state`: a dict the CALLER owns, mapping symbol -> the
    datetime the cooldown lifts, mutated in place by this function. Passing
    the SAME dict across repeated calls is what makes the cooldown persist;
    passing None (the default) means "no persisted cooldown state,"
    equivalent to always starting cold — deliberately explicit rather than
    hidden global mutable state, so it is trivially testable and so a
    caller running multiple independent symbols/backtests can keep separate
    dicts without any risk of cross-talk.
    """
    reasons: list[str] = []
    evidence: dict = {}
    allowed = True
    size_multiplier = 1.0

    # --- Check 1: funding extremity (soft crowding signal -> fails OPEN on missing data) ---
    rate = funding.get_current_funding_rate(symbol)
    if rate is None:
        reasons.append("funding rate unavailable — failing open on this check")
        evidence["funding_rate"] = None
        evidence["funding_annualized"] = None
    else:
        periods_per_year = (24.0 / config.FUNDING_INTERVAL_HOURS) * 365.0
        annualized = rate * periods_per_year
        evidence["funding_rate"] = rate
        evidence["funding_annualized"] = annualized
        crowded_long = side == "long" and annualized > config.FUNDING_ANNUALIZED_BLOCK_THRESHOLD
        crowded_short = side == "short" and annualized < -config.FUNDING_ANNUALIZED_BLOCK_THRESHOLD
        if crowded_long or crowded_short:
            allowed = False
            reasons.append(
                f"funding extremity: annualized {annualized:.1%} blocks new "
                f"{side} entries (crowded {'long' if crowded_long else 'short'} bias)"
            )

    # --- Check 2: basis (soft crowding signal -> fails OPEN with no spot feed) ---
    if spot is None:
        reasons.append("basis check skipped (fail-open): no SpotPriceProvider configured")
        evidence["basis_pct"] = None
    else:
        spot_price = spot.get_spot_price(symbol)
        if spot_price is None or spot_price <= 0 or perp_price is None:
            reasons.append(
                "basis check skipped (fail-open): spot price or perp_price unavailable"
            )
            evidence["basis_pct"] = None
        else:
            basis_pct = (perp_price - spot_price) / spot_price
            evidence["basis_pct"] = basis_pct
            evidence["spot_price"] = spot_price
            crowded_long = side == "long" and basis_pct > config.BASIS_BLOCK_THRESHOLD_PCT
            crowded_short = side == "short" and basis_pct < -config.BASIS_BLOCK_THRESHOLD_PCT
            if crowded_long or crowded_short:
                allowed = False
                reasons.append(
                    f"basis extremity: perp/spot basis {basis_pct:.2%} blocks new "
                    f"{side} entries (crowded {'long' if crowded_long else 'short'} bias)"
                )

    # --- Check 3: OI acceleration (NEVER a hard veto — size-tightening only) ---
    oi_history = open_interest.get_open_interest_history(symbol, config.OI_ACCEL_WINDOW_HOURS)
    if oi_history is None or len(oi_history) < 2 or price_change_pct is None:
        reasons.append(
            "OI-acceleration check skipped (fail-open, multiplier stays 1.0): "
            "insufficient OI history or no price_change_pct supplied"
        )
        evidence["oi_change_pct"] = None
    else:
        oi_start, oi_end = float(oi_history.iloc[0]), float(oi_history.iloc[-1])
        oi_change_pct = (oi_end - oi_start) / oi_start if oi_start else 0.0
        evidence["oi_change_pct"] = oi_change_pct
        oi_rising = oi_change_pct >= config.OI_RISING_MIN_PCT
        price_rising = price_change_pct > config.PRICE_RISING_MIN_PCT
        if oi_rising and not price_rising:
            # Ambiguous signal by design (per module docstring): rising OI
            # without confirming price action could mean building
            # conviction OR a crowded trade setting up to unwind. We do not
            # know which, so we tighten size rather than block outright.
            size_multiplier = config.OI_ACCEL_SIZE_MULTIPLIER
            reasons.append(
                f"OI acceleration caution: OI +{oi_change_pct:.1%} over "
                f"{config.OI_ACCEL_WINDOW_HOURS:.0f}h without confirming price move "
                f"({price_change_pct:+.1%}) — tightening size to "
                f"{size_multiplier:.2f}x, NOT a hard veto"
            )
        elif oi_rising and price_rising:
            reasons.append(
                f"OI acceleration: OI +{oi_change_pct:.1%} with confirming price move "
                f"({price_change_pct:+.1%}) — trend-confirming, no action"
            )

    # --- Check 4: liquidation-cascade cooldown (hard safety gate -> fails CLOSED) ---
    cooldown_state = {} if cooldown_state is None else cooldown_state
    cooldown_until = cooldown_state.get(symbol)
    if cooldown_until is not None and now < cooldown_until:
        allowed = False
        reasons.append(
            f"liquidation-cascade cooldown active for {symbol} until {cooldown_until.isoformat()}"
        )
        evidence["liquidation_cooldown_until"] = cooldown_until
        evidence["liquidation_notional_usd"] = None
    else:
        try:
            liq_notional = liquidations.get_liquidation_notional_usd(
                symbol, config.LIQUIDATION_LOOKBACK_MINUTES
            )
        except Exception as exc:  # noqa: BLE001 - a liquidation feed failure is itself a
            # stress signal for a hard safety gate: treat "can't read it" as
            # "assume the worst," fail CLOSED rather than silently passing.
            allowed = False
            liq_notional = None
            reasons.append(
                f"liquidation feed error — failing CLOSED defensively: {exc!r}"
            )
        if liq_notional is not None:
            evidence["liquidation_notional_usd"] = liq_notional
            if liq_notional > config.LIQUIDATION_NOTIONAL_BLOCK_USD:
                allowed = False
                new_cooldown_until = now + timedelta(minutes=config.LIQUIDATION_COOLDOWN_MINUTES)
                cooldown_state[symbol] = new_cooldown_until
                reasons.append(
                    f"liquidation cascade detected: ${liq_notional:,.0f} over "
                    f"{config.LIQUIDATION_LOOKBACK_MINUTES:.0f}m exceeds threshold "
                    f"${config.LIQUIDATION_NOTIONAL_BLOCK_USD:,.0f} — cooldown until "
                    f"{new_cooldown_until.isoformat()}"
                )
            elif symbol in cooldown_state:
                # Cooldown window has fully elapsed — clear it so a stale
                # entry doesn't linger in the caller-owned dict forever.
                del cooldown_state[symbol]

    if allowed and size_multiplier == 1.0 and not any(
        r.startswith("OI acceleration") for r in reasons
    ):
        reasons.append("all derivatives positioning checks passed")

    return DerivativesPositioningVerdict(
        allowed=allowed, reasons=tuple(reasons), evidence=evidence, size_multiplier=size_multiplier
    )
