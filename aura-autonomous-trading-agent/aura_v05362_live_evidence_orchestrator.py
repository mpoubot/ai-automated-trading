#!/usr/bin/env python3
"""
AURA v0.5.3.62 -- Live evidence orchestrator: sentiment + Elliott Wave +
sector-rotation evidence for `.356`'s live equity CLI.

WHY THIS EXISTS
------------------------------------------------------------------------
`.356` (`aura_v05356_stage3_live_equity_cli.py`) only ever fetched `.51`/
`.52` technical evidence. `aura_v054_signal_source.py`'s own
`FROZEN_DECIDE_KWARGS` was found to freeze `technical_weight`/
`short_technical_weight` at `0.0` (so `.51`/`.52` were mathematically
inert) AND never construct `sentiment_regime`/`wave_result` at all -- the
real root cause of the platform's zero-trade state (see
`AURA_v0.5.4_Zero_Trade_Root_Cause_Report_2026-09-15.md`). This module is
the missing piece: it builds real `sentiment_regime`/`wave_result`/
`sector_rotation_regime` evidence for a caller-supplied symbol universe,
so `.356` can populate them on `.55`'s `SymbolRequest` (which already had
unused `sentiment_regime`/`wave_result` fields flowing straight through
to `.53`'s `SymbolCycleInput` and `.50`'s `decide()` -- confirmed by
reading the whole chain, not assumed).

Built per Martin's explicit scoping (`AskUserQuestion`, 2026-09-24 and
2026-09-25):
  - Full evidence pipeline (news + sentiment + wave + sector rotation),
    not technical-only.
  - Sector rotation benchmark = SPY, safe havens = GLD + SLV, ranking
    universe = the existing 27-symbol `aura_v05351_equity_universe_v1.json`
    list (which already includes SPY/GLD/SLV as members).
  - Sector rotation is COMPUTE + LOG ONLY this round --
    `sector_rotation_weight` stays at `.50.decide()`'s own safe default of
    `0.0`; the regime is still computed and exposed on every
    `SymbolLiveEvidence` so it is visible in `.356`'s output, but is
    deliberately NOT threaded into `.53`'s `SymbolCycleInput` (that
    dataclass has no `sector_rotation_regime` field today, and adding one
    would mean modifying `.53` -- shared, already-tested infrastructure
    the existing dry-run path also depends on -- for a value that cannot
    affect any decision this round; see build report).
  - Technical signals brought back to `technical_weight`/
    `short_technical_weight` = 1.0 each (matching `sentiment_weight`/
    `wave_weight`, already 1.0 in `.054`'s `FROZEN_DECIDE_KWARGS`) --
    there was no prior validated non-zero value to restore, so this is a
    fresh, explicitly-confirmed number, not a guess.
  - Research parameters (`reversal_pct`, `decay_window_hours`,
    `min_source_count`, sector-rotation `lookback_bars`/`top_n`/
    `score_scale`/`defensive_ma_period`) have no prior approved production
    value anywhere in this repo (unlike the technical params, which had
    `AURA_v0.53_Frozen_Candidate_Freeze_Record_2026-09-15.md` as a real
    precedent) -- proposed with disclosed rationale and explicitly
    confirmed by Martin 2026-09-25, not silently defaulted.
  - `defensive_ratio_pair`/`defensive_ratio_bars` (an optional secondary
    sector-rotation confirmation signal) are left `None` this round --
    an explicitly supported safe/skip state in `.359`, not an invented
    value.

WHAT THIS MODULE DOES NOT DO
------------------------------------------------------------------------
  - Does not fetch market data or news itself. Bars (already converted to
    `.48`/`.359`/`.360`'s `list[dict]` shape) and the combined news-ledger
    events list are caller-supplied -- `.356` is the only place real
    Alpaca credentials/clients are constructed in this chain, and this
    module does not duplicate that.
  - Does not modify `.53`/`.55`/`.054` in any way. It is a new, standalone
    computation layer that `.356` calls and then merges into the existing,
    unmodified `SymbolRequest`/`SymbolCycleInput` shapes.
  - Does not pick `.50`'s decision weights on its own for every caller --
    `LIVE_EVIDENCE_DECIDE_KWARGS` below is Martin's own explicitly
    confirmed configuration for the live-evidence path specifically, kept
    separate from `.054`'s deliberately-frozen `FROZEN_DECIDE_KWARGS` so
    that module's own contract and test suite (`tests/
    test_aura_v054_signal_source.py`, which proves the frozen config
    always ABSTAINs) are completely undisturbed.
  - Does not attempt or reference order submission in any form.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aura_v05346_news_ingestion_classification as NEWS
import aura_v05360_research_full_evidence_builder as FULL_EVIDENCE

VERSION = "AURA v0.5.3.62"

# ----------------------------------------------------------------------
# Martin-confirmed live-decision weights (AskUserQuestion, 2026-09-24/25).
# Kept deliberately separate from aura_v054_signal_source.FROZEN_DECIDE_
# KWARGS -- see module docstring. decision_threshold/ai_penalty_per_
# concern/critic_penalty_per_issue are reused VERBATIM from that already-
# approved frozen record; only the five weight values below reflect a new
# decision, and every one of them was explicitly confirmed, not guessed.
# ----------------------------------------------------------------------
LIVE_EVIDENCE_DECIDE_KWARGS: dict[str, Any] = dict(
    sentiment_weight=1.0,
    wave_weight=1.0,
    technical_weight=1.0,
    short_technical_weight=1.0,
    sector_rotation_weight=0.0,  # compute + log only this round -- Martin, 2026-09-25
    decision_threshold=0.1,
    ai_penalty_per_concern=0.2,
    critic_penalty_per_issue=0.15,
)

# Martin-confirmed research parameters, 2026-09-25 (AskUserQuestion) --
# proposed with disclosed rationale, no prior approved production value
# existed for any of these. See module docstring.
SENTIMENT_WAVE_PARAMS: dict[str, float] = dict(
    reversal_pct=3.0,
    decay_window_hours=48.0,
    min_source_count=2,
)

BENCHMARK_SYMBOL = "SPY"
SAFE_HAVEN_SYMBOLS: tuple[str, ...] = ("GLD", "SLV")

SECTOR_ROTATION_PARAMS: dict[str, Any] = dict(
    lookback_bars=20,
    top_n=5,
    defensive_ma_period=20,  # inert this round -- defensive_ratio_pair/bars are None (see module docstring)
    score_scale=10.0,
)


class LiveEvidenceOrchestratorError(Exception):
    """Programmer-error / malformed-input only. A symbol lacking bars or
    news is NOT an error -- it is handled as reduced/absent evidence for
    that symbol (mirrors every other module in this chain's fail-open-
    per-symbol discipline)."""


# ============================================================================
# Bars-shape translation -- `.51`'s live fetch returns a pandas DataFrame;
# `.48`/`.359`/`.360` need `list[dict]`. This is the one place that
# translation happens, so it is never duplicated or subtly re-derived
# elsewhere.
# ============================================================================


def bars_df_to_research_dicts(bars_df: Any) -> list[dict[str, Any]]:
    """Converts a `.51`-shaped bars DataFrame (DatetimeIndex, lowercase
    `open`/`high`/`low`/`close`/`volume` columns -- see `.51`'s
    `REQUIRED_BAR_COLUMNS`) into the `list[dict]` shape `.48`/`.359`/`.360`
    require. Never mutates `bars_df`. Returns `[]` for `None` or an empty
    frame -- an empty bars list is `.360`'s/`.48`'s own well-defined
    "no wave evidence this cycle" input, not an error here."""
    if bars_df is None or len(bars_df) == 0:
        return []
    df = bars_df.reset_index()
    ts_col = df.columns[0]
    records: list[dict[str, Any]] = []
    for row in df.to_dict("records"):
        ts = row[ts_col]
        records.append(
            {
                "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
            }
        )
    return records


# ============================================================================
# News -- one combined ingest + read-back for the whole universe. `.46`'s
# ledger tags each article with the symbols it actually concerns; `.47`/
# `.360` filter per symbol internally, so this is never split per symbol.
# ============================================================================


def fetch_universe_news_events(
    news_client: Any,
    state_dir: Path,
    *,
    symbols: tuple[str, ...],
    start: Any,
    end: Any,
    limit: int = 50,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Ingests real Alpaca news for `symbols` into `.46`'s ledger, then
    reads the full ledger back. Fail-open on ingestion failure: `.46`'s
    own `ingest_and_classify_news` never overwrites a previously-good
    ledger with an empty one on a failed fetch, so this still returns
    whatever is already on disk (possibly `[]` on a genuinely first-ever
    run) plus a status dict the caller should log, never raises for an
    ordinary fetch failure."""
    now_dt = now or datetime.now(timezone.utc)
    ingestion_result = NEWS.ingest_and_classify_news(
        news_client, state_dir, symbols=symbols, start=start, end=end, limit=limit,
        fetched_at=now_dt.isoformat(),
    )
    events = NEWS.load_news_ledger(state_dir)
    return events, ingestion_result.to_dict()


# ============================================================================
# Per-symbol evidence -- sentiment + wave + (logged-only) sector rotation.
# ============================================================================


@dataclass(frozen=True)
class SymbolLiveEvidence:
    symbol: str
    sentiment_regime: Any | None
    wave_result: Any | None
    sector_rotation_regime: Any | None
    build_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "sentiment": self.sentiment_regime.to_dict() if self.sentiment_regime is not None else None,
            "wave": self.wave_result.to_dict() if self.wave_result is not None else None,
            "sector_rotation": self.sector_rotation_regime.to_dict() if self.sector_rotation_regime is not None else None,
            "build_error": self.build_error,
        }


def build_live_evidence_for_universe(
    bars_as_dicts_by_symbol: dict[str, list[dict[str, Any]]],
    news_events: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, SymbolLiveEvidence]:
    """One `SymbolLiveEvidence` per symbol in `bars_as_dicts_by_symbol`,
    built via `.360` (reused verbatim -- sentiment/wave/sector-rotation
    construction and its Decision-Seam shape validation are not
    reimplemented here). `technical_regime`/`short_technical_regime` are
    always passed as `None` to `.360` -- `.356` computes those separately
    via `.51`/`.52` and merges them into `.53`'s `SymbolCycleInput`
    itself; this function's only job is the sentiment/wave/sector-
    rotation leg.

    Fails open per symbol for wave evidence (a symbol with no/short bars
    simply gets `wave_result=None`, per `.360`'s own "if bars else None").
    Sector rotation needs benchmark + both safe-haven symbols' bars for
    the WHOLE universe at once (`.359` has no partial-universe mode) --
    if any of those three are missing this cycle, sector rotation is
    skipped for every symbol this cycle (recorded via `build_error`,
    never silently dropped).
    """
    now_dt = now or datetime.now(timezone.utc)
    benchmark_bars = bars_as_dicts_by_symbol.get(BENCHMARK_SYMBOL)
    safe_haven_bars = {s: bars_as_dicts_by_symbol[s] for s in SAFE_HAVEN_SYMBOLS if bars_as_dicts_by_symbol.get(s)}
    sector_rotation_available = bool(benchmark_bars) and len(safe_haven_bars) == len(SAFE_HAVEN_SYMBOLS)

    results: dict[str, SymbolLiveEvidence] = {}
    for symbol, bars in bars_as_dicts_by_symbol.items():
        sector_rotation_inputs = None
        if sector_rotation_available:
            sector_rotation_inputs = FULL_EVIDENCE.SectorRotationInputs(
                symbol_bars=bars,
                benchmark_symbol=BENCHMARK_SYMBOL,
                benchmark_bars=benchmark_bars,  # type: ignore[arg-type]
                universe_bars=bars_as_dicts_by_symbol,
                safe_haven_symbols=SAFE_HAVEN_SYMBOLS,
                safe_haven_bars=safe_haven_bars,
                defensive_ratio_pair=None,
                defensive_ratio_bars=None,
                lookback_bars=SECTOR_ROTATION_PARAMS["lookback_bars"],
                top_n=SECTOR_ROTATION_PARAMS["top_n"],
                defensive_ma_period=SECTOR_ROTATION_PARAMS["defensive_ma_period"],
                score_scale=SECTOR_ROTATION_PARAMS["score_scale"],
            )
        try:
            build_result = FULL_EVIDENCE.build_full_evidence_for_symbol(
                symbol,
                bars,
                news_events,
                reversal_pct=SENTIMENT_WAVE_PARAMS["reversal_pct"],
                decay_window_hours=SENTIMENT_WAVE_PARAMS["decay_window_hours"],
                min_source_count=SENTIMENT_WAVE_PARAMS["min_source_count"],
                technical_regime=None,
                short_technical_regime=None,
                sector_rotation_inputs=sector_rotation_inputs,
                now=now_dt,
            )
        except FULL_EVIDENCE.ResearchEvidenceError as exc:
            results[symbol] = SymbolLiveEvidence(
                symbol=symbol, sentiment_regime=None, wave_result=None, sector_rotation_regime=None,
                build_error=f"{type(exc).__name__}: {exc}",
            )
            continue

        build_error = None if sector_rotation_available else "SECTOR_ROTATION_SKIPPED_BENCHMARK_OR_SAFE_HAVEN_BARS_UNAVAILABLE"
        results[symbol] = SymbolLiveEvidence(
            symbol=symbol,
            sentiment_regime=build_result.sentiment_regime,
            wave_result=build_result.wave_result,
            sector_rotation_regime=build_result.sector_rotation_regime,
            build_error=build_error,
        )
    return results
