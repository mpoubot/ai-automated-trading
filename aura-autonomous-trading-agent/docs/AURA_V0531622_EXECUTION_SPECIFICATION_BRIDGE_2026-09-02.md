# AURA v0.5.3 — `.22` Contract Fix and Execution Specification Bridge

Date: 2026-09-02
Branch: `paper/runtime-alpaca-2026-09-01`

## Summary

Two changes, done in sequence, in response to the open question: "where
does `side` (BUY/SELL) actually come from for the v0.5.3.22 Alpaca PAPER
adapter, and does the chain ever silently map `BEAR -> SELL`?"

1. **Fixed a defense-in-depth gap in both `.22` files.** Both built the
   Alpaca `OrderSide` with a ternary (`OrderSide.BUY if spec["side"] ==
   "BUY" else OrderSide.SELL`) that had no third branch. Both call sites
   are already downstream of a fail-closed validator that rejects any
   `side` outside `{"BUY", "SELL"}`, so this was not currently
   exploitable — but a future loosening of that validator alone could
   have turned an unexpected value into a silent `SELL`. Replaced with a
   closed `side_map` lookup that rejects/raises on anything else.

2. **Traced the full `.12 -> .13 -> .14 -> .15 -> .16 -> ... -> .19`
   chain and confirmed it never produces `side`, `quantity`,
   `order_type`, or `client_order_id` anywhere.** `.15` (Position State)
   only ever emits `ENTRY_CANDIDATE` / `FLAT`; `.16` (Paper Execution)
   only ever emits `PAPER_ORDER_INTENT` / `NO_ORDER`. `.19` (Execution
   Safety) hard-codes `execution_authorized`, `paper_execution_authorized`,
   and `live_execution_authorized` to `False` on every run — by design,
   research-only. `.21`'s own docstring anticipated this gap and warned
   that "a future Alpaca paper-order adapter must consume a complete
   execution specification ... produced by the risk-authorized chain;
   this supervisor must never invent those fields." That component did
   not exist. Both `.22` files' own test suites only ever passed because
   their tests hand-construct a fake spec with `side` hardcoded — nothing
   derived it from real chain output.

## Direction rule (explicit decision, not inferred by AI)

The only frozen candidate `.13` currently evaluates is
`BEAR x LOW ATR x POSITIVE bar-2`. The repository's own research record
does not establish that this candidate is a validated short-entry
strategy — the frozen long-only research hypothesis (EMA3/EMA8 bullish
crossover + MACD histogram + relative volume) is a separate, unrelated
research track, and the v0.5.1 `signal_master.csv` explicitly labels its
own observations "no trading decision added."

Decision: **`BEAR` must never map to `SELL`.** Until a validated
directional strategy explicitly supplies an executable direction, `BEAR`
produces `NO_EXECUTION_DIRECTION` / fail-closed, not an order.

## What was built: `aura_v05323_execution_specification_builder.py`

The missing bridge between `.19` (Execution Safety) + `.13` (Signal
Decision) and `.22`. For each symbol it produces either a complete
Execution Specification matching `aura_v05322_alpaca_paper_execution_adapter.py`'s
schema, or an explicit `BLOCKED` record with a reason. It never invents a
direction or a quantity:

- **Direction** comes only from two explicit, closed allowlists —
  `VALIDATED_LONG_ENTRY_REGIME_LABELS` and
  `VALIDATED_SHORT_ENTRY_REGIME_LABELS` — both **empty by default**. A
  `.13` `regime_state` produces `BUY` / `SELL` only if it is an exact
  member of the corresponding set. `BEAR x LOW ATR x POSITIVE bar-2` is a
  member of neither, so it always fails closed today. Populating either
  set is a deliberate, reviewed research decision — never inferred here.
- **Quantity** is never computed or defaulted. It must be supplied via an
  explicit `--sizing-config` JSON file (`{"BTC/USD": <qty>, "ETH/USD":
  <qty>}`); a missing or invalid entry is `MISSING_QUANTITY_SOURCE`, a
  fail-closed condition, not a default.

Tests (`tests/test_aura_v05323_execution_specification_builder.py`, 6/6
pass) prove, among other things:

- the real `BEAR` regime label produces `NO_VALIDATED_EXECUTION_DIRECTION`
  for both symbols, never a `side` key at all;
- a hypothetically-validated long-entry label with no sizing config still
  fails closed (`MISSING_QUANTITY_SOURCE`), never defaults a quantity;
- a hypothetically-validated long-entry label **with** sizing config
  produces a spec that is handed to the real, canonical `.22` adapter's
  own `validate_spec()` and is accepted outright — the actual contract
  proof that this bridge's output shape matches what `.22` expects.

## Current end-to-end status

Even with this bridge in place, the chain **still cannot reach a live
paper order today**, because `.19` unconditionally sets
`execution_authorized = False` on every run (research-only, untouched by
this change), and no `regime_state` currently in the codebase is on
either direction allowlist. That is the correct, safe state until both
of those are deliberately, separately opened by a reviewed decision.

## Superseded file

Per decision, `aura_v05322_alpaca_paper_execution_adapter.py` is treated
as canonical going forward. `aura_v05322_alpaca_paper_execution.py` (the
earlier draft — different credential env-var names, `kill_switch_active`
instead of `kill_switch`, no `execution_spec_version`) has been marked
`[SUPERSEDED]` in its docstring. It has not been deleted or had its
behavior changed; its own tests still pass. Deletion is left for a
follow-up once nothing references it.

## Not merged to `main`

All of the above is committed on `paper/runtime-alpaca-2026-09-01` only.
