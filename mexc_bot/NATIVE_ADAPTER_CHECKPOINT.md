# Native MEXC Data Adapter — Pre-Download Checkpoint

**Date:** 2026-09-17
**Status:** Step 3 checkpoint, per instruction — adapter built and offline-verified, **720-day download NOT started.** Waiting for approval to proceed, and for a decision on *where* it runs (see §7).

---

## 1. The three verified facts

Live read-only calls to MEXC were attempted first and are not possible from this environment (see §7) — WebFetch was also tried and blocked by its provenance rule (can't fetch an arbitrary constructed API URL that hasn't appeared in a prior search/fetch). Instead, all three were resolved from **ccxt 4.5.78's own installed source** (`ccxt/mexc.py`), which embeds real MEXC responses captured during ccxt's own development/testing, plus the exact unit-conversion code ccxt applies to them. This is code-level evidence, not a guess, and it's the same library `core/data_fetcher.py` already trusts today.

| # | Question | Answer | Evidence |
|---|---|---|---|
| 1 | Kline `time` field unit | **Seconds** | ccxt's captured example: `"time":[1634052300,...]` (10-digit = seconds-scale). Confirmed structurally: ccxt calls `convert_trading_view_to_ohlcv(data, ..., ms=False)`, and `ms=False` routes through `safe_timestamp()` — a seconds→ms ×1000 conversion. ccxt would not multiply if the source were already ms. |
| 2 | `open/high/low/close` vs `real*` | ccxt (and now this adapter) uses the **plain** fields | `convert_trading_view_to_ohlcv(data, 'time', 'open', 'high', 'low', 'close', 'vol')` — hardcoded, never touches `realOpen/realHigh/realLow/realClose`. Matching this matters: using `real*` instead would silently produce different bars than every backtest run against this strategy to date. |
| 3 | Funding-history order/units | `resultList` is **newest-first** (descending `settleTime`); `settleTime` is in **milliseconds** | ccxt's captured example: entry 0 `settleTime: 1609804800000` (2021-01-05 04:00 UTC) is *later* than entry 1's `1609776000000` (2021-01-04 20:00 UTC, 8h earlier) — index 0 is newest. Both are 13-digit (ms-scale) — unlike kline `time`, no unit conversion is applied (`safe_integer`, not `safe_timestamp`). ccxt then explicitly re-sorts ascending after fetching, which it would only need to do if the raw order were descending. |

**One item stays flagged, not guessed:** the funding endpoint's max `page_size` — MEXC's docs table says 1000, ccxt's inline code comment says "maximum is 100." The adapter doesn't pick a side; it requests 1000 but reads back the `pageSize` the API actually applied on every response and paginates against that, so it's correct either way.

All three (plus the schema/idempotent-symbol-mapping behavior) are covered by an offline unit test (`test_mexc_native_parsing.py`, run against the exact fixtures quoted above) — all passing. No live network call was made or needed for this part.

## 2. Adapter design

New file **`core/mexc_native.py`** — a standalone module, does not touch or import from `core/data_fetcher.py`, `core/strategy.py`, or anything the backtester/live bot currently runs. Nothing in the existing strategy path changed.

Functions:
- `to_native_symbol(symbol)` — `"BTC/USDT:USDT"` → `"BTC_USDT"`; idempotent (native symbols pass through unchanged).
- `fetch_kline_page` / `kline_response_to_df` — one raw request / parse into a DataFrame.
- `fetch_ohlcv_range_native(symbol, interval, start_ms, end_ms)` — pages a full date range at up to 2000 candles/request, advancing the window forward, deduplicating and trimming to the requested bounds. Mirrors the existing `fetch_ohlcv_range`'s loop shape.
- `fetch_funding_page_native` / `funding_response_to_df` — one raw request / parse.
- `fetch_funding_history_native(symbol, cutoff_ms)` — pages **forward from page 1** (newest-first, per fact #3) and stops as soon as a page's oldest row is already older than the requested cutoff — no need to walk all the way back through a symbol's entire history.
- Rate limiting: a flat 0.15s sleep between paginated requests (documented limit is 20 req/2s = 10 req/s; this leaves headroom rather than riding the limit exactly).

## 3. Exact symbol mapping

All 11 `DEFAULT_SYMBOLS`, verified by direct import/execution (not by hand):

```
BTC/USDT:USDT  -> BTC_USDT
ETH/USDT:USDT  -> ETH_USDT
BNB/USDT:USDT  -> BNB_USDT
XRP/USDT:USDT  -> XRP_USDT
ADA/USDT:USDT  -> ADA_USDT
DOGE/USDT:USDT -> DOGE_USDT
AVAX/USDT:USDT -> AVAX_USDT
DOT/USDT:USDT  -> DOT_USDT
SUI/USDT:USDT  -> SUI_USDT
TIA/USDT:USDT  -> TIA_USDT
COTI/USDT:USDT -> COTI_USDT
```

`cfg.TIMEFRAME = "1h"` maps to native interval **`Min60`** (confirmed via the same import — MEXC has no `Hour1`).

## 4. Output schema

**OHLCV** (`ohlcv_df`, one row per candle):

| column | source | notes |
|---|---|---|
| `timestamp` | native `time` × 1000 | pandas datetime64, UTC |
| `open`, `high`, `low`, `close` | native plain fields | per fact #2 — matches existing ccxt-based bars |
| `volume` | native `vol` | |
| `amount` | native `amount` | not used by the strategy; kept for audit |
| `real_open`, `real_high`, `real_low`, `real_close` | native `real*` | NaN if absent from the response; kept so the fair-price-adjusted view isn't lost even though it's unused today |

**Funding** (`funding_df`, one row per settlement):

| column | source | notes |
|---|---|---|
| `timestamp` | native `settleTime` (ms, no conversion needed) | pandas datetime64, UTC |
| `funding_rate` | native `fundingRate` | matches existing `funding_rate` column name in `data_fetcher.py` |
| `collect_cycle_hours` | native `collectCycle` | new field vs. what `ccxt` exposes; useful cross-check against `cfg.FUNDING_INTERVAL_HOURS = 8` |

## 5. Timestamp handling

- Kline: request `start`/`end` in **seconds** (native unit); response `time` also seconds → parsed with `unit="s"` directly into UTC-aware `pd.Timestamp`. The rest of the codebase (ms-based `start_ms`/`end_ms` args, matching `data_fetcher.py`'s convention) is converted to/from seconds only at the native-adapter boundary — nothing upstream of this module needs to change units.
- Funding: `settleTime` is already milliseconds — parsed with `unit="ms"` directly, no scaling.
- Both are timezone-aware UTC on output, unlike the existing `data_fetcher.py` functions, which currently produce timezone-naive timestamps (`pd.to_datetime(..., unit="ms")` with no `utc=True`). **This is a deliberate, flagged difference**, not an oversight — worth a decision before this feeds the backtester: keep it UTC-aware (safer, avoids silent DST/local-time bugs) and adjust the backtester's join logic if it assumes naive timestamps, or strip the tz to match today's behavior exactly. Not resolved here since it touches how the data plugs into existing code, which is out of scope for "build the adapter."

## 6. Handling of funding

- Preserves the existing `data_fetcher.fetch_funding_history` fallback contract: any endpoint failure or empty result should produce an empty DataFrame rather than raising, so the backtester's existing fallback to `cfg.FUNDING_RATE_FALLBACK` keeps working unmodified. (The current `fetch_funding_history_native` raises on an HTTP-level or `success:false` failure rather than swallowing it — that's a **gap**, flagged, not fixed silently: the runner script (`fetch_historical_native.py`) doesn't yet wrap per-symbol calls in a try/except that would let one bad symbol fall back to empty instead of aborting the whole run. Easy to add; deliberately left visible rather than quietly wrapping every failure mode before you've seen how it behaves.)
- `collect_cycle_hours` is captured per settlement so a later step can verify it matches `cfg.FUNDING_INTERVAL_HOURS = 8` for every symbol rather than assuming.

## 7. Known blocker — where this can actually run

**This cloud sandbox cannot reach `contract.mexc.com`.** Confirmed directly, not inferred: a `curl` from this sandbox's shell failed, and the sandbox's own network-proxy status endpoint recorded the reason —

```
"kind": "connect_rejected",
"detail": "gateway answered 403 to CONNECT (policy denial or upstream failure)",
"host": "contract.mexc.com:443"
```

That's a policy-level block on this domain from this environment, not a transient error. `WebFetch` was tried as a fallback and refused on a separate ground (its provenance rule — it won't fetch a URL that hasn't appeared in a prior search/fetch result, and a raw JSON API endpoint isn't a good fit for it regardless).

Practically, this means the adapter above is built and unit-verified from here, but the actual 720-day download **cannot run from this sandbox as-is**. Your Windows machine ("martinisma") is currently linked to this session with the `AI agents\AI automated trading` folder connected — the same repo this adapter lives in. Running `fetch_historical_native.py` from there via the device-bridge shell is the likely path (its network isn't subject to this sandbox's proxy policy), but that hasn't been tried, and given your working style I'm flagging it rather than just doing it — happy to test connectivity there first if you'd like, before committing to a full 720-day pull.

## 8. Proposed dataset directory structure

```
mexc_bot/data/native/
  ohlcv/
    BTC_USDT_1h.parquet        # ... one per symbol, 11 files
  funding/
    BTC_USDT_funding.parquet   # ... one per symbol, 11 files
  raw/
    ohlcv/BTC_USDT/*.json       # every raw kline page response, verbatim
    funding/BTC_USDT/*.json     # every raw funding page response, verbatim
  manifest.json                 # per-symbol row counts, actual date range
                                 # covered, fetch timestamp, and the
                                 # assumptions (facts #1-#3 above) in force
                                 # at fetch time
```

The `raw/` tree is the "preserve enough native information to reproduce the dataset later" requirement — it's not just an intermediate; if a parsing bug is found after the fact, the parquet files can be rebuilt from `raw/` without re-hitting MEXC.

## 9. Remaining uncertainty

1. **Where the download runs** — see §7. Needs a decision before anything else here proceeds.
2. **Funding `page_size` real cap** (1000 per docs vs. 100 per ccxt's comment) — handled defensively (reads the actual applied `pageSize` back), but not resolved to a single number; only matters for how many requests the full pull takes, not for correctness.
3. **Timezone-awareness mismatch** vs. the existing (naive-timestamp) `data_fetcher.py` output — flagged in §5, not resolved; affects how this plugs into the backtester later, not the adapter itself.
4. **Funding fetch failure handling** — currently raises rather than falling back to empty per-symbol, unlike `data_fetcher.py`'s existing contract (§6) — a small, deliberate gap left visible rather than patched without discussion.
5. **realOpen/High/Low/Close semantics** — confirmed *that* ccxt ignores them and this adapter matches that, but not *why* MEXC reports two OHLC variants (fair-price vs. last-traded-price) for this endpoint specifically, or whether that distinction should ever matter for this strategy. Out of scope here; noted for later.

Nothing beyond this has been touched — no live MEXC calls were made, no data was downloaded, `core/strategy.py` / `core/indicators.py` / `core/risk_manager.py` are unchanged.
