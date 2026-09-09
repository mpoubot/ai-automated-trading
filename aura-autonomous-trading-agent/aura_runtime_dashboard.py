#!/usr/bin/env python3
"""
AURA — paper-runtime monitoring dashboard.

READ-ONLY. Never places an order, never calls Alpaca, never writes to any
pipeline file. This script only reads what .21's cycle() loop has already
written under regime_output/runtime/ and renders it as a single offline
HTML page.

Purpose
-------
.21 (aura_v05321_alpaca_paper_runtime.py) archives every cycle's full chain
output (.12 market state -> .13 signal decision -> .14 risk gate -> .15
position state -> .16 paper execution -> .17 ledger -> .20 simulated fill
-> .18 reconciliation -> .19 execution safety / kill switch) under
regime_output/runtime/cycles/<cycle_id>/*.json, plus the latest summary in
regime_output/runtime/runtime_state.json. That is a lot of raw JSON to read
by hand after every run. This script turns it into one HTML page: a summary
of the most recent cycle, an aggregate view of how often each regime/
decision/gate outcome has occurred across history, and a per-cycle table
for drilling into any specific run.

It does not interpret or judge the results (e.g. it never suggests a coin
should be promoted into live signal generation) -- it just makes what the
pipeline already decided visible.

Usage
-----
    python aura_runtime_dashboard.py
    python aura_runtime_dashboard.py --runtime-dir regime_output/runtime --output regime_output/runtime/dashboard.html
    python aura_runtime_dashboard.py --max-cycles-detail 300

Re-run after any batch of cycles (or after --loop-seconds has been running
for a while) and open the output HTML file in a browser. Nothing here is
live -- it is a static snapshot of files already on disk.
"""
from __future__ import annotations

import argparse
import html
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DEFAULT_RUNTIME_DIR = ROOT / "regime_output" / "runtime"

STAGE_FILES = {
    "market_state": "market_state.json",
    "signal_decision": "signal_decision.json",
    "risk_gate": "risk_gate.json",
    "position_state": "position_state.json",
    "paper_execution": "paper_execution.json",
    "ledger": "decision_execution_ledger.json",
    "observed_execution": "observed_execution.json",
    "reconciliation": "reconciliation.json",
    "execution_safety": "execution_safety.json",
}

# Cycle directories are named "%Y%m%dT%H%M%SZ" by .21's cycle(). This lets us
# both sort chronologically by name and recover a real timestamp for display.
CYCLE_ID_FORMAT = "%Y%m%dT%H%M%SZ"


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _parse_cycle_timestamp(cycle_id: str) -> datetime | None:
    try:
        return datetime.strptime(cycle_id, CYCLE_ID_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def load_cycle(cycle_dir: Path) -> dict[str, Any]:
    """Read every stage file present in one cycle directory and reduce it to
    a compact record. Never raises -- a cycle interrupted mid-write (e.g. the
    process was killed between stages) is reported as INCOMPLETE rather than
    crashing the whole dashboard."""
    cycle_id = cycle_dir.name
    ts = _parse_cycle_timestamp(cycle_id)

    raw = {name: _load_json(cycle_dir / filename) for name, filename in STAGE_FILES.items()}
    present = {name: (doc is not None) for name, doc in raw.items()}
    missing = [name for name, ok in present.items() if not ok]

    record: dict[str, Any] = {
        "cycle_id": cycle_id,
        "timestamp": ts.isoformat() if ts else None,
        "missing_stages": missing,
        "complete": not missing,
    }

    ms = raw["market_state"]
    if ms:
        record["market_state_valid"] = ms.get("market_state_valid")
        record["symbols"] = {}
        for sym, sdata in (ms.get("symbols") or {}).items():
            state = (sdata or {}).get("market_state") or {}
            record["symbols"][sym] = {
                "data_status": sdata.get("data_status"),
                "market_state_valid": sdata.get("market_state_valid"),
                "regime_state": state.get("regime_state"),
                "candidate_match": state.get("candidate_match"),
                "atr_regime": state.get("atr_regime"),
                "trend_regime": state.get("trend_regime"),
                "bar_2_regime": state.get("bar_2_regime"),
            }

    sd = raw["signal_decision"]
    if sd:
        record["overall_decision"] = sd.get("overall_decision")
        record["signal_decisions"] = {
            sym: d.get("decision") for sym, d in (sd.get("decisions") or {}).items()
        }

    rg = raw["risk_gate"]
    if rg:
        record["overall_risk_decision"] = rg.get("overall_risk_decision")
        record["risk_authorized"] = rg.get("risk_authorized")
        record["risk_blocked_reasons"] = rg.get("blocked_reasons") or []

    ps = raw["position_state"]
    if ps:
        record["overall_position_state"] = ps.get("overall_position_state")

    pe = raw["paper_execution"]
    if pe:
        record["overall_execution_decision"] = pe.get("overall_execution_decision")
        record["paper_execution_allowed"] = pe.get("paper_execution_allowed")

    led = raw["ledger"]
    if led:
        record["overall_ledger_decision"] = led.get("overall_ledger_decision")

    oe = raw["observed_execution"]
    if oe:
        record["observed_order_statuses"] = {
            sym: d.get("order_status") for sym, d in (oe.get("observations") or {}).items()
        }

    rc = raw["reconciliation"]
    if rc:
        record["overall_reconciliation"] = rc.get("overall_reconciliation")

    es = raw["execution_safety"]
    if es:
        record["overall_execution_safety"] = es.get("overall_execution_safety")
        record["execution_authorized"] = es.get("execution_authorized")
        record["paper_execution_authorized"] = es.get("paper_execution_authorized")
        record["live_execution_authorized"] = es.get("live_execution_authorized")
        record["kill_switch_active"] = es.get("kill_switch_active")
        record["safety_blocked_reasons"] = es.get("blocked_reasons") or []

    return record


def discover_cycles(runtime_dir: Path) -> list[dict[str, Any]]:
    cycles_dir = runtime_dir / "cycles"
    if not cycles_dir.is_dir():
        return []
    dirs = [d for d in cycles_dir.iterdir() if d.is_dir()]
    # Sort chronologically by parsed timestamp where possible; anything that
    # doesn't match the expected cycle_id format (e.g. a manual test/probe
    # directory) sorts last by name so it doesn't silently disappear.
    def sort_key(d: Path):
        ts = _parse_cycle_timestamp(d.name)
        return (0, ts) if ts else (1, d.name)

    dirs.sort(key=sort_key)
    return [load_cycle(d) for d in dirs]


def build_aggregates(cycles: list[dict[str, Any]]) -> dict[str, Any]:
    agg: dict[str, Any] = {
        "total_cycles": len(cycles),
        "complete_cycles": sum(1 for c in cycles if c.get("complete")),
        "incomplete_cycles": sum(1 for c in cycles if not c.get("complete")),
        "kill_switch_active_count": sum(1 for c in cycles if c.get("kill_switch_active") is True),
        "kill_switch_inactive_count": sum(
            1 for c in cycles if c.get("kill_switch_active") is False
        ),
        "execution_authorized_count": sum(1 for c in cycles if c.get("execution_authorized") is True),
        "market_state_invalid_count": sum(
            1 for c in cycles if c.get("market_state_valid") is False
        ),
        "stage_status_counts": {},
        "regime_state_counts": defaultdict(Counter),
        "candidate_match_counts": defaultdict(lambda: {"true": 0, "false": 0}),
        "signal_decision_counts": defaultdict(Counter),
        "safety_blocked_reason_counts": Counter(),
    }

    stage_overall_fields = {
        "market_state_valid": "Market state valid",
        "overall_decision": "Signal decision (.13)",
        "overall_risk_decision": "Risk gate (.14)",
        "overall_position_state": "Position state (.15)",
        "overall_execution_decision": "Paper execution (.16)",
        "overall_ledger_decision": "Ledger (.17)",
        "overall_reconciliation": "Reconciliation (.18)",
        "overall_execution_safety": "Execution safety (.19)",
    }
    for field, label in stage_overall_fields.items():
        counts = Counter(str(c.get(field)) for c in cycles if field in c)
        agg["stage_status_counts"][label] = counts

    for c in cycles:
        for sym, sdata in (c.get("symbols") or {}).items():
            regime = sdata.get("regime_state")
            if regime:
                agg["regime_state_counts"][sym][regime] += 1
            match = sdata.get("candidate_match")
            if match is True:
                agg["candidate_match_counts"][sym]["true"] += 1
            elif match is False:
                agg["candidate_match_counts"][sym]["false"] += 1
        for sym, decision in (c.get("signal_decisions") or {}).items():
            if decision:
                agg["signal_decision_counts"][sym][decision] += 1
        for reason in c.get("safety_blocked_reasons") or []:
            agg["safety_blocked_reason_counts"][reason] += 1

    return agg


# ------------------------------------------------------------------------
# HTML rendering (stdlib only -- no template engine, no external assets, so
# this works fully offline).
# ------------------------------------------------------------------------

def esc(x: Any) -> str:
    return html.escape("" if x is None else str(x))


def badge(value: Any, good: set[str] | None = None, bad: set[str] | None = None) -> str:
    text = "—" if value is None else str(value)
    cls = "badge"
    if good and text in good:
        cls += " badge-good"
    elif bad and text in bad:
        cls += " badge-bad"
    else:
        cls += " badge-neutral"
    return f'<span class="{cls}">{esc(text)}</span>'


def render_counter_table(counter: Counter, total_label: str = "count") -> str:
    if not counter:
        return "<p class='muted'>No data yet.</p>"
    rows = []
    grand_total = sum(counter.values()) or 1
    for key, n in counter.most_common():
        pct = 100.0 * n / grand_total
        rows.append(
            f"<tr><td>{esc(key)}</td><td>{n}</td>"
            f"<td><div class='bar'><div class='bar-fill' style='width:{pct:.1f}%'></div></div></td>"
            f"<td>{pct:.1f}%</td></tr>"
        )
    return (
        "<table class='mini'><thead><tr><th>value</th><th>" + esc(total_label)
        + "</th><th></th><th>%</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def render_stage_summary(agg: dict[str, Any]) -> str:
    blocks = []
    for label, counts in agg["stage_status_counts"].items():
        blocks.append(f"<div class='panel'><h3>{esc(label)}</h3>{render_counter_table(counts)}</div>")
    return "<div class='grid'>" + "".join(blocks) + "</div>"


def render_regime_summary(agg: dict[str, Any]) -> str:
    blocks = []
    symbols = sorted(agg["regime_state_counts"].keys())
    for sym in symbols:
        counts = agg["regime_state_counts"][sym]
        match = agg["candidate_match_counts"][sym]
        blocks.append(
            f"<div class='panel'><h3>{esc(sym)}</h3>"
            f"<p class='muted'>Frozen-candidate match: "
            f"<b>{match['true']}</b> of <b>{match['true'] + match['false']}</b> cycles</p>"
            f"{render_counter_table(counts, total_label='cycles')}</div>"
        )
    if not blocks:
        return "<p class='muted'>No cycles with market state data yet.</p>"
    return "<div class='grid'>" + "".join(blocks) + "</div>"


def render_cycle_row(c: dict[str, Any]) -> str:
    safety = c.get("overall_execution_safety")
    kill = c.get("kill_switch_active")
    symbols_cell = "; ".join(
        f"{esc(sym)}: {esc(s.get('regime_state'))}"
        + (" [MATCH]" if s.get("candidate_match") else "")
        for sym, s in (c.get("symbols") or {}).items()
    ) or "—"
    return (
        "<tr>"
        f"<td>{esc(c.get('timestamp') or c.get('cycle_id'))}</td>"
        f"<td>{badge(c.get('market_state_valid'), good={'True'}, bad={'False'})}</td>"
        f"<td>{esc(symbols_cell)}</td>"
        f"<td>{badge(c.get('overall_decision'), good={'ENTRY_CANDIDATE'})}</td>"
        f"<td>{badge(c.get('overall_risk_decision'))}</td>"
        f"<td>{badge(c.get('overall_position_state'))}</td>"
        f"<td>{badge(c.get('overall_execution_decision'))}</td>"
        f"<td>{badge(c.get('overall_reconciliation'), good={'RECONCILED'})}</td>"
        f"<td>{badge(safety, good={'AUTHORIZED'}, bad={'BLOCKED'})}</td>"
        f"<td>{badge(kill, bad={'True'}, good={'False'})}</td>"
        f"<td>{'INCOMPLETE: ' + ', '.join(c['missing_stages']) if not c.get('complete') else ''}</td>"
        "</tr>"
    )


def render_html(runtime_dir: Path, runtime_state: dict[str, Any] | None, cycles: list[dict[str, Any]], max_detail: int) -> str:
    agg = build_aggregates(cycles)
    generated_at = datetime.now(timezone.utc).isoformat()

    latest = cycles[-1] if cycles else None
    latest_html = "<p class='muted'>No cycles found yet.</p>"
    if latest:
        latest_html = f"""
        <div class="cards">
          <div class="card"><div class="card-label">Latest cycle</div><div class="card-value">{esc(latest.get('timestamp') or latest.get('cycle_id'))}</div></div>
          <div class="card"><div class="card-label">Market state valid</div><div class="card-value">{badge(latest.get('market_state_valid'), good={'True'}, bad={'False'})}</div></div>
          <div class="card"><div class="card-label">Signal decision</div><div class="card-value">{badge(latest.get('overall_decision'), good={'ENTRY_CANDIDATE'})}</div></div>
          <div class="card"><div class="card-label">Execution safety</div><div class="card-value">{badge(latest.get('overall_execution_safety'), good={'AUTHORIZED'}, bad={'BLOCKED'})}</div></div>
          <div class="card"><div class="card-label">Kill switch</div><div class="card-value">{badge(latest.get('kill_switch_active'), bad={'True'}, good={'False'})}</div></div>
          <div class="card"><div class="card-label">Live execution authorized</div><div class="card-value">{badge(latest.get('live_execution_authorized'), bad={'True'}, good={'False'})}</div></div>
        </div>
        """

    runtime_state_html = "<p class='muted'>No runtime_state.json found.</p>"
    if runtime_state:
        runtime_state_html = "<table class='kv'>" + "".join(
            f"<tr><td>{esc(k)}</td><td>{esc(v)}</td></tr>" for k, v in runtime_state.items()
        ) + "</table>"

    detail_rows = cycles[-max_detail:] if max_detail > 0 else cycles
    detail_rows_html = "".join(render_cycle_row(c) for c in reversed(detail_rows))
    truncated_note = (
        f"<p class='muted'>Showing the most recent {len(detail_rows)} of {len(cycles)} cycles.</p>"
        if len(detail_rows) < len(cycles)
        else ""
    )

    blocked_reasons_html = render_counter_table(agg["safety_blocked_reason_counts"], total_label="cycles")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>AURA paper-runtime dashboard</title>
<style>
  :root {{
    --bg: #0b0e14; --panel: #131824; --border: #232a3a; --text: #e6e9ef; --muted: #8a93a6;
    --good: #1f9d55; --bad: #d64545; --neutral: #3a4256; --accent: #4f8cff;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--text); font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif; font-size:14px; line-height:1.45; }}
  header {{ padding:24px 28px; border-bottom:1px solid var(--border); }}
  header h1 {{ margin:0 0 4px 0; font-size:20px; }}
  header p {{ margin:0; color:var(--muted); font-size:12.5px; }}
  main {{ padding:24px 28px 60px 28px; max-width:1400px; margin:0 auto; }}
  section {{ margin-bottom:36px; }}
  section > h2 {{ font-size:15px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); margin:0 0 12px 0; border-bottom:1px solid var(--border); padding-bottom:8px; }}
  .muted {{ color:var(--muted); }}
  .cards {{ display:flex; flex-wrap:wrap; gap:12px; }}
  .card {{ background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:14px 16px; min-width:180px; flex:1; }}
  .card-label {{ color:var(--muted); font-size:11.5px; text-transform:uppercase; letter-spacing:.03em; margin-bottom:6px; }}
  .card-value {{ font-size:16px; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(280px,1fr)); gap:14px; }}
  .panel {{ background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:14px 16px; }}
  .panel h3 {{ margin:0 0 8px 0; font-size:13px; color:var(--accent); }}
  table {{ border-collapse:collapse; width:100%; }}
  table.mini td, table.mini th {{ padding:4px 6px; font-size:12.5px; border-bottom:1px solid var(--border); }}
  table.kv td {{ padding:4px 8px; font-size:12.5px; border-bottom:1px solid var(--border); }}
  table.kv td:first-child {{ color:var(--muted); width:220px; }}
  table.detail {{ font-size:12px; }}
  table.detail th, table.detail td {{ padding:6px 8px; border-bottom:1px solid var(--border); text-align:left; vertical-align:top; white-space:nowrap; }}
  table.detail td:nth-child(3) {{ white-space:normal; max-width:360px; }}
  .bar {{ background:var(--neutral); border-radius:3px; height:8px; width:90px; overflow:hidden; }}
  .bar-fill {{ background:var(--accent); height:100%; }}
  .badge {{ display:inline-block; padding:2px 8px; border-radius:10px; font-size:11.5px; font-weight:600; }}
  .badge-good {{ background:rgba(31,157,85,.18); color:#3ddc84; }}
  .badge-bad {{ background:rgba(214,69,69,.18); color:#ff7a7a; }}
  .badge-neutral {{ background:rgba(58,66,86,.5); color:var(--text); }}
  .note {{ background:rgba(79,140,255,.1); border:1px solid rgba(79,140,255,.3); border-radius:8px; padding:12px 16px; font-size:12.5px; color:var(--text); }}
</style>
</head>
<body>
<header>
  <h1>AURA paper-runtime dashboard</h1>
  <p>Read-only view of {esc(runtime_dir)} — generated {esc(generated_at)}. This page never places an order and never calls Alpaca; it only reads files .21 already wrote to disk.</p>
</header>
<main>

<section>
  <div class="note">RESEARCH / PAPER MONITORING ONLY. This dashboard cannot enable orders, change the kill switch, or authorize execution — those remain controlled by the pipeline's own config files, edited deliberately, never from here.</div>
</section>

<section>
  <h2>Latest cycle</h2>
  {latest_html}
</section>

<section>
  <h2>Runtime state (regime_output/runtime/runtime_state.json)</h2>
  {runtime_state_html}
</section>

<section>
  <h2>History summary — {agg['total_cycles']} cycles ({agg['complete_cycles']} complete, {agg['incomplete_cycles']} incomplete)</h2>
  <div class="cards">
    <div class="card"><div class="card-label">Kill switch active</div><div class="card-value">{agg['kill_switch_active_count']} / {agg['kill_switch_active_count'] + agg['kill_switch_inactive_count']}</div></div>
    <div class="card"><div class="card-label">Execution authorized</div><div class="card-value">{agg['execution_authorized_count']} / {agg['total_cycles']}</div></div>
    <div class="card"><div class="card-label">Market state invalid</div><div class="card-value">{agg['market_state_invalid_count']} / {agg['total_cycles']}</div></div>
  </div>
</section>

<section>
  <h2>Stage outcomes across all cycles</h2>
  {render_stage_summary(agg)}
</section>

<section>
  <h2>Regime state frequency per symbol</h2>
  {render_regime_summary(agg)}
</section>

<section>
  <h2>Safety block reasons</h2>
  {blocked_reasons_html}
</section>

<section>
  <h2>Per-cycle detail (most recent first)</h2>
  {truncated_note}
  <div style="overflow-x:auto;">
  <table class="detail">
    <thead><tr>
      <th>timestamp</th><th>mkt valid</th><th>regime / match</th><th>signal</th><th>risk</th>
      <th>position</th><th>paper exec</th><th>reconcile</th><th>safety</th><th>kill switch</th><th>notes</th>
    </tr></thead>
    <tbody>
      {detail_rows_html}
    </tbody>
  </table>
  </div>
</section>

</main>
</body>
</html>
"""


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
    p.add_argument("--output", type=Path, default=None, help="Default: <runtime-dir>/dashboard.html")
    p.add_argument("--max-cycles-detail", type=int, default=300, help="0 = show all cycles in the detail table")
    args = p.parse_args()

    runtime_dir = args.runtime_dir
    output_path = args.output or (runtime_dir / "dashboard.html")

    runtime_state = _load_json(runtime_dir / "runtime_state.json")
    cycles = discover_cycles(runtime_dir)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        render_html(runtime_dir, runtime_state, cycles, args.max_cycles_detail), encoding="utf-8"
    )

    print(f"Cycles found: {len(cycles)}")
    print(f"Dashboard written to: {output_path.resolve()}")
    print("Open it directly in a browser -- no server needed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
