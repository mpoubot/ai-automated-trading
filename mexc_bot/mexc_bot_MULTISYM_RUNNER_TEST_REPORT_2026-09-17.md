# Multi-Symbol Runner End-to-End Test — Pre-Full-Download Report

**Date:** 2026-09-17
**Status:** End-to-end test complete. **Full 11-symbol / 720-day download NOT started** — waiting for your go-ahead. No strategy files touched, no backtests/experiments run, no parameter changes, no Alpaca, no live trading, no git commit/push.

---

## What was tested

`fetch_historical_native.py`'s actual, unmodified `run()` function, against 4 symbols from `DEFAULT_SYMBOLS` — **BTC_USDT, ETH_USDT, SUI_USDT, DOGE_USDT** (BTC already verified solo in the prior pilot; ETH/SUI/DOGE added here for cross-symbol coverage: a major, a mid-cap, and a sub-cent-priced pair, to stress different price magnitudes) — over a 24-hour window.

**No shell tool was available on your Windows machine again this session** (same gap flagged in the last report — a `device_bash` tool briefly appears then reports "device not connected"), so the runner couldn't be executed directly on your machine. Instead: real kline (Min60, ~30h window) and funding-history (`page_num=1, page_size=5`) responses were fetched live from `contract.mexc.com` via the browser pane on your machine for all 4 symbols, saved verbatim under `data/native/raw/**/multisym_test_2026-09-17*.json`, and then **the actual runner script and the actual adapter code** were run against those captured responses (only the HTTP transport function, `core.mexc_native._get`, was substituted to replay the saved files instead of making a live call — nothing about `fetch_historical_native.run()` or the pagination/parsing logic in `core/mexc_native.py` was mocked or bypassed). Test harness: `tests/test_runner_multisym.py`.

## Results — all checks passed, all 4 symbols

| Symbol | OHLCV rows | Range | Funding rows | All checks |
|---|---|---|---|---|
| BTC_USDT | 24 | 2026-09-16 09:00 → 2026-09-17 08:00 | 2 | PASS |
| ETH_USDT | 24 | 2026-09-16 09:00 → 2026-09-17 08:00 | 3 | PASS |
| SUI_USDT | 24 | 2026-09-16 09:00 → 2026-09-17 08:00 | 3 | PASS |
| DOGE_USDT | 24 | 2026-09-16 09:00 → 2026-09-17 08:00 | 3 | PASS |

Per your checklist, verified for **every** symbol (not just BTC):

- **Symbol mapping** — `BTC/USDT:USDT → BTC_USDT`, `ETH/USDT:USDT → ETH_USDT`, `SUI/USDT:USDT → SUI_USDT`, `DOGE/USDT:USDT → DOGE_USDT`. Correct in all 4.
- **OHLCV row count** — 24 rows each, exactly matching a 24-hour window at `Min60`.
- **Chronological ordering** — `timestamp.is_monotonic_increasing` true for all 4.
- **1h continuity** — every consecutive gap is exactly 1 hour, no exceptions, in all 4.
- **Duplicates** — zero duplicate timestamps in OHLCV or funding, all 4 symbols.
- **Nulls** — zero nulls across `open/high/low/close/volume`, all 4.
- **Funding settlements** — present and non-empty for all 4 (2-3 rows depending on how the 24h window intersected each symbol's 8h settlement grid).
- **Funding interval** — `collect_cycle_hours == 8` on every row, all 4 (SUI and DOGE confirmed this independently of BTC/ETH, which the prior report already covered).
- **Timestamp compatibility** — the exact `backtester.py:78` comparison (`funding_df[funding_df["timestamp"] <= ts]`) was reproduced against each symbol's own OHLCV/funding pair and succeeded without a `TypeError` in all 4 — the tz-naive fix from the last checkpoint holds across symbols, not just BTC.

Extra value from the broader symbol set: ETH's fixture included a **negative funding rate** (`-0.000037`, i.e. shorts paid), and SUI/DOGE cover sub-$1 and sub-cent price magnitudes respectively — none of this surfaced any parsing or precision issue.

## Runner output structure — confirmed correct

The runner wrote exactly the structure proposed in the earlier checkpoint doc, to a scratch test root (`data/native_pilot_multisym_test/`, kept separate from where the real 720-day run will land):

```
ohlcv/{SYMBOL}_1h.parquet        — present for all 4, correct schema
funding/{SYMBOL}_funding.parquet — present for all 4, correct schema
raw/ohlcv/{SYMBOL}/*.json        — present for all 4 (raw kline page dumped)
raw/funding/{SYMBOL}/*.json      — present for all 4 (raw funding page dumped)
manifest.json                    — written, and its in-memory and on-disk
                                    copies verified byte-identical
```

`manifest.json` correctly records per-symbol row counts and actual date ranges covered, and the top-level `assumptions_in_force` block. That block was also **updated** while writing this report — it previously said the funding page-size cap was "unresolved (docs say 1000, ccxt comment says 100)"; the earlier BTC-solo pilot already resolved this live (1000 confirmed), so the manifest text now says that plainly instead of restating stale uncertainty.

## One thing worth knowing (test-harness limitation, not an adapter bug)

The first run of this test used a 48-hour window and appeared to hang — it wasn't hung, it was stuck re-walking the same 5-row funding fixture page 300+ times. Cause: the captured funding fixtures are single pages (5 real settlements, ~40h of real depth), and the test's fake network handler always replays that *same* page regardless of which `page_num` the runner requests. `fetch_funding_history_native` correctly stops once the oldest row on a page is older than the requested cutoff — but a 48h cutoff reaches slightly past what a 40h-deep fixture can ever show the mock, so the loop fell back to its other stop condition (`page_num >= totalPage`, which is 324) and dutifully — if pointlessly, against a mock that never changes — walked toward it. This is purely an artifact of testing with a shallow captured fixture against a deeper cutoff; **it is not a bug in the adapter**, since against the real API every page returns genuinely older data and the loop converges normally — which is exactly what the BTC-solo pilot already demonstrated with a live `page_size=1000` call. Fixed for this test by using a 24h window instead (within the fixtures' real depth). Noted here rather than silently reduced without explanation.

## What's still open

1. **No reliable shell on your Windows machine** — unchanged from the last report. `device_bash` still isn't consistently available in this session. The full 11-symbol/720-day run needs either that to come up, or you running `fetch_historical_native.py` yourself locally.
2. **This test used 24h and 4 of the 11 symbols** — a genuine end-to-end exercise of the runner's logic and file-writing, not a substitute for the real 720-day/11-symbol pull, which will hit real multi-page pagination (both kline, ~9 pages per symbol at 2000-row batches, and funding, however many pages 720 days actually requires) that this small test didn't exercise at scale.
3. **BTC/ETH/SUI/DOGE only** — the other 7 pairs (BNB, XRP, ADA, AVAX, DOT, TIA, COTI) are still unverified, though nothing about them is expected to differ structurally.

Nothing else changed. No git operations were performed.
