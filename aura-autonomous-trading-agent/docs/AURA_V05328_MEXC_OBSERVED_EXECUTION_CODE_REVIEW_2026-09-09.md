# `.28` Code Review — Against the 10-Point Checklist

Direct source inspection of `aura_v05328_mexc_observed_execution.py` and the installed ccxt 4.5.78 source, not a re-run of the 22 tests. No code changed. Two real findings surfaced that the test suite did not catch, because the `FakeExchange` test double doesn't replicate a real ccxt constraint. Everything else verified clean by reading the code, not by inference.

**Date:** 2026-09-09

---

## UPDATE (same day): all three fixes applied, re-verified, 51 assertions passing

Per your explicit approval, all three fixes below were applied and the suite re-run. Still not committed — the sections below are the original review as written before the fixes, kept for the record; see the addendum at the very end for what changed and how each fix was re-verified.

---

## 1. No hidden network path in the default CLI — PASS

`main()`: the `else` branch that calls `read_observed_execution()` (the only path that ever touches `exchange.*`) is reachable only when `args.query_live is True` **and** `AURA_MEXC_OBSERVATION_ENABLED == "true"`. The default branch sets `snapshot_status = "VALIDATED_NO_QUERY"` and returns without constructing an exchange at all. Confirmed by reading `main()` directly, not by trusting the docstring.

## 2. No write-capable exchange method reachable from `.28` — PASS

`grep -n "exchange\."` across the whole file returns exactly four calls: `fetch_orders`, `market`, `fetch_positions`, `fetch_order_trades`. No `create_order`, `cancel_order`, `set_leverage`, `cancelAllOrders`, or any other mutating method appears anywhere in the file.

## 3. Exact `externalOid` matching — PASS

`resolve_order()`: `if info.get("externalOid") == client_order_id` — exact string equality, no normalization, no case-folding, no substring matching.

## 4. Hash verification cannot be bypassed — PASS, with one note

Every exit path (`main()`'s three branches, `read_observed_execution()`'s return) ends in `finalize()`, which unconditionally overwrites `snapshot["snapshot_hash"]` with a fresh `canonical_snapshot_hash()` computed from the snapshot's own current fields. There's no path that ships a caller-supplied hash.

Note, not a defect: the canonical hash covers the decision-bearing fields (`order_status`, `match_count`, `position_exists`, `fill_price`, etc.) but not `raw_evidence`, `reason`, `queried_at`, or `guardrails` — so those could drift without invalidating the hash. This exactly mirrors `.18`'s own `canonical_ledger_hash`/`canonical_observed_hash`, which are equally selective. Consistent with existing convention, not a regression, but worth naming since you asked specifically about hash bypass.

## 5. `UNRESOLVED` is genuinely fail-closed — PASS

Traced every path that can set `order_status`: zero matches, multiple matches, and every raw-state branch in `classify_order_status()` either return a specific recognized value or `UNRESOLVED`. The FILLED/PARTIALLY_FILLED branch additionally downgrades back to `UNRESOLVED` (clearing `fill_price`/`fill_timestamp`) if `dealAvgPrice`/`updateTime` don't substantiate it. Nothing upgrades `UNRESOLVED` into a more confident state anywhere in the file.

One gap found: `ALLOWED_ORDER_STATUSES` is **defined but never checked**. Nothing in `finalize()` or elsewhere asserts that `snapshot["order_status"]` is actually a member of that set before output. In the current code this is harmless by construction — every assignment site sets one of the six literal strings — but it's a missing defensive check compared to `.18`'s own convention (`ALLOWED_OBSERVED_STATUSES` is actively validated in `observation_errors_for_symbol()`). Recommend adding the same active assertion here rather than relying on "can't currently happen."

## 6. Raw evidence cannot be silently transformed/lost — PARTIAL

`raw_evidence["order"]` and `raw_evidence["position"]` store the **verbatim** raw MEXC dicts (`order['info']`, `position['info']`), unmodified. That part is solid.

`observed_fills`, however, is a **curated subset** — `resolve_trades()` extracts only `trade_id`, `price`, `vol`, `fee`, `timestamp` from each trade's raw `info` dict, dropping everything else (`symbol`, `side`, `category`, `positionMode`, `taker`, etc.). Unlike order/position evidence, the original raw trade dict is not preserved anywhere in the snapshot. If a future mapping turns out to be wrong, there's no raw trade record left to re-derive from. Recommend storing the verbatim trade `info` dicts under `raw_evidence["trades"]` alongside the curated `observed_fills`, the same pattern already used for orders/positions.

## 7. Position evidence cannot independently produce `FILLED` — PASS

`resolve_position()`'s result (`position_exists`/`position_id`) is assigned directly to snapshot fields and never read anywhere inside the order-status/fill-price logic above it. The two code paths are structurally independent — confirmed by reading, not just by absence of an obvious shared variable.

## 8. Lookback boundaries are deterministic — PASS

`since_ms = int((submitted_at_dt - timedelta(minutes=lookback_minutes)).timestamp() * 1000)` — a pure function of the submission's own `observed_at` and the `lookback_minutes` parameter. No dependency on wall-clock "now" at query time. Test 22 exercises this exact computation.

## 9. Multiple matching orders cannot accidentally select one — PASS

`resolve_order()` internally sets `"order": matches[0]` even in the `match_count > 1` case (for the reason string), but the caller only reads `resolved["order"]`/sets `mexc_order_id`/`raw_evidence["order"]` inside the `match_count == 1` branch. When `match_count > 1`, `mexc_order_id` stays `None` and no specific order's raw data reaches the output — confirmed by reading the branch structure, not inferred. Minor style note only: `resolve_order()` returning an unused `matches[0]` in the conflict case is slightly misleading to a future reader, but it isn't reachable as a bug today.

## 10. The live gates cannot be bypassed through direct invocation — FAILS AS STATED, BY DESIGN (matches `.27`)

`AURA_MEXC_OBSERVATION_ENABLED` is checked **only inside `main()`** (line ~513). `read_observed_execution()` itself does not check it — if `exchange=None`, it only requires `MEXC_API_KEY`/`MEXC_API_SECRET` to be set, then queries live. So: importing the module and calling `read_observed_execution(submission)` directly, with credentials present in the environment, bypasses the enabled-flag gate entirely. The CLI gate cannot be bypassed *through the CLI*; it can be bypassed by not using the CLI.

This is not a `.28`-specific regression — `.27`'s `submit()` has the exact same property (`AURA_MEXC_LIVE_ORDERS_ENABLED` is checked only in `.27`'s `main()`, not in `submit()` itself). It's a consistent, apparently intentional convention across both modules: the CLI is treated as the sole gated production entry point, and direct function invocation is assumed to be test/orchestration code that supplies its own `exchange` (as every test in both suites does). Flagging it plainly rather than silently confirming a "cannot be bypassed" that isn't quite true — if anything ever calls `read_observed_execution()`/`submit()` directly in a production path without reimplementing the gate, the double-gate is void for that caller.

---

## Additional finding beyond the checklist — real bug, not caught by the 22 tests

**`resolve_position()`'s direct call to `exchange.market(symbol)` has no `load_markets()` guard, and the `FakeExchange` test double doesn't reproduce ccxt's actual behavior here.**

Verified directly from ccxt's own base class (`/usr/local/lib/python3.11/dist-packages/ccxt/base/exchange.py`, `market()`, line 6324): if `self.markets is None`, `market()` raises `ExchangeError('markets not loaded')` — it does **not** auto-load. `fetch_orders()`, `fetch_positions()`, and `fetch_order_trades()` each independently guard themselves (`if self.markets is None: self.load_markets()`) before doing anything else — but the bare `market()` helper `.28` calls directly in `resolve_position()` has no such guard, and `build_exchange()` (copied from `.27`) never calls `load_markets()` either.

In practice, today, this happens not to crash: `read_observed_execution()` always calls `resolve_order()` (which triggers `fetch_orders()`'s own internal `load_markets()`) *before* `resolve_position()` runs, so by the time `resolve_position()` calls `exchange.market(symbol)`, markets are already loaded as a side effect. But that's an **implicit, undocumented ordering dependency**, not a guaranteed invariant — it would break immediately (`ExchangeError: mexc markets not loaded`) if `resolve_position()` were ever called first, called standalone, or if the call order inside `read_observed_execution()` were ever refactored. My `FakeExchange.market()` stub just returns a dict unconditionally, so none of the 22 tests could have caught this — this is exactly the "tests pass but does it actually implement the behavior" gap you asked me to check for.

**Recommended fix** (not yet applied): add an explicit `exchange.load_markets()` call in `build_exchange()`, removing the dependency on call order entirely. One line, no behavior change to anything already verified above.

---

## Summary

Eight of your ten points verified clean by direct code reading. Two carry real findings: #6 (trade evidence isn't preserved verbatim, only orders/positions are) and #10 (the live-query gate is CLI-only, not function-level, mirroring `.27`). One additional bug found outside the checklist: `resolve_position()`'s `exchange.market()` call would fail on a real account today if it ever ran before `resolve_order()` — currently masked by call order, not tested because the fake exchange doesn't model ccxt's real "markets not loaded" behavior.

None of these are architecture-level problems — they're implementation gaps, all fixable with small, localized changes. Holding on any code change until you say so.

---

## Addendum — fixes applied, re-verified (same day)

**Fix 1 — `load_markets()` in `build_exchange()`.** Added, unconditional, right after constructing the exchange instance. Removes the ordering dependency entirely: `.28` no longer needs `resolve_order()` to run first for `resolve_position()`'s `exchange.market()` call to work.

Re-verification, not just re-running the old suite (which couldn't have caught this — see above): the `FakeExchange` test double was corrected to actually model ccxt's real constraint — `market()` now raises `ExchangeError` unless `load_markets()` has been called, and `fetch_orders`/`fetch_positions`/`fetch_order_trades` each self-set `markets_loaded = True` internally, matching `ccxt/mexc.py`'s own behavior exactly (verified against that source again, not assumed). New test 23 proves three things directly: (23a) `resolve_position()` on a fresh, unloaded fake genuinely raises `ExchangeError` — the defect the original 22 tests structurally could not detect; (23b) it succeeds once `load_markets()` is called; (23c) `build_exchange()` itself — the real construction path, exercised by swapping a network-free stand-in into `ccxt.mexc` for one call, not by trusting the fake — now calls `load_markets()` unconditionally.

**Fix 2 — active `ALLOWED_ORDER_STATUSES` enforcement.** Added to `finalize()`, the single funnel point every code path already passed through for hash computation — so the check can't be skipped by any current or future call site the way a check placed inside `read_observed_execution()` alone could be. An `OBSERVED` snapshot whose `order_status` isn't a recognized value is forced to `UNRESOLVED` (fill fields cleared), with the original unrecognized value preserved under `raw_evidence["unrecognized_order_status"]` — never silently dropped, never silently trusted. A non-`OBSERVED` snapshot's `order_status = None` (e.g. `VALIDATED_NO_QUERY`) is left alone.

New test 25 exercises this by calling `finalize()` directly with a hand-crafted unrecognized value (25a–25d), confirms a legitimate value passes through unchanged (25e), and confirms a non-`OBSERVED` snapshot isn't wrongly coerced (25f) — this has to be tested at the `finalize()` level directly, since `classify_order_status()` never produces an unrecognized value by construction today; the point of the fix is defense against a future change, not today's normal flow.

**Fix 3 — verbatim trade evidence.** `resolve_trades()` now returns the raw `info` dicts unmodified; a new `curate_fills()` derives the normalized `observed_fills` subset from that same raw list. `read_observed_execution()` stores both: `raw_evidence["trades"]` (verbatim) and `observed_fills` (curated) — matching the pattern already used for orders/positions.

New test 24 constructs a raw trade dict carrying fields `curate_fills()` deliberately drops (`symbol`, `side`, `profit`, `taker`, ...) and confirms all of them survive unchanged in `raw_evidence["trades"]` (24a/24b) while `observed_fills` remains the curated subset (24c) — proving nothing is lost, not just that something is present.

**#10 — documented, not changed**, per your instruction: the module docstring now states plainly that `AURA_MEXC_OBSERVATION_ENABLED` is checked only in `main()`, that `read_observed_execution()` is a lower-level primitive that does not independently enforce it (matching `.27`'s identical `submit()`/`AURA_MEXC_LIVE_ORDERS_ENABLED` property), and that a future orchestrator calling `.27`/`.28` directly must enforce the authorization boundary itself rather than relying on CLI conventions.

**Full suite: 51 assertions, all passing** (`python3 tests/test_aura_v05328_mexc_observed_execution.py`) — the original 22 (one, test 15, gained an extra assertion for raw trade preservation) plus 12 new assertions across tests 23–25. `git status` confirms the repository is still clean apart from the two untracked `.28` files — nothing committed.
