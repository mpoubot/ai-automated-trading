#!/usr/bin/env python3
"""
AURA v0.4.9 — Mission Control
Read-only research dashboard for the v0.4.8 paper laboratory.

IMPORTANT:
- This script NEVER submits orders.
- It NEVER changes the frozen signal definition.
- It NEVER changes the exit definitions.
- It only reads AURA_LIVE/*.csv and produces a local HTML dashboard.

Expected folder:
    aura-autonomous-trading-agent/
        aura_v049_mission_control.py
        AURA_LIVE/
            universe.csv
            signals.csv
            positions.csv
            position_snapshots.csv
            executions.csv
            daily_model_metrics.csv
            baseline.csv

Run:
    python aura_v049_mission_control.py

Optional:
    python aura_v049_mission_control.py --live-dir .\AURA_LIVE
"""

from __future__ import annotations

import argparse
import html
import math
from pathlib import Path
from datetime import datetime, timezone

import pandas as pd


MODELS = ["A_10D_TIME", "B_PURE_ATR_2x4x", "C_ATR_2x4x_10D"]

EXPECTED_FILES = [
    "universe.csv",
    "signals.csv",
    "positions.csv",
    "position_snapshots.csv",
    "executions.csv",
    "daily_model_metrics.csv",
    "baseline.csv",
]


def read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception as exc:
        print(f"[WARN] Could not read {path}: {exc}")
        return pd.DataFrame()


def num(series, default=0.0):
    s = pd.to_numeric(series, errors="coerce")
    return s.fillna(default)


def fmt_pct(v) -> str:
    if v is None or not math.isfinite(float(v)):
        return "—"
    return f"{float(v) * 100:.2f}%"


def fmt_num(v, digits=2) -> str:
    if v is None:
        return "—"
    try:
        v = float(v)
        if not math.isfinite(v):
            return "—"
        return f"{v:.{digits}f}"
    except Exception:
        return "—"


def latest_metrics(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "model" not in df.columns:
        return pd.DataFrame()

    x = df.copy()
    if "date" in x.columns:
        x["_date"] = pd.to_datetime(x["date"], errors="coerce")
        x = x.sort_values(["model", "_date"])
    return x.groupby("model", as_index=False).tail(1).drop(columns=["_date"], errors="ignore")


def metric_value(row, name):
    if row is None or name not in row.index:
        return None
    return row[name]


def make_model_rows(metrics: pd.DataFrame) -> str:
    rows = []
    for model in MODELS:
        hit = metrics[metrics["model"] == model] if not metrics.empty and "model" in metrics.columns else pd.DataFrame()
        r = hit.iloc[-1] if not hit.empty else None
        rows.append(
            "<tr>"
            f"<td><b>{html.escape(model)}</b></td>"
            f"<td>{fmt_num(metric_value(r,'closed_trades'),0)}</td>"
            f"<td>{fmt_num(metric_value(r,'open_positions'),0)}</td>"
            f"<td>{fmt_pct(metric_value(r,'net_mean'))}</td>"
            f"<td>{fmt_pct(metric_value(r,'net_median'))}</td>"
            f"<td>{fmt_pct(metric_value(r,'win_rate'))}</td>"
            f"<td>{fmt_num(metric_value(r,'profit_factor'))}</td>"
            f"<td>{fmt_pct(metric_value(r,'max_drawdown'))}</td>"
            f"<td>{fmt_pct(metric_value(r,'capital_efficiency'))}</td>"
            f"<td>{fmt_pct(metric_value(r,'mfe_capture_ratio'))}</td>"
            f"<td>{fmt_pct(metric_value(r,'net_lift_vs_baseline'))}</td>"
            "</tr>"
        )
    return "\n".join(rows)


def make_open_position_rows(positions: pd.DataFrame) -> str:
    if positions.empty:
        return '<tr><td colspan="9" class="muted">No open virtual positions.</td></tr>'

    p = positions.copy()
    status_col = next((c for c in ["status", "position_status"] if c in p.columns), None)
    if status_col:
        p = p[p[status_col].astype(str).str.upper().isin(["OPEN", "PENDING", "OPEN_POSITION"])]
    if p.empty:
        return '<tr><td colspan="9" class="muted">No open virtual positions.</td></tr>'

    out = []
    for _, r in p.tail(100).iterrows():
        out.append(
            "<tr>"
            f"<td>{html.escape(str(r.get('signal_id','—')))}</td>"
            f"<td>{html.escape(str(r.get('model','—')))}</td>"
            f"<td><b>{html.escape(str(r.get('symbol','—')))}</b></td>"
            f"<td>{html.escape(str(r.get('entry_date','—')))}</td>"
            f"<td>{fmt_num(r.get('entry_price'),2)}</td>"
            f"<td>{fmt_num(r.get('stop_price'),2)}</td>"
            f"<td>{fmt_num(r.get('target_price'),2)}</td>"
            f"<td>{fmt_pct(r.get('mfe'))}</td>"
            f"<td>{fmt_pct(r.get('mae'))}</td>"
            "</tr>"
        )
    return "\n".join(out)


def make_signal_rows(signals: pd.DataFrame) -> str:
    if signals.empty:
        return '<tr><td colspan="7" class="muted">No signals recorded.</td></tr>'

    s = signals.tail(50).iloc[::-1]
    out = []
    for _, r in s.iterrows():
        out.append(
            "<tr>"
            f"<td>{html.escape(str(r.get('signal_id','—')))}</td>"
            f"<td>{html.escape(str(r.get('symbol','—')))}</td>"
            f"<td>{html.escape(str(r.get('decision_date','—')))}</td>"
            f"<td>{html.escape(str(r.get('entry_date','—')))}</td>"
            f"<td>{fmt_num(r.get('signal_close'),2)}</td>"
            f"<td>{fmt_num(r.get('ema3'),2)}</td>"
            f"<td>{fmt_num(r.get('relative_volume'),2)}</td>"
            "</tr>"
        )
    return "\n".join(out)


def build_equity_js(executions: pd.DataFrame) -> str:
    if executions.empty:
        return "const equityLabels=[]; const equitySeries={};"

    x = executions.copy()
    if "exit_date" in x.columns:
        x["_date"] = pd.to_datetime(x["exit_date"], errors="coerce")
    elif "date" in x.columns:
        x["_date"] = pd.to_datetime(x["date"], errors="coerce")
    else:
        return "const equityLabels=[]; const equitySeries={};"

    pnl_col = next((c for c in ["net_return", "net_pnl", "return_net"] if c in x.columns), None)
    model_col = "model" if "model" in x.columns else None
    if pnl_col is None or model_col is None:
        return "const equityLabels=[]; const equitySeries={};"

    x[pnl_col] = pd.to_numeric(x[pnl_col], errors="coerce").fillna(0)
    x = x.dropna(subset=["_date"]).sort_values("_date")

    dates = sorted(x["_date"].dt.strftime("%Y-%m-%d").unique())
    payload = {}
    for model in MODELS:
        sub = x[x[model_col] == model].copy()
        daily = sub.groupby(sub["_date"].dt.strftime("%Y-%m-%d"))[pnl_col].sum()
        equity = []
        running = 0.0
        for d in dates:
            running += float(daily.get(d, 0.0))
            equity.append(round(running * 100.0, 6))
        payload[model] = equity

    import json
    return (
        "const equityLabels=" + json.dumps(dates) + ";\n"
        "const equitySeries=" + json.dumps(payload) + ";"
    )


def build_dashboard(live_dir: Path) -> Path:
    frames = {name: read_csv(live_dir / name) for name in EXPECTED_FILES}

    universe = frames["universe.csv"]
    signals = frames["signals.csv"]
    positions = frames["positions.csv"]
    snapshots = frames["position_snapshots.csv"]
    executions = frames["executions.csv"]
    metrics = frames["daily_model_metrics.csv"]
    baseline = frames["baseline.csv"]

    lm = latest_metrics(metrics)

    frozen_date = "—"
    universe_version = "—"
    universe_count = len(universe)
    if not universe.empty:
        if "frozen_date" in universe.columns:
            vals = universe["frozen_date"].dropna().astype(str)
            if len(vals):
                frozen_date = vals.iloc[0]
        if "universe_version" in universe.columns:
            vals = universe["universe_version"].dropna().astype(str)
            if len(vals):
                universe_version = vals.iloc[0]

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    equity_js = build_equity_js(executions)

    missing = [f for f in EXPECTED_FILES if not (live_dir / f).exists()]
    missing_note = (
        '<div class="warning">Missing files: '
        + ", ".join(html.escape(x) for x in missing)
        + "</div>"
        if missing else ""
    )

    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AURA v0.4.9 — Mission Control</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
:root {{
  --bg:#0b1020; --panel:#121a2d; --panel2:#18233b; --text:#e8edf7;
  --muted:#9aa8c1; --line:#2b3956; --accent:#62a8ff; --good:#55d187;
  --warn:#f3c969; --bad:#ff7070;
}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--text);font-family:Inter,Segoe UI,Arial,sans-serif}}
header{{padding:28px 34px;border-bottom:1px solid var(--line);background:#0d1426}}
h1{{margin:0 0 8px;font-size:28px}}
h2{{font-size:18px;margin:0 0 16px}}
.sub{{color:var(--muted)}}
.wrap{{max-width:1500px;margin:auto;padding:24px}}
.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:18px}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px}}
.kpi{{font-size:25px;font-weight:700;margin-top:6px}}
.label{{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.08em}}
.panel{{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px;margin-bottom:18px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{padding:9px 8px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}}
th{{color:var(--muted);font-weight:600}}
.muted{{color:var(--muted);text-align:center}}
.warning{{padding:12px;border:1px solid #6d5a25;background:#2a2412;color:#f5dc83;border-radius:10px;margin-bottom:18px}}
.badge{{display:inline-block;padding:4px 8px;border-radius:99px;background:#173d2b;color:#79e0a1;font-size:12px}}
.chartbox{{height:330px}}
.footer{{color:var(--muted);font-size:12px;padding:12px 0 30px}}
@media(max-width:1000px){{.grid{{grid-template-columns:repeat(2,1fr)}}}}
@media(max-width:650px){{.grid{{grid-template-columns:1fr}}.wrap{{padding:12px}}}}
</style>
</head>
<body>
<header>
  <div class="wrap">
    <div class="label">AURA QUANT RESEARCH PLATFORM</div>
    <h1>v0.4.9 — Mission Control</h1>
    <div class="sub">Read-only dashboard • v0.4.8 paper laboratory • NO ORDERS</div>
  </div>
</header>

<main class="wrap">
{missing_note}

<div class="grid">
  <div class="card"><div class="label">Frozen Universe</div><div class="kpi">{universe_count}</div><div class="sub">S&P 100 securities</div></div>
  <div class="card"><div class="label">Signals</div><div class="kpi">{len(signals)}</div><div class="sub">recorded in Signal Master</div></div>
  <div class="card"><div class="label">Virtual Positions</div><div class="kpi">{len(positions)}</div><div class="sub">ledger rows</div></div>
  <div class="card"><div class="label">Executions</div><div class="kpi">{len(executions)}</div><div class="sub">paper executions</div></div>
</div>

<div class="panel">
<h2>Research Configuration</h2>
<table>
<tr><th>Signal</th><td>EMA3/EMA8 bullish crossover + MACD histogram &gt; 0 + RelVol ≥ 1</td></tr>
<tr><th>Model A</th><td>10D TIME — close on the 10th complete trading session</td></tr>
<tr><th>Model B</th><td>PURE ATR 2x/4x — 2× ATR stop / 4× ATR target, 60-session research horizon</td></tr>
<tr><th>Model C</th><td>ATR 2x/4x + 10D CLOSE</td></tr>
<tr><th>Friction</th><td>5 bp entry slippage + 1 bp commission + 5 bp exit slippage + 1 bp commission = 12 bp round trip</td></tr>
<tr><th>Universe</th><td>{html.escape(universe_version)} • frozen {html.escape(frozen_date)}</td></tr>
<tr><th>Status</th><td><span class="badge">PAPER ONLY — NO ORDERS</span></td></tr>
</table>
</div>

<div class="panel">
<h2>Model Comparison</h2>
<div style="overflow:auto">
<table>
<thead><tr>
<th>Model</th><th>Closed</th><th>Open</th><th>Net Mean</th><th>Net Median</th>
<th>Win Rate</th><th>Profit Factor</th><th>Max DD</th><th>Capital Efficiency</th>
<th>MFE Capture</th><th>Net Lift vs Baseline</th>
</tr></thead>
<tbody>{make_model_rows(lm)}</tbody>
</table>
</div>
</div>

<div class="panel">
<h2>Paper Equity Curves</h2>
<div class="chartbox"><canvas id="equity"></canvas></div>
</div>

<div class="panel">
<h2>Open / Pending Virtual Positions</h2>
<div style="overflow:auto">
<table>
<thead><tr><th>Signal ID</th><th>Model</th><th>Symbol</th><th>Entry Date</th>
<th>Entry</th><th>Stop</th><th>Target</th><th>MFE</th><th>MAE</th></tr></thead>
<tbody>{make_open_position_rows(positions)}</tbody>
</table>
</div>
</div>

<div class="panel">
<h2>Recent Signal Master Events</h2>
<div style="overflow:auto">
<table>
<thead><tr><th>Signal ID</th><th>Symbol</th><th>Decision Date</th><th>Entry Date</th>
<th>Signal Close</th><th>EMA3</th><th>RelVol</th></tr></thead>
<tbody>{make_signal_rows(signals)}</tbody>
</table>
</div>
</div>

<div class="panel">
<h2>Research Guardrails</h2>
<ul>
<li>Mission Control is read-only.</li>
<li>The Signal Master remains the single source of truth for A/B/C comparison.</li>
<li>No sector filter, indicator threshold, ATR multiplier, or exit parameter is optimized here.</li>
<li>Model B remains a 60-session research horizon; this dashboard does not alter it.</li>
<li>All three models receive the same signal and therefore differ only by exit philosophy.</li>
<li>Paper trading data must remain separate from historical training/holdout data.</li>
</ul>
</div>

<div class="footer">
Generated {generated}. AURA v0.4.9 Mission Control is an analytics layer only.
</div>
</main>

<script>
{equity_js}
const ctx=document.getElementById('equity').getContext('2d');
const datasets=Object.keys(equitySeries).map(model => ({{
  label:model, data:equitySeries[model], tension:.2, fill:false
}}));
new Chart(ctx,{{
  type:'line',
  data:{{labels:equityLabels,datasets}},
  options:{{
    responsive:true, maintainAspectRatio:false,
    interaction:{{mode:'index',intersect:false}},
    plugins:{{legend:{{labels:{{color:'#e8edf7'}}}}}},
    scales:{{x:{{ticks:{{color:'#9aa8c1'}}}},y:{{ticks:{{color:'#9aa8c1',callback:v=>v+'%'}}}}}}
  }}
}});
</script>
</body>
</html>
"""

    out = live_dir / "mission_control.html"
    out.write_text(page, encoding="utf-8")

    # Machine-readable latest summary for later automation / Experiment B.
    summary_rows = []
    for model in MODELS:
        hit = lm[lm["model"] == model] if not lm.empty and "model" in lm.columns else pd.DataFrame()
        r = hit.iloc[-1] if not hit.empty else None
        summary_rows.append({
            "model": model,
            "closed_trades": r.get("closed_trades") if r is not None else 0,
            "open_positions": r.get("open_positions") if r is not None else 0,
            "net_mean": r.get("net_mean") if r is not None else None,
            "net_median": r.get("net_median") if r is not None else None,
            "win_rate": r.get("win_rate") if r is not None else None,
            "profit_factor": r.get("profit_factor") if r is not None else None,
            "max_drawdown": r.get("max_drawdown") if r is not None else None,
            "capital_efficiency": r.get("capital_efficiency") if r is not None else None,
            "mfe_capture_ratio": r.get("mfe_capture_ratio") if r is not None else None,
            "net_lift_vs_baseline": r.get("net_lift_vs_baseline") if r is not None else None,
        })
    pd.DataFrame(summary_rows).to_csv(live_dir / "mission_control_summary.csv", index=False)

    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-dir", default="AURA_LIVE", help="Path to the AURA_LIVE directory")
    args = parser.parse_args()

    live_dir = Path(args.live_dir)
    if not live_dir.exists():
        raise SystemExit(f"AURA_LIVE directory not found: {live_dir}")

    out = build_dashboard(live_dir)
    print("AURA v0.4.9 MISSION CONTROL COMPLETE")
    print(f"Dashboard: {out}")
    print(f"Summary:   {live_dir / 'mission_control_summary.csv'}")
    print("READ ONLY — PAPER DATA ONLY — NO ORDERS")


if __name__ == "__main__":
    main()
