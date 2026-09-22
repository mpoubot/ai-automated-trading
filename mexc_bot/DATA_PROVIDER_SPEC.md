# Native MEXC Futures Data Provider — Endpoint Specification

**Date:** 2026-09-17
**Status:** Step 2 of the MEXC-native reproducible-validation exercise. This document specifies the exact REST endpoints, parameters, and response shapes a native MEXC Futures adapter must implement to reproduce, without `ccxt`, the OHLCV and funding-rate data currently pulled by `core/data_fetcher.py`. It does not implement the adapter, download data, or touch the strategy — that's Step 3.
**Sources:** MEXC's own API docs, cross-checked across two documentation surfaces that currently coexist (`mexcdevelop.github.io/apidocs/contract_v1_en/` and `mexc.com/api-docs/futures/...`); both describe the same endpoints and agree except where noted below.

---

## 1. Why this is needed

Step 1 (`STRATEGY_SPEC.md`) established that the bot trades MEXC USDT-margined perpetual swaps on the `1h` timeframe, and needs two data series per symbol:

- OHLCV candles → feeds `core/indicators.py` / `core/strategy.py`.
- Funding-rate history → feeds the backtester's funding-cost model (`USE_REAL_FUNDING_HISTORY = True`, `FUNDING_RATE_FALLBACK` only as a last resort).

Both are currently fetched via `ccxt`'s MEXC swap wrapper (`fetch_ohlcv` / `fetch_ohlcv_range`, `fetch_funding_rate_history`), which pages by `since` (ms) + `limit=1000`. A native adapter replacing `ccxt` must reproduce those two DataFrame shapes — `[timestamp, open, high, low, close, volume]` and `[timestamp, funding_rate]` — from MEXC's own contract endpoints, which have a **different pagination model and response layout** than `ccxt` presents. The differences below are the load-bearing part of this spec.

---

## 2. Symbol format conversion

`ccxt` uses `"BTC/USDT:USDT"` (unified swap notation). MEXC's native contract API uses underscore notation: `"BTC_USDT"`.

Conversion needed: strip the `:USDT` settle suffix, replace `/` with `_`. E.g. `"BTC/USDT:USDT"` → `"BTC_USDT"`. This must be applied to every symbol before calling either endpoint below, and reversed (or tracked alongside) when writing results back into the existing DataFrame/backtester pipeline, which expects `ccxt`-style symbols elsewhere in the codebase.

---

## 3. K-line (OHLCV) endpoint

```
GET /api/v1/contract/kline/{symbol}
```

Public endpoint — no authentication, no API key required.

**Path parameter:**

| Name | Type | Required | Notes |
|---|---|---|---|
| `symbol` | string | yes | Native format, e.g. `BTC_USDT` — see §2 |

**Query parameters:**

| Name | Type | Required | Notes |
|---|---|---|---|
| `interval` | string | no | One of `Min1`, `Min5`, `Min15`, `Min30`, `Min60`, `Hour4`, `Hour8`, `Day1`, `Week1`, `Month1`. Default `Min1` if omitted. The bot's `TIMEFRAME = "1h"` maps to **`Min60`** (not `Hour1` — MEXC has no `Hour1`, only `Hour4`/`Hour8`). |
| `start` | long | no | **Unix timestamp in SECONDS, not milliseconds.** This is a divergence from `ccxt`'s `since`, which `fetch_ohlcv_range` passes in ms — the native adapter must divide by 1000. |
| `end` | long | no | Same units as `start`. |

**Response:**

```json
{
  "success": true,
  "code": 0,
  "data": {
    "time":  [1694... , ...],
    "open":  [...], "close": [...], "high": [...], "low": [...],
    "vol":   [...], "amount": [...],
    "realOpen": [...], "realClose": [...], "realHigh": [...], "realLow": [...]
  }
}
```

**Critical shape difference from `ccxt`:** this is **columnar** (parallel arrays, one per field, indexed by position), not the row-per-candle list of lists `ccxt.fetch_ohlcv` returns. The adapter must `zip()` `time`/`open`/`high`/`low`/`close`/`vol` into rows before building the DataFrame — `pd.DataFrame(raw, columns=[...])` as currently written in `data_fetcher.py` will not work directly against this payload.

**`open`/`high`/`low`/`close` vs `realOpen`/`realHigh`/`realLow`/`realClose`:** MEXC contract klines report `open`/`high`/`low`/`close` as **fair-price-adjusted** values for some contract types, with `real*` holding the unadjusted last-traded-price OHLC. Which one `ccxt`'s `fetch_ohlcv` maps to for MEXC swaps is not something I can confirm from documentation alone — this needs either a source-level check of `ccxt`'s MEXC implementation or a live side-by-side comparison before the native adapter is trusted to reproduce identical backtest numbers. Flagging rather than guessing.

**Time unit of the response `time` field:** documentation doesn't explicitly restate the unit for the response array (only for the `start`/`end` request params, which are seconds). Given MEXC's contract API is second-denominated elsewhere (funding `settleTime` is also typically seconds-scale on this API), the working assumption is `time` is in **seconds**, requiring `* 1000` before `pd.to_datetime(..., unit="ms")`, or `unit="s"` directly — but this should be verified empirically against one real response before being relied on, since getting it wrong silently shifts every candle by a 1000x timestamp error rather than raising.

**Limits:**
- Max **2000** candles per request (vs. `ccxt`'s 1000-row pagination batches currently hardcoded in `fetch_ohlcv_range`/`fetch_funding_history` — the native adapter can use a larger page size and will need fewer round trips for the same history).
- Rate limit: **20 requests / 2 seconds** (10 req/s) — tighter than assuming `ccxt`'s generic `exchange.rateLimit` sleep is correctly calibrated for this specific endpoint; the native adapter should hardcode its own limiter against this documented number rather than inherit a generic default.

**Pagination for long ranges:** unlike `ccxt.fetch_ohlcv`, which pages forward via `since`, this endpoint takes both `start` and `end`. For a range longer than 2000 candles at the requested interval, the adapter pages by advancing `start` to just past the last returned candle's `time` (mirroring the existing `fetch_ohlcv_range` loop logic, just with second-based timestamps and a 2000-row batch instead of 1000).

---

## 4. Funding rate history endpoint

```
GET /api/v1/contract/funding_rate/history
```

Public endpoint — no authentication required.

**Query parameters:**

| Name | Type | Required | Notes |
|---|---|---|---|
| `symbol` | string | yes | Native format, e.g. `BTC_USDT` |
| `page_num` | int | yes | Current page, default `1` |
| `page_size` | int | yes | Default `20`, **max 1000** |

**Response:**

```json
{
  "success": true,
  "code": 0,
  "data": {
    "pageSize": 1000,
    "totalCount": 1619,
    "totalPage": 2,
    "currentPage": 1,
    "resultList": [
      { "symbol": "BTC_USDT", "fundingRate": 0.0001, "settleTime": 1694..., "collectCycle": 8 }
    ]
  }
}
```

**Critical difference from `ccxt`:** this is **page-number pagination**, not `since`/`limit` time-range pagination. There is no documented `start`/`end` filter — the only way to bound a request to a date range is to page through `resultList` (at `page_size=1000`, matching the endpoint's max) and stop once `settleTime` walks past (or before, depending on sort order) the window the backtester needs. **Sort order of `resultList` (newest-first vs. oldest-first) is not stated in either documentation source** — this determines whether the adapter should start at `page_num=1` and stop early, or must walk all pages to reach the desired window. This needs to be checked against one real response before the pagination loop is written, since assuming the wrong order either wastes requests or silently truncates history.

**`collectCycle`:** funding interval in hours, present in the `mexc.com/api-docs` version of this endpoint's documented response but *not* listed in the older `mexcdevelop.github.io` version — likely a newer field. Useful cross-check against the bot's own `FUNDING_INTERVAL_HOURS = 8` assumption; worth asserting the two agree per-symbol rather than assuming.

**Limits:**
- `page_size` max 1000 per page (no documented cap on `totalPage`/history depth — the funding-rate-history docs page example shows `totalCount: 1619` for one symbol as a real observed figure, not a stated ceiling).
- Rate limit: **20 requests / 2 seconds**, same as kline.

**Fallback behavior to preserve:** `data_fetcher.fetch_funding_history` currently returns an *empty* DataFrame (not an exception) whenever funding data isn't available, and the caller falls back to `cfg.FUNDING_RATE_FALLBACK`. The native adapter should preserve that contract — return empty on any non-`success` response or on a symbol with `totalCount: 0` — rather than raising, so the existing fallback logic in the backtester keeps working unmodified.

---

## 5. Net differences the native adapter must handle (summary)

| | `ccxt` (current) | Native MEXC contract API |
|---|---|---|
| Symbol format | `BTC/USDT:USDT` | `BTC_USDT` |
| Kline time params | `since` (ms), `limit` | `start`/`end` (**seconds**) |
| Kline response shape | row-per-candle list | **columnar** parallel arrays, plus `real*` variants |
| Kline page size | 1000 (as coded) | up to 2000 |
| Funding pagination | `since` (ms) + `limit` | `page_num` + `page_size` (max 1000), **no time filter** |
| Funding response fields | `timestamp`, `fundingRate` | `settleTime`, `fundingRate`, `collectCycle` |
| Auth | keys optional (public data) | none required for either endpoint |
| Rate limit | generic `exchange.rateLimit` | documented 20 req / 2s for both endpoints specifically |

**Open items requiring empirical verification before the adapter can be trusted (not resolvable from documentation alone):**
1. Unit of the kline response `time` field (assumed seconds — needs confirmation).
2. Whether `ccxt.fetch_ohlcv` for MEXC swaps currently surfaces `open/high/low/close` or the `real*` variants — needed so the native adapter matches existing backtest numbers rather than silently changing them.
3. Sort order (`resultList` newest-first vs. oldest-first) of the funding-rate-history endpoint.

These three should be checked with a small number of live read-only calls (comparing native response vs. current `ccxt` output for one symbol) as the first task of Step 3, before writing the adapter itself — not guessed at.

---

## 6. Sources

- [MXC CONTRACT API (mexcdevelop.github.io)](https://mexcdevelop.github.io/apidocs/contract_v1_en/)
- [Get Candlestick Data (mexc.com/api-docs)](https://www.mexc.com/api-docs/futures/market-endpoints/get-candlestick-data)
- [Get Funding Rate History (mexc.com/api-docs)](https://www.mexc.com/api-docs/futures/market-endpoints/get-funding-rate-history)
- [Futures Market Endpoints index (mexc.com/api-docs)](https://www.mexc.com/api-docs/futures/market-endpoints/)
