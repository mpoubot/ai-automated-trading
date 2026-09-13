#!/usr/bin/env python3
"""
AURA v0.5.3.48 — Elliott Wave Research (deterministic, structural, candidate-based).

RESEARCH/INTELLIGENCE ONLY. Produces structural price-pattern CANDIDATES
with explicit validity evidence, never a trade direction, size, or
execution instruction. When more than one candidate interpretation is
internally consistent with the same price history -- which, per real
Elliott Wave theory, is common and expected -- this module reports ALL of
them and an explicit ambiguity status, rather than picking a winner. Not
imported by, and does not import, any order/position/authorization module
anywhere in this repo. Has no path to an `ExecutionIntent`.

Why this exists (v0.5.5 Final Implementation Baseline, milestone `.48`,
"Elliott Wave")
------------------------------------------------------------------------
This is the third module in the "Market State / Research" track (`.46`
news, `.47` sentiment, `.48` price-structure). Per the canonical
architecture/governance doc's Level 1/2/3 model, Elliott Wave is named
explicitly as one of the five "alternative-intelligence domains" this
governs: Level 1 observations (confirmed swing points) -> Level 2 derived
structure (candidate wave counts with validity evidence) -> stops there.
Only a future Level 3 module (not this one) may ever turn a candidate into
an `ExecutionIntent`, and even then only via Signal Decision -> Risk Gate
-> Position State -> Authorization like every other proposal.

Reuse scan (Martin's explicit A/B/C/D framework, performed BEFORE writing
any new code)
------------------------------------------------------------------------
A dedicated audit (2026-09-13) searched the full `aura_v053xx` chain, all
three DELTAX repos, Hermes Trader, every other competitor repo under
`/home/claude/work/competitors/`, `mexc_bot/`, CAURA, BABIL, and
optionwright for Elliott Wave / swing / pivot / zigzag / fractal /
Fibonacci code. **Zero application code implementing any of this was
found anywhere in this workspace** (two DELTAX repos have only prose
trading-course notes mentioning Fibonacci levels as commentary, not code;
this project's own prior audits -- `AURA_Competition_GitHub_Deep_Dive` §3.6
and the canonical governance doc's §5 evidence table -- independently
confirm the same absence). `.48` is therefore a from-scratch build; there
was nothing to classify A/B or adapt.

The only concrete methodological spec found anywhere in the project store
is one reconciliation-matrix line item (`AURA_Source_Recovery_Roadmap_
Reconciliation_2026-09-11.md`, sourced from `Elliott_Wave_Trading_Bot_
Guide.docx`): "8-wave count, 3 invalidation rules (Wave 2 <=100% retrace,
Wave 3 never shortest, Wave 4/Wave 1 no overlap), EWO oscillator
(5/34-period MA spread on typical price)". No execution-primitive content
from that source was carried into this module -- per Martin's explicit
instruction, `.48` implements only the structural/research half of that
spec, never the "MEXC pymexc execution primitives" the same source
document apparently also contained.

Architectural precedent this module mirrors
------------------------------------------------------------------------
Same shape as `.47`: pure functions over caller-supplied input (here,
OHLCV bars, rather than `.47`'s news events), required no-default research
parameters for anything that is a genuine open research question (the
zigzag reversal threshold), no new persisted state (mirrors `.44`'s own
precedent: an observation layer persists, a derivation layer computes on
demand), and explicit non-goal governance tests locking the module out of
ever touching an order, position, or trade decision.

Scoping decisions (Martin, AskUserQuestion, 2026-09-13 -- reproduced here
so a future reader does not have to reconstruct them from chat history)
------------------------------------------------------------------------
  1. Swing detection: **zigzag with a required percentage-reversal
     threshold** (`reversal_pct`), not N-bar fractals. Real Elliott Wave
     counting is built on significant price reversals, not raw local
     extrema (which produce far too many trivial "swings" to represent
     actual waves between). `reversal_pct` has NO default anywhere in this
     module's call path -- it is a genuine open research parameter, not an
     AURA constant, matching `.44`/`.45`/`.47`'s "don't invent numbers"
     convention.
  2. Wave-count scope: **single-degree candidate counts only**. This
     module does NOT attempt nested/multi-degree wave counting (is Wave 3
     itself a 5-wave impulse at a smaller degree) -- that is a genuinely
     AI-hard, highly ambiguous problem with no agreed automated solution
     anywhere in the industry, explicitly out of scope per Martin's
     instruction that ambiguous interpretations must stay explicitly
     uncertain rather than being dressed up as a confident answer. Every
     6-consecutive-swing window in the detected swing sequence is
     evaluated independently against the 3 documented invalidation rules;
     overlapping windows can and often will both be internally valid --
     that is reported as genuine ambiguity (`ambiguity_status`), never
     collapsed to a single "the" wave count.

What is deterministic vs. what would be heuristic/AI-derived
------------------------------------------------------------------------
Every computation in this module is deterministic and reproducible: given
the same bars and the same `reversal_pct`, `analyze_elliott_wave` always
returns the identical result (pure function, no randomness, no external
call). There is no AI/LLM anywhere in this module -- selecting which of
several valid candidates is "more likely" correct, or synthesizing a
market-state label from a wave count, would require exactly the kind of
subjective judgment this module deliberately does not make. That judgment
is left to a future `.49`-generation AI proposal (with `.49`'s own typed
validation boundary), which can be compared against this module's
deterministic candidates -- the same "deterministic vs. AI-derived,
compared side by side" pattern already established between `.47` and the
future AI-sentiment work.

EWO (Elliott Wave Oscillator)
------------------------------------------------------------------------
Implemented exactly as named in the sourced spec: a 5-period vs. 34-period
simple moving average spread on **typical price** `(H+L+C)/3`. Unlike
`reversal_pct`, the 5/34 window pair is not a free research parameter --
it is the literal, standard definition of "the EWO" (the same way "RSI"
means the 14-period formula unless stated otherwise), so it is exposed
with the conventional 5/34 default while still remaining overridable.
EWO is attached to each impulse candidate as auxiliary confirming context
only -- it is never used to decide a candidate's VALID/INVALID status,
since the sourced spec lists it as a separate oscillator/confirmation
tool, not a 4th invalidation rule, and inventing a new rule here would be
exactly the "strategy design disguised as implementation" failure mode
already flagged for `.44`.

Corrective (A-B-C) legs
------------------------------------------------------------------------
The 3 pivots immediately following a candidate impulse's Wave 5 are
labeled structurally (A/B/C) when present, completing the "8-wave count"
shape named in the sourced spec. No invalidation rule is applied to this
leg -- none was found in any source document for the corrective portion
specifically, and inventing one would not be honest. This is disclosed on
every `CorrectiveCandidate`, not silently implied to be rule-validated.

Persistence
------------------------------------------------------------------------
None. `.48` introduces no new state file, mirroring `.44`'s and `.47`'s
own precedent -- it is a pure function of caller-supplied OHLCV bars plus
caller-supplied parameters.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import numpy as np


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ElliottWaveError(Exception):
    pass


# ------------------------------------------------------------------------
# Zigzag swing detection -- single-state-variable formulation (the
# standard, widely-implemented zigzag algorithm). `reversal_pct` is a
# REQUIRED parameter with no default (see module docstring, scoping
# decision #1).
# ------------------------------------------------------------------------

KIND_HIGH = "HIGH"
KIND_LOW = "LOW"


@dataclass(frozen=True)
class SwingPoint:
    index: int                # position in the input `bars` list
    timestamp: str | None
    price: float
    kind: str                 # "HIGH" | "LOW"

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "timestamp": self.timestamp, "price": self.price, "kind": self.kind}


def compute_zigzag_swings(bars: list[dict[str, Any]], *, reversal_pct: float) -> tuple[SwingPoint, ...]:
    """Pure function: OHLCV bars in, confirmed alternating HIGH/LOW swing
    points out. A swing is confirmed only once price has actually reversed
    by `reversal_pct` percent from the running extreme -- the final,
    still-forming leg at the end of `bars` is never included (it has not
    reversed yet, so confirming it would be fabricating a pivot that
    hasn't happened)."""
    if reversal_pct is None or reversal_pct <= 0:
        raise ElliottWaveError(f"REVERSAL_PCT_REQUIRED_AND_POSITIVE:{reversal_pct!r}")
    n = len(bars)
    if n < 2:
        return ()

    trend = 1 if bars[1]["close"] >= bars[0]["close"] else -1
    if trend == 1:
        extreme_idx, extreme_price = 0, bars[0]["high"]
    else:
        extreme_idx, extreme_price = 0, bars[0]["low"]

    pivots: list[SwingPoint] = []
    for i in range(1, n):
        bar = bars[i]
        if trend == 1:
            if bar["high"] > extreme_price:
                extreme_price, extreme_idx = bar["high"], i
            reversal = (extreme_price - bar["low"]) / extreme_price * 100.0
            if reversal >= reversal_pct:
                pivots.append(SwingPoint(
                    index=extreme_idx, timestamp=bars[extreme_idx].get("timestamp"),
                    price=extreme_price, kind=KIND_HIGH,
                ))
                trend = -1
                extreme_idx, extreme_price = i, bar["low"]
        else:
            if bar["low"] < extreme_price:
                extreme_price, extreme_idx = bar["low"], i
            reversal = (bar["high"] - extreme_price) / extreme_price * 100.0
            if reversal >= reversal_pct:
                pivots.append(SwingPoint(
                    index=extreme_idx, timestamp=bars[extreme_idx].get("timestamp"),
                    price=extreme_price, kind=KIND_LOW,
                ))
                trend = 1
                extreme_idx, extreme_price = i, bar["high"]

    return tuple(pivots)


# ------------------------------------------------------------------------
# EWO -- the named, standard 5/34 typical-price MA-spread oscillator (see
# module docstring for why 5/34 is a definition, not a free parameter).
# ------------------------------------------------------------------------

def compute_ewo(bars: list[dict[str, Any]], *, fast_period: int = 5, slow_period: int = 34) -> tuple[float | None, ...]:
    """One EWO value per bar, aligned by index; `None` where insufficient
    history exists yet (never a fabricated early value)."""
    if fast_period <= 0 or slow_period <= 0 or fast_period >= slow_period:
        raise ElliottWaveError(f"INVALID_EWO_PERIODS:fast={fast_period!r},slow={slow_period!r}")
    n = len(bars)
    if n == 0:
        return ()
    typical = np.array([(b["high"] + b["low"] + b["close"]) / 3.0 for b in bars], dtype=float)

    result: list[float | None] = [None] * n
    for i in range(slow_period - 1, n):
        fast_avg = typical[i - fast_period + 1: i + 1].mean()
        slow_avg = typical[i - slow_period + 1: i + 1].mean()
        result[i] = float(fast_avg - slow_avg)
    return tuple(result)


# ------------------------------------------------------------------------
# The 3 sourced invalidation rules, applied to exactly one 6-pivot window
# (P0..P5 = wave-1..wave-5 endpoints). Pure function; never decides
# validity from EWO or anything not in the sourced spec.
# ------------------------------------------------------------------------

RULE_WAVE2_RETRACE = "WAVE2_NOT_BEYOND_100PCT_OF_WAVE1"
RULE_WAVE3_NOT_SHORTEST = "WAVE3_NOT_SHORTEST_OF_1_3_5"
RULE_WAVE4_NO_OVERLAP = "WAVE4_NO_OVERLAP_WITH_WAVE1"


@dataclass(frozen=True)
class RuleEvaluation:
    rule: str
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"rule": self.rule, "passed": self.passed, "detail": self.detail}


def _impulse_direction(window: tuple[SwingPoint, ...]) -> str | None:
    """UP if P0 is a LOW (impulse rises: LOW,HIGH,LOW,HIGH,LOW,HIGH);
    DOWN if P0 is a HIGH (mirror). None if the window is not a valid
    strictly-alternating 6-pivot sequence of the expected shape."""
    if len(window) != 6:
        return None
    expected_up = (KIND_LOW, KIND_HIGH, KIND_LOW, KIND_HIGH, KIND_LOW, KIND_HIGH)
    expected_down = (KIND_HIGH, KIND_LOW, KIND_HIGH, KIND_LOW, KIND_HIGH, KIND_LOW)
    kinds = tuple(p.kind for p in window)
    if kinds == expected_up:
        return "UP"
    if kinds == expected_down:
        return "DOWN"
    return None


def evaluate_impulse_rules(window: tuple[SwingPoint, ...]) -> tuple[str | None, tuple[RuleEvaluation, ...]]:
    """Returns (wave_direction, rule_evaluations) for one 6-pivot window.
    wave_direction is None (and rule_evaluations empty) if the window is
    not a structurally valid alternating 6-pivot impulse shape."""
    direction = _impulse_direction(window)
    if direction is None:
        return None, ()

    p0, p1, p2, p3, p4, p5 = (pt.price for pt in window)

    if direction == "UP":
        wave1, wave2, wave3, wave4, wave5 = (p1 - p0), (p1 - p2), (p3 - p2), (p3 - p4), (p5 - p4)
    else:
        wave1, wave2, wave3, wave4, wave5 = (p0 - p1), (p2 - p1), (p2 - p3), (p4 - p3), (p4 - p5)

    if wave1 <= 0 or wave3 <= 0 or wave5 <= 0 or wave4 <= 0:
        return direction, (RuleEvaluation(
            rule="DEGENERATE_WAVE_LENGTH", passed=False,
            detail=(
                f"one or more of wave1/wave3/wave4/wave5 was <= 0 "
                f"(wave1={wave1:.6g}, wave3={wave3:.6g}, wave4={wave4:.6g}, wave5={wave5:.6g})"
            ),
        ),)

    retrace_fraction = wave2 / wave1
    rule1 = RuleEvaluation(
        rule=RULE_WAVE2_RETRACE, passed=retrace_fraction < 1.0,
        detail=f"wave2 retraced {retrace_fraction * 100:.2f}% of wave1 (must be <100%)",
    )
    rule2_passed = not (wave3 < wave1 and wave3 < wave5)
    rule2 = RuleEvaluation(
        rule=RULE_WAVE3_NOT_SHORTEST, passed=rule2_passed,
        detail=f"wave1={wave1:.6g}, wave3={wave3:.6g}, wave5={wave5:.6g} (wave3 must not be the shortest)",
    )
    if direction == "UP":
        rule3_passed = p4 > p1
        rule3_detail = f"wave4 low={p4:.6g} vs wave1 high={p1:.6g} (wave4 low must stay above wave1 high)"
    else:
        rule3_passed = p4 < p1
        rule3_detail = f"wave4 high={p4:.6g} vs wave1 low={p1:.6g} (wave4 high must stay below wave1 low)"
    rule3 = RuleEvaluation(rule=RULE_WAVE4_NO_OVERLAP, passed=rule3_passed, detail=rule3_detail)

    return direction, (rule1, rule2, rule3)


# ------------------------------------------------------------------------
# Corrective (A-B-C) structural labeling -- no invalidation rule applied
# (see module docstring: none was found in any source document).
# ------------------------------------------------------------------------

@dataclass(frozen=True)
class CorrectiveCandidate:
    pivots: tuple[SwingPoint, ...]   # 3 points: A, B, C endpoints
    labels: tuple[str, ...]          # ("A", "B", "C")
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "pivots": [p.to_dict() for p in self.pivots],
            "labels": list(self.labels),
            "note": self.note,
        }


_NO_SOURCED_RULE_NOTE = (
    "Structural label only -- no sourced invalidation rule exists for the "
    "corrective leg in this project's source material; validity is not "
    "assessed for A/B/C, unlike the impulse leg's 3 documented rules."
)


def label_corrective_candidate(swings: tuple[SwingPoint, ...], after_index_in_swings: int) -> CorrectiveCandidate | None:
    """`after_index_in_swings` is the position within `swings` of the
    impulse's Wave 5 endpoint (P5). Returns a structural A/B/C label for
    the next 3 swings if they exist, else None (never fabricates missing
    pivots)."""
    start = after_index_in_swings + 1
    window = swings[start:start + 3]
    if len(window) != 3:
        return None
    return CorrectiveCandidate(pivots=window, labels=("A", "B", "C"), note=_NO_SOURCED_RULE_NOTE)


# ------------------------------------------------------------------------
# Impulse candidates -- every consecutive 6-pivot window, independently
# evaluated. Overlapping VALID windows are BOTH reported (ambiguity is
# real and disclosed, never resolved by picking one).
# ------------------------------------------------------------------------

@dataclass(frozen=True)
class ImpulseCandidate:
    window_start_swing_index: int       # position within the `swings` tuple
    wave_direction: str                 # "UP" | "DOWN" -- a structural/geometric label, NOT a trade instruction
    pivots: tuple[SwingPoint, ...]       # P0..P5 (6 points)
    rule_evaluations: tuple[RuleEvaluation, ...]
    validity: str                       # "VALID_CANDIDATE" | "INVALIDATED"
    failed_rules: tuple[str, ...]
    ewo_at_wave3_end: float | None
    ewo_at_wave5_end: float | None
    corrective_candidate: CorrectiveCandidate | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_start_swing_index": self.window_start_swing_index,
            "wave_direction": self.wave_direction,
            "pivots": [p.to_dict() for p in self.pivots],
            "rule_evaluations": [r.to_dict() for r in self.rule_evaluations],
            "validity": self.validity,
            "failed_rules": list(self.failed_rules),
            "ewo_at_wave3_end": self.ewo_at_wave3_end,
            "ewo_at_wave5_end": self.ewo_at_wave5_end,
            "corrective_candidate": self.corrective_candidate.to_dict() if self.corrective_candidate else None,
        }


def find_impulse_candidates(
    swings: tuple[SwingPoint, ...],
    ewo_series: tuple[float | None, ...],
) -> tuple[ImpulseCandidate, ...]:
    """Slides a 6-pivot window across every consecutive position in
    `swings` (overlapping windows included on purpose). Pure function."""
    candidates: list[ImpulseCandidate] = []
    for start in range(0, max(0, len(swings) - 5)):
        window = swings[start:start + 6]
        if len(window) != 6:
            continue
        direction, rule_evals = evaluate_impulse_rules(window)
        if direction is None:
            continue  # not a structurally valid alternating shape at this offset
        failed = tuple(r.rule for r in rule_evals if not r.passed)
        validity = "VALID_CANDIDATE" if not failed else "INVALIDATED"
        p3_idx = window[3].index
        p5_idx = window[5].index
        ewo3 = ewo_series[p3_idx] if 0 <= p3_idx < len(ewo_series) else None
        ewo5 = ewo_series[p5_idx] if 0 <= p5_idx < len(ewo_series) else None
        corrective = label_corrective_candidate(swings, start + 5)
        candidates.append(ImpulseCandidate(
            window_start_swing_index=start, wave_direction=direction, pivots=window,
            rule_evaluations=rule_evals, validity=validity, failed_rules=failed,
            ewo_at_wave3_end=ewo3, ewo_at_wave5_end=ewo5, corrective_candidate=corrective,
        ))
    return tuple(candidates)


# ------------------------------------------------------------------------
# Top-level orchestration.
# ------------------------------------------------------------------------

@dataclass(frozen=True)
class ElliottWaveResearchResult:
    symbol: str
    as_of: str
    reversal_pct: float
    swings: tuple[SwingPoint, ...]
    impulse_candidates: tuple[ImpulseCandidate, ...]
    valid_candidate_count: int
    ambiguity_status: str  # "INSUFFICIENT_DATA" | "ALL_CANDIDATES_INVALIDATED" | "SINGLE_VALID_CANDIDATE" | "AMBIGUOUS_MULTIPLE_CANDIDATES"

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "as_of": self.as_of,
            "reversal_pct": self.reversal_pct,
            "swings": [s.to_dict() for s in self.swings],
            "impulse_candidates": [c.to_dict() for c in self.impulse_candidates],
            "valid_candidate_count": self.valid_candidate_count,
            "ambiguity_status": self.ambiguity_status,
        }


def analyze_elliott_wave(
    bars: list[dict[str, Any]],
    symbol: str,
    *,
    reversal_pct: float,
    ewo_fast_period: int = 5,
    ewo_slow_period: int = 34,
    now: datetime | None = None,
) -> ElliottWaveResearchResult:
    """Pure function: OHLCV bars in, a full research result out. Same
    `bars`/`reversal_pct` always reproduces the identical result -- no
    randomness, no external call, no AI."""
    as_of = (now or datetime.now(timezone.utc)).isoformat()
    swings = compute_zigzag_swings(bars, reversal_pct=reversal_pct)
    ewo_series = compute_ewo(bars, fast_period=ewo_fast_period, slow_period=ewo_slow_period)
    candidates = find_impulse_candidates(swings, ewo_series)
    valid_count = sum(1 for c in candidates if c.validity == "VALID_CANDIDATE")

    if len(swings) < 6:
        ambiguity_status = "INSUFFICIENT_DATA"
    elif valid_count == 0:
        ambiguity_status = "ALL_CANDIDATES_INVALIDATED"
    elif valid_count == 1:
        ambiguity_status = "SINGLE_VALID_CANDIDATE"
    else:
        ambiguity_status = "AMBIGUOUS_MULTIPLE_CANDIDATES"

    return ElliottWaveResearchResult(
        symbol=symbol, as_of=as_of, reversal_pct=reversal_pct, swings=swings,
        impulse_candidates=candidates, valid_candidate_count=valid_count,
        ambiguity_status=ambiguity_status,
    )
