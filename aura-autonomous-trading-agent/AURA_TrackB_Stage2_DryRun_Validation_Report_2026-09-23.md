# AURA Track B — Stage 2: Dry-Run End-to-End Orchestration Validation Report

**Date:** 2026-09-23
**Checkpoint:** Stage 2 of your four agreed checkpoints (Stage 1 implementation — accepted; Stage 2 dry-run end-to-end validation — this report; Stage 3 first real Alpaca paper-order cycle; Stage 4 multi-day operation).
**Scope, per your instruction:** prove the orchestration and state machinery end-to-end — synthetic evidence → `.53` decision → real `.44` risk enforcement → portfolio state → authorization/claim handling → order-intent construction → dry-run execution boundary → audit/logging → resulting state — by running `run_stage1a_dry_run()` as an actual live multi-symbol process, not only unit tests. Stopping here to report before touching a real Alpaca account, per your explicit checkpoint discipline.

## What was actually executed

A new script, `scripts/stage2_dry_run_validation.py`, run directly with `python3` (not pytest) against one deterministic, synthetic-evidence, multi-symbol scenario. It ran three real process invocations:

1. **Run 1** — the main 5-symbol scenario, first pass.
2. **Run 2** — the identical main scenario, replayed a second time against the same isolated claim store (duplicate/replay protection).
3. **Run 3** — a separate single-symbol cycle with `.38`'s supervisor kill switch explicitly engaged.

All three runs called `run_stage1a_dry_run()` with `attempt_submission=False` hardcoded inside that function (not overridable by any argument this script passes) — no code path in this run could have reached `.35.submit()`. No `alpaca_client` object is constructed anywhere in the script or the module it exercises; the real `.44` risk check runs against a hand-built snapshot carrying one disclosed **synthetic** account-equity figure ($100,000), never a broker call.

Claim-store and authorization state were written only under an isolated directory outside the repository (this session's scratch/temp area, passed via `--state-dir`), never under `regime_output/`. Verified after the run: `regime_output/` contains zero files, and no repository file was modified. Full command:

```
python3 scripts/stage2_dry_run_validation.py --state-dir <isolated temp dir>
```
Exit code: **0** (all 11 built-in sanity checks passed).

## Symbols / scenarios covered

| Symbol | Scenario | Quantity | Notional @ $100 fixed reference price |
|---|---|---|---|
| AAPL | Long candidate, high conviction, small notional | 10 | $1,000 (1% of synthetic equity) |
| TSLA | Short candidate, shortability **positively verified** | 10 | $1,000 (1%) |
| GME | Short candidate, shortability **negative** (not shortable, not easy-to-borrow) | 10 | $1,000 (1%) |
| NVDA | Low-conviction — never becomes an executable candidate | 10 | n/a |
| MSFT | Long candidate, **deliberately oversized** to breach a real configured risk limit | 1,000 | $100,000 (100%) |
| IBM | Long candidate, run separately with the supervisor kill switch engaged | 10 | $1,000 |

Risk configuration: `PortfolioLimits(max_portfolio_heat_ratio=0.05)` — a real, numeric, configured limit, not `LIMIT_NOT_CONFIGURED`. `equity_history` seeded with one same-day-prior ALPACA equity observation so `daily_loss` is genuinely computable rather than fail-closed on missing history.

## Outcome, symbol by symbol

**AAPL (long, run 1).** `.50` decided `DECIDE_LONG`. Real `.44` enforcement: `ALLOW` (heat ratio 1% against a 5% cap). Reached `.38`: `READY_FOR_SUBMISSION` / `CONSTRUCTION_ONLY`, with a real `order_spec` returned (`side=buy`, `position_intent=buy_to_open`, `direction=OPEN_LONG`, a deterministic `client_order_id`). Never submitted.

**TSLA (short, verified, run 1).** `.50` decided `DECIDE_SHORT`. Shortability audit: raw `shortable=True`/`easy_to_borrow=True` → translated `SHORTABLE`, `direction_capability=BORROW_CONFIRMED`, `short_positively_verified=True`. Real `.44` enforcement: `ALLOW`. Reached `.38`: `READY_FOR_SUBMISSION`, real `order_spec` (`side=sell`, `position_intent=sell_to_open`, `direction=OPEN_SHORT`). Never submitted.

**GME (short, unverified, run 1).** `.50` decided `DECIDE_SHORT`. Shortability audit: raw `shortable=False`/`easy_to_borrow=False` → translated `NOT_SHORTABLE`, `short_positively_verified=False`. Real `.44` enforcement actually evaluated first and returned `ALLOW` (small notional) — enforcement runs *before* `.38` in `.53`'s pipeline, so this confirms the risk check and the shortability gate are independent layers, not one substituting for the other. Blocked at `.36.authorize()` itself (`stage=AUTHORIZATION`, `reason=ADAPTER_CAPABILITY_VALIDATION_FAILED`) — **before any authorization_id or claim was ever minted** (`authorization_id=None` in the audit trail). Never reached `.37`'s claim layer, never reached `.38`'s own client_order_id claim, never submitted.

**NVDA (low conviction, run 1).** `.50.decide()` itself returned an outcome outside `{DECIDE_LONG, DECIDE_SHORT}` — never became an execution candidate, never touched enforcement or `.38` at all. `risk_decision_status=NOT_EVALUATED`.

**MSFT (oversized, run 1).** `.50` decided `DECIDE_LONG`. Real `.44` enforcement: **`BLOCK`, reason `portfolio_heat[PORTFOLIO]:LIMIT_BREACHED`** — a genuine numeric-limit breach (100% heat ratio against a 5% cap), not merely "not computable." Never reached `.38`.

**AAPL / TSLA (run 2, identical replay).** Both blocked — `supervision_status=BLOCKED`, `reason=CLIENT_ORDER_ID_ALREADY_CLAIMED_AT_SUPERVISOR_LAYER`. Neither reached `READY_FOR_SUBMISSION` a second time, and no new claim files were created (verified in the state directory — still exactly 2 `.claimed` files, matching run 1's count). See "unexpected behavior" below for exactly which layer caught this and why.

**GME (run 2).** Same `ADAPTER_CAPABILITY_VALIDATION_FAILED` block as run 1 — a deterministic, evidence-driven rejection, not a claim-layer effect, so it is correctly identical on replay.

**NVDA / MSFT (run 2).** Identical to run 1 (low-conviction and risk-blocked decisions are deterministic functions of the evidence, not of claim state).

**IBM (run 3, kill switch).** `.38`'s supervisor kill switch was explicitly engaged (`supervisor_config={"kill_switch": True}`). Blocked at `stage=SUPERVISOR_KILL_SWITCH`, `reason=SUPERVISOR_KILL_SWITCH_ENGAGED`, before `.36.authorize()` was ever called.

## State and audit persistence

Every symbol in every run produced one `AuditRecord` (schema-versioned, matching Stage 1's checklist: decision hash, risk decision hash + blocked reasons, intended notional, submission status, raw-vs-translated shortability, kill-switch state, etc.). All three runs' full outcome summaries and audit records were serialized to a single JSON file, `stage2_full_audit_trail.json`, written to the isolated state directory (attached to this report). Claim-store state was independently verified on disk: `stage2-main-auth-claims/` holds 4 authorization records (2 symbols × 2 runs — see below on why this number is 4, not 2), `stage2-main-supervisor-claims/` holds exactly 2 `.claimed` files (AAPL, TSLA) — unchanged in count after run 2, confirming the duplicate was rejected rather than double-claimed.

## Failures / unexpected behavior

One genuine, worth-flagging finding, not a defect: **`.36.authorize()` mints a fresh `uuid4()` `authorization_id` on every call** (`aura_v05336_alpaca_equity_execution_authorization.py`), so `.37`'s authorization_id-keyed claim layer never actually collides between run 1 and run 2 — it guards against concurrent double-processing of *one* authorization attempt, not against replaying an identical decision in a later cycle. This is why the auth-claims directory holds 4 files (one fresh authorization_id per authorize() call, 2 symbols × 2 runs) rather than 2. **The real cross-cycle duplicate guard is `.38`'s own second, local claim on the deterministic `client_order_id`** (derived from `decision_hash`/symbol/`signal_timestamp` — the same input always produces the same `client_order_id`), and that is the layer that actually caught and rejected the AAPL/TSLA replay in run 2. Net effect is correct — an identical repeated cycle cannot double-submit — but it is worth knowing *which* layer is actually doing that job before Stage 3, since the authorization_id layer alone would not have caught it.

A second thing worth recording, already partially covered by the fixes made getting here: real `.44` enforcement requires an actual total-equity figure to compute `portfolio_heat`/`directional_exposure` at all — a snapshot built with zero broker contact and no synthetic equity supplied will BLOCK those two dimensions on `EXPOSURE_NOT_COMPUTABLE` regardless of whether any numeric limit is configured. This script supplies a disclosed, synthetic $100,000 equity figure specifically so the ALLOW/BLOCK contrast in this report is driven by the configured limit, not by an absence of data. `run_stage1a_dry_run()`'s docstring now documents this distinction; three unit tests were added/adjusted to cover both the equity-supplied and the no-equity-source fail-closed cases.

Two pre-existing, order-dependent test flakes noted in the Stage 1 report (`test_position_intent_modification_rejection`, `test_alpaca_claim_store_unreachable_pass_through`) are still present and still pass in isolation — unrelated to this milestone, unchanged.

## Confirmations

- **No real Alpaca account was contacted.** No `alpaca_client` object was constructed anywhere in this script or in the code path it exercises.
- **No order was submitted.** `attempt_submission=False` throughout; `.35.submit()` is unreachable from `run_stage1a_dry_run()`.
- **Track A (MEXC) remains untouched and frozen.** No MEXC file, backtest, or strategy artifact was read or modified this session.
- **No `.51`/`.52` `TechnicalScoringParams` were invented.** Every candidate drove `sentiment_regime` synthetic evidence only, exactly as Stage 1.
- **The real `.49` LLM client was not wired.** The `.54` neutral deterministic stub was used throughout.
- **`.53`, the core MEXC platform, and the existing risk model (`.44`) were not modified.** Only `.55` (the Stage 1/2 runner) gained a new optional `synthetic_account_equity_usd` parameter and a snapshot-freshness ordering fix; a new script and new/updated unit tests were added.
- **No git commit or push was performed.**

## Flagged, not decided (per your instruction)

- **`DECIDE_KWARGS_BASE` reuse** — this script uses the identical `.53`/`.50` test-fixture weights Stage 1 used. Still not a validated production configuration; still needs your explicit decision before Stage 3.
- **Credential-naming ambiguity** (`ALPACA_PAPER_API_KEY`/`SECRET_KEY` crypto-vs-equity) — untouched this stage, `.env.example` documentation from Stage 1 still stands, still needs resolution before Stage 3 can read live credentials.

## Issues to resolve before Stage 3

1. Decide whether Stage 3's real-account risk check should keep using a caller-disclosed equity figure (fine for a synthetic dry run, not appropriate once a real account is live) or switch fully to `build_portfolio_snapshot(alpaca_client=...)`'s real account equity — Stage 1B's `run_stage1b_paper_cycle` already does the latter and needed no change here.
2. Decide the `DECIDE_KWARGS_BASE` question above.
3. Decide the credential-naming question above.
4. Confirm you're comfortable with the authorization_id-vs-client_order_id claim-layering finding above — it doesn't block anything, but Stage 3 will be the first time it matters against a real account.

Everything else in the Stage 2 checklist — long/short candidates, verified/unverified shortability, real risk blocking, kill-switch blocking, duplicate/replay protection, and full-cycle persistence — ran cleanly and deterministically. Stopping here, as agreed, before doing anything involving a real Alpaca account.
