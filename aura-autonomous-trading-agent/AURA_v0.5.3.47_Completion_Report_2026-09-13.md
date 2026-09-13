# AURA v0.5.3.47 — Completion Report: Market Sentiment Scoring

**Date:** 2026-09-13
**Milestone:** `.47` (Final Implementation Baseline, "Market State / Research" track)
**Status:** Implemented, tested, verified. Includes one additive patch to `.46`.

---

## 1. Audit performed before writing any code

A dedicated audit subagent inspected `.46`'s exact public surface and on-disk ledger shape, searched the `aura_v053xx` chain for any prior sentiment/decay/weighting code (none found — `.44`/`.45`'s binary staleness-threshold pattern was the only conceptually related precedent), surveyed sibling/reference repos (DELTAX v2, Hermes Trader, and a broad grep of every other competitor repo under `/home/claude/work/competitors/`) for scoring formulas, and pulled the exact governing wording from this project's canonical architecture/governance doc. Full findings below.

**Key finding that changed the plan:** `.46` persists each item's ingestion venue as `source="ALPACA"` but never captured Alpaca's own per-article origin field (`News.source`, e.g. "Benzinga"). The governance doc requires a genuine minimum-source-diversity check before a news-derived signal may be promoted, and explicitly warns against fake corroboration (citing a competitor whose "multi-source" engine was one hardcoded fabricated source dressed up as several). Without the real per-article origin, `.47` could only count articles, not sources — which is exactly the anti-pattern the governance doc warns about. Flagged to Martin before writing `.47`'s scoring logic; his decision (below) was to patch `.46` first.

## 2. Reuse scan (A/B/C/D classification)

| Source | Classification | What was actually reused |
|---|---|---|
| Hermes Trader `pipeline/portfolio/selector.py::score_ticker()` | **B** (adapted, not copied) | The linear recency-decay **shape** — `recency = max(0, 1 - hours_ago/window)` — is reused in `_recency_weight()`, per the governance doc's own endorsement of this specific formula as "sound and directly transferable." The window value (Hermes hardcodes 48h), the LLM-derived `sentiment_score`, `enrichment_boost`, and hardcoded per-sector win-rate table were **not** reused — none fit `.47`'s deterministic, no-invented-numbers scope. |
| Hermes Trader `pipeline/sentiment/score.py` | D | Entirely LLM-prompt-driven; `.47`'s keyword rules were written independently. |
| DELTAX v2 `news_ai_processor.py` (`impact_score`, `trade_relevance`) | D | LLM-confidence-based; no deterministic equivalent to adapt. |
| DELTAX v2 `market_event_clustering.py` (Jaccard near-duplicate clustering) | C | Architecturally useful idea, not implemented — `.47`'s `min_source_count`/origin-diversity check solves the more specific problem the governance doc actually asks for (genuine multi-outlet corroboration); adding text-similarity dedup on top would be scope creep beyond what was asked. |
| DELTAX v2 `direction_router.py` | Not adopted (anti-pattern) | Already flagged in this project's own prior audit: AI sentiment feeding a trade direction with no typed validation boundary. `.47` avoids the whole class of bug by using no AI and never touching a trade decision. |
| `mexc_bot/`, CAURA, BABIL, optionwright | D | No sentiment-scoring code found in any (confirmed absent by grep). |

## 3. Martin's scoping decisions and how each was implemented

Confirmed twice — once via `AskUserQuestion`, once via a detailed written follow-up — consistently:

| Decision | Implementation |
|---|---|
| **Source diversity gap: patch `.46` first** | Added `NewsItem.origin_source: str \| None` to `.46`, populated from Alpaca's real `article.source` field (e.g. "Benzinga"), never fabricated (`None` when genuinely absent). `.46`'s dedup key is unchanged (`(source, external_id)`, where `source` remains the ingestion venue). One new `.46` test (`test_fetch_origin_source_none_when_genuinely_absent_not_fabricated`) added; `.46`'s full suite re-verified (29/29). |
| **Direction: deterministic keyword rules, no LLM** | `classify_sentiment_direction(headline, summary)` — two independent keyword sets (`BULLISH_KEYWORDS`, `BEARISH_KEYWORDS`). Both match → `MIXED` (conflicting, not silently averaged); neither match → `NEUTRAL` (honest "no signal found," never a guess). Every match retained as evidence. |
| **Decay window: required parameter, no default** | `score_symbol_sentiment(..., decay_window_hours, ...)` raises `SentimentScoringError` if not a positive number — no fallback value anywhere in the call path. `EXAMPLE_RESEARCH_DECAY_WINDOW_HOURS = 48.0` exists as documentation only (citing Hermes/the governance note as a candidate starting point for research); a dedicated test (`test_module_documents_but_never_reads_example_window`) asserts no function body references it. |
| **Output granularity: per-symbol only** | `score_all_symbols()` discovers symbols from `event["symbols"]` tags only; items with an empty `symbols` list (macro/market-wide news) are excluded entirely, not folded into any aggregate. No `score_market_wide`/`score_macro` function exists (locked in by `test_module_has_no_market_wide_aggregate_function`). |
| **Source diversity: real origin, not article count** | `score_symbol_sentiment` counts `len(set(distinct origin_source values))`, not `len(events)`. Ten same-wire articles yield `source_count=1`, not 10 (locked in by `test_ten_articles_one_wire_never_counted_as_ten_sources`). Below `min_source_count` (also a required, no-default parameter), `corroboration_status="INSUFFICIENT"` and `promotable_score=None` — the raw number is still computed and visible for audit, but explicitly marked not-usable. |

## 4. Reproducibility / auditability (Martin's explicit written requirement)

Every `SentimentRegime` result carries: `symbol`, `as_of` (calculation timestamp), `decay_window_hours` and `min_source_count` (the exact parameters used), `items_considered`, `input_event_ids` (every contributing article's external id, for full traceability back to `.46`'s ledger), `distinct_origin_sources` and `source_count`, per-direction counts (`bullish_count`/`bearish_count`/`neutral_count`/`mixed_count`), `raw_score`, and `promotable_score`. `score_symbol_sentiment` and `score_all_symbols` are pure functions of `(events, decay_window_hours, min_source_count, now)` — same inputs always reproduce the identical output (verified by `test_score_is_deterministic_reproducible_across_calls`).

## 5. Governance constraints respected

- **Level 1 → Level 2 only**: `.47` reads `.46`'s persisted ledger and derives a per-symbol reading; it has no path to an order, position, sizing, or authorization decision anywhere in its call graph (locked in by `test_module_has_no_trade_or_order_function`).
- **Explicit age/decay**: items outside `decay_window_hours` are excluded entirely, not down-weighted to a token nonzero value.
- **No corroboration fabrication**: covered in detail above.
- **Persistence**: `.47` introduces **no new persisted state**, deliberately mirroring `.44`'s own precedent (`.43` persists, `.44` computes on demand from `.43`'s data plus caller-supplied limits). `.47` is the same shape one level up: `.46` persists, `.47` computes on demand from `.46`'s ledger plus caller-supplied decay/corroboration parameters.

## 6. Disclosed limitations

- Keyword-based direction detection is English-only and will under-classify unusual phrasing into `NEUTRAL` — a disclosed false-negative risk, matching `.46`'s own disclosed classification limitation, and safer than a fabricated direction.
- No text-similarity/near-duplicate dedup (DELTAX's Jaccard-clustering idea, classified C above) — a wire service republishing near-identical copy under distinct article ids would currently count as separate items. The `origin_source` corroboration check still prevents this from being mistaken for genuine multi-outlet diversity, but it can inflate `items_considered`/direction counts. Left as a disclosed gap, not built now (would be scope creep beyond what was asked).
- `EXAMPLE_RESEARCH_DECAY_WINDOW_HOURS`/`min_source_count` values are not researched or validated in this milestone — `.47` provides the mechanism; choosing and validating actual research parameters is future work (likely alongside `.48`/`.49`/`.50` or the crypto validation campaign).

## 7. Tests and verification

- **`.46` patch**: 1 new test (origin_source capture), full `.46` suite re-verified at 29/29.
- **`.47`**: 30/30 new tests passing — direction classification (bullish/bearish/neutral/mixed/never-raises), required-parameter enforcement (decay window, min source count, both zero/None rejected; the documented example constant is never read by any function), data quality (no-data, outside-decay-window exclusion, missing-symbol-tag exclusion, future-timestamp clock-skew handling), corroboration (single-source hidden, multi-source promoted, ten-articles-one-wire never treated as ten sources, `None` origin not counted), score math (partial offset, recency weighting favoring fresher items, neutral/mixed contribute zero but are counted, determinism/reproducibility, input-event-id traceability, missing-timestamp exclusion), `score_all_symbols` (symbol discovery, macro-item exclusion, empty ledger, multi-symbol item fan-out), and four governance/non-goal invariant tests.
- **Full regression suite: 751/751 passing** (720 prior + 1 `.46` patch test + 30 `.47` tests).
- **Lint: clean** (`pyflakes`) on both files and both test files.

## 8. Next

`.48` (Elliott Wave / research-strategy intelligence), then `.49` (AI proposal pipeline + typed/adversarial AI boundary — where AI-derived sentiment can finally be introduced and compared against `.47`'s deterministic baseline, per Martin's explicit "we can later compare deterministic sentiment vs. AI-derived sentiment" framing), `.50` (Formal Decision Engine) — then circle back to `.41` (Ghost Trades), then `.51`→`.53`. Same audit-first/reuse-scan-first/test/verify/report/commit discipline for every remaining milestone.

Checkpoint commit to be created locally immediately after this report is saved and delivered, bundling the `.46` patch and the new `.47` module together (mirroring how `.45` bundled its additive `.40` extension into one commit). **Not pushed to GitHub**, per the standing rule.
