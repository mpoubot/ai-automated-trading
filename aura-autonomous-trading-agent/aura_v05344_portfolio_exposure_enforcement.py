#!/usr/bin/env python3
"""
AURA v0.5.3.44 — Portfolio Exposure Enforcement (deterministic, fail-closed).

ENFORCEMENT ONLY. This module never places, cancels, or modifies an order,
never mutates a position, and is not an execution engine — it consumes
`.43`'s canonical `PortfolioSnapshot` and exposure report and produces a
deterministic ALLOW/BLOCK decision plus per-dimension evidence. Wiring this
decision into `.31`/`.36`'s authorization chain (so a BLOCK actually stops
an order) is explicitly left to a later step — this milestone's acceptance
criteria, per Martin's scoping message, is the deterministic decision
engine itself, built on `.43`'s data, not a change to the execution spine.

Why this exists, and why it is scoped exactly this way (Martin's explicit
instruction, 2026-09-12)
------------------------------------------------------------------------
".43 = observe reality. .44 = enforce what we can prove. Later capability
= acquire missing information." Concretely:

  1. `.44` NEVER invents a portfolio limit. Every threshold this module can
     enforce against is an explicit, optional field on `PortfolioLimits`,
     defaulting to `None` ("not configured"). If no limit is configured,
     the decision is `LIMIT_NOT_CONFIGURED` — never a silent PASS, and
     never a number this module chose on its own ("3x leverage, 25%
     concentration, 5% daily loss" is exactly the kind of invented number
     Martin explicitly ruled out).
  2. `.44` does not expand what `.43` can compute. `.43` already marks 4 of
     its 12 dimensions structurally `NOT_COMPUTABLE` (per_trade_risk,
     correlation, liquidity_concentration, equity_sector_concentration) —
     this module reports those as `NOT_COMPUTABLE` too (informational,
     never blocking, never silently dropped) rather than trying to derive
     them from something else. The roadmap's original `.44` line item also
     names ATR-based sizing, stop-loss placement, a consecutive-loss limit,
     and a correlation check as "runtime risk rules" — every one of those
     needs a data source that does not exist in AURA today (a persisted
     per-position stop distance, a live cross-symbol return history, a
     trade-outcome win/loss ledger). Per Martin's explicit instruction not
     to expand `.44` into building that missing data, this module reports
     `consecutive_loss_limit` as `NOT_COMPUTABLE` for the same structural
     reason, alongside `.43`'s four, and does not implement ATR-sizing or
     stop-loss placement at all — those remain a later, separate milestone
     once the underlying data becomes computable.
  3. Data-quality is part of the decision, not a precondition checked
     elsewhere. A stale snapshot, an incomplete cross-venue picture, or a
     failed venue fetch must produce `BLOCK` (or a per-venue-scoped BLOCK),
     never be silently treated as "zero exposure" or "assume it's fine."

Reuse scan (Martin's explicit A/B/C/D framework, performed before writing
any new code)
------------------------------------------------------------------------
Extends `.43`'s own reuse scan (CAURA, BABIL, three DELTAX repos, `mexc_bot`)
with the three sources Martin specifically named this round:
  - TradePilot AI risk engine: already independently confirmed, in the
    v0.5.5 Final Implementation Baseline's own rolling open-items table
    (item 5, "Resolved in the Addendum"), to be an execution-time position-
    size/order-safety gate (per-order, not portfolio-level) — (C)
    architecturally adjacent (it is exactly the kind of deterministic,
    non-AI gate `.44` is) but not itself a portfolio-aggregation engine to
    adopt; its EXISTENCE is evidence for the design shape used here (a
    small, pure, deterministic decision function with explicit reject
    reasons), not a component to import.
  - ORION: per the Final Baseline, ORION's role is the `.49` adversarial-
    challenge AI critique pattern (a second AI pass scoring a *proposal*),
    not portfolio-exposure enforcement — (D) not applicable to `.44`.
  - Dark Wolf Sentinel: recovered competition code per the Final Baseline's
    open-items table; no portfolio-exposure or cross-venue aggregation
    logic was found in it during `.43`'s survey or this one (its
    contribution, per the recovery docs, is elsewhere in the roadmap) —
    (D) not applicable here.
  - `.31`/`.36` (`aura_v05331_mexc_execution_authorization.py`,
    `aura_v05336_alpaca_equity_execution_authorization.py`): both already
    establish the exact decision SHAPE this module reuses conceptually —
    a pure `_evaluate_guardrails()`-style function, one check at a time,
    each with its own named rejection reason, never trusting a caller-
    supplied safety state without independently recomputing it. (B) this
    shape is adapted here (see `DimensionVerdict`/`EnforcementDecision`
    below); the actual authorization code itself is not modified or
    imported — deliberately: wiring `.44`'s decision into `.31`/`.36` is
    left to a later step, not invented here as an unrequested change to
    the execution spine.
  - `mexc_bot/core/risk_manager.py`'s `CircuitBreaker` (already classified
    (B) in `.43`'s report): its daily-loss-pct/drawdown-pct FORMULAS are
    already reused by `.43`'s `compute_daily_loss`/`compute_max_drawdown`
    — `.44` does not re-derive them, it only adds the limit-comparison and
    fail-closed data-quality layer on top of `.43`'s already-computed
    numbers. Its hardcoded 8%/20% thresholds are explicitly NOT adopted
    here (see next section) — its own origin document (`mexc_bot/config.py`)
    discloses them as "best known settings" from a thin backtest edge, not
    confirmed AURA policy, and no `aura_v053xx` file or config has ever
    ratified them. Per Martin's explicit instruction ("First identify
    which limits are already specified in AURA/DELTAX/strategy
    configuration... where no authoritative threshold exists, expose
    LIMIT_NOT_CONFIGURED rather than choosing a number yourself"): no
    `aura_v053xx` config, doc, or constant anywhere marks ANY numeric
    portfolio limit as AURA's own decided policy today — confirmed by a
    repo-wide audit before writing this module. Every field on
    `PortfolioLimits` below therefore defaults to `None`. This is a
    finding to report to Martin, not a gap this module silently fills.

Decision model (Martin's own ASCII flowchart, implemented literally)
------------------------------------------------------------------------
    exposure data valid?
        NO  -> BLOCK  (stale snapshot / incomplete cross-venue data /
                        failed venue fetch / insufficient history yet)
        YES ->
    applicable limit configured?
        NO  -> LIMIT_NOT_CONFIGURED
        YES ->
    limit breached?
        YES -> BLOCK
        NO  -> PASS

One addition Martin's flowchart does not explicitly separate but this
module needs: a dimension `.43` marks structurally `NOT_COMPUTABLE` (no
data source anywhere in AURA, not a timing/venue problem) is reported as
its own `NOT_COMPUTABLE` verdict — informational, never blocking. Treating
a permanent structural gap the same as a transient data-quality failure
would either (a) block trading forever on something that can never
resolve, or (b) require inventing a workaround — both wrong. `NOT_COMPUTABLE`
makes the gap visible without either failure mode.

Data-quality -> verdict mapping, and WHY (documented per Martin's request
to make every judgment call auditable)
------------------------------------------------------------------------
  - Snapshot older than `max_snapshot_age_seconds` (a REQUIRED, caller-
    supplied operational parameter — see `evaluate_portfolio_enforcement`'s
    docstring for why this is not defaulted): the ENTIRE decision short-
    circuits to a single BLOCK. An old snapshot cannot selectively be
    trusted for some dimensions and not others.
  - A CONFIGURED venue with `VenueFetchStatus.status == "FAILED"`: any
    dimension that is cross-venue/aggregate (portfolio_heat,
    asset_concentration, directional_exposure — all sum across every
    position in the snapshot) BLOCKs, because the failed venue's real
    positions are simply absent from that sum — reporting a heat/
    concentration/direction number computed only from the venues that
    happened to succeed would silently understate true exposure, exactly
    the "failed venue treated as zero exposure" failure Martin named
    explicitly. A dimension that is genuinely per-venue (daily_loss,
    max_drawdown, leverage_exposure's per-venue aggregate, the MEXC
    leverage-cap check) BLOCKs only for the failed venue itself; a
    venue that succeeded is still evaluated normally.
  - A venue `NOT_CONFIGURED` (this deployment simply isn't wired to that
    venue in this run): per-venue dimensions for that venue are
    `NOT_APPLICABLE` (not evaluated, not counted as a data-quality
    failure) — this is a deployment fact, not a broken fetch. Aggregate
    dimensions still only need the CONFIGURED venues to all have
    succeeded; a venue nobody configured cannot make the aggregate
    "incomplete."
  - `.43`'s own `NOT_YET_AVAILABLE` (daily_loss with no same-day prior
    snapshot yet) BLOCKs with reason `INSUFFICIENT_HISTORY` — per
    Martin's fail-closed instruction ("incomplete... -> fail closed where
    the relevant exposure cannot be proven safe"), not proven safe means
    BLOCK, even though `.43`'s own report correctly notes this is expected
    and self-resolving as history accumulates, not a bug.
  - `asset_concentration`/`leverage_exposure` reporting `NOT_COMPUTABLE`
    purely because a venue/the portfolio holds ZERO priced positions (a
    genuinely flat book) is treated as trivially safe (PASS if a limit is
    configured, else LIMIT_NOT_CONFIGURED) rather than BLOCK — an empty
    portfolio has no concentration or leverage to breach. This is
    distinguished from "positions exist but could not be priced"
    (`positions_excluded_no_notional > 0`), which DOES BLOCK — that is a
    real "cannot prove safe" situation, not an empty book.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
OBSERVABILITY_MODULE_PATH = ROOT / "aura_v05343_portfolio_exposure_observability.py"


def _load_observability_module():
    spec = importlib.util.spec_from_file_location(
        "aura_v05343_portfolio_exposure_observability", OBSERVABILITY_MODULE_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses inside .43 need this registered first
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


OBS = _load_observability_module()
VENUES = OBS.VENUES


# ------------------------------------------------------------------------
# stable_json / sha256_text -- copied, not imported, matching this repo's
# own established convention (every aura_v053NN module defines these
# itself; see e.g. .43's own copy, which is itself copied from .29/.12).
# ------------------------------------------------------------------------

def stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


# ------------------------------------------------------------------------
# Verdicts.
# ------------------------------------------------------------------------

PASS = "PASS"
BLOCK = "BLOCK"
LIMIT_NOT_CONFIGURED = "LIMIT_NOT_CONFIGURED"
NOT_COMPUTABLE = "NOT_COMPUTABLE"
NOT_APPLICABLE = "NOT_APPLICABLE"

ALLOW_VERDICTS = frozenset({PASS, LIMIT_NOT_CONFIGURED, NOT_COMPUTABLE, NOT_APPLICABLE})


@dataclass(frozen=True)
class PortfolioLimits:
    """Every field defaults to None / empty ("not configured"). This
    module never substitutes a number of its own for a missing field --
    see the module docstring for why. Martin (or a future, explicitly-
    approved risk-configuration milestone) supplies these; until then
    every dimension below reports LIMIT_NOT_CONFIGURED, not a guess."""
    max_portfolio_heat_ratio: float | None = None
    max_asset_concentration_ratio: float | None = None
    max_net_exposure_ratio: float | None = None
    max_leverage_ratio_by_venue: dict[str, float] = field(default_factory=dict)
    mexc_leverage_cap: float | None = None
    max_daily_loss_pct_by_venue: dict[str, float] = field(default_factory=dict)
    max_drawdown_pct_by_venue: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_portfolio_heat_ratio": self.max_portfolio_heat_ratio,
            "max_asset_concentration_ratio": self.max_asset_concentration_ratio,
            "max_net_exposure_ratio": self.max_net_exposure_ratio,
            "max_leverage_ratio_by_venue": dict(self.max_leverage_ratio_by_venue),
            "mexc_leverage_cap": self.mexc_leverage_cap,
            "max_daily_loss_pct_by_venue": dict(self.max_daily_loss_pct_by_venue),
            "max_drawdown_pct_by_venue": dict(self.max_drawdown_pct_by_venue),
        }


@dataclass(frozen=True)
class DimensionVerdict:
    dimension: str
    venue: str | None
    verdict: str
    reason: str
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"dimension": self.dimension, "venue": self.venue, "verdict": self.verdict,
                "reason": self.reason, "evidence": self.evidence}


@dataclass(frozen=True)
class EnforcementDecision:
    as_of: str
    snapshot_as_of: str
    snapshot_state_hash: str
    overall_verdict: str  # "ALLOW" | "BLOCK"
    dimension_verdicts: tuple[DimensionVerdict, ...]
    decision_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of, "snapshot_as_of": self.snapshot_as_of,
            "snapshot_state_hash": self.snapshot_state_hash, "overall_verdict": self.overall_verdict,
            "dimension_verdicts": [v.to_dict() for v in self.dimension_verdicts],
            "decision_hash": self.decision_hash,
        }


def _finalize(as_of: str, snapshot_as_of: str, snapshot_state_hash: str, verdicts: list[DimensionVerdict]) -> EnforcementDecision:
    overall = BLOCK if any(v.verdict == BLOCK for v in verdicts) else "ALLOW"
    body = {
        "as_of": as_of, "snapshot_as_of": snapshot_as_of, "snapshot_state_hash": snapshot_state_hash,
        "overall_verdict": overall, "dimension_verdicts": [v.to_dict() for v in verdicts],
    }
    decision_hash = sha256_text(stable_json(body))
    return EnforcementDecision(
        as_of=as_of, snapshot_as_of=snapshot_as_of, snapshot_state_hash=snapshot_state_hash,
        overall_verdict=overall, dimension_verdicts=tuple(verdicts), decision_hash=decision_hash,
    )


# ------------------------------------------------------------------------
# Snapshot freshness -- the top of Martin's flowchart, evaluated first and
# short-circuits everything else if it fails.
# ------------------------------------------------------------------------

def _check_snapshot_freshness(snapshot, now: datetime, max_snapshot_age_seconds: float) -> DimensionVerdict | None:
    snapshot_dt = _parse_iso(snapshot.as_of)
    age_seconds = (now - snapshot_dt).total_seconds()
    if age_seconds > max_snapshot_age_seconds:
        return DimensionVerdict(
            dimension="snapshot_freshness", venue=None, verdict=BLOCK, reason="SNAPSHOT_STALE",
            evidence={"snapshot_as_of": snapshot.as_of, "age_seconds": age_seconds, "max_snapshot_age_seconds": max_snapshot_age_seconds},
        )
    if age_seconds < 0:
        # A snapshot timestamped in the future relative to `now` is exactly
        # as untrustworthy as a stale one -- never silently accepted.
        return DimensionVerdict(
            dimension="snapshot_freshness", venue=None, verdict=BLOCK, reason="SNAPSHOT_TIMESTAMP_IN_FUTURE",
            evidence={"snapshot_as_of": snapshot.as_of, "now": now.isoformat()},
        )
    return None


def _failed_venues(snapshot) -> list[str]:
    return [v for v, st in snapshot.venue_fetch_status.items() if st.status == "FAILED"]


# ------------------------------------------------------------------------
# Aggregate (cross-venue) dimensions: portfolio_heat, asset_concentration,
# directional_exposure. All three BLOCK if any configured venue failed --
# see module docstring for why a partial sum cannot substitute here.
# ------------------------------------------------------------------------

def _aggregate_data_quality_block(snapshot) -> DimensionVerdict | None:
    failed = _failed_venues(snapshot)
    if failed:
        return DimensionVerdict(
            dimension="__aggregate__", venue=None, verdict=BLOCK, reason="VENUE_DATA_INCOMPLETE",
            evidence={"failed_venues": failed, "reason": "one or more configured venues failed to fetch; an aggregate computed from the remaining venues would silently understate true exposure"},
        )
    return None


def _check_portfolio_heat(exposure_report: dict, snapshot, limits: PortfolioLimits) -> DimensionVerdict:
    blocked = _aggregate_data_quality_block(snapshot)
    if blocked is not None:
        return DimensionVerdict(dimension="portfolio_heat", venue=None, verdict=BLOCK, reason=blocked.reason, evidence=blocked.evidence)
    report = exposure_report["portfolio_heat"]
    if report["status"] != "COMPUTABLE":
        return DimensionVerdict(dimension="portfolio_heat", venue=None, verdict=BLOCK, reason="EXPOSURE_NOT_COMPUTABLE",
                                 evidence={"upstream_reason": report.get("reason")})
    if limits.max_portfolio_heat_ratio is None:
        return DimensionVerdict(dimension="portfolio_heat", venue=None, verdict=LIMIT_NOT_CONFIGURED, reason="NO_LIMIT_CONFIGURED",
                                 evidence={"heat_ratio": report["heat_ratio"]})
    breached = report["heat_ratio"] > limits.max_portfolio_heat_ratio
    return DimensionVerdict(
        dimension="portfolio_heat", venue=None, verdict=BLOCK if breached else PASS,
        reason="LIMIT_BREACHED" if breached else "WITHIN_LIMIT",
        evidence={"heat_ratio": report["heat_ratio"], "limit": limits.max_portfolio_heat_ratio},
    )


def _check_asset_concentration(exposure_report: dict, snapshot, limits: PortfolioLimits) -> DimensionVerdict:
    blocked = _aggregate_data_quality_block(snapshot)
    if blocked is not None:
        return DimensionVerdict(dimension="asset_concentration", venue=None, verdict=BLOCK, reason=blocked.reason, evidence=blocked.evidence)
    report = exposure_report["asset_concentration"]
    excluded = report.get("positions_excluded_no_notional", 0)
    # A genuinely empty book (zero positions anywhere in the snapshot) is
    # safe by construction, regardless of how .43 happened to label its
    # own status for a zero-total sum (its own code returns NOT_COMPUTABLE
    # for "no priced positions", which is ambiguous between "no positions
    # at all" and "positions exist but none could be priced" -- .44
    # disambiguates using the snapshot's own position count directly,
    # since that is unambiguous). Anything else that isn't cleanly
    # COMPUTABLE, or that excluded real positions for lack of a price,
    # cannot be proven safe.
    flat_book = len(snapshot.positions) == 0
    if not flat_book and (report["status"] != "COMPUTABLE" or excluded):
        return DimensionVerdict(dimension="asset_concentration", venue=None, verdict=BLOCK, reason="EXPOSURE_NOT_COMPUTABLE",
                                 evidence={"upstream_status": report["status"], "positions_excluded_no_notional": excluded})
    max_share = max(report["by_symbol"].values()) if report["by_symbol"] else 0.0
    if limits.max_asset_concentration_ratio is None:
        return DimensionVerdict(dimension="asset_concentration", venue=None, verdict=LIMIT_NOT_CONFIGURED, reason="NO_LIMIT_CONFIGURED",
                                 evidence={"max_symbol_share": max_share, "by_symbol": report["by_symbol"]})
    breached = max_share > limits.max_asset_concentration_ratio
    return DimensionVerdict(
        dimension="asset_concentration", venue=None, verdict=BLOCK if breached else PASS,
        reason="LIMIT_BREACHED" if breached else "WITHIN_LIMIT",
        evidence={"max_symbol_share": max_share, "limit": limits.max_asset_concentration_ratio, "by_symbol": report["by_symbol"]},
    )


def _check_directional_exposure(exposure_report: dict, snapshot, limits: PortfolioLimits) -> DimensionVerdict:
    blocked = _aggregate_data_quality_block(snapshot)
    if blocked is not None:
        return DimensionVerdict(dimension="directional_exposure", venue=None, verdict=BLOCK, reason=blocked.reason, evidence=blocked.evidence)
    report = exposure_report["directional_exposure"]
    if report["status"] != "COMPUTABLE":
        return DimensionVerdict(dimension="directional_exposure", venue=None, verdict=BLOCK, reason="EXPOSURE_NOT_COMPUTABLE",
                                 evidence={"upstream_reason": report.get("reason")})
    if limits.max_net_exposure_ratio is None:
        return DimensionVerdict(dimension="directional_exposure", venue=None, verdict=LIMIT_NOT_CONFIGURED, reason="NO_LIMIT_CONFIGURED",
                                 evidence={"net_exposure_ratio": report["net_exposure_ratio"]})
    breached = abs(report["net_exposure_ratio"]) > limits.max_net_exposure_ratio
    return DimensionVerdict(
        dimension="directional_exposure", venue=None, verdict=BLOCK if breached else PASS,
        reason="LIMIT_BREACHED" if breached else "WITHIN_LIMIT",
        evidence={"net_exposure_ratio": report["net_exposure_ratio"], "limit": limits.max_net_exposure_ratio},
    )


# ------------------------------------------------------------------------
# Per-venue dimensions: leverage_exposure (per-venue aggregate), the MEXC
# leverage-cap check, daily_loss, max_drawdown. Each venue is evaluated
# independently -- a failed/NOT_CONFIGURED venue never blocks a venue
# that actually succeeded.
# ------------------------------------------------------------------------

def _venue_data_quality(snapshot, venue: str) -> DimensionVerdict | None:
    """Returns a BLOCK/NOT_APPLICABLE verdict template if this venue's own
    fetch status disqualifies it, else None (safe to evaluate this venue's
    numbers)."""
    status = snapshot.venue_fetch_status.get(venue)
    if status is None or status.status == "NOT_CONFIGURED":
        return DimensionVerdict(dimension="__venue__", venue=venue, verdict=NOT_APPLICABLE, reason="VENUE_NOT_CONFIGURED", evidence={})
    if status.status == "FAILED":
        return DimensionVerdict(dimension="__venue__", venue=venue, verdict=BLOCK, reason="VENUE_FETCH_FAILED", evidence={"error": status.error})
    return None


def _check_leverage_exposure(exposure_report: dict, snapshot, limits: PortfolioLimits) -> list[DimensionVerdict]:
    out: list[DimensionVerdict] = []
    per_venue = exposure_report["leverage_exposure"]["per_venue_aggregate"]
    for venue in VENUES:
        dq = _venue_data_quality(snapshot, venue)
        if dq is not None:
            out.append(DimensionVerdict(dimension="leverage_exposure", venue=venue, verdict=dq.verdict, reason=dq.reason, evidence=dq.evidence))
            continue
        venue_report = per_venue.get(venue, {"status": "NOT_COMPUTABLE"})
        positions_count = snapshot.venue_fetch_status[venue].positions_count
        if venue_report["status"] != "COMPUTABLE":
            if positions_count == 0:
                # A flat book has zero leverage by construction -- safe, not a data-quality problem.
                verdict, reason, evidence = (PASS, "FLAT_BOOK", {"aggregate_leverage_ratio": 0.0}) if limits.max_leverage_ratio_by_venue.get(venue) is not None else (LIMIT_NOT_CONFIGURED, "NO_LIMIT_CONFIGURED", {"aggregate_leverage_ratio": 0.0})
                out.append(DimensionVerdict(dimension="leverage_exposure", venue=venue, verdict=verdict, reason=reason, evidence=evidence))
            else:
                out.append(DimensionVerdict(dimension="leverage_exposure", venue=venue, verdict=BLOCK, reason="EXPOSURE_NOT_COMPUTABLE", evidence={"positions_count": positions_count}))
            continue
        limit = limits.max_leverage_ratio_by_venue.get(venue)
        if limit is None:
            out.append(DimensionVerdict(dimension="leverage_exposure", venue=venue, verdict=LIMIT_NOT_CONFIGURED, reason="NO_LIMIT_CONFIGURED",
                                         evidence={"aggregate_leverage_ratio": venue_report["aggregate_leverage_ratio"]}))
            continue
        breached = venue_report["aggregate_leverage_ratio"] > limit
        out.append(DimensionVerdict(dimension="leverage_exposure", venue=venue, verdict=BLOCK if breached else PASS,
                                     reason="LIMIT_BREACHED" if breached else "WITHIN_LIMIT",
                                     evidence={"aggregate_leverage_ratio": venue_report["aggregate_leverage_ratio"], "limit": limit}))
    return out


def _check_mexc_leverage_cap(snapshot, limits: PortfolioLimits) -> DimensionVerdict:
    dq = _venue_data_quality(snapshot, "MEXC")
    if dq is not None:
        return DimensionVerdict(dimension="mexc_leverage_cap", venue="MEXC", verdict=dq.verdict, reason=dq.reason, evidence=dq.evidence)
    positions = list(snapshot.positions)
    result = OBS.check_mexc_leverage_cap(positions, limits.mexc_leverage_cap)
    if not result["cap_configured"]:
        return DimensionVerdict(dimension="mexc_leverage_cap", venue="MEXC", verdict=LIMIT_NOT_CONFIGURED, reason="NO_LIMIT_CONFIGURED",
                                 evidence={"positions": result["positions"]})
    breached = bool(result["any_position_exceeds_cap"])
    return DimensionVerdict(dimension="mexc_leverage_cap", venue="MEXC", verdict=BLOCK if breached else PASS,
                             reason="LIMIT_BREACHED" if breached else "WITHIN_LIMIT",
                             evidence={"positions": result["positions"], "cap": limits.mexc_leverage_cap})


def _check_daily_loss(exposure_report: dict, snapshot, limits: PortfolioLimits) -> list[DimensionVerdict]:
    out: list[DimensionVerdict] = []
    per_venue = exposure_report["daily_loss"]
    for venue in VENUES:
        dq = _venue_data_quality(snapshot, venue)
        if dq is not None:
            out.append(DimensionVerdict(dimension="daily_loss", venue=venue, verdict=dq.verdict, reason=dq.reason, evidence=dq.evidence))
            continue
        venue_report = per_venue[venue]
        if venue_report["status"] == "NOT_YET_AVAILABLE":
            out.append(DimensionVerdict(dimension="daily_loss", venue=venue, verdict=BLOCK, reason="INSUFFICIENT_HISTORY", evidence={"upstream_reason": venue_report["reason"]}))
            continue
        if venue_report["status"] != "COMPUTABLE":
            out.append(DimensionVerdict(dimension="daily_loss", venue=venue, verdict=BLOCK, reason="EXPOSURE_NOT_COMPUTABLE", evidence={"upstream_reason": venue_report.get("reason")}))
            continue
        limit = limits.max_daily_loss_pct_by_venue.get(venue)
        if limit is None:
            out.append(DimensionVerdict(dimension="daily_loss", venue=venue, verdict=LIMIT_NOT_CONFIGURED, reason="NO_LIMIT_CONFIGURED",
                                         evidence={"daily_loss_pct": venue_report["daily_loss_pct"]}))
            continue
        breached = venue_report["daily_loss_pct"] > limit
        out.append(DimensionVerdict(dimension="daily_loss", venue=venue, verdict=BLOCK if breached else PASS,
                                     reason="LIMIT_BREACHED" if breached else "WITHIN_LIMIT",
                                     evidence={"daily_loss_pct": venue_report["daily_loss_pct"], "limit": limit}))
    return out


def _check_max_drawdown(exposure_report: dict, snapshot, limits: PortfolioLimits) -> list[DimensionVerdict]:
    out: list[DimensionVerdict] = []
    per_venue = exposure_report["max_drawdown"]
    for venue in VENUES:
        dq = _venue_data_quality(snapshot, venue)
        if dq is not None:
            out.append(DimensionVerdict(dimension="max_drawdown", venue=venue, verdict=dq.verdict, reason=dq.reason, evidence=dq.evidence))
            continue
        venue_report = per_venue[venue]
        if venue_report["status"] != "COMPUTABLE":
            out.append(DimensionVerdict(dimension="max_drawdown", venue=venue, verdict=BLOCK, reason="EXPOSURE_NOT_COMPUTABLE", evidence={"upstream_reason": venue_report.get("reason")}))
            continue
        limit = limits.max_drawdown_pct_by_venue.get(venue)
        if limit is None:
            out.append(DimensionVerdict(dimension="max_drawdown", venue=venue, verdict=LIMIT_NOT_CONFIGURED, reason="NO_LIMIT_CONFIGURED",
                                         evidence={"drawdown_pct": venue_report["drawdown_pct"]}))
            continue
        breached = venue_report["drawdown_pct"] > limit
        out.append(DimensionVerdict(dimension="max_drawdown", venue=venue, verdict=BLOCK if breached else PASS,
                                     reason="LIMIT_BREACHED" if breached else "WITHIN_LIMIT",
                                     evidence={"drawdown_pct": venue_report["drawdown_pct"], "limit": limit}))
    return out


# ------------------------------------------------------------------------
# Structurally NOT_COMPUTABLE dimensions -- informational passthrough,
# never blocking. Mirrors .43's NOT_COMPUTABLE_STATIC, plus
# consecutive_loss_limit (a .44-roadmap item with no live data source --
# see module docstring point 2).
# ------------------------------------------------------------------------

NOT_COMPUTABLE_DIMENSIONS: dict[str, str] = {
    **OBS.NOT_COMPUTABLE_STATIC,
    "consecutive_loss_limit": "no live trade-outcome (win/loss) ledger exists anywhere in AURA today -- .29's intent ledger tracks order lifecycle state, not realized win/loss outcome",
}


def _not_computable_verdicts() -> list[DimensionVerdict]:
    return [
        DimensionVerdict(dimension=dim, venue=None, verdict=NOT_COMPUTABLE, reason="NO_DATA_SOURCE", evidence={"reason": reason})
        for dim, reason in NOT_COMPUTABLE_DIMENSIONS.items()
    ]


# ------------------------------------------------------------------------
# Public entry points.
# ------------------------------------------------------------------------

def evaluate_portfolio_enforcement(
    snapshot, exposure_report: dict[str, Any], limits: PortfolioLimits, *,
    max_snapshot_age_seconds: float, now: datetime | None = None,
) -> EnforcementDecision:
    """Evaluates the CURRENT portfolio state (no hypothetical trade) against
    `limits`. `max_snapshot_age_seconds` has no default on purpose: this is
    an operational parameter (how old is too old to trust), not a strategy-
    design number -- the caller wiring this into a real runtime loop must
    make that choice explicitly, matching this module's own refusal to
    invent portfolio limits. Pure function of its inputs plus `now`
    (defaults to real UTC now only for convenience; pass `now` explicitly
    for deterministic/reproducible tests)."""
    now = now or datetime.now(timezone.utc)
    as_of = now.isoformat()

    fresh_block = _check_snapshot_freshness(snapshot, now, max_snapshot_age_seconds)
    if fresh_block is not None:
        return _finalize(as_of, snapshot.as_of, snapshot.state_hash, [fresh_block])

    verdicts: list[DimensionVerdict] = []
    verdicts.append(_check_portfolio_heat(exposure_report, snapshot, limits))
    verdicts.append(_check_asset_concentration(exposure_report, snapshot, limits))
    verdicts.append(_check_directional_exposure(exposure_report, snapshot, limits))
    verdicts.extend(_check_leverage_exposure(exposure_report, snapshot, limits))
    verdicts.append(_check_mexc_leverage_cap(snapshot, limits))
    verdicts.extend(_check_daily_loss(exposure_report, snapshot, limits))
    verdicts.extend(_check_max_drawdown(exposure_report, snapshot, limits))
    verdicts.extend(_not_computable_verdicts())

    return _finalize(as_of, snapshot.as_of, snapshot.state_hash, verdicts)


def evaluate_hypothetical_trade(
    snapshot, equity_history: list[dict[str, Any]], hypothetical: Any, limits: PortfolioLimits, *,
    max_snapshot_age_seconds: float, now: datetime | None = None,
) -> EnforcementDecision:
    """Authorization-time gate: would adding `hypothetical` (a .43
    PositionRecord that does not yet exist in `snapshot`) breach a
    configured limit? Reuses .43's OWN `project_post_trade_exposure()` for
    the projected numbers -- this module does not build a second position-
    aggregation model. Current-state-only dimensions unaffected by one
    hypothetical order (daily_loss, max_drawdown) are evaluated against the
    snapshot as it actually is today; asset_concentration is likewise
    evaluated against the current state only, since .43's own projection
    helper does not include it (this module does not extend that helper --
    see module docstring point 2 on not expanding .43)."""
    now = now or datetime.now(timezone.utc)
    as_of = now.isoformat()

    fresh_block = _check_snapshot_freshness(snapshot, now, max_snapshot_age_seconds)
    if fresh_block is not None:
        return _finalize(as_of, snapshot.as_of, snapshot.state_hash, [fresh_block])

    current_report = OBS.compute_exposure_dimensions(snapshot, equity_history, mexc_leverage_cap=limits.mexc_leverage_cap)
    projection = OBS.project_post_trade_exposure(snapshot, equity_history, hypothetical, mexc_leverage_cap=limits.mexc_leverage_cap)

    verdicts: list[DimensionVerdict] = []

    # Projected (post-trade) aggregate + leverage/cap checks -- built from
    # projection["after"], reusing the exact same check functions by
    # constructing a minimal exposure-report-shaped dict for the parts
    # project_post_trade_exposure actually provides.
    after = projection["after"]
    pseudo_report_for_heat = {"portfolio_heat": after["portfolio_heat"]}
    pseudo_report_for_direction = {"directional_exposure": after["directional_exposure"]}
    verdicts.append(_check_portfolio_heat(pseudo_report_for_heat, snapshot, limits))
    verdicts.append(_check_directional_exposure(pseudo_report_for_direction, snapshot, limits))

    pseudo_report_for_leverage = {"leverage_exposure": after["leverage_exposure"]}
    verdicts.extend(_check_leverage_exposure(pseudo_report_for_leverage, snapshot, limits))

    # mexc_leverage_cap_check from the projection already reflects the
    # hypothetical position -- use it directly rather than re-deriving.
    projected_cap = after["mexc_leverage_cap_check"]
    dq = _venue_data_quality(snapshot, "MEXC")
    if dq is not None and hypothetical.venue == "MEXC":
        verdicts.append(DimensionVerdict(dimension="mexc_leverage_cap", venue="MEXC", verdict=dq.verdict, reason=dq.reason, evidence=dq.evidence))
    elif not projected_cap["cap_configured"]:
        verdicts.append(DimensionVerdict(dimension="mexc_leverage_cap", venue="MEXC", verdict=LIMIT_NOT_CONFIGURED, reason="NO_LIMIT_CONFIGURED", evidence={"positions": projected_cap["positions"]}))
    else:
        breached = bool(projected_cap["any_position_exceeds_cap"])
        verdicts.append(DimensionVerdict(dimension="mexc_leverage_cap", venue="MEXC", verdict=BLOCK if breached else PASS,
                                          reason="LIMIT_BREACHED" if breached else "WITHIN_LIMIT",
                                          evidence={"positions": projected_cap["positions"], "cap": limits.mexc_leverage_cap}))

    # Current-state-only dimensions, unaffected by the hypothetical trade.
    verdicts.append(_check_asset_concentration(current_report, snapshot, limits))
    verdicts.extend(_check_daily_loss(current_report, snapshot, limits))
    verdicts.extend(_check_max_drawdown(current_report, snapshot, limits))
    verdicts.extend(_not_computable_verdicts())

    return _finalize(as_of, snapshot.as_of, snapshot.state_hash, verdicts)
