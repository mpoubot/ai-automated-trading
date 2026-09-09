import csv, json, time, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path

# EXPERIMENT B v2: Top-200 market-cap universe.
# This deliberately keeps entry, risk, sizing and exit logic unchanged.
# Exit diagnostics belong to the separate B2 experiment.

DRY_RUN = True
SCAN_INTERVAL_SECONDS = 15 * 60

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "research"
EVENTS = OUT / "quality_universe_events_v2.csv"

FIELDS = [
    "timestamp_utc","experiment","universe_rule","symbol","rank",
    "market_cap_usd","price_usd","volume_24h_usd","volume_market_cap",
    "change_24h_pct","change_7d_pct","change_30d_pct",
    "selected","reason","dry_run"
]

def append_row(row):
    OUT.mkdir(parents=True, exist_ok=True)
    new = not EVENTS.exists()
    with EVENTS.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)

def load_top200():
    params = {
        "vs_currency":"usd", "order":"market_cap_desc",
        "per_page":"200", "page":"1", "sparkline":"false",
        "price_change_percentage":"7d,30d"
    }
    url = "https://api.coingecko.com/api/v3/coins/markets?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent":"quality-universe-experiment/2.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))

def scan():
    coins = load_top200()
    now = datetime.now(timezone.utc).isoformat()
    for rank, c in enumerate(coins, 1):
        mc = c.get("market_cap") or 0
        vol = c.get("total_volume") or 0
        append_row({
            "timestamp_utc": now,
            "experiment": "B",
            "universe_rule": "TOP_200_MARKET_CAP",
            "symbol": str(c.get("symbol","")).upper(),
            "rank": rank,
            "market_cap_usd": mc,
            "price_usd": c.get("current_price") or "",
            "volume_24h_usd": vol,
            "volume_market_cap": (vol/mc if mc else ""),
            "change_24h_pct": c.get("price_change_percentage_24h") or "",
            "change_7d_pct": c.get("price_change_percentage_7d_in_currency") or "",
            "change_30d_pct": c.get("price_change_percentage_30d_in_currency") or "",
            "selected": True,
            "reason": "TOP_200_MARKET_CAP",
            "dry_run": DRY_RUN
        })
    print(f"[{now}] Experiment B: recorded {len(coins)} Top-200 assets")
    print(f"Output: {EVENTS}")
    print("Strategy/exit settings unchanged.")
    print("DRY_RUN=True | No orders are placed.")

def main():
    print("="*70)
    print("EXPERIMENT B v2 - QUALITY UNIVERSE")
    print("Universe: Top 200 by market cap")
    print("Entry/risk/exit logic: unchanged")
    print(f"DRY_RUN={DRY_RUN}")
    print(f"Output={EVENTS}")
    print("="*70)
    while True:
        try:
            scan()
            time.sleep(SCAN_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            print("\nExperiment B stopped cleanly. Existing CSV preserved.")
            break
        except Exception as e:
            print(f"Scan error: {type(e).__name__}: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()
