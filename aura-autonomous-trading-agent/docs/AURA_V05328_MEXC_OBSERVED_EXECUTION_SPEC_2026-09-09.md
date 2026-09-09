# `.28` MEXC Observed Execution — Capability Verification and Schema Specification

Specification only. No code written. Grounded entirely in direct inspection of the installed `ccxt` 4.5.78 source (`/usr/local/lib/python3.11/dist-packages/ccxt/mexc.py`) — not memory, not the earlier connection-guide docs, not the DELTAX reference material. Every claim below cites the function and line range it came from. Where source-reading alone can't settle something, that's named explicitly rather than assumed.

**Date:** 2026-09-09

Scope: swap/futures only (MEXC perpetuals), matching `.27`'s own scope. Spot is not addressed.

---

## 1. Verified MEXC/CCXT capabilities (swap/futures)

### 1.1 Submission already supports a client order id (`create_swap_order`, ~line 2497–2534)

`create_order()` accepts a client-supplied id via `params['clientOrderId']` or `params['externalOid']`; ccxt normalizes either name and sends it to MEXC as `externalOid`:

```
clientOrderId = self.safe_string_2(params, 'clientOrderId', 'externalOid')
...
request['externalOid'] = clientOrderId
```

This **contradicts `.27`'s current conservative assumption** that no such parameter exists. `.27` itself has not been changed based on this — flagging it as a fact for the record, not acting on it, per your instruction to leave the adapter alone.

### 1.2 `fetch_order()` — no client-order-id lookup for swap (~line 3429–3457)

The swap branch only accepts MEXC's own numeric `orderId`. There is no `origClientOrderId`-style parameter for swap (spot has one; swap doesn't). Raw response shape for a regular order:

```
orderId, symbol, positionId, price, vol, leverage, side (raw "1"/"2", not unified),
category, orderType (raw, not unified), dealAvgPrice, dealVol, orderMargin,
takerFee, makerFee, profit, feeCurrency, openType, state (raw "1"-"5"),
externalOid, errorCode, usedMargin, createTime, updateTime, positionMode
```

A separate, differently-shaped "stop order" variant exists (`triggerType`/`triggerPrice`/`executeCycle`/`trend` + shared fields) — out of scope since `.27` only submits market orders, no triggers.

**Implication:** `.28` cannot look up "the order for client_order_id X" directly. It has to list and filter.

### 1.3 `fetch_orders()` / `fetch_open_orders()` — list-based, includes `externalOid` per item

Swap branch calls `contractPrivateGetOrderListHistoryOrders`, returning a list of orders in the same raw shape as §1.2, each carrying its own `externalOid`. This is the only practical way to resolve client_order_id → MEXC order: list orders in a bounded time window (`since=...`) and filter client-side.

### 1.4 `parse_order()` gap — unified `clientOrderId` is never populated for swap (line 3520)

```
'clientOrderId': self.safe_string(order, 'clientOrderId'),
```

This reads only the raw `clientOrderId` field (spot/margin). Swap's raw field is named `externalOid` instead, which `parse_order()` never reads into the unified output. **Consequence: `order['clientOrderId']` will be `None` for every swap order, even when we set `externalOid` at submission.** `.28` must read `order['info']['externalOid']` directly — the raw dict, not the unified field. Confirmed by direct reading of the function body, not inferred.

### 1.5 `parse_order_status()` gap — two raw states pass through unmapped (line 3563–3577)

```
'2': 'open',
'3': 'closed',
'4': 'canceled',
# '1': 'uninformed',  # TODO: wt?
# '5': 'invalid',  #  TODO: wt?
```

Raw states `'1'` and `'5'` are commented out in ccxt's own source — the library authors' own comments ("TODO: wt?") signal they weren't certain what these mean either. When MEXC returns state `1` or `5`, ccxt's unified `status` field will literally be the string `"1"` or `"5"`, not a recognized value. **`.28` must treat any status outside `{open, closed, canceled}` as unresolved, never assume it means filled or rejected.**

### 1.6 `fetch_positions()` — all open positions in one call, no per-order linkage (line 5024–5171)

`contractPrivateGetPositionOpenPositions`, no symbol argument required, returns only **currently open** positions:

```
positionId, symbol, positionType (1=long/2=short), openType, state, holdVol,
frozenVol, closeVol, holdAvgPrice, openAvgPrice, closeAvgPrice, liquidatePrice,
oim, im, holdFee, realised, leverage, createTime, updateTime, autoAddIm
```

Two things worth being precise about:
- ccxt's unified position object's `id` field is hardcoded `None` (line 5145) — `positionId` only exists in `position['info']`.
- There is **no client-order-id or order-id linkage on a position at all**. A position aggregates all fills for a symbol; it cannot be tied back to one `ExecutionIntent`. This actually matches AURA's own model — `.15` Position State is already symbol-level, not intent-level — so no new design problem here, just worth stating explicitly.
- Because the endpoint only returns *open* positions, "position closed" is never a status value you observe — it's an **absence**. `.28` must call `fetch_positions()` for every symbol in scope on every observation cycle so that absence is a confirmed, current-as-of-fetch fact, not a stale assumption.

### 1.7 `fetch_my_trades()` — swap branch has no client-order-id field at all (line 3968–4057)

Swap branch calls `contractPrivateGetOrderListOrderDeals`. Raw fields per trade:

```
id, symbol, side (raw), vol, price, feeCurrency, fee, timestamp, profit,
category, orderId, positionMode, taker
```

**No `externalOid`, no `clientOrderId` anywhere on a trade record.** Trades can only be tied back to our `client_order_id` indirectly, through a two-step join: `client_order_id` → (§1.3, filter on `info.externalOid`) → MEXC `orderId` → (this call, filter on `orderId`) → trades.

### 1.8 `fetch_order_trades(id, symbol)` — direct trade lookup once the MEXC order id is known (line 4059+)

Given a resolved MEXC `orderId`, this fetches that order's trades directly rather than filtering the full `fetch_my_trades()` list. Preferred over §1.7 once step §1.3's join has produced an order id — fewer records to filter, and it's the more literal "observed fill evidence" for one order.

---

## 2. What this means for `.28`'s read strategy — IMPLEMENTED

Implemented as `read_observed_execution()` in
`aura_v05328_mexc_observed_execution.py`, with 22 test assertions
(`tests/test_aura_v05328_mexc_observed_execution.py`, all passing) — CLI
contract tests for the no-network-call/validation/double-gate path, plus
direct unit tests against an injected `FakeExchange` for every
classification branch below.

```
read_observed_execution(client_order_id, symbol, since_ts) -> observation
```

1. `fetch_orders(symbol, since=since_ts)` — list orders in a bounded lookback window (§1.3).
2. Filter client-side: keep orders where `order['info'].get('externalOid') == client_order_id` (§1.4 — never trust the unified field).
3. Zero matches → **unresolved**, not `REJECTED`. Absence within a lookback window is not evidence of rejection — it can mean the window was too short or MEXC hasn't indexed it yet (see §4, eventual-consistency caveat). This is a real design decision, not a mechanical default — see §3.
4. Exactly one match → read the raw `state` defensively per §1.5's gap. Map `dealVol` vs `vol` to distinguish open/no-fill vs partially filled vs fully filled. Any state outside the three recognized codes → unresolved, same as zero matches.
5. More than one match (should not happen given `.27`'s atomic claim, but a duplicate `externalOid` submitted outside AURA is conceivable) → **fail closed as a conflict**, never silently pick one.
6. `fetch_positions()` for the symbol, separately — determines `position_exists` by presence/absence (§1.6). This is symbol-level, independent of steps 1–5.
7. Once a MEXC order id is resolved (step 4), call `fetch_order_trades(order_id, symbol)` (§1.8) to get the actual fill price/timestamp from a trade record — closer to "observed" than the order's own `dealAvgPrice`, consistent with your submission-result-vs-observed-execution distinction.

---

## 3. Canonical schema — LOCKED: Option B, `OBSERVED_EXECUTION_MEXC_V1`

Decided (2026-09-09): a versioned, MEXC-specific schema, `engine == "OBSERVED_EXECUTION_MEXC_V1"`, distinct from `.18`'s existing `OBSERVED_EXECUTION` schema rather than extending it. `.18` is left untouched by this work — wiring it to consume this schema is later, separate work, matching the sequencing (schema settled → `.18` integration → `.17` vocabulary → end-to-end lifecycle).

Emitted fields (`base_snapshot()` in the implementation): `client_order_id`, `symbol`, `mexc_order_id`, `order_status`, `raw_status`, `match_count`, `position_exists`, `position_id`, `fill_price`, `fill_timestamp`, `observed_fills` (raw trade evidence, list), `lookback_window_start`, `queried_at`, `source_submission_status`, `reason`, `raw_evidence` (verbatim MEXC responses — order/position, for audit), plus the usual `agent_version`/`engine`/`snapshot_status`/`snapshot_hash`/`guardrails`.

`order_status` is the six-value enum:

```
FILLED | PARTIALLY_FILLED | REJECTED | CANCELED | PENDING | UNRESOLVED
```

`UNRESOLVED` — not `UNKNOWN` — is the sixth state, named per your instruction: it communicates "AURA queried MEXC but reconciliation has not yet established the truth," never "the software errored." It fires on: zero matching orders in the lookback window (§2.3); more than one match, treated as a conflict (§2.5); a raw order state outside the three ccxt maps with confidence (`2`/`3`/`4`); and — one addition beyond what was originally specified here — a `FILLED`/`PARTIALLY_FILLED` classification that lacks a usable `dealAvgPrice`/`updateTime` to substantiate it, so a fill is never reported without the evidence that proves it. `REJECTED` is currently unreachable from `.28`'s own logic (nothing here observes a pre-submission refusal — that's `.27`'s `REJECTED`, already resolved) and is kept in the enum for schema symmetry with `.18` and for a future direct-rejection signal if MEXC's cancelled-order endpoint ever needs to report one.

Two of your refinements beyond the original two-option framing, both implemented:

- **Raw evidence is preserved, never discarded.** `raw_status` carries the literal MEXC state code; `raw_evidence.order`/`raw_evidence.position` carry the verbatim raw dicts MEXC returned. The normalized `order_status` is for AURA's logic; the raw fields are the audit trail if a mapping is later found to be wrong.
- **Fill price/timestamp come from the order's own `dealAvgPrice`/`updateTime`, never a value `.28` computes from trades.** Trade records (`observed_fills`) are attached as supporting evidence only — consistent with "never fabricate."

Anti-false-attribution, as you specified: `position_exists`/`position_id` are reported as plain symbol-level facts from `fetch_positions()` — supporting evidence only. `.28` does not, and a future `.18` integration must not, treat `position_exists == True` as proof that *this* intent specifically filled; MEXC positions aggregate every fill for a symbol with no order/client-id linkage at all (confirmed directly from `parse_position()` — see §1.6).

A smaller, mechanical note: `.18`'s schema requires `position_id` to be a string when `position_exists` is `True`; MEXC's raw `positionId` is numeric, so `.28` does a plain `str()` cast at the boundary (already implemented in `resolve_position()`).

---

## 4. What source-reading alone cannot settle

These need verification against a real MEXC account/live data before `.28` can be considered correct, not just complete:

- **Eventual-consistency lag**: how soon a just-submitted order appears in `fetch_orders()`'s history endpoint. Determines the minimum lookback window and how long "no match yet" must be tolerated before it's treated as meaningful absence rather than indexing delay.
- **Whether raw state `3` (`closed`) ever means "closed via cancellation"** rather than exclusively "closed via full fill." ccxt maps `3→closed` and `4→canceled` as separate codes, implying no, but ccxt's own uncertainty about states `1`/`5` (§1.5) means I'm not willing to extend that confidence to `3` without checking MEXC's actual contract-v1 API docs (the URL ccxt's own docstring cites) or observing it directly.
- **What raw states `1` and `5` actually represent** in practice, and whether either can plausibly occur for a market order that `.27` just submitted (vs. only being reachable for other order types AURA never uses).

These remain explicit verification gates, not silently converted into assumptions, per your instruction: `.28`'s state-mapping logic fails closed (→ `UNRESOLVED`) on raw state `3` without matching volumes and on states `1`/`5`/anything unrecognized (both implemented and tested — see tests 17–18 in `test_aura_v05328_mexc_observed_execution.py`). The lookback-window default (`DEFAULT_LOOKBACK_MINUTES = 60`, configurable via `--lookback-minutes`) is a placeholder pending real-account calibration, not a verified value.

---

## Summary

`.28` (`aura_v05328_mexc_observed_execution.py`) is implemented and tested — 22 assertions passing, no real network call anywhere in the test suite, matching `.27`'s own testing discipline. It reads `.27`'s own submission-result JSON for which intent to observe, makes only read-only MEXC/ccxt calls (`fetch_orders`, `fetch_positions`, `fetch_order_trades` — never `create_order`/`cancel_order`/`set_leverage`), and emits a hash-chain-verified `OBSERVED_EXECUTION_MEXC_V1` snapshot. Default CLI invocation performs no network call at all; a real query requires both `--query-live` and `AURA_MEXC_OBSERVATION_ENABLED=true`, mirroring `.27`'s double-gate convention.

Not yet done, correctly deferred per your sequencing: wiring `.18` to consume this schema, deciding `.17`'s ledger-event vocabulary for `.27`/`.28` outcomes, and any live-account calibration of the lookback window or the raw-state-`3` assumption (§4). The files are untracked in git, not yet committed — awaiting your review before any commit/push, consistent with how `.27` was handled.

Waiting on your call on §3 before writing any `.28` code, per your own sequencing (schema settled → `.18` updated → `.17` vocabulary decided → integration).
