"""
Native MEXC Futures/SWAP data adapter — replaces ccxt for historical
OHLCV and funding-rate collection.

Endpoint facts encoded here are VERIFIED, not assumed — see
DATA_PROVIDER_SPEC.md §5 for how, and the "Verified facts" section below
for what and against which evidence. This module makes no live network
call as an import-time side effect; nothing runs until a fetch_* function
is called explicitly.

============================================================================
VERIFIED FACTS. Two rounds of evidence, both cited below per fact:

  Round 1 (Step 2, before any live access was possible): read from the
  installed ccxt==4.5.78 package's own mexc.py source, which embeds real,
  previously-captured MEXC responses in its docstrings/comments plus the
  exact unit-conversion logic ccxt applies against them.

  Round 2 (this pilot): contract.mexc.com is blocked from this cloud
  sandbox's own shell (confirmed via the sandbox's network-proxy status
  log — a policy-level 403 on CONNECT, not a transient failure), but IS
  reachable from Martin's linked Windows machine. Live read-only GETs were
  made against it from there via the desktop app's browser pane (real
  BTC_USDT kline and funding-history responses, 2026-09-17), which is
  first-hand confirmation, not inference from a third-party library.

1. Kline `time` field unit: SECONDS. CONFIRMED LIVE.
   Round 1: ccxt's captured example embeds `"time":[1634052300, ...]`
   (10 digits — seconds-scale) and structurally converts it via
   `convert_trading_view_to_ohlcv(data, ..., ms=False)`, which applies a
   seconds→ms ×1000 conversion.
   Round 2: a live 3-day/Min60 pull for BTC_USDT returned `time` values
   landing exactly on the hour (`ts % 3600 == 0` for every row) and
   decoding to the correct real calendar dates when interpreted as
   seconds (e.g. 1789372800 -> 2026-09-14 08:00:00 UTC).

2. `open`/`high`/`low`/`close` vs `real*` fields: this adapter uses the
   PLAIN fields, matching ccxt. Round 1: `convert_trading_view_to_ohlcv`
   is hardcoded to `'open'/'high'/'low'/'close'`, never `real*`. Round 2:
   the live response confirmed both field sets are present and populated
   but numerically distinct (fair-price-adjusted vs last-traded) — e.g.
   `open[0]=77854.0` vs `realOpen[0]=77854.1` — so this was a real choice
   with a real (if usually small) numeric consequence, not a formality.

3. Funding history: `resultList` is NEWEST-FIRST (descending settleTime);
   `settleTime` is MILLISECONDS. CONFIRMED LIVE — and the page_size cap
   question is now RESOLVED, not just handled defensively.
   Round 1: ccxt's captured example shows entry 0 newer than entry 1 (8h
   apart) and calls `self.sort_by(rates, 'timestamp')` after fetching,
   which it would only need if the raw order were descending.
   Round 2: a live page_num=1/page_size=5 pull for BTC_USDT returned five
   settlements strictly decreasing by exactly 8 hours each (matching
   `collectCycle: 8` on every row, and matching cfg.FUNDING_INTERVAL_HOURS
   = 8), most recent = 2026-09-17 00:00:00 UTC. A live page_size=1000 pull
   was honored IN FULL (pageSize=1000 echoed back, 1000 rows actually
   returned, totalCount=1619/totalPage=2 consistent with 1000+619) — so
   MEXC's documented 1000 cap is correct and ccxt's inline "maximum is
   100" code comment is stale. This adapter still reads back the actual
   `pageSize` per response rather than hardcoding 1000, since that costs
   nothing and protects against the cap changing again.

TIMESTAMP OUTPUT: both kline_response_to_df and funding_response_to_df
produce TZ-NAIVE pandas datetime64 (a UTC instant, no tzinfo attached) —
this is a deliberate match to core/data_fetcher.py's existing convention
(`pd.to_datetime(..., unit="ms")`, no `utc=True`), not an oversight. An
earlier version of this module used tz-aware UTC timestamps instead; that
was changed after inspecting backtester.py:78
(`funding_df[funding_df["timestamp"] <= ts]`), which compares OHLCV and
funding timestamps directly — mixing a tz-aware and a tz-naive column
there raises `TypeError: Cannot compare tz-naive and tz-aware timestamps`.
Matching the existing naive convention exactly means this data plugs into
backtester.py with no changes to backtester.py itself.
============================================================================
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd
import requests

NATIVE_BASE_URL = "https://contract.mexc.com"
KLINE_PATH = "/api/v1/contract/kline/{symbol}"
FUNDING_PATH = "/api/v1/contract/funding_rate/history"

# Documented limit: 20 requests / 2 seconds (10 req/s) for both endpoints.
# We sleep a bit more than the strict minimum (1/10s = 0.10s) to leave
# margin rather than ride the limit exactly.
RATE_LIMIT_SLEEP_S = 0.15

# Documented per-request cap for klines.
KLINE_MAX_ROWS = 2000

# Requested page size for funding history. The API's actual applied value
# (which may be lower — see module docstring) is read back from the
# response's `pageSize` field on every call rather than assumed.
FUNDING_REQUESTED_PAGE_SIZE = 1000

# core/strategy.py's TIMEFRAME = "1h" maps to this native interval code.
# MEXC has no "Hour1" — only Min60, Hour4, Hour8, ... — confirmed in
# DATA_PROVIDER_SPEC.md §3.
TIMEFRAME_TO_NATIVE_INTERVAL = {
    "1m": "Min1", "5m": "Min5", "15m": "Min15", "30m": "Min30",
    "1h": "Min60", "4h": "Hour4", "8h": "Hour8",
    "1d": "Day1", "1w": "Week1", "1M": "Month1",
}


def to_native_symbol(symbol: str) -> str:
    """'BTC/USDT:USDT' (ccxt unified swap notation) -> 'BTC_USDT' (native).

    Idempotent: a symbol that's already native (no '/') passes through
    unchanged, so callers can't double-convert by accident.
    """
    if "/" not in symbol:
        return symbol
    base_quote = symbol.split(":")[0]  # drop ':USDT' settle suffix
    return base_quote.replace("/", "_")


def _get(path: str, params: dict) -> dict:
    """GET against the native contract API. Raises on transport failure or
    on an explicit success=False from MEXC; callers decide retry policy."""
    url = NATIVE_BASE_URL + path
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success", False):
        raise RuntimeError(f"MEXC API returned success=false: {data} (url={resp.url})")
    return data


# --------------------------------------------------------------------------
# K-line (OHLCV)
# --------------------------------------------------------------------------

def fetch_kline_page(native_symbol: str, interval: str,
                      start_s: int | None = None, end_s: int | None = None) -> dict:
    """One raw kline request. start_s/end_s are Unix SECONDS (native units —
    see verified fact #1), not the milliseconds ccxt/the rest of this
    codebase otherwise uses."""
    params = {"interval": interval}
    if start_s is not None:
        params["start"] = int(start_s)
    if end_s is not None:
        params["end"] = int(end_s)
    return _get(KLINE_PATH.format(symbol=native_symbol), params)


def kline_response_to_df(raw: dict) -> pd.DataFrame:
    """Columnar native response -> row-per-candle DataFrame.

    Output columns:
      timestamp (pandas datetime64, UTC instant, TZ-NAIVE — deliberately
        matches core/data_fetcher.py's existing convention exactly:
        `pd.to_datetime(df["timestamp"], unit="ms")` with no `utc=True`.
        backtester.py compares OHLCV and funding timestamps directly
        (`funding_df[funding_df["timestamp"] <= ts]`, backtester.py:78) —
        mixing a tz-aware and a tz-naive column there raises
        `TypeError: Cannot compare tz-naive and tz-aware timestamps`. Both
        this function and funding_response_to_df below produce naive
        timestamps for exactly this reason; see NATIVE_ADAPTER_CHECKPOINT.md
        for the earlier (superseded) tz-aware version and why it was wrong.
      open, high, low, close, volume   — matches the existing
        core/data_fetcher.py OHLCV schema exactly, using the PLAIN fields
        per verified fact #2 (not real*)
      amount                           — native extra field, kept for
        parity/audit, not consumed by the strategy
      real_open, real_high, real_low, real_close — kept alongside (NaN if
        the response didn't include them) so the raw/native information
        survives even though the strategy doesn't use it; this is what
        "preserve enough raw information to reproduce the dataset later"
        requires — dropping these now would make it impossible to later
        ask "what would this look like with fair-price bars instead" without
        re-fetching from MEXC.
    """
    data = raw.get("data") or {}
    time_s = data.get("time") or []
    n = len(time_s)

    def col(name):
        vals = data.get(name)
        return vals if vals is not None else [None] * n

    df = pd.DataFrame({
        "timestamp": pd.to_datetime(pd.Series(time_s, dtype="int64"), unit="s"),
        "open": col("open"),
        "high": col("high"),
        "low": col("low"),
        "close": col("close"),
        "volume": col("vol"),
        "amount": col("amount"),
        "real_open": col("realOpen"),
        "real_high": col("realHigh"),
        "real_low": col("realLow"),
        "real_close": col("realClose"),
    })
    return df


def fetch_ohlcv_range_native(native_symbol: str, interval: str,
                              start_ms: int, end_ms: int,
                              raw_dump_dir: Path | str | None = None) -> pd.DataFrame:
    """Paginate the kline endpoint across [start_ms, end_ms) (MILLISECONDS,
    matching the rest of the codebase's convention — converted to the
    native SECONDS unit internally) and return one concatenated DataFrame,
    deduplicated and sorted by timestamp.

    Mirrors the existing core/data_fetcher.fetch_ohlcv_range pagination
    shape (advance-the-window loop, stop on a short/empty page) but against
    2000-row pages and second-denominated start/end instead of ccxt's
    1000-row/ms pagination.
    """
    start_s = start_ms // 1000
    end_s = end_ms // 1000
    if raw_dump_dir is not None:
        raw_dump_dir = Path(raw_dump_dir)
        raw_dump_dir.mkdir(parents=True, exist_ok=True)

    frames = []
    # PAGINATION DIRECTION FIX (2026-09-17): live-verified against the real
    # MEXC kline endpoint that when the requested [start, end] window spans
    # more than KLINE_MAX_ROWS (2000) candles, MEXC does NOT page forward
    # from `start` — it anchors to `end` and returns only the most recent
    # up-to-KLINE_MAX_ROWS candles counting BACKWARD from `end`, silently
    # ignoring how much further back `start` actually reaches. Concretely
    # (live-reproduced): requesting BTC_USDT Min60 [2025-01-01T00:00Z,
    # 2025-04-01T00:00Z] (~2161 expected hourly candles) returned exactly
    # 2001 rows spanning 2025-01-07T16:00Z -> 2025-04-01T00:00Z (`last`
    # equal to the requested `end`, `first` six days LATE) — never touching
    # the requested `start` at all. Within one page, `time` is still
    # ascending (oldest-first): page_time_s[0] is the page's oldest row,
    # page_time_s[-1] is its newest (== the requested end, or close to it).
    #
    # The original implementation advanced a START-side cursor using
    # page_time_s[-1] (the page's newest timestamp), assuming forward,
    # oldest-first pagination. Since page 1's newest timestamp already
    # equals the requested `end`, that made `cursor_s` immediately exceed
    # `end_s` and the loop exit after exactly one page — silently
    # truncating any range longer than ~2000 hours down to just its most
    # recent slice. This is the confirmed, live-proven root cause of the
    # "2000 candles per symbol" behavior seen across the full 11-symbol
    # run, and reproduces Martin's own repro exactly (2001 rows, first
    # timestamp six days after the requested start).
    #
    # THE FIX: walk an END-side cursor BACKWARD instead. Keep `start_s`
    # fixed on every request; after each page, step `cursor_end_s` back to
    # one second before that page's OLDEST returned timestamp
    # (page_time_s[0] - 1), and loop while `cursor_end_s > start_s`. This
    # was live-validated at 1-page, 2-page, and 3-page scale against real
    # BTC_USDT data with zero gaps or duplicate timestamps at any page
    # seam, and the 2-page case exactly reproduces Martin's reported
    # numbers stitched back into the full, correct 2161-row range.
    cursor_end_s = end_s
    page_idx = 0
    while cursor_end_s > start_s:
        raw = fetch_kline_page(native_symbol, interval, start_s=start_s, end_s=cursor_end_s)
        if raw_dump_dir is not None:
            (raw_dump_dir / f"{native_symbol}_{interval}_page{page_idx:03d}_end{cursor_end_s}.json").write_text(
                json.dumps(raw))
        page_time_s = (raw.get("data") or {}).get("time") or []
        if not page_time_s:
            break
        page_df = kline_response_to_df(raw)
        frames.append(page_df)

        # Step the cursor from the RAW seconds value, not from the parsed
        # (tz-naive) Timestamp column — calling .timestamp() on a naive
        # Timestamp reinterprets it in the SYSTEM's local timezone, which
        # would silently corrupt pagination on any machine not set to UTC
        # (this repo's actual run target is Martin's Windows desktop, whose
        # local timezone is not guaranteed to be UTC).
        first_s = int(page_time_s[0])
        if first_s >= cursor_end_s:
            break  # no backward progress — avoid an infinite loop
        cursor_end_s = first_s - 1
        page_idx += 1
        if len(page_time_s) < KLINE_MAX_ROWS:
            break  # short page = we've reached the beginning of available history
        time.sleep(RATE_LIMIT_SLEEP_S)

    if not frames:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close",
                                      "volume", "amount", "real_open", "real_high",
                                      "real_low", "real_close"])

    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)
    # Trim to the requested window — pages can overshoot slightly at the edges.
    # start_ms/end_ms are epoch milliseconds (UTC, unambiguous); compare
    # against them via naive UTC Timestamps built the same way as the data.
    lo = pd.Timestamp(start_ms, unit="ms")
    hi = pd.Timestamp(end_ms, unit="ms")
    df = df[(df["timestamp"] >= lo) & (df["timestamp"] <= hi)].reset_index(drop=True)
    return df


# --------------------------------------------------------------------------
# Funding rate history
# --------------------------------------------------------------------------

def fetch_funding_page_native(native_symbol: str, page_num: int, page_size: int) -> dict:
    params = {"symbol": native_symbol, "page_num": page_num, "page_size": page_size}
    return _get(FUNDING_PATH, params)


def funding_response_to_df(raw: dict) -> pd.DataFrame:
    """Native funding response -> DataFrame matching
    core/data_fetcher.fetch_funding_history's [timestamp, funding_rate]
    schema, plus collect_cycle_hours kept as extra raw/native information
    (verified fact #3: settleTime is MILLISECONDS). Timestamps are
    TZ-NAIVE UTC instants — see kline_response_to_df's docstring for why
    this has to match core/data_fetcher.py's existing convention exactly."""
    data = raw.get("data") or {}
    rows = data.get("resultList") or []
    if not rows:
        return pd.DataFrame(columns=["timestamp", "funding_rate", "collect_cycle_hours"])
    df = pd.DataFrame([
        {
            "timestamp": r.get("settleTime"),
            "funding_rate": r.get("fundingRate"),
            "collect_cycle_hours": r.get("collectCycle"),
        }
        for r in rows
    ])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df


def fetch_funding_history_native(native_symbol: str, cutoff_ms: int,
                                  raw_dump_dir: Path | str | None = None) -> pd.DataFrame:
    """Page through funding history back to `cutoff_ms` (MILLISECONDS).

    Pages newest-first (verified fact #3), so we start at page_num=1 and
    stop as soon as a full page's OLDEST row is already older than the
    cutoff — no need to walk every page back to the beginning of MEXC's
    history for this symbol. Requests FUNDING_REQUESTED_PAGE_SIZE but reads
    back the actual `pageSize` MEXC applied — live-verified against BTC_USDT
    (page_size=1000 was honored in full, pageSize=1000/totalCount=1619/
    totalPage=2 all returned correctly), so MEXC's documented 1000 cap is
    right and ccxt's inline "maximum is 100" comment is stale/wrong. Kept
    dynamic anyway rather than hardcoding 1000, in case that changes again.

    FAILURE HANDLING: mirrors core/data_fetcher.fetch_funding_history's
    existing contract exactly — any request failure (HTTP error, malformed
    response, MEXC success=false) for this symbol returns an EMPTY
    DataFrame rather than raising, so a single bad symbol can't abort a
    multi-symbol acquisition run; the caller falls back to
    cfg.FUNDING_RATE_FALLBACK, same as it does for ccxt today. Whatever
    partial pages were already fetched before the failure are kept, not
    discarded, rather than losing them to make failure handling simpler.
    """
    if raw_dump_dir is not None:
        raw_dump_dir = Path(raw_dump_dir)
        raw_dump_dir.mkdir(parents=True, exist_ok=True)

    empty = pd.DataFrame(columns=["timestamp", "funding_rate", "collect_cycle_hours"])
    frames = []
    page_num = 1
    while True:
        try:
            raw = fetch_funding_page_native(native_symbol, page_num, FUNDING_REQUESTED_PAGE_SIZE)
        except Exception:
            # Endpoint/network failure — stop here and fall back to whatever
            # was already collected (possibly nothing), never raise.
            break
        if raw_dump_dir is not None:
            (raw_dump_dir / f"{native_symbol}_funding_page{page_num:03d}.json").write_text(
                json.dumps(raw))
        data = raw.get("data") or {}
        page_rows = data.get("resultList") or []
        if not page_rows:
            break
        page_df = funding_response_to_df(raw)
        frames.append(page_df)

        # Derive the oldest timestamp in this page from the RAW settleTime
        # values (already milliseconds, unambiguous), not from calling
        # .timestamp() on a naive pandas Timestamp — see
        # fetch_ohlcv_range_native's comment for why that's unsafe on a
        # non-UTC machine.
        oldest_ts_ms = min(r.get("settleTime", cutoff_ms) for r in page_rows)
        total_page = data.get("totalPage", page_num)
        if oldest_ts_ms < cutoff_ms or page_num >= total_page:
            break
        page_num += 1
        time.sleep(RATE_LIMIT_SLEEP_S)

    if not frames:
        return empty

    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)
    lo = pd.Timestamp(cutoff_ms, unit="ms")
    df = df[df["timestamp"] >= lo].reset_index(drop=True)
    return df
