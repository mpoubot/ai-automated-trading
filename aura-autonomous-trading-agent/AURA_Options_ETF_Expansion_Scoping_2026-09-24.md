# AURA — Options & ETF Expansion Scoping

**Date:** 2026-09-24 (updated same day — see §7 changelog)
**Prepared for:** Martin
**Purpose:** Module-by-module build scope for adding ETF and options support to Track B (AURA), following the same milestone pattern already used to build the Alpaca equity execution floor (`.334`–`.338`). This is a planning document only — no code has been written or changed as part of producing it.

**Governance note, stated once and binding throughout:** DELTAX V1 and DELTAX_v2 are used below only as *design-pattern references* — named explicitly wherever a pattern is worth knowing about — never as implementation sources. Every module proposed below is to be independently re-derived from first principles (options-pricing textbooks, Alpaca's own API docs, AURA's own existing conventions), not adapted from DELTAX code. Where DELTAX V1/V2 have a relevant pattern, it's noted as "reference only" so the distinction stays visible module by module. The same disclosed-adaptation discipline applies to the Lablab hackathon competitor sources referenced in §3's new Phase O4B, per Martin's 2026-09-24 rule-relaxation ("same as DELTAX").

---

## 1. ETF — scope

This is close to already done. The instrument-metadata layer (`.334`) already classifies ETF as a first-class instrument type, and the equity execution adapter, authorization, replay-protection, and supervisor modules (`.335`–`.338`) treat ETF and STOCK identically — Alpaca's own API doesn't distinguish them at the order level. The target-architecture multi-asset table confirms every row (data, sessions, shorting, order types, tick/lot size) is "same as stocks" for ETFs.

Remaining work is curation and verification, not new engineering:

| Item | What it involves | Estimate |
|---|---|---|
| Universe curation | Decide which ETFs to add to the `.351`/`.352` pinned 27-symbol universe (or build a separate ETF universe file) — sector ETFs, broad-index ETFs, or both | 0.5–1 day |
| Shortability verification | Confirm Alpaca's `shortable`/`easy_to_borrow` metadata behaves the same for the specific ETFs chosen as it does for the existing equity universe (some sector/leveraged ETFs have different borrow characteristics) | 0.5–1 day |
| Test coverage | Add ETF fixtures to the existing `.334`–`.338` and `.351`/`.352` test suites so ETF symbols are exercised, not just asserted to be "the same as stocks" | 0.5–1 day |
| **Total** | | **~2–3 days** |

No new module numbers are needed for ETF support. The one adjacent idea worth flagging and explicitly *not* including here: ETF-pair or sector-rotation-style strategies are a natural fit for a widened ETF universe — sector rotation itself now has a first implementation (`aura_v05359_sector_rotation.py`, built 2026-09-24) but is still research-track-only and unvalidated for promotion; wiring it to a widened ETF universe would be a separate, later scoping exercise, not part of this document.

---

## 2. Options — why this is a different category of work

The target-architecture spec's own multi-asset table already grades this honestly: for options, almost every row reads "no existing implementation" or "DELTAX V1 only (reference)." Options aren't an incremental extension of the equity floor — they need their own data shape, their own pricing math, their own execution semantics (an order can be one to four legs, submitted atomically), their own exit logic (time-decay/expiry-driven, not a trailing stop), and — the part that's easy to underweight — their own trading signal, since none of AURA's existing technical indicators (EMA, RSI, MACD, Bollinger, ATR) say anything about which strike, which expiry, or which structure to trade.

**Options entitlement — confirmed 2026-09-24 (previously an open question):** Martin's Alpaca account has **Options Level 3** trading entitlement enabled (confirmed via account screenshot). This closes §6's original open decision #1. Phase O2 (market data) is therefore a data-adapter build against an already-entitled account, not a data-adapter-plus-account-enablement task — though whether Alpaca's options *market-data* subscription/breadth is sufficient (as opposed to trading entitlement, which is now confirmed) is still worth a quick check when O2 actually starts (§6, open decision #2 below, unchanged).

---

## 3. Options — module-by-module scope

Numbering below follows the existing `aura_v05NNN_*` convention, starting at the next free slot after `.358` (I'm calling it `.360`+ here as a placeholder sequence — you'll want to confirm the actual next-available number against whatever's landed in the repo by the time this work starts). Note: `.359` (sector rotation), `.360` (research full-evidence builder), and now also `.361` (portfolio enforcement decision journal, built 2026-09-24 same day as this update) are all real, built modules as of 2026-09-24, unrelated to this options sequence — the placeholders below still start fresh from the next actually-free number at build time, which by now means `.362` or later, not `.360`/`.361` as literally written below.

### Phase O1 — Instrument & metadata foundation
**`aura_v05360_options_instrument_metadata.py`** (pattern-analogue of `.334`)
Represents an options contract as a typed object: underlying symbol, strike, expiry date, right (call/put), multiplier (almost always 100), OCC-style symbol parsing/construction. Also defines the multi-leg position type — a spread or condor is a *structure* (an ordered list of legs, each with its own strike/expiry/right/side/ratio), not a scalar quantity like a stock position. This is the one place where DELTAX V1's `build_mleg_args` is worth reading as a reference for what fields a multi-leg order needs to carry — not for its code, but so the metadata schema doesn't have to be redesigned twice.
*Estimate: 2–3 days.*

### Phase O2 — Market data
**`aura_v05361_options_market_data.py`**
Fetches options chains (strikes/expiries available for a given underlying) and quotes/IV for specific contracts. Read-only, same fail-closed conventions as `.321`'s equity data adapter (never guess a stale or missing quote is close enough). Options trading entitlement is confirmed (§2); if Alpaca's options *data* isn't sufficient (breadth of chains, IV quality), this phase also needs a vendor decision (§6, open decision #2).
*Estimate: 3–5 days, more if a second data vendor needs integrating.*

### Phase O3 — Greeks engine
**`aura_v05362_options_greeks.py`** (pattern-analogue of `aura_v054_atr.py`'s "isolated reimplementation" style)
An isolated, independently-derived Black-Scholes (or binomial, if American-style early exercise matters for the chosen structures) pricing and Greeks module — delta, gamma, theta, vega, rho — built from standard options-pricing formulas and validated against known reference values (e.g., published Black-Scholes worked examples), not against DELTAX V1's `quantum_catalyst.py` output. This module should be pure and heavily unit-tested, the same way `.054_atr.py` was kept isolated from the rest of the pipeline.
*Estimate: 4–6 days, including validation against reference values.*

### Phase O4 — Execution adapter
**`aura_v05363_options_execution_adapter.py`** (pattern-analogue of `.335`)
Submits options orders to Alpaca — both single-leg and multi-leg. The multi-leg case is the genuinely new engineering problem here: a 2-to-4-leg combo order needs all-or-nothing submission semantics (you don't want three legs of an iron condor filled and the fourth rejected), which is structurally different from anything the existing equity/crypto adapters handle. Needs its own classification of partial-fill/rejection states distinct from `.22`/`.335`'s single-leg enum.
*Estimate: 5–8 days — this is the highest-uncertainty item in the whole scope, since it depends on exactly what Alpaca's multi-leg order API guarantees.*

### Phase O4B — Assignment / exercise reconciliation (new, added 2026-09-24)
**`aura_v05368_options_assignment_reconciliation.py`** (placeholder number — depends directly on O1's contract metadata and O4's execution adapter existing first, since there is nothing to reconcile against before then)

Added following the 2026-09-24 Lablab hackathon 26-repo competitor audit (`AURA_Lablab_Hackathon_26Repo_Competitor_Audit_2026-09-24.md`, ranked finding #2). Two independently-built hackathon systems — ThetaTrap (`execution.py::reconcile_account_activities`) and optionwright (`reconciler.py`), both VALIDATED with passing test suites — converge on the same answer for options assignment/exercise: **never try to trade out of it.** Detect the mismatch between AURA's own book and the broker's real account activity (an assignment notice, an exercised contract, an expired ITM position), and freeze that underlying to manual-only until a human resolves it. Neither system attempts automated remediation.

This has zero prior art anywhere in AURA today — Track A/B are both equities-only so far, and equities have no assignment/exercise concept. It is also, per the audit, the single most option-specific "do this before going live" lesson found across all 26 competitor repos, which is why it's called out as its own phase rather than folded into O4 or O9.

Design sketch, not yet built: on each reconciliation cycle, diff AURA's own recorded options positions (from `.29`'s intent ledger, extended for options, or an options-specific equivalent) against the broker's actual account activity feed. Any position AURA didn't expect to change — an assignment, an exercise, an expiry-driven auto-liquidation — trips a hard freeze on that underlying (no new orders, existing orders in that underlying cancelled) until Martin manually clears it. This is a detection-and-freeze module, not a remediation engine — consistent with the ThetaTrap/optionwright precedent and with AURA's own existing fail-closed philosophy (e.g. `.44`'s "cannot prove safe → BLOCK", never a synthetic "assume it's fine").

*Estimate: 3–5 days once O1/O4 exist. Cannot start meaningfully before then — this is a reconciliation module with nothing to reconcile against until options positions can actually be opened.*

### Phase O5 — Authorization and replay protection
**`aura_v05364_options_execution_authorization.py`** / **`aura_v05365_options_replay_protected_consumption.py`** (pattern-analogues of `.336`/`.337`)
Same architecture as the equity pair: `ALLOWED_ENVIRONMENTS = frozenset({"PAPER"})` with LIVE structurally absent, independent recomputation of safety state rather than trusting caller-supplied flags, atomic never-released claims. This is the one phase that's mostly mechanical — the pattern from `.336`/`.337` transfers cleanly, just re-typed for options intents.
*Estimate: 2–3 days.*

### Phase O6 — Exit engine
**`aura_v05366_options_exit_engine.py`**
Expiry/DTE-aware exits — closing or rolling a position as it approaches expiry, plus structure-appropriate profit-target/stop-loss logic for defined-risk spreads. This is a different exit philosophy from `.054_exit_engine.py`'s ATR trailing stop (which assumes an open-ended long position, not a time-decaying defined-risk structure), so it's a new module rather than an extension of the existing one. DELTAX V1's DTE-based exit rules (7–21 DTE, no 0DTE) are a reasonable reference starting point for what constraints to consider, independently re-implemented.
*Estimate: 3–5 days.*

### Phase O7 — Cost model
**`aura_v05367_options_cost_model.py`**
Per-contract commission plus realistic bid-ask spread cost (options spreads are proportionally much wider than equity spreads and vary heavily by liquidity) — replacing the flat 0.10% round-trip assumption used in the equity/crypto research track, which doesn't transfer to options.
*Estimate: 1–2 days.*

### Phase O8 — Risk and portfolio exposure extension
Extends `.343`/`.344` (or adds options-specific siblings) to compute Greeks-based exposure — net portfolio delta, gamma, vega — rather than the current share-count/notional model, plus new limit types (max portfolio delta, per-position max loss on defined-risk structures). DELTAX V1's hard per-position (1%) and portfolio (5%) risk caps are a reasonable reference for the shape of these limits, independently re-derived and re-justified for AURA's own risk tolerance rather than copied as numbers. Note: `.344` itself gained a general-purpose correlation-group concentration dimension and a decision journal (`.361`) on 2026-09-24 (unrelated to options, see the portfolio-hardening completion report of the same date) — O8 should build on that already-extended `.344`, not the pre-2026-09-24 version.
*Estimate: 4–6 days.*

### Phase O9 — Supervisor integration
Extends `.338`'s common execution supervisor (or adds an options-specific supervisor following the same shape) so options orders flow through the identical fail-closed, PAPER-only, kill-switch-gated path everything else uses — no separate, weaker path for the new asset class.
*Estimate: 2–3 days.*

### Phase O10 — Signal / strategy research (open-ended, not an engineering estimate)
Everything above builds the *plumbing*. It does not produce a trading edge. Technical indicators don't translate to options selection — you need a strategy hypothesis (which strikes, which expiries, which structures, under what regime). DELTAX V1's probability-of-touch/expectancy-gate approach is the only reference pattern that exists anywhere in the estate, and its own team explicitly states they never validated a real edge with it. Re-deriving that approach independently, backtesting it, and then clearing your own Strategy Registry's nine required evidence categories (`.339`: out-of-sample, walk-forward, permutation/matched controls, cost sensitivity, parameter-plateau, unseen-symbol validation, regime stability, prospective evidence, paper-execution evidence) before anything is eligible to paper-trade is genuinely open-ended research, not a schedulable engineering task. It's realistic to budget several weeks for a first pass at this and still come out the other side with a "no validated edge" result — that's what happened to DELTAX V1's own team, and it's also what happened to Track A's MEXC momentum strategy after a comparably rigorous process.

---

## 4. Effort summary

| Phase | Deliverable | Estimate |
|---|---|---|
| ETF | Universe curation + verification + tests | 2–3 days |
| O1 | Options instrument/metadata | 2–3 days |
| O2 | Options market data (chains/IV) | 3–5 days |
| O3 | Greeks engine | 4–6 days |
| O4 | Multi-leg execution adapter | 5–8 days |
| O4B | Assignment/exercise reconciliation (new) | 3–5 days |
| O5 | Authorization + replay protection | 2–3 days |
| O6 | DTE-aware exit engine | 3–5 days |
| O7 | Options cost model | 1–2 days |
| O8 | Greeks-aware risk/exposure | 4–6 days |
| O9 | Supervisor integration | 2–3 days |
| **Engineering subtotal (O1–O9, incl. O4B)** | | **~29–46 focused person-days (≈6–9 weeks)** |
| O10 | Strategy research/validation | Open-ended; budget several additional weeks for a first pass, with no guarantee of a validated result |

This is deliberately a wider range than a single-track project like the equity floor, because three phases (O2, O4, O4B) carry real external-dependency or novel-engineering risk — options data breadth/vendor choice, the exact guarantees Alpaca's multi-leg order API provides, and assignment/exercise handling having zero prior art in AURA — that can't be fully pinned down until they're actually opened up and tested against a real account.

---

## 5. Sequencing and dependencies

O1 (metadata) has no dependencies and can start immediately. O2 (data) should start in parallel once the entitlement question is answered — it now is (§2), so O2 can start as soon as engineering time is allocated. O3 (Greeks) depends only on O2 for real quote/IV data, though its pricing math can be built and validated against synthetic inputs before O2 lands. O4 (execution adapter) depends on O1 and benefits from O2 for realistic testing, but is otherwise independent. **O4B (assignment/exercise reconciliation) depends on both O1 and O4** — it has nothing to reconcile against until options positions can be opened — and should be sequenced immediately after O4, before O9's supervisor integration ties everything together, so no options order flow goes live-paper without it. O5 (auth/replay) depends on O4 existing to authorize against. O6 (exits) and O7 (cost model) depend on O1/O3 for contract/Greeks data but not on each other. O8 (risk) depends on O3 (needs Greeks to compute exposure) and ties into the existing `.343`/`.344` pair (now already extended once, 2026-09-24, for correlation-group concentration and a decision journal — see O8's note above). O9 (supervisor) is last, tying O4/O4B/O5/O8 together the same way `.338` did for the equity floor. O10 (research) can start conceptually at any point but can't produce anything tradeable until O1–O9 give it something to execute against, and shouldn't be rushed to fit the engineering timeline — it's the part where quality actually matters most.

The ETF work is fully independent of all of the above and could be picked up on its own at any time.

---

## 6. Open decisions for you

1. ~~**Options data/entitlement**: does the Alpaca account already have options trading and market-data access enabled?~~ **Resolved 2026-09-24 — Options Level 3 confirmed enabled.** (Options *market-data* breadth/quality, as opposed to trading entitlement, is folded into open decision #2 below.)
2. **Vendor fallback**: if Alpaca's options data isn't sufficient (breadth of chains, IV quality), is a second data vendor in scope, or should options data be deferred until Alpaca's own offering is confirmed adequate?
3. **Structure scope**: DELTAX V1's reference pattern covers defined-risk structures (verticals, iron condors). Is that the intended scope for AURA too, or should single-leg (long calls/puts) be in scope as a simpler first cut before multi-leg?
4. **Sequencing relative to Track B's frozen weights**: Track B's live chain currently ABSTAINs on every cycle by design (frozen `technical_weight`/`short_technical_weight` at `0.0`). Does it make sense to prioritize options/ETF plumbing now, or does resolving that open decision (un-freezing the existing equity signal) take priority first, since it's a smaller, already-built-and-tested lever?
5. **Track A**: Track A (mexc_bot) is paused pending Track B reaching a working operational paper-trading state. Options/ETF work is additional Track B scope — worth confirming this doesn't change your view on when Track A should resume.

---

## 7. Changelog

- **2026-09-24 (update):** Logged confirmed Options Level 3 entitlement, closing open decision #1. Added Phase O4B (assignment/exercise reconciliation), sourced from the same-day Lablab hackathon 26-repo competitor audit, ranked finding #2 — a genuine new gap with zero prior AURA art, sequenced after O4/O1 since it has nothing to reconcile against before then. Updated the effort table, sequencing section, and O8's note to reflect `.344`'s same-day, options-unrelated hardening pass (correlation-group concentration + decision journal, `.361`) — and noted that `.361` being a now-real module means the options placeholder numbering below effectively starts at `.362`, not `.361` as literally written.
- **2026-09-24 (original):** Initial scoping document.

---

*This document proposes scope and sequencing only. No module listed above has been built except where explicitly noted (ETF's underlying `.334`–`.338` equity floor, `.359`/`.360` research-evidence wiring, and `.344`/`.361`'s correlation-group/journal hardening — none of which are options-specific). Module numbers for the options phases above are placeholders pending confirmation of the actual next-available number in the repo at build time.*
