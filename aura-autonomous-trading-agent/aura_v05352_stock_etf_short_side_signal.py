#!/usr/bin/env python3
"""
AURA v0.5.3.52 — Stock/ETF short-side technical signal source.

`.52` is the mirror-image sibling of `.51` ("Live Alpaca equities/ETFs
technical signal source"): where `.51` produces a LONG-only, unsigned
bullishness magnitude feeding `.50` as an always-ADDED evidence term,
`.52` produces a SHORT-only, unsigned bearishness magnitude feeding `.50`
as an always-SUBTRACTED evidence term. Per Martin's explicit `.51` GO
(2026-09-13), `.51` was deliberately scoped long-only, with `.52` reserved
as "the separate short-side milestone" — this module is that milestone.

Architecture
------------------------------------------------------------------------
    Pinned Equity/ETF Universe (reused verbatim from `.51`)
             |
    Existing Technical Features (`.51`'s `build_technical_features`, reused directly)
             |
    `.52` Live SHORT-side Technical Signal (this module, new bearish scoring)
             |
    Typed Technical Evidence (ShortTechnicalRegime)
             |
    `.50` Decision Engine (small additive extension, subtract-only -- see below)
             |
    `.35`/`.38` Enforcement (unchanged; SHORT-execution readiness already
    dormant/ready per `.33`-`.38` -- shortability gating remains entirely
    THEIR concern at execution time, not this module's -- see "Shortability
    gating" below)

`.52`'s own scope stops at producing a `ShortTechnicalRegime` and feeding
it into `.50`'s `CandidateEvidence`/`decide()`. It does not touch `.39`,
`.44`, or overall roadmap sequencing, and it does not modify `.51` in any
way -- `.51`'s module, tests, and "technical never subtracts" governance
test are completely untouched by this milestone.

Reuse-first audit (two dedicated subagent passes, read-only, file:line
cited, performed before writing any code this milestone -- the second
pass explicitly covering Martin's expanded mandatory checklist: DELTAX/
DELTAX_v2, Hermes Trader, Aegis/AegisAlpha, mexc_bot, existing AURA
`.01`-`.51`, v0.4/v0.47 scanners, existing crypto/equity backtesters,
walk-forward testing, permutation testing, parameter sweeps, MAE/MFE
diagnostics, regime diagnostics, long/short tests, existing MEXC
execution/reconciliation code)
------------------------------------------------------------------------
Formal classification scheme used throughout (Martin's explicit
instruction): A = DIRECT REUSE (unchanged). B = ADAPT/EXTRACT (algorithm/
logic proven and useful, interface adapted to AURA's contract).
C = REFERENCE ONLY (useful for comparison, not implementation).
D = REJECT (known flaw or violates AURA's current requirements).

  - `.51`'s own `build_technical_features` (technical_agent.py lineage),
    `fetch_recent_bars`, `AlpacaHistoricalBarsClient`, `BarsClient`
    Protocol, `load_pinned_universe`/`PinnedUniverse`, and
    `aura_v05351_equity_universe_v1.json` -- classified **A, DIRECT
    REUSE**. All of this is direction-agnostic: the same bars, the same
    feature computation (EMA 3/8/21/50, RSI-14, MACD/signal, ATR%,
    relative volume, price acceleration), and the same pinned universe
    are valid inputs to a bearish evaluation exactly as they are to a
    bullish one. Reused via `.52`'s own `_load_module` dynamic-import
    convention (see below) -- the same pattern `.50` already established
    for reusing `.33`/`.49` without copy-paste drift risk -- rather than
    copied, because this is genuine reuse of another AURA milestone's own
    canonical contract (not an external research script), and `.52`'s
    bearish scoring must never silently drift from `.51`'s bullish
    feature computation.
  - `signal_validator.py`'s "score the last row" mechanism (already
    classified B by `.51`'s own audit) -- reused a second time, again as
    **B**: the mechanism (read `df.iloc[-1]` plus trailing `.tail(...)`
    windows, accumulate points, clamp, classify into a status) is sound
    and reused; every constant is, again, a required `ShortTechnicalScoringParams`
    field, never invented.
  - `mexc_bot/core/strategy.py::evaluate_signal()` (found by the
    supplementary audit) -- both a momentum mode and a MACD-crossover
    mode, EACH explicitly mirrored long/short with an ADX regime filter.
    Classified **B**: concrete, independent, working prior art (in a
    sibling recovered project, not AURA itself) that an exact-mirror,
    direction-flipped bearish scoring rule built from the same bullish
    inputs is a sound, previously-validated shape. Not reused as code
    (different feature set, hidden module-level constants that conflict
    with AURA's "never invent numbers" discipline -- see mexc_bot's
    overall B classification in the supplementary audit for detail), but
    the SHAPE is exactly what `.52` implements below.
  - `mexc_bot/long_only_test.py` / `short_only_test.py` (found by the
    supplementary audit) -- a genuine mirror-pair validation methodology:
    "only the direction filter changes; indicators, entry timing, exits,
    sizing and costs are unchanged," tested with a strict holdout and an
    explicit no-tune-after-holdout rule. Classified **B**, methodology
    only (not code): this is concrete prior evidence, from an unrelated
    recovered codebase, that "hold every other component fixed and flip
    only the direction rule" is a workable, low-risk way to isolate a
    short-side variant -- directly validating the shape of `.52`'s design
    (new subtract-only evidence dimension, sign-flipped, reusing `.51`'s
    own feature computation unchanged). This methodology is noted here as
    the precedent for how a FUTURE validation milestone should test `.52`
    once built -- `.52` itself does not run a holdout test (that is
    backtesting/validation work, explicitly out of scope per Martin's
    ".52/.53 first, comprehensive crypto backtesting after" sequencing).
  - `mexc_bot/trade_diagnostics.py` "LONG vs SHORT" breakdown, `walk_forward.py`,
    `permutation_test.py`, `param_sweep.py` (found by the supplementary
    audit) -- classified **C, REFERENCE ONLY** for `.52` specifically:
    all are POST-TRADE / backtesting-validation tooling, not live signal
    generation, and Martin's own instruction defers "assemble the
    strongest proven components" backtesting-reuse work to AFTER `.53`,
    against a dedicated future backtester-reuse audit. Noted here for
    that future work, not adopted now.
  - DELTAX_v2 `deltax/technical_scanner.py` (a production SP500 scanner
    producing both "long"/"short" technical candidates via a
    VWAP-deviation + ATR14 + market-regime-proxy feature set) -- found by
    both audit passes, classified **C, REFERENCE ONLY**: confirms a
    mirrored long/short stock scanner exists in the recovered corpus, but
    uses a different feature family than `.51`/`.52` (VWAP deviation, not
    EMA/RSI/MACD), and its regime-gating idea is the same "VWAP weak
    count" style filter that DELTAX's own `backtest/regime_test.py`
    explicitly found **no predictive evidence for** at any horizon tested
    ("do not use this filter to pick a directional side") -- a documented
    negative result, not a source to copy from. `.52` does not adopt any
    regime-gate; it mirrors `.51`'s own ungated design exactly.
  - `.33`-`.38` SHORT-execution readiness (re-confirmed by both audit
    passes, file:line, from `.51`'s own prior audit): fully implemented
    and dormant. `.35` has 45 passing tests including 8 SHORT-specific
    ones; `.33`'s `CanonicalExecutionIntent` and `.38`'s supervisor both
    already handle a SHORT `side` end-to-end. Classified **A, DIRECT
    REUSE** -- nothing in `.33`-`.38` needs to change for `.52` to exist;
    `.52` produces evidence for `.50` to arbitrate, exactly as `.51` does,
    and `.33`-`.38` remain the unmodified, already-ready execution path.
  - `mexc_bot/live_bot.py` (found by the supplementary audit) --
    classified **D for its execution path** (places real orders via ccxt
    directly, no AI-proposes/`.50`-arbitrates/`.33`-`.38`-enforces
    separation, no reconciliation ledger -- exactly the anti-pattern
    AURA's architecture exists to prevent) -- not reused in any form.
    Confirms, by contrast, why `.52` must (and does) stop at producing
    typed evidence for `.50`, never touching execution itself.
  - `.42` (MEXC crypto short-side signal research, re-confirmed by the
    first `.52` audit pass) -- classified **C, REFERENCE ONLY**: a
    standalone offline backtest harness for MEXC perpetuals, never wired
    to `.50`, methodology-relevant only, not structurally reusable here
    (different venue, different instrument type, different feature set).
  - No genuine live bearish/short equity technical-signal-generation
    module was found ANYWHERE in the full recovered corpus (AURA's own
    `.01`-`.51`, DELTAX/DELTAX_v2, Hermes Trader, Aegis/AegisAlpha,
    mexc_bot, BABIL, CAURA, optionwright) -- `.52`'s bearish scoring
    function is greenfield, built by mirroring `.51`'s own already-
    approved bullish mechanism with every comparison direction-flipped,
    not imported from any prior source.

Sub-questions resolved directly (per Martin's explicit standing
authorization this session: "continue `.52 -> .53` without waiting for
another approval unless you discover a genuine architectural ambiguity or
a safety-critical conflict" -- none of the following rose to that bar;
each is a narrow, reversible scoping choice with a clear, previously-
established precedent to follow)
------------------------------------------------------------------------
  1. **`.52` -> `.50` integration shape: a wholly new, independent,
     subtract-only 4th `CandidateEvidence` dimension -- mirroring `.51`'s
     own additive-extension shape exactly, sign flipped.** The
     alternative (making `.51`'s existing `TechnicalRegime` bidirectional)
     was explicitly rejected by the first audit pass and is rejected here
     too: `.51`'s `TechnicalRegime`/`TechnicalScoringParams` are an
     already-approved, already-tested contract (47 tests), and Martin's
     standing instruction is "do not redesign completed milestones unless
     a concrete defect requires it" -- no defect exists in `.51`, so it is
     not touched.
  2. **Shortability gating: NOT this module's concern.** `.52` computes
     short-side evidence for every symbol in the pinned universe
     unconditionally, exactly mirroring how `.51` computes long-side
     evidence unconditionally with no check on whether long execution is
     actually possible for a symbol. Whether a symbol can actually be
     shorted is `.33`-`.38`'s existing, already-tested, execution-time
     concern (confirmed dormant-and-ready by both audit passes) -- adding
     a second, earlier shortability check here would duplicate that
     existing gate, which Martin's "do not redesign completed milestones"
     and ".44 already owns portfolio-wide arbitration, don't duplicate it"
     precedents (from `.50`'s own docstring) both argue against.
  3. **Universe scope: reuse `.51`'s exact same 27-symbol pinned universe
     verbatim, same JSON file, same loader.** No new universe file. A
     shortability-filtered subset would require this module to duplicate
     `.33`-`.38`'s own shortability knowledge (or invent a new one) --
     directly conflicting with decision 2 above. `.52` evaluates the full
     universe; downstream execution authorization decides what is
     actually actionable, exactly as it already does for `.51`.
  4. **Bearish scoring rule shape: exact mirror of `.51`'s bullish rules,
     direction-flipped throughout, not an asymmetric/independent rule
     set.** This is the shape `mexc_bot`'s own long/short mirror pair (and
     AlphaPilot's exact-mirror pattern, noted by the first audit pass)
     both independently validate as sound. An asymmetric rule set would
     be inventing new, unvalidated bearish-specific logic with no prior
     art anywhere in the recovered corpus -- rejected in favor of the
     validated mirror shape.
  5. **Mechanically-neutral parameters (`min_bars_required`,
     `max_bar_age_seconds`, `persistence_lookback_bars`, etc.): duplicated
     as independent required fields on a new `ShortTechnicalScoringParams`
     dataclass, NOT shared with `.51`'s `TechnicalScoringParams` via any
     new composition/inheritance mechanism.** This matches `.51`'s own
     precedent exactly (a single, self-contained, fully-enumerated
     required-fields dataclass, no shared abstraction invented) and keeps
     `.52` independently testable and evolvable without coupling `.51`'s
     and `.52`'s calibration together. If Martin later wants these two
     symbols' worth of neutral parameters to be forced identical by
     construction, that is a deliberate, separate, explicitly-approved
     change, not something this milestone does on its own initiative.

Why "exact mirror, sign-flipped" is not merely convenient but principled
------------------------------------------------------------------------
Every comparison in `evaluate_current_bar_short` below is the literal
logical negation of its `.51` counterpart (`<` where `.51` has `>`, a
downward EMA cross where `.51` has an upward one, oversold-penalty where
`.51` has overbought-penalty, a bearish "squeeze" penalty where `.51` has
a bullish "whipsaw" penalty). This is deliberate: it is the only rule
shape for which "the underlying technical setup is symmetric, only the
side differs" is true by construction, rather than by claim. Any
asymmetry between the long and short rule sets would need its own
independent justification/derivation that does not exist anywhere in this
project's research to date -- consistent with `.51`'s own "never invent
numbers" discipline applied one level up, to rule SHAPE, not just rule
MAGNITUDE.

`.50` integration changes (the ONLY changes made to
`aura_v05350_decision_engine.py` this milestone -- everything else in
`.50`, including everything `.51` already added, is unmodified)
------------------------------------------------------------------------
  1. `CandidateEvidence` gains two new fields: `short_technical_regime:
     Any | None` (a `.52` `ShortTechnicalRegime` instance, duck-typed,
     mirroring `technical_regime` exactly) and `short_technical_usable:
     bool`.
  2. `build_candidate_evidence()` gains one new optional parameter,
     `short_technical_regime: Any | None = None`, defaulting to `None` so
     every existing caller (including all of `.50`'s 64 prior tests, and
     everything `.51` already added) is unaffected. A candidate is
     `short_technical_usable` only when `status in {"CONFIRMING",
     "CONFIRMED"}` -- the exact same quality bar `.51` uses, mirrored.
  3. `compute_base_rank_score()` gains one new REQUIRED (no default)
     parameter, `short_technical_weight: float`, and one new weighted
     term: `score -= short_technical_weight * (short_technical_regime.
     signal_score / 100.0)` when `short_technical_usable` -- added ONLY
     when usable, and ALWAYS SUBTRACTED (never added), the exact mirror
     of `.51`'s own term, sign-flipped. This preserves `.50`'s "direction
     is decided before penalties are applied" invariant unchanged, and
     extends it symmetrically: a purely-short-technical candidate can now
     become SHORT_LEANING (score < 0) exactly as a purely-long-technical
     candidate becomes LONG_LEANING (score > 0) -- neither can flip a
     decision made from the other evidence dimensions, only contribute to
     the same additive/subtractive base score they already share.
  4. `decide()` gains the same new required `short_technical_weight`
     parameter, threaded straight through to `compute_base_rank_score`.
     No other logic in `decide()` changes.
  5. `.50`'s existing test suite (64 tests: 51 prior + 13 from `.51`) was
     updated to pass `short_technical_weight=0.0` at each of their
     `decide()`/`compute_base_rank_score()` call sites -- an explicit,
     caller-supplied "short-technical evidence not in use here" value for
     tests that predate this milestone. `.50`'s FULL PRIOR TEST SUITE (all
     64 tests) passes unmodified in behavior after this change -- only the
     explicit `short_technical_weight=0.0` argument was added to each call
     site, nothing else.
  No other `.50` function, dataclass, or docstring section was touched.
  `.51`'s own `technical_regime`/`technical_usable`/`technical_weight`
  integration is completely unmodified -- `.52` adds a fourth, sibling
  dimension alongside it, never alters it.

`ShortTechnicalRegime` design
------------------------------------------------------------------------
`.52` is scoped SHORT-only this milestone (the mirror of `.51`'s
long-only scoping). `ShortTechnicalRegime.signal_score` is an UNSIGNED
bearishness magnitude in `[0, 100]` (same clamp convention as `.51`,
driven by caller-supplied point weights) -- there is no bullish scoring
path anywhere in this module (see the governance test
`test_module_has_no_long_or_bullish_scoring_path`, AST-based per `.51`'s
own established convention, avoiding the false-positive class this
project already fixed twice -- once in `.49`'s suite, once in `.51`'s own
-- from a raw substring search matching this module's own docstring
prose). `.50`'s new short-technical term is therefore always subtractive
when usable, never additive, consistent with `.52`'s short-only scope.

Fail-closed / freshness discipline
------------------------------------------------------------------------
Identical discipline to `.51`, mirrored exactly: `ShortTechnicalRegime.
status` is one of `INSUFFICIENT_DATA` / `STALE_DATA` / `EARLY` /
`CONFIRMING` / `CONFIRMED` / `FAILED` (the same status vocabulary `.51`
already defined and validated -- reused directly via `M51.
TECHNICAL_STATUSES`/`M51.USABLE_TECHNICAL_STATUSES`, not redefined, so the
two modules' status semantics can never drift apart). Only `CONFIRMING`/
`CONFIRMED` are `short_technical_usable` by `.50`.
`INSUFFICIENT_DATA`/`STALE_DATA`/`FAILED` are recorded but contribute
nothing to `.50`'s score.

No look-ahead / no repainting
------------------------------------------------------------------------
`.52` performs NO new feature computation of its own beyond one small,
purely-local, strictly-backward-looking derivation: a bearish EMA3/EMA8
downward-cross flag, computed from the current and immediately prior
row's already-backward-looking `ema_3`/`ema_8` values (both already
present in the `.51`-reused `build_technical_features` output) -- no
`.shift(-N)`, no forward-looking window, anywhere in this module.
Everything else is `.51`'s own already-proven-no-look-ahead feature
computation, reused directly. A dedicated regression test
(`test_no_look_ahead_appending_future_bars_does_not_change_past_row`,
mirroring `.51`'s own test of the same name) proves this directly for
`.52`'s own evaluation function, not merely inherited by assumption.

Persistence
------------------------------------------------------------------------
None. `build_short_technical_regime_from_bars` is a pure function of a
caller-supplied bars DataFrame and required parameters, mirroring `.47`/
`.48`/`.49`/`.50`/`.51`'s own precedent. The live fetch layer is reused
directly from `.51` (`M51.fetch_recent_bars`/`M51.
AlpacaHistoricalBarsClient`) -- this module defines no new network code
of its own.

Known limitations (disclosed, not silently worked around)
------------------------------------------------------------------------
  - No live smoke test against a real Alpaca market-data call was run in
    this sandboxed environment -- identical disclosed limitation to
    `.51`, for the identical reason (no live credentials configured here).
  - `ShortTechnicalScoringParams` has 26 required fields (no defaults),
    matching `.51`'s own deliberate verbosity -- every constant is an
    explicit parameter, never a hidden default, per Martin's "never
    invent numbers" instruction applied identically here.
  - `.52` is SHORT-only this milestone -- `ShortTechnicalRegime` has no
    bullish scoring path, mirroring `.51`'s own long-only limitation.
  - Shortability (whether a given symbol can actually be shorted at
    execution time) is NOT checked by this module -- an explicit,
    disclosed scoping decision (see "Sub-questions resolved directly", 2
    above), not an oversight. `.33`-`.38` already own this check.
  - No holdout/permutation validation of this milestone's own bearish
    scoring rules has been run -- that is explicitly deferred to a future
    backtesting/validation phase (Martin's own stated sequencing: `.52`/
    `.53` first, comprehensive crypto backtesting/validation after `.53`).
    The mirror-shape design is methodologically supported by
    `mexc_bot`'s own long/short mirror-pair precedent (see audit above),
    but that is prior art from an unrelated codebase, not a validation of
    `.52`'s own specific rule magnitudes (which, per the point above, have
    no default magnitudes supplied by this module at all).
  - This module has no scheduled/orchestrated entry point of its own,
    mirroring `.51`'s own disclosed limitation -- left to a future
    orchestration milestone (`.53`).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VERSION = "AURA v0.5.3.52"
ENGINE = "SHORT_SIDE_EQUITY_TECHNICAL_SIGNAL_SOURCE"
SCHEMA_VERSION = "1.0"

ROOT = Path(__file__).resolve().parent


class ShortSignalSourceError(Exception):
    """Raised for programmer-error / required-parameter / structurally-
    malformed-input violations only -- never for a symbol simply lacking
    enough clean bars or having a stale feed (those are expected, handled
    outcomes: ShortTechnicalRegime.status == INSUFFICIENT_DATA /
    STALE_DATA), mirroring `.51`'s own `LiveSignalSourceError` discipline
    exactly (a distinct exception CLASS, deliberately, so a `.51` error
    can never be silently mistaken for a `.52` one or vice versa).
    """


# ============================================================================
# Dynamic import of `.51` -- same `_load_module` pattern `.50` established
# for reusing `.33`/`.49` without copy-paste drift risk. `.51` is used
# strictly through its existing public functions/constants; nothing in
# `.51` is modified by this module.
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


def load_equity_signal_module():
    return _load_module(
        "aura_v05351_live_alpaca_equity_signal_source", "aura_v05351_live_alpaca_equity_signal_source.py"
    )


# ============================================================================
# Scoring parameters -- the bearish mirror of `.51`'s TechnicalScoringParams.
# EVERY field is required, no default, per this project's "never invent
# numbers" discipline. Deliberately NOT shared/composed with `.51`'s own
# dataclass (see "Sub-questions resolved directly", 5, above).
# ============================================================================


@dataclass(frozen=True, slots=True)
class ShortTechnicalScoringParams:
    min_bars_required: int
    max_bar_age_seconds: float
    ema3_cross_below_ema8_points: float
    ema3_below_ema8_points: float
    ema8_below_ema21_points: float
    ema21_below_ema50_points: float
    macd_bearish_points: float
    rsi_weak_low: float
    rsi_weak_high: float
    rsi_weak_points: float
    rsi_oversold_threshold: float
    rsi_oversold_penalty_points: float
    rel_volume_threshold: float
    rel_volume_points: float
    price_deceleration_points: float
    persistence_lookback_bars: int
    persistence_min_bars_for_bonus: int
    persistence_points: float
    squeeze_prior_move_threshold: float
    squeeze_now_move_threshold: float
    squeeze_penalty_points: float
    score_floor: float
    score_ceiling: float
    early_status_max_persistence: int
    confirmed_score_threshold: float
    confirmed_min_persistence: int
    confirming_score_threshold: float

    def __post_init__(self) -> None:
        if self.min_bars_required <= 0:
            raise ShortSignalSourceError("INVALID_MIN_BARS_REQUIRED:must be > 0")
        if self.max_bar_age_seconds <= 0:
            raise ShortSignalSourceError("INVALID_MAX_BAR_AGE_SECONDS:must be > 0")
        if self.rsi_weak_low > self.rsi_weak_high:
            raise ShortSignalSourceError("INVALID_RSI_WEAK_BAND:low must be <= high")
        if self.score_floor > self.score_ceiling:
            raise ShortSignalSourceError("INVALID_SCORE_BOUNDS:floor must be <= ceiling")
        if self.persistence_lookback_bars <= 0:
            raise ShortSignalSourceError("INVALID_PERSISTENCE_LOOKBACK_BARS:must be > 0")


# ============================================================================
# Current-bar bearish evaluation -- the exact mirror of `.51`'s
# evaluate_current_bar, every comparison direction-flipped. Reads ONLY
# features_df.iloc[-1], features_df.iloc[-2] (for the local downward-cross
# derivation), and trailing .tail(...) windows -- no look-ahead. `.51`'s
# feature computation is reused unmodified via M51.build_technical_features;
# this function adds no new feature columns beyond one local, strictly
# backward-looking boolean.
# ============================================================================


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(value != value)  # NaN != NaN
    except Exception:
        return False


def evaluate_current_bar_short(features_df: Any, *, params: ShortTechnicalScoringParams) -> dict[str, Any]:
    """Deterministic, LLM-free, live/current-bar bearish evaluation --
    the mirror of `.51`'s `evaluate_current_bar`, direction-flipped
    throughout. Returns a plain dict, wrapped into a typed
    `ShortTechnicalRegime` by `build_short_technical_regime_from_bars`
    below, mirroring `.51`'s own two-layer shape exactly.
    """
    if len(features_df) < params.min_bars_required:
        return {
            "status": "INSUFFICIENT_DATA",
            "signal_score": 0.0,
            "persistence_bars": 0,
            "rsi_14": None,
            "rel_volume": None,
            "atr_pct": None,
            "price_acceleration": None,
            "reasons": ("insufficient bars: need >= " + str(params.min_bars_required),),
        }

    row = features_df.iloc[-1]
    required_fields = (
        "close", "high", "low", "ema_3", "ema_8", "ema_21", "ema_50", "macd", "macd_signal",
        "rsi_14", "rel_volume", "atr_pct", "price_acceleration",
    )
    if any(_is_missing(row.get(field)) for field in required_fields):
        return {
            "status": "INSUFFICIENT_DATA",
            "signal_score": 0.0,
            "persistence_bars": 0,
            "rsi_14": None,
            "rel_volume": None,
            "atr_pct": None,
            "price_acceleration": None,
            "reasons": ("required indicator value is missing/NaN on the current bar",),
        }

    if len(features_df) < 2:
        return {
            "status": "INSUFFICIENT_DATA",
            "signal_score": 0.0,
            "persistence_bars": 0,
            "rsi_14": None,
            "rel_volume": None,
            "atr_pct": None,
            "price_acceleration": None,
            "reasons": ("not enough bars to evaluate a downward EMA crossover",),
        }
    prior_row = features_df.iloc[-2]
    if _is_missing(prior_row.get("ema_3")) or _is_missing(prior_row.get("ema_8")):
        return {
            "status": "INSUFFICIENT_DATA",
            "signal_score": 0.0,
            "persistence_bars": 0,
            "rsi_14": None,
            "rel_volume": None,
            "atr_pct": None,
            "price_acceleration": None,
            "reasons": ("prior bar EMA3/EMA8 value is missing/NaN",),
        }

    # Local, strictly backward-looking bearish-crossover derivation (the
    # mirror of `.51`-reused `cross_3_8`, which only flags UPWARD crosses).
    cross_3_8_down = bool(row["ema_3"] < row["ema_8"]) and bool(prior_row["ema_3"] >= prior_row["ema_8"])

    score = 0.0
    reasons: list[str] = []

    if cross_3_8_down:
        score += params.ema3_cross_below_ema8_points
        reasons.append("EMA3 crossed below EMA8")
    if row["ema_3"] < row["ema_8"]:
        score += params.ema3_below_ema8_points
        reasons.append("EMA3 remains below EMA8")
    if row["ema_8"] < row["ema_21"]:
        score += params.ema8_below_ema21_points
        reasons.append("EMA8 < EMA21")
    if row["ema_21"] < row["ema_50"]:
        score += params.ema21_below_ema50_points
        reasons.append("EMA21 < EMA50")
    if row["macd"] < row["macd_signal"]:
        score += params.macd_bearish_points
        reasons.append("MACD bearish")
    if params.rsi_weak_low <= row["rsi_14"] <= params.rsi_weak_high:
        score += params.rsi_weak_points
        reasons.append("RSI weak/bearish band")
    elif row["rsi_14"] < params.rsi_oversold_threshold:
        score -= params.rsi_oversold_penalty_points
        reasons.append("RSI extremely oversold (squeeze/bounce risk)")
    if row["rel_volume"] >= params.rel_volume_threshold:
        score += params.rel_volume_points
        reasons.append(f"Relative volume >= {params.rel_volume_threshold}x")
    if row["price_acceleration"] < 0:
        score += params.price_deceleration_points
        reasons.append("Negative acceleration")

    tail_persist = features_df[["ema_3", "ema_8"]].tail(params.persistence_lookback_bars)
    persistence = int((tail_persist["ema_3"] < tail_persist["ema_8"]).sum())
    if persistence >= params.persistence_min_bars_for_bonus:
        score += params.persistence_points
        reasons.append(f"EMA3/8 bearish persistence {params.persistence_lookback_bars} bars")

    recent = features_df["close"].tail(4)
    if len(recent) == 4 and not any(_is_missing(v) for v in recent):
        prev_move = recent.iloc[-2] / recent.iloc[-3] - 1
        now_move = recent.iloc[-1] / recent.iloc[-2] - 1
        if prev_move < -params.squeeze_prior_move_threshold and now_move > params.squeeze_now_move_threshold:
            score -= params.squeeze_penalty_points
            reasons.append("Possible short squeeze / bearish momentum reversal")

    score = max(params.score_floor, min(params.score_ceiling, score))

    if cross_3_8_down and persistence <= params.early_status_max_persistence:
        status = "EARLY"
    elif score >= params.confirmed_score_threshold and persistence >= params.confirmed_min_persistence:
        status = "CONFIRMED"
    elif score >= params.confirming_score_threshold:
        status = "CONFIRMING"
    else:
        status = "FAILED"

    return {
        "status": status,
        "signal_score": float(score),
        "persistence_bars": persistence,
        "rsi_14": float(row["rsi_14"]),
        "rel_volume": float(row["rel_volume"]),
        "atr_pct": float(row["atr_pct"]),
        "price_acceleration": float(row["price_acceleration"]),
        "reasons": tuple(reasons),
    }


# ============================================================================
# ShortTechnicalRegime -- the .50-compatible typed evidence output.
# Mirrors `.51`'s TechnicalRegime exactly in shape; status vocabulary is
# REUSED directly from `.51` (via the loaded M51 module), never redefined,
# so the two modules' status semantics can never drift apart.
# ============================================================================


@dataclass(frozen=True, slots=True)
class ShortTechnicalRegime:
    symbol: str
    as_of: str
    last_bar_timestamp: str | None
    status: str  # one of M51.TECHNICAL_STATUSES
    signal_score: float  # unsigned bearishness magnitude, [score_floor, score_ceiling] -- SHORT-only this milestone
    persistence_bars: int
    rsi_14: float | None
    rel_volume: float | None
    atr_pct: float | None
    price_acceleration: float | None
    reasons: tuple[str, ...]
    bars_used: int
    universe_version: str
    scoring_params_hash: str

    def __post_init__(self) -> None:
        m51 = load_equity_signal_module()
        if self.status not in m51.TECHNICAL_STATUSES:
            raise ShortSignalSourceError(f"INVALID_SHORT_TECHNICAL_STATUS:{self.status}")


def _short_scoring_params_hash(params: ShortTechnicalScoringParams) -> str:
    m51 = load_equity_signal_module()
    return m51.sha256_text(
        m51.stable_json(
            {
                field: getattr(params, field)
                for field in (
                    "min_bars_required", "max_bar_age_seconds", "ema3_cross_below_ema8_points",
                    "ema3_below_ema8_points", "ema8_below_ema21_points", "ema21_below_ema50_points",
                    "macd_bearish_points", "rsi_weak_low", "rsi_weak_high", "rsi_weak_points",
                    "rsi_oversold_threshold", "rsi_oversold_penalty_points", "rel_volume_threshold",
                    "rel_volume_points", "price_deceleration_points", "persistence_lookback_bars",
                    "persistence_min_bars_for_bonus", "persistence_points", "squeeze_prior_move_threshold",
                    "squeeze_now_move_threshold", "squeeze_penalty_points", "score_floor", "score_ceiling",
                    "early_status_max_persistence", "confirmed_score_threshold", "confirmed_min_persistence",
                    "confirming_score_threshold",
                )
            }
        )
    )


def _validate_bars_frame(bars_df: Any, m51: Any) -> None:
    """Structural validation only, mirroring `.51`'s own
    `_validate_bars_frame` -- a well-formed fetch should never produce a
    frame missing these columns or without a valid index; if it does,
    that is a caller/integration error (raises), distinct from a symbol
    simply having too few or too-stale CLEAN bars.
    """
    if bars_df is None:
        raise ShortSignalSourceError("BARS_FRAME_IS_NONE")
    missing = [c for c in m51.REQUIRED_BAR_COLUMNS if c not in getattr(bars_df, "columns", [])]
    if missing:
        raise ShortSignalSourceError(f"BARS_FRAME_MISSING_COLUMNS:{missing}")


def build_short_technical_regime_from_bars(
    symbol: str,
    bars_df: Any,
    *,
    params: ShortTechnicalScoringParams,
    universe_version: str,
    now: datetime | None = None,
) -> ShortTechnicalRegime:
    """The deterministic, network-free core: bars DataFrame -> `.51`-
    reused features -> `.52`'s own bearish current-bar evaluation ->
    typed, fail-closed, freshness-checked ShortTechnicalRegime. Pure
    function of its inputs; no I/O. Mirrors `.51`'s own
    `build_technical_regime_from_bars` exactly in structure.
    """
    m51 = load_equity_signal_module()
    _validate_bars_frame(bars_df, m51)
    now_dt = now or datetime.now(timezone.utc)
    as_of = m51._now_iso(now_dt)
    params_hash = _short_scoring_params_hash(params)

    if len(bars_df) == 0:
        return ShortTechnicalRegime(
            symbol=symbol, as_of=as_of, last_bar_timestamp=None, status="INSUFFICIENT_DATA",
            signal_score=0.0, persistence_bars=0, rsi_14=None, rel_volume=None, atr_pct=None,
            price_acceleration=None, reasons=("no bars available",), bars_used=0,
            universe_version=universe_version, scoring_params_hash=params_hash,
        )

    last_bar_ts = bars_df.index[-1]
    last_bar_dt = last_bar_ts.to_pydatetime() if hasattr(last_bar_ts, "to_pydatetime") else last_bar_ts
    if last_bar_dt.tzinfo is None:
        last_bar_dt = last_bar_dt.replace(tzinfo=timezone.utc)
    bar_age_seconds = (now_dt - last_bar_dt).total_seconds()

    if bar_age_seconds < 0:
        return ShortTechnicalRegime(
            symbol=symbol, as_of=as_of, last_bar_timestamp=m51._now_iso(last_bar_dt), status="STALE_DATA",
            signal_score=0.0, persistence_bars=0, rsi_14=None, rel_volume=None, atr_pct=None,
            price_acceleration=None, reasons=("last bar is future-dated",), bars_used=len(bars_df),
            universe_version=universe_version, scoring_params_hash=params_hash,
        )
    if bar_age_seconds > params.max_bar_age_seconds:
        return ShortTechnicalRegime(
            symbol=symbol, as_of=as_of, last_bar_timestamp=m51._now_iso(last_bar_dt), status="STALE_DATA",
            signal_score=0.0, persistence_bars=0, rsi_14=None, rel_volume=None, atr_pct=None,
            price_acceleration=None,
            reasons=(f"last bar age {bar_age_seconds:.0f}s exceeds max_bar_age_seconds {params.max_bar_age_seconds}",),
            bars_used=len(bars_df), universe_version=universe_version, scoring_params_hash=params_hash,
        )

    features = m51.build_technical_features(bars_df)
    result = evaluate_current_bar_short(features, params=params)

    return ShortTechnicalRegime(
        symbol=symbol,
        as_of=as_of,
        last_bar_timestamp=m51._now_iso(last_bar_dt),
        status=result["status"],
        signal_score=result["signal_score"],
        persistence_bars=result["persistence_bars"],
        rsi_14=result["rsi_14"],
        rel_volume=result["rel_volume"],
        atr_pct=result["atr_pct"],
        price_acceleration=result["price_acceleration"],
        reasons=result["reasons"],
        bars_used=len(bars_df),
        universe_version=universe_version,
        scoring_params_hash=params_hash,
    )


def is_short_technical_usable(regime: ShortTechnicalRegime) -> bool:
    m51 = load_equity_signal_module()
    return regime.status in m51.USABLE_TECHNICAL_STATUSES


# ============================================================================
# Live data fetch -- reused DIRECTLY from `.51` (M51.fetch_recent_bars /
# M51.AlpacaHistoricalBarsClient / M51.BarsClient), not redefined. `.52`
# introduces no new network code of its own -- the bars a short evaluation
# needs are identical to the bars a long evaluation needs.
# ============================================================================


def fetch_live_short_technical_regime(
    symbol: str,
    *,
    bars_client: Any,
    params: ShortTechnicalScoringParams,
    lookback_bars: int,
    universe_version: str,
    now: datetime | None = None,
) -> ShortTechnicalRegime:
    """Orchestration entry point: `.51`-reused live fetch -> `.52`'s own
    deterministic bearish evaluation. Any exception from the fetch layer
    propagates as `ShortSignalSourceError` (or is already a `.51`
    `LiveSignalSourceError`, allowed to propagate as-is -- both are
    programmer-error-only exception classes, never swallowed), mirroring
    `.51`'s own `fetch_live_technical_regime` fail-closed discipline
    exactly.
    """
    m51 = load_equity_signal_module()
    try:
        bars_df = m51.fetch_recent_bars(symbol, client=bars_client, lookback_bars=lookback_bars, end=now)
    except m51.LiveSignalSourceError:
        raise
    except Exception as exc:  # noqa: BLE001 -- deliberately captured, not swallowed silently
        raise ShortSignalSourceError(f"BARS_FETCH_FAILED:{symbol}:{exc}") from exc

    return build_short_technical_regime_from_bars(
        symbol, bars_df, params=params, universe_version=universe_version, now=now
    )
