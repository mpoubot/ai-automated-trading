#!/usr/bin/env python3
"""AURA v0.5.3.21 — Alpaca crypto closed-1H market-data adapter.

Paper-runtime data adapter only. It fetches closed 1H bars for the full
tradable Alpaca crypto universe (discovered live via Alpaca's Assets API)
and writes the canonical CLOSED 1H OHLCV CSV consumed by v0.5.3.12. It
never submits an order and never mutates account state.

REQUIRED_SYMBOLS (BTC/USD, ETH/USD) are the only symbols the v0.5.3
frozen-candidate signal chain (.13 onward) is validated against. A failure
fetching a required symbol is fatal, exactly as before this module supported
more than two symbols. Every other symbol discovered in the live Alpaca
crypto universe is OPTIONAL: it is fetched best-effort for monitoring/data
purposes only, and a fetch failure for an optional symbol is logged and
skipped rather than failing the whole cycle. This means a thinly-traded or
temporarily-unavailable altcoin can never block BTC/ETH signal generation.

The adapter uses Alpaca's documented paginated crypto-bars endpoint directly.
This is deliberate: the API may return fewer bars than requested even when
more data exists, so the adapter must follow next_page_token until the required
warmup is actually satisfied. A single short response must never be mistaken
for a complete historical window.

Credentials are read only from ALPACA_PAPER_API_KEY and
ALPACA_PAPER_SECRET_KEY (or ALPACA_API_KEY / ALPACA_SECRET_KEY as fallback).
A local .env file (if present in the current working directory) is loaded
automatically via python-dotenv; it never overrides a variable already set
in the process environment (e.g. a CI secret).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

# The only symbols the frozen research candidate is validated against. These
# must always be present and always fetch successfully -- a failure here
# remains fatal, unchanged from prior versions of this module.
REQUIRED_SYMBOLS = ["BTC/USD", "ETH/USD"]

DEFAULT_OUTPUT = Path(r"data\prospective_alpaca\alpaca_1h_closed_bars.csv")
BARS = 240
LOOKBACK_DAYS = 14
ALPACA_CRYPTO_BARS_URL = "https://data.alpaca.markets/v1beta3/crypto/us/bars"
ALPACA_TRADING_BASE_URL = "https://paper-api.alpaca.markets"
ALPACA_ASSETS_URL = f"{ALPACA_TRADING_BASE_URL}/v2/assets"
REQUEST_LIMIT = 1000
REQUEST_TIMEOUT_SECONDS = 30

# Going from 2 symbols to a full discovered universe means many more
# sequential HTTP calls. Alpaca rate-limits by HTTP 429; retry those with a
# short backoff rather than letting a transient limit fail the whole cycle.
RATE_LIMIT_MAX_RETRIES = 3
RATE_LIMIT_BACKOFF_SECONDS = 2.0
INTER_SYMBOL_DELAY_SECONDS = 0.25

# Only USD-quoted spot pairs are considered part of the discovered universe.
# This keeps every symbol's price series in a consistent, comparable quote
# currency and matches Alpaca's own crypto paper-trading offering.
_USD_PAIR_RE = re.compile(r"^[A-Z0-9]{2,10}/USD$")


def _get_with_rate_limit_retry(
    url: str, *, headers: dict[str, str], params: dict[str, object]
):
    """GET with a short retry/backoff on HTTP 429 only. All other status
    codes (including other 4xx/5xx) are returned as-is for the caller to
    fail closed on -- this never masks a real error, only a transient rate
    limit."""
    attempt = 0
    while True:
        response = requests.get(
            url, headers=headers, params=params, timeout=REQUEST_TIMEOUT_SECONDS
        )
        if response.status_code != 429 or attempt >= RATE_LIMIT_MAX_RETRIES:
            return response
        attempt += 1
        time.sleep(RATE_LIMIT_BACKOFF_SECONDS * attempt)


def discover_crypto_universe(headers: dict[str, str]) -> list[str]:
    """
    Discover the live, tradable Alpaca crypto universe via the Assets API.

    Returns a sorted, de-duplicated list of USD-quoted symbols. This is a
    read-only lookup -- it never places an order and never mutates account
    state. A failure here is fatal: without a universe list we cannot know
    which optional symbols to attempt, and REQUIRED_SYMBOLS availability
    itself depends on Alpaca's own asset status.
    """
    response = _get_with_rate_limit_retry(
        ALPACA_ASSETS_URL,
        headers=headers,
        params={"asset_class": "crypto", "status": "active"},
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"Alpaca assets HTTP {response.status_code}: {response.text[:500]}"
        )

    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError("Malformed Alpaca assets response: expected a list")

    universe: set[str] = set()
    skipped_non_usd_or_malformed = 0

    for asset in payload:
        if not isinstance(asset, dict):
            continue
        if asset.get("tradable") is not True:
            continue

        raw_symbol = asset.get("symbol")
        if not isinstance(raw_symbol, str) or not raw_symbol:
            skipped_non_usd_or_malformed += 1
            continue

        symbol = raw_symbol.strip().upper()

        # Some Alpaca asset responses use bare symbology (e.g. "BTCUSD")
        # instead of the slash-delimited pair format the bars endpoint
        # expects ("BTC/USD"). Normalize defensively rather than assume.
        if "/" not in symbol and symbol.endswith("USD") and len(symbol) > 3:
            symbol = f"{symbol[:-3]}/USD"

        if _USD_PAIR_RE.match(symbol):
            universe.add(symbol)
        else:
            skipped_non_usd_or_malformed += 1

    if skipped_non_usd_or_malformed:
        print(
            f"NOTE: skipped {skipped_non_usd_or_malformed} non-USD-quoted or "
            "malformed crypto asset entries from the Alpaca universe listing.",
            file=sys.stderr,
        )

    return sorted(universe)


def fetch_closed_bars(
    *,
    symbol: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    required_bars: int,
    headers: dict[str, str],
) -> pd.DataFrame:
    """Fetch at least required closed 1H bars, following API pagination."""
    rows: list[dict] = []
    page_token: str | None = None
    pages = 0

    while True:
        params = {
            "symbols": symbol,
            "timeframe": "1Hour",
            "start": start.isoformat().replace("+00:00", "Z"),
            "end": end.isoformat().replace("+00:00", "Z"),
            "limit": REQUEST_LIMIT,
            "sort": "asc",
        }
        if page_token:
            params["page_token"] = page_token

        response = _get_with_rate_limit_retry(
            ALPACA_CRYPTO_BARS_URL, headers=headers, params=params
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Alpaca crypto bars HTTP {response.status_code} for {symbol}: "
                f"{response.text[:500]}"
            )

        payload = response.json()
        symbol_rows = payload.get("bars", {}).get(symbol, [])
        if not isinstance(symbol_rows, list):
            raise RuntimeError(f"Malformed Alpaca bars response for {symbol}")

        rows.extend(symbol_rows)
        pages += 1

        # The API explicitly permits fewer results than requested. Continue
        # whenever a next_page_token exists; stop only when pagination ends.
        page_token = payload.get("next_page_token")
        if not page_token:
            break

        if pages >= 100:
            raise RuntimeError(f"FAIL-CLOSED: pagination limit exceeded for {symbol}")

        # Once enough rows have been collected, we still do not need another
        # page. We will normalize/filter/deduplicate below before the final
        # exact-count check.
        if len(rows) >= required_bars:
            break

    if not rows:
        raise RuntimeError(f"Alpaca returned no crypto bars for {symbol}")

    df = pd.DataFrame(rows)
    required_columns = {"t", "o", "h", "l", "c", "v"}
    missing = required_columns - set(df.columns)
    if missing:
        raise RuntimeError(
            f"Alpaca response missing columns for {symbol}: {sorted(missing)}"
        )

    df = df.rename(
        columns={
            "t": "timestamp",
            "o": "open",
            "h": "high",
            "l": "low",
            "c": "close",
            "v": "volume",
        }
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    df["symbol"] = symbol

    # Never pass the currently forming hour downstream. The end boundary is
    # itself the current completed-hour boundary, so timestamps must be < end.
    df = df[df["timestamp"] < end].copy()
    df = df[["timestamp", "symbol", "open", "high", "low", "close", "volume"]]
    df = (
        df.sort_values("timestamp")
        .drop_duplicates("timestamp")
        .tail(required_bars)
        .reset_index(drop=True)
    )

    if len(df) < required_bars:
        raise RuntimeError(
            f"INSUFFICIENT_CLOSED_BARS:{symbol}:{len(df)}<{required_bars}"
        )

    return df


def main() -> int:
    # Load .env (if present) into the process environment. This does not
    # override variables already set (e.g. by a CI secret injected via the
    # GitHub Actions `env:` block) -- it only fills in what's missing, so a
    # local .env never shadows a real deployment credential.
    load_dotenv()

    p = argparse.ArgumentParser()
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    p.add_argument("--bars", type=int, default=BARS)
    p.add_argument("--lookback-days", type=int, default=LOOKBACK_DAYS)
    p.add_argument(
        "--required-only",
        action="store_true",
        help=(
            "Skip Alpaca universe discovery and fetch only REQUIRED_SYMBOLS "
            "(BTC/USD, ETH/USD). Useful for a fast smoke test."
        ),
    )
    args = p.parse_args()

    if args.bars < 204:
        print("FAIL-CLOSED: --bars must be >= 204 for the frozen EMA50 warmup.", file=sys.stderr)
        return 2
    if args.lookback_days * 24 < args.bars + 4:
        print("FAIL-CLOSED: lookback window is too short for requested bars.", file=sys.stderr)
        return 2

    key = os.getenv("ALPACA_PAPER_API_KEY") or os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_PAPER_SECRET_KEY") or os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        print("FAIL-CLOSED: Alpaca credentials are missing.", file=sys.stderr)
        return 2

    try:
        now = pd.Timestamp.now(tz="UTC")
        # The runtime works on completed 1H candles only. Query through the
        # latest completed hour boundary, never through the current hour.
        end = now.floor("h")
        start = end - pd.Timedelta(days=args.lookback_days)

        headers = {
            "APCA-API-KEY-ID": key,
            "APCA-API-SECRET-KEY": secret,
            "Accept": "application/json",
        }

        # Discover the live tradable Alpaca crypto universe. REQUIRED_SYMBOLS
        # are always attempted regardless of what the universe listing
        # returns -- their fetch failure is fatal either way.
        if args.required_only:
            universe: list[str] = []
        else:
            universe = discover_crypto_universe(headers)

        optional_symbols = sorted(
            set(universe) - set(REQUIRED_SYMBOLS)
        )

        frames = []
        fetched_symbols: list[str] = []
        skipped_optional: list[tuple[str, str]] = []

        # REQUIRED_SYMBOLS: any failure here is fatal, unchanged from before
        # this module supported more than two symbols.
        for symbol in REQUIRED_SYMBOLS:
            frames.append(
                fetch_closed_bars(
                    symbol=symbol,
                    start=start,
                    end=end,
                    required_bars=args.bars,
                    headers=headers,
                )
            )
            fetched_symbols.append(symbol)

        # Optional symbols: best-effort. A single thin/unavailable coin must
        # never block the required BTC/ETH signal chain, so failures here are
        # logged and skipped rather than raised.
        for symbol in optional_symbols:
            time.sleep(INTER_SYMBOL_DELAY_SECONDS)
            try:
                frames.append(
                    fetch_closed_bars(
                        symbol=symbol,
                        start=start,
                        end=end,
                        required_bars=args.bars,
                        headers=headers,
                    )
                )
                fetched_symbols.append(symbol)
            except Exception as exc:  # noqa: BLE001 - deliberately broad, optional path
                skipped_optional.append((symbol, f"{type(exc).__name__}: {exc}"))

        out = pd.concat(frames, ignore_index=True).sort_values(
            ["symbol", "timestamp"]
        )

        # Final canonical-stream integrity checks before publishing the file.
        # Every symbol that made it into `out` was already returned at
        # exactly `required_bars` rows by fetch_closed_bars(), so these are
        # consistency checks, not a second chance to fail an optional symbol.
        missing_required = [s for s in REQUIRED_SYMBOLS if s not in fetched_symbols]
        if missing_required:
            raise RuntimeError(
                f"FAIL-CLOSED: required symbols missing from output: {missing_required}"
            )

        for symbol in fetched_symbols:
            g = out[out["symbol"] == symbol].sort_values("timestamp")
            if len(g) != args.bars:
                raise RuntimeError(
                    f"FAIL-CLOSED: final bar count mismatch for {symbol}: "
                    f"{len(g)} != {args.bars}"
                )
            if g["timestamp"].duplicated().any():
                raise RuntimeError(f"FAIL-CLOSED: duplicate timestamps for {symbol}")
            if len(g) > 1 and not (g["timestamp"].diff().dropna() == pd.Timedelta(hours=1)).all():
                raise RuntimeError(f"FAIL-CLOSED: 1H continuity gap for {symbol}")
            if (g["timestamp"] >= end).any():
                raise RuntimeError(f"FAIL-CLOSED: open/current hour leaked for {symbol}")

        args.output.parent.mkdir(parents=True, exist_ok=True)
        out.to_csv(args.output, index=False)

        print("=" * 80)
        print("AURA v0.5.3.21 — ALPACA CLOSED 1H DATA")
        print("MODE: PAPER DATA ONLY / NO ORDERS")
        print(f"WINDOW: {start.isoformat()} -> {end.isoformat()}")
        print(f"REQUIRED CLOSED BARS: {args.bars}")
        print(f"OUTPUT: {args.output.resolve()}")
        print()
        print(f"REQUIRED SYMBOLS ({len(REQUIRED_SYMBOLS)}):")
        for symbol in REQUIRED_SYMBOLS:
            g = out[out["symbol"] == symbol]
            print(
                f"  {symbol}: {len(g)} closed bars; "
                f"oldest={g['timestamp'].min().isoformat()}; "
                f"latest={g['timestamp'].max().isoformat()}"
            )
        fetched_optional = [s for s in fetched_symbols if s not in REQUIRED_SYMBOLS]
        print()
        print(
            f"OPTIONAL SYMBOLS: {len(fetched_optional)} fetched, "
            f"{len(skipped_optional)} skipped (universe size: {len(universe)})"
        )
        for symbol, reason in skipped_optional:
            print(f"  SKIPPED {symbol}: {reason}")
        print("=" * 80)
        return 0

    except Exception as exc:
        print(f"FAIL-CLOSED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
