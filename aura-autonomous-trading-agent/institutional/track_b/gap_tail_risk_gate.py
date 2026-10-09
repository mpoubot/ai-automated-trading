#!/usr/bin/env python3
"""
AURA Track B -- Overnight-gap / tail-risk handling for short positions.

WHAT THIS MODULE IS, AND IS DELIBERATELY NOT
------------------------------------------------------------------------
A short position has structurally unbounded loss potential and is
specifically exposed to overnight gap risk (a stop order cannot execute
while the market is closed). Nothing in the audited codebase computes a
gap-aware stop distance or vetoes a short on tail-risk sizing grounds --
this module fills exactly that gap, nothing else.

Two genuinely different functions, not one, because they answer two
different questions
------------------------------------------------------------------------
  (a) `compute_gap_aware_stop_distance` -- a SIZING helper. "Given this
      symbol's own gap history and its ATR, how far away should a stop
      be placed so an ordinary overnight gap doesn't trigger it
      spuriously." This is advisory sizing input for a caller's own
      stop-placement logic; it is not a check_fn and makes no
      allow/block decision. Insufficient history -> graceful fallback to
      a pure ATR-based distance (documented below) rather than raising,
      because this function's job is to produce SOME usable distance,
      and falling back to the plain-ATR distance is the conservative,
      already-used-elsewhere-in-this-repo default, not a silently wrong
      answer.
  (b) `evaluate_gap_tail_risk` / `build_gap_tail_risk_check_fn` -- a
      pre-trade VETO. "Given a worst-plausible overnight gap, would this
      specific `quantity` at this specific `entry_price_estimate` lose
      more than `max_single_position_loss_pct_of_equity` of
      `account_equity_usd`." This is a binary allow/block decision, and
      because it is a decision about risk the caller might not otherwise
      see, insufficient gap history here is treated OPPOSITE to (a):
      FAIL CLOSED (veto), not a graceful fallback -- an unknown tail-risk
      profile is never treated as "fine to size against", mirroring this
      project's established fail-closed discipline for unknown safety-
      relevant data (`.34`'s `BORROW_UNKNOWN`, this package's own
      `borrow_fee_gate.py` missing-rate branch). Documented explicitly
      here so the asymmetry between (a) and (b) is never mistaken for
      an inconsistency.

The adapter pattern -- why `build_gap_tail_risk_check_fn` looks slightly
different from this package's other two factories
------------------------------------------------------------------------
`.368`'s `enforcement_check_fn` contract is exactly `(symbol, direction,
quantity, decision) -> verdict` -- it carries no account-equity or
entry-price fields, and `TradingDecision` (`.350`) does not carry them
either (sizing/pricing is explicitly out of `.350`'s scope per its own
docstring). This gate's entire decision is a function of equity and
price, so those two values must come from SOMEWHERE outside the 4-arg
contract. Rather than silently reaching into a global or a hidden
singleton, this module takes the explicit, honest path: `evaluate_
gap_tail_risk` is the real, richly-parameterized function (symbol,
direction, quantity, entry_price_estimate, account_equity_usd, plus
kwargs) that a caller can call directly with values it already has: and
`build_gap_tail_risk_check_fn` is a THIN ADAPTER that closes over two
caller-supplied zero/one-arg lookup callables (`account_equity_lookup`,
`entry_price_lookup`) so the result can still be dropped into `.368`'s
`combine_enforcement_check_fns` alongside the other two gates in this
package when a caller has those lookups available (e.g. a live equity
curve and a live quote feed). Each call to the adapter re-invokes both
lookups fresh -- it never caches a stale equity/price snapshot across
candidates, matching this project's "never trust a stale safety number"
discipline.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

VERSION = "AURA Track B v0.1.0"
ENGINE = "GAP_TAIL_RISK_GATE"

# Proposed institutional defaults, NOT yet validated for AURA specifically
# (same disclosure convention as `.344`'s own threshold constants).
DEFAULT_ATR_MULTIPLE = 2.0
DEFAULT_OVERNIGHT_GAP_PERCENTILE_STOP = 0.95
DEFAULT_WORST_PLAUSIBLE_GAP_MULTIPLE = 3.0
DEFAULT_OVERNIGHT_GAP_PERCENTILE_TAIL = 0.99
DEFAULT_MAX_SINGLE_POSITION_LOSS_PCT_OF_EQUITY = 0.015

_MIN_BARS_FOR_GAP_HISTORY = 10  # below this, any percentile estimate is treated as statistically unusable


@dataclass(frozen=True, slots=True)
class GapTailRiskVerdict:
    allowed: bool
    reasons: tuple[str, ...]
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "reasons": list(self.reasons), "evidence": dict(self.evidence)}


def _has_required_bar_columns(bars: Any) -> bool:
    return hasattr(bars, "columns") and {"close", "open"}.issubset(set(bars.columns))


def _overnight_gap_pct_series(bars: Any) -> Any | None:
    """Close-to-next-open overnight percentage moves:
    gap_pct[i] = (open[i+1] - close[i]) / close[i].
    Returns `None` if `bars` doesn't have the shape to compute this."""
    if not _has_required_bar_columns(bars) or len(bars) < 2:
        return None
    next_open = bars["open"].shift(-1)
    close = bars["close"]
    gap_pct = (next_open - close) / close
    return gap_pct.iloc[:-1]  # last row's "next open" is NaN (shifted off the end)


def compute_gap_aware_stop_distance(
    bars: Any,
    *,
    atr: float,
    atr_multiple: float = DEFAULT_ATR_MULTIPLE,
    overnight_gap_percentile: float = DEFAULT_OVERNIGHT_GAP_PERCENTILE_STOP,
) -> float:
    """Returns `max(atr_multiple * atr, historical_gap_price_distance)`,
    where `historical_gap_price_distance` is the `overnight_gap_percentile`
    -th percentile of the ABSOLUTE close-to-next-open percentage move in
    `bars`, converted to a price distance using `bars`'s most recent
    close. SIZING helper, not a veto -- see module docstring.

    Graceful fallback: if `bars` lacks the required columns, or has fewer
    than `_MIN_BARS_FOR_GAP_HISTORY` usable overnight-gap observations,
    returns `atr_multiple * atr` alone (the plain-ATR distance) rather
    than raising or fabricating a percentile from too little data. This
    is a documented, intentional fallback -- a stop distance based on
    ATR alone is already a sane, conservative, widely-used default; it
    is simply not gap-aware when history is too thin to say anything
    about this symbol's own overnight-gap behavior."""
    base_distance = atr_multiple * atr

    gap_pct = _overnight_gap_pct_series(bars)
    if gap_pct is None or len(gap_pct.dropna()) < _MIN_BARS_FOR_GAP_HISTORY:
        return base_distance

    abs_gap_pct = gap_pct.abs().dropna()
    percentile_pct = float(abs_gap_pct.quantile(overnight_gap_percentile))
    latest_close = float(bars["close"].iloc[-1])
    gap_price_distance = percentile_pct * latest_close

    return max(base_distance, gap_price_distance)


def evaluate_gap_tail_risk(
    symbol: str,
    direction: str,
    quantity: float,
    entry_price_estimate: float,
    account_equity_usd: float,
    *,
    bars_provider: Callable[[str], Any],
    worst_plausible_gap_multiple: float = DEFAULT_WORST_PLAUSIBLE_GAP_MULTIPLE,
    overnight_gap_percentile: float = DEFAULT_OVERNIGHT_GAP_PERCENTILE_TAIL,
    max_single_position_loss_pct_of_equity: float = DEFAULT_MAX_SINGLE_POSITION_LOSS_PCT_OF_EQUITY,
) -> GapTailRiskVerdict:
    """The real, richly-parameterized pre-trade tail-risk decision. See
    module docstring for why this takes `entry_price_estimate` and
    `account_equity_usd` directly rather than only the 4-arg check_fn
    shape.

    `direction != "OPEN_SHORT"` -> allow immediately (this gate is new-
    short-entry-only, same scoping discipline as the rest of this
    package).

    Otherwise: estimate the worst plausible overnight gap as
    `worst_plausible_gap_multiple` times the historical `overnight_gap_
    percentile`-th percentile ABSOLUTE overnight move for `symbol`
    (sourced via `bars_provider`), apply it adversely (price UP, against
    a short) to `entry_price_estimate`, multiply by `quantity` to get a
    hypothetical dollar loss, and veto if that loss exceeds
    `max_single_position_loss_pct_of_equity` of `account_equity_usd`.

    FAIL CLOSED if `bars_provider` cannot supply usable gap history (too
    few rows, wrong columns, or raises) -- an unknown tail-risk profile
    is never treated as "fine to size against". See module docstring,
    part (b), for why this is the opposite fallback behavior from
    `compute_gap_aware_stop_distance`."""
    if direction != "OPEN_SHORT":
        return GapTailRiskVerdict(allowed=True, reasons=(), evidence={"symbol": symbol, "direction": direction})

    try:
        bars = bars_provider(symbol)
    except Exception as exc:  # pragma: no cover -- defensive
        return GapTailRiskVerdict(
            allowed=False,
            reasons=(
                f"GAP_HISTORY_UNAVAILABLE:bars_provider raised {type(exc).__name__} for {symbol} -- fail-closed, "
                "an unknown overnight-gap tail-risk profile is never treated as safe to size a short against",
            ),
            evidence={"symbol": symbol},
        )

    gap_pct = _overnight_gap_pct_series(bars)
    usable = None if gap_pct is None else gap_pct.abs().dropna()
    if usable is None or len(usable) < _MIN_BARS_FOR_GAP_HISTORY:
        return GapTailRiskVerdict(
            allowed=False,
            reasons=(
                f"GAP_HISTORY_INSUFFICIENT:{symbol} has fewer than {_MIN_BARS_FOR_GAP_HISTORY} usable overnight-gap "
                "observations -- fail-closed, an unknown overnight-gap tail-risk profile is never treated as safe "
                "to size a short against",
            ),
            evidence={"symbol": symbol},
        )

    tail_gap_pct = float(usable.quantile(overnight_gap_percentile))
    worst_plausible_gap_pct = worst_plausible_gap_multiple * tail_gap_pct
    hypothetical_loss_per_share = entry_price_estimate * worst_plausible_gap_pct
    hypothetical_loss_total = hypothetical_loss_per_share * quantity
    loss_limit_usd = max_single_position_loss_pct_of_equity * account_equity_usd

    evidence = {
        "symbol": symbol,
        "tail_gap_pct": tail_gap_pct,
        "worst_plausible_gap_pct": worst_plausible_gap_pct,
        "entry_price_estimate": entry_price_estimate,
        "quantity": quantity,
        "hypothetical_loss_total_usd": hypothetical_loss_total,
        "account_equity_usd": account_equity_usd,
        "loss_limit_usd": loss_limit_usd,
    }

    if hypothetical_loss_total > loss_limit_usd:
        return GapTailRiskVerdict(
            allowed=False,
            reasons=(
                f"GAP_TAIL_RISK_TOO_LARGE:{symbol} hypothetical overnight-gap loss ${hypothetical_loss_total:,.2f} "
                f"(worst-plausible gap {worst_plausible_gap_pct:.4f} = {worst_plausible_gap_multiple:.2f}x the "
                f"{overnight_gap_percentile:.2f}-percentile historical gap {tail_gap_pct:.4f}, on {quantity} shares "
                f"@ ${entry_price_estimate:,.2f}) exceeds the ${loss_limit_usd:,.2f} limit "
                f"({max_single_position_loss_pct_of_equity:.4f} of ${account_equity_usd:,.2f} equity) -- veto",
            ),
            evidence=evidence,
        )

    return GapTailRiskVerdict(allowed=True, reasons=(), evidence=evidence)


def build_gap_tail_risk_check_fn(
    bars_provider: Callable[[str], Any],
    account_equity_lookup: Callable[[], float],
    entry_price_lookup: Callable[[str], float],
    *,
    worst_plausible_gap_multiple: float = DEFAULT_WORST_PLAUSIBLE_GAP_MULTIPLE,
    overnight_gap_percentile: float = DEFAULT_OVERNIGHT_GAP_PERCENTILE_TAIL,
    max_single_position_loss_pct_of_equity: float = DEFAULT_MAX_SINGLE_POSITION_LOSS_PCT_OF_EQUITY,
) -> Callable[[str, str, Any, Any], GapTailRiskVerdict]:
    """THIN ADAPTER from the real `evaluate_gap_tail_risk` function to
    `.368`'s exact `(symbol, direction, quantity, decision) -> verdict`
    contract, by closing over `account_equity_lookup` (zero-arg) and
    `entry_price_lookup` (one-arg, by symbol). See module docstring for
    why this adapter exists and why it re-calls both lookups on every
    invocation rather than caching."""

    def check(symbol: str, direction: str, quantity: Any, decision: Any) -> GapTailRiskVerdict:
        if direction != "OPEN_SHORT":
            return GapTailRiskVerdict(allowed=True, reasons=(), evidence={"symbol": symbol, "direction": direction})
        account_equity_usd = account_equity_lookup()
        entry_price_estimate = entry_price_lookup(symbol)
        return evaluate_gap_tail_risk(
            symbol, direction, quantity, entry_price_estimate, account_equity_usd,
            bars_provider=bars_provider,
            worst_plausible_gap_multiple=worst_plausible_gap_multiple,
            overnight_gap_percentile=overnight_gap_percentile,
            max_single_position_loss_pct_of_equity=max_single_position_loss_pct_of_equity,
        )

    return check
