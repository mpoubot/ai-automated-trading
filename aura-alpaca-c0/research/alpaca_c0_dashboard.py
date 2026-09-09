"""AURA Alpaca C0 read-only backtest dashboard generator.

Consumes the deterministic outputs written by research/alpaca_c0_backtest.py and
creates a standalone dashboard.html. No market-data calls and no order execution.
"""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Iterable

import pandas as pd


REQUIRED_TRADE_COLUMNS = {
    "trade_id", "symbol", "entry_timestamp", "exit_timestamp", "net_return",
    "mfe", "mae", "funding_return", "hold_bars", "exit_reason",
}


def load_run(input_dir: Path) -> tuple[dict, pd.DataFrame]:
    summary_path = input_dir / "summary.json"
    trades_path = input_dir / "trades_c0.csv"
    if not summary_path.exists():
        raise FileNotFoundError(f"Missing {summary_path}")
    if not trades_path.exists():
        raise FileNotFoundError(f"Missing {trades_path}")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    trades = pd.read_csv(trades_path)
    missing = sorted(REQUIRED_TRADE_COLUMNS - set(trades.columns))
    if missing:
        raise ValueError(f"trades_c0.csv missing required columns: {', '.join(missing)}")
    for col in ["net_return", "mfe", "mae", "funding_return"]:
        trades[col] = pd.to_numeric(trades[col], errors="coerce")
    return summary, trades


def pct(value: object) -> str:
    try:
        return f"{float(value) * 100:.3f}%"
    except (TypeError, ValueError):
        return "—"


def num(value: object, digits: int = 4) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def svg_line(values: Iterable[float], width: int = 900, height: int = 240, label: str = "") -> str:
    vals = [float(v) for v in values]
    if not vals:
        return '<div class="empty">No data</div>'
    lo, hi = min(vals), max(vals)
    span = hi - lo or 1.0
    pad = 18
    points = []
    for i, value in enumerate(vals):
        x = pad + (width - 2 * pad) * (i / max(1, len(vals) - 1))
        y = height - pad - (height - 2 * pad) * ((value - lo) / span)
        points.append(f"{x:.1f},{y:.1f}")
    zero_y = None
    if lo <= 0 <= hi:
        zero_y = height - pad - (height - 2 * pad) * ((0 - lo) / span)
    zero = f'<line x1="{pad}" y1="{zero_y:.1f}" x2="{width-pad}" y2="{zero_y:.1f}" class="zero" />' if zero_y is not None else ""
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(label)}" '
        f'class="chart"><rect width="100%" height="100%" class="chart-bg"/>{zero}'
        f'<polyline points="{" ".join(points)}" class="line" fill="none"/></svg>'
    )


def table_html(frame: pd.DataFrame, columns: list[str], max_rows: int = 100) -> str:
    view = frame[columns].head(max_rows).copy()
    for col in view.columns:
        if col.endswith("_return") or col in {"net_return", "mfe", "mae"}:
            view[col] = view[col].map(pct)
    return view.to_html(index=False, classes="data", border=0, escape=True)


def build_dashboard(input_dir: Path, output_dir: Path) -> Path:
    summary, trades = load_run(input_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    ordered = trades.sort_values("entry_timestamp").reset_index(drop=True)
    equity = (1 + ordered["net_return"].fillna(0)).cumprod()
    drawdown = equity / equity.cummax() - 1
    symbol = (
        trades.groupby("symbol", dropna=False)
        .agg(trades=("trade_id", "count"), net_mean=("net_return", "mean"),
             win_rate=("net_return", lambda s: float((s > 0).mean())),
             mean_mfe=("mfe", "mean"), mean_mae=("mae", "mean"))
        .reset_index()
        .sort_values("net_mean", ascending=False)
    )
    latest = ordered.iloc[-1] if not ordered.empty else None
    title = "AURA Alpaca C0 Backtest Dashboard"
    kpis = [
        ("Trades", summary.get("trades", len(trades))),
        ("Win rate", pct(summary.get("win_rate"))),
        ("Net mean", pct(summary.get("net_mean"))),
        ("Profit factor", num(summary.get("profit_factor"), 3)),
        ("Max drawdown", pct(summary.get("max_drawdown"))),
        ("Mean MFE", pct(summary.get("mean_mfe"))),
        ("Mean MAE", pct(summary.get("mean_mae"))),
        ("Funding model", summary.get("funding_model", "—")),
    ]
    cards = "".join(f'<div class="card"><div class="label">{html.escape(str(k))}</div><div class="value">{html.escape(str(v))}</div></div>' for k, v in kpis)
    symbol_view = symbol.copy()
    for c in ["net_mean", "win_rate", "mean_mfe", "mean_mae"]:
        symbol_view[c] = symbol_view[c].map(pct)
    symbol_table = symbol_view.to_html(index=False, classes="data", border=0, escape=True)
    trade_cols = ["trade_id", "symbol", "entry_timestamp", "exit_timestamp", "net_return", "mfe", "mae", "funding_return", "hold_bars", "exit_reason"]
    trades_table = table_html(ordered.sort_values("entry_timestamp", ascending=False), trade_cols)
    last_trade = "—" if latest is None else f"{latest['symbol']} • {latest['entry_timestamp']} → {latest['exit_timestamp']}"

    doc = f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
body{{margin:0;background:#0b1020;color:#e8edf7;font-family:Segoe UI,Arial,sans-serif}}
.wrap{{max-width:1400px;margin:auto;padding:24px}} .muted{{color:#9aa8c1}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}}
.card,.panel{{background:#121a2d;border:1px solid #2b3956;border-radius:12px;padding:16px;margin-bottom:16px}}
.label{{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:#9aa8c1}} .value{{font-size:22px;font-weight:700;margin-top:6px}}
.data{{border-collapse:collapse;width:100%;font-size:13px}} .data th,.data td{{padding:8px;border-bottom:1px solid #2b3956;text-align:left}} .data th{{color:#9aa8c1}}
.chart{{width:100%;height:auto;display:block}} .chart-bg{{fill:#0e1628}} .line{{stroke:#7dd3fc;stroke-width:2.5}} .zero{{stroke:#59657d;stroke-width:1;stroke-dasharray:4 4}}
.warning{{background:#2b2513;border:1px solid #7b6222;padding:12px;border-radius:10px}}
.empty{{color:#9aa8c1;padding:12px 0}} code{{color:#b7e3ff}}
</style></head><body><div class="wrap">
<div class="panel"><div class="label">AURA Research • Read only</div><h1>{html.escape(title)}</h1>
<p class="muted">Generated from <code>{html.escape(str(input_dir))}</code>. No orders are placed by this dashboard.</p>
<div class="warning">This dashboard is a diagnostic view. It does not optimize parameters, change the C0 strategy, or connect to an execution adapter.</div></div>
<div class="grid">{cards}</div>
<div class="panel"><h2>Equity curve</h2>{svg_line(equity.tolist(), label="Cumulative equity")}</div>
<div class="panel"><h2>Drawdown</h2>{svg_line(drawdown.tolist(), label="Drawdown")}</div>
<div class="panel"><h2>Symbol breakdown</h2>{symbol_table if not symbol.empty else '<div class="empty">No trades.</div>'}</div>
<div class="panel"><h2>Run context</h2><p>Latest trade: {html.escape(last_trade)}</p><pre>{html.escape(json.dumps(summary, indent=2, default=str))}</pre></div>
<div class="panel"><h2>Trades</h2>{trades_table if not ordered.empty else '<div class="empty">No trades.</div>'}</div>
</div></body></html>"""
    path = output_dir / "dashboard.html"
    path.write_text(doc, encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="AURA_ALPACA_C0")
    parser.add_argument("--output", default="AURA_ALPACA_C0_DASHBOARD")
    args = parser.parse_args()
    path = build_dashboard(Path(args.input), Path(args.output))
    print(f"dashboard={path.resolve()}")


if __name__ == "__main__":
    main()
