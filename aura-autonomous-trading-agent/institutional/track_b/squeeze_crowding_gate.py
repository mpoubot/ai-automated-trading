#!/usr/bin/env python3
"""
AURA Track B -- Short-squeeze / crowded-short veto.

WHAT THIS MODULE IS, AND IS DELIBERATELY NOT
------------------------------------------------------------------------
This gate answers "is this symbol structurally dangerous to short right
now because too many other shorts are already crowded into it (or it is
already squeezing today)". Nothing else in the audited codebase checks
short-interest-as-percent-of-float, days-to-cover, or a same-day price/
volume squeeze signature -- this is genuinely new, non-overlapping
coverage, not a restatement of `.335`'s borrow-availability check or
this package's own `borrow_fee_gate.py` (which prices the cost of
borrowing, not the crowding/squeeze risk of the trade itself).

Two independent sub-checks, OR'd together
------------------------------------------------------------------------
  (a) LAGGED tier -- `short_interest_pct_float` / `days_to_cover` from a
      `ShortInterestDataProvider` (bi-monthly FINRA-cadence in the real
      world; see `borrow_data_feeds.py`'s module docstring on the
      CLAIMED-adjacent/lagged tier of this data). Vetoes only when BOTH
      values are present AND BOTH exceed their thresholds -- a crowded
      short with a short days-to-cover is a materially different risk
      than a crowded short that could unwind over weeks, so neither
      datum alone is treated as sufficient. If EITHER datum is `None`,
      this specific sub-check does not fire a veto on its own (the data
      is explicitly optional/lagged, so its absence is not itself
      disqualifying) -- but the absence is always recorded in `reasons`,
      never silently passed over, per this project's "never silently
      skip a check without saying so" discipline (`.368`'s own
      `combine_enforcement_check_fns` docstring states the same
      principle for composed checks; this module applies it within a
      single check).
  (b) SAME-DAY tier -- an intraday price/volume squeeze signature,
      computed from `bars_provider(symbol)` when supplied. Hard vetoes
      regardless of what the lagged tier says, because a live squeeze in
      progress is a same-day fact, not a lagged inference -- it should
      never be softened by stale short-interest data saying things
      looked calm two weeks ago. `bars_provider` is optional: if `None`,
      this sub-check is skipped and said so in `reasons` (same "note the
      skip" discipline as (a)), never silently treated as "no squeeze".

Overall result: the gate vetoes (blocks) if EITHER (a) or (b) fires.
Direction-scoping: `direction != "OPEN_SHORT"` returns `allowed=True`
immediately, mirroring `.368`'s own discipline -- this gate is new-short-
entry-only; it has no opinion on longs or on closing an existing short.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from borrow_data_feeds import ShortInterestDataProvider

VERSION = "AURA Track B v0.1.0"
ENGINE = "SQUEEZE_CROWDING_GATE"

# Proposed institutional defaults, NOT yet validated for AURA specifically
# (same disclosure convention as `.344`'s own threshold constants).
DEFAULT_SHORT_INTEREST_PCT_VETO = 0.20
DEFAULT_DAYS_TO_COVER_VETO = 5.0
DEFAULT_INTRADAY_PRICE_PCT_THRESHOLD = 0.10
DEFAULT_INTRADAY_VOLUME_MULTIPLE_THRESHOLD = 3.0
DEFAULT_VOLUME_LOOKBACK_DAYS = 20


@dataclass(frozen=True, slots=True)
class SqueezeCrowdingVerdict:
    allowed: bool
    reasons: tuple[str, ...]
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "reasons": list(self.reasons), "evidence": dict(self.evidence)}


def _evaluate_lagged_tier(
    symbol: str,
    short_interest_feed: ShortInterestDataProvider,
    *,
    short_interest_pct_veto: float,
    days_to_cover_veto: float,
) -> tuple[bool, list[str], dict[str, Any]]:
    """Returns (fired, reasons, evidence) for sub-check (a). `fired=True`
    means this sub-check wants to veto."""
    snapshot = short_interest_feed.get_short_interest_snapshot(symbol)
    evidence: dict[str, Any] = {
        "symbol": snapshot.symbol,
        "short_interest_pct_float": snapshot.short_interest_pct_float,
        "days_to_cover": snapshot.days_to_cover,
        "as_of_date": snapshot.as_of_date.isoformat() if snapshot.as_of_date else None,
        "source_label": snapshot.source_label,
    }

    if snapshot.short_interest_pct_float is None or snapshot.days_to_cover is None:
        missing = [
            name
            for name, value in (
                ("short_interest_pct_float", snapshot.short_interest_pct_float),
                ("days_to_cover", snapshot.days_to_cover),
            )
            if value is None
        ]
        return (
            False,
            [
                f"LAGGED_SHORT_INTEREST_DATA_MISSING:{symbol} missing {', '.join(missing)} from "
                f"{snapshot.source_label} -- lagged-tier crowding sub-check SKIPPED (not a block, data is "
                "explicitly optional/lagged), noted here rather than silently passed over",
            ],
            evidence,
        )

    if (
        snapshot.short_interest_pct_float > short_interest_pct_veto
        and snapshot.days_to_cover > days_to_cover_veto
    ):
        return (
            True,
            [
                f"CROWDED_SHORT:{symbol} short_interest_pct_float={snapshot.short_interest_pct_float:.4f} > "
                f"{short_interest_pct_veto:.4f} AND days_to_cover={snapshot.days_to_cover:.2f} > "
                f"{days_to_cover_veto:.2f} (as of {evidence['as_of_date']}, lagged FINRA-cadence data) -- "
                "crowded-short veto",
            ],
            evidence,
        )

    return (
        False,
        [
            f"LAGGED_SHORT_INTEREST_OK:{symbol} short_interest_pct_float={snapshot.short_interest_pct_float:.4f}, "
            f"days_to_cover={snapshot.days_to_cover:.2f} (as of {evidence['as_of_date']}) -- within thresholds",
        ],
        evidence,
    )


def _evaluate_same_day_tier(
    symbol: str,
    bars_provider: Callable[[str], Any] | None,
    *,
    intraday_price_pct_threshold: float,
    intraday_volume_multiple_threshold: float,
    volume_lookback_days: int,
) -> tuple[bool, list[str], dict[str, Any]]:
    """Returns (fired, reasons, evidence) for sub-check (b). Pure best-
    effort: any shape problem with the bars frame (too few rows, missing
    columns) is treated as "cannot evaluate this sub-check today", noted
    in reasons, and never raises -- consistent with `.352`'s own
    `_validate_bars_frame`/early-return discipline for feature frames
    that are too short or malformed to score."""
    if bars_provider is None:
        return (
            False,
            [
                "SAME_DAY_SQUEEZE_CHECK_SKIPPED:no bars_provider supplied -- same-day price/volume squeeze "
                "sub-check SKIPPED (not a block), noted here rather than silently treated as 'no squeeze'",
            ],
            {},
        )

    try:
        bars = bars_provider(symbol)
    except Exception as exc:  # pragma: no cover -- defensive, mirrors fail-safe discipline elsewhere in repo
        return (
            False,
            [
                f"SAME_DAY_SQUEEZE_CHECK_ERROR:bars_provider raised {type(exc).__name__} for {symbol} -- "
                "same-day sub-check SKIPPED (not a block), noted here rather than silently treated as 'no squeeze'",
            ],
            {},
        )

    required_cols = {"close", "volume"}
    has_cols = hasattr(bars, "__len__") and hasattr(bars, "columns") and required_cols.issubset(set(bars.columns))
    if not has_cols or len(bars) < volume_lookback_days + 1:
        return (
            False,
            [
                f"SAME_DAY_SQUEEZE_CHECK_INSUFFICIENT_DATA:{symbol} bars frame missing required columns or has "
                f"< {volume_lookback_days + 1} rows -- same-day sub-check SKIPPED (not a block), noted here "
                "rather than silently treated as 'no squeeze'",
            ],
            {},
        )

    latest = bars.iloc[-1]
    prior_close = bars.iloc[-2]["close"]
    evidence: dict[str, Any] = {}
    if prior_close in (0, None) or (hasattr(prior_close, "__eq__") and prior_close == 0):
        return (
            False,
            [
                f"SAME_DAY_SQUEEZE_CHECK_INVALID_PRIOR_CLOSE:{symbol} prior close is zero/None -- same-day "
                "sub-check SKIPPED (not a block), noted here rather than silently treated as 'no squeeze'",
            ],
            {},
        )

    latest_return = (float(latest["close"]) - float(prior_close)) / float(prior_close)
    lookback_avg_volume = float(bars["volume"].iloc[-(volume_lookback_days + 1):-1].mean())
    latest_volume = float(latest["volume"])
    volume_multiple = (latest_volume / lookback_avg_volume) if lookback_avg_volume > 0 else float("inf")

    evidence = {
        "latest_return": latest_return,
        "latest_volume": latest_volume,
        "lookback_avg_volume": lookback_avg_volume,
        "volume_multiple": volume_multiple,
    }

    if latest_return > intraday_price_pct_threshold and volume_multiple > intraday_volume_multiple_threshold:
        return (
            True,
            [
                f"SAME_DAY_SQUEEZE_SIGNATURE:{symbol} latest-session return {latest_return:.4f} > "
                f"{intraday_price_pct_threshold:.4f} AND volume {volume_multiple:.2f}x the "
                f"{volume_lookback_days}-day average > {intraday_volume_multiple_threshold:.2f}x -- "
                "live squeeze signature veto, hard block regardless of lagged data",
            ],
            evidence,
        )

    return (
        False,
        [
            f"SAME_DAY_SQUEEZE_CHECK_OK:{symbol} latest-session return {latest_return:.4f}, volume "
            f"{volume_multiple:.2f}x average -- within thresholds",
        ],
        evidence,
    )


def evaluate_squeeze_crowding_for_open_short(
    symbol: str,
    short_interest_feed: ShortInterestDataProvider,
    bars_provider: Callable[[str], Any] | None = None,
    *,
    short_interest_pct_veto: float = DEFAULT_SHORT_INTEREST_PCT_VETO,
    days_to_cover_veto: float = DEFAULT_DAYS_TO_COVER_VETO,
    intraday_price_pct_threshold: float = DEFAULT_INTRADAY_PRICE_PCT_THRESHOLD,
    intraday_volume_multiple_threshold: float = DEFAULT_INTRADAY_VOLUME_MULTIPLE_THRESHOLD,
    volume_lookback_days: int = DEFAULT_VOLUME_LOOKBACK_DAYS,
) -> SqueezeCrowdingVerdict:
    lagged_fired, lagged_reasons, lagged_evidence = _evaluate_lagged_tier(
        symbol, short_interest_feed,
        short_interest_pct_veto=short_interest_pct_veto,
        days_to_cover_veto=days_to_cover_veto,
    )
    same_day_fired, same_day_reasons, same_day_evidence = _evaluate_same_day_tier(
        symbol, bars_provider,
        intraday_price_pct_threshold=intraday_price_pct_threshold,
        intraday_volume_multiple_threshold=intraday_volume_multiple_threshold,
        volume_lookback_days=volume_lookback_days,
    )

    allowed = not (lagged_fired or same_day_fired)
    reasons = tuple(lagged_reasons + same_day_reasons)
    evidence = {"lagged_tier": lagged_evidence, "same_day_tier": same_day_evidence}
    return SqueezeCrowdingVerdict(allowed=allowed, reasons=reasons, evidence=evidence)


def build_squeeze_crowding_check_fn(
    short_interest_feed: ShortInterestDataProvider,
    bars_provider: Callable[[str], Any] | None = None,
    *,
    short_interest_pct_veto: float = DEFAULT_SHORT_INTEREST_PCT_VETO,
    days_to_cover_veto: float = DEFAULT_DAYS_TO_COVER_VETO,
    intraday_price_pct_threshold: float = DEFAULT_INTRADAY_PRICE_PCT_THRESHOLD,
    intraday_volume_multiple_threshold: float = DEFAULT_INTRADAY_VOLUME_MULTIPLE_THRESHOLD,
    volume_lookback_days: int = DEFAULT_VOLUME_LOOKBACK_DAYS,
) -> Callable[[str, str, Any, Any], SqueezeCrowdingVerdict]:
    """Returns a closure matching `.368`'s `enforcement_check_fn` contract
    exactly: `(symbol, direction, quantity, decision) -> SqueezeCrowdingVerdict`.
    `quantity`/`decision` are unused -- this gate's entire decision is a
    function of `symbol` (and `direction`, for scoping)."""

    def check(symbol: str, direction: str, quantity: Any, decision: Any) -> SqueezeCrowdingVerdict:
        if direction != "OPEN_SHORT":
            return SqueezeCrowdingVerdict(allowed=True, reasons=(), evidence={})
        return evaluate_squeeze_crowding_for_open_short(
            symbol, short_interest_feed, bars_provider,
            short_interest_pct_veto=short_interest_pct_veto,
            days_to_cover_veto=days_to_cover_veto,
            intraday_price_pct_threshold=intraday_price_pct_threshold,
            intraday_volume_multiple_threshold=intraday_volume_multiple_threshold,
            volume_lookback_days=volume_lookback_days,
        )

    return check
