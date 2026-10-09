#!/usr/bin/env python3
"""
AURA portfolio/macro_buckets.py — Cross-Track Macro Risk-Factor Bucket
Concentration.

WHAT THIS MODULE IS, AND WHAT IT REUSES
------------------------------------------------------------------------
This is a NEW portfolio-risk dimension that extends `.344`'s enforcement
chain. It is not a modification of `.344` — it imports `.344`'s real,
already-defined `DimensionVerdict` dataclass and verdict constants
(`PASS` / `BLOCK` / `LIMIT_NOT_CONFIGURED` / `NOT_COMPUTABLE`) directly
from the staged module file, and `.343`'s real `PositionRecord` /
`PortfolioSnapshot` (via `.344`'s own already-loaded `OBS` reference, so
every class identity here is byte-identical to the one `.344` itself
uses — no re-definition, no duck-typed lookalike).

Why this dimension exists
------------------------------------------------------------------------
`.344`'s own `asset_concentration` dimension caps a single symbol's share
of the book, and its `correlation_group_concentration` extension (2026-
09-24) caps a caller-named group of symbols. Neither one is built to see
a shared MACRO risk factor that cuts straight across asset class and
venue — e.g. "every Track A MEXC crypto perpetual AND every high-beta
Track B equity are all, at bottom, one leveraged bet on risk sentiment."
`.344` itself only ever sums within one already-computed exposure report
at a time; it has no cross-venue, cross-track "economic factor" concept.
This module adds exactly that, as its own dimension, without touching
`.344`.

Design, OBSERVED vs DERIVED vs EFFECTIVE
------------------------------------------------------------------------
  - OBSERVED: `snapshot.positions` (each position's `venue`, `symbol`,
    `direction`, `notional_usd`) and the caller-supplied
    `account_equity_usd` — both taken as given, never recomputed here.
  - DERIVED: `compute_bucket_exposure()`'s per-bucket signed/gross
    notional and `exposure_ratio` — a pure function of the OBSERVED
    inputs plus the caller-supplied `MacroBucketConfig`.
  - EFFECTIVE: `evaluate_macro_bucket_dimension()`'s per-bucket
    `DimensionVerdict.verdict` (`LIMIT_NOT_CONFIGURED` / `PASS` /
    `BLOCK`) — the only part of this module's output that actually
    participates in an ALLOW/BLOCK enforcement decision.

House convention followed exactly (per `.344`'s own documented rule:
"never invent a limit"): `MacroBucketConfig.max_bucket_exposure_ratio`
and `.max_same_direction_stacking_ratio` both default to `None`.
`PROPOSED_MACRO_BUCKET_CONFIG` below is a separate, explicitly-opt-in
constant carrying this project's own prior proposed starter numbers —
never the dataclass's own default. Martin must explicitly pass it (or
his own config) for this dimension to ever BLOCK anything; until then,
every bucket reports `LIMIT_NOT_CONFIGURED`.

Judgment calls, disclosed (per this project's "document every judgment
call" discipline)
------------------------------------------------------------------------
  - `bucket_membership` keys may be an exact symbol (the common case) or
    a simple glob pattern (matched via `fnmatch.fnmatchcase`, e.g.
    `"*_CALL_*"`) — "symbol_or_pattern" per the spec that commissioned
    this module. A symbol matching more than one pattern within the SAME
    bucket has its matching weights summed (documented, not silently
    picking one); a symbol matching patterns across DIFFERENT buckets
    contributes independently to each bucket, which is the entire point
    of this module (the same position IS simultaneously part of more
    than one macro risk factor).
  - A position with `notional_usd is None` (not yet priced) is excluded
    from every bucket sum rather than treated as zero exposure. This is
    informational-sizing dimension, not `.344`'s own fail-closed
    aggregate-dimension machinery, so an exclusion here does not itself
    produce a `BLOCK` or `NOT_COMPUTABLE` verdict — but the exclusion
    count is always visible in `evidence["positions_excluded_no_notional"]`
    so it is never silently dropped. This is a disclosed simplification,
    not a claim that it matches `.344`'s own stricter aggregate-dimension
    data-quality gating — flagged here for Martin's review, not papered
    over.
  - `exposure_ratio = gross_notional_usd / account_equity_usd` — UPDATED
    2026-10-09 per Martin's explicit directive. Previously this used
    `abs(net_notional_usd)`, which let an offsetting long/short pair in
    the same bucket mask the bucket's true total risk (two $50k opposite
    positions netted to ~$0 exposure even though the book has $100k of
    gross macro-factor risk on). Gross notional is now the basis
    precisely so counter-positions cannot mask total risk. `net_notional_usd`
    is still computed and reported for visibility, but no longer gates
    anything on its own.
  - `account_equity_usd <= 0` (or exposure impossible to express as a
    ratio) reports `NOT_COMPUTABLE` for that bucket rather than a
    division error or a silent `PASS` — same "don't claim safety you
    can't prove" principle `.344` applies everywhere.
  - SAME-DIRECTION STACKING — IMPLEMENTED 2026-10-09 per Martin's
    explicit directive (exact formula and threshold given, not inferred):

        Stacking Ratio = ( Sum of Max(0, Signed Notional Exposure) ) / Total Equity

    computed PER BUCKET, PER DIRECTION — `long_stacking_ratio` sums only
    the positive part of each position's signed, bucket-weighted notional
    (i.e. every LONG contributor, SHORTs contribute 0 to this sum);
    `short_stacking_ratio` sums the positive part of the NEGATED signed
    notional (i.e. every SHORT contributor). `SAME_DIRECTION_STACKING_
    HARD_THRESHOLD = 0.15` (15% of total account equity) is enforced
    UNCONDITIONALLY — deliberately a hardcoded module constant, NOT
    sourced from `MacroBucketConfig.max_same_direction_stacking_ratio`,
    so this safety check can never be silently disabled by a config
    object that leaves that field at its default `None`. Breaching it in
    a given direction, for a given bucket, is a hard deterministic
    `BLOCK` — both as a portfolio-level dimension (every configured
    bucket, both directions, every evaluation cycle — see
    `evaluate_macro_bucket_dimension()`) and as a genuine pre-trade veto
    for any candidate order whose symbol falls in that bucket (see
    `evaluate_stacking_pretrade_direction()`, wired into
    `portfolio_additional_enforcement.build_additional_portfolio_check_fn()`).
    `MacroBucketConfig.max_same_direction_stacking_ratio` remains on the
    dataclass for backward-compatible storage/display only — it is not
    read by this enforcement path; the hard 0.15 threshold is the one
    actually enforced, by design, per Martin's directive.
"""
from __future__ import annotations

import fnmatch
import importlib.util
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ------------------------------------------------------------------------
# Loader: reuse .344's (and, transitively, .343's) REAL classes from the
# staged source files -- never redefine them. Each new portfolio/ module
# that needs these carries this same small loader block, matching this
# repo's own established convention (every aura_v053NN module is
# independently file-loadable and copies its own small helpers rather
# than depending on a shared, not-yet-existing package) -- see e.g.
# `.344`'s own `_load_observability_module()`. Modules are cached in
# `sys.modules` under their real `aura_v053NN...` name, so every
# portfolio/ file that loads `.344` resolves to the exact same module
# object/class identity, regardless of load order.
# ------------------------------------------------------------------------


_REQUIRED_CORE_FILES: tuple[str, ...] = ("aura_v05344_portfolio_exposure_enforcement.py",)


def _find_aura_core_dir() -> Path:
    """Locates the directory containing the real `.343`/`.344`/`.361`/
    `.368` source files this module reuses. In production, this
    portfolio/ package is deployed separately from the flat
    `aura_v053NN...` module directory (unlike every existing aura_v053NN
    module, which assumes its dependencies are siblings in the same
    directory via `Path(__file__).resolve().parent`) -- so the location
    must be told to this module explicitly. Set the `AURA_CORE_DIR`
    environment variable to that directory in any real deployment.

    HARDENED 2026-10-09 per Martin's explicit directive ("ensure the
    AURA_CORE_DIR environment variable wiring is fully functional"):
    this now validates that every file this module actually needs
    (`_REQUIRED_CORE_FILES`) is PRESENT in the resolved directory, not
    just that the directory itself exists -- a directory that exists but
    is missing the real module now fails closed here, with a specific,
    actionable error naming the exact missing file, instead of deferring
    to a much less legible failure later inside `_load_module_from_file`
    (a bare `FileNotFoundError` / `spec is None` with no context). The
    hardcoded fallback below is this sandbox's staged copy, used only
    when the environment variable is not set, so this module still runs
    out of the box in this build/test environment -- it is validated the
    same way, so a broken sandbox fails exactly as loudly as a broken
    real deployment would."""
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
DimensionVerdict = _ENF.DimensionVerdict
EnforcementDecision = _ENF.EnforcementDecision
PASS = _ENF.PASS
BLOCK = _ENF.BLOCK
LIMIT_NOT_CONFIGURED = _ENF.LIMIT_NOT_CONFIGURED
NOT_COMPUTABLE = _ENF.NOT_COMPUTABLE
NOT_APPLICABLE = _ENF.NOT_APPLICABLE


# ------------------------------------------------------------------------
# Config.
# ------------------------------------------------------------------------

@dataclass(frozen=True)
class MacroBucketConfig:
    """`bucket_membership`: {bucket_name: {symbol_or_pattern: weight}} --
    the one REQUIRED field (there is no meaningful default bucket map;
    an empty dict is a valid, explicit "no buckets defined" choice, not
    a different default). `max_bucket_exposure_ratio` and
    `max_same_direction_stacking_ratio` both default to `None`, per
    house convention -- see module docstring. Never substitute a number
    of your own for either; `PROPOSED_MACRO_BUCKET_CONFIG` below is the
    separate, explicit opt-in."""
    bucket_membership: dict[str, dict[str, float]]
    max_bucket_exposure_ratio: float | None = None
    max_same_direction_stacking_ratio: float | None = None  # NOT YET enforced -- see module docstring.

    def to_dict(self) -> dict[str, Any]:
        return {
            "bucket_membership": {b: dict(w) for b, w in self.bucket_membership.items()},
            "max_bucket_exposure_ratio": self.max_bucket_exposure_ratio,
            "max_same_direction_stacking_ratio": self.max_same_direction_stacking_ratio,
        }


# ------------------------------------------------------------------------
# A reasonable starter mapping, proposed per this project's own prior
# design work -- Martin must explicitly choose to pass this in; it is
# NEVER the dataclass's own default (see module docstring).
# ------------------------------------------------------------------------

TRACK_A_CRYPTO_PAIRS: tuple[str, ...] = (
    "BTC/USDT:USDT", "ETH/USDT:USDT", "BNB/USDT:USDT", "XRP/USDT:USDT",
    "ADA/USDT:USDT", "DOGE/USDT:USDT", "AVAX/USDT:USDT", "DOT/USDT:USDT",
    "SUI/USDT:USDT", "TIA/USDT:USDT", "COTI/USDT:USDT",
)

# Hard, non-configurable, deterministic -- Martin's explicit directive,
# 2026-10-09. Deliberately NOT sourced from `MacroBucketConfig.
# max_same_direction_stacking_ratio` (which stays on the dataclass for
# backward-compatible storage/display only) so this safety check can
# never be silently disabled by a config that leaves that field at its
# default `None`. See module docstring.
SAME_DIRECTION_STACKING_HARD_THRESHOLD: float = 0.15

PROPOSED_MACRO_BUCKET_CONFIG = MacroBucketConfig(
    bucket_membership={
        "RISK_SENTIMENT_BETA": {
            **{sym: 1.0 for sym in TRACK_A_CRYPTO_PAIRS},
            # Broad-market/high-beta equity proxies, illustrative starter
            # set -- not exhaustive, weight 0.7 (less than the 1.0 given
            # to the crypto pairs themselves, since these are a *proxy*
            # for the same risk-sentiment factor, not the factor itself).
            "SPY": 0.7, "QQQ": 0.7, "NVDA": 0.7, "TSLA": 0.7, "AMD": 0.7, "META": 0.7,
        },
        "RATES_SENSITIVITY": {"BAC": 1.0, "GS": 1.0, "JPM": 1.0, "XLF": 1.0},
        "COMMODITY_INFLATION": {"XOM": 1.0, "CVX": 1.0, "XLE": 1.0, "GLD": 1.0, "SLV": 1.0},
    },
    max_bucket_exposure_ratio=0.30,
)


# ------------------------------------------------------------------------
# Compute.
# ------------------------------------------------------------------------

def _bucket_weight_for_symbol(symbol: str, membership: dict[str, float]) -> float:
    """Exact match wins outright. Otherwise, sums the weights of every
    glob-style pattern key (one containing `*`, `?`, or `[`) that matches
    `symbol` via `fnmatch.fnmatchcase` -- summed, not "first match wins",
    so overlapping patterns are a disclosed, deliberate choice, not a
    silent pick. Returns 0.0 (no contribution) if nothing matches."""
    if symbol in membership:
        return membership[symbol]
    total = 0.0
    for pattern, weight in membership.items():
        if any(ch in pattern for ch in "*?[") and fnmatch.fnmatchcase(symbol, pattern):
            total += weight
    return total


def compute_bucket_exposure(
    snapshot: PortfolioSnapshot, config: MacroBucketConfig, account_equity_usd: float,
) -> dict[str, dict[str, Any]]:
    """DERIVED. Sums SIGNED notional (long positive, short negative,
    scaled by each matching bucket weight) per bucket, across every
    position in `snapshot` regardless of venue -- this is the whole
    point: crossing Track A MEXC crypto and Track B Alpaca equities in
    one view. A position with `notional_usd is None` is excluded from
    the sum (see module docstring) but counted in
    `positions_excluded_no_notional` for that bucket, so the gap is never
    silently invisible.

    `exposure_ratio` is `gross_notional_usd / account_equity_usd` (see
    module docstring -- UPDATED 2026-10-09, gross rather than net, so
    offsetting positions cannot mask total bucket risk).

    `long_stacking_notional_usd`/`short_stacking_notional_usd` and their
    `..._ratio` counterparts implement Martin's exact stacking-ratio
    formula (module docstring): the sum of `max(0, signed_notional)`
    per bucket is the LONG-side stack (every SHORT position contributes
    0 to this sum); the sum of `max(0, -signed_notional)` is the
    SHORT-side stack. Both are reported here regardless of whether
    `account_equity_usd` is usable; the `..._ratio` fields are `None`
    when it is not (same `NOT_COMPUTABLE`-worthy condition as
    `exposure_ratio`)."""
    out: dict[str, dict[str, Any]] = {}
    for bucket_name, membership in config.bucket_membership.items():
        net = 0.0
        gross = 0.0
        long_stack = 0.0
        short_stack = 0.0
        excluded_no_notional = 0
        for p in snapshot.positions:
            weight = _bucket_weight_for_symbol(p.symbol, membership)
            if weight == 0.0:
                continue
            if p.notional_usd is None:
                excluded_no_notional += 1
                continue
            sign = 1.0 if p.direction == "LONG" else -1.0
            scaled = p.notional_usd * weight
            signed = sign * scaled
            net += signed
            gross += abs(scaled)
            long_stack += max(0.0, signed)
            short_stack += max(0.0, -signed)
        ratio: float | None
        long_stacking_ratio: float | None
        short_stacking_ratio: float | None
        if account_equity_usd is None or account_equity_usd <= 0:
            ratio = None
            long_stacking_ratio = None
            short_stacking_ratio = None
        else:
            ratio = gross / account_equity_usd
            long_stacking_ratio = long_stack / account_equity_usd
            short_stacking_ratio = short_stack / account_equity_usd
        out[bucket_name] = {
            "net_notional_usd": net,
            "gross_notional_usd": gross,
            "exposure_ratio": ratio,
            "long_stacking_notional_usd": long_stack,
            "short_stacking_notional_usd": short_stack,
            "long_stacking_ratio": long_stacking_ratio,
            "short_stacking_ratio": short_stacking_ratio,
            "positions_excluded_no_notional": excluded_no_notional,
        }
    return out


def evaluate_macro_bucket_dimension(
    snapshot: PortfolioSnapshot, config: MacroBucketConfig, *, account_equity_usd: float, now: datetime | None = None,
) -> tuple[DimensionVerdict, ...]:
    """EFFECTIVE. One `DimensionVerdict` (dimension="macro_bucket_exposure",
    the bucket name lives in `evidence["bucket"]`, same shape `.344`'s own
    `correlation_group_concentration` extension already uses) per
    configured bucket, sorted by bucket name for determinism.
    `LIMIT_NOT_CONFIGURED` if `config.max_bucket_exposure_ratio is None`;
    `NOT_COMPUTABLE` if the ratio itself could not be computed (no usable
    account equity); else `BLOCK` if `exposure_ratio > limit` else
    `PASS`. If no buckets are configured at all, returns a single
    `LIMIT_NOT_CONFIGURED` placeholder so the dimension is always
    visible, matching `.344`'s own "never silently omit a dimension"
    convention."""
    _ = now  # not used by this dimension's own logic; accepted for signature symmetry with the sibling dimensions.
    exposures = compute_bucket_exposure(snapshot, config, account_equity_usd)

    if not exposures:
        return (
            DimensionVerdict(
                dimension="macro_bucket_exposure", venue=None, verdict=LIMIT_NOT_CONFIGURED,
                reason="NO_BUCKETS_CONFIGURED", evidence={},
            ),
        )

    out: list[DimensionVerdict] = []
    for bucket_name in sorted(exposures):
        result = exposures[bucket_name]
        if config.max_bucket_exposure_ratio is None:
            out.append(DimensionVerdict(
                dimension="macro_bucket_exposure", venue=None, verdict=LIMIT_NOT_CONFIGURED,
                reason="NO_LIMIT_CONFIGURED", evidence={"bucket": bucket_name, **result},
            ))
            continue
        ratio = result["exposure_ratio"]
        if ratio is None:
            out.append(DimensionVerdict(
                dimension="macro_bucket_exposure", venue=None, verdict=NOT_COMPUTABLE,
                reason="ACCOUNT_EQUITY_NOT_AVAILABLE", evidence={"bucket": bucket_name, **result},
            ))
            continue
        breached = ratio > config.max_bucket_exposure_ratio
        out.append(DimensionVerdict(
            dimension="macro_bucket_exposure", venue=None, verdict=BLOCK if breached else PASS,
            reason="LIMIT_BREACHED" if breached else "WITHIN_LIMIT",
            evidence={"bucket": bucket_name, "limit": config.max_bucket_exposure_ratio, **result},
        ))

    # Same-direction stacking -- hard, unconditional, deterministic (see
    # module docstring). Evaluated for EVERY configured bucket in BOTH
    # directions, every cycle, independent of `max_bucket_exposure_ratio`
    # (that field gates the dimension above; this one is never gated by
    # it, and is never `LIMIT_NOT_CONFIGURED` -- the threshold is a fixed
    # module constant, not a config field that could be left unset).
    for bucket_name in sorted(exposures):
        result = exposures[bucket_name]
        for direction_label, ratio_key, notional_key in (
            ("LONG", "long_stacking_ratio", "long_stacking_notional_usd"),
            ("SHORT", "short_stacking_ratio", "short_stacking_notional_usd"),
        ):
            stack_ratio = result[ratio_key]
            if stack_ratio is None:
                out.append(DimensionVerdict(
                    dimension="macro_bucket_same_direction_stacking", venue=None, verdict=NOT_COMPUTABLE,
                    reason="ACCOUNT_EQUITY_NOT_AVAILABLE",
                    evidence={"bucket": bucket_name, "direction": direction_label},
                ))
                continue
            breached = stack_ratio > SAME_DIRECTION_STACKING_HARD_THRESHOLD
            out.append(DimensionVerdict(
                dimension="macro_bucket_same_direction_stacking", venue=None, verdict=BLOCK if breached else PASS,
                reason="LIMIT_BREACHED" if breached else "WITHIN_LIMIT",
                evidence={
                    "bucket": bucket_name, "direction": direction_label,
                    "stacking_ratio": stack_ratio, "threshold": SAME_DIRECTION_STACKING_HARD_THRESHOLD,
                    "stacking_notional_usd": result[notional_key],
                },
            ))
    return tuple(out)


def evaluate_stacking_pretrade_direction(
    snapshot: PortfolioSnapshot, config: MacroBucketConfig, symbol: str, open_direction: str, *, account_equity_usd: float,
) -> tuple[DimensionVerdict, ...]:
    """EFFECTIVE, PRE-TRADE. For a candidate `OPEN_LONG`/`OPEN_SHORT`
    order in `symbol`, evaluates the CURRENT (pre-trade) same-direction
    stacking ratio -- per Martin's explicit directive, 2026-10-09 -- for
    every bucket `symbol` has a nonzero weight in, in the direction that
    matches the candidate order only (`OPEN_LONG` -> `long_stacking_ratio`,
    `OPEN_SHORT` -> `short_stacking_ratio`). This is deliberately a
    CURRENT-STATE check, not a would-this-one-trade-push-us-over
    projection: "if any macro risk bucket breaches this in a given
    direction" (the exact wording this check implements) describes an
    already-existing condition of the book, not a hypothetical one -- a
    bucket already at/over the hard threshold blocks every new entry in
    that direction for every symbol in that bucket, regardless of how
    small the new entry would be.

    Returns one `DimensionVerdict` per matching bucket (dimension=
    `"macro_bucket_same_direction_stacking"`, same shape the portfolio-
    level dimension in `evaluate_macro_bucket_dimension()` uses, plus
    `evidence["symbol"]`); an empty tuple if `symbol` matches no
    configured bucket, or if `open_direction` is not `OPEN_LONG`/
    `OPEN_SHORT` (exits are never gated by this check, matching `.368`'s
    own earnings-blackout convention of only gating opens)."""
    if open_direction not in ("OPEN_LONG", "OPEN_SHORT"):
        return ()

    exposures = compute_bucket_exposure(snapshot, config, account_equity_usd)
    if open_direction == "OPEN_LONG":
        ratio_key, notional_key, direction_label = "long_stacking_ratio", "long_stacking_notional_usd", "LONG"
    else:
        ratio_key, notional_key, direction_label = "short_stacking_ratio", "short_stacking_notional_usd", "SHORT"

    out: list[DimensionVerdict] = []
    for bucket_name, membership in config.bucket_membership.items():
        if _bucket_weight_for_symbol(symbol, membership) == 0.0:
            continue
        result = exposures[bucket_name]
        stack_ratio = result[ratio_key]
        if stack_ratio is None:
            out.append(DimensionVerdict(
                dimension="macro_bucket_same_direction_stacking", venue=None, verdict=NOT_COMPUTABLE,
                reason="ACCOUNT_EQUITY_NOT_AVAILABLE",
                evidence={"bucket": bucket_name, "direction": direction_label, "symbol": symbol},
            ))
            continue
        breached = stack_ratio > SAME_DIRECTION_STACKING_HARD_THRESHOLD
        out.append(DimensionVerdict(
            dimension="macro_bucket_same_direction_stacking", venue=None, verdict=BLOCK if breached else PASS,
            reason="LIMIT_BREACHED" if breached else "WITHIN_LIMIT",
            evidence={
                "bucket": bucket_name, "direction": direction_label, "symbol": symbol,
                "stacking_ratio": stack_ratio, "threshold": SAME_DIRECTION_STACKING_HARD_THRESHOLD,
                "stacking_notional_usd": result[notional_key],
            },
        ))
    return tuple(out)
