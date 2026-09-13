#!/usr/bin/env python3
"""
AURA v0.5.3.50 — Formal Decision Engine + deterministic, LLM-free critic.

WHY THIS MILESTONE IS ARCHITECTURALLY THE MOST IMPORTANT ONE SO FAR
(Martin's explicit framing, 2026-09-13): `.49` proved AURA can safely ASK
an AI for an opinion. `.50` proves AURA can make a trading DECISION
without trusting that opinion. This module is the deterministic authority
that sits between `.49` (AI proposes/challenges) and `.33`-`.38` (the
already-built, deterministic Risk/Execution spine): AI proposes/challenges
-> `.50` deterministically DECIDES -> `.33`-`.38` deterministically
enforce. Nothing in this module delegates the final decision to an LLM,
and nothing an LLM produces (a `.49` `AIProposal`'s `stated_confidence`,
or a `.49` `Critique`) can ever increase this module's output score or
flip its decided direction — see "Sequential penalty-only ranking" below.

What `.50` actually is (original, pre-renumbering description, Master
Roadmap v0.5.5 old `.49`): "Formal Decision Engine (arbitrates
deterministic/AI/news/sentiment/Elliott Wave proposals into one output),
incorporating a deterministic, LLM-free critic component between Research
output and this arbitration step. Unifies all proposal sources." Per the
Final Baseline §6 acceptance criterion 3 (partially satisfied by `.49`,
completed here): "The deterministic, LLM-free critic planned for `.50`
and the AI-vs-AI adversarial-challenge role in `.49` are both present and
distinct — a regression test should confirm neither can be silently
substituted for the other."

Level 1/2/3 placement: `.46`/`.47`/`.48` are Level 1 observations, `.49`'s
`AIProposal`/`Critique` are Level 1/2 research evidence. `.50` is
explicitly Level 3 — a Trading Decision. Per Martin's own instruction this
session, `.50`'s decision is NOT itself a `CanonicalExecutionIntent`
(`.33`) and never carries qty/price/order_type/broker fields — sizing and
final order construction remain downstream, deterministic, and outside
this module's scope. `.50` decides DIRECTION and WHETHER to act on a
candidate; `.33`-`.38` (unchanged, not modified here) remain the only
path to an actual order.

Reuse-first audit (performed before writing any code, via a dedicated
audit subagent)
------------------------------------------------------------------------
  - Hermes Trader `reasoning/critic.py`/`reasoning/calibration.py` (read
    in full): the closest analog to a "deterministic, LLM-free critic"
    anywhere in the searched corpus, and the source explicitly named in
    this project's own Master Roadmap. Classified **C**, not reused
    directly: every threshold in both files (win-rate buckets, adjustment
    magnitudes, confidence caps) is a hardcoded, invented constant with no
    derivation shown — directly conflicting with this project's "never
    invent numbers" discipline (`.44`/`.47`/`.48`/`.49`). It also depends
    on a live realized-outcomes SQLite table (a `predictions` table with
    a `was_correct` column) that does not exist anywhere in AURA today —
    see "Martin's scoping decision on the critic's data source" below for
    how this gap was resolved. What WAS reused (**B**, shape only): a
    critic that reasons over structured historical/corroboration data,
    produces an issues list plus a derived pass/fail verdict, and sits
    between research output and a downstream decision step, never itself
    calling an external model.
  - `.39` (Strategy Registry) `SOURCE_KINDS = {"DETERMINISTIC_RESEARCH",
    "AI_PROPOSED", "HUMAN_PROPOSED"}` and `.33` (Canonical Execution
    Specification) `SOURCE_KINDS = {"DETERMINISTIC_SIGNAL", "AI_PROPOSAL",
    "HUMAN_OVERRIDE"}` are two INCOMPATIBLE three-way vocabularies already
    live in this codebase. Per Martin's explicit choice, `.50` adopts
    `.33`'s vocabulary (the execution-facing one, already validated
    downstream by `.38`'s supervisor test) — see "Source-kind" below.
  - `.44` (Portfolio Exposure Enforcement) `_check_snapshot_freshness`:
    reused in shape (**A**, this project's own established pattern, not a
    competitor's) for `check_evidence_freshness` below — required,
    no-default `max_evidence_age_seconds`, BLOCK on stale OR
    future-dated timestamps.
  - Competitor sweep (AlphaPilot, DELTAX v2, CAURA, BABIL, optionwright,
    every other repo under `/home/claude/work/competitors/`): no genuine
    multi-source arbitration engine found anywhere. AlphaPilot's
    `decision_engine.py` was found and explicitly NOT adopted — it fuses
    decision-making with qty/strike/stop-loss/price generation in one
    step (exactly the anti-pattern this milestone must avoid) and
    tie-breaks candidates with a bare `list.sort()` and no documented
    rationale. `.50` is genuine greenfield here, confirming the Master
    Roadmap's own framing that this is a novel AURA capability, not
    something to import.
  - `.46`/`.47`/`.48`'s output contracts were read in full to build the
    real candidate-shortlist integration below (`build_candidate_
    evidence`) — no cross-module code in this repo combined them before
    `.50`; `.49`'s own docstring explicitly deferred this wiring to
    "most likely `.50`."

Martin's scoping decisions (`AskUserQuestion`, 2026-09-13) and how each
was implemented
------------------------------------------------------------------------
  1. **Source-kind: adopt `.33`'s vocabulary.** `.50` dynamically loads
     `.33` (read-only, calls nothing that mutates anything) purely to
     read its `SOURCE_KINDS` constant directly, rather than copy-pasting
     a value that could silently drift from the real one `.38` already
     validates against. Every `TradingDecision.source_kind` this module
     produces is `"DETERMINISTIC_SIGNAL"` — see "Why source_kind is
     always DETERMINISTIC_SIGNAL" below.
  2. **Critic's data source: check structured data already available
     now, not a new realized-outcomes store.** `run_deterministic_critic`
     checks cross-source directional agreement between `.47`'s sentiment
     and `.48`'s (single-valid-candidate) wave direction, `.47`'s own
     `corroboration_status`, and `.48`'s `ambiguity_status` — all
     already-computed, already-typed fields from `.46`/`.47`/`.48`. It
     does NOT check historical win rate; that remains a disclosed
     limitation (see "Known limitations"), matching Martin's explicit
     choice not to expand this milestone into building a new persisted
     store.
  3. **Ranking: sequential penalty-only chain.** The deterministic base
     score (`compute_base_rank_score`, built purely from `.47`/`.48`
     evidence) is the only thing that can make a candidate's score
     positive or negative. `.49`'s AI `stated_confidence` is NEVER added
     to any score anywhere in this module — it is carried through
     `TradingDecision` purely as informational/audit context. `.49`'s
     challenge penalty (via `.49`'s own `apply_challenge_penalty`,
     reused verbatim, not reimplemented) and `.50`'s own deterministic
     critic's penalty can only SUBTRACT from the base score. See
     "Direction is decided before penalties are applied" below for why
     this also means AI/critic penalties can demote a decision toward
     `NO_TRADE` but can never flip its direction.
  4. **Shortlist eligibility: at least one source with usable evidence,
     not all three required.** `is_shortlist_eligible` returns True if
     ANY of sentiment (SUFFICIENT corroboration), wave (a single valid
     candidate), or raw news presence is true. Missing sources are
     recorded (`sources_present`) but never penalized.

Direction is decided before penalties are applied
------------------------------------------------------------------------
`base_score_direction` computes LONG_LEANING / SHORT_LEANING /
NO_DIRECTIONAL_EVIDENCE from `base_rank_score` alone — a number built
exclusively from `.47`/`.48`'s deterministic evidence, computed BEFORE
any `.49` AI proposal is even generated. `.49`'s challenge penalty and
`.50`'s own critic penalty are then applied to produce `final_rank_score`,
which only ever controls whether a leaning clears `decision_threshold`
into an actual `DECIDE_LONG`/`DECIDE_SHORT`, or falls back to `NO_TRADE`.
A penalty can never turn a LONG-leaning candidate into a SHORT decision,
or vice versa — it can only weaken conviction toward no action. This is
the concrete, testable implementation of "AI proposes/challenges but
cannot author, size, or flip a trading decision."

Why `source_kind` is always `DETERMINISTIC_SIGNAL`
------------------------------------------------------------------------
`.33`'s `AI_PROPOSAL` tag exists for content that bypasses `.50`'s
arbitration entirely (analogous to `HUMAN_OVERRIDE`) and is asserted
directly at the execution-spec layer. Every `TradingDecision` produced
here is the output of THIS module's own deterministic code — `decide()`
contains no LLM call and no code path that returns early with an AI's
verdict standing in for its own. Even when a `.49` `AIProposal`/`Critique`
contributed evidence to the process, the decision itself was still made
deterministically by `.50`, so tagging it `AI_PROPOSAL` would misrepresent
who actually decided. A dedicated governance test locks this in.

Distinctness from `.49` (Final Baseline acceptance criterion 3, completed
here)
------------------------------------------------------------------------
`.49`'s `Critique` (AI-vs-AI, requires an `llm_client`, carries
`.concerns`) and this module's `DeterministicCritique` (LLM-free, no
`llm_client` parameter anywhere in `run_deterministic_critic`'s signature,
carries `.issues`) are deliberately different dataclasses with
deliberately different field names for their objection lists. This is not
cosmetic: `.49`'s `apply_challenge_penalty(score, critique, *,
penalty_per_concern=...)` reads `critique.concerns` — passing it a
`DeterministicCritique` raises `AttributeError` (it has no `.concerns`),
and this module's own critic-penalty step reads `.issues` — passing it a
`.49` `Critique` likewise raises `AttributeError` (it has no `.issues`).
Neither can be silently substituted for the other; both are proven unable
to by dedicated tests, not merely by convention.

Persistence
------------------------------------------------------------------------
None — every function here is a pure function of caller-supplied inputs
(typed evidence from `.46`/`.47`/`.48`, an injected `.49` module
reference and `LLMClient`, required research parameters) plus optional
`now`, mirroring `.47`/`.48`/`.49`'s own precedent. Every audit-relevant
fact (base score, final score, every issue/concern, every threshold used,
the exact candidate/proposal/critique identities involved) is carried on
the returned `TradingDecision`, including a `decision_hash` (mirrors
`.44`'s `EnforcementDecision.decision_hash` convention) binding it to its
own exact content. A future milestone is expected to persist these for a
durable audit trail; `.50` provides the complete in-memory record.

Known limitations (disclosed, not silently worked around)
------------------------------------------------------------------------
  - No historical win-rate / realized-outcome check (Martin's explicit
    scoping choice — see above). The deterministic critic checks
    cross-source agreement and each source's own quality flags, not
    track record. AURA has no persisted realized-outcomes store today;
    building one was explicitly out of scope for this milestone.
  - `.50` decides each candidate independently — it does NOT select a
    single "best" candidate among several competing symbols. This is a
    deliberate design choice, not an oversight: AURA's portfolio can hold
    multiple positions, and `.44` (Portfolio Exposure Enforcement)
    already exists as the portfolio-wide arbitration layer. Introducing a
    second, competing cross-candidate ranking mechanism here would
    duplicate `.44`'s responsibility and was judged out of `.50`'s
    literal contract ("arbitrates ... proposals into one output" is read
    here as "one output per candidate," not "one candidate portfolio-
    wide" — flagged explicitly in the completion report for Martin to
    correct if a different reading was intended).
  - `wave_weight`/`sentiment_weight`/`technical_weight`/
    `decision_threshold`/`ai_penalty_per_concern`/
    `critic_penalty_per_issue`/`max_evidence_age_seconds` are all
    REQUIRED, no-default research parameters (mirroring `.47`'s
    `decay_window_hours` convention) — this module provides the
    mechanism, never the specific values a real trading decision would
    use.

Addendum (`.51`, 2026-09-13) — small additive extension
------------------------------------------------------------------------
`.51` ("Live Alpaca equities/ETFs technical signal source") added a
THIRD, independent evidence dimension alongside sentiment (`.47`) and
Elliott Wave (`.48`): `technical_regime` (a `.51` `TechnicalRegime`
instance, duck-typed exactly like the other two). This was a small,
additive change, not a redesign: `CandidateEvidence` gained
`technical_regime`/`technical_usable` fields, `build_candidate_evidence`
gained one new optional (default `None`) parameter, and
`compute_base_rank_score`/`decide` gained one new REQUIRED (no default)
`technical_weight` parameter and one new score term — added only when
`technical_usable`, and only ever ADDED (never subtracted, since `.51`
is scoped LONG-only). Every existing caller that predates `.51` and
does not pass `technical_regime`/`technical_weight` continues to behave
functionally identically to before this addendum (all 51 of `.50`'s
own prior tests pass unmodified in behavior — only the new required
`technical_weight` argument was added to each existing call site). See
`aura_v05351_live_alpaca_equity_signal_source.py`'s own docstring for
the full detail of this extension and its reuse-first audit.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VERSION = "AURA v0.5.3.50"
ENGINE = "DECISION_ENGINE"
SCHEMA_VERSION = "1.0"

ROOT = Path(__file__).resolve().parent

DIRECTIONS = frozenset({"LONG_LEANING", "SHORT_LEANING", "NO_DIRECTIONAL_EVIDENCE"})
OUTCOMES = frozenset({"DECIDE_LONG", "DECIDE_SHORT", "NO_TRADE", "ABSTAIN"})


class DecisionEngineError(Exception):
    """Raised for programmer-error / required-parameter violations only —
    never for a candidate simply lacking evidence or failing the critic
    (those are expected, handled outcomes: ABSTAIN / NO_TRADE / a
    DeterministicCritique with issues), mirroring `.47`'s/`.49`'s own
    error-class discipline.
    """


# ============================================================================
# Stable JSON / hashing helpers -- copied, not imported, per this repo's
# established convention (see `.33`/`.40`/`.44`/`.45`/`.49`).
# ============================================================================


def stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# ============================================================================
# Dynamic imports -- same `_load_module` pattern established by `.38`
# (which itself cites `.30`/`.31`/`.35`/`.36`/`.37`). None of `.33`,
# `.46`, `.47`, `.48`, `.49` is modified; each is used strictly through
# its existing public functions/constants.
# ============================================================================


def _load_module(module_name: str, filename: str):
    try:
        return __import__(module_name)
    except ImportError:
        import importlib.util

        spec = importlib.util.spec_from_file_location(module_name, ROOT / filename)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod


def load_canonical_spec_module():
    return _load_module("aura_v05333_canonical_execution_specification", "aura_v05333_canonical_execution_specification.py")


def load_proposal_module():
    return _load_module("aura_v05349_ai_proposal_pipeline", "aura_v05349_ai_proposal_pipeline.py")


def source_kinds() -> frozenset[str]:
    """`.50`'s adopted source-kind vocabulary — read directly from `.33`
    at call time, never hardcoded, so it cannot silently drift from the
    vocabulary `.38` already validates downstream.
    """
    return frozenset(load_canonical_spec_module().SOURCE_KINDS)


# ============================================================================
# Candidate evidence — `.50`'s own structured integration of `.46`/`.47`/
# `.48`'s outputs. Built here because no prior module combines them (see
# reuse-scan above); `.46`/`.47`/`.48` are read-only inputs, never
# modified.
# ============================================================================


@dataclass(frozen=True, slots=True)
class CandidateEvidence:
    candidate_id: str
    symbol: str
    as_of: str
    sentiment_regime: Any | None  # `.47` SentimentRegime instance, duck-typed
    wave_result: Any | None  # `.48` ElliottWaveResearchResult instance, duck-typed
    technical_regime: Any | None  # `.51` TechnicalRegime instance, duck-typed -- added by `.51`, additive only (see module docstring addendum below `.50`'s own docstring, and `.51`'s own docstring)
    sentiment_usable: bool
    wave_usable: bool
    technical_usable: bool  # added by `.51`
    news_item_count: int
    sources_present: tuple[str, ...]
    evidence_summary: str


def _single_valid_wave_direction(wave_result: Any) -> str | None:
    """The direction of the sole VALID_CANDIDATE, when `.48`'s
    `ambiguity_status` is exactly `SINGLE_VALID_CANDIDATE`. Defensive:
    re-derives from `impulse_candidates` rather than trusting the status
    string alone, and returns `None` (never guesses) if the count doesn't
    actually match what the status claims.
    """
    valid = [c for c in getattr(wave_result, "impulse_candidates", ()) if getattr(c, "validity", None) == "VALID_CANDIDATE"]
    if len(valid) != 1:
        return None
    return getattr(valid[0], "wave_direction", None)


def _render_evidence_summary(
    symbol: str,
    sentiment_regime: Any | None,
    wave_result: Any | None,
    news_item_count: int,
    sentiment_usable: bool,
    wave_usable: bool,
    technical_regime: Any | None = None,
    technical_usable: bool = False,
) -> str:
    """Human/AI-readable evidence text -- becomes `.49` `Candidate.
    evidence_summary` verbatim. Deterministically built from the same
    typed fields the rest of this module reasons over, not free-form.
    """
    parts = [f"symbol={symbol}"]
    if sentiment_regime is not None:
        if sentiment_usable:
            parts.append(
                f"sentiment: promotable_score={getattr(sentiment_regime, 'promotable_score', None)} "
                f"(source_count={getattr(sentiment_regime, 'source_count', None)}, "
                f"corroboration={getattr(sentiment_regime, 'corroboration_status', None)})"
            )
        else:
            parts.append(f"sentiment: NOT usable (corroboration={getattr(sentiment_regime, 'corroboration_status', None)})")
    else:
        parts.append("sentiment: no data")
    if wave_result is not None:
        direction = _single_valid_wave_direction(wave_result) if wave_usable else None
        if wave_usable:
            parts.append(f"elliott_wave: single valid candidate, direction={direction}")
        else:
            parts.append(f"elliott_wave: NOT usable (ambiguity_status={getattr(wave_result, 'ambiguity_status', None)})")
    else:
        parts.append("elliott_wave: no data")
    if technical_regime is not None:
        if technical_usable:
            parts.append(
                f"technical: status={getattr(technical_regime, 'status', None)} "
                f"signal_score={getattr(technical_regime, 'signal_score', None)}"
            )
        else:
            parts.append(f"technical: NOT usable (status={getattr(technical_regime, 'status', None)})")
    else:
        parts.append("technical: no data")
    parts.append(f"news_item_count={news_item_count}")
    return "; ".join(parts)


def build_candidate_evidence(
    symbol: str,
    *,
    sentiment_regime: Any | None = None,
    wave_result: Any | None = None,
    technical_regime: Any | None = None,
    news_item_count: int = 0,
    now: datetime | None = None,
) -> CandidateEvidence:
    """Build `.50`'s structured evidence record for one symbol from
    `.46`/`.47`/`.48`'s typed outputs, plus `.51`'s `technical_regime`
    (added by `.51`, additive only -- defaults to `None` so every caller
    that predates `.51` is unaffected). A source counts as "usable" only
    when it passes its OWN internal quality bar — `.47`'s
    `promotable_score is not None` (i.e. `corroboration_status ==
    SUFFICIENT`), `.48`'s `ambiguity_status == SINGLE_VALID_CANDIDATE`,
    and `.51`'s `status in {"CONFIRMING", "CONFIRMED"}`
    — never merely "present". Sources below their own bar are still
    recorded (visible for audit, flagged by the deterministic critic) but
    contribute nothing to the base rank score, exactly mirroring `.48`'s
    own "ambiguity is preserved, never collapsed" discipline one layer up.
    """
    as_of = _now_iso(now)
    sentiment_usable = sentiment_regime is not None and getattr(sentiment_regime, "promotable_score", None) is not None
    wave_usable = wave_result is not None and getattr(wave_result, "ambiguity_status", None) == "SINGLE_VALID_CANDIDATE"
    technical_usable = technical_regime is not None and getattr(technical_regime, "status", None) in ("CONFIRMING", "CONFIRMED")
    sources_present = tuple(
        name
        for name, present in (
            ("SENTIMENT", sentiment_regime is not None),
            ("ELLIOTT_WAVE", wave_result is not None),
            ("TECHNICAL", technical_regime is not None),
            ("NEWS", news_item_count > 0),
        )
        if present
    )
    fingerprint = sha256_text(
        stable_json(
            {
                "symbol": symbol,
                "as_of": as_of,
                "sentiment_as_of": getattr(sentiment_regime, "as_of", None),
                "wave_as_of": getattr(wave_result, "as_of", None),
                "technical_as_of": getattr(technical_regime, "as_of", None),
                "news_item_count": news_item_count,
            }
        )
    )[:16]
    return CandidateEvidence(
        candidate_id=f"{symbol}:{fingerprint}",
        symbol=symbol,
        as_of=as_of,
        sentiment_regime=sentiment_regime,
        wave_result=wave_result,
        technical_regime=technical_regime,
        sentiment_usable=sentiment_usable,
        wave_usable=wave_usable,
        technical_usable=technical_usable,
        news_item_count=news_item_count,
        sources_present=sources_present,
        evidence_summary=_render_evidence_summary(
            symbol, sentiment_regime, wave_result, news_item_count, sentiment_usable, wave_usable,
            technical_regime=technical_regime, technical_usable=technical_usable,
        ),
    )


def is_shortlist_eligible(evidence: CandidateEvidence) -> bool:
    """Martin's explicit scoping decision: at least one source with
    USABLE evidence, not all three (now four, since `.51`) required. Raw
    news presence counts as usable on its own (`.46` has no analogous
    internal quality gate to check against).
    """
    return evidence.sentiment_usable or evidence.wave_usable or evidence.technical_usable or evidence.news_item_count > 0


# ============================================================================
# Evidence freshness -- reuses `.44`'s `_check_snapshot_freshness` shape
# verbatim (required, no-default max age; BLOCK on stale OR future-dated).
# ============================================================================


@dataclass(frozen=True, slots=True)
class FreshnessCheck:
    is_fresh: bool
    reason: str | None


def check_evidence_freshness(
    evidence: CandidateEvidence,
    *,
    max_evidence_age_seconds: float,
    now: datetime | None = None,
) -> FreshnessCheck:
    if max_evidence_age_seconds <= 0:
        raise DecisionEngineError("INVALID_MAX_EVIDENCE_AGE_SECONDS:must be > 0")
    now_dt = now or datetime.now(timezone.utc)

    ages: list[float] = []
    if evidence.sentiment_usable:
        ages.append((now_dt - _parse_iso(evidence.sentiment_regime.as_of)).total_seconds())
    if evidence.wave_usable:
        ages.append((now_dt - _parse_iso(evidence.wave_result.as_of)).total_seconds())
    if evidence.technical_usable:
        ages.append((now_dt - _parse_iso(evidence.technical_regime.as_of)).total_seconds())

    if not ages:
        # Only raw news (or nothing usable at all -- caught separately by
        # is_shortlist_eligible). No usable, timestamped source to check
        # staleness against at this layer -- a pass-through, not a
        # silent trust: `.46` NewsItem-level recency is `.47`'s own
        # concern (decay_window_hours), already enforced upstream.
        return FreshnessCheck(True, None)

    if any(age < 0 for age in ages):
        return FreshnessCheck(False, "evidence timestamp is future-dated")
    if all(age > max_evidence_age_seconds for age in ages):
        return FreshnessCheck(False, f"all usable evidence stale (max age {max_evidence_age_seconds}s)")
    return FreshnessCheck(True, None)


def build_shortlist(
    symbol_evidence: tuple[CandidateEvidence, ...],
    *,
    max_evidence_age_seconds: float,
    now: datetime | None = None,
) -> tuple[CandidateEvidence, ...]:
    """Filter to shortlist-eligible, fresh evidence. Ineligible or stale
    candidates are dropped from the RETURNED shortlist but nothing about
    them is destroyed by this function -- the caller retains the original
    `symbol_evidence` tuple for audit if it wants to record why a symbol
    never reached the shortlist.
    """
    eligible = []
    for evidence in symbol_evidence:
        if not is_shortlist_eligible(evidence):
            continue
        fresh = check_evidence_freshness(evidence, max_evidence_age_seconds=max_evidence_age_seconds, now=now)
        if not fresh.is_fresh:
            continue
        eligible.append(evidence)
    return tuple(eligible)


# ============================================================================
# Deterministic base rank score -- built PURELY from `.47`/`.48` evidence.
# This, and only this, determines decided DIRECTION (see module docstring,
# "Direction is decided before penalties are applied").
# ============================================================================


def compute_base_rank_score(
    evidence: CandidateEvidence, *, sentiment_weight: float, wave_weight: float, technical_weight: float
) -> float:
    """`technical_weight` was added by `.51` (additive extension to an
    already-frozen `.50` function -- see `.51`'s module docstring
    "`.50` integration changes"). It is REQUIRED, no default, matching
    this project's "never invent numbers" discipline for every other
    weight/penalty in this module. `.51` is scoped LONG-only this
    milestone, so the technical term is always ADDED when usable, never
    subtracted -- there is no bearish/short technical signal today.
    """
    if sentiment_weight < 0:
        raise DecisionEngineError("INVALID_SENTIMENT_WEIGHT:must be >= 0")
    if wave_weight < 0:
        raise DecisionEngineError("INVALID_WAVE_WEIGHT:must be >= 0")
    if technical_weight < 0:
        raise DecisionEngineError("INVALID_TECHNICAL_WEIGHT:must be >= 0")

    score = 0.0
    if evidence.sentiment_usable:
        score += sentiment_weight * evidence.sentiment_regime.promotable_score
    if evidence.wave_usable:
        direction = _single_valid_wave_direction(evidence.wave_result)
        if direction == "UP":
            score += wave_weight
        elif direction == "DOWN":
            score -= wave_weight
    if evidence.technical_usable:
        score += technical_weight * (evidence.technical_regime.signal_score / 100.0)
    return score


def base_score_direction(base_rank_score: float) -> str:
    if base_rank_score > 0:
        return "LONG_LEANING"
    if base_rank_score < 0:
        return "SHORT_LEANING"
    return "NO_DIRECTIONAL_EVIDENCE"


# ============================================================================
# Deterministic, LLM-free critic (Final Baseline acceptance criterion 3's
# `.50` half). NO `llm_client` parameter anywhere in this function's
# signature -- structurally, not just by convention, this cannot make a
# network call.
# ============================================================================


@dataclass(frozen=True, slots=True)
class DeterministicCritique:
    passed: bool  # derived from `issues` being empty -- same shape as `.49`'s Critique.passed, deliberately different field name (`issues`, not `concerns`) so the two types cannot be accidentally duck-typed into each other's consumer function.
    issues: tuple[str, ...]
    reviewed_at: str


def run_deterministic_critic(evidence: CandidateEvidence, *, now: datetime | None = None) -> DeterministicCritique:
    """Checks structured data already computed by `.47`/`.48` — cross-
    source directional agreement, each source's own corroboration/
    ambiguity flags. Does NOT check historical win rate (disclosed
    limitation, see module docstring — AURA has no realized-outcomes
    store to check against yet).
    """
    issues: list[str] = []

    if evidence.sentiment_usable and evidence.wave_usable:
        raw_score = evidence.sentiment_regime.promotable_score
        sentiment_dir = "UP" if raw_score > 0 else ("DOWN" if raw_score < 0 else None)
        wave_dir = _single_valid_wave_direction(evidence.wave_result)
        if sentiment_dir and wave_dir and sentiment_dir != wave_dir:
            issues.append(f"cross-source direction conflict: sentiment implies {sentiment_dir}, elliott wave implies {wave_dir}")

    if evidence.sentiment_regime is not None and getattr(evidence.sentiment_regime, "corroboration_status", None) == "INSUFFICIENT":
        issues.append("sentiment corroboration insufficient (below min_source_count)")

    if evidence.wave_result is not None:
        ambiguity = getattr(evidence.wave_result, "ambiguity_status", None)
        if ambiguity == "AMBIGUOUS_MULTIPLE_CANDIDATES":
            issues.append(f"elliott wave ambiguous: {getattr(evidence.wave_result, 'valid_candidate_count', '?')} independently valid candidates, none preferred")
        elif ambiguity == "ALL_CANDIDATES_INVALIDATED":
            issues.append("elliott wave: all candidates invalidated")
        elif ambiguity == "INSUFFICIENT_DATA":
            issues.append("elliott wave: insufficient data for any candidate")

    if not evidence.sources_present:
        # Defensive only -- build_shortlist should already exclude this
        # via is_shortlist_eligible before it ever reaches the critic.
        issues.append("no evidence sources present")

    return DeterministicCritique(passed=not issues, issues=tuple(issues), reviewed_at=_now_iso(now))


# ============================================================================
# Trading Decision -- Level 3 output. No qty/price/side/order_type/broker
# field exists anywhere on this type (locked in by a governance test,
# mirroring `.49`'s `AIProposal`/`Critique`).
# ============================================================================


@dataclass(frozen=True, slots=True)
class TradingDecision:
    candidate_id: str
    symbol: str
    source_kind: str  # always "DETERMINISTIC_SIGNAL" -- see module docstring
    direction: str  # one of DIRECTIONS, from compute_base_rank_score ONLY
    outcome: str  # one of OUTCOMES
    base_rank_score: float
    final_rank_score: float
    decision_threshold: float
    evidence_sources: tuple[str, ...]
    ai_proposal_status: str | None
    ai_stated_confidence: float | None  # informational only -- never read by any scoring function in this module
    ai_challenge_passed: bool | None
    ai_challenge_concern_count: int
    deterministic_critique_passed: bool
    deterministic_critique_issue_count: int
    reasons: tuple[str, ...]
    decided_at: str
    decision_hash: str

    def __post_init__(self) -> None:
        if self.direction not in DIRECTIONS:
            raise DecisionEngineError(f"INVALID_DIRECTION:{self.direction}")
        if self.outcome not in OUTCOMES:
            raise DecisionEngineError(f"INVALID_OUTCOME:{self.outcome}")


def _decision_hash(payload: dict[str, Any]) -> str:
    return sha256_text(stable_json(payload))


def decide(
    evidence: CandidateEvidence,
    *,
    sentiment_weight: float,
    wave_weight: float,
    technical_weight: float,
    decision_threshold: float,
    ai_penalty_per_concern: float,
    critic_penalty_per_issue: float,
    proposal_module: Any,
    llm_client: Any,
    now: datetime | None = None,
) -> TradingDecision:
    """The top-level orchestration function: candidate evidence -> base
    score -> `.49` AI proposal + adversarial challenge (skipped when
    there is no directional evidence at all -- see module docstring's
    disclosed efficiency choice) -> `.50`'s own deterministic critic ->
    final score -> decision. Every step is auditable on the returned
    `TradingDecision`.
    """
    if decision_threshold < 0:
        raise DecisionEngineError("INVALID_DECISION_THRESHOLD:must be >= 0")
    if ai_penalty_per_concern < 0:
        raise DecisionEngineError("INVALID_AI_PENALTY_PER_CONCERN:must be >= 0")
    if critic_penalty_per_issue < 0:
        raise DecisionEngineError("INVALID_CRITIC_PENALTY_PER_ISSUE:must be >= 0")

    decided_at = _now_iso(now)
    base = compute_base_rank_score(
        evidence, sentiment_weight=sentiment_weight, wave_weight=wave_weight, technical_weight=technical_weight
    )
    direction = base_score_direction(base)

    ai_proposal = None
    ai_critique = None
    if direction != "NO_DIRECTIONAL_EVIDENCE":
        candidate = proposal_module.Candidate(
            candidate_id=evidence.candidate_id, symbol=evidence.symbol, evidence_summary=evidence.evidence_summary
        )
        shortlist_hash = proposal_module.build_shortlist_hash((candidate,))
        ai_proposal = proposal_module.generate_proposal_for_candidate(
            candidate, shortlist_hash=shortlist_hash, llm_client=llm_client, now=now
        )
        if ai_proposal.status == "PROPOSED":
            ai_critique = proposal_module.challenge_proposal(ai_proposal, candidate, llm_client=llm_client, now=now)

    det_critique = run_deterministic_critic(evidence, now=now)

    final = base
    if ai_critique is not None:
        final = proposal_module.apply_challenge_penalty(final, ai_critique, penalty_per_concern=ai_penalty_per_concern)
    if not det_critique.passed:
        final -= critic_penalty_per_issue * len(det_critique.issues)

    if direction == "NO_DIRECTIONAL_EVIDENCE":
        outcome = "ABSTAIN"
    elif abs(final) < decision_threshold:
        outcome = "NO_TRADE"
    elif direction == "LONG_LEANING":
        outcome = "DECIDE_LONG"
    else:
        outcome = "DECIDE_SHORT"

    reasons = tuple(det_critique.issues) + (tuple(ai_critique.concerns) if ai_critique is not None else ())

    payload = {
        "candidate_id": evidence.candidate_id,
        "symbol": evidence.symbol,
        "direction": direction,
        "outcome": outcome,
        "base_rank_score": base,
        "final_rank_score": final,
        "decision_threshold": decision_threshold,
        "ai_proposal_status": ai_proposal.status if ai_proposal is not None else None,
        "ai_challenge_passed": ai_critique.passed if ai_critique is not None else None,
        "deterministic_critique_passed": det_critique.passed,
        "reasons": list(reasons),
        "decided_at": decided_at,
    }

    return TradingDecision(
        candidate_id=evidence.candidate_id,
        symbol=evidence.symbol,
        source_kind="DETERMINISTIC_SIGNAL",
        direction=direction,
        outcome=outcome,
        base_rank_score=base,
        final_rank_score=final,
        decision_threshold=decision_threshold,
        evidence_sources=evidence.sources_present,
        ai_proposal_status=ai_proposal.status if ai_proposal is not None else None,
        ai_stated_confidence=ai_proposal.stated_confidence if ai_proposal is not None else None,
        ai_challenge_passed=ai_critique.passed if ai_critique is not None else None,
        ai_challenge_concern_count=len(ai_critique.concerns) if ai_critique is not None else 0,
        deterministic_critique_passed=det_critique.passed,
        deterministic_critique_issue_count=len(det_critique.issues),
        reasons=reasons,
        decided_at=decided_at,
        decision_hash=_decision_hash(payload),
    )
