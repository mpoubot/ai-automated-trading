# MEXC Native Adapter — Pagination Bug Fix Report

**Date:** 2026-09-17
**Status:** Fix applied and validated. **Full 11-symbol / 720-day acquisition NOT re-run** — waiting for your go-ahead, per instruction. No strategy/backtester/config/risk-manager/data_fetcher files touched. No git commit or push.

---

## 1. Root cause of the pagination failure

`fetch_ohlcv_range_native()` in `core/mexc_native.py` paginated forward: it kept a `start`-side cursor and advanced it using each page's newest returned timestamp, on the assumption that the API returns oldest-first pages walking forward toward `end`.

Live testing against the real MEXC kline endpoint (via the browser pane on this machine, read-only GETs to `contract.mexc.com`) proved that assumption wrong. When a requested `[start, end]` window spans more than ~2000 hourly candles, MEXC's endpoint **anchors to `end`** and returns only the most recent slice counting backward from `end` — it silently ignores how far back `start` actually reaches. It does not return the oldest chunk first.

Because of that, the very first page's newest timestamp already equals the requested `end`. The old code then computed `cursor_s = last_s + 1`, which immediately exceeded `end_s`, so the `while cursor_s < end_s` loop exited after exactly one page — silently truncating every range longer than ~2000 hours down to just its most recent slice. This is exactly what produced the "2000 candles per symbol" console output across the full 11-symbol run, and it exactly reproduces your own repro:

```
requested: 2025-01-01 -> 2025-04-01 (expected ~2161 rows)
got:       ROWS: 2001, FIRST: 2025-01-07 16:00:00, LAST: 2025-04-01 00:00:00
```

This was demonstrated, not assumed: I reproduced your exact numbers live against the real API before touching any code, per your instruction not to guess.

## 2. Exact file(s) modified

**Only `core/mexc_native.py`** — specifically, only the loop body of `fetch_ohlcv_range_native()`. Nothing else in that file changed (funding-history code, kline parsing, symbol mapping, constants, `fetch_kline_page`, `_get` are all untouched). No other file was modified:

- `fetch_historical_native.py` — **not touched** (not required; the function's public signature and millisecond contract are unchanged).
- `backtester.py`, `config.py`, `core/data_fetcher.py`, `core/strategy.py`, `core/indicators.py`, `core/risk_manager.py`, `core/trade_logger.py`, `core/trade_metrics.py`, `run_broad_backtest.py`, `tests/test_mexc_native.py` — **not touched**. Confirmed via a before/after directory listing of `mexc_bot/core/`: every file's size and modified-time is identical to before except `mexc_native.py`.

## 3. Description of the fix

Replaced the forward, `start`-side cursor with a backward, `end`-side cursor:

- `start_s` stays fixed on every request (it's now sent on every page, not just the first).
- After each page, the cursor steps back to one second before that page's **oldest** returned timestamp (`page_time_s[0] - 1`), not its newest.
- The loop continues while `cursor_end_s > start_s`, and still breaks on no-progress or a short page (meaning the beginning of available history was reached) — same stop conditions as before, just anchored on the other end.

Everything else about the function is unchanged: native endpoint (`fetch_kline_page`), native symbol format, `Min60` interval, millisecond boundaries at the public `fetch_ohlcv_range_native(start_ms, end_ms)` signature, conversion to the repo's tz-naive timestamp convention, `drop_duplicates(subset="timestamp")`, final `sort_values("timestamp")`, exact trim to `[start_ms, end_ms]`, the existing per-page raw-dump behavior (dump filenames now key off the request's `end` cursor rather than `start`, since that's what varies per page now), and the existing `time.sleep(RATE_LIMIT_SLEEP_S)` between pages. The function signature did not need to change.

## 4. Test results (A–E)

All five were run by executing the **actual fixed function** (`core.mexc_native.fetch_ohlcv_range_native`, unmodified apart from the fix above) against fixtures built from live-captured, real MEXC responses — only the HTTP transport (`_get`) was replaced with a fixture replay keyed on the exact request parameters the function itself generates. This is real code execution against real (previously live-verified) data, not hand arithmetic.

| Test | Range | Pages | Raw rows | Final rows | First | Last | Result |
|---|---|---|---|---|---|---|---|
| **A** — known-good small range | 2024-09-27 → 2024-10-10 | 1 | 313 | 313 | 2024-09-27 00:00 | 2024-10-10 00:00 | **PASS** — matches previously-documented result exactly |
| **B** — crosses the 2000-row boundary | 2025-01-01 → 2025-04-01 | 2 | 2161 | 2161 | 2025-01-01 00:00 | 2025-04-01 00:00 | **PASS** — first is now correct (previously 2025-01-07 16:00); count matches the expected ~2161 |
| **C** — gap/duplicate integrity on B | (same as B) | — | — | 2161 | — | — | **PASS** — monotonically increasing, 0 duplicate timestamps, every consecutive gap exactly 1 hour, no missing candle |
| **D** — range shorter than 2000 rows | (same case as A — this adapter has one code path regardless of range length) | 1 | 313 | 313 | 2024-09-27 00:00 | 2024-10-10 00:00 | **PASS** |
| **E** — substantially larger than 2000 rows (~250 days) | 2024-06-01 → 2025-02-06 | 3 | 6001 | 6001 | 2024-06-01 00:00 | 2025-02-06 00:00 | **PASS** — 0 duplicates, all gaps exactly 1h, correctly terminates once `first == start` |

Overall: **5/5 PASS**, zero gaps, zero duplicates, exact boundary matches on every case.

One honest caveat on method: TEST D reuses TEST A's exact range rather than a second, separately-fetched small range, since the fixed function has no branch that treats "short" ranges differently — the same single-page code path is exercised either way, so a second range would not add independent evidence. TEST E's page fixtures were captured from 3 sequential live requests during root-cause diagnosis (not synthesized), so it's a real multi-page (3-page) exercise of the new backward-cursor loop, one page beyond what TEST B alone shows.

## 5. Is the 720-day acquisition now safe to run?

**Yes, with one caveat.** The fix is validated up to 3 sequential pages (~6000 rows / ~250 days) with exact boundary and integrity checks. A 720-day/1h pull is ~17,280 candles, which will need roughly 9 pages per symbol under the new backward-cursor loop — one order of magnitude more pages than what's been directly tested here, though it's the same loop and stop conditions validated at 1, 2, and 3 pages, with no reason to expect different behavior at page 9 than page 3.

Given that, and per your own instruction not to run the full acquisition until pagination is independently validated: I'd suggest one more real-world check before committing to the full 11-symbol run — either (a) a single live pull for one symbol (e.g. BTC_USDT) across the full 720-day window, checked for row count / gaps / duplicates / correct start-end before running all 11, or (b) going straight to the full run if you're comfortable with the evidence above. I did **not** run either of these — the full 720-day/11-symbol acquisition was explicitly out of scope for this task, and I'm stopping here per your instruction.

## 6. Git

No commit, no push, no other git operation was performed.

---

**Files changed on your machine:** `mexc_bot\core\mexc_native.py` only (verified via before/after directory listing — every other file's size and modified time is unchanged).
