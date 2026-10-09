#!/usr/bin/env python3
"""
AURA Track B -- Borrow-fee / hard-to-borrow (HTB) pre-trade gate, plus a
separate in-trade recall/fee-spike monitor for already-open shorts.

WHAT THIS MODULE IS, AND IS DELIBERATELY NOT
------------------------------------------------------------------------
This is NOT a duplicate of `.335.alpaca_asset_to_shortability_status` /
`.335.validate_equity_spec`'s existing hard block on `NOT_SHORTABLE`
inventory. That check answers "can this broker locate shares to borrow
at all". This gate answers a genuinely different question that nothing
else in the audited codebase answers: "even if shares ARE available,
what does borrowing them actually COST, and is that cost acceptable for
a new short entry". A stock can be `SHORTABLE`/`BORROW_CONFIRMED` by
`.335`'s own logic and still carry a borrow fee that eats the entire
expected edge of the trade (the classic HTB "fee trap"): this gate fills
that gap, it does not re-decide availability.

This module's check function is wired as one more `enforcement_check_fn`-
shaped callable, composable via `.368`'s own `combine_enforcement_check_fns`
(not reimplemented here) alongside `.368`'s earnings-blackout gate and
`.344`'s portfolio-exposure check, at the exact same choke point `.53`
already calls through.

Pre-trade gate vs. in-trade monitor -- two different shapes, by design
------------------------------------------------------------------------
`build_borrow_fee_check_fn` produces a pre-trade `(symbol, direction,
quantity, decision) -> BorrowFeeVerdict` closure, matching `.368`'s
contract exactly, for gating NEW entries. `evaluate_open_short_for_recall`
is a deliberately SEPARATE, differently-shaped function -- it is not a
`check_fn` at all, because the thing it protects (an already-open short
position that must poll for recall/fee-spike risk) is not a candidate
flowing through `.53.run_cycle`'s enforcement choke point. Forcing both
concerns into one function signature would blur "should we open this"
with "should we now force-close that", which this project's existing
gates (e.g. `.368`'s own day-of-only earnings blackout, which explicitly
only ever fires on new entries because of how `.53` calls it) keep
structurally separate. A caller (a stage-3 live trader's position-
monitoring loop, not built here) is expected to poll
`evaluate_open_short_for_recall` once per open short position per cycle.

Fail-open vs. fail-closed, stated per branch
------------------------------------------------------------------------
  - `easy_to_borrow is False` (actively confirmed hard-to-borrow by the
    feed): FAIL CLOSED, hard veto. Not ambiguous -- the feed is telling
    us borrowing is constrained right now.
  - `borrow_fee_rate_annualized is None` (feed has no answer): FAIL
    CLOSED, hard veto. Per the task's explicit instruction and this
    project's own "never assume a missing safety-relevant number is
    fine" discipline (mirrors `.34`'s `BORROW_UNKNOWN` fail-closed
    treatment) -- a short with an unknown borrow cost is never opened
    here, full stop. This is deliberately stricter than, say, `.368`'s
    earnings-blackout gate, which fails closed on calendar-wide outages
    but still allows symbols confirmed absent from a working calendar;
    there is no equivalent "confirmed absent = fine" case for a missing
    fee rate, because every real equity DOES have *some* borrow cost --
    an unknown one is never equivalent to a known-low one.
  - `borrow_fee_rate_annualized >= hard_veto_annualized_rate`: FAIL
    CLOSED, hard veto. The fee itself is judged too expensive to trade.
  - `caution_zone_min_rate <= rate < hard_veto_annualized_rate`: ALLOWS
    the trade, but returns a `suggested_hold_time_multiplier < 1.0` and
    says so in `reasons`. This is advisory output only -- this gate's
    own job is a binary allow/block on the (symbol, direction, quantity,
    decision) tuple; it has no sizing or holding-period authority of its
    own (same scope discipline as `.368`: "no new data fetching, no
    scoring, no sizing" beyond its one decision). A caller that wants to
    actually shrink a planned holding period must read this field and
    act on it; this gate does not enforce it.
  - `rate < caution_zone_min_rate`: ALLOWS, no note, `multiplier=1.0`.
  - `direction != "OPEN_SHORT"`: ALLOWS immediately (CLOSE_SHORT included
    -- closing a short is never blocked by this pre-trade gate; that is
    `evaluate_open_short_for_recall`'s job instead, which a caller drives
    directly against an open position, not through this check_fn). The
    feed IS still consulted for non-OPEN_SHORT directions below, purely
    so `evidence` is populated symmetrically for audit/observability
    even though the verdict is a pass-through -- a deliberate choice,
    not a cost-saving skip, see `build_borrow_fee_check_fn` docstring.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from borrow_data_feeds import BorrowDataProvider, BorrowSnapshot

VERSION = "AURA Track B v0.1.0"
ENGINE = "BORROW_FEE_GATE"

# Proposed institutional defaults, NOT yet validated for AURA specifically
# (same disclosure convention as `.344`'s own threshold constants) --
# these are reasonable starting points drawn from common HTB-desk
# practice, not numbers derived from AURA's own backtests or Martin's
# own risk tolerance. Revisit once real borrow-fee data is wired in.
DEFAULT_HARD_VETO_ANNUALIZED_RATE = 0.20
DEFAULT_CAUTION_ZONE_MIN_RATE = 0.05
DEFAULT_SPIKE_MULTIPLE_FORCES_EXIT = 2.0


@dataclass(frozen=True, slots=True)
class BorrowFeeVerdict:
    allowed: bool
    reasons: tuple[str, ...]
    evidence: dict[str, Any] = field(default_factory=dict)
    # Advisory only -- see module docstring "caution zone" branch above.
    # This gate never enforces this value itself; a caller may use it to
    # tighten a planned holding period. 1.0 means "no tightening advised".
    suggested_hold_time_multiplier: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reasons": list(self.reasons),
            "evidence": dict(self.evidence),
            "suggested_hold_time_multiplier": self.suggested_hold_time_multiplier,
        }


def _snapshot_evidence(snapshot: BorrowSnapshot) -> dict[str, Any]:
    return {
        "symbol": snapshot.symbol,
        "easy_to_borrow": snapshot.easy_to_borrow,
        "borrow_fee_rate_annualized": snapshot.borrow_fee_rate_annualized,
        "fetched_at": snapshot.fetched_at.isoformat(),
        "source_label": snapshot.source_label,
    }


def _hold_time_multiplier_for_caution_zone(
    rate: float, *, caution_zone_min_rate: float, hard_veto_annualized_rate: float,
) -> float:
    """Linear shrink from 1.0 (at `caution_zone_min_rate`) down to 0.25
    (just below `hard_veto_annualized_rate`) -- a stepped, disclosed,
    NOT-yet-validated-for-AURA shape. Never reaches 0.0 from this
    function alone (that would be indistinguishable from a hard veto);
    the floor of 0.25 is itself a proposed institutional default."""
    span = hard_veto_annualized_rate - caution_zone_min_rate
    if span <= 0:
        return 1.0
    position = (rate - caution_zone_min_rate) / span  # 0.0 .. <1.0 within the caution zone
    position = min(max(position, 0.0), 1.0)
    return round(1.0 - (0.75 * position), 4)


def evaluate_borrow_fee_for_open_short(
    symbol: str,
    borrow_feed: BorrowDataProvider,
    *,
    hard_veto_annualized_rate: float = DEFAULT_HARD_VETO_ANNUALIZED_RATE,
    caution_zone_min_rate: float = DEFAULT_CAUTION_ZONE_MIN_RATE,
) -> BorrowFeeVerdict:
    """Pure-ish (one feed call) evaluation of whether `symbol` is safe to
    open a NEW short on, purely from a borrow-cost/availability angle.
    Does not consult `direction` -- that scoping lives in
    `build_borrow_fee_check_fn`, one layer up, so this function can also
    be reused directly in tests or by a future sizing tool without having
    to fabricate a `decision` object."""
    snapshot = borrow_feed.get_borrow_snapshot(symbol)
    evidence = _snapshot_evidence(snapshot)

    if snapshot.easy_to_borrow is False:
        return BorrowFeeVerdict(
            allowed=False,
            reasons=(
                f"BORROW_NOT_EASY:{symbol} is flagged not-easy-to-borrow by "
                f"{snapshot.source_label} -- fail-closed, no new short entry",
            ),
            evidence=evidence,
        )

    rate = snapshot.borrow_fee_rate_annualized
    if rate is None:
        return BorrowFeeVerdict(
            allowed=False,
            reasons=(
                f"BORROW_FEE_UNAVAILABLE:{symbol} has no usable annualized borrow-fee rate from "
                f"{snapshot.source_label} -- fail-closed, a short is never opened on an unknown borrow cost",
            ),
            evidence=evidence,
        )

    if rate >= hard_veto_annualized_rate:
        return BorrowFeeVerdict(
            allowed=False,
            reasons=(
                f"BORROW_FEE_TOO_HIGH:{symbol} annualized borrow fee {rate:.4f} >= hard-veto threshold "
                f"{hard_veto_annualized_rate:.4f} -- fee-trap risk, no new short entry",
            ),
            evidence=evidence,
        )

    if rate >= caution_zone_min_rate:
        multiplier = _hold_time_multiplier_for_caution_zone(
            rate, caution_zone_min_rate=caution_zone_min_rate, hard_veto_annualized_rate=hard_veto_annualized_rate,
        )
        return BorrowFeeVerdict(
            allowed=True,
            reasons=(
                f"BORROW_FEE_CAUTION_ZONE:{symbol} annualized borrow fee {rate:.4f} is within the caution "
                f"zone [{caution_zone_min_rate:.4f}, {hard_veto_annualized_rate:.4f}) -- allowed, but advisory "
                f"suggested_hold_time_multiplier={multiplier} (not enforced by this gate; caller's choice to act on it)",
            ),
            evidence=evidence,
            suggested_hold_time_multiplier=multiplier,
        )

    return BorrowFeeVerdict(allowed=True, reasons=(), evidence=evidence)


def build_borrow_fee_check_fn(
    borrow_feed: BorrowDataProvider,
    *,
    hard_veto_annualized_rate: float = DEFAULT_HARD_VETO_ANNUALIZED_RATE,
    caution_zone_min_rate: float = DEFAULT_CAUTION_ZONE_MIN_RATE,
    spike_multiple_forces_exit: float = DEFAULT_SPIKE_MULTIPLE_FORCES_EXIT,  # kept for signature symmetry w/ recall monitor; unused here
) -> Callable[[str, str, Any, Any], BorrowFeeVerdict]:
    """Returns a closure matching `.368`'s `enforcement_check_fn` contract
    exactly: `(symbol, direction, quantity, decision) -> BorrowFeeVerdict`.

    Non-`OPEN_SHORT` directions (`OPEN_LONG`, `CLOSE_LONG`, `CLOSE_SHORT`)
    always return `allowed=True` -- this gate scopes itself to new short
    entries only, mirroring `.368`'s own direction-scoping discipline.
    The feed is STILL called for these directions (not skipped) so the
    returned verdict's `evidence` is populated for audit/observability
    even on a pass-through; this is a deliberate design choice (symmetry
    and uniform logging) over short-circuiting, and costs one extra feed
    read per non-short candidate, which is acceptable since the fixture/
    real feeds here are expected to be cheap, cached per-cycle reads, not
    novel network calls per candidate.

    `spike_multiple_forces_exit` is accepted here purely so a caller can
    construct both this check_fn and the recall monitor from one shared
    config without juggling two different parameter sets; it has no
    effect on this closure's own pre-trade verdict.
    """

    def check(symbol: str, direction: str, quantity: Any, decision: Any) -> BorrowFeeVerdict:
        verdict = evaluate_borrow_fee_for_open_short(
            symbol, borrow_feed,
            hard_veto_annualized_rate=hard_veto_annualized_rate,
            caution_zone_min_rate=caution_zone_min_rate,
        )
        if direction != "OPEN_SHORT":
            # Pass-through for every other direction, but keep the real
            # evidence gathered above for audit -- never silently drop it.
            return BorrowFeeVerdict(allowed=True, reasons=(), evidence=verdict.evidence)
        return verdict

    return check


# ============================================================================
# In-trade monitor -- NOT a check_fn. Polled directly by a caller against
# an already-open short position. See module docstring for why this is
# intentionally a separate function shape, not folded into the gate above.
# ============================================================================


def evaluate_open_short_for_recall(
    position_symbol: str,
    entry_borrow_fee_rate: float,
    *,
    borrow_feed: BorrowDataProvider,
    spike_multiple_forces_exit: float = DEFAULT_SPIKE_MULTIPLE_FORCES_EXIT,
) -> BorrowFeeVerdict:
    """For an already-open short position, decide whether current borrow
    conditions now require a forced exit. `allowed=False` here means
    "force exit this position" (not "block a new entry" -- the semantic
    meaning of `.allowed` is inverted relative to the pre-trade gate
    above by necessity of this being a different kind of decision; this
    is documented explicitly here and at every call site this function
    expects, rather than left implicit).

    Fail-closed branches (force exit):
      - `easy_to_borrow` has flipped to `False` since entry (an explicit
        recall signal from the feed) -- forced exit, no ambiguity.
      - `borrow_fee_rate_annualized` is now `None` (feed lost the ability
        to answer) -- forced exit. An open short with an unknown current
        borrow cost is treated exactly as fail-closed as a new one would
        be; silence from the feed is never treated as "unchanged".
      - current rate >= `spike_multiple_forces_exit` x `entry_borrow_fee_rate`
        -- the fee has spiked enough that the original trade's economics
        are no longer the ones that justified entry; forced exit.
    Otherwise `allowed=True` ("still fine to hold").
    """
    snapshot = borrow_feed.get_borrow_snapshot(position_symbol)
    evidence = _snapshot_evidence(snapshot)
    evidence["entry_borrow_fee_rate"] = entry_borrow_fee_rate
    evidence["spike_multiple_forces_exit"] = spike_multiple_forces_exit

    if snapshot.easy_to_borrow is False:
        return BorrowFeeVerdict(
            allowed=False,
            reasons=(
                f"BORROW_RECALLED:{position_symbol} flipped to not-easy-to-borrow since entry "
                f"({snapshot.source_label}) -- forced exit, fail-closed",
            ),
            evidence=evidence,
        )

    current_rate = snapshot.borrow_fee_rate_annualized
    if current_rate is None:
        return BorrowFeeVerdict(
            allowed=False,
            reasons=(
                f"BORROW_FEE_NOW_UNAVAILABLE:{position_symbol} has no current borrow-fee reading from "
                f"{snapshot.source_label} -- forced exit, an open short is never held against an unknown "
                "current borrow cost",
            ),
            evidence=evidence,
        )

    if entry_borrow_fee_rate <= 0:
        # Degenerate entry rate (e.g. a bad historical record) -- avoid a
        # divide-by-zero-shaped comparison; treat any positive current
        # rate against a non-positive entry rate as an automatic spike,
        # since the ratio itself is undefined/meaningless otherwise.
        if current_rate > 0:
            return BorrowFeeVerdict(
                allowed=False,
                reasons=(
                    f"BORROW_FEE_SPIKE:{position_symbol} entry_borrow_fee_rate={entry_borrow_fee_rate} is "
                    f"non-positive while current rate={current_rate} -- undefined spike ratio, forced exit "
                    "fail-closed rather than silently dividing",
                ),
                evidence=evidence,
            )
        return BorrowFeeVerdict(allowed=True, reasons=(), evidence=evidence)

    spike_ratio = current_rate / entry_borrow_fee_rate
    evidence["spike_ratio"] = spike_ratio
    if spike_ratio >= spike_multiple_forces_exit:
        return BorrowFeeVerdict(
            allowed=False,
            reasons=(
                f"BORROW_FEE_SPIKE:{position_symbol} current rate {current_rate:.4f} is {spike_ratio:.2f}x "
                f"entry rate {entry_borrow_fee_rate:.4f}, >= spike_multiple_forces_exit="
                f"{spike_multiple_forces_exit:.2f} -- forced exit",
            ),
            evidence=evidence,
        )

    return BorrowFeeVerdict(allowed=True, reasons=(), evidence=evidence)
