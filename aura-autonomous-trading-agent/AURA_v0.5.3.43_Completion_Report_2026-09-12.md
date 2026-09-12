# AURA v0.5.3.43 Completion Report — Portfolio Exposure Observability (cross-venue)

**Date:** 2026-09-12
**Scope:** Milestone `.43` on the v0.5.5 Final Implementation Baseline, built per Martin's explicit, detailed scoping decision (AskUserQuestion, 2026-09-12): **Option 1 — full cross-venue position aggregation now**, with a reuse-first investigation mandatory before any new code, observability-only (no enforcement — that is `.44`'s job), and an explicit instruction not to invent or approximate any data AURA does not actually have.

This is the second milestone built under the "reorder build, keep numbering" sequence (`.42` → `.50`, then back to `.41`, then `.51`→`.53`) established after `.41`'s dependency on `.50` was found and escalated.

---

## 1. Audit (per the standing 7-step milestone procedure)

The pre-implementation audit found the roadmap's `.43` line item ("12-dimension portfolio exposure observability") to be far larger in true scope than its one-line description suggested:

- **No common cross-venue position schema exists anywhere in this repo.** MEXC only had a single-symbol position query (`.28`'s `resolve_position()`, deliberately scoped to order reconciliation, not portfolio observation). Alpaca's own sizing module (`.25`'s `fetch_account_state()`) queries positions but discards quantity/price/side — it only needs a symbol set and a count.
- **The "`.15` 12-state Position State machine" referenced in other project documents does not exist anywhere in this repository.** Verified by a full read of `.15`'s own contract (FLAT / ENTRY_CANDIDATE / BLOCKED only, and its own docstring says so explicitly) and a repo-wide grep for all 12 state names claimed elsewhere — zero hits outside the referencing documents themselves. This finding is preserved as-is in this report, not "fixed" by retroactively building a 12-state machine, per Martin's explicit instruction after reviewing this finding.
- **Essentially none of the 12-dimension checklist is computed live**, in any form, anywhere in AURA today.

Per Martin's decision, `.43` was scoped as: build the full canonical cross-venue position snapshot (MEXC + Alpaca), then the 12-dimension exposure layer on top of it; do not invent or approximate missing data — mark genuinely-unavailable dimensions `NOT_COMPUTABLE` explicitly; stay observability-only; make freshness/source/completeness/failure state explicit so `.44` can safely consume this later.

---

## 2. Reuse scan (Martin's explicit A/B/C/D framework — performed before writing any new code)

Surveyed: this repo's own CAURA lineage, the BABIL hackathon submission (competitor, reference-only), all three DELTAX repositories (`pautax007_DELTAX`, `mpoubot_DELTAX`, `mpoubot_DELTAX_v2`), and this project's own sibling `mexc_bot/` research track. One agent performed the initial cross-repo survey; the single most relevant finding (`mexc_bot/core/risk_manager.py`) was additionally verified by a direct, personal full read; exact MEXC/ccxt and Alpaca/alpaca-py field mappings were verified by introspecting the actual installed library source code (`python3 -c "import ccxt/alpaca; inspect.getsource(...)"`), not assumed from documentation or memory.

| Component | Classification | Reasoning |
|---|---|---|
| CAURA `max_drawdown_from_returns` / `drawdown_from_running_max` | (C) architecturally useful, incompatible | Correct drawdown math, but single-series backtest helpers — not live multi-venue position state. Drawdown math re-derived here on this module's own persisted equity history instead. |
| BABIL `risk_evaluator.py`, `babil_authorization.py`, `babil_pre_execution.py` | (D) not applicable | Confirmed by direct read: per-order execution-safety/sizing gates only. No portfolio-aggregation concept in any of the three files. |
| DELTAX `gates.py::gate_portfolio_risk` | (C) architecturally useful, incompatible | Single aggregate committed-max-loss-vs-equity threshold — the "aggregate risk vs equity" pattern is reasonable, but single-scalar, single-venue, no multi-dimension model. |
| DELTAX `reconcile.py` | (C) partially adopted | Its fail-closed-on-unparseable-holding discipline is adopted here (see `VenueFetchStatus`); its option-leg-pairing logic does not apply to AURA's stock/crypto-perpetual positions. |
| DELTAX v2 `portfolio_risk_monitor.py` (780 lines, Postgres-backed) | (C) architecturally useful, incompatible | Computes only 2 of the 12 dimensions (two-tier daily-loss threshold, crude per-asset-class market-value split). The two-tier daily-loss-threshold shape informed `compute_daily_loss`'s design, but the implementation (Postgres, single-venue, in-memory) does not fit AURA's atomic-file/hash-chain convention — reimplemented here on that convention instead of adopted directly. |
| `mexc_bot/core/risk_manager.py::CircuitBreaker` | (B) adaptable with minor changes | Verified by direct read; no test suite exists for it in its origin. The daily-loss-pct-of-day-start-equity and drawdown-pct-from-peak *formulas* are correct and standard, and are adapted here almost verbatim (`compute_daily_loss`, `compute_max_drawdown`) — but its storage (a mutable in-process instance that does not survive a restart) is not reusable; this module persists the same math atomically instead. Its `calc_position_plan`'s `MAX_LEVERAGE` clamp idea directly informed `check_mexc_leverage_cap`, parameterized rather than hardcoded. |
| Correlation, liquidity-depth, sector-map data, any venue-agnostic snapshot | (D) unavailable | No sibling project anywhere has these. Confirmed absent, not merely unwired, by direct code read — carried into this report as structurally `NOT_COMPUTABLE`, not invented. |

Net effect: two formulas (daily-loss-pct, drawdown-pct) and one fail-closed discipline were adapted from existing code; the canonical cross-venue schema, both venue fetchers, and the exposure-dimension computations are newly implemented, because nothing reusable existed for them anywhere in the surveyed repositories.

---

## 3. What was newly implemented

**One new file: `aura_v05343_portfolio_exposure_observability.py`** (~750 lines). Library module — no CLI/scheduled entry point of its own in this milestone (it is called by whatever orchestrates a snapshot run; that orchestration is future work, not part of `.43`'s acceptance criteria).

### Canonical position schema

```
PositionRecord:   venue, symbol, direction (LONG/SHORT), quantity, entry_price,
                  leverage (MEXC: real; Alpaca: None — never fabricated as 1.0),
                  mark_price, notional_usd, notional_basis
                  ("MARK_TO_MARKET" | "ENTRY_PRICE_ESTIMATE" | None),
                  unrealized_pnl_usd, liquidation_price (MEXC only),
                  raw_source_id (traceability only), as_of

VenueFetchStatus: venue, status ("SUCCESS" | "FAILED" | "NOT_CONFIGURED"),
                  error, fetched_at, positions_count, equity

PortfolioSnapshot: as_of, positions (tuple[PositionRecord]),
                   venue_fetch_status (dict[venue -> VenueFetchStatus]),
                   is_complete (True iff every CONFIGURED venue == SUCCESS),
                   state_hash (sha256 of a stable-JSON body — same
                   hash-chain-self-verification convention as every other
                   aura_v053NN module)
```

### MEXC fetcher (`fetch_mexc_portfolio`)

Widened from `.28`'s single-symbol `resolve_position()` to enumerate **all** open positions via `exchange.fetch_positions()` (no symbol filter). Field parsing verified against ccxt 4.5.78's actual installed `MexcClass.parse_position()` source: `positionType` ('1'=LONG, else SHORT), `holdVol` (quantity, filtered to >0), `openAvgPrice` (entry), `leverage`, `liquidatePrice`. ccxt's own unified `notional`/`markPrice`/`unrealizedPnl` fields were confirmed (by reading the same installed source) to be hardcoded `None` for MEXC and are not read. Mark-to-market notional instead requires a supplementary `fetch_ticker()` call combined with the market's real `contractSize` (confirmed non-1 and populated for MEXC swaps by reading `fetch_swap_markets()`'s source — e.g. BTC_USDT = 0.0001 BTC/contract). If the ticker call fails, notional falls back to an explicitly-labeled `ENTRY_PRICE_ESTIMATE`; if `contractSize` itself is unavailable, notional is left `None` rather than guessed at `contractSize=1`. Any exception from `fetch_positions()` fails the whole venue closed (`status="FAILED"`) rather than returning a partial list silently; a subsequent equity-fetch failure (`fetch_balance()`) is independent and best-effort — position data already fetched is still reported, with equity honestly `None`.

`fetch_mexc_funding_exposure()` computes the COMPUTABLE half of dimension 10 (current funding rate × notional per open MEXC position, direction-signed); the "basis" half (perp vs spot) is explicitly reported as its own `NOT_COMPUTABLE` sub-field, never conflated with the computed funding number.

### Alpaca fetcher (`fetch_alpaca_portfolio`)

Widened from `.25`'s `fetch_account_state()` (which discards qty/price/side). Uses `alpaca.trading.models.Position`'s verified fields directly (`market_value`, `current_price`, `unrealized_pl` — richer than MEXC, no supplementary call needed). Leverage is always reported `None` (this account model carries no margin data) rather than fabricated. Account-fetch and positions-fetch are separate calls; either failing fails the venue closed, while equity already learned before a positions-fetch failure is preserved rather than discarded.

### Orchestration (`build_portfolio_snapshot`)

Each venue is independent — MEXC failing never blocks Alpaca data and vice versa. A venue not passed at all is reported `NOT_CONFIGURED` and does not count against `is_complete`.

### Persisted equity history (`load_equity_history` / `append_equity_history`)

Atomic whole-file read-modify-write (tmp-then-`os.replace()`, matching `.29`'s established pattern), one entry per venue per run where equity was actually observed — a failed or unavailable equity reading is never recorded as a fabricated zero. This is the data source `daily_loss` and `max_drawdown` read from.

### The 12 dimensions (plus the separately-tracked MEXC leverage cap)

| # | Dimension | Status | Data source |
|---|---|---|---|
| 1 | per_trade_risk | **NOT_COMPUTABLE** | AURA does not persist a stop-loss/planned-risk distance for any already-open position; `.25` only computes a target risk fraction at new-entry sizing time. |
| 2 | daily_loss | COMPUTABLE / NOT_YET_AVAILABLE | This module's own persisted equity history (needs a same-day prior snapshot). |
| 3 | max_drawdown | COMPUTABLE | This module's own persisted equity history (peak-so-far well-defined from a single point). |
| 4 | portfolio_heat | COMPUTABLE (partial-coverage flagged) | sum(\|notional\|)/equity across priced positions. |
| 5 | asset_concentration | COMPUTABLE (partial-coverage flagged) | Per-symbol share of total priced notional. |
| 6 | correlation | **NOT_COMPUTABLE** | No continuously-updated cross-symbol return history exists live in AURA. |
| 7 | directional_exposure | COMPUTABLE | net(LONG notional − SHORT notional)/equity. |
| 8 | leverage_exposure | COMPUTABLE for MEXC (real per-position); Alpaca reported unlevered, not fabricated | Per-position + per-venue aggregate. |
| 9 | liquidity_concentration | **NOT_COMPUTABLE** | No order-book depth/volume data available outside backtest CSVs. |
| 10 | crypto_funding_basis_exposure | PARTIAL/COMPUTABLE (funding half only) | `fetch_mexc_funding_exposure()`; basis half explicitly `NOT_COMPUTABLE` (no live spot reference feed). |
| 11 | equity_sector_concentration | **NOT_COMPUTABLE** | No sector mapping data anywhere in this repo. |
| 12 | projected_post_trade_risk | COMPUTABLE | `project_post_trade_exposure()` — pure function overlaying one hypothetical position, recomputing dimensions 4/5/7/8 before/after. |
| 13* | MEXC leverage cap check | Leverage always COMPUTABLE; PASS/FAIL only if a cap is configured | `check_mexc_leverage_cap(positions, cap)` — `cap` has no default and no config-file value exists anywhere in `.34` today (confirmed absent by the audit); called with `cap=None` it reports current leverage only, `cap_configured=False`, and never a fabricated verdict. |

4 of the 12 dimensions are structurally `NOT_COMPUTABLE` today (correlation, liquidity_concentration, equity_sector_concentration, per_trade_risk) — each with an explicit, honest reason string in `NOT_COMPUTABLE_STATIC`, not a placeholder value.

---

## 4. Tests

`tests/test_aura_v05343_portfolio_exposure_observability.py` — **39 tests**, all real-path against injected fakes (`FakeMexcExchange`, `FakeAlpacaClient`/`FakeAlpacaPosition`), mirroring `.28`'s established `FakeExchange` convention. No real broker credentials, no real network call, anywhere in this file. Covers, per Martin's explicit requirement:

- **Both venues, success and failure paths**: MEXC long+short parsing, zero-`holdVol` exclusion, fail-closed on `fetch_positions()` exception, equity-fetch failure independent of position data; Alpaca success (long+short), zero-qty exclusion, fail-closed on account-fetch exception, fail-closed on positions-fetch exception with equity preserved.
- **Missing/stale/partial broker data**: ticker-fetch failure falling back to `ENTRY_PRICE_ESTIMATE`; unknown `contractSize` leaving notional honestly `None`; funding-rate fetch failure per-symbol not blocking other symbols; one venue failing while the other succeeds in `build_portfolio_snapshot`.
- **Positions existing outside AURA's own intent ledger**: an explicit test (`test_positions_outside_intent_ledger_are_still_captured`) confirms a broker-reported position with a `raw_source_id` matching no AURA ledger convention is still captured — this module reads broker truth directly and has no concept of "in the ledger" at all.
- **Cross-venue aggregation**: `is_complete` true only when every *configured* venue succeeds; `NOT_CONFIGURED` venues never count against completeness; partial-failure and single-venue-configured cases.
- **State-hash reproducibility** and sensitivity to content changes.
- **Equity history** atomic round-trip, accumulation across multiple appends, never recording a `None` equity as zero.
- **Every dimension function's branches**: `NOT_COMPUTABLE`/`NOT_YET_AVAILABLE`/`COMPUTABLE` for daily_loss and max_drawdown; partial-coverage flagging for portfolio_heat and asset_concentration; directional_exposure sign correctness; leverage_exposure per-position and per-venue aggregation (Alpaca leverage never fabricated); leverage-cap with and without a configured cap, and correct breach detection; the full `compute_exposure_dimensions()` assembly including all 4 static `NOT_COMPUTABLE` dimensions; `project_post_trade_exposure()`'s before/after overlay and non-mutation of the original snapshot.
- **Persistence**: atomic snapshot write, no leftover `.tmp` file, round-trip content match.
- **A static guard** confirming the module's executable code never imports or calls any execution-spine or promotion-gate function (`place_order`, `submit_order`, `cancel_order`, `record_evidence`, or any `.27`/`.23`/`.39` module) — this module is observability-only by construction, not just by docstring claim.

One test-infrastructure fix was required during this milestone: this is the first `aura_v053NN` module to define dataclasses under `from __future__ import annotations`, and Python's dataclass machinery resolves field types via `sys.modules[cls.__module__]` — the existing dynamic-file-load pattern (`spec_from_file_location` + `module_from_spec` + `exec_module`, unchanged in every prior test file) does not register the module in `sys.modules` before `exec_module` runs, causing `AttributeError: 'NoneType' object has no attribute '__dict__'` inside `dataclass()`. Fixed by adding one line (`sys.modules[spec.name] = mod`) before `exec_module()` in this test file only — a test-loader fix, not a change to the module's own runtime behavior or to any other test file's loader.

## 5. Verification

- **New tests:** 39/39 passed.
- **Full regression suite:** 623/623 passed (was 623 = 584 prior + 39 new; +39, 0 removed, 0 modified elsewhere).
- **Lint (`ruff check`):** clean on both new files (0 errors) after removing one unused import (`dataclasses.field`, never used) and one unused local variable in a test. The pre-existing repo-wide lint backlog is unchanged by this milestone and out of scope.
- **Disclosed limitation (unchanged from `.42`):** this sandboxed session's outbound network egress policy blocks both `mexc.com` and `alpaca.markets` domains entirely (confirmed via the proxy's own status endpoint — a 403 policy denial, not transient). Every fetcher in this module is verified against the actual installed ccxt 4.5.78 / alpaca-py library source code (field names and shapes read directly via `inspect.getsource()`, not assumed) and exercised in tests against injected fakes, but could not be exercised against a real, live MEXC or Alpaca account in this session. A live smoke test from an environment with broker network access is the right next check before `.44` (enforcement) depends on this data.

## 6. Known limitations / what remains open

- 4 of 12 dimensions are structurally `NOT_COMPUTABLE` today (see table above) — genuine gaps in AURA's available data, not implementation shortcuts. `.44` (enforcement) must be designed to skip or explicitly flag any dimension it cannot enforce, rather than assume all 12 are always present.
- The MEXC leverage cap has no configured value anywhere in AURA today; `check_mexc_leverage_cap` will report `cap_configured=False` until Martin (or a future milestone) supplies one.
- `daily_loss` will read `NOT_YET_AVAILABLE` on every venue until this module has actually run and persisted at least one same-day prior equity snapshot — this is expected, self-resolving behavior, not a bug.
- No live smoke test against real MEXC/Alpaca accounts has been possible in this sandboxed session (see disclosed limitation above); recommended before `.44` depends on this module's live output.
- This milestone has no scheduled/orchestrated entry point of its own — wiring a periodic snapshot run (and where its state directory lives in the real deployment, vs. this module's `DEFAULT_STATE_DIR`) is left to whichever milestone orchestrates the live runtime loop; not part of `.43`'s acceptance criteria as scoped by Martin.

## 7. Commit

Checkpoint commit to be created locally after this report is saved (files: `aura_v05343_portfolio_exposure_observability.py`, `tests/test_aura_v05343_portfolio_exposure_observability.py`, this report). **Not pushed to GitHub**, per the standing rule (commit after verification is pre-authorized; push requires separate explicit approval).

## 8. Next

Per Martin's explicit instruction and the reordered build sequence: proceed to `.44` (Portfolio exposure — **enforcement**, built on `.43`'s canonical snapshot and exposure report) only now that `.43` is verified — "MEXC + Alpaca → Canonical Position Snapshot → 12 Exposure Dimensions → `.43` OBSERVE → `.44` ENFORCE." Then `.45` (Alpaca `EXECUTION_UNCERTAIN` resolution authority), `.46`→`.47`→`.48` (News/Sentiment/Elliott Wave), `.49` (AI proposal pipeline, amended scope), `.50` (Decision Engine) — then back to `.41` (Ghost Trades, now that `.50` will exist), then `.51`→`.53`, then the full post-`.53` crypto validation campaign.
