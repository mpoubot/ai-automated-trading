#!/usr/bin/env python3
"""
AURA v0.5.3.51 — Live Alpaca equities/ETFs technical signal source.

Per every revision of the governing roadmap (v0.5.4 §5, Master Roadmap
v0.5.5 §5, Final Implementation Baseline §3), this milestone's one-line
description has always been: "the execution spine has been ready since
`.38`; this finally feeds it." `.51` closes the gap: it is the first
module in this project that computes a LIVE, current-bar equity/ETF
technical signal and delivers it as typed evidence to `.50`'s Decision
Engine, using the venue (Alpaca) `.35`-`.38` already know how to execute
against.

Architecture (per Martin's explicit `.51` GO, 2026-09-13):

    Pinned Equity/ETF Universe
             |
    Existing Technical Features (technical_agent.py, adapted)
             |
    `.51` Live Technical Signal (this module)
             |
    Typed Technical Evidence (TechnicalRegime)
             |
    `.50` Decision Engine (small additive extension -- see below)
             |
    `.44` Enforcement

`.51`'s own scope stops at producing a `TechnicalRegime` and feeding it
into `.50`'s `CandidateEvidence`/`decide()`. It does not build a
`CanonicalExecutionIntent` (that boundary belongs to `.50`, unchanged --
see `.50`'s own docstring) and it does not touch `.39`, `.44`, or the
overall roadmap sequencing.

Reuse-first audit (dedicated subagent pass, read-only, file:line cited,
performed before writing any code this milestone)
------------------------------------------------------------------------
  - `technical_agent.py` (`build_features`, `rsi`): pure, stateless,
    already-causal feature computation over a bars DataFrame (EMA 3/8/
    21/50, RSI-14, MACD/signal, Bollinger position, ATR-14/ATR%, relative
    volume, price acceleration, EMA crossover flags). Classified **A** --
    reused verbatim (copied, not imported, per this repo's established
    convention for cross-module reuse -- see `.33`/`.40`/`.44`/`.45`/
    `.49`/`.50`'s own "stable_json"/"sha256_text" helpers, each copied
    independently rather than imported). Every computation here is
    backward-looking only (`.diff()`, `.rolling()`, `.ewm()`, `.shift(1)`
    -- never a negative/forward shift) -- confirmed no-look-ahead by
    construction, and verified by a dedicated regression test below
    (`test_no_look_ahead_appending_future_bars_does_not_change_past_row`).
  - `signal_validator.py` (`evaluate_signal`): the "score the last row"
    mechanism (`df.iloc[-1]`) is a genuine, already-correct live/current-
    bar evaluation pattern -- classified **B**, mechanism reused, but
    EVERY scoring constant in the original (point values per rule, RSI
    band bounds, relative-volume threshold, whipsaw thresholds, status
    cutoffs) is hand-picked with no derivation shown, directly
    conflicting with this project's "never invent numbers" discipline
    (`.44`/`.47`/`.48`/`.49`/`.50`). Per Martin's explicit instruction,
    every one of those constants is replaced below by a field on
    `TechnicalScoringParams` -- a required, no-default, caller-supplied
    dataclass. This module supplies the MECHANISM, never the specific
    trading thresholds a real evaluation would use.
  - `historical_signal_scanner.py` (`load_data`): the only genuine,
    working Alpaca equity market-data fetch code in this repository
    (`StockHistoricalDataClient`/`StockBarsRequest`/`TimeFrame.Day`).
    Classified **B** -- the SDK call shape is reused (adapted into
    `AlpacaHistoricalBarsClient` below), but it fetches an explicit
    historical `start`/`end` range for backtesting, not "the most recent
    N bars ending now" -- `.51` adapts the same client/request shape for
    a live lookback window instead (see `fetch_recent_bars` below).
  - `historical_signal_quality_scanner_v04`-`v0471.py` series and
    `aura_regime_backtest_multi_symbol.py`: read in full. All are
    explicitly self-labeled historical-backtest/research-only, with no
    live/current-bar evaluation entry point (`discover_signals` is
    vectorized over an entire historical DataFrame, not "evaluate just
    the last row"), and `v0471.py`'s own banner labels its frozen
    entry/exit logic "PRELIMINARY". Per Martin's explicit instruction,
    these are treated as RESEARCH EVIDENCE ONLY this milestone -- `.51`
    does not adopt, optimize against, or tune parameters using any
    result from this series. Not reused directly; noted for a future
    research-validation milestone, not `.51`.
  - `multi_symbol_historical_scanner.py:28-68`: the only pinned
    equity/ETF symbol list anywhere in the codebase (27 symbols spanning
    tech, consumer/EV, financials, energy, industrials/materials,
    defense, healthcare, and index/commodity-proxy ETFs). Verified
    directly from the file (not reconstructed from memory or a prior
    report) and reused verbatim as `.51`'s pinned starting universe, per
    Martin's explicit instruction -- see `aura_v05351_equity_universe_v1.
    json`.
  - Competitor sweep (AegisAlpha, Hermes Trader, DELTAX/DELTAX-AURA/
    DELTAX V2, CAURA, BABIL, optionwright, and the rest of
    `/home/claude/work/competitors/`): AegisAlpha's `strategy/screener.py`
    (`StockLatestBarRequest`/momentum-prescreen/shortlist-to-AI pattern)
    is the closest external analog to `.51`'s own
    fetch -> deterministic-score -> shortlist -> Decision Engine shape --
    classified **B**, shape only; not reused directly because its
    momentum metric is invented/hardcoded with no derivation, and on
    fetch failure it silently falls back to a stale watchlist rather than
    failing closed -- the opposite of this project's fail-closed
    discipline (`.44`/`.47`/`.48`/`.50` all BLOCK/ABSTAIN on missing or
    stale data, never silently reuse stale state; `.51` does the same --
    see `TechnicalStatus.STALE_DATA`/`INSUFFICIENT_DATA` below). Hermes
    Trader's `pipeline/alpaca_feed/data.py`
    (`StockLatestQuoteRequest`/`get_stock_latest_quote`) confirms a
    second viable live-data SDK primitive; not adopted here because it
    fetches only a bid/ask midpoint, not the OHLCV bar history the
    reused `technical_agent.py` feature set requires. No genuine live
    equity technical-signal-generation module was found anywhere in the
    competitor corpus -- `.51`'s core evaluation logic is greenfield,
    built from the reused pieces above, not imported from a competitor.

Martin's scoping decisions (this session, 2026-09-13) and how each was
implemented
------------------------------------------------------------------------
  1. **`.51` -> `.50` integration: feed `.50` directly, same standard as
     `.46`/`.47`/`.48`.** No `.39` involvement -- `.39` is unchanged, per
     explicit instruction. See "`.50` integration changes" below for the
     exact, small, additive extension made to `.50`.
  2. **Technical signal basis: adapt `technical_agent.py` +
     `signal_validator.py`.** Implemented exactly as described in the
     audit above -- features reused verbatim, scoring mechanism reused,
     every constant replaced by a required `TechnicalScoringParams`
     field. `.51` provides a genuine deterministic current-bar evaluation
     function (`evaluate_current_bar`), not a historical scanner.
  3. **Equity/ETF universe: the verified 27-symbol
     `multi_symbol_historical_scanner.py` list, configuration-driven and
     versioned.** Stored in `aura_v05351_equity_universe_v1.json`
     (version `"v1"`, with its source cited in-file), loaded by
     `load_pinned_universe()` below -- not hardcoded into this module,
     and not dynamically discovered. This is a RESEARCH STARTING
     UNIVERSE, not a claim that any symbol here is validated for live
     trading (matching the disclosed-limitation pattern `.42`/`.43`
     already use for their own frozen candidates).
  4. **Architecture: narrowly scoped, no `.39`/`.44`/`.50`/roadmap
     redesign.** `.50`'s change is the single small additive extension
     described below; `.39` and `.44` are untouched; nothing here alters
     milestone sequencing.

`.50` integration changes (the ONLY changes made to `aura_v05350_
decision_engine.py` this milestone -- everything else in `.50` is
unmodified)
------------------------------------------------------------------------
  1. `CandidateEvidence` gains two new fields: `technical_regime: Any |
     None` (a `.51` `TechnicalRegime` instance, duck-typed, exactly
     mirroring how `sentiment_regime`/`wave_result` are already duck-
     typed) and `technical_usable: bool`.
  2. `build_candidate_evidence()` gains one new optional parameter,
     `technical_regime: Any | None = None`, defaulting to `None` so
     every existing caller (including all of `.50`'s own 51 prior tests)
     is unaffected -- calling it without this argument produces
     functionally identical behavior to before this change. A candidate
     is `technical_usable` only when its own internal quality bar is
     met (`status in {"CONFIRMING", "CONFIRMED"}` -- see
     `TechnicalRegime` below), mirroring `.47`/`.48`'s "usable means
     passed its OWN quality bar, not merely present" discipline exactly.
  3. `compute_base_rank_score()` gains one new REQUIRED (no default)
     parameter, `technical_weight: float`, and one new weighted term:
     `score += technical_weight * (technical_regime.signal_score /
     100.0)` when `technical_usable` -- added ONLY when usable, and
     ALWAYS added (never subtracted), because `.51` is explicitly scoped
     LONG-only this milestone (Martin's explicit decision -- `.52`
     remains the separate short-side milestone). This preserves
     `.50`'s "direction is decided before penalties are applied"
     invariant unchanged: a purely-technical LONG_LEANING candidate
     still cannot become SHORT_LEANING from any penalty, exactly as
     before.
  4. `decide()` gains the same new required `technical_weight`
     parameter, threaded straight through to `compute_base_rank_score`.
     No other logic in `decide()` changes.
  5. `.50`'s existing 51 tests were updated to pass `technical_weight=
     0.0` at each of their `decide()`/`compute_base_rank_score()` call
     sites (an explicit, caller-supplied "technical evidence not in use
     here" value for tests that predate this milestone and are not
     testing technical integration -- never a code-level default).
     `.50`'s FULL PRIOR TEST SUITE (all 51 tests) passes unmodified in
     behavior after this change -- only the explicit `technical_weight=
     0.0` argument was added to each call site, nothing else.
  No other `.50` function, dataclass, or docstring section was touched.
  `run_deterministic_critic`, `TradingDecision`, `source_kind` handling,
  and the `.49` AI-proposal/challenge integration are all completely
  unmodified.

Why this is additive, not invasive
------------------------------------------------------------------------
Every existing `.50` caller that does not know about `.51` continues to
work exactly as it did before -- `technical_regime` defaults to `None`,
`technical_usable` becomes `False`, and the new score term contributes
nothing. This mirrors exactly how `.48`'s `wave_result` was added
alongside `.47`'s pre-existing `sentiment_regime` when `.50` was first
built -- a new, independent, optional evidence dimension, combined only
by addition, never by replacing or reinterpreting an existing one.

`TechnicalRegime` design
------------------------------------------------------------------------
`.51` is scoped LONG-only this milestone (Martin's explicit decision).
Accordingly `TechnicalRegime.signal_score` is an UNSIGNED bullishness
magnitude in `[0, 100]` (reused directly from `signal_validator.py`'s
own `[0, 100]` clamp, now driven by caller-supplied point weights rather
than hardcoded ones) -- there is no bearish/short scoring path anywhere
in this module. `.50`'s new technical term is therefore always additive
when usable, never subtractive, consistent with `.51`'s long-only scope.
A future `.52` (or a `.51` amendment, if Martin later chooses to fold
short-side in) would need its own signed convention; not built here.

Fail-closed / freshness discipline
------------------------------------------------------------------------
`TechnicalRegime.status` is one of `INSUFFICIENT_DATA` (fewer than
`min_bars_required` clean bars, or any NaN in a value the evaluation
actually reads), `STALE_DATA` (the last bar is older than
`max_bar_age_seconds`), `EARLY`/`CONFIRMING`/`CONFIRMED`/`FAILED`
(genuine evaluation outcomes, reused from `signal_validator.py`'s own
status vocabulary). Only `CONFIRMING`/`CONFIRMED` are `technical_usable`
by `.50`. `INSUFFICIENT_DATA`/`STALE_DATA`/`FAILED` are recorded (visible
for audit, exactly like `.47`/`.48`'s own below-bar sources) but
contribute nothing to `.50`'s score -- ambiguity/inadequacy is preserved,
never silently collapsed into a false signal, matching `.48`'s own
discipline one layer up.

No look-ahead / no repainting
------------------------------------------------------------------------
`build_features` (reused from `technical_agent.py`) computes every
indicator using only `.diff()`/`.rolling()`/`.ewm()`/`.shift(1)` --
strictly backward-looking. `evaluate_current_bar` reads only `df.iloc
[-1]` and trailing `.tail(...)` windows of whatever DataFrame it is
given. Because production code always evaluates the LAST row of
whatever bars were fetched "as of now", a row's own computed values can
never change once bars after it exist -- proven directly by
`test_no_look_ahead_appending_future_bars_does_not_change_past_row`
below (computes features over a truncated bars window vs. the same
window plus future bars, and asserts every computed value for the
shared, older row is bit-for-bit identical).

Persistence
------------------------------------------------------------------------
None. `build_technical_regime_from_bars` (the deterministic core) is a
pure function of a caller-supplied bars DataFrame and required
parameters, mirroring `.47`/`.48`/`.49`/`.50`'s own precedent. The live
fetch layer (`fetch_recent_bars`/`AlpacaHistoricalBarsClient`) is
isolated to two clearly-marked functions, exactly like `.49`'s
`AnthropicClient` isolation -- every other function in this module is
network-free and directly testable.

Known limitations (disclosed, not silently worked around)
------------------------------------------------------------------------
  - No live smoke test against a real Alpaca market-data call was run in
    this sandboxed environment (no live credentials configured here) --
    matches the exact disclosed-limitation pattern `.35`/`.49` already
    use for their own live SDK integrations. `AlpacaHistoricalBarsClient`
    is unit-tested against a fake client shaped like the real SDK's
    response object (`.df`, a `(symbol, timestamp)`-MultiIndex
    DataFrame), not against a live API call.
  - `TechnicalScoringParams` has ~20 required fields (no defaults) --
    deliberately verbose rather than a small number of coarse weights,
    because Martin's explicit instruction was to replace EVERY
    undocumented constant in `signal_validator.py`'s original scoring
    logic with an explicit parameter, not to redesign the scoring
    mechanism into something coarser. This module supplies no default
    values for any of them -- a real evaluation run requires a caller
    (a future orchestration milestone, or Martin directly) to supply
    genuine, derived research values.
  - `.51` is LONG-only this milestone (Martin's explicit decision,
    `.52` remains separate for the short side) -- `TechnicalRegime` has
    no bearish/short scoring path, and `.50`'s new technical term is
    always additive.
  - The `historical_signal_quality_scanner_v04`-`v0471.py` series'
    walk-forward-validated entry/exit logic is NOT what `.51` evaluates
    live -- per Martin's explicit instruction, `.51` does not optimize
    or tune its own scoring basis using that series' historical results
    this milestone. If Martin later wants `.51`'s live evaluation
    upgraded to match that more rigorous, validated basis, that is a
    deliberate, separate, explicitly-approved change -- not something
    this milestone did on its own initiative.
  - This module has no scheduled/orchestrated entry point of its own
    (mirrors `.43`'s own disclosed limitation) -- wiring a periodic live
    evaluation run (and where its universe config/lookback state lives
    in a real deployment) is left to a future orchestration milestone.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

VERSION = "AURA v0.5.3.51"
ENGINE = "LIVE_EQUITY_TECHNICAL_SIGNAL_SOURCE"
SCHEMA_VERSION = "1.0"

ROOT = Path(__file__).resolve().parent
DEFAULT_UNIVERSE_CONFIG_PATH = ROOT / "aura_v05351_equity_universe_v1.json"

TECHNICAL_STATUSES = frozenset(
    {"INSUFFICIENT_DATA", "STALE_DATA", "EARLY", "CONFIRMING", "CONFIRMED", "FAILED"}
)
USABLE_TECHNICAL_STATUSES = frozenset({"CONFIRMING", "CONFIRMED"})

REQUIRED_BAR_COLUMNS = ("open", "high", "low", "close", "volume")


class LiveSignalSourceError(Exception):
    """Raised for programmer-error / required-parameter / structurally-
    malformed-input violations only -- never for a symbol simply lacking
    enough clean bars or having a stale feed (those are expected,
    handled outcomes: TechnicalRegime.status == INSUFFICIENT_DATA /
    STALE_DATA), mirroring `.47`'s/`.49`'s/`.50`'s own error-class
    discipline.
    """


# ============================================================================
# Stable JSON / hashing helpers -- copied, not imported, per this repo's
# established convention (see `.33`/`.40`/`.44`/`.45`/`.49`/`.50`).
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
# Pinned equity/ETF universe -- configuration-driven and versioned, per
# Martin's explicit instruction. Verified directly from
# multi_symbol_historical_scanner.py:28-68 (see module docstring), not
# reconstructed from memory. This is a research starting universe, not a
# live-trading validation claim.
# ============================================================================


@dataclass(frozen=True, slots=True)
class PinnedUniverse:
    version: str
    symbols: tuple[str, ...]
    source: str


def load_pinned_universe(config_path: Path | str = DEFAULT_UNIVERSE_CONFIG_PATH) -> PinnedUniverse:
    """Loads the pinned universe from its versioned JSON config file.
    Fails closed (raises `LiveSignalSourceError`) on a missing file,
    malformed JSON, empty symbol list, or a duplicate symbol -- a
    corrupted universe config must never silently produce a partial or
    wrong universe.
    """
    path = Path(config_path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LiveSignalSourceError(f"UNIVERSE_CONFIG_UNREADABLE:{path}:{exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LiveSignalSourceError(f"UNIVERSE_CONFIG_MALFORMED_JSON:{path}:{exc}") from exc

    version = data.get("version")
    symbols = data.get("symbols")
    source = data.get("source", "")
    if not isinstance(version, str) or not version:
        raise LiveSignalSourceError(f"UNIVERSE_CONFIG_MISSING_VERSION:{path}")
    if not isinstance(symbols, list) or not symbols or not all(isinstance(s, str) and s for s in symbols):
        raise LiveSignalSourceError(f"UNIVERSE_CONFIG_INVALID_SYMBOLS:{path}")
    if len(set(symbols)) != len(symbols):
        raise LiveSignalSourceError(f"UNIVERSE_CONFIG_DUPLICATE_SYMBOLS:{path}")

    return PinnedUniverse(version=version, symbols=tuple(symbols), source=source)


# ============================================================================
# Technical feature computation -- reused verbatim (copied, not imported)
# from technical_agent.py. Strictly backward-looking: `.diff()`,
# `.rolling()`, `.ewm()`, `.shift(1)` only -- no look-ahead, no
# repainting. Requires pandas; imported lazily inside functions so this
# module can still be imported (e.g. for the universe config / dataclass
# definitions) in an environment where pandas is briefly unavailable,
# mirroring how `.35`'s alpaca import is only exercised at call time.
# ============================================================================


def _rsi(close, period: int = 14):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


def build_technical_features(df: Any) -> Any:
    """Reused verbatim from `technical_agent.py`'s `build_features`
    (copied, not imported, per this repo's convention) -- EMA 3/8/21/50,
    RSI-14, MACD/signal, Bollinger-band position, ATR-14/ATR%, relative
    volume (vs. its own trailing 20-bar average), price acceleration,
    and EMA crossover flags. Every computation reads only the current
    row and prior rows -- no look-ahead.
    """
    import numpy as np

    out = df.copy()
    close = out["close"]
    volume = out["volume"]

    out["ema_3"] = close.ewm(span=3, adjust=False).mean()
    out["ema_8"] = close.ewm(span=8, adjust=False).mean()
    out["ema_21"] = close.ewm(span=21, adjust=False).mean()
    out["ema_50"] = close.ewm(span=50, adjust=False).mean()

    out["rsi_14"] = _rsi(close)

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    out["macd"] = ema12 - ema26
    out["macd_signal"] = out["macd"].ewm(span=9, adjust=False).mean()

    mid = close.rolling(20).mean()
    std = close.rolling(20).std()
    out["bb_upper"] = mid + 2 * std
    out["bb_lower"] = mid - 2 * std
    width = (out["bb_upper"] - out["bb_lower"]).replace(0, np.nan)
    out["bb_position"] = (close - out["bb_lower"]) / width

    prev = close.shift(1)
    import pandas as pd

    tr = pd.concat(
        [out["high"] - out["low"], (out["high"] - prev).abs(), (out["low"] - prev).abs()],
        axis=1,
    ).max(axis=1)
    out["atr_14"] = tr.rolling(14).mean()
    out["atr_pct"] = out["atr_14"] / close * 100

    out["rel_volume"] = volume / volume.rolling(20).mean().replace(0, np.nan)

    out["return_1"] = close.pct_change()
    out["price_acceleration"] = out["return_1"].diff()

    out["cross_3_8"] = (out["ema_3"] > out["ema_8"]) & (out["ema_3"].shift(1) <= out["ema_8"].shift(1))
    out["cross_8_21"] = (out["ema_8"] > out["ema_21"]) & (out["ema_8"].shift(1) <= out["ema_21"].shift(1))
    out["cross_21_50"] = (out["ema_21"] > out["ema_50"]) & (out["ema_21"].shift(1) <= out["ema_50"].shift(1))
    return out


# ============================================================================
# Scoring parameters -- EVERY constant from signal_validator.py's
# original scoring logic, replaced by a required (no-default) field.
# This module supplies the mechanism; a caller supplies genuine,
# research-derived magnitudes. Never invent numbers.
# ============================================================================


@dataclass(frozen=True, slots=True)
class TechnicalScoringParams:
    min_bars_required: int
    max_bar_age_seconds: float
    ema3_cross_ema8_points: float
    ema3_above_ema8_points: float
    ema8_above_ema21_points: float
    ema21_above_ema50_points: float
    macd_bullish_points: float
    rsi_constructive_low: float
    rsi_constructive_high: float
    rsi_constructive_points: float
    rsi_extended_threshold: float
    rsi_extended_penalty_points: float
    rel_volume_threshold: float
    rel_volume_points: float
    price_acceleration_points: float
    persistence_lookback_bars: int
    persistence_min_bars_for_bonus: int
    persistence_points: float
    whipsaw_prior_move_threshold: float
    whipsaw_now_move_threshold: float
    whipsaw_penalty_points: float
    score_floor: float
    score_ceiling: float
    early_status_max_persistence: int
    confirmed_score_threshold: float
    confirmed_min_persistence: int
    confirming_score_threshold: float

    def __post_init__(self) -> None:
        if self.min_bars_required <= 0:
            raise LiveSignalSourceError("INVALID_MIN_BARS_REQUIRED:must be > 0")
        if self.max_bar_age_seconds <= 0:
            raise LiveSignalSourceError("INVALID_MAX_BAR_AGE_SECONDS:must be > 0")
        if self.rsi_constructive_low > self.rsi_constructive_high:
            raise LiveSignalSourceError("INVALID_RSI_CONSTRUCTIVE_BAND:low must be <= high")
        if self.score_floor > self.score_ceiling:
            raise LiveSignalSourceError("INVALID_SCORE_BOUNDS:floor must be <= ceiling")
        if self.persistence_lookback_bars <= 0:
            raise LiveSignalSourceError("INVALID_PERSISTENCE_LOOKBACK_BARS:must be > 0")


# ============================================================================
# Current-bar evaluation -- adapted from signal_validator.py's
# evaluate_signal(). Reads ONLY df.iloc[-1] and trailing .tail(...)
# windows -- no look-ahead. Every scoring constant is now a required
# TechnicalScoringParams field; no defaults exist anywhere in this
# function.
# ============================================================================


def evaluate_current_bar(features_df: Any, *, params: TechnicalScoringParams) -> dict[str, Any]:
    """Deterministic, LLM-free, live/current-bar evaluation. Returns a
    plain dict (wrapped into a typed `TechnicalRegime` by
    `build_technical_regime_from_bars` below) so this function stays a
    direct, minimally-adapted descendant of `signal_validator.
    evaluate_signal` for reviewability.
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
    required_fields = ("close", "high", "low", "cross_3_8", "ema_3", "ema_8", "ema_21", "ema_50", "macd", "macd_signal", "rsi_14", "rel_volume", "atr_pct", "price_acceleration")
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

    score = 0.0
    reasons: list[str] = []

    if bool(row["cross_3_8"]):
        score += params.ema3_cross_ema8_points
        reasons.append("EMA3 crossed above EMA8")
    if row["ema_3"] > row["ema_8"]:
        score += params.ema3_above_ema8_points
        reasons.append("EMA3 remains above EMA8")
    if row["ema_8"] > row["ema_21"]:
        score += params.ema8_above_ema21_points
        reasons.append("EMA8 > EMA21")
    if row["ema_21"] > row["ema_50"]:
        score += params.ema21_above_ema50_points
        reasons.append("EMA21 > EMA50")
    if row["macd"] > row["macd_signal"]:
        score += params.macd_bullish_points
        reasons.append("MACD bullish")
    if params.rsi_constructive_low <= row["rsi_14"] <= params.rsi_constructive_high:
        score += params.rsi_constructive_points
        reasons.append("RSI constructive")
    elif row["rsi_14"] > params.rsi_extended_threshold:
        score -= params.rsi_extended_penalty_points
        reasons.append("RSI extremely extended")
    if row["rel_volume"] >= params.rel_volume_threshold:
        score += params.rel_volume_points
        reasons.append(f"Relative volume >= {params.rel_volume_threshold}x")
    if row["price_acceleration"] > 0:
        score += params.price_acceleration_points
        reasons.append("Positive acceleration")

    tail_persist = features_df[["ema_3", "ema_8"]].tail(params.persistence_lookback_bars)
    persistence = int((tail_persist["ema_3"] > tail_persist["ema_8"]).sum())
    if persistence >= params.persistence_min_bars_for_bonus:
        score += params.persistence_points
        reasons.append(f"EMA3/8 persisted {params.persistence_lookback_bars} bars")

    recent = features_df["close"].tail(4)
    if len(recent) == 4 and not any(_is_missing(v) for v in recent):
        prev_move = recent.iloc[-2] / recent.iloc[-3] - 1
        now_move = recent.iloc[-1] / recent.iloc[-2] - 1
        if prev_move > params.whipsaw_prior_move_threshold and now_move < -params.whipsaw_now_move_threshold:
            score -= params.whipsaw_penalty_points
            reasons.append("Possible momentum reversal / whipsaw")

    score = max(params.score_floor, min(params.score_ceiling, score))

    if bool(row["cross_3_8"]) and persistence <= params.early_status_max_persistence:
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


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(value != value)  # NaN != NaN
    except Exception:
        return False


# ============================================================================
# TechnicalRegime -- the .50-compatible typed evidence output. Symbol/
# venue-agnostic field shape, mirroring `.47`'s SentimentRegime and
# `.48`'s ElliottWaveResearchResult (bare `symbol: str`, no venue field).
# ============================================================================


@dataclass(frozen=True, slots=True)
class TechnicalRegime:
    symbol: str
    as_of: str  # evaluation time (when this regime was computed)
    last_bar_timestamp: str | None  # the actual last bar's own timestamp, distinct from as_of
    status: str  # one of TECHNICAL_STATUSES
    signal_score: float  # unsigned bullishness magnitude, [score_floor, score_ceiling] -- LONG-only this milestone
    persistence_bars: int
    rsi_14: float | None
    rel_volume: float | None
    atr_pct: float | None
    price_acceleration: float | None
    reasons: tuple[str, ...]
    bars_used: int
    universe_version: str
    scoring_params_hash: str  # binds this regime to the exact parameters used to produce it

    def __post_init__(self) -> None:
        if self.status not in TECHNICAL_STATUSES:
            raise LiveSignalSourceError(f"INVALID_TECHNICAL_STATUS:{self.status}")


def _scoring_params_hash(params: TechnicalScoringParams) -> str:
    return sha256_text(
        stable_json(
            {
                field: getattr(params, field)
                for field in (
                    "min_bars_required", "max_bar_age_seconds", "ema3_cross_ema8_points", "ema3_above_ema8_points",
                    "ema8_above_ema21_points", "ema21_above_ema50_points", "macd_bullish_points",
                    "rsi_constructive_low", "rsi_constructive_high", "rsi_constructive_points",
                    "rsi_extended_threshold", "rsi_extended_penalty_points", "rel_volume_threshold",
                    "rel_volume_points", "price_acceleration_points", "persistence_lookback_bars",
                    "persistence_min_bars_for_bonus", "persistence_points", "whipsaw_prior_move_threshold",
                    "whipsaw_now_move_threshold", "whipsaw_penalty_points", "score_floor", "score_ceiling",
                    "early_status_max_persistence", "confirmed_score_threshold", "confirmed_min_persistence",
                    "confirming_score_threshold",
                )
            }
        )
    )


def _validate_bars_frame(bars_df: Any) -> None:
    """Structural validation only -- a well-formed fetch should never
    produce a frame missing these columns or without a valid index; if
    it does, that is a caller/integration error (raises), distinct from
    a symbol simply having too few or too-stale CLEAN bars (a typed,
    non-raising TechnicalRegime.status outcome, handled below).
    """
    if bars_df is None:
        raise LiveSignalSourceError("BARS_FRAME_IS_NONE")
    missing = [c for c in REQUIRED_BAR_COLUMNS if c not in getattr(bars_df, "columns", [])]
    if missing:
        raise LiveSignalSourceError(f"BARS_FRAME_MISSING_COLUMNS:{missing}")


def build_technical_regime_from_bars(
    symbol: str,
    bars_df: Any,
    *,
    params: TechnicalScoringParams,
    universe_version: str,
    now: datetime | None = None,
) -> TechnicalRegime:
    """The deterministic, network-free core: bars DataFrame -> features
    -> current-bar evaluation -> typed, fail-closed, freshness-checked
    TechnicalRegime. Pure function of its inputs; no I/O.
    """
    _validate_bars_frame(bars_df)
    now_dt = now or datetime.now(timezone.utc)
    as_of = _now_iso(now_dt)
    params_hash = _scoring_params_hash(params)

    if len(bars_df) == 0:
        return TechnicalRegime(
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
        return TechnicalRegime(
            symbol=symbol, as_of=as_of, last_bar_timestamp=_now_iso(last_bar_dt), status="STALE_DATA",
            signal_score=0.0, persistence_bars=0, rsi_14=None, rel_volume=None, atr_pct=None,
            price_acceleration=None, reasons=("last bar is future-dated",), bars_used=len(bars_df),
            universe_version=universe_version, scoring_params_hash=params_hash,
        )
    if bar_age_seconds > params.max_bar_age_seconds:
        return TechnicalRegime(
            symbol=symbol, as_of=as_of, last_bar_timestamp=_now_iso(last_bar_dt), status="STALE_DATA",
            signal_score=0.0, persistence_bars=0, rsi_14=None, rel_volume=None, atr_pct=None,
            price_acceleration=None,
            reasons=(f"last bar age {bar_age_seconds:.0f}s exceeds max_bar_age_seconds {params.max_bar_age_seconds}",),
            bars_used=len(bars_df), universe_version=universe_version, scoring_params_hash=params_hash,
        )

    features = build_technical_features(bars_df)
    result = evaluate_current_bar(features, params=params)

    return TechnicalRegime(
        symbol=symbol,
        as_of=as_of,
        last_bar_timestamp=_now_iso(last_bar_dt),
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


def is_technical_usable(regime: TechnicalRegime) -> bool:
    return regime.status in USABLE_TECHNICAL_STATUSES


# ============================================================================
# Live data fetch -- adapted from historical_signal_scanner.py's
# load_data() (the only genuine, working Alpaca equity market-data fetch
# code in this repo). Isolated to these two functions/classes so
# everything else in this module stays network-free and directly
# testable, mirroring `.49`'s AnthropicClient isolation.
# ============================================================================


class BarsClient(Protocol):
    def get_stock_bars(self, request: Any) -> Any: ...


class AlpacaHistoricalBarsClient:
    """Thin wrapper over alpaca-py's StockHistoricalDataClient, adapted
    from historical_signal_scanner.py:18-24. No credentials are read or
    validated at import time -- only when actually constructed.
    """

    def __init__(self, api_key: str, secret_key: str) -> None:
        from alpaca.data.historical import StockHistoricalDataClient

        self._client = StockHistoricalDataClient(api_key, secret_key)

    def get_stock_bars(self, request: Any) -> Any:
        return self._client.get_stock_bars(request)


def fetch_recent_bars(
    symbol: str,
    *,
    client: BarsClient,
    lookback_bars: int,
    end: datetime | None = None,
    calendar_buffer_days: int = 12,
) -> Any:
    """Fetches recent daily bars for `symbol` ending at `end` (default
    now), requesting enough calendar days to cover `lookback_bars`
    trading days plus a fixed weekend/holiday buffer, then trims to
    exactly the last `lookback_bars` rows actually returned.
    `calendar_buffer_days` is a purely mechanical calendar-conversion
    margin (not a trading/strategy magnitude) -- kept as an explicit,
    named, overridable parameter rather than an invented number baked
    silently into the date math.
    """
    if lookback_bars <= 0:
        raise LiveSignalSourceError("INVALID_LOOKBACK_BARS:must be > 0")

    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    end_dt = end or datetime.now(timezone.utc)
    # Trading days are ~5/7 of calendar days; over-fetch generously and trim.
    calendar_days = int(lookback_bars * 7 / 5) + calendar_buffer_days
    start_dt = end_dt - timedelta(days=calendar_days)

    request = StockBarsRequest(
        symbol_or_symbols=[symbol],
        timeframe=TimeFrame.Day,
        start=start_dt,
        end=end_dt,
        feed=DataFeed.IEX,
    )
    response = client.get_stock_bars(request)
    df = response.df
    if df is None or len(df) == 0:
        raise LiveSignalSourceError(f"NO_BARS_RETURNED:{symbol}")
    if hasattr(df.index, "levels") and len(df.index.levels) > 1:
        df = df.xs(symbol, level="symbol")
    df = df.sort_index()
    return df.tail(lookback_bars)


def fetch_live_technical_regime(
    symbol: str,
    *,
    bars_client: BarsClient,
    params: TechnicalScoringParams,
    lookback_bars: int,
    universe_version: str,
    now: datetime | None = None,
) -> TechnicalRegime:
    """Orchestration entry point: live fetch -> deterministic evaluation.
    Any exception from the fetch layer propagates as
    `LiveSignalSourceError` (or is already one) -- this function does
    not silently fall back to stale data or swallow fetch failures,
    unlike the AegisAlpha reference pattern explicitly rejected in the
    reuse-first audit above.
    """
    try:
        bars_df = fetch_recent_bars(symbol, client=bars_client, lookback_bars=lookback_bars, end=now)
    except LiveSignalSourceError:
        raise
    except Exception as exc:  # noqa: BLE001 -- deliberately captured, not swallowed silently
        raise LiveSignalSourceError(f"BARS_FETCH_FAILED:{symbol}:{exc}") from exc

    return build_technical_regime_from_bars(symbol, bars_df, params=params, universe_version=universe_version, now=now)
