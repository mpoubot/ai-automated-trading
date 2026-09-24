#!/usr/bin/env python3
"""
AURA v0.5.3.58 -- Track B: read-only Streamlit monitoring dashboard for the
scheduled loop runner (`aura_v05357_stage3_scheduled_runner.py`).

WHAT THIS MODULE IS, AND IS DELIBERATELY NOT
------------------------------------------------------------------------
This is a pure viewer. It never imports `.56` or `.57`, never touches
Alpaca, never reads or requires any credential, and never writes
anything except Streamlit's own internal state. All it does is read the
JSON files that `.57` already writes to its `--output-dir`
(`stage3_scheduled_output/` by default: one `cycle_<timestamp>.json` per
loop pass plus a rolling `latest.json`) and render them.

There is no code path in this file that can submit, modify, or cancel an
order -- there is no broker client here at all. If the scheduler behind
it has never run, or is not running right now, this dashboard just shows
"no cycle data yet" / a stale-data warning; it cannot make anything
happen on its own.

VISUAL DESIGN NOTE
------------------------------------------------------------------------
The dark, card-based look here was requested to visually match a
reference screenshot Martin shared (a different project's dashboard).
Per his explicit decision: this is a *look-and-feel* reference only --
no code, components, or files from that other project were read or
reused. Colors/spacing follow this codebase's own dataviz skill
(references/palette.md's validated dark categorical + status palette),
built fresh in this file.

The "market session" card below is a **local calendar estimate**
(weekday + America/New_York clock hours only -- it does NOT know about
NYSE holidays, and it does NOT call Alpaca's real market clock, since
that would require credentials this pure-viewer file deliberately never
touches). It is labeled "est." for that reason. The real, authoritative
market-hours gate lives in `.57`'s own `is_market_open()`, which does
call the real (read-only) Alpaca clock before each cycle.

Run with:
    streamlit run aura_v05358_dashboard.py -- --output-dir stage3_scheduled_output

(Everything after the bare `--` is this script's own argv; Streamlit's
own flags, if any, go before it.)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import streamlit as st

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover -- missing tzdata on some Windows installs
    _ET = None

ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = ROOT / "stage3_scheduled_output"
DEFAULT_REFRESH_SECONDS = 30
DEFAULT_STALE_WARNING_SECONDS = 900  # 15 min -- generous vs. the runner's 5 min default interval
DEFAULT_EQUITY_CHART_POINTS = 200

# ============================================================================
# Palette -- from this repo's dataviz skill, references/palette.md (dark
# column only; this dashboard is dark-only by design, matching the
# requested reference look). Not copied from any other project's code.
# ============================================================================
PAGE_PLANE = "#0d0d0d"
CHART_SURFACE = "#1a1a19"
PRIMARY_INK = "#ffffff"
SECONDARY_INK = "#c3c2b7"
MUTED_INK = "#898781"
GRIDLINE = "#2c2c2a"
BASELINE_AXIS = "#383835"
BORDER = "rgba(255,255,255,0.10)"
SERIES_1_BLUE = "#3987e5"  # dark-mode categorical slot 1
STATUS_GOOD = "#0ca30c"
STATUS_WARNING = "#fab219"
STATUS_CRITICAL = "#e66767"  # dark-mode red (slot 8 dark), reserved status use here


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
                         help=f"Directory the scheduled runner writes cycle_*.json/latest.json into. "
                              f"Default: {DEFAULT_OUTPUT_DIR}")
    # Streamlit re-invokes this script itself; ignore anything it injects
    # that we don't recognise instead of crashing the app on it.
    known, _unknown = parser.parse_known_args(argv)
    return known


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_latest(output_dir: Path) -> dict[str, Any] | None:
    latest_path = output_dir / "latest.json"
    if not latest_path.exists():
        return None
    try:
        return json.loads(latest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def list_cycle_files(output_dir: Path) -> list[Path]:
    if not output_dir.exists():
        return []
    # Filenames are `cycle_<YYYYmmddTHHMMSSffffffZ>.json` -- sortable as strings.
    return sorted(output_dir.glob("cycle_*.json"))


def load_cycle(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def summarize_cycle(result: dict[str, Any]) -> dict[str, Any]:
    stage1 = result.get("stage1_report") or {}
    outcomes = stage1.get("outcomes") or []
    outcome_str = ", ".join(
        f"{o.get('symbol')}={o.get('decision_outcome')}" for o in outcomes
    ) or "(no symbols usable)"
    return {
        "observed_at": result.get("observed_at", ""),
        "submitted_count": stage1.get("submitted_count", 0),
        "account_equity_usd": result.get("account_equity_usd"),
        "outcomes": outcome_str,
        "symbol_fetch_failures": len(result.get("symbol_fetch_failures") or []),
    }


# ============================================================================
# CSS -- dark theme, badge pills, stat tiles. Fresh CSS written against this
# repo's own dataviz skill tokens, not copied from any other project.
# ============================================================================


def inject_css() -> None:
    st.markdown(
        f"""
        <style>
        .stApp {{ background-color: {PAGE_PLANE}; }}

        .aura-badge-row {{ display: flex; flex-wrap: wrap; gap: 0.5rem; margin: 0.75rem 0 1.25rem 0; }}
        .aura-badge {{
            display: inline-flex; align-items: center; gap: 0.4rem;
            padding: 0.3rem 0.75rem; border-radius: 999px;
            border: 1px solid {BORDER}; background-color: {CHART_SURFACE};
            color: {SECONDARY_INK}; font-size: 0.78rem; font-weight: 600;
            letter-spacing: 0.02em; text-transform: uppercase;
        }}
        .aura-badge-dot {{ width: 8px; height: 8px; border-radius: 50%; display: inline-block; }}
        .aura-badge-good {{ color: {STATUS_GOOD}; border-color: {STATUS_GOOD}55; }}
        .aura-badge-warning {{ color: {STATUS_WARNING}; border-color: {STATUS_WARNING}55; }}
        .aura-badge-critical {{ color: {STATUS_CRITICAL}; border-color: {STATUS_CRITICAL}55; }}
        .aura-badge-accent {{ color: {SERIES_1_BLUE}; border-color: {SERIES_1_BLUE}55; }}

        .aura-stat-row {{ display: flex; flex-wrap: wrap; gap: 0.9rem; margin-bottom: 1.25rem; }}
        .aura-stat-tile {{
            flex: 1 1 200px; background-color: {CHART_SURFACE}; border: 1px solid {BORDER};
            border-radius: 0.6rem; padding: 0.95rem 1.1rem;
        }}
        .aura-stat-label {{
            color: {MUTED_INK}; font-size: 0.72rem; font-weight: 600;
            letter-spacing: 0.04em; text-transform: uppercase; margin-bottom: 0.35rem;
        }}
        .aura-stat-value {{ color: {PRIMARY_INK}; font-size: 1.9rem; font-weight: 600; line-height: 1.1; }}
        .aura-stat-value-critical {{ color: {STATUS_CRITICAL}; }}
        .aura-stat-value-good {{ color: {STATUS_GOOD}; }}
        .aura-stat-delta {{ font-size: 0.8rem; margin-top: 0.3rem; }}
        .aura-stat-delta-up {{ color: {STATUS_GOOD}; }}
        .aura-stat-delta-down {{ color: {STATUS_CRITICAL}; }}
        .aura-stat-delta-flat {{ color: {MUTED_INK}; }}
        .aura-stat-sub {{ color: {SECONDARY_INK}; font-size: 0.78rem; margin-top: 0.3rem; }}

        .aura-section-title {{
            color: {PRIMARY_INK}; font-size: 0.95rem; font-weight: 700;
            text-transform: uppercase; letter-spacing: 0.03em;
            margin: 1.5rem 0 0.5rem 0; border-bottom: 1px solid {GRIDLINE}; padding-bottom: 0.4rem;
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def badge(label: str, status: str = "muted") -> str:
    """status: good | warning | critical | accent | muted"""
    cls = {"good": "aura-badge-good", "warning": "aura-badge-warning",
           "critical": "aura-badge-critical", "accent": "aura-badge-accent"}.get(status, "")
    dot_color = {"good": STATUS_GOOD, "warning": STATUS_WARNING,
                 "critical": STATUS_CRITICAL, "accent": SERIES_1_BLUE}.get(status, MUTED_INK)
    return (f'<span class="aura-badge {cls}">'
            f'<span class="aura-badge-dot" style="background-color:{dot_color};"></span>{label}</span>')


def render_badge_row(latest: dict[str, Any] | None, cycle_count: int, market: dict[str, Any]) -> None:
    badges = [badge("PREVIEW ONLY · NO ORDERS", "accent"), badge("ALPACA PAPER", "muted")]
    if market["is_open_estimated"] is True:
        badges.append(badge("MARKET OPEN (EST.)", "good"))
    elif market["is_open_estimated"] is False:
        badges.append(badge("MARKET CLOSED (EST.)", "muted"))
    else:
        badges.append(badge("MARKET STATUS UNKNOWN", "warning"))
    badges.append(badge(market["et_clock_str"], "muted"))
    badges.append(badge(f"{cycle_count} CYCLES RECORDED", "muted"))
    if latest is not None:
        submitted = (latest.get("stage1_report") or {}).get("submitted_count", 0)
        if submitted != 0:
            badges.append(badge(f"submitted_count={submitted} — INVESTIGATE", "critical"))
    st.markdown(f'<div class="aura-badge-row">{"".join(badges)}</div>', unsafe_allow_html=True)


def stat_tile(label: str, value: str, *, value_class: str = "", sub: str = "",
              delta: str = "", delta_class: str = "") -> str:
    delta_html = f'<div class="aura-stat-delta {delta_class}">{delta}</div>' if delta else ""
    sub_html = f'<div class="aura-stat-sub">{sub}</div>' if sub else ""
    return (
        f'<div class="aura-stat-tile">'
        f'<div class="aura-stat-label">{label}</div>'
        f'<div class="aura-stat-value {value_class}">{value}</div>'
        f'{delta_html}{sub_html}'
        f'</div>'
    )


# ============================================================================
# Market session estimate -- pure calendar math, no broker call. See the
# module docstring's VISUAL DESIGN NOTE for why this is approximate.
# ============================================================================


def nyse_session_status(now_utc: datetime) -> dict[str, Any]:
    if _ET is None:
        return {"is_open_estimated": None, "et_clock_str": "ET TIME UNAVAILABLE (tzdata missing)",
                "next_change_str": ""}
    now_et = now_utc.astimezone(_ET)
    et_clock_str = f"{now_et.strftime('%a %H:%M:%S')} ET"

    def session_bounds(day_et: datetime) -> tuple[datetime, datetime]:
        open_t = day_et.replace(hour=9, minute=30, second=0, microsecond=0)
        close_t = day_et.replace(hour=16, minute=0, second=0, microsecond=0)
        return open_t, close_t

    is_weekday = now_et.weekday() < 5  # Mon=0 .. Fri=4
    open_t, close_t = session_bounds(now_et)
    is_open = is_weekday and open_t <= now_et < close_t

    if is_open:
        delta = close_t - now_et
        return {"is_open_estimated": True, "et_clock_str": et_clock_str,
                "next_change_str": f"Closes in ~{_format_timedelta(delta)} (est.)"}

    # Find the next weekday open at/after now_et.
    candidate = now_et
    for _ in range(8):
        cand_open, _cand_close = session_bounds(candidate)
        if candidate.weekday() < 5 and now_et < cand_open:
            delta = cand_open - now_et
            return {"is_open_estimated": False, "et_clock_str": et_clock_str,
                    "next_change_str": f"Opens in ~{_format_timedelta(delta)} (est., ignores holidays)"}
        candidate = (candidate + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return {"is_open_estimated": False, "et_clock_str": et_clock_str, "next_change_str": ""}


def _format_timedelta(delta: timedelta) -> str:
    total_seconds = max(0, int(delta.total_seconds()))
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


# ============================================================================
# Rendering
# ============================================================================


def render_freshness(latest: dict[str, Any] | None, stale_after_seconds: int) -> None:
    if latest is None:
        st.warning("No cycle data yet -- the scheduled runner (`aura_v05357_stage3_scheduled_runner.py`) "
                   "has not written a `latest.json` in this output directory. Start it to begin seeing data here.")
        return
    observed_at = _parse_iso(latest.get("observed_at"))
    if observed_at is None:
        st.info("Latest cycle file has no readable `observed_at` timestamp.")
        return
    age_seconds = (_now_utc() - observed_at).total_seconds()
    age_minutes = age_seconds / 60.0
    if age_seconds > stale_after_seconds:
        st.error(
            f"Latest cycle is {age_minutes:.1f} minutes old (observed_at={latest.get('observed_at')}). "
            f"The scheduler may be stopped, the market may be closed, or it's outside trading hours."
        )
    else:
        st.success(f"Latest cycle: {age_minutes:.1f} minutes ago ({latest.get('observed_at')})")


def render_stat_tiles(latest: dict[str, Any] | None, cycle_files: list[Path], market: dict[str, Any]) -> None:
    stage1 = (latest.get("stage1_report") or {}) if latest else {}
    submitted_count = stage1.get("submitted_count", 0)

    # Account equity + delta vs previous recorded cycle.
    equity = latest.get("account_equity_usd") if latest else None
    equity_str = f"${equity:,.2f}" if isinstance(equity, (int, float)) else "n/a"
    delta_html, delta_class = "", "aura-stat-delta-flat"
    if isinstance(equity, (int, float)) and len(cycle_files) >= 2:
        prev = load_cycle(cycle_files[-2])
        prev_equity = (prev or {}).get("account_equity_usd")
        if isinstance(prev_equity, (int, float)):
            diff = equity - prev_equity
            if abs(diff) < 0.005:
                delta_html, delta_class = "flat vs previous cycle", "aura-stat-delta-flat"
            elif diff > 0:
                delta_html, delta_class = f"▲ ${diff:,.2f} vs previous cycle", "aura-stat-delta-up"
            else:
                delta_html, delta_class = f"▼ ${abs(diff):,.2f} vs previous cycle", "aura-stat-delta-down"
    elif isinstance(equity, (int, float)):
        delta_html, delta_class = "first recorded cycle", "aura-stat-delta-flat"

    submitted_value_class = "" if submitted_count == 0 else "aura-stat-value-critical"
    submitted_sub = "always 0 by design" if submitted_count == 0 else "STRUCTURALLY UNEXPECTED"

    market_value = "OPEN" if market["is_open_estimated"] else ("CLOSED" if market["is_open_estimated"] is False else "N/A")
    market_class = "aura-stat-value-good" if market["is_open_estimated"] else ""

    tiles = [
        stat_tile("Account equity (USD)", equity_str, delta=delta_html, delta_class=delta_class),
        stat_tile("Submitted this cycle", str(submitted_count), value_class=submitted_value_class, sub=submitted_sub),
        stat_tile("Cycles recorded", str(len(cycle_files)), sub="all-time, this output directory"),
        stat_tile("Market session (est.)", market_value, value_class=market_class, sub=market["next_change_str"]),
    ]
    st.markdown(f'<div class="aura-stat-row">{"".join(tiles)}</div>', unsafe_allow_html=True)

    if submitted_count != 0:
        st.error(
            f"submitted_count={submitted_count} — this should structurally never happen from the "
            f"scheduled runner. Stop the scheduler and investigate before trusting any further output."
        )
    if latest and latest.get("technical_weight_is_zero_warning"):
        st.info(latest["technical_weight_is_zero_warning"])
    failures = (latest or {}).get("symbol_fetch_failures") or []
    if failures:
        st.warning(f"Symbol fetch failures this cycle: {failures}")


def render_equity_chart(cycle_files: list[Path], max_points: int) -> None:
    st.markdown('<div class="aura-section-title">Account equity (recorded cycles)</div>', unsafe_allow_html=True)
    recent = cycle_files[-max_points:]
    rows = []
    for path in recent:
        result = load_cycle(path)
        if result is None:
            continue
        observed_at = _parse_iso(result.get("observed_at"))
        equity = result.get("account_equity_usd")
        if observed_at is not None and isinstance(equity, (int, float)):
            rows.append({"observed_at": observed_at, "account_equity_usd": equity})

    if len(rows) < 2:
        st.caption("Not enough recorded cycles yet for a chart (need 2+). "
                   "Equity will read flat here until Stage 3E activates real signal weights -- "
                   "every cycle currently resolves to ABSTAIN by frozen design.")
        return

    import altair as alt

    chart = (
        alt.Chart(alt.Data(values=rows))
        .mark_line(
            color=SERIES_1_BLUE, strokeWidth=2, interpolate="monotone",
            point=alt.OverlayMarkDef(color=SERIES_1_BLUE, size=64, filled=True),
        )
        .encode(
            x=alt.X("observed_at:T", title=None,
                    axis=alt.Axis(gridColor=GRIDLINE, domainColor=BASELINE_AXIS,
                                   tickColor=BASELINE_AXIS, labelColor=SECONDARY_INK)),
            y=alt.Y("account_equity_usd:Q", title=None, scale=alt.Scale(zero=False),
                    axis=alt.Axis(gridColor=GRIDLINE, domainColor=BASELINE_AXIS,
                                   tickColor=BASELINE_AXIS, labelColor=SECONDARY_INK, format="$,.0f")),
            tooltip=[alt.Tooltip("observed_at:T", title="Observed at"),
                     alt.Tooltip("account_equity_usd:Q", title="Equity (USD)", format="$,.2f")],
        )
        .properties(height=260, background=CHART_SURFACE)
        .configure_view(strokeWidth=0)
    )
    st.altair_chart(chart, use_container_width=True)


def render_symbol_table(latest: dict[str, Any] | None) -> None:
    st.markdown('<div class="aura-section-title">Per-symbol outcomes (latest cycle)</div>', unsafe_allow_html=True)
    if latest is None:
        st.caption("No cycle data yet.")
        return
    stage1 = latest.get("stage1_report") or {}
    outcomes = stage1.get("outcomes") or []
    audit_by_symbol = {a.get("symbol"): a for a in (stage1.get("audit_records") or [])}
    if not outcomes:
        st.caption("No symbols produced an outcome this cycle.")
        return

    rows = []
    for o in outcomes:
        symbol = o.get("symbol")
        audit = audit_by_symbol.get(symbol, {})
        rows.append({
            "Symbol": symbol,
            "Decision": o.get("decision_outcome"),
            "Rank score": o.get("decision_final_rank_score"),
            "Risk status": audit.get("risk_decision_status", "n/a"),
            "Risk blocked reasons": ", ".join(audit.get("risk_decision_blocked_reasons") or []) or "-",
            "Reasons": ", ".join(o.get("reasons") or []) or "-",
        })
    st.dataframe(rows, use_container_width=True, hide_index=True)


def render_history(cycle_files: list[Path], max_rows: int) -> None:
    st.markdown(f'<div class="aura-section-title">Cycle history (most recent {max_rows})</div>',
                unsafe_allow_html=True)
    if not cycle_files:
        st.caption("No cycle files yet.")
        return
    recent = list(reversed(cycle_files))[:max_rows]
    rows = []
    for path in recent:
        result = load_cycle(path)
        if result is None:
            rows.append({"file": path.name, "observed_at": "(unreadable)", "submitted_count": "?",
                         "account_equity_usd": "?", "outcomes": "?", "symbol_fetch_failures": "?"})
            continue
        s = summarize_cycle(result)
        rows.append({
            "file": path.name,
            "observed_at": s["observed_at"],
            "submitted_count": s["submitted_count"],
            "account_equity_usd": s["account_equity_usd"],
            "outcomes": s["outcomes"],
            "symbol_fetch_failures": s["symbol_fetch_failures"],
        })
    st.dataframe(rows, use_container_width=True, hide_index=True)


def main() -> None:
    args = parse_args(sys.argv[1:])

    st.set_page_config(page_title="AURA Track B -- Stage 3 Preview Monitor", layout="wide")
    inject_css()

    st.title("AURA Track B — Stage 3 Scheduled Preview Monitor")
    st.caption("aura_v05358_dashboard.py — reads aura_v05357's cycle output. No trading logic, no broker access.")

    with st.sidebar:
        st.header("Settings")
        output_dir_str = st.text_input("Output directory", value=str(args.output_dir))
        output_dir = Path(output_dir_str)
        stale_after = st.number_input("Stale warning threshold (seconds)", min_value=60,
                                       value=DEFAULT_STALE_WARNING_SECONDS, step=60)
        history_rows = st.number_input("History rows to show", min_value=5, max_value=500, value=50, step=5)
        chart_points = st.number_input("Equity chart points (most recent)", min_value=10, max_value=2000,
                                        value=DEFAULT_EQUITY_CHART_POINTS, step=10)
        auto_refresh = st.checkbox("Auto-refresh", value=True, key="auto_refresh")
        refresh_seconds = st.number_input("Refresh interval (seconds)", min_value=5, max_value=300,
                                           value=DEFAULT_REFRESH_SECONDS, step=5, key="refresh_seconds")
        if st.button("Refresh now"):
            st.rerun()
        st.caption(f"Page rendered at {_now_utc().isoformat()}")

    market = nyse_session_status(_now_utc())
    cycle_files = list_cycle_files(output_dir)
    latest = load_latest(output_dir) if output_dir.exists() else None

    render_badge_row(latest, len(cycle_files), market)

    if not output_dir.exists():
        st.warning(f"Output directory does not exist yet: `{output_dir}`. "
                   f"Start the scheduled runner (`aura_v05357_stage3_scheduled_runner.py --output-dir "
                   f"{output_dir}`) to create it.")
    else:
        render_freshness(latest, int(stale_after))
        render_stat_tiles(latest, cycle_files, market)
        render_equity_chart(cycle_files, int(chart_points))
        render_symbol_table(latest)
        render_history(cycle_files, int(history_rows))

    if auto_refresh:
        import time
        time.sleep(int(refresh_seconds))
        st.rerun()


if __name__ == "__main__":
    main()
