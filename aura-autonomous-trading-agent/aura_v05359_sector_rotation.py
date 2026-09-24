#!/usr/bin/env python3
"""
AURA v0.5.3.59 -- Sector rotation regime engine.

WHY THIS MODULE EXISTS
------------------------------------------------------------------------
Confirmed by two independent full-repo greps (this project's own System
Inventory & Gap Analysis, 2026-09-24, and the codebase survey performed
to write this module): sector rotation has NEVER been implemented
anywhere in either AURA track. It existed only as a Level-1
"alternative-intelligence" concept in the 2026-09-08 governance doc and
an unbuilt strategy family in the 2026-09-10 roadmap. This module is the
first working implementation.

GOVERNANCE NOTE -- read before touching this file
------------------------------------------------------------------------
The algorithm below is adapted from two source repositories Martin
explicitly authorized as a DIRECT IMPLEMENTATION SOURCE for this specific
build (`AskUserQuestion`, 2026-09-24, "Relax the rule for this build --
adapt/port DELTAX code directly"). This SUPERSEDES, for this module only,
the standing non-negotiable rule recorded in
`AURA_DELTAX_MEXC_canonical_architecture_and_governance_2026-09-08.md`
("DELTAX / DELTAX v2 are research references only, not implementation
sources"). That standing rule still governs every OTHER file in this
repository -- this exception is scoped to sector-rotation logic only,
per this specific authorization, not a blanket reopening of the rule.

Sources adapted:
  - `deltax/rotation.py` (DELTAX V1, `pautax007/DELTAX` lineage, MIT-
    licensed): the core algorithm below -- relative-strength-vs-benchmark
    ranking, a two-signal regime gate (benchmark-vs-safe-haven momentum,
    plus a cyclical/defensive ratio vs. its own moving average), and
    subsector amplification -- is a direct, disclosed adaptation of this
    file's `roc()`, `rank_sectors()`, `regime()`, `defensive_switch()`,
    and `best_subsector()` functions. Reimplemented in AURA's own coding
    conventions (frozen dataclasses, required-parameter discipline, no
    hardcoded universe/thresholds -- see below), not copy-pasted, but the
    algorithm itself -- the formulas and the gating logic -- is
    deliberately the same one DELTAX V1 uses, per Martin's explicit
    instruction to port it directly rather than re-derive from scratch.
  - `helpers/etf_rotation_scanner.py` (DELTAX_v2, `mpoubot/DELTAX_v2`):
    contributed the idea of confirming a rotation call with more than one
    independent price feature rather than a single ROC number (see
    `_confirmation_features` below) -- adapted in spirit, not copied
    (DELTAX_v2's version is a standalone 5-feature vote with no regime
    gate; this module folds a lighter 2-feature confirmation into the
    DELTAX V1 regime-gated design instead of running two separate
    systems).

DELTAX V1's OWN DISCLOSED CAVEAT -- carried forward deliberately
------------------------------------------------------------------------
`rotation.py`'s own docstring states that true sector rotation "takes
weeks to unfold," and that a 5-session lookback makes this "a momentum
tilt on a rotation signal, not a rotation strategy." That caveat is
carried forward here unchanged: this module has NOT been backtested,
walk-forward validated, or promoted through AURA's own Strategy Registry
(`.339`) evidence gate. It is research-track evidence only -- see
`aura_v05360_research_full_evidence_builder.py` for how it's wired into
`.350`'s evidence model, and that module's own docstring for why it does
NOT reach the live `.356`/`.357` Stage 3 chain.

WHAT THIS MODULE COMPUTES
------------------------------------------------------------------------
Given OHLCV bars for a benchmark, a set of safe-haven tickers, a
configured sector/subsector universe, and a defensive-ratio pair, this
module answers ONE question per candidate symbol: "is this symbol
currently a rotation leader, a laggard, a favored safe haven, or not
applicable at all?" -- expressed as a `SectorRotationRegime` with a
signed `rotation_score` in [-1, 1], directly consumable as a new,
additive evidence dimension by `.350`'s `compute_base_rank_score`
(see that module's `sector_rotation_weight`/`sector_rotation_regime`
addendum, added alongside this module).

This module does NOT select a portfolio or place any order. Like `.48`
(Elliott Wave), it is a pure, stateless research function: same bars in,
same result out, no I/O, no network call, no randomness.

DESIGN CHOICES DELIBERATELY DIFFERENT FROM DELTAX V1
------------------------------------------------------------------------
  - The sector/subsector universe, benchmark symbol, safe-haven symbols,
    defensive-ratio pair, lookback, top-N, and the `score_scale` factor
    that turns a relative-strength number into a bounded [-1, 1] score
    are ALL required, caller-supplied parameters -- never hardcoded
    module constants (DELTAX V1 hardcodes its own universe as module-
    level constants). This matches this project's "never invent numbers"
    discipline (`.47`/`.48`/`.51`/`.52`): the specific sector list and
    scaling used in any real evaluation are a research decision for the
    caller to make and disclose, not something baked silently into this
    module.
  - Fail-closed throughout: missing benchmark data, missing safe-haven
    data insufficient to evaluate the regime-gate's confirming condition,
    or a symbol outside the configured universe all resolve to
    `NO_DATA`/`NOT_APPLICABLE` (usable=False), never a guessed regime.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

VERSION = "AURA v0.5.3.59"

MARKET_REGIMES = frozenset({"RISK_ON", "RISK_OFF", "UNKNOWN"})
ROTATION_TIERS = frozenset(
    {
        "LEADER",  # RISK_ON, in configured universe, rs > 0, ranked in top_n
        "RANKED_NOT_TOP",  # RISK_ON, rs > 0, but outside top_n
        "LAGGARD",  # rs <= 0 (either regime)
        "SAFE_HAVEN_FAVORED",  # RISK_OFF, symbol is a configured safe haven
        "SAFE_HAVEN_NOT_FAVORED",  # RISK_ON, symbol is a configured safe haven
        "NOT_FAVORED_RISK_OFF",  # RISK_OFF, symbol is a non-haven universe member with rs > 0 (regime overrides raw strength)
        "NOT_APPLICABLE",  # symbol is outside the configured universe and not a safe haven
        "NO_DATA",  # insufficient bars, or regime is UNKNOWN
    }
)


class SectorRotationError(Exception):
    """Raised for programmer-error / required-parameter violations only --
    never for a symbol simply lacking data or being outside the
    configured universe (those are expected, handled outcomes: NO_DATA /
    NOT_APPLICABLE), mirroring `.47`/`.48`/`.50`'s own error-class
    discipline.
    """


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def _extract_closes(bars: list[dict[str, Any]]) -> list[float]:
    return [float(b["close"]) for b in bars]


def roc(bars: list[dict[str, Any]], lookback: int) -> float | None:
    """Rate of change over `lookback` bars: closes[-1] / closes[-1-lookback] - 1.
    Adapted directly from DELTAX V1 `rotation.py::roc()`. Returns `None`
    (never a guessed value) if there isn't enough history or the anchor
    close is zero.
    """
    if lookback <= 0:
        raise SectorRotationError("INVALID_LOOKBACK:must be > 0")
    closes = _extract_closes(bars)
    if len(closes) < lookback + 1:
        return None
    anchor = closes[-1 - lookback]
    if anchor == 0:
        return None
    return closes[-1] / anchor - 1.0


def _confirmation_features(bars: list[dict[str, Any]], benchmark_bars: list[dict[str, Any]]) -> tuple[int, int]:
    """Lightweight adaptation of DELTAX_v2 `etf_rotation_scanner.py`'s
    multi-feature confirmation idea: count how many of two independent,
    cheap-to-compute features point the same direction as the primary ROC
    signal, rather than trusting a single number. Returns
    (bullish_feature_count, bearish_feature_count) out of 2 features:
    (a) latest-bar directional move (close > open), and (b) relative
    strength vs. the benchmark's own latest-bar move. Missing data on
    either feature simply excludes it from the count (never guessed).
    """
    bullish = 0
    bearish = 0
    if bars and "open" in bars[-1] and "close" in bars[-1]:
        last = bars[-1]
        if float(last["close"]) > float(last["open"]):
            bullish += 1
        elif float(last["close"]) < float(last["open"]):
            bearish += 1
    if bars and benchmark_bars and "close" in bars[-1] and len(bars) >= 2 and len(benchmark_bars) >= 2:
        own_move = float(bars[-1]["close"]) / float(bars[-2]["close"]) - 1.0 if bars[-2]["close"] else None
        bench_move = float(benchmark_bars[-1]["close"]) / float(benchmark_bars[-2]["close"]) - 1.0 if benchmark_bars[-2]["close"] else None
        if own_move is not None and bench_move is not None:
            if own_move > bench_move:
                bullish += 1
            elif own_move < bench_move:
                bearish += 1
    return bullish, bearish


def rank_sectors(
    universe_bars: dict[str, list[dict[str, Any]]],
    benchmark_bars: list[dict[str, Any]],
    *,
    lookback: int,
) -> tuple[tuple[str, float], ...]:
    """Rank every universe member by relative strength (own ROC minus
    benchmark ROC), descending. Adapted directly from DELTAX V1
    `rotation.py::rank_sectors()`. Members with insufficient data, or
    whose relative strength can't be computed because the benchmark
    itself lacks data, are excluded (never assigned a guessed rank).
    """
    bench_roc = roc(benchmark_bars, lookback)
    if bench_roc is None:
        return ()
    ranked: list[tuple[str, float]] = []
    for symbol, bars in universe_bars.items():
        sym_roc = roc(bars, lookback)
        if sym_roc is None:
            continue
        ranked.append((symbol, sym_roc - bench_roc))
    ranked.sort(key=lambda pair: pair[1], reverse=True)
    return tuple(ranked)


def market_regime(
    benchmark_bars: list[dict[str, Any]],
    safe_haven_bars: dict[str, list[dict[str, Any]]],
    *,
    lookback: int,
) -> str:
    """Adapted directly from DELTAX V1 `rotation.py::regime()`. RISK_OFF
    only when BOTH the benchmark's own ROC is negative AND the best
    safe-haven ROC beats it (a confirming condition, not a single
    threshold). Returns UNKNOWN -- never a guessed RISK_ON/RISK_OFF --
    when the benchmark has insufficient data, or when there is no
    safe-haven data available to evaluate the confirming condition (a
    fail-closed choice, stricter than DELTAX V1's own default).
    """
    bench_roc = roc(benchmark_bars, lookback)
    if bench_roc is None:
        return "UNKNOWN"
    haven_rocs = [r for r in (roc(bars, lookback) for bars in safe_haven_bars.values()) if r is not None]
    if not haven_rocs:
        return "UNKNOWN"
    best_haven = max(haven_rocs)
    if bench_roc < 0 and best_haven > bench_roc:
        return "RISK_OFF"
    return "RISK_ON"


def defensive_switch_triggered(
    cyclical_bars: list[dict[str, Any]],
    defensive_bars: list[dict[str, Any]],
    *,
    ma_period: int,
) -> bool | None:
    """Adapted directly from DELTAX V1 `rotation.py::defensive_switch()`.
    Cyclical/defensive price ratio falling below its own trailing moving
    average is treated as an independent, secondary risk-off signal.
    Returns `None` (never guessed) if there isn't enough aligned history
    on both legs to compute the ratio series and its moving average.
    """
    if ma_period <= 0:
        raise SectorRotationError("INVALID_MA_PERIOD:must be > 0")
    cyc = _extract_closes(cyclical_bars)
    dfn = _extract_closes(defensive_bars)
    n = min(len(cyc), len(dfn))
    if n < ma_period + 1:
        return None
    cyc, dfn = cyc[-n:], dfn[-n:]
    if any(d == 0 for d in dfn):
        return None
    ratio_series = [c / d for c, d in zip(cyc, dfn)]
    ma = sum(ratio_series[-ma_period - 1 : -1]) / ma_period
    return ratio_series[-1] < ma


@dataclass(frozen=True)
class SectorRotationRegime:
    symbol: str
    as_of: str
    lookback_bars: int
    benchmark_symbol: str
    score_scale: float
    market_regime: str  # one of MARKET_REGIMES
    defensive_switch_triggered: bool | None
    relative_strength: float | None  # own ROC minus benchmark ROC; None if not computable
    sector_rank: int | None  # 1-based rank among ranked universe members (only when usable and in universe)
    universe_size: int  # number of universe members that had a computable rank this call
    is_safe_haven: bool
    confirmation_bullish_features: int  # out of 2, see _confirmation_features
    confirmation_bearish_features: int
    rotation_tier: str  # one of ROTATION_TIERS
    rotation_score: float | None  # signed, in [-1, 1]; None only for NO_DATA/NOT_APPLICABLE

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "as_of": self.as_of,
            "lookback_bars": self.lookback_bars,
            "benchmark_symbol": self.benchmark_symbol,
            "score_scale": self.score_scale,
            "market_regime": self.market_regime,
            "defensive_switch_triggered": self.defensive_switch_triggered,
            "relative_strength": self.relative_strength,
            "sector_rank": self.sector_rank,
            "universe_size": self.universe_size,
            "is_safe_haven": self.is_safe_haven,
            "confirmation_bullish_features": self.confirmation_bullish_features,
            "confirmation_bearish_features": self.confirmation_bearish_features,
            "rotation_tier": self.rotation_tier,
            "rotation_score": self.rotation_score,
        }


def _clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def analyze_sector_rotation(
    symbol: str,
    symbol_bars: list[dict[str, Any]],
    *,
    benchmark_symbol: str,
    benchmark_bars: list[dict[str, Any]],
    universe_bars: dict[str, list[dict[str, Any]]],
    safe_haven_symbols: tuple[str, ...],
    safe_haven_bars: dict[str, list[dict[str, Any]]],
    defensive_ratio_pair: tuple[str, str] | None,
    defensive_ratio_bars: tuple[list[dict[str, Any]], list[dict[str, Any]]] | None,
    lookback_bars: int,
    top_n: int,
    defensive_ma_period: int,
    score_scale: float,
    now: datetime | None = None,
) -> SectorRotationRegime:
    """Per-symbol sector-rotation evidence, combining the regime gate,
    the defensive switch, relative-strength ranking, and a lightweight
    price-feature confirmation into one signed `rotation_score`.

    `universe_bars` should contain bars for every symbol the caller wants
    ranked (typically the sector/subsector ETF universe), INCLUDING
    `symbol_bars` under `symbol` if `symbol` is itself a universe member
    (it is not added automatically -- this function never mutates or
    infers the universe the caller supplied).

    All of `benchmark_symbol`, `lookback_bars`, `top_n`,
    `defensive_ma_period`, and `score_scale` are required, no-default
    research parameters (this project's established discipline -- see
    module docstring). `score_scale` is the factor that turns a raw
    relative-strength number (typically a few percent over a short
    lookback) into a value clipped to [-1, 1]; it is not invented by this
    module and must be chosen and disclosed by the caller.
    """
    if top_n <= 0:
        raise SectorRotationError("INVALID_TOP_N:must be > 0")
    if score_scale <= 0:
        raise SectorRotationError("INVALID_SCORE_SCALE:must be > 0")

    as_of = _now_iso(now)
    regime = market_regime(benchmark_bars, safe_haven_bars, lookback=lookback_bars)
    is_haven = symbol in safe_haven_symbols

    switch = None
    if defensive_ratio_pair is not None and defensive_ratio_bars is not None:
        cyclical_bars, defensive_bars = defensive_ratio_bars
        switch = defensive_switch_triggered(cyclical_bars, defensive_bars, ma_period=defensive_ma_period)

    rs = roc(symbol_bars, lookback_bars)
    if rs is not None:
        bench_roc = roc(benchmark_bars, lookback_bars)
        rs = None if bench_roc is None else (rs - bench_roc)

    ranked = rank_sectors(universe_bars, benchmark_bars, lookback=lookback_bars) if universe_bars else ()
    rank_lookup = {sym: i + 1 for i, (sym, _rs) in enumerate(ranked)}
    sector_rank = rank_lookup.get(symbol)

    bull_feat, bear_feat = _confirmation_features(symbol_bars, benchmark_bars)

    in_universe = symbol in universe_bars or rs is not None and not is_haven

    if regime == "UNKNOWN":
        tier, score = "NO_DATA", None
    elif is_haven:
        haven_rs = rs
        if regime == "RISK_OFF":
            tier = "SAFE_HAVEN_FAVORED"
            score = 0.0 if haven_rs is None else _clip(haven_rs * score_scale, 0.0, 1.0)
        else:
            tier = "SAFE_HAVEN_NOT_FAVORED"
            score = 0.0
    elif rs is None or not in_universe:
        tier, score = "NOT_APPLICABLE", None
    elif regime == "RISK_OFF":
        if rs <= 0:
            tier = "LAGGARD"
            score = _clip(rs * score_scale, -1.0, 0.0)
        else:
            # Regime overrides raw relative strength: capital isn't
            # rotating into cyclicals in a risk-off tape, even if this
            # member is technically outperforming the benchmark.
            tier = "NOT_FAVORED_RISK_OFF"
            score = 0.0
    else:  # RISK_ON
        if rs <= 0:
            tier = "LAGGARD"
            score = _clip(rs * score_scale, -1.0, 0.0)
        elif sector_rank is not None and sector_rank <= top_n:
            tier = "LEADER"
            score = _clip(rs * score_scale, 0.0, 1.0)
        else:
            tier = "RANKED_NOT_TOP"
            score = _clip(rs * score_scale * 0.5, 0.0, 1.0)

    return SectorRotationRegime(
        symbol=symbol,
        as_of=as_of,
        lookback_bars=lookback_bars,
        benchmark_symbol=benchmark_symbol,
        score_scale=score_scale,
        market_regime=regime,
        defensive_switch_triggered=switch,
        relative_strength=rs,
        sector_rank=sector_rank,
        universe_size=len(ranked),
        is_safe_haven=is_haven,
        confirmation_bullish_features=bull_feat,
        confirmation_bearish_features=bear_feat,
        rotation_tier=tier,
        rotation_score=score,
    )


def select_rotation_leaders(
    universe_bars: dict[str, list[dict[str, Any]]],
    benchmark_bars: list[dict[str, Any]],
    safe_haven_bars: dict[str, list[dict[str, Any]]],
    *,
    subsector_map: dict[str, tuple[str, ...]] | None,
    subsector_bars: dict[str, list[dict[str, Any]]] | None,
    lookback_bars: int,
    top_n: int,
    now: datetime | None = None,
) -> tuple[str, ...]:
    """Portfolio-level convenience helper (not used by `.350`'s per-symbol
    evidence path): the top-N ranked universe members by relative
    strength, with each optionally amplified to its strongest mapped
    subsector -- adapted directly from DELTAX V1 `rotation.py::
    best_subsector()`/`select()`. A subsector replaces its parent only
    when its own ROC beats the parent's; otherwise the parent is kept.
    Falls back to the safe-haven with the best ROC if the regime is
    RISK_OFF or no benchmark data is available (never returns an empty
    selection silently -- callers should treat an empty safe-haven set
    plus RISK_OFF/UNKNOWN as "no confident selection," and this function
    returns `()` in exactly that case, never guessing a fallback).
    """
    regime = market_regime(benchmark_bars, safe_haven_bars, lookback=lookback_bars)
    if regime != "RISK_ON":
        haven_ranked = sorted(
            ((sym, r) for sym, r in ((s, roc(b, lookback_bars)) for s, b in safe_haven_bars.items()) if r is not None),
            key=lambda pair: pair[1],
            reverse=True,
        )
        return (haven_ranked[0][0],) if haven_ranked else ()

    ranked = rank_sectors(universe_bars, benchmark_bars, lookback=lookback_bars)
    leaders = [sym for sym, rs in ranked if rs > 0][:top_n]

    if not subsector_map or not subsector_bars:
        return tuple(leaders)

    seen: set[str] = set()
    amplified: list[str] = []
    for parent in leaders:
        parent_rs = dict(ranked).get(parent)
        candidates = subsector_map.get(parent, ())
        best_symbol = parent
        best_rs = parent_rs
        for sub in candidates:
            sub_bars = subsector_bars.get(sub)
            if sub_bars is None:
                continue
            sub_roc = roc(sub_bars, lookback_bars)
            bench_roc = roc(benchmark_bars, lookback_bars)
            if sub_roc is None or bench_roc is None:
                continue
            sub_rs = sub_roc - bench_roc
            if best_rs is None or sub_rs > best_rs:
                best_symbol, best_rs = sub, sub_rs
        if best_symbol not in seen:
            amplified.append(best_symbol)
            seen.add(best_symbol)
    return tuple(amplified)
