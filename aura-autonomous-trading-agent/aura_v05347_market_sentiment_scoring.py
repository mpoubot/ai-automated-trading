#!/usr/bin/env python3
"""
AURA v0.5.3.47 — Market Sentiment Scoring (deterministic, per-symbol).

Level 1 -> Level 2 derivation only, per this project's own canonical
architecture/governance doc (`AURA_DELTAX_MEXC_canonical_architecture_and_
governance_2026-09-08.md` §4.2-4.4): reads `.46`'s persisted, individually-
classified news items (Level 1 observations) and derives one deterministic,
evidence-backed sentiment score per symbol (a Level 2 Market State reading).
NEVER produces a trade decision, direction, or size. NEVER writes to any
execution-authority module. Not imported by, and does not import, any
order/position/authorization code anywhere in this repo.

Why this exists (v0.5.5 Final Implementation Baseline, milestone `.47`,
"Market sentiment scoring")
------------------------------------------------------------------------
`.46` classifies WHAT KIND of news an item is (earnings, macro, M&A, ...)
but explicitly never judges what it MEANS or aggregates across items. `.47`
is the first module to do either: it assigns a deterministic bullish/
bearish/neutral direction to each item and combines same-symbol items,
recency-weighted, into one auditable per-symbol score.

Reuse scan (Martin's explicit A/B/C/D framework, performed BEFORE writing
any new code)
------------------------------------------------------------------------
  - Hermes Trader (competitor, reference-only) `pipeline/portfolio/
    selector.py::score_ticker()`: (B, adapted not copied) the linear
    recency-decay SHAPE -- `recency = max(0, 1 - hours_ago/window)` -- is
    adopted here (`_recency_weight` below), because this project's own
    governance doc explicitly singles it out as "sound and directly
    transferable." NOT adopted: Hermes hardcodes the window at 48h and its
    `sentiment_score`/`enrichment_boost`/sector-win-rate composite comes
    from an LLM-scored signal feed plus a hardcoded per-sector table --
    none of that fits `.47`'s deterministic-only, no-invented-numbers
    scope. Its `bull`/`bear` counting and `avg_recency` averaging shape
    (not values) also informed this module's aggregation.
  - Hermes Trader `pipeline/sentiment/score.py`: (D) not applicable --
    entirely LLM-prompt-driven direction assignment. `.47`'s own directional
    keyword rules were written independently (see `SENTIMENT_KEYWORDS`
    below), not derived from this file's prompt engineering.
  - DELTAX v2 `news_ai_processor.py::impact_score`/`trade_relevance`: (D)
    not applicable -- LLM-confidence-based, no deterministic equivalent to
    adapt.
  - DELTAX v2 `market_event_clustering.py` (Jaccard-similarity near-
    duplicate clustering before scoring): (C) architecturally useful idea
    (don't let near-duplicate wire copies of the same story inflate a
    score) but NOT implemented here -- `.47`'s `min_source_count` check
    (below) solves the more specific problem this project's governance doc
    actually asks for (genuine multi-OUTLET corroboration, not just
    dedup of near-identical text), and doing both would be scope creep
    beyond what was asked; text-similarity dedup remains a possible future
    refinement, not built now.
  - DELTAX v2 `direction_router.py`: explicitly NOT adapted -- this
    project's own prior audit already flagged its core flaw (AI sentiment
    fed straight into a trade direction with no typed validation boundary).
    `.47` avoids the entire class of bug by using no AI and by refusing to
    ever touch a trade decision at all (see "Governance constraints"
    below).
  - `mexc_bot/`, CAURA, BABIL, optionwright: grepped; no sentiment-scoring
    code found in any (confirmed absent, not merely unwired).

Scoping decisions (Martin, 2026-09-13 -- both an AskUserQuestion round and
a follow-up written summary; reproduced here so a future reader does not
have to reconstruct them from chat history)
------------------------------------------------------------------------
  1. Direction: deterministic keyword/phrase rules with retained evidence,
     no LLM. Falls back to NEUTRAL (no cues matched) or MIXED (both
     bullish and bearish cues matched -- conflicting, not averaged away
     silently) rather than guessing.
  2. Decay window: a REQUIRED caller-supplied parameter
     (`decay_window_hours`), no hardcoded default anywhere in this module.
     Hermes' 48h linear-decay formula shape is reused; its specific window
     value is explicitly NOT baked in -- "that's something we should test,
     not assume" (Martin). `EXAMPLE_RESEARCH_DECAY_WINDOW_HOURS = 48.0`
     below is documented as a candidate value for a caller/researcher to
     try, never read by any function in this module.
  3. Output granularity: per-symbol only. Items with an empty `symbols`
     list (macro/market-wide news) are excluded from every symbol's score
     and are not aggregated into any market-wide bucket -- deferred to a
     future capability, not invented here.
  4. Source diversity: uses `.46`'s `origin_source` field (added
     2026-09-13 specifically to unblock this requirement -- see `.46`'s
     completion report addendum) to count genuinely distinct outlets, not
     article count. `min_source_count` is likewise a REQUIRED caller-
     supplied parameter, no default. Below that threshold, the computed
     score is still shown (for audit) but is explicitly marked
     `corroboration_status="INSUFFICIENT"` and `promotable_score=None` --
     nothing downstream may treat an under-corroborated number as if it
     were a usable Market State reading. Ten articles from one wire is not
     ten sources; this is checked for real, not assumed.

Governance constraints this module is built to respect (from
`AURA_DELTAX_MEXC_canonical_architecture_and_governance_2026-09-08.md`
§4.2-4.4)
------------------------------------------------------------------------
  "Every Level 1 observation... carries an explicit age/decay[.]" --
      `_recency_weight` computes this from each item's own `created_at`;
      items older than `decay_window_hours` are excluded entirely, not
      down-weighted to a token nonzero value.
  "A single unverified news item... should not be able to move Market
  State on its own[.]" -- enforced by `min_source_count` /
      `corroboration_status` as described above.
  "No alternative-intelligence signal may determine trade direction or
  size on its own[.]" -- this module has no path to an order, position,
      sizing, or authorization decision anywhere in its call graph.
  "Level 1 observations... Level 2 derived Market State... only Level 3
  may produce an ExecutionIntent." -- `.47` is Level 2 only. It is a pure,
      read-only function of `.46`'s ledger; it persists nothing of its
      own (there is no new state file here -- see "Persistence" below).

Persistence
------------------------------------------------------------------------
`.47` introduces NO new persisted state, deliberately mirroring `.44`'s own
precedent: `.43` (observe reality) persists a ledger; `.44` (enforce)
computes on demand from `.43`'s data plus caller-supplied limits, with no
ledger of its own. `.47` is the same shape one level up the stack: `.46`
(ingest) persists a ledger; `.47` (derive) computes on demand from `.46`'s
data plus caller-supplied decay/corroboration parameters. A future
milestone can add persistence for computed regimes if a consumer needs
history; nothing here forecloses that.

Reproducibility / auditability
------------------------------------------------------------------------
Every `SentimentRegime` carries, alongside its score: the exact
`decay_window_hours` and `min_source_count` used, the calculation
timestamp (`as_of`), every input item's `external_id`
(`input_event_ids`), the distinct origin sources actually observed, and
per-direction-bucket counts. Given the same ledger state and the same
parameters, `score_symbol_sentiment` is a pure function and always
reproduces the same output.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

# `.46` is loaded dynamically by callers/tests the same way every other
# aura_v053NN module loads its dependencies in this repo (see e.g. `.45`'s
# `_load_audit_module`) -- this module itself only needs the plain dict
# shape `.46`'s `load_news_ledger` returns, so it takes that list of event
# dicts as a parameter rather than importing `.46` directly, keeping this
# module independently loadable/testable like the rest of the chain.

EXAMPLE_RESEARCH_DECAY_WINDOW_HOURS = 48.0  # NOT read by any function below.
# Documented candidate only, per Hermes' governance-endorsed formula shape;
# every real call must supply its own `decay_window_hours` explicitly.


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class SentimentScoringError(Exception):
    pass


# ------------------------------------------------------------------------
# Deterministic per-item direction. Two independent keyword sets; both
# matching is MIXED (conflicting, not averaged away silently), neither
# matching is NEUTRAL (an honest "no directional signal found" reading,
# not a fabricated guess) -- the same non-fabrication discipline as `.46`'s
# UNCATEGORIZED fallback.
# ------------------------------------------------------------------------

BULLISH_KEYWORDS: tuple[str, ...] = (
    r"\bbeats?\s+estimates?\b", r"\bbeats?\s+expectations\b", r"\bexceeds?\s+expectations\b",
    r"\bupgrades?\b", r"\braises?\s+(guidance|outlook|forecast)\b", r"\bstrong\s+(quarter|results|growth|demand)\b",
    r"\brecord\s+(revenue|earnings|profit|sales)\b", r"\bbullish\b", r"\boutperforms?\b",
    r"\bsurges?\b", r"\brallies?\b", r"\bbeat\s+the\s+street\b",
)

BEARISH_KEYWORDS: tuple[str, ...] = (
    r"\bmisses?\s+estimates?\b", r"\bmisses?\s+expectations\b", r"\bfalls?\s+short\b",
    r"\bdowngrades?\b", r"\bcuts?\s+(guidance|outlook|forecast)\b", r"\bweak\s+(quarter|results|demand)\b",
    r"\bbearish\b", r"\bunderperforms?\b", r"\bwarns?\b", r"\bplunges?\b", r"\bslumps?\b",
    r"\btumbles?\b", r"\blawsuit\b", r"\bprobe\b", r"\binvestigation\b", r"\brecall(s|ed)?\b",
    r"\bmiss(es)?\s+the\s+street\b",
)

DIRECTION_BULLISH = "BULLISH"
DIRECTION_BEARISH = "BEARISH"
DIRECTION_NEUTRAL = "NEUTRAL"
DIRECTION_MIXED = "MIXED"

_DIRECTION_VALUE = {
    DIRECTION_BULLISH: 1.0,
    DIRECTION_BEARISH: -1.0,
    DIRECTION_NEUTRAL: 0.0,
    DIRECTION_MIXED: 0.0,  # conflicting cues cancel; the item still counts toward volume/corroboration.
}


def classify_sentiment_direction(headline: str, summary: str) -> tuple[str, dict[str, tuple[str, ...]]]:
    """Pure function: text in, (direction, evidence) out. Never raises."""
    text = f"{headline or ''}\n{summary or ''}"
    bull_hits = tuple(p for p in BULLISH_KEYWORDS if re.search(p, text, re.IGNORECASE))
    bear_hits = tuple(p for p in BEARISH_KEYWORDS if re.search(p, text, re.IGNORECASE))
    evidence: dict[str, tuple[str, ...]] = {}
    if bull_hits:
        evidence["BULLISH"] = bull_hits
    if bear_hits:
        evidence["BEARISH"] = bear_hits
    if bull_hits and bear_hits:
        return DIRECTION_MIXED, evidence
    if bull_hits:
        return DIRECTION_BULLISH, evidence
    if bear_hits:
        return DIRECTION_BEARISH, evidence
    return DIRECTION_NEUTRAL, evidence


# ------------------------------------------------------------------------
# Recency weight -- Hermes' linear-decay SHAPE, reused; the window is
# never defaulted (see module docstring, scoping decision #2).
# ------------------------------------------------------------------------

def _recency_weight(item_created_at: datetime, now: datetime, decay_window_hours: float) -> float:
    if decay_window_hours <= 0:
        raise SentimentScoringError(f"INVALID_DECAY_WINDOW:{decay_window_hours!r}")
    hours_ago = (now - item_created_at).total_seconds() / 3600.0
    if hours_ago < 0:
        hours_ago = 0.0  # a clock-skew/future timestamp is treated as "just happened", never negative weight.
    return max(0.0, 1.0 - hours_ago / decay_window_hours)


# ------------------------------------------------------------------------
# Per-symbol aggregate. Auditable by construction: every field a reviewer
# would need to reproduce or challenge the number is present on the
# result, per Martin's explicit reproducibility requirement.
# ------------------------------------------------------------------------

@dataclass(frozen=True)
class SentimentRegime:
    symbol: str
    as_of: str
    decay_window_hours: float
    min_source_count: int
    items_considered: int
    input_event_ids: tuple[str, ...]
    distinct_origin_sources: tuple[str, ...]
    source_count: int
    corroboration_status: str            # "SUFFICIENT" | "INSUFFICIENT" | "NO_DATA"
    bullish_count: int
    bearish_count: int
    neutral_count: int
    mixed_count: int
    raw_score: float | None              # recency-weighted average direction in [-1, +1]; None if no items.
    promotable_score: float | None       # raw_score if corroboration SUFFICIENT, else None -- never a
                                          # number a downstream consumer could mistake for usable Market
                                          # State when corroboration has not actually been established.

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "as_of": self.as_of,
            "decay_window_hours": self.decay_window_hours,
            "min_source_count": self.min_source_count,
            "items_considered": self.items_considered,
            "input_event_ids": list(self.input_event_ids),
            "distinct_origin_sources": list(self.distinct_origin_sources),
            "source_count": self.source_count,
            "corroboration_status": self.corroboration_status,
            "bullish_count": self.bullish_count,
            "bearish_count": self.bearish_count,
            "neutral_count": self.neutral_count,
            "mixed_count": self.mixed_count,
            "raw_score": self.raw_score,
            "promotable_score": self.promotable_score,
        }


def score_symbol_sentiment(
    events: list[dict[str, Any]],
    symbol: str,
    *,
    decay_window_hours: float,
    min_source_count: int,
    now: datetime | None = None,
) -> SentimentRegime:
    """Pure function over `.46`'s ledger event dicts (as returned by
    `load_news_ledger`). Filters to events tagged with `symbol` whose
    `created_at` falls within `decay_window_hours` of `now`, classifies
    each deterministically, and combines them into one auditable regime.
    Raises `SentimentScoringError` if `decay_window_hours` or
    `min_source_count` is not a positive value -- these are required,
    caller-supplied research parameters, never defaulted (see module
    docstring, scoping decisions #2 and #4)."""
    if decay_window_hours is None or decay_window_hours <= 0:
        raise SentimentScoringError(f"DECAY_WINDOW_REQUIRED_AND_POSITIVE:{decay_window_hours!r}")
    if min_source_count is None or min_source_count < 1:
        raise SentimentScoringError(f"MIN_SOURCE_COUNT_REQUIRED_AND_POSITIVE:{min_source_count!r}")

    now = now or datetime.now(timezone.utc)
    as_of = now.isoformat()

    relevant: list[tuple[dict[str, Any], datetime, float]] = []
    for event in events:
        if symbol not in (event.get("symbols") or []):
            continue
        created_at = _parse_iso(event.get("created_at"))
        if created_at is None:
            continue  # cannot age an item with no parseable timestamp -- excluded, not assumed-fresh.
        weight = _recency_weight(created_at, now, decay_window_hours)
        if weight <= 0.0:
            continue  # outside the decay window entirely.
        relevant.append((event, created_at, weight))

    if not relevant:
        return SentimentRegime(
            symbol=symbol, as_of=as_of, decay_window_hours=decay_window_hours,
            min_source_count=min_source_count, items_considered=0, input_event_ids=(),
            distinct_origin_sources=(), source_count=0, corroboration_status="NO_DATA",
            bullish_count=0, bearish_count=0, neutral_count=0, mixed_count=0,
            raw_score=None, promotable_score=None,
        )

    bullish_count = bearish_count = neutral_count = mixed_count = 0
    weighted_sum = 0.0
    weight_sum = 0.0
    origin_sources: set[str] = set()
    input_ids: list[str] = []

    for event, _created_at, weight in relevant:
        direction, _evidence = classify_sentiment_direction(event.get("headline", ""), event.get("summary", ""))
        if direction == DIRECTION_BULLISH:
            bullish_count += 1
        elif direction == DIRECTION_BEARISH:
            bearish_count += 1
        elif direction == DIRECTION_MIXED:
            mixed_count += 1
        else:
            neutral_count += 1
        weighted_sum += _DIRECTION_VALUE[direction] * weight
        weight_sum += weight
        origin = event.get("origin_source")
        if origin:
            origin_sources.add(origin)
        input_ids.append(event.get("external_id", ""))

    raw_score = (weighted_sum / weight_sum) if weight_sum > 0 else None
    source_count = len(origin_sources)
    corroboration_status = "SUFFICIENT" if source_count >= min_source_count else "INSUFFICIENT"
    promotable_score = raw_score if corroboration_status == "SUFFICIENT" else None

    return SentimentRegime(
        symbol=symbol,
        as_of=as_of,
        decay_window_hours=decay_window_hours,
        min_source_count=min_source_count,
        items_considered=len(relevant),
        input_event_ids=tuple(input_ids),
        distinct_origin_sources=tuple(sorted(origin_sources)),
        source_count=source_count,
        corroboration_status=corroboration_status,
        bullish_count=bullish_count,
        bearish_count=bearish_count,
        neutral_count=neutral_count,
        mixed_count=mixed_count,
        raw_score=raw_score,
        promotable_score=promotable_score,
    )


def score_all_symbols(
    events: list[dict[str, Any]],
    *,
    decay_window_hours: float,
    min_source_count: int,
    now: datetime | None = None,
) -> dict[str, SentimentRegime]:
    """Convenience wrapper: discovers every symbol tagged anywhere in
    `events` (excluding the empty-symbols/macro items, per Martin's
    per-symbol-only scoping decision) and scores each independently via
    `score_symbol_sentiment`. Symbol discovery and per-symbol scoring are
    both pure functions of `events`, so this remains fully reproducible."""
    symbols: set[str] = set()
    for event in events:
        symbols.update(event.get("symbols") or [])
    return {
        symbol: score_symbol_sentiment(
            events, symbol, decay_window_hours=decay_window_hours,
            min_source_count=min_source_count, now=now,
        )
        for symbol in sorted(symbols)
    }
