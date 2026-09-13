# AURA v0.5.3.46 — Completion Report: News Ingestion + Classification

**Date:** 2026-09-13
**Milestone:** `.46` (Final Implementation Baseline, "Market State / Research" track)
**Status:** Implemented, tested, verified. Not yet checkpointed at the time of writing this report (commit follows immediately after).

---

## 1. Audit performed before writing any code

A dedicated audit subagent searched (a) the canonical `aura_v053*.py` chain, (b) sibling/reference repositories (DELTAX_v2, Hermes Trader, Options Sniper, CAURA, BABIL, optionwright, mexc_bot), (c) the project's own roadmap docs for `.46`'s exact scope, (d) installed dependencies, and (e) existing tests — before any new code was written.

**Finding: `.46` was a clean, unstarted slot.** Zero news/sentiment code exists anywhere in `.1`–`.45`. Two modules carry explicit non-goal disclaimers reserving this territory:
- `aura_v05334_asset_instrument_metadata.py:186` — "No... research/intelligence content (news, sentiment, Elliott Wave, sector rotation) -- those remain upstream of `.33`."
- `aura_v05338_common_execution_supervisor.py:337` — "No strategy/AI/news/sentiment/Elliott-Wave/stock-selection/portfolio engine of any kind."

`requirements.txt` has never listed a news or NLP dependency (no `nltk`, `vaderSentiment`, `transformers`, `feedparser`, `spacy`, `textblob`). No news-API keys are provisioned in `.env.example`. No existing test references news or sentiment.

## 2. Reuse scan (A/B/C/D classification)

| Source | Classification | Why |
|---|---|---|
| DELTAX v2 `market_news_ingestion.py` / `company_news_ingestion.py` | C | Fetch→normalize→persist shape is reasonable, but built against Finnhub/Marketaux with a Postgres store — incompatible with AURA's atomic-file convention and the Alpaca-only scoping decision. |
| DELTAX v2 `news_ai_processor.py` / `direction_router.py` | Not adopted (anti-pattern) | Already flagged in this project's own prior audit (`AURA_DELTAX_MEXC_deltax_v2_typed_boundary_audit`): AI-derived sentiment fed straight into a LONG/SHORT decision with no typed validation boundary. `.46` sidesteps this entirely by using no AI at all. |
| Hermes Trader `pipeline/sentiment/score.py` | D | LLM-prompt-driven scorer; scoring is `.47`'s job, not `.46`'s. |
| `mexc_bot/` | D | Zero news/sentiment code found (full grep). |
| CAURA, BABIL, optionwright | D | No news/sentiment code found in any of the three. |
| TradePilot, ORION, Dark Wolf Sentinel | D (unverifiable) | Only presentation PDFs exist on disk; no unpacked source to audit. |
| `alpaca-py` `NewsClient`/`NewsRequest`/`News`/`NewsSet` | **A** | Already an installed, already-a-project-dependency library. Verified by direct `inspect.getsource()` read (not documentation, not assumed). This is the one concrete reusable asset and what `.46` is built on. |

**Conclusion:** nothing existing was directly reusable or adaptable for `.46`'s classification logic itself; the DELTAX/Hermes prior art informed shape and, more importantly, a named anti-pattern to avoid. `alpaca-py`'s `NewsClient` (A) is the only component actually reused.

## 3. Martin's scoping decisions (AskUserQuestion, 2026-09-13) and how each was implemented

| Question | Decision | Implementation |
|---|---|---|
| Source scope | **Alpaca only** | `fetch_alpaca_news()` calls `alpaca.data.historical.news.NewsClient.get_news()` exclusively. No Finnhub/Marketaux code, no new dependencies, no new API keys required. Schema (`NewsItem.source`) is source-neutral in shape so a later source can be added without a redesign. |
| Classification method | **Deterministic only** | `classify_news_item()` is a pure function using fixed regex/keyword rules (`CATEGORY_KEYWORDS`, 10 categories). No LLM call anywhere in the module. Every classification carries its own matched-keyword evidence (`classification_evidence`) for auditability. `classification_method` is stamped `"DETERMINISTIC_KEYWORD_RULES"` on every item so no consumer can mistake this for an AI judgment. |
| Earnings blackout windows | **Deferred to a later/separate milestone** | Not implemented. No `compute_blackout_window`/`is_earnings_blackout` function exists in the module (locked in by `test_module_has_no_earnings_blackout_function`). |
| Persistence model | **New minimal event ledger, mirroring `.43`'s pattern** | `news_events.json`: a single JSON file, whole-file atomic read-modify-write (`_atomic_write_json`, tmp-then-`os.replace()`), one `state_hash` over the full event list — byte-for-byte the same pattern as `.43`'s `equity_history.json`, not `.40`'s heavier per-record hash-chain state machine. |

## 4. What was implemented

**New file:** `aura_v05346_news_ingestion_classification.py` (~400 lines)

- `NewsItem`, `NewsFetchStatus`, `IngestionResult` — frozen dataclasses, source-neutral schema.
- `classify_news_item(headline, summary) -> (categories, evidence)` — pure, deterministic, non-exclusive multi-category tagging across 10 categories (EARNINGS, GUIDANCE, ANALYST_ACTION, MERGER_ACQUISITION, REGULATORY, LEGAL, DIVIDEND, MANAGEMENT_CHANGE, PRODUCT, MACRO), falling back to `UNCATEGORIZED` rather than fabricating a category when nothing matches.
- `fetch_alpaca_news(client, ...)` — calls the real `NewsClient.get_news()` API (verified field mappings: `News.id` int, `.symbols` always-present list, `NewsSet.data["news"]` the article list), classifies each article, and returns `(items, NewsFetchStatus)`. Any exception (network failure, auth failure, etc.) yields `status="FAILED"` with the exception recorded — never a fabricated empty success. `client=None` yields `"NOT_CONFIGURED"`.
- `load_news_ledger` / `append_news_ledger` — the `.43`-pattern ledger described above. Dedups strictly on `(source, external_id)` — Alpaca's own stable article id — never on content hash, since a source may legitimately update an article's text under the same id.
- `ingest_and_classify_news(client, state_dir, ...)` — the orchestration entry point. On a failed or unconfigured fetch, the ledger is left completely untouched (a failed fetch never overwrites a good ledger with an empty one).

**New file:** `tests/test_aura_v05346_news_ingestion_classification.py` (28 tests)

## 5. Governance constraints respected (from the canonical architecture/governance doc, §4.3/§4.4)

- *"Every Level 1 observation carries an explicit age/decay"* — `created_at`/`updated_at`/`fetched_at` are preserved verbatim on every item; `.46` does not itself judge staleness (left to `.47`).
- *"A single unverified news item should not be able to move Market State on its own"* — `.46` produces no aggregate signal, score, or decision of any kind. It persists individually-tagged items only.
- *"No alternative-intelligence signal may determine trade direction or size on its own"* — the module has no path to an order, position, or sizing decision anywhere in its call graph, and is not imported by (and does not import) any execution-authority module. Locked in by three dedicated governance tests (`test_module_has_no_sentiment_scoring_function`, `test_module_has_no_trade_or_order_function`, `test_module_has_no_earnings_blackout_function`, `test_ingestion_result_carries_no_direction_or_size_fields`).

## 6. Disclosed limitations

- Same sandboxed network egress limitation disclosed in `.42`/`.43`/`.45`: `alpaca.markets` could not be reached from this session. `fetch_alpaca_news` is verified against the actual installed `alpaca-py` 0.44.0 source and exercised against injected fakes, not a live account. A live smoke test is the right next check before `.47` depends on this data in production.
- Classification is keyword-based and English-only; unusual phrasing may land in `UNCATEGORIZED`. This is a disclosed false-negative risk, not a bug — under-classifying is safer than fabricating, and every decision carries matched-keyword evidence for audit.
- Dedup keys on `(source, external_id)`. If Alpaca ever republishes an article under a new id, it is treated as a new item rather than an edit — no article-versioning is attempted.

## 7. Tests and verification

- **28/28 new tests passing** — classification (categories, multi-category, uncategorized fallback, case-insensitivity, evidence, never-raises), fetch (not-configured, success field-mapping, failure, missing-optional-fields, symbol-filter pass-through, empty-result-is-still-success), ledger (empty-on-no-file, persist, dedup, same-headline-different-id-not-deduped, state_hash presence/correctness, atomic-write-leaves-no-tmp-file), orchestration (success path, duplicate-on-repeat, failed-fetch-does-not-touch-ledger, not-configured-does-not-write, mixed new/duplicate batch), and four governance/non-goal invariant tests.
- **Full regression suite: 720/720 passing** (692 prior + 28 new). One run showed a single failure in `.40`'s pre-existing `test_mexc_concurrent_attempts_with_audit_trail_exactly_one_wins` (a threading/concurrency test unrelated to `.46` — `.40` was not touched this milestone); confirmed flaky by re-running it in isolation three times (all passed) and re-running the full suite once more (720/720 clean). Not a `.46` regression.
- **Lint: clean** (`pyflakes`) on both the new module and its test file.

## 8. Next

`.47` (Market sentiment scoring) builds on this ledger. Then `.48` (Elliott Wave), `.49` (AI proposal pipeline), `.50` (Formal Decision Engine) — then circle back to `.41` (Ghost Trades), then `.51`→`.53`. Same audit-first/reuse-scan-first/test/verify/report/commit discipline for every remaining milestone.

Checkpoint commit to be created locally immediately after this report is saved and delivered. **Not pushed to GitHub**, per the standing rule.
