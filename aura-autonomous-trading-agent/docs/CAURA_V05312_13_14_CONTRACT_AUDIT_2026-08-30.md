# CAURA v0.5.3.12 → .13 → .14 Contract Audit

Date: 2026-08-30
Branch: `audit/caura-v052-to-aura-v053`

## Source snapshot

Audited directly from the uploaded full `CAURAv0.5.2_BACKUP_2026-08-30.zip` snapshot.

Exact source SHA-256 hashes:

- `aura_v05312_market_state_engine.py` — `64044556e6d79ec0e08e68ab64960f3d5b26d8489403d77034ff7ca3989ab239`
- `aura_v05313_signal_decision_engine.py` — `69d56af5488896e24637935760e0a4fbdc135d3f1c3d3f1e1609d868168373b7`
- `aura_v05314_risk_gate.py` — `caea8c4dc9aab105ad2ed515a9bd8e8bf5980b642e9dd6ea0ce7d959bcf7d822`

All three are syntactically parseable Python source and contain no exchange/network execution dependency. Their imports are limited to standard library plus NumPy/Pandas in `.12`.

## GitHub comparison

The current GitHub `main` tree does not contain these three filenames. A direct repository lookup for `aura_v05312_market_state_engine.py` returned not found, and GitHub code search for `v05314` returned zero results.

Therefore the uploaded CAURA source is the authoritative recovery source for these three components until explicitly promoted.

## .12 Market State Engine

The source is correctly designed as the canonical market-state producer:

- consumes closed 1H OHLCV;
- validates chronology, duplicates, OHLCV integrity, freshness and continuity;
- constructs closed 4H candles without interpolation/forward-fill;
- computes the frozen research indicators once;
- publishes a deterministic canonical payload and SHA-256 state hash;
- fails closed on invalid required data;
- does not fetch from Alpaca or place orders.

Frozen configuration in the source:

```text
candidate          = BEAR x LOW ATR x POSITIVE bar-2
ATR threshold      = 0.596%
4H EMA             = 50
1H ATR period      = 14
bar-2 lag           = 2 hours
symbols             = BTC/USD, ETH/USD
```

The canonical payload contains:

```text
agent_version
engine
 generated_at
symbols
frozen_configuration
```

Inside `canonical_state.symbols`, each symbol maps directly to its market-state dictionary.

## .13 Signal Decision Engine

The source correctly states that it must consume only the canonical `.12` market-state JSON and must not fetch data, recalculate indicators, resample candles, tune parameters, or execute trades.

It verifies:

- source engine;
- source data validity;
- source guardrails;
- frozen configuration;
- `.12` state hash;
- deterministic state ID;
- required BTC/USD and ETH/USD state presence.

It evaluates the frozen candidate using fields supplied by `.12` and returns `SIGNAL_CANDIDATE`, `NO_SIGNAL`, or `BLOCKED`.

## .14 Risk Gate

The source correctly treats the Risk Gate as a security boundary rather than a strategy optimizer.

It verifies:

- upstream engine/version;
- upstream decision status;
- state identity and hash verification flag;
- frozen configuration verification;
- single-source-of-truth guardrails;
- no indicator recalculation;
- no market-data fetch;
- orders/paper/live execution disabled;
- required symbol set;
- timestamp synchronization;
- candidate consistency.

Semantics are correctly intended to be:

```text
SIGNAL_CANDIDATE + all checks pass → RISK_PASS
NO_SIGNAL                         → NO_SIGNAL
invalid/untrusted/inconsistent    → BLOCKED
```

Critically, the source hard-codes:

```text
execution_permitted = False
```

even when `RISK_PASS` is reached.

## CONTRACT DEFECT FOUND

The actual source snapshot exposes a concrete `.12 → .13` schema mismatch.

`.12` creates the canonical payload with:

```text
canonical_state.symbols["BTC/USD"] = <market_state dictionary>
canonical_state.symbols["ETH/USD"] = <market_state dictionary>
```

But `.13` `verify_state_hash()` iterates over `canonical_state.symbols` and expects each symbol entry to contain:

```text
item["market_state"]
```

That wrapper does not exist in `.12`'s canonical payload.

The same mismatch is visible in the supplied generated artifacts inside the CAURA snapshot:

- `market_state_snapshot.json` contains a top-level `symbols` structure where each symbol has `data_status`, `market_state_valid`, `invalid_reasons`, and `market_state`.
- Its nested `canonical_state.symbols` structure contains the raw market-state dictionary directly.
- The supplied `signal_decision.json` is `BLOCKED` with `MISSING_MARKET_STATE:BTC/USD` and `MISSING_MARKET_STATE:ETH/USD`.

Therefore this is not a theoretical concern; the supplied snapshot contains direct evidence of the failure.

## Reproduction

Running the exact `.12` source against the included canonical closed-bar dataset succeeds and produces a valid state. In the audit environment it produced:

```text
DATA STATUS        : VALID
MARKET STATE VALID : True
```

The generated state is internally deterministic.

Running the exact `.13` source against that `.12` output fails closed with:

```text
STATUS           : BLOCKED
OVERALL DECISION : BLOCKED
STATE HASH VERIFIED : False

MISSING_MARKET_STATE:BTC/USD
MISSING_MARKET_STATE:ETH/USD
```

Running `.14` against that blocked `.13` output also correctly fails closed:

```text
STATUS               : BLOCKED
RISK AUTHORIZED      : False
EXECUTION PERMITTED  : False
```

This proves the current recovered chain is **not yet acceptance-ready**.

## Important interpretation

This audit does NOT conclude that `.12` should be changed or that `.13` should be changed yet.

The architecture explicitly makes `.12` the Single Source of Truth. Therefore the next engineering decision must be made at the contract level first:

1. Freeze the intended canonical schema.
2. Decide whether `.13` must consume the exact `.12` canonical payload as emitted, or whether a separately defined envelope is intended.
3. Update only the consumer/producer necessary to make that contract explicit.
4. Add fixtures that prove the contract.
5. Re-run the eight `.14` acceptance cases.

Do not make a cosmetic change merely to make the current test pass.

## Required `.14` acceptance matrix

1. VALID + NO_SIGNAL → `NO_SIGNAL`
2. VALID + SIGNAL_CANDIDATE → `RISK_PASS` with `execution_permitted=false`
3. Bad state hash → `BLOCKED`
4. Missing BTC/USD → `BLOCKED`
5. Missing ETH/USD → `BLOCKED`
6. Timestamp mismatch → `BLOCKED`
7. Candidate/reason/match inconsistency → `BLOCKED`
8. Malformed JSON → `BLOCKED`

## Promotion decision

**PROMOTION STATUS: HOLD**

Do not promote `.12–.14` to `main` yet.

The source is successfully recovered and the defect is now reproducible and precisely localized. The next change should be a deliberate contract fix on this audit branch, followed by deterministic regression tests.

No live trading, paper execution, or execution authorization is enabled by this audit.
