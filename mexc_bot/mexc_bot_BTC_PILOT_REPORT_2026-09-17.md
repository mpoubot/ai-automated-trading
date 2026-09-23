# BTC_USDT Pilot — Pre-Download Verification Report

**Date:** 2026-09-17
**Status:** Pilot complete. **Full 11-symbol / 720-day download NOT started** — waiting for your go-ahead, per instruction. No strategy files touched, no optimization run, no Alpaca, no live trading, no git commit/push.

---

## 1. Windows connectivity result

**Works — confirmed live, not assumed.** This cloud sandbox still cannot reach `contract.mexc.com` (that block is unchanged), but no shell/`device_bash` tool is available on the linked Windows machine in this session — only the desktop app's browser pane and file-transfer tools. So connectivity was tested by driving the browser pane on Martin's machine ("martinisma") directly to the native API URLs and reading the raw response back — a real read-only GET from that machine's own network, not a simulation.

- `GET https://contract.mexc.com/api/v1/contract/kline/BTC_USDT?interval=Min60&start=...&end=...` → succeeded, real data returned.
- `GET https://contract.mexc.com/api/v1/contract/funding_rate/history?symbol=BTC_USDT&page_num=1&page_size=5` (and again with `page_size=1000`) → succeeded, real data returned.

One consequence worth flagging for the eventual full run: without a `device_bash` tool in this session, the actual acquisition script (`fetch_historical_native.py`) can't currently be *executed* on the Windows machine from here — driving 99+ paginated requests one at a time through browser-pane page loads would be slow and fragile. If `device_bash` becomes available (or you run the script yourself locally with `python fetch_historical_native.py`), that's the clean path. Otherwise we'd need to talk through an alternative before the full run.

## 2. Actual API response structure

Both endpoints matched the Step-2 spec and the ccxt-derived predictions exactly — nothing in the live response contradicted what was written down beforehand. Raw responses saved verbatim in the new `data/native/raw/` tree:

- `data/native/raw/ohlcv/BTC_USDT/pilot_live_2026-09-17.json`
- `data/native/raw/funding/BTC_USDT/pilot_live_2026-09-17_page1_size5.json`

Kline: `{"success", "code", "data": {"time", "open", "close", "high", "low", "vol", "amount", "realOpen", "realClose", "realHigh", "realLow"}}` — columnar, all 11 arrays present and populated (not just the subset ccxt's older captured example showed).

Funding: `{"success", "code", "data": {"pageSize", "totalCount", "totalPage", "currentPage", "resultList": [{"symbol", "fundingRate", "settleTime", "collectCycle"}]}}`.

## 3. Row count / date range

- **OHLCV pilot:** requested a 3-day window (`start`/`end` ≈ now−72h to now) at `Min60` → **72 rows**, exactly matching 72 hourly candles for 3 days. Chronological range: **2026-09-14 08:00:00 → 2026-09-17 07:00:00** (naive, UTC instant — see §4).
- **Funding pilot:** `page_num=1, page_size=5` → **5 rows**, most recent settlement **2026-09-17 00:00:00 UTC**. A follow-up diagnostic call with `page_size=1000` returned **all 1000 rows in full** (`pageSize: 1000` echoed back, `totalCount: 1619`, `totalPage: 2` — 1000 + 619 = 1619, consistent) — this resolves the one open uncertainty from the Step-2/checkpoint docs: **MEXC's documented 1000-row cap is correct; ccxt's inline code comment claiming a 100-row cap is stale.**

## 4. Timestamp interpretation

Both facts from the checkpoint doc were confirmed against live data, not just ccxt's captured examples:

- Kline `time` is **seconds**: every value in the pilot response satisfies `time % 3600 == 0` (lands exactly on the hour) and decodes to the correct real calendar date when interpreted as seconds (e.g. `1789372800` → `2026-09-14 08:00:00 UTC`).
- Funding `settleTime` is **milliseconds**, and settlements are exactly 8 hours apart (`collectCycle: 8` on every row, matching `cfg.FUNDING_INTERVAL_HOURS = 8`).

**One change made as a result of this pilot:** the adapter originally produced timezone-*aware* UTC timestamps (a defensible default, noted as an open item in the last checkpoint). Reading `backtester.py` more closely surfaced a concrete incompatibility — line 78, `funding_df[funding_df["timestamp"] <= ts]`, compares an OHLCV-derived timestamp against a funding-derived one directly; mixing tz-aware and tz-naive `Timestamp` columns there raises `TypeError: Cannot compare tz-naive and tz-aware timestamps`. `core/data_fetcher.py`'s existing OHLCV/funding functions are both tz-naive (`pd.to_datetime(..., unit="ms")` with no `utc=True`). **`core/mexc_native.py` now produces tz-naive timestamps too**, matching that convention exactly — verified below, not just asserted. This also required moving pagination bookkeeping (`fetch_ohlcv_range_native`, `fetch_funding_history_native`) off `Timestamp.timestamp()` calls and onto the raw integer epoch values instead, since calling `.timestamp()` on a *naive* `Timestamp` re-interprets it in the machine's local timezone — silently wrong on any machine not set to UTC, which includes the Windows box this will likely run on.

## 5. Funding result

Live data for BTC_USDT, most recent 5 settlements:

| timestamp (UTC) | funding_rate | collect_cycle_hours |
|---|---|---|
| 2026-09-17 00:00:00 | 0.000093 | 8 |
| 2026-09-16 16:00:00 | 0.000052 | 8 |
| 2026-09-16 08:00:00 | 0.000029 | 8 |
| 2026-09-16 00:00:00 | 0.000040 | 8 |
| 2026-09-15 16:00:00 | 0.000098 | 8 |

All positive (longs pay, shorts receive) over this short window, evenly 8h-spaced, no duplicates, `collect_cycle_hours` consistently 8 — matching `cfg.FUNDING_INTERVAL_HOURS`.

**Failure-handling gap closed:** `fetch_funding_history_native` previously raised on any HTTP/response failure. It now wraps the request in a `try/except` and returns whatever was already accumulated (possibly empty) instead of raising, matching `core/data_fetcher.fetch_funding_history`'s existing empty-DataFrame-on-failure contract exactly — so one bad symbol during the eventual 11-symbol run can't abort the whole acquisition. (OHLCV fetching was left as-is, raising on failure — `data_fetcher.py` has no fallback concept for missing OHLCV either, so there's nothing to fall back to.)

## 6. Backtester compatibility

Directly tested, not inferred: `tests/test_mexc_native.py::test_backtester_compatibility` reproduces `backtester.py:78`'s exact comparison (`funding_df[funding_df["timestamp"] <= ts]`) using the live pilot data, and confirms the resulting dtype is identical to what `data_fetcher.py` itself produces for the same instant. All 6 tests pass:

```
PASS: symbol mapping
PASS: kline parsing (time units, plain vs real* fields, schema)
PASS: funding parsing (settleTime units, newest-first order preserved)
PASS: live pilot OHLCV (72 rows, no gaps, no dupes, no nulls, tz-naive, chronological)
PASS: live pilot funding (5 rows, newest-first, 8h spacing, no dupes)
PASS: backtester.py:78-style comparison succeeds; dtype matches data_fetcher.py exactly
```

OHLCV chronology/integrity, checked directly on the live pilot data: monotonically increasing, zero duplicate timestamps, zero gaps other than the expected 1-hour step, zero nulls across `open/high/low/close/volume`. Funding: zero duplicate timestamps, correctly newest-first as the raw API returns it (sorting to ascending happens one level up, in `fetch_funding_history_native`, not in the per-page parser — the per-page parser preserves raw order faithfully, which is what let this test actually verify fact #3 instead of just assuming its own output).

## 7. Remaining issues

1. **No reliable shell on the Windows machine in this session** — a `device_bash` tool briefly appeared mid-task, but two consecutive calls (including one retry, per its own guidance) both failed with "device not connected to the bridge," so it wasn't actually usable — this pilot ran entirely through the browser pane and file-transfer tools instead. That's fine for a one-symbol pilot but isn't a practical way to drive a 99-request paginated download across 11 symbols. Needs `device_bash` to come up reliably, or you running `fetch_historical_native.py` locally, before the full pull.
2. **`fetch_historical_native.py` (the full-run script) hasn't been touched since the last checkpoint** — the fixes above all landed in `core/mexc_native.py`, which it imports, so it inherits them automatically, but it hasn't been re-tested end-to-end (only the smaller pilot functions have live-verified data behind them).
3. **`realOpen/High/Low/Close` semantics** — confirmed live that they're genuinely different from the plain fields (not just a documentation formality), still don't know *why* MEXC reports two series for this endpoint. Unchanged from the last checkpoint; still out of scope.
4. **Symbols other than BTC_USDT are unverified** — this pilot only exercised BTC_USDT. The other 10 pairs are assumed to behave identically (same endpoint, same field shapes) but that's an assumption until at least spot-checked.

Nothing else changed. `core/strategy.py`, `core/indicators.py`, `core/risk_manager.py`, `core/data_fetcher.py` are all untouched. No git operations were performed.
