"""
Data access layer: wraps ccxt calls to MEXC for market discovery and OHLCV data.
Used identically by the backtester (historical) and live bot (recent candles).
"""
import time
import pandas as pd
import ccxt
import config as cfg


def build_exchange(api_key: str = "", api_secret: str = ""):
    """Create a ccxt MEXC exchange instance. Public data works with no keys;
    keys are only required for placing orders / reading account balance."""
    exchange_class = getattr(ccxt, cfg.EXCHANGE_ID)
    exchange = exchange_class({
        "apiKey": api_key,
        "secret": api_secret,
        "enableRateLimit": True,
        "options": {"defaultType": cfg.MARKET_TYPE},
    })
    return exchange


def get_candidate_universe(exchange, quote_asset: str = None) -> list[dict]:
    """
    Returns a list of dicts: {"symbol": ..., "quote_volume_24h": ..., "listing_age_days": ...}
    for all markets matching the configured quote asset and market type.
    Note: MEXC's API does not always expose exact listing date — where unavailable,
    listing_age_days defaults to a large number (treated as "old enough") rather than
    blocking the pair; tighten this if precise listing-age filtering matters to you.
    """
    quote_asset = quote_asset or cfg.QUOTE_ASSET
    markets = exchange.load_markets()
    tickers = exchange.fetch_tickers()

    candidates = []
    for symbol, market in markets.items():
        if not market.get("active", True):
            continue
        if market.get("quote") != quote_asset:
            continue
        if cfg.MARKET_TYPE == "swap" and not market.get("swap"):
            continue
        if cfg.MARKET_TYPE == "spot" and not market.get("spot"):
            continue

        ticker = tickers.get(symbol, {})
        quote_volume = ticker.get("quoteVolume") or 0

        candidates.append({
            "symbol": symbol,
            "quote_volume_24h": quote_volume,
            "listing_age_days": 9999,  # see docstring note
        })

    candidates.sort(key=lambda c: c["quote_volume_24h"], reverse=True)
    return candidates[: cfg.MAX_PAIRS_SCANNED]


def fetch_ohlcv_df(exchange, symbol: str, timeframe: str = None,
                    limit: int = None, since: int = None) -> pd.DataFrame:
    """Fetch OHLCV candles for a symbol and return as a DataFrame with
    columns: timestamp, open, high, low, close, volume."""
    timeframe = timeframe or cfg.TIMEFRAME
    limit = limit or cfg.CANDLES_LOOKBACK

    raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit, since=since)
    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df


def fetch_funding_history(exchange, symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """
    Fetch historical funding rates for a perpetual symbol.

    Returns a DataFrame with columns [timestamp, funding_rate], or an EMPTY
    DataFrame if the exchange/ccxt doesn't support it for this symbol. Callers
    must handle the empty case by falling back to cfg.FUNDING_RATE_FALLBACK —
    silently assuming zero funding would flatter every backtest.
    """
    empty = pd.DataFrame(columns=["timestamp", "funding_rate"])
    if not getattr(exchange, "has", {}).get("fetchFundingRateHistory"):
        return empty

    rows = []
    since = start_ms
    try:
        while since < end_ms:
            batch = exchange.fetch_funding_rate_history(symbol, since=since, limit=1000)
            if not batch:
                break
            rows.extend(batch)
            last_ts = batch[-1]["timestamp"]
            if last_ts is None or last_ts <= since:
                break
            since = last_ts + 1
            time.sleep(exchange.rateLimit / 1000)
            if len(batch) < 1000:
                break
    except Exception:
        # Not supported / rate limited / symbol unavailable — caller falls back.
        return empty

    if not rows:
        return empty

    df = pd.DataFrame([
        {"timestamp": r.get("timestamp"), "funding_rate": r.get("fundingRate")}
        for r in rows
    ]).dropna()
    if df.empty:
        return empty
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df.sort_values("timestamp").reset_index(drop=True)


def fetch_ohlcv_range(exchange, symbol: str, timeframe: str,
                       start_ms: int, end_ms: int) -> pd.DataFrame:
    """Fetch OHLCV across a date range by paginating fetch_ohlcv calls.
    Used by the backtester to pull long historical windows."""
    all_rows = []
    since = start_ms
    while since < end_ms:
        batch = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=1000)
        if not batch:
            break
        all_rows.extend(batch)
        last_ts = batch[-1][0]
        if last_ts <= since:
            break
        since = last_ts + 1
        time.sleep(exchange.rateLimit / 1000)
        if len(batch) < 1000:
            break

    df = pd.DataFrame(all_rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    if not df.empty:
        df = df[(df["timestamp"] >= start_ms) & (df["timestamp"] <= end_ms)]
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df.reset_index(drop=True)
