#!/usr/bin/env python3
"""
AURA portfolio/validation_throttle.py — Capital-Allocation Validation
Throttle (a NEW capital-governance layer, not a `.344`-style risk limit).

WHAT THIS MODULE IS, AND WHY IT IS DELIBERATELY DIFFERENT FROM EVERY
OTHER NEW MODULE IN THIS PACKAGE
------------------------------------------------------------------------
Every other dimension added in this package (`macro_buckets.py`,
`greeks_limits.py`) follows `.344`'s own "never invent a limit" rule
literally: every new numeric threshold defaults to `None` until Martin
explicitly ratifies it, because those modules enforce POLICY LIMITS on
exposure that already exists -- exactly the kind of number `.344`'s
module docstring says must never be guessed.

This module is not that. `TrackValidationRecord`/`ValidationThrottleConfig`
do not cap an existing exposure; they GATE HOW MUCH NEW CAPITAL a track is
even allowed to receive, based on whether that track has cleared a
validation bar. There is no pre-existing "AURA policy" value for "how
many out-of-sample windows constitute validated", the way there is
sometimes a pre-existing circuit-breaker threshold elsewhere in this
repo -- this is a brand-new governance mechanism being proposed fresh,
not a number this project already had and that `.344`'s discipline would
otherwise require surfacing as `LIMIT_NOT_CONFIGURED`. For that reason,
`ValidationThrottleConfig`'s fields carry concrete defaults directly on
the dataclass, NOT split into a separate `PROPOSED_*` constant the way
every other new config in this package is. Every one of those defaults
is marked below as "PROPOSED, PENDING MARTIN'S REVIEW" -- Martin can
construct `ValidationThrottleConfig()` with no arguments today and get a
real, usable throttle, but every number in it is this project's own
proposal, not yet a ratified AURA policy, and should be read as such.

What this dimension does, and -- just as important -- what it does NOT do
------------------------------------------------------------------------
`evaluate_validation_throttle_dimension()` below ALWAYS reports `PASS`.
This dimension never BLOCKs a trade directly. It is an informational/
SIZING dimension: its `DimensionVerdict.evidence` carries the computed
`state` and `allocation_multiplier` for a downstream position-sizing
step to read and apply. A track sized at `allocation_multiplier == 0.0`
(an `UNVALIDATED` track) is a de facto block -- achieved through sizing
the trade to zero, not through a `BLOCK` verdict. This is a deliberate
design choice, different from every other dimension in this package and
from `.344` itself: `.344`'s own `_finalize()` treats `overall_verdict =
BLOCK if any(v.verdict == BLOCK ...)` -- an always-`PASS` dimension can
never, by construction, flip that overall verdict to `BLOCK` on its own.
Capital governance here is enforced by WHAT GETS SIZED, not by WHAT GETS
VETOED, and the two mechanisms are intentionally kept separate so a
caller reading dimension verdicts for "did anything actually block this
trade" is never confused by a sizing signal that looks like a veto.

Reuses `.344`'s real `DimensionVerdict` and `PASS` constant, loaded the
same way every other module in this package does (see `macro_buckets.py`
for the fuller rationale of the loader block duplicated here).
"""
from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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

DimensionVerdict = _ENF.DimensionVerdict
PASS = _ENF.PASS


# ------------------------------------------------------------------------
# State model.
# ------------------------------------------------------------------------

VALIDATION_STATES: frozenset[str] = frozenset({"UNVALIDATED", "PAPER_VALIDATED", "LIVE_VALIDATED"})


@dataclass(frozen=True)
class TrackValidationRecord:
    """OBSERVED, caller-maintained record of one track's current
    validation state. `supporting_metrics` is free-form (e.g. the window
    results or pilot results that produced `state`, for audit) -- not
    interpreted by this module beyond one documented convention: a
    `LIVE_VALIDATED` record may carry `supporting_metrics
    ["live_validated_stage_index"]` (int, default 0) to select which of
    `ValidationThrottleConfig.live_validated_scale_stages` currently
    applies. This convention exists because
    `evaluate_validation_throttle_dimension()`'s own signature (per this
    module's spec) takes only `track_records`/`config`, with no separate
    per-track stage-index parameter -- so the stage index has to live
    somewhere on the record itself."""
    track_id: str
    state: str  # "UNVALIDATED" | "PAPER_VALIDATED" | "LIVE_VALIDATED"
    as_of: str
    supporting_metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "track_id": self.track_id, "state": self.state, "as_of": self.as_of,
            "supporting_metrics": dict(self.supporting_metrics),
        }


@dataclass(frozen=True)
class ValidationThrottleConfig:
    """Every default below is PROPOSED, PENDING MARTIN'S REVIEW -- see
    module docstring for why this dataclass does not follow the
    "defaults to None" convention the other new modules in this package
    use. Martin can override any field explicitly; nothing here is a
    ratified AURA policy yet."""
    # PROPOSED, PENDING MARTIN'S REVIEW: how many non-overlapping
    # out-of-sample windows must a track clear before "paper validated".
    paper_validated_min_windows: int = 3
    # PROPOSED, PENDING MARTIN'S REVIEW: every window's profit factor
    # must exceed this.
    paper_validated_min_profit_factor: float = 1.2
    # PROPOSED, PENDING MARTIN'S REVIEW: every window's p-value must be
    # below this (statistical significance bar).
    paper_validated_max_p_value: float = 0.10
    # PROPOSED, PENDING MARTIN'S REVIEW: fraction of equity allocated to
    # a PAPER_VALIDATED track's real-money pilot.
    paper_pilot_allocation_pct: float = 0.03
    # PROPOSED, PENDING MARTIN'S REVIEW: successive scale-up stages once
    # LIVE_VALIDATED, selected via TrackValidationRecord.supporting_metrics
    # ["live_validated_stage_index"] (see TrackValidationRecord docstring).
    live_validated_scale_stages: tuple[float, ...] = (0.25, 0.50, 1.00)

    def to_dict(self) -> dict[str, Any]:
        return {
            "paper_validated_min_windows": self.paper_validated_min_windows,
            "paper_validated_min_profit_factor": self.paper_validated_min_profit_factor,
            "paper_validated_max_p_value": self.paper_validated_max_p_value,
            "paper_pilot_allocation_pct": self.paper_pilot_allocation_pct,
            "live_validated_scale_stages": tuple(self.live_validated_scale_stages),
        }


# ------------------------------------------------------------------------
# Classification.
# ------------------------------------------------------------------------

def classify_track_validation_state(window_results: list[dict[str, Any]], *, config: ValidationThrottleConfig) -> str:
    """DERIVED. `window_results`: one dict per non-overlapping
    out-of-sample test window, each `{"profit_factor": float, "p_value":
    float, "n_trades": int}`. Distinguishes only `UNVALIDATED` vs
    `PAPER_VALIDATED` from backtest-style window results -- promoting to
    `LIVE_VALIDATED` requires real executed-trade evidence, a separate,
    caller-supplied decision (see `classify_live_validation()` below),
    never computed here. Returns `PAPER_VALIDATED` iff there are at least
    `paper_validated_min_windows` windows AND every one of them clears
    both the profit-factor and p-value bars; else `UNVALIDATED`."""
    if len(window_results) < config.paper_validated_min_windows:
        return "UNVALIDATED"
    for window in window_results:
        pf = window.get("profit_factor")
        pv = window.get("p_value")
        if pf is None or pv is None:
            return "UNVALIDATED"
        if not (pf > config.paper_validated_min_profit_factor and pv < config.paper_validated_max_p_value):
            return "UNVALIDATED"
    return "PAPER_VALIDATED"


def classify_live_validation(paper_pilot_results: dict[str, Any], *, config: ValidationThrottleConfig) -> bool:
    """DERIVED. `paper_pilot_results`: a dict of REAL, executed-trade
    pilot results (same `profit_factor`/`p_value` shape as one window
    above, but from live fills, not a backtest). Uses the SAME bar as
    `classify_track_validation_state()` -- this function only decides
    whether that bar is cleared; the caller is responsible for actually
    transitioning a `TrackValidationRecord.state` to `LIVE_VALIDATED`
    once this returns `True` (this module performs no mutation)."""
    pf = paper_pilot_results.get("profit_factor")
    pv = paper_pilot_results.get("p_value")
    if pf is None or pv is None:
        return False
    return bool(pf > config.paper_validated_min_profit_factor and pv < config.paper_validated_max_p_value)


def compute_capital_allocation_multiplier(
    state: str, *, config: ValidationThrottleConfig, live_validated_stage_index: int = 0,
) -> float:
    """DERIVED. The sizing multiplier this validation state currently
    implies: `0.0` for `UNVALIDATED` (the de facto block -- see module
    docstring), `paper_pilot_allocation_pct` for `PAPER_VALIDATED`, and
    `live_validated_scale_stages[min(live_validated_stage_index, len(
    stages) - 1)]` for `LIVE_VALIDATED` (a negative index is clamped to
    0, an index past the end is clamped to the last/largest stage,
    rather than raising -- a caller progressing through stages should
    never be blocked by an off-by-one)."""
    if state == "UNVALIDATED":
        return 0.0
    if state == "PAPER_VALIDATED":
        return config.paper_pilot_allocation_pct
    if state == "LIVE_VALIDATED":
        stages = config.live_validated_scale_stages
        idx = max(0, min(live_validated_stage_index, len(stages) - 1))
        return stages[idx]
    raise ValueError(f"unknown validation state: {state!r} (expected one of {sorted(VALIDATION_STATES)})")


def evaluate_validation_throttle_dimension(
    track_records: dict[str, TrackValidationRecord], config: ValidationThrottleConfig,
) -> tuple[DimensionVerdict, ...]:
    """EFFECTIVE (but never a veto -- see module docstring). One
    `DimensionVerdict` per track (dimension="capital_allocation_throttle",
    the track id lives in `evidence["track_id"]`), sorted by track id for
    determinism. `verdict` is ALWAYS `PASS` -- this dimension sizes, it
    never blocks on its own. `evidence` carries `state` and the computed
    `allocation_multiplier` for a downstream position-sizing step to
    read."""
    out: list[DimensionVerdict] = []
    for track_id in sorted(track_records):
        record = track_records[track_id]
        stage_index = 0
        metrics = record.supporting_metrics or {}
        if isinstance(metrics, dict):
            raw_stage = metrics.get("live_validated_stage_index", 0)
            if isinstance(raw_stage, int) and not isinstance(raw_stage, bool):
                stage_index = raw_stage
        multiplier = compute_capital_allocation_multiplier(
            record.state, config=config, live_validated_stage_index=stage_index,
        )
        out.append(DimensionVerdict(
            dimension="capital_allocation_throttle", venue=None, verdict=PASS,
            reason="SIZING_ONLY_NEVER_BLOCKS",
            evidence={
                "track_id": track_id, "state": record.state, "allocation_multiplier": multiplier,
                "as_of": record.as_of, "live_validated_stage_index": stage_index,
            },
        ))
    return tuple(out)
