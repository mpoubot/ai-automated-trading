"""
Asset Selection & Executability Experiment V1
Public MEXC Futures/Swap market-data capture.

Examples:
  python asset_selection_capture.py --symbols KAITO_USDT,CRV_USDT,BTC_USDT
  python asset_selection_capture.py --signals asset_events.csv

This module observes market data only. It does not place orders.
"""

from __future__ import annotations
import argparse
import csv
import json
import math
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def mexc_symbol(s: str) -> str:
    s = s.upper().strip()
    s = s.replace(":USDT", "").replace("/", "_").replace("-", "_")
    if s.endswith("USDT") and "_" not in s:
        s = s[:-4] + "_USDT"
    return s


def base_asset(s: str) -> str:
    x = mexc_symbol(s)
    return x[:-5] if x.endswith("_USDT") else x.split("_")[0]


class MEXCFutures:
    def __init__(self, cfg: dict):
        self.base = cfg["mexc"]["base_url"].rstrip("/")
        self.timeout = cfg["mexc"]["request_timeout_seconds"]
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": cfg["mexc"]["user_agent"]})

    def get(self, path: str, params: Optional[dict] = None) -> Any:
        r = self.session.get(self.base + path, params=params, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("success") is False:
            raise RuntimeError(str(data))
        return data

    def ticker(self, symbol: Optional[str] = None) -> Any:
        params = {"symbol": mexc_symbol(symbol)} if symbol else None
        return self.get("/api/v1/contract/ticker", params)

    def depth(self, symbol: str, limit: int = 100) -> Any:
        return self.get(f"/api/v1/contract/depth/{mexc_symbol(symbol)}",
                        {"limit": limit})

    def detail(self, symbol: str) -> Any:
        return self.get("/api/v1/contract/detail",
                        {"symbol": mexc_symbol(symbol)})


class CoinGecko:
    def __init__(self, cfg: dict):
        self.enabled = cfg["coingecko"]["enabled"]
        self.base = cfg["coingecko"]["base_url"].rstrip("/")
        self.timeout = cfg["coingecko"]["request_timeout_seconds"]
        self.per_page = cfg["coingecko"]["markets_per_page"]
        self.max_pages = cfg["coingecko"]["max_pages"]
        self.cache_minutes = cfg["coingecko"]["cache_minutes"]
        self.session = requests.Session()
        self.cache_path = Path("research/asset_selection/coingecko_cache.json")
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)

    def _load_cache(self) -> dict:
        if not self.cache_path.exists():
            return {}
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def markets(self) -> list[dict]:
        if not self.enabled:
            return []
        cache = self._load_cache()
        now = time.time()
        if cache.get("timestamp") and now - cache["timestamp"] < self.cache_minutes * 60:
            return cache.get("data", [])
        out = []
        for page in range(1, self.max_pages + 1):
            r = self.session.get(
                self.base + "/coins/markets",
                params={
                    "vs_currency": "usd",
                    "order": "market_cap_desc",
                    "per_page": self.per_page,
                    "page": page,
                    "sparkline": "false"
                },
                timeout=self.timeout
            )
            r.raise_for_status()
            data = r.json()
            if not data:
                break
            out.extend(data)
        self.cache_path.write_text(
            json.dumps({"timestamp": now, "data": out}),
            encoding="utf-8"
        )
        return out


def orderbook_metrics(depth: dict, mid: float) -> dict:
    bids = depth.get("data", depth)
    if isinstance(bids, dict):
        bids = bids.get("bids", [])
    asks = depth.get("asks", []) if isinstance(depth, dict) else []
    if not asks and isinstance(depth.get("data"), dict):
        asks = depth["data"].get("asks", [])
        bids = depth["data"].get("bids", [])

    def norm(levels):
        out = []
        for x in levels or []:
            try:
                if isinstance(x, dict):
                    p = float(x.get("price"))
                    q = float(x.get("vol", x.get("quantity", x.get("qty", 0))))
                else:
                    p, q = float(x[0]), float(x[1])
                out.append((p, q))
            except Exception:
                continue
        return out

    bids = norm(bids)
    asks = norm(asks)

    if not bids or not asks or mid <= 0:
        return {
            "bid": "", "ask": "", "spread_bps": "",
            "depth_5bps_usd": "", "depth_10bps_usd": "",
            "depth_25bps_usd": "", "depth_50bps_usd": ""
        }

    best_bid = max(p for p, _ in bids)
    best_ask = min(p for p, _ in asks)
    mid2 = (best_bid + best_ask) / 2
    spread_bps = (best_ask - best_bid) / mid2 * 10000 if mid2 else math.nan

    result = {
        "bid": best_bid,
        "ask": best_ask,
        "spread_bps": spread_bps
    }

    for bps in (5, 10, 25, 50):
        lo = mid2 * (1 - bps / 10000)
        hi = mid2 * (1 + bps / 10000)
        bid_value = sum(p * q for p, q in bids if p >= lo)
        ask_value = sum(p * q for p, q in asks if p <= hi)
        result[f"depth_{bps}bps_usd"] = bid_value + ask_value
    return result


def category_for(base: str, markets: list[dict]) -> dict:
    base = base.lower()
    for x in markets:
        if str(x.get("symbol", "")).lower() == base:
            return {
                "market_cap_rank": x.get("market_cap_rank"),
                "market_cap_usd": x.get("market_cap"),
                "volume_24h_usd": x.get("total_volume"),
                "asset_category": "unknown",
                "asset_category_source": "coingecko_market"
            }
    return {
        "market_cap_rank": "",
        "market_cap_usd": "",
        "volume_24h_usd": "",
        "asset_category": "unknown",
        "asset_category_source": "not_found"
    }


def capture(symbol: str, client: MEXCFutures, cg: CoinGecko, cfg: dict) -> dict:
    symbol = mexc_symbol(symbol)
    base = base_asset(symbol)

    ticker = client.ticker(symbol)
    data = ticker.get("data", ticker)
    if isinstance(data, list):
        data = data[0] if data else {}

    last = float(data.get("lastPrice", data.get("last", 0)) or 0)
    fair = float(data.get("fairPrice", last) or last)

    try:
        depth = client.depth(symbol, cfg["mexc"]["depth_levels"])
    except Exception as e:
        depth = {"error": str(e)}

    ob = orderbook_metrics(depth, fair or last)

    try:
        detail = client.detail(symbol)
        detail_data = detail.get("data", detail)
        if isinstance(detail_data, list):
            detail_data = detail_data[0] if detail_data else {}
    except Exception:
        detail_data = {}

    markets = cg.markets()
    cgx = category_for(base, markets)

    volume = cgx.get("volume_24h_usd") or data.get("amount24")
    mcap = cgx.get("market_cap_usd")
    vmcap = ""
    if mcap and volume:
        vmcap = float(volume) / float(mcap) * 100

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "base_asset": base,
        "price": last,
        "fair_price": fair,
        "market_cap_rank": cgx.get("market_cap_rank", ""),
        "market_cap_usd": mcap or "",
        "volume_24h_usd": volume or "",
        "volume_mcap_pct": vmcap,
        **ob,
        "mexc_api_eligible": bool(detail_data) if detail_data else "",
        "innovation_zone": "",
        "contract_detail_json": json.dumps(detail_data, separators=(",", ":"))
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", help="Comma-separated MEXC futures symbols")
    p.add_argument("--signals", help="Existing asset_events.csv; enrich signal rows")
    p.add_argument("--config", default="config.json")
    p.add_argument("--out", default="asset_snapshots.csv")
    args = p.parse_args()

    cfg = load_json(args.config)
    client = MEXCFutures(cfg)
    cg = CoinGecko(cfg)

    symbols = []
    if args.symbols:
        symbols = [x.strip() for x in args.symbols.split(",") if x.strip()]
    elif args.signals:
        with open(args.signals, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("event_type") == "signal":
                    symbols.append(row["symbol"])
        symbols = list(dict.fromkeys(symbols))
    else:
        p.error("Provide --symbols or --signals")

    rows = []
    for s in symbols:
        try:
            rows.append(capture(s, client, cg, cfg))
            print(f"[OK] {s}")
        except Exception as e:
            print(f"[ERROR] {s}: {e}")

    if rows:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        keys = sorted({k for r in rows for k in r.keys()})
        write_header = not out.exists()
        with out.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
            if write_header:
                w.writeheader()
            w.writerows(rows)
        print(f"Saved {len(rows)} snapshots -> {out}")


if __name__ == "__main__":
    main()
