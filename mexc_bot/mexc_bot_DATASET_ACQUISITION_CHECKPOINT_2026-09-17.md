# Dataset Acquisition Checkpoint Report

**Acquisition timestamp (UTC):** 2026-09-17T20:25:50Z
**Status:** Full 11-symbol / 720-day native acquisition complete and validated. **Stopping here per your instruction** — no backtesting/research experiments have been started. Waiting for your review before proceeding further.

---

## Method note (read this first)

There is still no shell (`device_bash`) tool available on your Windows machine in this session — the same gap flagged in every prior report in this project. `fetch_historical_native.py` was **not executed directly**. Instead, exactly as in the BTC 720-day validation you approved, the fixed backward-cursor pagination algorithm in `core/mexc_native.py::fetch_ohlcv_range_native()` was replicated line-for-line in a JavaScript fetch loop, executed in the browser pane running on your machine, against the **real, live** `contract.mexc.com` API. This is genuine network traffic against the real endpoint for all 11 symbols — not a simulation and not synthetic data.

Two method choices this required, disclosed transparently:

1. **Transfer-size workaround.** The bridge that returns browser JS results to this sandbox truncates any single result over ~256KB. Each symbol's 17,280-row OHLCV series was extracted by compressing the 5 numeric fields (gzip + base64), splitting into 2–3 chunks, and reassembling them here. This worked reliably and was verified end-to-end (see Validation below).
2. **Scope reduction — `amount` and `real_*` fields dropped.** The native kline response's `amount` field and four `real_*` fields (`real_open/high/low/close`) — which `core/mexc_native.py`'s own docstring says are kept only "for parity/audit, not consumed by the strategy" — were **not** carried through this acquisition, because including them pushed every transfer over the size ceiling even in small batches. They exist as NaN columns in the output parquet for schema compatibility, but contain no data this round. This is an environment-constraint limitation, not something I decided to skip for convenience — happy to re-run just those fields through a slower per-page method if you want them.

Two mid-acquisition manual-transcription errors were caught and fixed before finalizing: ETH_USDT's and SUI_USDT's funding data were initially typed by hand into a file and silently corrupted (one field truncated from 1618 to ~1058 elements). Both were re-fetched and re-extracted through the reliable file-based path, and **every symbol's funding data was independently re-verified for internal length consistency** before the parquet build ran (see Validation).

---

## Data hygiene

Your instruction was explicit: do not mix old and new raw dumps, and do not delete anything without approval. The old dataset at `data\native\` (the pre-fix run, every symbol showing the ~2000-row truncation bug) has **not been touched**. The new acquisition was delivered as a separate zip, `data\native_v2_full720day_20260917.zip` (8,921,296 bytes, confirmed landed on your machine at that exact size), sitting **alongside** the old `data\native\` directory. Nothing was overwritten or deleted. Please extract it into a folder of your choosing (e.g. `data\native_v2_full720day_20260917\`) when ready — I could not do this step for you since there is no shell tool on your machine in this session.

The zip contains:
```
manifest.json
manifest_sha256.txt
ohlcv/<SYMBOL>_1h.parquet          (11 files)
funding/<SYMBOL>_funding.parquet   (11 files)
raw/ohlcv/<SYMBOL>/fields_open_high_low_close_vol.json    (this acquisition's raw field data, 11 files)
raw/funding/<SYMBOL>/fields_cycle_rate_settle.json        (this acquisition's raw field data, 11 files)
```

---

## Symbols, timeframe, requested range

- **Symbols (11, native format):** BTC_USDT, ETH_USDT, BNB_USDT, XRP_USDT, ADA_USDT, DOGE_USDT, AVAX_USDT, DOT_USDT, SUI_USDT, TIA_USDT, COTI_USDT
- **Timeframe:** 1h (native `Min60`)
- **Requested range:** 2024-09-27T19:40:44Z → 2026-09-17T19:40:44Z (720 days, computed once at acquisition start and reused identically for every symbol, matching production's single-`now`-snapshot behavior)
- **Source:** Native MEXC Futures/Perpetual Swap API only (`contract.mexc.com`) — no Alpaca, no CCXT

---

## Per-symbol results

| Symbol | OHLCV rows | Pages | OHLCV range (UTC) | Funding rows | Pages | Funding range (UTC) | Dup | Gaps | Status |
|---|---:|---:|---|---:|---:|---|---:|---:|---|
| BTC_USDT | 17,280 | 9 | 2024-09-27 20:00 → 2026-09-17 19:00 | 1,618 | 2 | 2025-03-27 16:00 → 2026-09-17 16:00 | 0 | 0 | **PASS** |
| ETH_USDT | 17,280 | 9 | 2024-09-27 20:00 → 2026-09-17 19:00 | 1,618 | 2 | 2025-03-27 16:00 → 2026-09-17 16:00 | 0 | 0 | **PASS** |
| BNB_USDT | 17,280 | 9 | 2024-09-27 20:00 → 2026-09-17 19:00 | 1,618 | 2 | 2025-03-27 16:00 → 2026-09-17 16:00 | 0 | 0 | **PASS** |
| XRP_USDT | 17,280 | 9 | 2024-09-27 20:00 → 2026-09-17 19:00 | 1,618 | 2 | 2025-03-27 16:00 → 2026-09-17 16:00 | 0 | 0 | **PASS** |
| ADA_USDT | 17,280 | 9 | 2024-09-27 20:00 → 2026-09-17 19:00 | 1,618 | 2 | 2025-03-27 16:00 → 2026-09-17 16:00 | 0 | 0 | **PASS** |
| DOGE_USDT | 17,280 | 9 | 2024-09-27 20:00 → 2026-09-17 19:00 | 1,618 | 2 | 2025-03-27 16:00 → 2026-09-17 16:00 | 0 | 0 | **PASS** |
| AVAX_USDT | 17,280 | 9 | 2024-09-27 20:00 → 2026-09-17 19:00 | 1,618 | 2 | 2025-03-27 16:00 → 2026-09-17 16:00 | 0 | 0 | **PASS** |
| DOT_USDT | 17,280 | 9 | 2024-09-27 20:00 → 2026-09-17 19:00 | 1,618 | 2 | 2025-03-27 16:00 → 2026-09-17 16:00 | 0 | 0 | **PASS** |
| SUI_USDT | 17,280 | 9 | 2024-09-27 20:00 → 2026-09-17 19:00 | 1,618 | 2 | 2025-03-27 16:00 → 2026-09-17 16:00 | 0 | 0 | **PASS** |
| TIA_USDT | 17,280 | 9 | 2024-09-27 20:00 → 2026-09-17 19:00 | 3,236 | 4 | 2025-03-27 16:00 → 2026-09-17 20:00 | 0 | 0 | **PASS** |
| COTI_USDT | 17,280 | 9 | 2024-09-27 20:00 → 2026-09-17 19:00 | 2,401 | 3 | 2025-03-27 16:00 → 2026-09-17 20:00 | 0 | 0 | **PASS** |

Every symbol independently satisfies all nine checklist items you specified: row count ≈17,280, chronological order, 0 duplicate timestamps, 0 hourly gaps, 0 NaN, 0 infinite values, valid OHLC relationships (`high >= low` everywhere), non-negative volume everywhere, and full coverage of the requested 720-day period. This was verified twice — once during the build, and once again independently by re-reading the finished parquet files fresh with pandas (not reusing in-memory state from the build step).

**Page counts:** all 11 symbols needed exactly 9 OHLCV pages (8×2000 + 1×1280 rows), consistent with the BTC 720-day pilot you already reviewed.

---

## Warnings and anomalies

1. **Funding-history retention is shallower than kline history — expected, not a bug.** 8 of 11 symbols (BTC, ETH, BNB, XRP, ADA, DOGE, AVAX, DOT, SUI) return exactly 1,618 funding rows starting 2025-03-27, not reaching back to the requested 2024-09-27 start. This matches the pre-fix manifest's finding for BTC and the earlier BTC pilot report — MEXC's funding endpoint simply doesn't retain history as far back as its kline endpoint, regardless of what cutoff is requested.
2. **TIA_USDT and COTI_USDT have different funding row counts than the other 9 symbols.** TIA returns 3,236 rows (exactly double 1,618 — consistent with a 4-hour funding `collectCycle` versus the 8-hour cycle on the other symbols) and COTI returns 2,401 rows (a non-uniform/mixed cycle). Both are genuine live API responses, not extraction errors — the `collect_cycle_hours` column in each funding parquet records this per-row.
3. **Funding data's last timestamp for TIA/COTI is slightly past the requested end** (2026-09-17 20:00 vs. requested 19:40:44). The funding fetch trims only by a lower cutoff, not an upper bound, so it naturally includes whatever the most recent settled funding row is "as of now" — a few symbols' most recent row happened to fall a little later than the fixed request window. This is expected, not a defect.
4. **`amount` / `real_open` / `real_high` / `real_low` / `real_close` columns are present but empty (NaN)** in every OHLCV parquet file — see the scope-reduction note above. The 5 fields your strategy actually consumes (open, high, low, close, volume) are fully populated and validated.
5. **Two symbols' funding data were corrupted-then-fixed mid-acquisition** (ETH_USDT, SUI_USDT — see Method note). Flagging this so it's on record even though the final delivered files are confirmed correct by the independent re-verification pass.

---

## SHA-256 hashes

Full manifest with per-file hashes is in `manifest.json` inside the zip; `manifest_sha256.txt` gives the hash of the manifest itself. Summary:

| File | SHA-256 |
|---|---|
| BTC_USDT_1h.parquet | `4470ab2b6892b1759a0d6c28c58e74784755833d2a48067de3db80424085c9c5` |
| BTC_USDT_funding.parquet | `ad091b3cb6be0a4960c937e9f3c07d1b62f33beea6f939f2a2404039f396fba1` |
| ETH_USDT_1h.parquet | `60738c05e9cf68026a2264b30727f3ab939a996f252106e21356d956c3f4dda6` |
| ETH_USDT_funding.parquet | `cc416bbb97fd940245be3e7116299d1daaa0cc90d8e6d63a705677139e74195e` |
| BNB_USDT_1h.parquet | `3ac35ce0155c54d78ceb48737f9d1bc72042765869d6cf91fe68539fee8c0463` |
| BNB_USDT_funding.parquet | `806f328c57ab4e773c7dfafc8e2eeb721ce75bde4c5169ac6bbbc4f166445562` |
| XRP_USDT_1h.parquet | `6aa02e807bafe3b6cee6420b55bd710e8e31aaee083709bc37adace8137f8ed2` |
| XRP_USDT_funding.parquet | `17a9da8a18dfad59a3500c01df941a5645f0c2ce99f9af2696db7827e7f3853b` |
| ADA_USDT_1h.parquet | `5c2b63736f30c4dac082f5d217b96c795a316d1583df4ca31257375e3e1a1d2e` |
| ADA_USDT_funding.parquet | `a2fa24a46eb94e2ed43332f0b8a87adf11c33a5f735439f2946485a82d64334d` |
| DOGE_USDT_1h.parquet | `1448b3ac4b36094c29d7b61f9faaf6023d62a7345b9e7028d77fa986836a829d` |
| DOGE_USDT_funding.parquet | `ea37397bfcb84ec20cbb6dba60da0dd1c66f852266f9730360703c64ae38f62f` |
| AVAX_USDT_1h.parquet | `302980afaf495a0d6f03d32bb7f1721b61ade6f592bfadd523d425c55e41e83d` |
| AVAX_USDT_funding.parquet | `2d33ccd77131dd731737d6341d91e6354a5c27508afd76092bb2431a65ff2456` |
| DOT_USDT_1h.parquet | `6288c751a2d07d23e4bf169582633182591c8ebdbf11e6d108c0f2e9a6678164` |
| DOT_USDT_funding.parquet | `a2fdbcf451c9636a17cd688320b9cd3ac6941bd20b8c26c34f8d6abec1ef997a` |
| SUI_USDT_1h.parquet | `1385835dddcea35ad23d9dafa7ba0a53f0e25802a2a373969059ffce1aeb1811` |
| SUI_USDT_funding.parquet | `db940b32e0279f70be90cd6fa2759eec1cbb7dec59bf335258008577b4fbd73e` |
| TIA_USDT_1h.parquet | `12b2988557ef2c77e0474ea9bb20fe11d46adc191f2b813c79b369cc685fafc0` |
| TIA_USDT_funding.parquet | `72aee8d61afdd094fda4762eb851d381e5610d90cedb4bd7775d0e02974ed204` |
| COTI_USDT_1h.parquet | `7975e612b134aa2c7cf81ab188352ef5383bbee37ec49b5ab72c9b4f3ad2b15c` |
| COTI_USDT_funding.parquet | `6cdb06358cdc2454ed565951671cb9ccf3575560fa515fdab9e52d7981e74092` |
| manifest.json | `3522da1b772a020ddd53c9a93025ab2ebe26dd1d4efc356599c8f9a590d97156` |
| **native_v2_full720day_20260917.zip** (full package) | `dba578a03f7adad7fe5c0af13d19020b8d6a199c0b367370dd7346f47d3bfeba` |

---

## Ready / not-ready statement

**This dataset is READY for the next research phase**, with one caveat you should weigh: the `amount`/`real_*` columns are empty due to the transfer-size scope reduction described above. If your research plans don't need those columns (your own adapter docstring says they're not consumed by the strategy), the dataset is complete and clean as delivered. If you do want them, tell me and I'll re-run just those fields through a slower, smaller-batch extraction and patch the existing parquet files rather than re-running the whole acquisition.

No strategy, backtester, config, indicators, risk-manager, or data_fetcher files were touched. Nothing was committed or pushed. No backtesting or research experiments have been started — waiting for your review and go-ahead before continuing.
