#!/usr/bin/env python3
"""
AURA v0.5.1.1 — Crypto Path Diagnostic Analysis

Research-only diagnostic layer.
- Reads early_path_diagnostics.csv produced by AURA v0.5.1.
- Does NOT alter C0.
- Does NOT optimize or select a threshold.
- Produces descriptive distributions, Pearson/Spearman correlations,
  winner/loser separation, fixed threshold diagnostics, and HTML report.
"""

from __future__ import annotations
import argparse
import html
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd


PERCENTILES = [0.10, 0.25, 0.50, 0.75, 0.90]
THRESHOLDS = [-0.005, -0.010, -0.015, -0.020, -0.025]

EARLY_VARS = [
    "bar_1_mfe", "bar_1_mae", "bar_1_close_return_before_costs",
    "bar_2_mfe", "bar_2_mae", "bar_2_close_return_before_costs",
    "bar_3_mfe", "bar_3_mae", "bar_3_close_return_before_costs",
]


def pearson(x, y):
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    mask = np.isfinite(x) & np.isfinite(y)
    x, y = x[mask], y[mask]
    if len(x) < 3 or np.std(x) == 0 or np.std(y) == 0:
        return np.nan
    return float(np.corrcoef(x, y)[0, 1])


def rankdata_average(a):
    """Average ranks, implemented without scipy."""
    a = np.asarray(a, dtype=float)
    order = np.argsort(a, kind="mergesort")
    ranks = np.empty(len(a), dtype=float)
    i = 0
    while i < len(a):
        j = i + 1
        while j < len(a) and a[order[j]] == a[order[i]]:
            j += 1
        avg_rank = (i + 1 + j) / 2.0
        ranks[order[i:j]] = avg_rank
        i = j
    return ranks


def spearman(x, y):
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    mask = np.isfinite(x) & np.isfinite(y)
    x, y = x[mask], y[mask]
    if len(x) < 3:
        return np.nan
    return pearson(rankdata_average(x), rankdata_average(y))


def pct(v):
    return f"{v * 100:.3f}%" if pd.notna(v) else ""


def pct_or_blank(v):
    return "" if pd.isna(v) else f"{v * 100:.3f}%"


def main():
    ap = argparse.ArgumentParser(description="AURA v0.5.1.1 Crypto Path Diagnostic")
    ap.add_argument("--input", required=True, help="v0.5.1 output directory")
    ap.add_argument("--output", required=True, help="v0.5.1.1 output directory")
    args = ap.parse_args()

    inp = Path(args.input)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)

    src = inp / "early_path_diagnostics.csv"
    if not src.exists():
        raise SystemExit(f"Missing required file: {src}")

    df = pd.read_csv(src)
    required = ["trade_id", "symbol", "net_return"] + EARLY_VARS
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise SystemExit(f"Missing columns: {missing}")

    for c in ["net_return"] + EARLY_VARS:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df["outcome"] = np.where(df["net_return"] > 0, "WINNER",
                             np.where(df["net_return"] < 0, "LOSER", "FLAT"))
    analysis_df = df[df["outcome"].isin(["WINNER", "LOSER"])].copy()

    # 1) Distributions: variable x outcome x percentile
    rows = []
    for var in EARLY_VARS:
        for outcome in ["WINNER", "LOSER", "ALL"]:
            s = analysis_df[var] if outcome == "ALL" else analysis_df.loc[analysis_df["outcome"] == outcome, var]
            for p in PERCENTILES:
                rows.append({
                    "variable": var,
                    "outcome": outcome,
                    "percentile": int(p * 100),
                    "value": s.quantile(p) if len(s) else np.nan,
                    "n": int(s.notna().sum()),
                })
    pd.DataFrame(rows).to_csv(out / "path_distributions.csv", index=False)

    # 2) Correlations with final net return
    corr_rows = []
    for var in EARLY_VARS:
        x = analysis_df[var]
        y = analysis_df["net_return"]
        corr_rows.append({
            "variable": var,
            "target": "net_return",
            "n": int((x.notna() & y.notna()).sum()),
            "pearson_r": pearson(x, y),
            "spearman_rho": spearman(x, y),
        })
    corr = pd.DataFrame(corr_rows).sort_values("pearson_r", ascending=False)
    corr.to_csv(out / "path_correlations.csv", index=False)

    # 3) Winner/loser separation and progression diagnostics
    wl_rows = []
    for var in EARLY_VARS:
        for outcome in ["WINNER", "LOSER"]:
            s = analysis_df.loc[analysis_df["outcome"] == outcome, var]
            wl_rows.append({
                "metric": "mean",
                "variable": var,
                "outcome": outcome,
                "value": s.mean(),
                "n": int(s.notna().sum()),
            })
            wl_rows.append({
                "metric": "median",
                "variable": var,
                "outcome": outcome,
                "value": s.median(),
                "n": int(s.notna().sum()),
            })

    valid = analysis_df.dropna(subset=["bar_1_mfe", "bar_3_mfe"]).copy()
    valid["bar3_mfe_gt_bar1_mfe"] = valid["bar_3_mfe"] > valid["bar_1_mfe"]
    valid["bar3_mfe_gt_2x_bar1_mfe"] = valid["bar_3_mfe"] > (2.0 * valid["bar_1_mfe"])

    for rule in ["bar3_mfe_gt_bar1_mfe", "bar3_mfe_gt_2x_bar1_mfe"]:
        for outcome in ["WINNER", "LOSER"]:
            s = valid.loc[valid["outcome"] == outcome, rule]
            wl_rows.append({
                "metric": rule,
                "variable": rule,
                "outcome": outcome,
                "value": float(s.mean()) if len(s) else np.nan,
                "n": int(len(s)),
            })

    pd.DataFrame(wl_rows).to_csv(out / "path_winner_loser.csv", index=False)

    # 4) Fixed descriptive MAE thresholds. No optimization/selection.
    threshold_rows = []
    winners = analysis_df["outcome"] == "WINNER"
    losers = analysis_df["outcome"] == "LOSER"
    n_w = int(winners.sum())
    n_l = int(losers.sum())

    for bar in [1, 2, 3]:
        mae_col = f"bar_{bar}_mae"
        for t in THRESHOLDS:
            trigger = analysis_df[mae_col] <= t
            w_trigger = int((trigger & winners).sum())
            l_trigger = int((trigger & losers).sum())
            threshold_rows.append({
                "bar": bar,
                "mae_threshold": t,
                "threshold_label": pct(t),
                "winner_n": n_w,
                "loser_n": n_l,
                "winner_trigger_n": w_trigger,
                "loser_trigger_n": l_trigger,
                "false_negative_rate_winners": w_trigger / n_w if n_w else np.nan,
                "loser_trigger_rate": l_trigger / n_l if n_l else np.nan,
                "winner_survival_rate": 1 - (w_trigger / n_w) if n_w else np.nan,
                "loser_survival_rate": 1 - (l_trigger / n_l) if n_l else np.nan,
                "interpretation": "DESCRIPTIVE ONLY — NOT OPTIMIZED",
            })
    pd.DataFrame(threshold_rows).to_csv(out / "path_threshold_diagnostics.csv", index=False)

    # 5) Run metadata / summary
    summary = {
        "engine": "AURA v0.5.1.1",
        "mode": "RESEARCH ONLY — NO STRATEGY CHANGE — NO ORDERS",
        "input": str(src),
        "trades_total": int(len(df)),
        "completed_nonflat_trades": int(len(analysis_df)),
        "winners": n_w,
        "losers": n_l,
        "symbols": sorted(df["symbol"].dropna().unique().tolist()),
        "fixed_thresholds": THRESHOLDS,
        "percentiles": PERCENTILES,
        "guardrail": "Thresholds are descriptive diagnostics only; no threshold is selected.",
        "top_pearson": corr.iloc[0]["variable"] if len(corr) else None,
        "top_pearson_r": float(corr.iloc[0]["pearson_r"]) if len(corr) else None,
    }
    (out / "research_run.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    # 6) HTML report
    dist = pd.read_csv(out / "path_distributions.csv")
    thresh = pd.read_csv(out / "path_threshold_diagnostics.csv")
    wl = pd.read_csv(out / "path_winner_loser.csv")

    # compact HTML tables
    corr_html = corr.copy()
    for c in ["pearson_r", "spearman_rho"]:
        corr_html[c] = corr_html[c].map(lambda x: f"{x:.4f}" if pd.notna(x) else "")
    corr_html = corr_html.to_html(index=False, classes="data", border=0)

    dist_show = dist.copy()
    dist_show["value"] = dist_show["value"].map(pct_or_blank)
    dist_show = dist_show.to_html(index=False, classes="data", border=0)

    thresh_show = thresh.copy()
    for c in ["mae_threshold", "false_negative_rate_winners", "loser_trigger_rate",
              "winner_survival_rate", "loser_survival_rate"]:
        if c in thresh_show:
            thresh_show[c] = thresh_show[c].map(pct_or_blank)
    thresh_show = thresh_show.to_html(index=False, classes="data", border=0)

    wl_show = wl.copy()
    wl_show["value"] = wl_show["value"].map(pct_or_blank)
    wl_show = wl_show.to_html(index=False, classes="data", border=0)

    css = """
    body{font-family:Arial,sans-serif;background:#0b1220;color:#e8eefc;margin:0;padding:32px}
    .wrap{max-width:1500px;margin:auto}.card{background:#121c30;border:1px solid #293753;
    border-radius:14px;padding:24px;margin:18px 0}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
    .metric{background:#0e1729;border:1px solid #293753;border-radius:10px;padding:16px}
    .metric b{font-size:25px;display:block;margin-top:6px}h1{margin-bottom:6px}
    h2{margin-top:0}table{width:100%;border-collapse:collapse;font-size:13px}
    th,td{padding:8px;border-bottom:1px solid #293753;text-align:left}th{color:#9eb4d9}
    .guard{border-left:4px solid #e3b341;background:#211d0d;padding:14px;border-radius:8px}
    .small{color:#9eb4d9}.positive{color:#73d39b}
    @media(max-width:900px){.grid{grid-template-columns:repeat(2,1fr)}}
    """
    top_var = html.escape(str(summary["top_pearson"]))
    top_r = summary["top_pearson_r"]
    html_doc = f"""<!doctype html><html><head><meta charset="utf-8">
    <title>AURA v0.5.1.1 Path Diagnostic</title><style>{css}</style></head>
    <body><div class="wrap">
    <div class="card"><div class="small">AURA CRYPTO RESEARCH</div>
    <h1>v0.5.1.1 — Path Diagnostic Analysis</h1>
    <div class="small">Read-only • C0 unchanged • no orders • no threshold selected</div></div>
    <div class="grid">
      <div class="metric">TRADES<b>{len(analysis_df)}</b></div>
      <div class="metric">WINNERS<b>{n_w}</b></div>
      <div class="metric">LOSERS<b>{n_l}</b></div>
      <div class="metric">TOP PEARSON<b>{top_r:.3f}</b></div>
    </div>
    <div class="card"><h2>Interpretation Guardrail</h2>
    <div class="guard"><b>DESCRIPTIVE RESEARCH ONLY.</b>
    Fixed MAE levels are reported but not optimized or selected. Correlation is not proof of
    causal or out-of-sample predictive power. Any future rule must be pre-registered and tested
    on genuinely unseen data.</div></div>
    <div class="card"><h2>Correlation Matrix</h2>
    <p class="small">Strongest Pearson variable in this sample: <b>{top_var}</b> (r={top_r:.4f}).</p>
    {corr_html}</div>
    <div class="card"><h2>MFE / MAE / Close Return Distributions</h2>{dist_show}</div>
    <div class="card"><h2>Winner / Loser Separation</h2>{wl_show}</div>
    <div class="card"><h2>Fixed MAE Threshold Diagnostics</h2>{thresh_show}</div>
    </div></body></html>"""

    (out / "path_diagnostic_report.html").write_text(html_doc, encoding="utf-8")

    print("=" * 78)
    print("AURA v0.5.1.1 — CRYPTO PATH DIAGNOSTIC")
    print("=" * 78)
    print("MODE            : RESEARCH ONLY — NO STRATEGY CHANGE — NO ORDERS")
    print(f"INPUT           : {src}")
    print(f"COMPLETED TRADES: {len(analysis_df)}")
    print(f"WINNERS         : {n_w}")
    print(f"LOSERS          : {n_l}")
    print(f"TOP PEARSON     : {top_var}  r={top_r:.4f}" if top_r is not None else "TOP PEARSON     : n/a")
    print("Fixed thresholds : -0.5%, -1.0%, -1.5%, -2.0%, -2.5%")
    print("GUARDRAIL        : DESCRIPTIVE ONLY — NO THRESHOLD SELECTED")
    print()
    print("Files:")
    for name in [
        "path_correlations.csv",
        "path_distributions.csv",
        "path_winner_loser.csv",
        "path_threshold_diagnostics.csv",
        "path_diagnostic_report.html",
        "research_run.json",
    ]:
        print(f"  {out / name}")
    print()
    print("AURA v0.5.1.1 COMPLETE — NO STRATEGY CHANGE")


if __name__ == "__main__":
    main()
