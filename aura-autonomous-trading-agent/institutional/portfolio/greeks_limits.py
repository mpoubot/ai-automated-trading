#!/usr/bin/env python3
"""
AURA portfolio/greeks_limits.py — Portfolio Greeks Aggregation and Limits.

WHAT THIS MODULE IS, AND WHAT IT EXTENDS
------------------------------------------------------------------------
`.343`'s own O8 extension computes exactly one aggregate Greek,
`compute_net_portfolio_delta()` — net delta only, and it is an
all-or-nothing computation: ANY option leg with a missing/invalid delta
makes the WHOLE dimension `NOT_COMPUTABLE`. Gamma and vega aggregation do
not exist anywhere in AURA today. This module adds both, plus a
deliberately more forgiving availability model: `option_detail`'s
`gamma`/`vega` keys are optional per `.343`'s own documented contract
("may be `None`/absent"), so a leg missing one of them is an EXPECTED,
not an exceptional, situation — this module reports that mix explicitly
as `PARTIAL` rather than collapsing the whole aggregate to
`NOT_COMPUTABLE` the way `.343`'s delta-only aggregator does for a
missing delta (delta itself is still treated as required per leg here,
matching `.343`'s own convention for that one Greek specifically).

Reuses `.344`'s real `DimensionVerdict`/verdict constants and (via
`.344`'s own `OBS` reference) `.343`'s real `PositionRecord`/
`PortfolioSnapshot`/`DEFAULT_OPTION_MULTIPLIER` — same loader pattern as
every other module in this package (see `macro_buckets.py` for the
fuller rationale).

OBSERVED vs DERIVED vs EFFECTIVE
------------------------------------------------------------------------
  - OBSERVED: each `PositionRecord.option_detail` (`delta`/`gamma`/
    `vega`/`multiplier`, caller-supplied, never computed here — this
    module makes no pricing call, same discipline `.343` already states
    for `aura_v05375`), `.mark_price`, `.quantity`, `.direction`.
  - DERIVED: `aggregate_portfolio_greeks()`'s `net_delta_usd` /
    `gamma_impact_usd_per_1pct_move` / `vega_usd_per_vol_point`.
  - EFFECTIVE: `evaluate_greeks_dimension()`'s and
    `would_breach_with_new_position()`'s `DimensionVerdict.verdict`.

House convention followed exactly: every `GreeksLimitsConfig` field
defaults to `None`. `PROPOSED_GREEKS_LIMITS_CONFIG` is the separate,
explicit opt-in carrying this project's own proposed starter numbers —
never the dataclass's own default.

Judgment calls, disclosed
------------------------------------------------------------------------
  - Dollar delta: `delta * quantity * multiplier * mark_price`, exactly
    as specified. A leg missing a valid `delta` OR a valid `mark_price`
    is excluded from `net_delta_usd` and recorded in
    `legs_excluded_missing_data` — this mirrors `.343`'s own
    `compute_net_portfolio_delta()` exclusion discipline (name the
    offending leg, never silently drop it).
  - Dollar gamma impact for a 1% underlying move — FORMULA FIXED
    2026-10-09 per Martin's explicit directive (previously a disclosed
    judgment call open for review; now a specified, enforced formula):

        Dollar Gamma = 0.5 * Position Gamma * (0.01 * Spot Price)^2 * 100

    implemented per leg as `sign * 0.5 * gamma * (0.01 * mark_price) ** 2
    * quantity * multiplier` — the standard second-order "gamma P&L"
    convexity approximation for a 1% underlying move. `multiplier`
    continues to be read per-leg from `option_detail["multiplier"]`
    (defaulting to `DEFAULT_OPTION_MULTIPLIER`, which equals `100` for
    every contract type this repo currently trades — the literal `100`
    in Martin's formula), rather than hardcoding `100` and losing the
    ability to honor a genuinely different per-leg contract multiplier
    should one ever appear.
  - Dollar vega — FORMULA FIXED 2026-10-09 per Martin's explicit
    directive (previously `vega * quantity * multiplier`, with no
    `mark_price`/spot-price term at all; now specified and enforced):

        Dollar Vega = Position Vega * 0.01 * Spot Price * 100

    implemented per leg as `sign * vega * 0.01 * mark_price * quantity *
    multiplier`, same `multiplier` convention as gamma above. This is a
    DELIBERATE departure from the conventional "vega already expressed
    as a dollar-per-vol-point sensitivity" convention this module used
    before — Martin's formula instead normalizes vega against the
    underlying's own price, consistent with how this module's gamma
    formula already treats a 1% price move. Because this formula now
    REQUIRES a usable `mark_price` (unlike the prior formula), a leg with
    a valid `vega` but no valid `mark_price` is now excluded from
    `vega_usd_per_vol_point` and reported in `vega_legs_missing_vega` —
    this is a behavior change from before, disclosed here rather than
    silently introduced.
  - "Spot Price" in both formulas above is sourced from
    `PositionRecord.mark_price` — the only live price field `.343`'s
    current schema carries for an option leg (there is no separate
    underlying-vs-option-premium price field in `option_detail` or on
    `PositionRecord` itself). Flagged explicitly: if `.343` is ever
    extended with a true underlying-spot field distinct from the
    option's own mark, that field should replace `mark_price` here.

MARK-PRICE FALLBACK AND WIDE-SPREAD HANDLING (2026-10-09, Task 2, per
Martin's explicit directive)
------------------------------------------------------------------------
Because every formula above now REQUIRES `mark_price` (delta always
did; gamma/vega now do too, since 2026-10-09), a stale feed, empty
print, or temporarily missing mark price silently excludes that leg
from EVERY Greek, not just one -- in practice this means a single bad
print can blind the portfolio-level Greeks gate on exactly the leg it
most needs to see. This module now supports three OPTIONAL, caller-
supplied collaborators (all default `None` -- omitting them reproduces
this module's prior behavior byte-for-byte, same "leaving it `None`
changes nothing" convention as every other optional parameter in this
package):

  1. `mark_price_cache` (`MarkPriceCache`, a caller-owned, mutable
     object -- same "caller owns the state dict" convention
     `track_a/derivatives_positioning_overlay.py`'s liquidation-cooldown
     state already uses, never a hidden module-level global). Every leg
     with a genuinely OBSERVED, nonzero `mark_price` is recorded into it
     (keyed by `f"{venue}:{symbol}"`) on every call -- this is what
     makes a "prior cycle" value exist for a LATER call to fall back to.
     A cached value older than `max_mark_price_cache_age_seconds`
     (default 900s = 15 minutes, per Martin's exact spec) is treated as
     unavailable, not stale-but-usable.
  2. `underlying_price_provider` (`UnderlyingPriceProvider` Protocol,
     one method: `get_underlying_price(symbol, *, now) -> float | None`)
     -- supplies "the last available OBSERVED underlying asset price"
     Martin's spec calls for. **Disclosed correction to a literal
     reading of the instruction:** Martin's spec says to derive the
     proxy as "the underlying asset price multiplied by the contract
     size." The existing Dollar-Gamma/Dollar-Vega formulas already
     multiply by `multiplier` as their OWN separate term (the literal
     `* 100`). Substituting an already-multiplier-scaled figure into
     those formulas' Spot-Price slot would silently double the contract
     multiplier into the result -- a 100x inflation of Dollar Gamma/Vega
     for every leg that falls back through this path, which is exactly
     the kind of "silently converting a bad/wrong print into an
     EFFECTIVE risk value" Martin's own point 2 says never to do. This
     module therefore feeds only the raw per-unit `underlying_price`
     into the Spot-Price slot, and reports the full `underlying_price *
     multiplier` figure separately, evidence-only, as
     `underlying_notional_proxy_usd` -- satisfying the audit-visibility
     intent of Martin's instruction without the double-count bug.
  3. `quote_provider` (`QuoteProvider` Protocol, one method:
     `get_option_quote(position, *, now) -> {"bid": float, "ask":
     float} | None`) -- an OPTIONAL per-leg bid/ask lookup (the same
     shape `options_o10/chain_data_feeds.py`'s `OptionsChainProvider`
     already returns per contract, but intentionally NOT imported here:
     `portfolio/` and `options_o10/` are separate subpackages and this
     module does not reach across that boundary -- a caller that has
     both wires its own adapter). When supplied and a leg's quoted
     `(ask - bid) / mid` exceeds `WIDE_SPREAD_LIQUIDITY_FLOOR_PCT`
     (`0.10` -- the SAME literal threshold as
     `options_o10/strategy_config.py`'s own `max_bid_ask_spread_pct_of_
     mid` long-leg bound, reused for consistency, not re-derived), that
     leg is excluded from every Greek's sum and reported in the new
     `legs_excluded_wide_spread` list -- **regardless of whether a usable
     price was found for it by (1)/(2) above.** This is the module's
     answer to Martin's point 2: a wide-spread leg's Greeks are computed
     (so a caller can SEE what the excluded number would have been, same
     "always evaluate everything" discipline as `execution_constraints.
     py`) but never summed into the portfolio aggregate -- a bad/illiquid
     print never becomes an EFFECTIVE risk figure. Omitting
     `quote_provider` skips this check entirely (disclosed gap: no spread
     data source exists yet for most callers), it does not fail closed on
     its own.

Fallback order (deterministic, disclosed -- Martin's spec gives two
alternatives without an explicit priority, so this module picks one and
names it rather than leaving it ambiguous): (a) a genuinely OBSERVED,
nonzero `mark_price` always wins outright; failing that, (b) the
underlying-price proxy, when `underlying_price_provider` is supplied
and returns a usable value (the freshest signal available when a live
feed exists at all); failing that, (c) a cached mark price from the
prior cycle, no older than 15 minutes; failing all three, (d) the leg
is excluded and named in `legs_excluded_missing_data` (delta) /
`gamma_legs_missing_gamma` / `vega_legs_missing_vega`, exactly as
before this change. Every leg's resolved `price_basis` ("OBSERVED_MARK"
/ "UNDERLYING_PRICE_PROXY" / "CACHED_PRIOR_CYCLE_MARK" / "UNAVAILABLE")
is now carried in that leg's evidence, so a caller can always tell
which path produced the number feeding the aggregate -- never silently
indistinguishable from a clean, live print.
  - A leg's `gamma`/`vega` being absent is NOT treated the same as a
    missing `delta`: it does not exclude that leg from `legs_excluded_
    missing_data` (the field this module reuses verbatim from the
    spec), but IS tracked separately, per Greek, in
    `gamma_legs_missing_gamma` / `vega_legs_missing_vega` — so "what was
    silently dropped" is always fully visible, just not conflated across
    the three different Greeks' very different availability.
  - `status`: `"NOT_COMPUTABLE"` only if there are option positions but
    NONE of them have a usable delta (`legs_included == 0`); `"PARTIAL"`
    if some legs were excluded for delta OR some legs are missing gamma/
    vega while others have it; `"COMPUTABLE"` otherwise (including the
    trivial empty-book case, matching `.343`'s own "flat book is safe by
    construction" convention).
"""
from __future__ import annotations

import importlib.util
import math
import os
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

# ------------------------------------------------------------------------
# Loader (duplicated per-file by this package's own convention -- see
# macro_buckets.py's module docstring for why).
# ------------------------------------------------------------------------


_REQUIRED_CORE_FILES: tuple[str, ...] = ("aura_v05344_portfolio_exposure_enforcement.py",)


def _find_aura_core_dir() -> Path:
    """HARDENED 2026-10-09 per Martin's explicit directive -- validates
    that every required core file is actually present in the resolved
    directory, not just that the directory exists (see macro_buckets.py's
    fuller docstring for the rationale; identical logic, kept duplicated
    per this package's own convention)."""
    env = os.environ.get("AURA_CORE_DIR")
    if env:
        candidate = Path(env)
        if not candidate.exists():
            raise RuntimeError(f"AURA_CORE_DIR={env!r} does not exist")
        missing = [f for f in _REQUIRED_CORE_FILES if not (candidate / f).is_file()]
        if missing:
            raise RuntimeError(
                f"AURA_CORE_DIR={env!r} exists but is missing required core module "
                f"file(s): {', '.join(missing)}. Point AURA_CORE_DIR at the directory "
                f"that directly contains the real aura_v053NN modules."
            )
        return candidate
    fallback = Path("/mnt/user-data/uploads/AI automated trading/aura-autonomous-trading-agent")
    if fallback.exists() and all((fallback / f).is_file() for f in _REQUIRED_CORE_FILES):
        return fallback
    raise RuntimeError(
        "Could not locate the aura_v053NN core module directory. Set the "
        "AURA_CORE_DIR environment variable to the directory containing "
        "aura_v05344_portfolio_exposure_enforcement.py and its siblings."
    )


def _load_module_from_file(module_name: str, file_path: Path):
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_CORE_DIR = _find_aura_core_dir()
_ENF = _load_module_from_file(
    "aura_v05344_portfolio_exposure_enforcement",
    _CORE_DIR / "aura_v05344_portfolio_exposure_enforcement.py",
)

OBS = _ENF.OBS  # .343, loaded transitively by .344 itself -- same instance.
PositionRecord = OBS.PositionRecord
PortfolioSnapshot = OBS.PortfolioSnapshot
DEFAULT_OPTION_MULTIPLIER = OBS.DEFAULT_OPTION_MULTIPLIER
DimensionVerdict = _ENF.DimensionVerdict
PASS = _ENF.PASS
BLOCK = _ENF.BLOCK
LIMIT_NOT_CONFIGURED = _ENF.LIMIT_NOT_CONFIGURED
NOT_COMPUTABLE = _ENF.NOT_COMPUTABLE


# ------------------------------------------------------------------------
# Config.
# ------------------------------------------------------------------------

@dataclass(frozen=True)
class GreeksLimitsConfig:
    """Every field defaults to `None`, per house convention -- see module
    docstring. `PROPOSED_GREEKS_LIMITS_CONFIG` below is the separate,
    explicit opt-in."""
    max_net_delta_ratio: float | None = None
    max_gamma_impact_ratio: float | None = None
    max_vega_loss_ratio: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_net_delta_ratio": self.max_net_delta_ratio,
            "max_gamma_impact_ratio": self.max_gamma_impact_ratio,
            "max_vega_loss_ratio": self.max_vega_loss_ratio,
        }


PROPOSED_GREEKS_LIMITS_CONFIG = GreeksLimitsConfig(
    max_net_delta_ratio=0.15,
    max_gamma_impact_ratio=0.05,
    max_vega_loss_ratio=0.005,
)


# ------------------------------------------------------------------------
# Mark-price fallback / wide-spread handling collaborators (Task 2,
# 2026-10-09). See module docstring section above for the full
# rationale, the disclosed multiplier-double-count correction, and the
# deterministic fallback order. All three are optional; omitting every
# one of them reproduces this module's pre-2026-10-09 behavior exactly.
# ------------------------------------------------------------------------

MAX_MARK_PRICE_CACHE_AGE_SECONDS: float = 900.0  # 15 minutes, per Martin's exact spec
WIDE_SPREAD_LIQUIDITY_FLOOR_PCT: float = 0.10  # same literal value as options_o10/strategy_config.py's max_bid_ask_spread_pct_of_mid (long-leg bound)


@dataclass
class MarkPriceCache:
    """Caller-owned, mutable cache of the last-OBSERVED (never a
    fallback/proxy value) mark price per leg, keyed by `f"{venue}:
    {symbol}"`. Pass the SAME instance across evaluation cycles for the
    'cached historical mark price from the prior cycle' fallback to
    have anything to read -- a fresh instance every call is equivalent
    to never supplying one at all. Never persisted to disk by this
    module; a caller that wants cross-process durability owns that
    separately."""

    _store: dict[str, tuple[float, datetime]] = field(default_factory=dict)

    def record(self, leg_id: str, mark_price: float, *, now: datetime) -> None:
        self._store[leg_id] = (mark_price, now)

    def get_if_fresh(self, leg_id: str, *, now: datetime, max_age_seconds: float) -> float | None:
        entry = self._store.get(leg_id)
        if entry is None:
            return None
        price, observed_at = entry
        age_seconds = (now - observed_at).total_seconds()
        if age_seconds < 0 or age_seconds > max_age_seconds:
            return None
        return price

    def age_seconds(self, leg_id: str, *, now: datetime) -> float | None:
        entry = self._store.get(leg_id)
        if entry is None:
            return None
        return (now - entry[1]).total_seconds()


@runtime_checkable
class UnderlyingPriceProvider(Protocol):
    """Supplies 'the last available OBSERVED underlying asset price' for
    a leg's own underlying symbol (`PositionRecord.symbol` -- see module
    docstring disclosure: `.343`'s schema does not distinguish an
    option leg's own symbol field from a true underlying ticker; if a
    future schema version does, this is the seam to re-point). Returns
    `None` (never an invented number) when no observed price is
    available for that symbol."""

    def get_underlying_price(self, underlying_symbol: str, *, now: datetime) -> float | None:
        ...


@runtime_checkable
class QuoteProvider(Protocol):
    """Supplies a per-leg bid/ask quote for the wide-spread check.
    Returns `None` (never an invented quote) when no quote is available
    for that leg. Deliberately NOT `options_o10.chain_data_feeds.
    OptionsChainProvider` itself -- `portfolio/` does not import across
    the `options_o10/` package boundary; a caller that has both wires
    its own thin adapter closure."""

    def get_option_quote(self, position: Any, *, now: datetime) -> dict[str, Any] | None:
        ...


def _resolve_spot_price_for_leg(
    p: Any,
    *,
    now: datetime,
    mark_price_cache: MarkPriceCache | None,
    underlying_price_provider: UnderlyingPriceProvider | None,
    max_mark_price_cache_age_seconds: float,
) -> tuple[float | None, str, dict[str, Any]]:
    """Returns `(spot_price, price_basis, evidence_extra)`. `price_basis`
    is one of `"OBSERVED_MARK"` / `"UNDERLYING_PRICE_PROXY"` /
    `"CACHED_PRIOR_CYCLE_MARK"` / `"UNAVAILABLE"` -- see module
    docstring for the deterministic fallback order and the disclosed
    multiplier-double-count correction applied to path (2)."""
    leg_id = _leg_id(p)

    if _valid_num(p.mark_price) and float(p.mark_price) != 0.0:
        price = float(p.mark_price)
        if mark_price_cache is not None:
            mark_price_cache.record(leg_id, price, now=now)
        return price, "OBSERVED_MARK", {}

    if underlying_price_provider is not None:
        try:
            underlying_price = underlying_price_provider.get_underlying_price(p.symbol, now=now)
        except Exception:
            underlying_price = None
        if _valid_num(underlying_price) and float(underlying_price) > 0:
            underlying_price = float(underlying_price)
            detail = p.option_detail if isinstance(p.option_detail, dict) else {}
            multiplier = detail.get("multiplier", DEFAULT_OPTION_MULTIPLIER)
            if not _valid_num(multiplier):
                multiplier = DEFAULT_OPTION_MULTIPLIER
            notional_proxy_usd = underlying_price * float(multiplier)
            if mark_price_cache is not None:
                mark_price_cache.record(leg_id, underlying_price, now=now)
            return (
                underlying_price,
                "UNDERLYING_PRICE_PROXY",
                {
                    "underlying_notional_proxy_usd": notional_proxy_usd,
                    "note": (
                        "underlying_price (not underlying_price * multiplier) is what feeds "
                        "the Spot-Price formula slot -- the formula applies * multiplier as "
                        "its own separate term; see module docstring Task-2 disclosure."
                    ),
                },
            )

    if mark_price_cache is not None:
        cached = mark_price_cache.get_if_fresh(leg_id, now=now, max_age_seconds=max_mark_price_cache_age_seconds)
        if cached is not None:
            return (
                cached,
                "CACHED_PRIOR_CYCLE_MARK",
                {"cache_age_seconds": mark_price_cache.age_seconds(leg_id, now=now)},
            )

    return None, "UNAVAILABLE", {}


def _leg_blocked_by_wide_spread(
    p: Any,
    *,
    now: datetime,
    quote_provider: QuoteProvider | None,
    liquidity_floor_pct: float,
) -> tuple[bool, dict[str, Any]]:
    """Returns `(blocked, evidence)`. `quote_provider is None` is a
    disclosed gap (no spread data source wired yet) -- it is treated as
    'this check does not apply here', never as a block. A quote that IS
    available and fails the floor is unconditionally a block -- never
    overridable by the caller of `aggregate_portfolio_greeks`, matching
    `execution_constraints.py`'s own `orders_enabled` intercept
    discipline (not a tunable threshold judgment call at the call site,
    a categorical liquidity-quality gate)."""
    if quote_provider is None:
        return False, {}
    try:
        quote = quote_provider.get_option_quote(p, now=now)
    except Exception:
        quote = None
    if not quote:
        return False, {}
    bid, ask = quote.get("bid"), quote.get("ask")
    if not _valid_num(bid) or not _valid_num(ask):
        return False, {}
    bid, ask = float(bid), float(ask)
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return True, {"reason": "NON_POSITIVE_MID", "bid": bid, "ask": ask}
    spread_pct = (ask - bid) / mid
    if spread_pct > liquidity_floor_pct:
        return True, {
            "reason": "SPREAD_EXCEEDS_LIQUIDITY_FLOOR",
            "spread_pct_of_mid": spread_pct,
            "liquidity_floor_pct": liquidity_floor_pct,
            "bid": bid,
            "ask": ask,
        }
    return False, {}


# ------------------------------------------------------------------------
# Aggregation.
# ------------------------------------------------------------------------

def _valid_num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _leg_id(p: Any) -> str:
    return f"{p.venue}:{p.symbol}"


def aggregate_portfolio_greeks(
    snapshot: PortfolioSnapshot,
    *,
    now: datetime | None = None,
    mark_price_cache: MarkPriceCache | None = None,
    underlying_price_provider: UnderlyingPriceProvider | None = None,
    quote_provider: QuoteProvider | None = None,
    max_mark_price_cache_age_seconds: float = MAX_MARK_PRICE_CACHE_AGE_SECONDS,
    wide_spread_liquidity_floor_pct: float = WIDE_SPREAD_LIQUIDITY_FLOOR_PCT,
) -> dict[str, Any]:
    """DERIVED. See module docstring for the exact formulas, the
    mark-price fallback mechanism, and the wide-spread handling policy
    (Task 2, 2026-10-09). A flat option book (zero legs with
    `option_detail` set) is `COMPUTABLE` with every Greek at `0.0`,
    matching `.343`'s own "empty book is safe by construction"
    convention for `compute_net_portfolio_delta()`.

    All five keyword-only parameters default to values that reproduce
    this function's pre-2026-10-09 behavior exactly when `mark_price_
    cache`/`underlying_price_provider`/`quote_provider` are all left
    `None` (the mark-price fallback simply never has anywhere to fall
    back to, and the wide-spread check is skipped) -- no existing
    caller's behavior changes by upgrading to this signature."""
    now = now or datetime.now(timezone.utc)
    option_legs = [p for p in snapshot.positions if isinstance(p.option_detail, dict)]
    if not option_legs:
        return {
            "net_delta_usd": 0.0,
            "gamma_impact_usd_per_1pct_move": 0.0,
            "vega_usd_per_vol_point": 0.0,
            "legs_included": 0,
            "legs_excluded_missing_data": [],
            "gamma_legs_missing_gamma": [],
            "vega_legs_missing_vega": [],
            "legs_excluded_wide_spread": [],
            "leg_price_basis": {},
            "leg_price_evidence": {},
            "status": "COMPUTABLE",
        }

    delta_included_usd: list[float] = []
    delta_excluded: list[str] = []
    gamma_included_usd: list[float] = []
    gamma_missing: list[str] = []
    vega_included_usd: list[float] = []
    vega_missing: list[str] = []
    wide_spread_excluded: list[str] = []
    leg_price_basis: dict[str, str] = {}
    leg_price_evidence: dict[str, dict[str, Any]] = {}

    for p in option_legs:
        leg_id = _leg_id(p)
        detail = p.option_detail
        sign = 1.0 if p.direction == "LONG" else -1.0
        multiplier = detail.get("multiplier", DEFAULT_OPTION_MULTIPLIER)
        if not _valid_num(multiplier):
            multiplier = DEFAULT_OPTION_MULTIPLIER
        multiplier = float(multiplier)

        spot_price, price_basis, price_evidence = _resolve_spot_price_for_leg(
            p,
            now=now,
            mark_price_cache=mark_price_cache,
            underlying_price_provider=underlying_price_provider,
            max_mark_price_cache_age_seconds=max_mark_price_cache_age_seconds,
        )
        leg_price_basis[leg_id] = price_basis
        has_price = spot_price is not None

        is_wide_spread, spread_evidence = _leg_blocked_by_wide_spread(
            p, now=now, quote_provider=quote_provider, liquidity_floor_pct=wide_spread_liquidity_floor_pct,
        )
        if price_evidence or spread_evidence:
            leg_price_evidence[leg_id] = {**price_evidence, **({"spread": spread_evidence} if spread_evidence else {})}
        if is_wide_spread:
            wide_spread_excluded.append(leg_id)

        delta = detail.get("delta")
        if not _valid_num(delta) or not has_price or is_wide_spread:
            delta_excluded.append(leg_id)
        else:
            delta_included_usd.append(sign * float(delta) * p.quantity * multiplier * float(spot_price))

        gamma = detail.get("gamma")
        if _valid_num(gamma) and has_price and not is_wide_spread:
            move = 0.01 * float(spot_price)
            gamma_included_usd.append(sign * 0.5 * float(gamma) * (move ** 2) * p.quantity * multiplier)
        else:
            gamma_missing.append(leg_id)

        vega = detail.get("vega")
        if _valid_num(vega) and has_price and not is_wide_spread:
            # Dollar Vega = Position Vega * 0.01 * Spot Price * 100 (Martin's
            # explicit directive, 2026-10-09) -- "Spot Price" = spot_price
            # (OBSERVED mark_price, or a disclosed fallback -- see module
            # docstring), "* 100" = this leg's own multiplier.
            vega_included_usd.append(sign * float(vega) * 0.01 * float(spot_price) * p.quantity * multiplier)
        else:
            vega_missing.append(leg_id)

    legs_included = len(delta_included_usd)
    net_delta_usd = sum(delta_included_usd) if delta_included_usd else None
    gamma_impact = sum(gamma_included_usd) if gamma_included_usd else None
    vega_usd = sum(vega_included_usd) if vega_included_usd else None

    if legs_included == 0:
        status = "NOT_COMPUTABLE"
    elif delta_excluded or gamma_missing or vega_missing or wide_spread_excluded:
        status = "PARTIAL"
    else:
        status = "COMPUTABLE"

    return {
        "net_delta_usd": net_delta_usd,
        "gamma_impact_usd_per_1pct_move": gamma_impact,
        "vega_usd_per_vol_point": vega_usd,
        "legs_included": legs_included,
        "legs_excluded_missing_data": sorted(delta_excluded),
        "gamma_legs_missing_gamma": sorted(gamma_missing),
        "vega_legs_missing_vega": sorted(vega_missing),
        "leg_price_evidence": leg_price_evidence,
        "legs_excluded_wide_spread": sorted(wide_spread_excluded),
        "leg_price_basis": leg_price_basis,
        "status": status,
    }


# ------------------------------------------------------------------------
# Limits evaluation.
# ------------------------------------------------------------------------

_DIMENSION_SPECS: tuple[tuple[str, str, str], ...] = (
    # (verdict dimension name, GreeksLimitsConfig field name, aggregate key)
    ("greeks_net_delta", "max_net_delta_ratio", "net_delta_usd"),
    ("greeks_gamma_impact", "max_gamma_impact_ratio", "gamma_impact_usd_per_1pct_move"),
    ("greeks_vega_loss", "max_vega_loss_ratio", "vega_usd_per_vol_point"),
)


def _evaluate_greeks_from_aggregate(
    agg: dict[str, Any], config: GreeksLimitsConfig, *, account_equity_usd: float,
) -> tuple[DimensionVerdict, ...]:
    out: list[DimensionVerdict] = []
    for dimension, field_name, agg_key in _DIMENSION_SPECS:
        limit = getattr(config, field_name)
        if agg["status"] == "NOT_COMPUTABLE":
            out.append(DimensionVerdict(
                dimension=dimension, venue=None, verdict=NOT_COMPUTABLE, reason="EXPOSURE_NOT_COMPUTABLE",
                evidence={"aggregate": agg},
            ))
            continue
        value = agg[agg_key]
        if value is None:
            out.append(DimensionVerdict(
                dimension=dimension, venue=None, verdict=NOT_COMPUTABLE, reason="GREEK_NOT_AVAILABLE",
                evidence={"aggregate": agg},
            ))
            continue
        if limit is None:
            out.append(DimensionVerdict(
                dimension=dimension, venue=None, verdict=LIMIT_NOT_CONFIGURED, reason="NO_LIMIT_CONFIGURED",
                evidence={"value_usd": value, "account_equity_usd": account_equity_usd},
            ))
            continue
        if account_equity_usd is None or account_equity_usd <= 0:
            out.append(DimensionVerdict(
                dimension=dimension, venue=None, verdict=NOT_COMPUTABLE, reason="ACCOUNT_EQUITY_NOT_AVAILABLE",
                evidence={"value_usd": value},
            ))
            continue
        ratio = abs(value) / account_equity_usd
        breached = ratio > limit
        out.append(DimensionVerdict(
            dimension=dimension, venue=None, verdict=BLOCK if breached else PASS,
            reason="LIMIT_BREACHED" if breached else "WITHIN_LIMIT",
            evidence={"value_usd": value, "ratio": ratio, "limit": limit},
        ))
    return tuple(out)


def evaluate_greeks_dimension(
    snapshot: PortfolioSnapshot,
    config: GreeksLimitsConfig,
    *,
    account_equity_usd: float,
    now: datetime | None = None,
    mark_price_cache: MarkPriceCache | None = None,
    underlying_price_provider: UnderlyingPriceProvider | None = None,
    quote_provider: QuoteProvider | None = None,
    max_mark_price_cache_age_seconds: float = MAX_MARK_PRICE_CACHE_AGE_SECONDS,
    wide_spread_liquidity_floor_pct: float = WIDE_SPREAD_LIQUIDITY_FLOOR_PCT,
) -> tuple[DimensionVerdict, ...]:
    """EFFECTIVE. One `DimensionVerdict` each for net delta, gamma
    impact, and vega loss, evaluated against the CURRENT portfolio
    state. `LIMIT_NOT_CONFIGURED` when the corresponding config field is
    `None`; `NOT_COMPUTABLE` passthrough when the aggregate itself
    couldn't be computed for that Greek (either the whole aggregate is
    `NOT_COMPUTABLE`, or that specific Greek's sum is `None` because no
    leg could supply it, or `account_equity_usd` is unusable); else
    `BLOCK`/`PASS` against `abs(value) / account_equity_usd` vs the
    configured limit.

    `now` is used to resolve cache freshness for the mark-price fallback
    (Task 2, 2026-10-09) when `mark_price_cache` is supplied -- defaults
    to wall-clock `now` otherwise. The five new keyword-only parameters
    all default to values that reproduce this function's pre-2026-10-09
    behavior exactly; see `aggregate_portfolio_greeks()`'s docstring."""
    agg = aggregate_portfolio_greeks(
        snapshot,
        now=now,
        mark_price_cache=mark_price_cache,
        underlying_price_provider=underlying_price_provider,
        quote_provider=quote_provider,
        max_mark_price_cache_age_seconds=max_mark_price_cache_age_seconds,
        wide_spread_liquidity_floor_pct=wide_spread_liquidity_floor_pct,
    )
    return _evaluate_greeks_from_aggregate(agg, config, account_equity_usd=account_equity_usd)


def would_breach_with_new_position(
    snapshot: PortfolioSnapshot,
    proposed_position: PositionRecord,
    config: GreeksLimitsConfig,
    *,
    account_equity_usd: float,
    now: datetime | None = None,
    mark_price_cache: MarkPriceCache | None = None,
    underlying_price_provider: UnderlyingPriceProvider | None = None,
    quote_provider: QuoteProvider | None = None,
    max_mark_price_cache_age_seconds: float = MAX_MARK_PRICE_CACHE_AGE_SECONDS,
    wide_spread_liquidity_floor_pct: float = WIDE_SPREAD_LIQUIDITY_FLOOR_PCT,
) -> tuple[DimensionVerdict, ...]:
    """EFFECTIVE, PRE-TRADE. Same as `evaluate_greeks_dimension()`, but
    computed on `snapshot.positions + (proposed_position,)` -- i.e. would
    ADDING this one hypothetical position breach a configured Greeks
    limit? This is the function meant to actually gate new options order
    flow (wrapped into a `(symbol, direction, quantity, decision)`
    check_fn in `portfolio_additional_enforcement.py`). The hypothetical
    snapshot built here via `dataclasses.replace()` is for Greeks
    computation ONLY -- its `as_of`/`state_hash` are carried over
    unchanged from the real snapshot and are not meaningful for a
    position set that was never actually fetched; this function never
    returns or persists that hypothetical snapshot itself.

    The five new keyword-only parameters (Task 2, 2026-10-09) pass
    straight through to `evaluate_greeks_dimension()`/
    `aggregate_portfolio_greeks()` -- see their docstrings. All default
    to values that reproduce this function's pre-2026-10-09 behavior
    exactly."""
    hypothetical_positions = tuple(snapshot.positions) + (proposed_position,)
    hypothetical_snapshot = replace(snapshot, positions=hypothetical_positions)
    return evaluate_greeks_dimension(
        hypothetical_snapshot,
        config,
        account_equity_usd=account_equity_usd,
        now=now,
        mark_price_cache=mark_price_cache,
        underlying_price_provider=underlying_price_provider,
        quote_provider=quote_provider,
        max_mark_price_cache_age_seconds=max_mark_price_cache_age_seconds,
        wide_spread_liquidity_floor_pct=wide_spread_liquidity_floor_pct,
    )
