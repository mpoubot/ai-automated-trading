# AURA v0.5.3.50 — Completion Report

**Date:** 2026-09-13
**Milestone:** Formal Decision Engine + deterministic, LLM-free critic
**Module:** `aura_v05350_decision_engine.py`
**Tests:** `tests/test_aura_v05350_decision_engine.py`
**Status:** Complete. Stopping here per instruction — `.41` not started.

---

## 1. Why this milestone matters (Martin's framing)

`.49` proved AURA can safely *ask* an AI for an opinion. `.50` proves AURA can make a trading *decision* without trusting that opinion. `.50` is the deterministic authority that sits between `.49` (AI proposes/challenges) and the already-built deterministic Risk/Execution spine (`.33`–`.38`):

**AI proposes/challenges → `.50` deterministically DECIDES → `.33`–`.38` deterministically enforce.**

Nothing in `.50` delegates the final decision to an LLM. Nothing an LLM produces — a `.49` `AIProposal.stated_confidence`, or a `.49` `Critique` — can ever increase `.50`'s output score or flip its decided direction. `.50`'s output is explicitly a Level 3 Trading Decision but is **not** itself a `CanonicalExecutionIntent`: it carries no qty/price/order_type/broker field. Sizing and final order construction remain downstream, deterministic, and out of scope here.

## 2. Reuse-first audit (performed before writing any code)

| Source | Classification | Disposition |
|---|---|---|
| Hermes Trader `reasoning/critic.py` / `reasoning/calibration.py` | **C** | Closest analog to a "deterministic, LLM-free critic" anywhere in the corpus, and the source named in AURA's own Master Roadmap. Not reused directly: every threshold (win-rate buckets, adjustment magnitudes, confidence caps) is a hardcoded, invented constant with no derivation — conflicts with this project's "never invent numbers" discipline. It also depends on a live realized-outcomes SQLite table that does not exist anywhere in AURA. What *was* reused (**B**, shape only): a critic that reasons over structured historical/corroboration data, produces an issues list plus a derived pass/fail verdict, and sits between research output and a downstream decision step without ever calling an external model itself. |
| `.39` `SOURCE_KINDS` vs `.33` `SOURCE_KINDS` | — | Discovered two incompatible three-way source-kind vocabularies already live in the codebase (`.39`: `DETERMINISTIC_RESEARCH/AI_PROPOSED/HUMAN_PROPOSED`; `.33`: `DETERMINISTIC_SIGNAL/AI_PROPOSAL/HUMAN_OVERRIDE`). Escalated to Martin rather than guessed — resolved below. |
| `.44` `_check_snapshot_freshness` | **A** | Reused in shape for `check_evidence_freshness` — required, no-default `max_evidence_age_seconds`; BLOCK on stale OR future-dated timestamps. This project's own established pattern. |
| AlphaPilot `decision_engine.py` (competitor sweep) | Anti-pattern, **not adopted** | Fuses decision-making with qty/strike/stop-loss/price generation in one step — exactly the pattern `.50` must avoid. Tie-breaks with a bare `list.sort()` and no documented rationale. |
| DELTAX v2 / CAURA / BABIL / optionwright / mexc_bot | — | No genuine multi-source arbitration engine found anywhere in the sweep. `.50` is confirmed greenfield — a novel AURA capability, not an import target. |
| `.46` / `.47` / `.48` output contracts | Read in full | No cross-module code in this repo combined them before `.50`; `.49`'s own docstring explicitly deferred this wiring to "most likely `.50`." Used to build the real candidate-shortlist integration (`build_candidate_evidence`). |
| `.49` `apply_challenge_penalty` | Reused verbatim | Not reimplemented — `.50` imports and calls `.49`'s own function rather than duplicating its subtract-only penalty logic. |

## 3. Martin's scoping decisions and how each was implemented

1. **Source-kind vocabulary: adopt `.33`'s (`DETERMINISTIC_SIGNAL / AI_PROPOSAL / HUMAN_OVERRIDE`).** `.50` dynamically loads `.33` read-only, purely to read its `SOURCE_KINDS` constant live — never a copy-pasted value that could drift from what `.38` already validates downstream. Every `TradingDecision.source_kind` produced by `.50` is `"DETERMINISTIC_SIGNAL"`, because every decision is the output of `.50`'s own deterministic code, never an AI verdict standing in for it — even when a `.49` proposal/critique contributed evidence to the process. A dedicated governance test locks this in.

2. **Critic's data source: structured data already available now, not a new realized-outcomes store.** `run_deterministic_critic` checks cross-source directional agreement between `.47`'s sentiment and `.48`'s (single-valid-candidate) wave direction, `.47`'s own `corroboration_status`, and `.48`'s `ambiguity_status` — all already-computed, already-typed fields. It does **not** check historical win-rate; disclosed as a known limitation below.

3. **Ranking: sequential penalty-only chain.** The deterministic base score (`compute_base_rank_score`, built purely from `.47`/`.48` evidence) is the only thing that determines whether a candidate is positive or negative. `.49`'s AI `stated_confidence` is never added to any score — it is carried on `TradingDecision` purely as informational/audit context. `.49`'s challenge penalty (reused verbatim) and `.50`'s own deterministic critic penalty can only *subtract* from the base score.

4. **Shortlist eligibility: at least one source with usable evidence, not all three required.** `is_shortlist_eligible` returns true if any of sentiment (sufficient corroboration), wave (a single valid candidate), or raw news presence is true. Missing sources are recorded (`sources_present`) but never penalized.

## 4. Direction is decided before penalties are applied

`base_score_direction` computes `LONG_LEANING` / `SHORT_LEANING` / `NO_DIRECTIONAL_EVIDENCE` from `base_rank_score` alone — a number built exclusively from `.47`/`.48`'s deterministic evidence, computed **before** any `.49` AI proposal is even generated. `.49`'s challenge penalty and `.50`'s own critic penalty are then applied to produce `final_rank_score`, which only ever controls whether a leaning clears `decision_threshold` into `DECIDE_LONG`/`DECIDE_SHORT`, or falls back to `NO_TRADE`. A penalty can never turn a LONG-leaning candidate into a SHORT decision or vice versa — it can only weaken conviction toward no action. This is the concrete, testable implementation of "AI proposes/challenges but cannot author, size, or flip a trading decision," and is directly covered by tests (see §7).

## 5. Distinctness from `.49` (Final Baseline §6 acceptance criterion 3 — now complete)

`.49`'s `Critique` (AI-vs-AI, requires an `llm_client`, carries `.concerns`) and `.50`'s `DeterministicCritique` (LLM-free, **no** `llm_client` parameter anywhere in `run_deterministic_critic`'s signature, carries `.issues`) are deliberately different dataclasses with deliberately different field names. This is structural, not cosmetic:

- `.49`'s `apply_challenge_penalty(score, critique, *, penalty_per_concern=...)` reads `critique.concerns` — passing it a `DeterministicCritique` raises `AttributeError` (no `.concerns` attribute exists on it).
- `.50`'s own critic-penalty step reads `.issues` — passing it a `.49` `Critique` likewise raises `AttributeError` (no `.issues` attribute exists on it).

Neither can be silently substituted for the other. This is proven by dedicated tests (not merely asserted by convention): `test_ai_critique_cannot_be_substituted_into_deterministic_critic_penalty_path`, its inverse using `.49`'s own `apply_challenge_penalty`, an `inspect.signature` check proving `run_deterministic_critic` has no `llm_client` parameter, a test proving `.50`'s critic is pure/deterministic (same input → same output, no injected client) while `.49`'s AI critique genuinely varies by injected client, and `test_neither_49_nor_50_critic_has_veto_over_the_other` proving `decide()` never hard-vetoes on either critique's `passed` flag alone. This closes the gap explicitly disclosed as outstanding in the `.49` completion report.

## 6. Governance constraints respected

- No qty/price/side/order_type/broker field exists anywhere on `TradingDecision` — checked structurally by a deny-list field-name test, not just by inspection.
- No trade/order-submission function name exists anywhere in the module — checked by a source-level test.
- `source_kind` is always `"DETERMINISTIC_SIGNAL"`, read live from `.33`, never hardcoded independently.
- `stated_confidence` is never referenced in any score-mutating line — checked by a structural source-level test.
- `.33`, `.46`, `.47`, `.48`, `.49` are not modified. `.50` is purely additive, consuming each through its existing public functions/constants only.
- `.49` was not modified (no defect was found in it requiring a fix; its outstanding acceptance-criterion-3 gap is closed by `.50`'s new code and tests, not by editing `.49`).

## 7. Tests

`tests/test_aura_v05350_decision_engine.py` — **51 tests**, organized into the 13 categories specified:

1. Candidate construction from `.46`/`.47`/`.48` (usable/not-usable per source, `candidate_id` determinism, eligibility)
2. Deterministic ranking (weight validation, sentiment-only/wave-only/combined scoring, ambiguous-wave-contributes-zero, direction mapping)
3. `.49` proposal integration (generated only when directional evidence exists, skipped otherwise)
4. `.49` adversarial penalty integration (penalty applied correctly via a sequenced fake client; no penalty when the challenge passes clean)
5. Deterministic critic (conflict/insufficient/ambiguous/invalidated flagging, clean pass, no-`llm_client`-parameter structural proof)
6. Conflicting AI proposal vs. deterministic evidence (AI confidence never added to score even at 0.99 stated confidence; AI cannot flip a decided direction)
7. Missing/invalid evidence (`ABSTAIN` on news-only/no-directional-evidence; negative-threshold/negative-penalty rejection)
8. Stale data (freshness checks; `build_shortlist` exclusion of stale/future-dated evidence)
9. Multiple candidates (independent per-candidate decisions, e.g. BTC and ETH decided independently)
10. Tie/ambiguity (ambiguous wave never resolves to a direction; identical final scores across different candidates decided independently and correctly)
11. Fail-closed behavior (reproducibility of `decision_hash`; `NO_TRADE` below threshold; `TradingDecision.__post_init__` rejects invalid direction/outcome)
12. Proof no LLM output can authorize execution (deny-list field-name check; no trade/order function names in the module; `source_kind` always `DETERMINISTIC_SIGNAL` read live from `.33`; structural check that `stated_confidence` never appears in a score-mutating line)
13. `.49` vs `.50` distinctness/non-substitutability (different dataclass types/field names; `llm_client`-parameter presence/absence via `inspect.signature`; literal `AttributeError` cross-substitution tests in both directions; proof `.50`'s critic is pure while `.49`'s genuinely varies by client; proof neither critique's `passed` flag alone vetoes `decide()`)

All 51 pass. `pyflakes` clean on both the module and the test file (one dead-variable test bug found and fixed — see §8).

**Full regression suite:** `python3 -m pytest tests/ -q` → **897 passed** (846 prior + 51 new), run twice to confirm. One transient failure was observed on the first full-suite run in an unrelated, pre-existing `.40` concurrency test (`test_mexc_concurrent_attempts_with_audit_trail_exactly_one_wins`, a 15-thread contention test) — confirmed flaky under full-suite load, not a `.50`-caused regression: it passed 3/3 in isolation immediately after, and the full suite then passed clean (897/897) on a second run with no code changes in between. `.50` has no dependency on `.40` and touches none of its code.

## 8. Bugs found and fixed this milestone

- **Test bug (not a module bug):** `test_decide_applies_ai_challenge_penalty_to_final_score` assigned a `client` variable that was immediately superseded by the actually-used `seq` (`SequencedClient`) instance passed to `decide()` — a dead assignment flagged by `pyflakes`. Fixed by removing the stray assignment; no module code was touched.
- No module-level bugs were found in `aura_v05350_decision_engine.py` itself — it compiled, lint-passed, and its own 51 tests passed on the first execution after the dead-variable test fix.

## 9. Persistence

None. Every function is a pure function of caller-supplied inputs (typed evidence from `.46`/`.47`/`.48`, an injected `.49` module reference and `LLMClient`, required research parameters) plus optional `now`, mirroring `.47`/`.48`/`.49`'s own precedent. Every audit-relevant fact (base score, final score, every issue/concern, every threshold used, the exact candidate/proposal/critique identities involved) is carried on the returned `TradingDecision`, including a `decision_hash` binding it to its own exact content (mirrors `.44`'s `EnforcementDecision.decision_hash` convention). A future milestone is expected to persist these for a durable audit trail; `.50` provides the complete in-memory record.

## 10. Known limitations (disclosed, not silently worked around)

- **No historical win-rate / realized-outcome check** — Martin's explicit scoping choice. The deterministic critic checks cross-source agreement and each source's own quality flags, not track record. AURA has no persisted realized-outcomes store today; building one was explicitly out of scope for this milestone.
- **Per-candidate decisions, not portfolio-wide "pick one winner" arbitration.** `.50` decides each candidate independently — it does not select a single "best" candidate among several competing symbols. This is a deliberate reading, not an oversight: AURA's portfolio can hold multiple positions, and `.44` (Portfolio Exposure Enforcement) already exists as the portfolio-wide arbitration layer. Introducing a second, competing cross-candidate ranking mechanism here would duplicate `.44`'s responsibility. `.50`'s literal contract text ("arbitrates ... proposals into one output") is read here as "one output per candidate," not "one candidate portfolio-wide." **Flagging explicitly for Martin to correct if a different reading was intended** — this is the one open interpretation question from this milestone.
- All research-magnitude parameters (`sentiment_weight`, `wave_weight`, `decision_threshold`, `ai_penalty_per_concern`, `critic_penalty_per_issue`, `max_evidence_age_seconds`) are required, no-default caller/configuration-supplied values — `.50` provides the mechanism, never invents the specific values a real trading decision would use.
- No live end-to-end smoke test against a real Anthropic API call was run for the `.49` integration path (mirrors `.49`'s own disclosed limitation) — all tests use fake/scripted LLM clients.

## 11. Reproducibility / auditability

Every `TradingDecision` carries its own `decision_hash` (sha256 over a stable-JSON payload of its own content), the exact `base_rank_score`/`final_rank_score`/`decision_threshold` used, every deterministic-critic issue and AI-challenge concern, the `.49` proposal's status/stated confidence, and a `decided_at` timestamp — sufficient to reconstruct and re-verify any decision from its recorded inputs, matching this project's established hash-chain/audit convention (`.33`, `.44`, `.49`).

## 12. Governance summary

- AI (`.49`) proposes and challenges; it cannot author, size, or execute.
- `.50` decides deterministically; it cannot call an LLM and cannot be short-circuited by one.
- `.33`–`.38` (unmodified) remain the only path to an actual order.
- No code path anywhere in `.50` allows an AI output to author, size, or flip a trading decision — proven structurally, not just by convention.

## 13. Checkpoint

Local commit only, not pushed (per explicit instruction). Files staged: `aura_v05350_decision_engine.py`, `tests/test_aura_v05350_decision_engine.py`, this report. No other untracked files touched.

## 14. Next

Stopping here per instruction. Awaiting Martin's review of this report — particularly the per-candidate-vs-portfolio-wide interpretation flagged in §10 — before proceeding to `.41` (Ghost Trades / Counterfactual-Shadow-Decision Ledger, whose compound dependency on both `.39` and `.50` is now satisfied).
