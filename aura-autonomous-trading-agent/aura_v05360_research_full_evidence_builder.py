#!/usr/bin/env python3
"""
AURA v0.5.3.60 -- Research-track full-evidence builder.

Wires `.46` (news), `.47` (sentiment), `.48` (Elliott Wave), and `.359`
(sector rotation) together into one `.350` `CandidateEvidence` per
symbol -- the first place in this codebase all four of these previously-
disconnected evidence sources are actually combined for a real decision.
Built per Martin's explicit 2026-09-24 authorization to wire these into
the decision path, following his own confirmed defaults where he
expressed no preference (see "Scope" below).

SCOPE, EXPLICITLY: RESEARCH TRACK ONLY, NOT THE LIVE CHAIN
------------------------------------------------------------------------
This module does NOT modify, import, or get imported by `.356`
(stage3_live_equity_cli.py), `.357` (stage3_scheduled_runner.py), or
`aura_v054_signal_source.py`'s `AuraFrozenDecisionEngineSignalSource`.
Those remain exactly as before -- still frozen, still technical-only,
still proven to always ABSTAIN. This was Martin's own choice when
scoping this build (`AskUserQuestion`, 2026-09-24: on validation path,
"no preference" -> defaulted to "validate in the research/backtest
track first, promote via `.339` only after evidence clears," matching
how `.51`/`.52` themselves were built and tested before ever reaching
`.356`; on weights, "no preference" -> defaulted to keeping
`technical_weight`/`short_technical_weight` frozen at `0.0` so this
change is isolated rather than compounding two behavioral changes at
once). Promoting this evidence into the live chain is a SEPARATE, future
decision requiring the Strategy Registry's (`.339`) nine evidence
categories to actually clear first -- none of that validation has been
done. Running this module produces `TradingDecision`s for research/
backtesting use, never a live order.

WHAT THIS MODULE DOES NOT DO
------------------------------------------------------------------------
  - Does not fetch data itself. Bars, news-ledger events, and the
    sector-rotation universe's bars are all caller-supplied -- mirroring
    every other module in this chain's "pure function over caller-
    supplied inputs" discipline.
  - Does not pick weights. `sentiment_weight`, `wave_weight`, and
    `sector_rotation_weight` are the caller's own research parameters
    (see `.339`'s evidence-gate requirements for what's needed before any
    weight choice could be promoted); this module never defaults or
    suggests one.
  - Does not validate AI output, because there isn't any at this seam:
    `.46`/`.47`/`.48` are AURA's own deterministic, non-AI modules, so
    there is no untyped LLM-output boundary here of the kind the DELTAX
    port-scoping research (behind this same build) found in DELTAX_v2's
    `direction_router.py` (a `NewsAnalysis.direction`/`.confidence`
    reaching a routing decision with no type/range check at that exact
    seam). As a direct, deliberate response to that finding anyway,
    `build_full_evidence_for_symbol` below still re-validates the shape
    of whatever `.47`/`.359` hand it immediately before constructing
    `.350` evidence (see `_validate_regime_shape`) -- enforcing this
    project's own Decision-Seam Validation Rule at the actual seam, not
    one hop upstream, as a matter of discipline even where today's
    specific defect class doesn't apply yet.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import aura_v05347_market_sentiment_scoring as SENTIMENT
import aura_v05348_elliott_wave_research as WAVE
import aura_v05350_decision_engine as ENGINE
import aura_v05359_sector_rotation as ROTATION

VERSION = "AURA v0.5.3.60"


class ResearchEvidenceError(Exception):
    """Raised for programmer-error / malformed-evidence-shape violations
    only -- never for a symbol simply lacking data (that's NOT_USABLE, an
    expected, handled outcome recorded on the evidence, not an
    exception).
    """


def _validate_regime_shape(name: str, regime: Any, *, score_attr: str, score_range: tuple[float, float]) -> None:
    """Decision-Seam Validation Rule, applied at the exact point evidence
    from `.47`/`.359` is about to be handed to `.350`. Confirms the
    scoring attribute this module is about to read is present and,
    numeric and within range -- rather than trusting the upstream
    module's own internal invariants to have held all the way through.
    """
    if regime is None:
        return
    value = getattr(regime, score_attr, None)
    if value is None:
        return  # NOT_USABLE is a legitimate, expected state -- .350 itself gates on this.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResearchEvidenceError(f"INVALID_{name}_SCORE_TYPE:{score_attr}={value!r} is not numeric")
    if not (score_range[0] <= float(value) <= score_range[1]):
        raise ResearchEvidenceError(f"INVALID_{name}_SCORE_RANGE:{score_attr}={value!r} outside {score_range}")


@dataclass(frozen=True)
class SectorRotationInputs:
    """Bundles `.359`'s many required inputs so callers building evidence
    for a batch of symbols can supply the shared universe/benchmark data
    once. Every field is required -- see `.359`'s own module docstring
    for why none of this is defaulted by this project's convention."""

    symbol_bars: list[dict[str, Any]]
    benchmark_symbol: str
    benchmark_bars: list[dict[str, Any]]
    universe_bars: dict[str, list[dict[str, Any]]]
    safe_haven_symbols: tuple[str, ...]
    safe_haven_bars: dict[str, list[dict[str, Any]]]
    defensive_ratio_pair: tuple[str, str] | None
    defensive_ratio_bars: tuple[list[dict[str, Any]], list[dict[str, Any]]] | None
    lookback_bars: int
    top_n: int
    defensive_ma_period: int
    score_scale: float


@dataclass(frozen=True)
class FullEvidenceBuildResult:
    symbol: str
    evidence: Any  # `.350` CandidateEvidence
    news_events_considered: int
    sentiment_regime: Any | None
    wave_result: Any | None
    sector_rotation_regime: Any | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "evidence_summary": self.evidence.evidence_summary,
            "candidate_id": self.evidence.candidate_id,
            "sources_present": list(self.evidence.sources_present),
            "news_events_considered": self.news_events_considered,
            "sentiment": self.sentiment_regime.to_dict() if self.sentiment_regime is not None else None,
            "wave": self.wave_result.to_dict() if self.wave_result is not None else None,
            "sector_rotation": self.sector_rotation_regime.to_dict() if self.sector_rotation_regime is not None else None,
        }


def build_full_evidence_for_symbol(
    symbol: str,
    bars: list[dict[str, Any]],
    news_events: list[dict[str, Any]],
    *,
    reversal_pct: float,
    decay_window_hours: float,
    min_source_count: int,
    technical_regime: Any | None = None,
    short_technical_regime: Any | None = None,
    sector_rotation_inputs: SectorRotationInputs | None = None,
    now: datetime | None = None,
) -> FullEvidenceBuildResult:
    """Build one `.350` `CandidateEvidence` for `symbol`, combining `.47`
    sentiment (scored from `news_events`, in `.46`'s own ledger-event
    shape), `.48` Elliott Wave (computed from `bars`), and -- when
    `sector_rotation_inputs` is supplied -- `.359` sector rotation.
    `technical_regime`/`short_technical_regime` pass straight through to
    `.350` unchanged (this module adds no opinion about them; supply
    `.51`/`.52`'s own output directly if the caller wants a like-for-like
    comparison against the frozen live configuration).

    `reversal_pct`, `decay_window_hours`, and `min_source_count` are
    required, no-default research parameters -- forwarded verbatim to
    `.47`/`.48`, never invented here.
    """
    sentiment_regime = SENTIMENT.score_symbol_sentiment(
        news_events, symbol, decay_window_hours=decay_window_hours, min_source_count=min_source_count, now=now
    )
    _validate_regime_shape("SENTIMENT", sentiment_regime, score_attr="promotable_score", score_range=(-1.0, 1.0))

    wave_result = WAVE.analyze_elliott_wave(bars, symbol, reversal_pct=reversal_pct, now=now) if bars else None

    sector_rotation_regime = None
    if sector_rotation_inputs is not None:
        sri = sector_rotation_inputs
        sector_rotation_regime = ROTATION.analyze_sector_rotation(
            symbol,
            sri.symbol_bars,
            benchmark_symbol=sri.benchmark_symbol,
            benchmark_bars=sri.benchmark_bars,
            universe_bars=sri.universe_bars,
            safe_haven_symbols=sri.safe_haven_symbols,
            safe_haven_bars=sri.safe_haven_bars,
            defensive_ratio_pair=sri.defensive_ratio_pair,
            defensive_ratio_bars=sri.defensive_ratio_bars,
            lookback_bars=sri.lookback_bars,
            top_n=sri.top_n,
            defensive_ma_period=sri.defensive_ma_period,
            score_scale=sri.score_scale,
            now=now,
        )
        _validate_regime_shape("SECTOR_ROTATION", sector_rotation_regime, score_attr="rotation_score", score_range=(-1.0, 1.0))

    news_count = sum(1 for e in news_events if symbol in (e.get("symbols") or []))

    evidence = ENGINE.build_candidate_evidence(
        symbol,
        sentiment_regime=sentiment_regime,
        wave_result=wave_result,
        technical_regime=technical_regime,
        short_technical_regime=short_technical_regime,
        sector_rotation_regime=sector_rotation_regime,
        news_item_count=news_count,
        now=now,
    )

    return FullEvidenceBuildResult(
        symbol=symbol,
        evidence=evidence,
        news_events_considered=news_count,
        sentiment_regime=sentiment_regime,
        wave_result=wave_result,
        sector_rotation_regime=sector_rotation_regime,
    )
