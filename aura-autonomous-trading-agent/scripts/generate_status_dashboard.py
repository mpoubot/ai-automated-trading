#!/usr/bin/env python3
"""
Generate the static AURA paper-chain status dashboard (docs/index.html).

Read-only. Consumes:
    regime_output/runtime/runtime_state.json          (v0.5.3.21)
    <latest cycle dir>/signal_decision.json            (v0.5.3.13)
    <latest cycle dir>/execution_safety.json           (v0.5.3.19)
    <latest cycle dir>/execution_specification.json    (v0.5.3.23, optional)
    regime_output/runtime/cycles/*                     (recent-cycle history)

It never fetches market data, never calls Alpaca, and never invents a
value for anything it can't find -- a missing input renders as "no data
yet", the same honest placeholder state this repo's sibling
(aura-alpaca-crypto-research) dashboard uses, never a fabricated number.

Output is a single self-contained HTML file (styled to match that sibling
repo's dashboard) plus docs/.nojekyll, ready for GitHub Pages served from
/docs.
"""
from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_STATE = ROOT / "regime_output" / "runtime" / "runtime_state.json"
CYCLES_DIR = ROOT / "regime_output" / "runtime" / "cycles"
DOCS_DIR = ROOT / "docs"
REQUIRED_SYMBOLS = ("BTC/USD", "ETH/USD")  # always shown, even with no data yet


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def badge(text: str, kind: str = "neutral") -> str:
    return f'<span class="badge {esc(kind)}">{esc(text)}</span>'


def symbol_card(symbol: str, signal: dict | None, safety: dict | None, spec: dict | None) -> str:
    signal_item = (signal or {}).get("decisions", {}).get(symbol) if signal else None
    safety_item = (safety or {}).get("decisions", {}).get(symbol) if safety else None
    spec_item = (spec or {}).get("decisions", {}).get(symbol) if spec else None

    regime_state = signal_item.get("regime_state") if signal_item else None
    signal_decision = signal_item.get("decision") if signal_item else None
    safety_authorized = safety_item.get("execution_authorized") if safety_item else None
    safety_reason = safety_item.get("reason") if safety_item else None
    spec_status = spec_item.get("status") if spec_item else None
    spec_reason = spec_item.get("reason") if spec_item else None
    spec_side = None
    spec_qty = None
    if spec_item and spec_item.get("status") == "EXECUTION_SPEC_READY":
        es = spec_item.get("execution_specification", {})
        spec_side = es.get("side")
        spec_qty = es.get("quantity")

    spec_kind = "safe" if spec_status == "EXECUTION_SPEC_READY" else "warn"

    rows = [
        ("Regime state (.13)", regime_state or "—"),
        ("Signal decision (.13)", signal_decision or "—"),
        ("Execution authorized (.19)", "TRUE" if safety_authorized else "FALSE"),
        ("Safety reason (.19)", safety_reason or "—"),
        ("Execution spec (.23)", spec_status or "NOT RUN"),
        ("Spec reason (.23)", spec_reason or "—"),
    ]
    if spec_side:
        rows.append(("Side", spec_side))
        rows.append(("Quantity", spec_qty))

    row_html = "".join(
        f'<tr><td class="label">{esc(k)}</td><td>{esc(v)}</td></tr>' for k, v in rows
    )

    return f"""
<div class="card">
  <h2>{esc(symbol)} {badge(spec_status or "NOT RUN", spec_kind)}</h2>
  <table>{row_html}</table>
</div>"""


def recent_cycles_rows(limit: int = 20) -> str:
    if not CYCLES_DIR.exists():
        return '<tr><td colspan="4">No cycles recorded yet.</td></tr>'
    cycle_dirs = sorted((d for d in CYCLES_DIR.iterdir() if d.is_dir()), reverse=True)[:limit]
    if not cycle_dirs:
        return '<tr><td colspan="4">No cycles recorded yet.</td></tr>'

    rows = []
    for cycle_dir in cycle_dirs:
        safety = load_json(cycle_dir / "execution_safety.json")
        spec = load_json(cycle_dir / "execution_specification.json")
        safety_status = safety.get("overall_execution_safety") if safety else "—"
        spec_status = spec.get("overall_status") if spec else "NOT RUN"
        kill_switch = safety.get("kill_switch_active") if safety else None
        rows.append(
            f"<tr><td>{esc(cycle_dir.name)}</td>"
            f"<td>{esc(safety_status)}</td>"
            f"<td>{esc('ACTIVE' if kill_switch else 'off' if kill_switch is not None else '—')}</td>"
            f"<td>{esc(spec_status)}</td></tr>"
        )
    return "".join(rows)


def render(runtime_state: dict | None, signal: dict | None, safety: dict | None, spec: dict | None) -> str:
    generated_at = datetime.now(timezone.utc).isoformat()

    if runtime_state is None:
        status_banner = (
            '<div class="card"><h2>Live test status</h2>'
            '<p class="warn">No runtime cycle has been published yet. Once '
            "aura_v05321_alpaca_paper_runtime.py runs (via the GitHub Actions "
            "workflow or locally), this dashboard will report real signal, "
            "safety, and execution-specification state per cycle.</p></div>"
        )
        cards = ""
        top_metrics = ""
    else:
        cycle_id = runtime_state.get("cycle_id", "—")
        run_status = runtime_state.get("status", "—")
        kill_switch = runtime_state.get("kill_switch_active")
        exec_authorized = runtime_state.get("execution_authorized")
        run_kind = "safe" if run_status == "PASS" else "warn"

        top_metrics = f"""
<div class="grid">
<div class="card"><div class="label">Last cycle</div><div class="metric">{esc(cycle_id)}</div></div>
<div class="card"><div class="label">Runtime status</div><div class="metric">{badge(run_status, run_kind)}</div></div>
<div class="card"><div class="label">Kill switch</div><div class="metric">{badge('ACTIVE' if kill_switch else 'off', 'warn' if kill_switch else 'safe')}</div></div>
<div class="card"><div class="label">Execution authorized</div><div class="metric">{badge('TRUE' if exec_authorized else 'FALSE', 'warn' if exec_authorized else 'safe')}</div></div>
</div>"""
        status_banner = ""
        # Discovered-universe symbol list: any symbol any upstream payload
        # actually reported for this cycle, unioned with REQUIRED_SYMBOLS so
        # BTC/USD and ETH/USD always render a card even if this particular
        # cycle happened to produce no decision entry for one of them (e.g.
        # a required-symbol BLOCKED outcome with an empty decisions dict).
        # This mirrors the required-vs-optional symbol pattern used
        # throughout the v0.5.3 pipeline (.13-.19, .23): required symbols
        # are always shown, every other discovered symbol is shown too.
        discovered_symbols = sorted(
            set((signal or {}).get("decisions", {}).keys())
            | set((safety or {}).get("decisions", {}).keys())
            | set((spec or {}).get("decisions", {}).keys())
            | set(REQUIRED_SYMBOLS)
        )
        cards = "".join(symbol_card(s, signal, safety, spec) for s in discovered_symbols)

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AURA Paper-Chain Status</title>
<style>
body{{font-family:system-ui,-apple-system,Segoe UI,sans-serif;max-width:1200px;margin:35px auto;padding:0 20px;background:#0b1020;color:#eef2ff}}
a{{color:#8ab4ff}}
.card{{background:#151c30;border:1px solid #2b3655;border-radius:14px;padding:20px;margin:16px 0}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}}
.metric{{font-size:22px;font-weight:700}}
.label{{font-size:12px;color:#9aa8c7;text-transform:uppercase;letter-spacing:.06em}}
table{{width:100%;border-collapse:collapse}}
th,td{{padding:8px 9px;border-bottom:1px solid #29334f;text-align:left;font-size:14px}}
td.label{{color:#9aa8c7;width:45%}}
.badge{{display:inline-block;padding:4px 10px;border-radius:999px;font-size:13px;font-weight:600}}
.badge.safe{{background:#123424;color:#86efac}}
.badge.warn{{background:#3a2c10;color:#fbbf24}}
.badge.neutral{{background:#263451;color:#cbd5e1}}
</style></head>
<body>
<p><a href="https://github.com/mpoubot/aura-autonomous-trading-agent">← AURA repository</a></p>
<h1>AURA Paper-Chain Status</h1>
<p>v0.5.3 chain • Alpaca crypto (full discovered coin universe; BTC/USD, ETH/USD always required) • PAPER account • {badge('NO LIVE EXECUTION', 'safe')}</p>

{top_metrics}
{status_banner}
{cards}

<div class="card"><h2>Safety boundaries (unchanged by this dashboard)</h2>
<ul>
<li>.19 Execution Safety hard-codes execution_authorized = False on every run (research-only), untouched here.</li>
<li>.23's direction allowlists are empty by default. BEAR never maps to SELL. See
<a href="https://github.com/mpoubot/aura-autonomous-trading-agent/blob/paper/runtime-alpaca-2026-09-01/docs/AURA_V0531622_EXECUTION_SPECIFICATION_BRIDGE_2026-09-02.md">the contract writeup</a>.</li>
<li>Quantity is never fabricated. Without an explicit --sizing-config, every symbol fails closed (MISSING_QUANTITY_SOURCE).</li>
<li>This page only reads and renders already-published JSON. It has no ability to place an order.</li>
</ul></div>

<div class="card"><h2>Recent cycles</h2>
<table><tr><th>Cycle</th><th>Safety verdict</th><th>Kill switch</th><th>Execution spec</th></tr>
{recent_cycles_rows()}
</table></div>

<p style="color:#6b7896;font-size:12px">Generated {esc(generated_at)} by scripts/generate_status_dashboard.py</p>
</body></html>
"""


def main() -> int:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / ".nojekyll").touch()

    runtime_state = load_json(RUNTIME_STATE)
    signal = safety = spec = None
    if runtime_state and runtime_state.get("cycle_directory"):
        cycle_dir = Path(runtime_state["cycle_directory"])
        signal = load_json(cycle_dir / "signal_decision.json")
        safety = load_json(cycle_dir / "execution_safety.json")
        spec = load_json(cycle_dir / "execution_specification.json")

    page = render(runtime_state, signal, safety, spec)
    (DOCS_DIR / "index.html").write_text(page, encoding="utf-8")
    print(f"Wrote {DOCS_DIR / 'index.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
