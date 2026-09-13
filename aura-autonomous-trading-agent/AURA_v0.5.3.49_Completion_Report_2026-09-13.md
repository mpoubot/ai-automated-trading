# AURA v0.5.3.49 — Completion Report: AI Proposal Generation Pipeline + AI Capability Contract

**Date:** 2026-09-13
**Milestone:** `.49` (v0.5.5 Final Implementation Baseline, amended scope §6)
**Status:** Implemented, tested, verified.

---

## 1. Reuse-first audit (performed before writing any code)

A dedicated audit subagent read the governing spec (`AURA_v0.5.5_Final_Implementation_Baseline_2026-09-12.md` §6) in full and searched AURA's own `.31`–`.48` chain, all DELTAX repos, Hermes Trader, every competitor repo under `/home/claude/work/competitors/`, `mexc_bot/`, CAURA, BABIL, and optionwright for LLM-proposal, typed-validation-boundary, adversarial/critic, sanitizer, and confidence-calibration code.

**Key finding**: `.49`'s two named source patterns — ORION (adversarial-challenge) and Catalyst Surface Agent (non-expansive repair) — were both marked "Not located" in this project's own prior competitor audit. The acceptance criteria describe the pattern, not verified source. Nothing in AURA itself implements any of `.49` — confirmed by grep; `.46`'s and `.39`'s own comments explicitly defer this scope to `.49`.

**New finding this audit** (not one of the 9 previously-named/verified submissions — flagged as unverified, not run through this project's Implemented/Demonstrated/Validated/Claimed process): `/home/claude/work/competitors/198ea29e-aegismain/` ("Aegis"), an options-trading agent whose architecture maps closely onto `.49`'s requirements:
- `aegis/agents/llm.py` — provider-agnostic `LLMClient` protocol, degrade-to-`NullClient`-on-missing-key. **(B, adapted)** — reused in shape for this module's `LLMClient`/`NullClient`/`AnthropicClient`, Anthropic-only per your scoping decision (Aegis's Gemini backend was not ported).
- `aegis/agents/critic.py` — the closest match found anywhere to the adversarial-challenge role: the model's schema has no approval field, `passed` is derived from `concerns` being empty, and it fails closed on any client/API/parse error. **(B, adapted)** — reused in shape for `challenge_proposal`/`Critique`; the options-specific prompt content was not reused, AURA's own prompt/fields were written independently.
- `aegis/core/proposal.py` — content-hash-bound proposal (approval/critique is bound to a hash of exact content, voided by any edit). **(B, adapted)** — reused conceptually in `_proposal_content_hash`, reimplemented with this repo's own `stable_json`/`sha256_text` convention (first established in `.33`), not Aegis's Decimal-normalizing canonicalizer.
- No sanitizer implementing true subtract-only repair exists anywhere in the searched corpus — Aegis's critic fails closed rather than repairing. **(D)** for literal non-expansive repair specifically — see §3.

Already-validated patterns (per this project's own prior audits, not re-verified here): PrintRunner's closed-output-space LLM constraint and Hermes Trader's rolling confidence calibration, both named in the Final Baseline as feeding the AI Capability Contract. `.49` implements its own, more conservative version of the closed-output-space idea (see §2) rather than importing PrintRunner code directly, which was not found to be directly portable.

## 2. Scoping decisions (confirmed via `AskUserQuestion`) and how each was implemented

| Decision | Implementation |
|---|---|
| **LLM provider: Anthropic only** | `LLMClient` protocol + `AnthropicClient` + `NullClient`, adapted from Aegis's `llm.py`. No Gemini or other backend. `build_llm_client()` degrades to `NullClient` when `ANTHROPIC_API_KEY` is unset — a missing key costs a rejected proposal, never a crash or an unsafe fallback. |
| **Typed contract shape: frozen dataclasses** | `Candidate`, `AIProposal`, `Critique` are all `@dataclass(frozen=True, slots=True)`, matching every other `.31`–`.48` module. No Pydantic dependency introduced. |
| **Non-expansive repair: fail closed on any invalid field** | This is a **deliberate, documented deviation** from acceptance criterion 2's literal text ("may only remove or null the invalid field... never invent, substitute, or expand a value" implies partial recovery of the valid remainder). You were shown this exact tension before choosing: no reference implementation of true subtract-only repair exists anywhere in the audited corpus (Aegis, the closest analog, also fails closed). Concretely implemented: `generate_proposal_for_candidate` rejects the ENTIRE proposal on ANY validation failure (missing/wrong-type/out-of-range field, or a `candidate_id` outside the closed shortlist) — there is no code path that returns a proposal with some fields kept and others nulled. Every rejection carries an exact `rejection_reason`, which is this module's implementation of "every repair is logged to the audit trail" — the log entry IS the rejection, not a silent drop. A dedicated test corpus (`test_no_partial_repair_corpus`) proves this for eight different malformed-response shapes, each with at least one valid field alongside an invalid one — `thesis`/`stated_confidence` are `None` in every case, never partially retained. |
| **Adversarial-challenge scope: narrow (ranking-score critique only)** | `challenge_proposal`/`Critique` produce ONLY a `concerns` list feeding `apply_challenge_penalty`. Aegis's broader "full second-opinion review" pattern was explicitly not adopted. `Critique` has no field for approval — `passed` is derived (`not concerns`), never asserted by the model; `test_challenge_ignores_an_extraneous_approval_field_derivation_is_real` proves this by injecting an extraneous `"passed": true` alongside real concerns and confirming the derived verdict is still `False`. |

## 3. Closed-output-space enforcement (this module's implementation of the PrintRunner-named pattern)

`.49` goes further than "choose from a list": each proposal-generation call presents the AI with exactly ONE candidate at a time, and the AI is only asked to echo back that SAME `candidate_id`. Any mismatch — including a plausible-looking hallucinated id — is a hard rejection (`test_rejected_on_candidate_id_mismatch_closed_output_space`). `.49` does not build the candidate shortlist itself; that is caller-supplied (a future integration point, most likely `.50`, is expected to build it from `.46`/`.47`/`.48`'s deterministic evidence) — `.49` is a pure function of whatever shortlist it's given.

## 4. Governance constraints respected

- **Structurally, not just conventionally, non-authorizing**: `AIProposal` and `Critique` have no field capable of expressing quantity, price, side, order type, broker call, or execution intent — there is no field to repurpose into one. `test_aiproposal_has_no_execution_field_names` / `test_critique_has_no_execution_field_names` / `test_candidate_has_no_execution_field_names` check every dataclass field against a deny-list (`qty`, `price`, `side`, `order_type`, `broker`, `execution_intent`, `signal`, `direction`, `size`, `leverage`, `time_in_force`, `venue`, etc.).
- **No path to `.33`'s execution spine**: `test_module_never_imports_or_constructs_canonical_execution_intent` confirms no import of `.33`, no dynamic cross-module import of any kind, and no `CanonicalExecutionIntent(` constructor call anywhere in the module.
- **Critique binds nothing but a ranking score**: `Critique.passed` is consulted by exactly one function in this module, `apply_challenge_penalty`, which adjusts a caller-supplied `base_rank_score` — never an authorization/order decision. `base_rank_score` itself is out of scope for `.49` (expected from a future ranking capability, most likely `.50`).
- **Level 1/2 only**: no function anywhere in this module has a path to an order, position, sizing, or authorization decision (`test_module_has_no_trade_or_order_function`).
- **No invented research numbers**: `penalty_per_concern` (the magnitude by which a challenge reduces a ranking score) is a REQUIRED keyword argument with no default — mirroring `.47`'s `decay_window_hours`/`min_source_count` convention. Omitting it raises `TypeError` (`test_apply_challenge_penalty_requires_keyword_arg`); a negative value raises `AIProposalPipelineError`.

## 5. Reproducibility / auditability

Every `AIProposal` carries the exact candidate/symbol requested, `shortlist_hash` (binds it to the exact shortlist it was generated against — `build_shortlist_hash` is order-sensitive, so reordering the shortlist changes the hash), `model_name`, `generated_at`, the verbatim `raw_response` (preserved even when rejected, for audit), `status`, and `rejection_reason`. Every `Critique` carries `proposal_content_hash` (binds it to the exact proposal content reviewed — a differently-worded thesis produces a different hash, verified by `test_challenge_binds_to_proposal_content_hash`), `model_name`, `raw_response`, and `reviewed_at`. `generate_proposal_for_candidate` is deterministic given the same inputs and a fixed `now` (`test_generate_proposal_is_reproducible_given_same_inputs`).

## 6. Persistence

None — `.49` introduces no new persisted state, mirroring `.47`/`.48`'s own precedent. Every function is a pure function of caller-supplied inputs (a shortlist, an injected `LLMClient`, optional `now`) plus required research parameters; every audit-relevant fact lives on the returned dataclasses. A future integration milestone (most likely `.50`, when a real Decision Engine exists to journal decisions) is expected to persist these for a durable audit trail — `.49` provides the complete in-memory record; it does not yet write it anywhere. This is a disclosed limitation, not an oversight.

## 7. Tests and verification

**57/57 new tests passing**, organized as:
- LLM client construction: `NullClient` behavior, degrade-without-key, Anthropic construction with an injected fake SDK (no live network call anywhere in this suite), model-env-override, retry-on-429 with exponential backoff, no-retry-on-non-transient-4xx.
- Proposal generation happy path: correct field population, confidence boundary values (0.0/1.0 accepted), one proposal per candidate always returned in order, shared `shortlist_hash` across a batch, reproducibility.
- `build_shortlist_hash`: deterministic for identical content, changes on reorder, changes on content change.
- **Fail-closed rejection paths** (the core of this milestone's approved deviation): no model configured, `llm_client=None`, the LLM call raising, empty response, unparseable response, **candidate_id mismatch (closed-output-space enforcement)**, missing/empty thesis, every invalid confidence shape (`None`, string, bool, out-of-range, NaN) via a parametrized test, and an 8-entry **fuzzed malformed-output corpus** proving no rejected proposal ever retains a partial valid field.
- `AIProposal.__post_init__` structural validation: invalid status rejected, `PROPOSED` requires thesis+confidence, non-`PROPOSED` requires a reason.
- Adversarial-challenge role: requires a `PROPOSED` proposal (raises on a rejected one), requires the matching candidate, no-concerns → `passed=True`, concerns preserved verbatim → `passed=False`, **derivation-is-real test** (an extraneous `"passed": true`/`"approved": true` in the raw response is ignored — the derived verdict still reflects `concerns` only), fail-closed on no client / call error / empty response / unparseable response, content-hash binding (differs across differently-worded proposals, stable across identical ones).
- Ranking penalty: required-keyword enforcement, negative-penalty rejection, `passed=True` → unchanged, exact arithmetic for N concerns, zero-penalty no-op.
- Governance/non-goal invariants: no trade/order function name anywhere in the module, deny-list field-name check on all three dataclasses, no `CanonicalExecutionIntent` import/construction/dynamic-import of any kind, no approval-shaped field on `Critique`, and a structural check that `Critique` has no consumer other than the ranking-penalty function.

**Full regression suite: 846/846 passing** (789 prior + 57 new). **Lint: clean** (`pyflakes`) on both the module and its test file.

## 8. Known limitations

- **Fail-closed instead of literal subtract-only repair** — the approved deviation from acceptance criterion 2's exact text, documented in full in §3 above and in the module's own docstring. If a future milestone (or Martin) decides true subtract-only repair is needed after all, it would be a targeted addition to `generate_proposal_for_candidate`'s validation branches, not a redesign.
- **Acceptance criterion 3 (`.49` vs `.50` distinctness test) is only partially satisfiable right now** — `.50`'s deterministic LLM-free critic does not exist yet. This module cannot yet contain a cross-module test proving the two are distinct and non-substitutable, since there is nothing on the `.50` side to compare against. This is disclosed, not fabricated: the test will need to be added once `.50` exists, most naturally as part of `.50`'s own completion work.
- **No live Anthropic smoke test** — every test in this suite uses an injected fake `LLMClient`; the `AnthropicClient` class itself has not been exercised against a real API call in this environment (no `ANTHROPIC_API_KEY` is configured here). Retry/backoff and text-extraction logic are unit-tested against fake SDK responses shaped like the real SDK's response objects, but a live smoke test against the actual Anthropic API is the natural next check before this pipeline is wired into a live-adjacent path, matching the same disclosed-limitation pattern used in `.42`/`.43`/`.45`/`.46`/`.48`.
- **The candidate shortlist itself is caller-supplied** — `.49` provides no function that builds a shortlist from `.46`/`.47`/`.48`'s outputs. That wiring is explicitly out of scope here and expected to land with `.50` or a dedicated integration milestone.
- **Ranking-score baseline (`base_rank_score`) is not computed by this module** — `apply_challenge_penalty` only adjusts a score it's given; the scoring/ranking algorithm itself is out of scope for `.49`, matching `.44`'s "never invent numbers, never invent the algorithm either" discipline for anything that's a genuine research/strategy decision rather than a mechanical transformation.

## 9. Next

Per the standing procedure, stopping here to report before starting `.50`. Checkpoint commit to be created locally immediately after this report is saved and delivered. **Not pushed to GitHub** — the git-proxy authorization issue from the `.48`→`.49` transition is still unresolved; a verified git bundle of the full `cff57fe`→`49b20ba` history was created and independently restore-tested before this milestone began, so `.49`'s work proceeds on top of a confirmed-recoverable base regardless of when the GitHub sync is fixed.

`.50` (Formal Decision Engine + deterministic LLM-free critic) is next per the agreed sequence, followed by circling back to `.41` (Ghost Trades), then `.51`→`.53`. Separately, `.50` is also where acceptance criterion 3's `.49`-vs-`.50` distinctness test becomes completable, and where a real candidate-shortlist builder (consuming `.46`/`.47`/`.48`'s outputs) most naturally belongs.
