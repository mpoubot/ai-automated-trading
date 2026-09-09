import csv
from pathlib import Path

# EXPERIMENT B2: exit-efficiency diagnostics.
# Separate from Experiment B so asset-selection evidence remains clean.
# No live trading and no orders.

ROOT = Path(__file__).resolve().parent
INPUT = ROOT / "research" / "b2_trade_samples.csv"
OUTPUT = ROOT / "research" / "b2_exit_diagnostics.csv"

def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

def main():
    print("EXPERIMENT B2 - EXIT EFFICIENCY")
    print("No live trading. No orders.")
    if not INPUT.exists():
        print(f"Input not found: {INPUT}")
        print("B2 needs trade + price-path data before calculating MFE/MAE.")
        print("Required: symbol, entry_time, entry_price, stop_price,")
        print("peak_price, trough_price, exit_time, exit_price, exit_reason.")
        return

    with INPUT.open("r", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print("Input contains no rows.")
        return

    required = {"symbol","entry_time","entry_price","stop_price",
                "exit_time","exit_price","exit_reason"}
    missing = required - set(rows[0])
    if missing:
        print("Missing required fields:", ", ".join(sorted(missing)))
        return

    results = []
    for r in rows:
        entry, stop, exitp = num(r.get("entry_price")), num(r.get("stop_price")), num(r.get("exit_price"))
        if entry is None or stop is None or exitp is None or abs(entry-stop) == 0:
            continue
        risk = abs(entry-stop)
        side = str(r.get("side","LONG")).upper()
        peak, trough = num(r.get("peak_price")), num(r.get("trough_price"))
        if side == "SHORT":
            realized = (entry-exitp)/risk
            mfe = (entry-trough)/risk if trough is not None else ""
            mae = (peak-entry)/risk if peak is not None else ""
        else:
            realized = (exitp-entry)/risk
            mfe = (peak-entry)/risk if peak is not None else ""
            mae = (entry-trough)/risk if trough is not None else ""
        results.append({
            "symbol":r.get("symbol",""), "entry_time":r.get("entry_time",""),
            "exit_time":r.get("exit_time",""), "side":side,
            "entry_price":entry, "stop_price":stop,
            "peak_price":peak if peak is not None else "",
            "trough_price":trough if trough is not None else "",
            "exit_price":exitp, "exit_reason":r.get("exit_reason",""),
            "mfe_r":mfe, "mae_r":mae, "realized_r":realized
        })

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0]))
        w.writeheader()
        w.writerows(results)
    print(f"Trades analysed: {len(results)}")
    print(f"Saved: {OUTPUT}")

if __name__ == "__main__":
    main()
