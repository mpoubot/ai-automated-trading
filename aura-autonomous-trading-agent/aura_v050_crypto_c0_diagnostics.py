#!/usr/bin/env python3
"""
AURA v0.5.0 — C0 Diagnostic / Report Layer

Read-only analysis of the C0 crypto research ledger.

Expected directory:
    AURA_CRYPTO/
        trades.csv
        position_snapshots.csv
        funding_history.csv
        bars_1h.csv
        daily_model_metrics.csv

Outputs:
    AURA_CRYPTO/C0_diagnostic_report.html
    AURA_CRYPTO/C0_trade_diagnostics.csv
    AURA_CRYPTO/C0_mfe_mae_percentiles.csv
    AURA_CRYPTO/C0_hourly_behavior.csv
    AURA_CRYPTO/C0_symbol_behavior.csv
    AURA_CRYPTO/C0_funding_diagnostics.csv

This script NEVER changes the strategy and NEVER submits orders.
"""

from __future__ import annotations

import argparse
import html
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd


def read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)


def pct(v):
    if v is None or pd.isna(v):
        return "—"
    return f"{float(v) * 100:.3f}%"


def num(v, digits=4):
    if v is None or pd.isna(v):
        return "—"
    return f"{float(v):.{digits}f}"


def corr(a, b):
    x = pd.concat([pd.Series(a), pd.Series(b)], axis=1).dropna()
    if len(x) < 3:
        return np.nan
    return x.iloc[:, 0].corr(x.iloc[:, 1])


def add_trade_funding_diagnostics(trades, funding):
    t = trades.copy()
    for c in ["entry_timestamp", "exit_timestamp"]:
        t[c] = pd.to_datetime(t[c], utc=True, errors="coerce")

    if funding.empty:
        t["funding_events"] = 0
        t["funding_rate_mean"] = np.nan
        t["funding_rate_max_abs"] = np.nan
        t["funding_rate_sum"] = np.nan
        return t

    f = funding.copy()
    f["settle_time"] = pd.to_datetime(f["settle_time"], utc=True, errors="coerce")
    f["funding_rate"] = pd.to_numeric(f["funding_rate"], errors="coerce")

    rows = []
    for _, tr in t.iterrows():
        sub = f[
            (f["symbol"] == tr["symbol"])
            & (f["settle_time"] > tr["entry_timestamp"])
            & (f["settle_time"] <= tr["exit_timestamp"])
        ]
        rates = sub["funding_rate"].dropna()
        rows.append({
            "trade_id": tr["trade_id"],
            "funding_events": len(rates),
            "funding_rate_mean": rates.mean() if len(rates) else np.nan,
            "funding_rate_max_abs": rates.abs().max() if len(rates) else np.nan,
            "funding_rate_sum": rates.sum() if len(rates) else np.nan,
        })

    fd = pd.DataFrame(rows)
    return t.merge(fd, on="trade_id", how="left")


def make_table(df, max_rows=100):
    if df.empty:
        return '<div class="empty">No data available.</div>'
    x = df.head(max_rows).copy()
    for c in x.columns:
        x[c] = x[c].map(lambda v: "" if pd.isna(v) else str(v))
    return x.to_html(index=False, classes="data", border=0, escape=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--crypto-dir", default="AURA_CRYPTO")
    args = ap.parse_args()

    d = Path(args.crypto_dir)
    if not d.exists():
        raise SystemExit(f"Crypto directory not found: {d}")

    trades = read_csv(d / "trades.csv")
    snapshots = read_csv(d / "position_snapshots.csv")
    funding = read_csv(d / "funding_history.csv")
    bars = read_csv(d / "bars_1h.csv")
    metrics = read_csv(d / "daily_model_metrics.csv")

    if trades.empty:
        raise SystemExit("trades.csv is empty — no diagnostic report can be produced yet.")

    for c in ["signal_timestamp", "entry_timestamp", "exit_timestamp"]:
        trades[c] = pd.to_datetime(trades[c], utc=True, errors="coerce")

    for c in ["net_return", "mfe", "mae", "funding_return", "commission_return", "price_return"]:
        if c in trades.columns:
            trades[c] = pd.to_numeric(trades[c], errors="coerce")

    # --- Core diagnostics ---------------------------------------------------
    td = add_trade_funding_diagnostics(trades, funding)

    td["entry_hour_utc"] = td["entry_timestamp"].dt.hour
    td["entry_weekday_utc"] = td["entry_timestamp"].dt.day_name()
    td["winner"] = td["net_return"] > 0
    td["mfe_capture_ratio"] = np.where(
        td["mfe"] > 0, td["net_return"] / td["mfe"], np.nan
    )

    # Funding relationship diagnostics.
    funding_corr_mae = corr(td["funding_return"], td["mae"])
    funding_corr_net = corr(td["funding_return"], td["net_return"])
    rate_corr_mae = corr(td["funding_rate_sum"], td["mae"])
    rate_corr_net = corr(td["funding_rate_sum"], td["net_return"])

    # MFE/MAE distributions.
    probs = [0.00, 0.10, 0.25, 0.50, 0.75, 0.90, 1.00]
    dist_rows = []
    for metric in ["mfe", "mae", "net_return", "mfe_capture_ratio"]:
        if metric not in td.columns:
            continue
        for q in probs:
            dist_rows.append({
                "metric": metric,
                "percentile": f"{int(q*100)}th",
                "value": td[metric].quantile(q),
            })
    dist = pd.DataFrame(dist_rows)

    # Winner vs loser path behavior.
    wl = (
        td.groupby("winner")
        .agg(
            trades=("trade_id", "count"),
            net_mean=("net_return", "mean"),
            net_median=("net_return", "median"),
            mean_mfe=("mfe", "mean"),
            median_mfe=("mfe", "median"),
            mean_mae=("mae", "mean"),
            median_mae=("mae", "median"),
            mean_funding=("funding_return", "mean"),
            mean_mfe_capture=("mfe_capture_ratio", "mean"),
        )
        .reset_index()
    )
    wl["result"] = wl["winner"].map({True: "WINNERS", False: "LOSERS"})

    # Hourly behavior.
    hourly = (
        td.groupby("entry_hour_utc")
        .agg(
            trades=("trade_id", "count"),
            net_mean=("net_return", "mean"),
            net_median=("net_return", "median"),
            win_rate=("winner", "mean"),
            mean_mfe=("mfe", "mean"),
            mean_mae=("mae", "mean"),
            mean_funding=("funding_return", "mean"),
        )
        .reset_index()
        .sort_values("entry_hour_utc")
    )

    # Symbol behavior.
    symbol = (
        td.groupby("symbol")
        .agg(
            trades=("trade_id", "count"),
            net_mean=("net_return", "mean"),
            net_median=("net_return", "median"),
            win_rate=("winner", "mean"),
            mean_mfe=("mfe", "mean"),
            mean_mae=("mae", "mean"),
            mean_funding=("funding_return", "mean"),
            mean_mfe_capture=("mfe_capture_ratio", "mean"),
        )
        .reset_index()
        .sort_values("net_mean", ascending=False)
    )

    funding_diag = pd.DataFrame([{
        "trades": len(td),
        "funding_return_mean": td["funding_return"].mean(),
        "funding_return_median": td["funding_return"].median(),
        "funding_rate_sum_mean": td["funding_rate_sum"].mean(),
        "funding_rate_max_abs_mean": td["funding_rate_max_abs"].mean(),
        "corr_funding_return_mae": funding_corr_mae,
        "corr_funding_return_net": funding_corr_net,
        "corr_funding_rate_sum_mae": rate_corr_mae,
        "corr_funding_rate_sum_net": rate_corr_net,
    }])

    # Save machine-readable outputs.
    td.to_csv(d / "C0_trade_diagnostics.csv", index=False)
    dist.to_csv(d / "C0_mfe_mae_percentiles.csv", index=False)
    hourly.to_csv(d / "C0_hourly_behavior.csv", index=False)
    symbol.to_csv(d / "C0_symbol_behavior.csv", index=False)
    funding_diag.to_csv(d / "C0_funding_diagnostics.csv", index=False)

    # HTML report.
    start = td["signal_timestamp"].min()
    end = td["exit_timestamp"].max()
    gross = td["price_return"].mean()
    net = td["net_return"].mean()
    median = td["net_return"].median()
    win = td["winner"].mean()
    pf = (
        td.loc[td["net_return"] > 0, "net_return"].sum()
        / abs(td.loc[td["net_return"] < 0, "net_return"].sum())
        if (td["net_return"] < 0).any() else np.inf
    )

    html_doc = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>AURA v0.5.0 — C0 Diagnostic Report</title>
<style>
body{{font-family:Segoe UI,Arial,sans-serif;background:#0b1020;color:#e8edf7;margin:0}}
.wrap{{max-width:1450px;margin:auto;padding:28px}}
.panel{{background:#121a2d;border:1px solid #2b3956;border-radius:14px;padding:20px;margin:0 0 18px}}
h1{{margin:0 0 8px}} h2{{font-size:18px}}
.sub,.note{{color:#9aa8c1}}
.grid{{display:grid;grid-template-columns:repeat(5,1fr);gap:12px}}
.kpi{{font-size:24px;font-weight:700;margin-top:5px}}
.label{{font-size:11px;text-transform:uppercase;color:#9aa8c1;letter-spacing:.08em}}
table.data{{border-collapse:collapse;width:100%;font-size:13px}}
table.data th,table.data td{{border-bottom:1px solid #2b3956;padding:8px;text-align:left}}
table.data th{{color:#9aa8c1}}
.empty{{color:#9aa8c1}}
.warning{{border:1px solid #7b6222;background:#2b2513;padding:12px;border-radius:10px}}
</style>
</head>
<body>
<div class="wrap">
<div class="panel">
<div class="label">AURA CRYPTO RESEARCH</div>
<h1>v0.5.0 C0 Diagnostic Report</h1>
<div class="sub">Read-only analysis • no strategy changes • no orders</div>
<p>Observation window: {start} → {end}</p>
</div>

<div class="grid">
<div class="panel"><div class="label">Trades</div><div class="kpi">{len(td)}</div></div>
<div class="panel"><div class="label">Gross Mean</div><div class="kpi">{pct(gross)}</div></div>
<div class="panel"><div class="label">Net Mean</div><div class="kpi">{pct(net)}</div></div>
<div class="panel"><div class="label">Net Median</div><div class="kpi">{pct(median)}</div></div>
<div class="panel"><div class="label">Win Rate</div><div class="kpi">{pct(win)}</div></div>
</div>

<div class="panel">
<h2>Interpretation Guardrail</h2>
<div class="warning">
This is a diagnostic layer, not an optimization layer. Hourly, symbol and funding differences
are observations only. No filter or parameter should be selected from this report until the
C0 horizon has been expanded and the hypothesis is independently validated.
</div>
</div>

<div class="panel"><h2>MFE / MAE Percentiles</h2>{make_table(dist)}</div>
<div class="panel"><h2>Winners vs Losers</h2>{make_table(wl[["result","trades","net_mean","net_median","mean_mfe","median_mfe","mean_mae","median_mae","mean_funding","mean_mfe_capture"]])}</div>
<div class="panel"><h2>Hourly Behavior (UTC)</h2>{make_table(hourly)}</div>
<div class="panel"><h2>Symbol Behavior</h2>{make_table(symbol)}</div>
<div class="panel"><h2>Funding Diagnostics</h2>{make_table(funding_diag)}</div>
<div class="panel">
<h2>Current C0 Summary</h2>
<table class="data">
<tr><th>Gross mean</th><td>{pct(gross)}</td></tr>
<tr><th>Net mean</th><td>{pct(net)}</td></tr>
<tr><th>Net median</th><td>{pct(median)}</td></tr>
<tr><th>Win rate</th><td>{pct(win)}</td></tr>
<tr><th>Profit factor</th><td>{num(pf,3)}</td></tr>
<tr><th>Mean MFE</th><td>{pct(td["mfe"].mean())}</td></tr>
<tr><th>Mean MAE</th><td>{pct(td["mae"].mean())}</td></tr>
<tr><th>MFE / MAE</th><td>{num(td["mfe"].mean()/abs(td["mae"].mean()),3)}</td></tr>
<tr><th>Funding ↔ MAE correlation</th><td>{num(funding_corr_mae,3)}</td></tr>
<tr><th>Funding ↔ Net return correlation</th><td>{num(funding_corr_net,3)}</td></tr>
</table>
</div>
</div>
</body>
</html>"""

    report = d / "C0_diagnostic_report.html"
    report.write_text(html_doc, encoding="utf-8")

    print("AURA v0.5.0 — C0 DIAGNOSTIC REPORT COMPLETE")
    print(f"Observation window: {start} -> {end}")
    print(f"Trades: {len(td)}")
    print(f"Gross mean: {pct(gross)}")
    print(f"Net mean:   {pct(net)}")
    print(f"Net median: {pct(median)}")
    print(f"Win rate:   {pct(win)}")
    print(f"Profit factor: {num(pf,3)}")
    print(f"Funding ↔ MAE corr: {num(funding_corr_mae,3)}")
    print(f"Funding ↔ Net corr: {num(funding_corr_net,3)}")
    print(f"Report: {report}")
    print("READ ONLY — NO ORDERS")


if __name__ == "__main__":
    main()
