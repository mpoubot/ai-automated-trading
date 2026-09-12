# AURA v0.5.3.44 Completion Report — Portfolio Exposure Enforcement (deterministic, fail-closed)

**Date:** 2026-09-12
**Scope:** Milestone `.44` on the v0.5.5 Final Implementation Baseline, built per Martin's explicit scoping message (2026-09-12): a deterministic, fail-closed decision layer built directly on `.43`'s canonical `PortfolioSnapshot`/exposure report — enforcement-only (no order placement, no position mutation), no invented limits, and explicitly narrowed away from ATR-sizing/stop-loss/consecutive-loss/correlation (deferred as "later capability" once their underlying data becomes computable, per Martin's own words: "`.43` = observe reality, `.44` = enforce what we can prove, later capability = acquire missing information").

Third milestone in the reordered build sequence (`.42` → `.43` → `.44` → ... → `.50`, then back to `.41`, then `.51`→`.53`).

---

## 1. Audit (per the standing 7-step milestone procedure)

A dedicated audit agent (plus my own direct verification of the most load-bearing findings) confirmed, before any code was written:

- **No live ATR-based sizing, stop-loss, daily-loss limit, drawdown halt, consecutive-loss limit, or correlation check exists anywhere in the canonical `aura_v053xx` chain.** The one nuance: `mexc_bot/live_bot.py` — a separate, legacy, single-process bot never imported by any `aura_v053xx` file — does run `CircuitBreaker`/`calc_position_plan()` live, but in-process only (no persistence, no tests, dies on restart) and entirely outside the chain `.44` extends.
- **`.31`/`.36` (MEXC/Alpaca authorization)** both already share one decision shape (`_evaluate_guardrails()` → named rejection reasons → never trusting a caller-supplied safety state without independently recomputing it) — the pattern this module's `DimensionVerdict`/`EnforcementDecision` shape is adapted from. Per Martin's explicit scope ("must not become an execution engine"), this milestone does **not** modify `.31`/`.36` or wire itself into them — it produces the deterministic decision only; wiring a `BLOCK` into an actual order-authorization rejection is left to a later, explicitly-approved step.
- **`.25` (Alpaca position sizing)** is flat-fraction (10% of equity, 5-position cap), Alpaca-only, no ATR, no per-trade risk concept — confirmed directly, not just from secondhand notes.
- **No numeric portfolio limit is AURA's own ratified policy anywhere today.** The "2% risk/trade, 8% daily loss, 20% max-drawdown-halt, 1.0x ATR stop, 1.5x ATR trail, 24-candle time stop" parameters (traced to `03_CRYPTO_RESEARCH_EVIDENCE.docx` via a 360-day/144-combination backtest sweep) live only in `mexc_bot/config.py`, which explicitly disclaims them: *"This edge is THIN... Treat as 'best known settings,' not 'proven profitable.'"* No `aura_v053xx` file, config, or doc anywhere marks any numeric limit as AURA's decided policy. Per Martin's explicit instruction, this finding — not an assumption either way — settles the question: **every limit in this module defaults to unconfigured.**

---

## 2. Reuse scan (Martin's A/B/C/D framework, extended with TradePilot/ORION/Dark Wolf Sentinel this round)

| Component | Classification | Reasoning |
|---|---|---|
| TradePilot AI risk engine | (C) architecturally adjacent, not adopted | Confirmed (via the Final Baseline's own resolved open-item) to be a per-order execution-time gate, not a portfolio-aggregation engine. Its existence supports this module's design shape (a small, pure, deterministic decision function with explicit reject reasons) but nothing from it is imported. |
| ORION | (D) not applicable | Its role, per the Final Baseline, is the `.49` adversarial-challenge AI critique pattern (a second AI opinion on a proposal) — unrelated to portfolio-exposure enforcement. |
| Dark Wolf Sentinel | (D) not applicable | No portfolio-exposure or cross-venue aggregation logic found in it during this or `.43`'s survey. |
| `.31`/`.36` authorization guardrail shape | (B) adapted | The `_evaluate_guardrails()` → named-reason → never-trust-the-caller pattern is reused conceptually for `DimensionVerdict`/`EnforcementDecision`. The modules themselves are not modified or imported — deliberately, per Martin's "must not become an execution engine" instruction. |
| `mexc_bot/core/risk_manager.py`'s daily-loss-pct/drawdown-pct formulas | (B) already reused — one level removed | These formulas are already inside `.43`'s `compute_daily_loss`/`compute_max_drawdown` (confirmed in `.43`'s own reuse scan). `.44` does not re-derive them; it adds the limit-comparison and fail-closed data-quality layer on top of `.43`'s already-computed numbers. Its 8%/20% *threshold values* are explicitly **not** adopted (see §1) — only the *formula shape* was reusable, not the disclaimed numbers. |
| `.43`'s `PortfolioSnapshot`/`compute_exposure_dimensions`/`project_post_trade_exposure`/`check_mexc_leverage_cap` | (A) directly reused | `.44` is built entirely on top of these — no second position-aggregation model was created. `evaluate_hypothetical_trade()` calls `.43`'s own `project_post_trade_exposure()` rather than reimplementing a projection. |

---

## 3. What was newly implemented

**One new file: `aura_v05344_portfolio_exposure_enforcement.py`** (~430 lines). Pure-function library — no CLI, no scheduled entry point, no order-submission code path anywhere in it (confirmed by a static regression-test guard, §4).

### Decision model (Martin's own flowchart, implemented literally)

```
exposure data valid?
    NO  -> BLOCK   (stale snapshot / incomplete cross-venue data /
                     failed venue fetch / insufficient history yet)
    YES ->
applicable limit configured?
    NO  -> LIMIT_NOT_CONFIGURED
    YES ->
limit breached?
    YES -> BLOCK
    NO  -> PASS
```

Plus one addition the flowchart's binary doesn't separate: a dimension `.43` marks structurally `NOT_COMPUTABLE` (no data source anywhere in AURA, not a timing/venue problem) gets its own `NOT_COMPUTABLE` verdict — informational, never blocking, never fabricated. Treating a permanent structural gap the same as a transient data-quality failure would either deadlock trading forever on something that can never resolve, or require inventing a workaround; `NOT_COMPUTABLE` avoids both.

### `PortfolioLimits` — every field defaults to `None` / empty

```
max_portfolio_heat_ratio, max_asset_concentration_ratio, max_net_exposure_ratio,
max_leverage_ratio_by_venue: dict, mexc_leverage_cap,
max_daily_loss_pct_by_venue: dict, max_drawdown_pct_by_venue: dict
```

**No field has a built-in numeric default anywhere in the module.** This module does not choose a leverage cap, a concentration ceiling, or a daily-loss threshold on its own — see §1's audit finding. Every dimension without a supplied value reports `LIMIT_NOT_CONFIGURED`, never a silent PASS and never an invented number.

### Which limits already existed vs. remain unconfigured (Martin's explicit required disclosure)

| Limit | Already-existing AURA/DELTAX authoritative value? | `.44` default |
|---|---|---|
| `max_portfolio_heat_ratio` | **No** — no config anywhere | Unconfigured |
| `max_asset_concentration_ratio` | **No** | Unconfigured |
| `max_net_exposure_ratio` | **No** | Unconfigured |
| `max_leverage_ratio_by_venue` | **No** — `.25`'s 10%-of-equity sizing cap is a *position-sizing* rule, not a *leverage-ratio* limit, and is Alpaca-only | Unconfigured |
| `mexc_leverage_cap` | **No** — `.43`'s own report already flagged this absence explicitly | Unconfigured |
| `max_daily_loss_pct_by_venue` | **Candidate exists** (`mexc_bot/config.py`'s 8%) but explicitly disclaimed as "thin edge, not proven profitable," never ratified into `aura_v053xx` | Unconfigured |
| `max_drawdown_pct_by_venue` | **Candidate exists** (`mexc_bot/config.py`'s 20%), same disclaimer | Unconfigured |

**Every single limit is unconfigured today.** This is not a shortcut — it is the accurate, audited state of AURA's actual decided risk policy. Martin (or a future, explicitly-approved risk-configuration milestone) sets these; `.44` will start enforcing the moment a value is supplied, with no code change required.

### `.44` decisions that are newly introduced (i.e., not dictated by an existing spec, and therefore worth Martin's explicit review)

These are genuine design choices this milestone had to make, each documented in the module's own docstring for auditability:

1. **Aggregate dimensions (portfolio_heat, asset_concentration, directional_exposure) BLOCK if *any* configured venue's fetch failed** — even though the aggregate could technically still be computed from the venues that succeeded. Rationale: a partial sum silently understates true exposure (the exact "failed venue treated as zero exposure" failure mode Martin named explicitly) — not computing it at all, and saying why, is safer than reporting a number that looks complete but isn't.
2. **Per-venue dimensions (daily_loss, max_drawdown, leverage_exposure per venue, MEXC leverage cap) BLOCK only for the venue that actually failed** — a succeeding venue's own numbers are still evaluated normally. A failed MEXC fetch must not stop AURA from seeing that Alpaca's daily loss is fine (or not).
3. **`NOT_YET_AVAILABLE` (no same-day prior equity snapshot yet) is treated as BLOCK, reason `INSUFFICIENT_HISTORY`** — per Martin's literal fail-closed instruction ("cannot be proven safe" → BLOCK), even though `.43`'s own report calls this an expected, self-resolving bootstrap state rather than a bug. This means a freshly-started `.44` will BLOCK on daily-loss for the first same-day check until one same-day snapshot exists. Flagged here explicitly in case Martin wants a different bootstrap behavior once this is wired into a real runtime loop.
4. **A genuinely flat book (zero positions in a venue, or in the whole portfolio) is PASS/trivially-safe, not a data-quality failure** — distinguished from "positions exist but couldn't be priced" (`positions_excluded_no_notional > 0`), which DOES BLOCK. Determined using the snapshot's own position count directly, because `.43`'s own `asset_concentration`/`leverage_exposure` status field conflates these two cases under one `NOT_COMPUTABLE` label.
5. **A venue `NOT_CONFIGURED` (not wired into this deployment run) is `NOT_APPLICABLE`, not a data-quality failure, and does not count against aggregate completeness** — a venue nobody configured cannot make the aggregate "incomplete"; this is a deployment fact, not a broken fetch.
6. **`max_snapshot_age_seconds` has no default value** — deliberately a required keyword argument on both public entry points, because "how old is too old" is an operational choice the caller wiring this into a real runtime loop must make explicitly, matching this module's own refusal to invent numbers, even though it is a different *kind* of number (an engineering parameter, not a strategy-design threshold like leverage/concentration/loss limits).
7. **`consecutive_loss_limit` is reported as a 15th `NOT_COMPUTABLE` dimension** (alongside `.43`'s four) even though it isn't one of `.43`'s 12 — the original roadmap wording for `.44` names it explicitly, and no trade-outcome (win/loss) ledger exists anywhere in AURA to compute it from (`.29`'s intent ledger tracks order lifecycle state, not realized win/loss outcome). Reported for visibility rather than silently dropped, per the same "never hide a structural gap" principle as `.43`.
8. **`evaluate_hypothetical_trade()` evaluates asset_concentration against the CURRENT state only**, not a projected post-trade state — because `.43`'s own `project_post_trade_exposure()` does not include asset_concentration in its before/after overlay, and per Martin's explicit instruction not to expand `.43`'s capabilities from within `.44`, this module does not add that projection itself.

---

## 4. Tests

`tests/test_aura_v05344_portfolio_exposure_enforcement.py` — **29 tests**, all built on real `.43` data structures (`PortfolioSnapshot`, `VenueFetchStatus`, `PositionRecord` — not mocks), feeding `.44`'s actual public functions. Covers, per Martin's explicit list:

- **Clean portfolio**: flat book with no limits configured (ALLOW, everything `LIMIT_NOT_CONFIGURED`); a real position within every configured limit (ALLOW, everything `PASS`/`NOT_COMPUTABLE`).
- **Each individual limit breach**: portfolio_heat, asset_concentration, directional_exposure, leverage_exposure (venue-scoped), MEXC leverage cap, daily_loss, max_drawdown — one dedicated test per dimension.
- **Multiple simultaneous breaches**: a single position breaching heat, concentration, leverage, and the leverage cap all at once — confirms every breach is reported, not just the first.
- **Stale snapshot**: blocks everything immediately with a single `SNAPSHOT_STALE` verdict; a snapshot timestamped in the future is likewise blocked (`SNAPSHOT_TIMESTAMP_IN_FUTURE`); a snapshot within the freshness window is not penalized.
- **Partial/failed venue**: a failed MEXC fetch blocks all three aggregate dimensions with `VENUE_DATA_INCOMPLETE` (never silently computed as if MEXC held nothing); Alpaca's own per-venue dimensions are still evaluated normally despite MEXC's failure; a `NOT_CONFIGURED` venue is `NOT_APPLICABLE`, not penalized; a position that exists but couldn't be priced correctly BLOCKs asset_concentration (distinguished from a genuinely flat book).
- **`NOT_COMPUTABLE`**: all 5 structurally-uncomputable dimensions (`.43`'s 4 plus `consecutive_loss_limit`) are reported and never block the overall decision.
- **Missing configuration**: an extreme position (leverage 100x, huge notional) with zero limits configured never BLOCKs on a limit basis — every applicable dimension reports `LIMIT_NOT_CONFIGURED`.
- **Positions outside AURA's intent ledger**: a position with a `raw_source_id` matching no AURA ledger convention is still fully enforced — this module has no concept of "in the ledger" at all.
- **Fail-closed behavior**: insufficient history blocks even against a maximally generous configured limit; a flat venue's leverage is `PASS`/`FLAT_BOOK`, not penalized as unknown.
- **Deterministic/reproducible decisions**: identical inputs (including `now`) produce an identical `decision_hash` across repeated calls; different limits produce a different hash and a different verdict.
- **The hypothetical-trade (authorization-time) path**: `evaluate_hypothetical_trade()` blocks when a projected trade would breach a limit, allows when it stays within limits, does not mutate the original snapshot's positions, and is itself subject to the same staleness gate.
- **A static guard** confirming the module's executable code never references `place_order`, `submit_order`, `cancel_order`, or either authorization module's actual submission functions — enforcement-only by construction, not just by docstring claim.

Two test-authoring bugs were found and fixed during first-run verification (both test-data issues, not module bugs): (1) an initial `asset_concentration` test on a genuinely empty portfolio incorrectly expected `.43`'s own conflated `NOT_COMPUTABLE` status to short-circuit to a false BLOCK — traced to a real ambiguity in `.43`'s own status field (see decision #4 above) and fixed by disambiguating using the snapshot's position count directly, a module fix, not just a test fix; (2) two portfolio-heat test scenarios initially assumed equity from one venue only, when `.43` correctly aggregates equity across all configured venues — the tests were corrected to supply realistic multi-venue equity figures rather than changing the (correct) aggregation behavior.

## 5. Verification

- **New tests:** 29/29 passed.
- **Full regression suite:** 652/652 passed (was 623 prior + 29 new; +29, 0 removed, 0 modified elsewhere).
- **Lint (`ruff check`):** clean on both new files (0 errors).
- **Manual smoke test:** run directly (outside pytest) against hand-built `.43` snapshots with real breach/pass/not-configured combinations — confirmed correct decision output before the automated suite was written.

## 6. Known limitations / what remains open

- **Every portfolio limit is unconfigured.** `.44` cannot actually block a real trade today until Martin (or a future risk-configuration milestone) supplies real numeric limits. This is the correct, honest state given the audit finding in §1 — not a gap in this milestone's implementation.
- **Not wired into `.31`/`.36`'s authorization chain.** This milestone delivers the deterministic decision engine only, per Martin's "must not become an execution engine" scope. Connecting a `BLOCK` verdict to an actual authorization rejection is a distinct, separate step requiring its own explicit review (it touches the execution spine).
- **ATR-based sizing, stop-loss placement, and a live correlation check remain out of `.44`'s scope**, per Martin's explicit instruction not to expand `.43`'s computability from within `.44` — these require new data sources (a persisted per-position stop distance, a live cross-symbol return history) that belong to a later, separate capability milestone.
- **`asset_concentration` is not evaluated in the hypothetical-trade (pre-trade) path** against the projected post-trade state, only the current state — because `.43`'s own projection helper doesn't cover it and `.44` does not extend `.43`.
- **`max_snapshot_age_seconds` has no default** — whoever wires this into a real runtime loop must choose one explicitly; no recommendation is embedded in this module.

## 7. Commit

Checkpoint commit to be created locally after this report is saved (files: `aura_v05344_portfolio_exposure_enforcement.py`, `tests/test_aura_v05344_portfolio_exposure_enforcement.py`, this report). **Not pushed to GitHub**, per the standing rule.

## 8. Next

Per Martin's instruction: do not start `.45` until `.44` is verified and checkpointed (done, this report). Next: `.45` (Alpaca `EXECUTION_UNCERTAIN` resolution authority), `.46`→`.47`→`.48` (News/Sentiment/Elliott Wave), `.49` (AI proposal pipeline, amended scope), `.50` (Decision Engine) — then back to `.41` (Ghost Trades, now that `.50` will exist), then `.51`→`.53`, then the full post-`.53` crypto validation campaign.
