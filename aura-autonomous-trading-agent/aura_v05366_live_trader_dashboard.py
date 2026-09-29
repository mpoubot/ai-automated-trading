#!/usr/bin/env python3
"""
AURA v0.5.3.66 -- Track B: Streamlit monitoring dashboard for the
continuous, real-order-submission loop
(`aura_v05365_stage1b_scheduled_live_trader.py`).

WHAT THIS MODULE IS, AND HOW IT DIFFERS FROM `.358`
------------------------------------------------------------------------
`.358` (`aura_v05358_dashboard.py`) is a pure, credential-free viewer of
`.357`'s permanently-preview-only cycle output -- by explicit design, it
never touches Alpaca. Martin asked (2026-09-29) for a dashboard that also
shows what `.365` (the live loop) is actually doing, PLUS current real
positions -- and current positions cannot be answered from `.365`'s own
cycle files alone: `.363`'s `_stage1_report_to_dict` (reused unmodified
by `.365`) never serializes a position list, only per-symbol
outcomes/audit records. Showing positions genuinely requires a fresh,
read-only Alpaca call. Martin explicitly confirmed (AskUserQuestion,
2026-09-29) building this as a SEPARATE new module rather than folding
that into `.358` -- so `.358`'s existing "never touches Alpaca, never
requires a credential" guarantee stays true and unchanged for anyone
relying on it, and this file's own docstring can be honest about the
different trust boundary it has instead.

THIS MODULE DOES MAKE REAL, READ-ONLY ALPACA CALLS
------------------------------------------------------------------------
Specifically: `get_account()` and `get_all_positions()`, via `.43`'s
already-tested `build_portfolio_snapshot(alpaca_client=...)` -- the SAME
function `.355`/`.44` already use elsewhere in this repo to determine
"what do we currently hold", never reimplemented here. It also calls
`.357`'s own `is_market_open()` (also read-only) for a real market-hours
badge, instead of `.358`'s local calendar estimate -- since this module
already holds real credentials for the positions call, there is no
reason to fall back to an estimate here.

There is still no code path in this file that can submit, modify, or
cancel an order -- no order-submission function is imported anywhere
here, only the same read-only account/position/clock calls already used
elsewhere in this repo.

Reused, not duplicated
------------------------------------------------------------------------
- `.356.load_equity_paper_credentials()` / `.356.build_trading_client()`
  -- same two required env vars, same fail-closed behavior, as every
  other module in this repo that reads real Alpaca data.
- `.343.build_portfolio_snapshot()` -- real positions/equity.
- `.357.is_market_open()` -- real market-hours check.
- `.365.check_kill_switch()` / `.365.read_todays_submitted_count()` --
  the SAME kill-switch/daily-cap state `.365`'s own loop reads, so this
  dashboard's "kill switch armed" and "submitted today" tiles can never
  drift from what the live loop itself is actually enforcing.
- Cycle-history rendering (badges, stat tiles, equity chart, per-symbol
  table, history table) follows `.358`'s own established shape/CSS/
  palette (this repo's dataviz skill, references/palette.md) --
  `.365`'s cycle files are written by the SAME `_stage1_report_to_dict`
  `.356`/`.358` already understand, so the shape is identical; only the
  framing text changes (this loop is EXPECTED to submit orders, unlike
  `.357`, so there is no "submitted_count != 0 -> investigate" badge
  here -- that would be actively wrong for this module).

Run with:
    streamlit run aura_v05366_live_trader_dashboard.py -- --output-dir stage1b_live_trader_output

(Everything after the bare `--` is this script's own argv; Streamlit's
own flags, if any, go before it.)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import streamlit as st

ROOT = Path(__file__).resolve().parent
DEFAULT_REFRESH_SECONDS = 30
DEFAULT_STALE_WARNING_SECONDS = 900  # 15 min -- generous vs. the loop's 5 min default interval
DEFAULT_EQUITY_CHART_POINTS = 200
DEFAULT_ACCOUNT_DATA_TTL_SECONDS = 15  # how long a live positions/equity fetch is cached before re-fetching

# ============================================================================
# Palette -- same tokens as `.358` (this repo's dataviz skill,
# references/palette.md, dark column). Kept identical for visual
# consistency across the two dashboards.
# ============================================================================
PAGE_PLANE = "#0d0d0d"
CHART_SURFACE = "#1a1a19"
PRIMARY_INK = "#ffffff"
SECONDARY_INK = "#c3c2b7"
MUTED_INK = "#898781"
GRIDLINE = "#2c2c2a"
BASELINE_AXIS = "#383835"
BORDER = "rgba(255,255,255,0.10)"
SERIES_1_BLUE = "#3987e5"
STATUS_GOOD = "#0ca30c"
STATUS_WARNING = "#fab219"
STATUS_CRITICAL = "#e66767"


def _load_module(module_name: str, filename: str):
    try:
        return __import__(module_name)
    except ImportError:
        import importlib.util

        spec = importlib.util.spec_from_file_location(module_name, ROOT / filename)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod


def load_live_trader_module():
    """`.365` -- source of `check_kill_switch`/`read_todays_submitted_count`
    and their default paths, so this dashboard's kill-switch/daily-cap
    tiles read the SAME state the live loop itself enforces, never a
    reimplementation."""
    return _load_module("aura_v05365_stage1b_scheduled_live_trader", "aura_v05365_stage1b_scheduled_live_trader.py")


def load_equity_cli_module():
    """`.356` -- source of credential loading and trading-client
    construction, reused unmodified."""
    return _load_module("aura_v05356_stage3_live_equity_cli", "aura_v05356_stage3_live_equity_cli.py")


def load_scheduled_runner_module():
    """`.357` -- source of the real, read-only `is_market_open()`."""
    return _load_module("aura_v05357_stage3_scheduled_runner", "aura_v05357_stage3_scheduled_runner.py")


def load_observability_module():
    """`.343` -- source of `build_portfolio_snapshot()`, the same
    real-positions call `.355`/`.44` already use elsewhere in this repo."""
    return _load_module("aura_v05343_portfolio_exposure_observability", "aura_v05343_portfolio_exposure_observability.py")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    live_trader_module = load_live_trader_module()
    parser.add_argument("--output-dir", type=Path, default=live_trader_module.DEFAULT_OUTPUT_DIR,
                         help=f"Directory .365 writes cycle_*.json/latest.json into. "
                              f"Default: {live_trader_module.DEFAULT_OUTPUT_DIR}")
    parser.add_argument("--kill-switch-file", type=Path, default=live_trader_module.DEFAULT_KILL_SWITCH_PATH,
                         help=f"Default: {live_trader_module.DEFAULT_KILL_SWITCH_PATH}")
    parser.add_argument("--daily-order-log-path", type=Path, default=live_trader_module.DEFAULT_DAILY_ORDER_LOG_PATH,
                         help=f"Default: {live_trader_module.DEFAULT_DAILY_ORDER_LOG_PATH}")
    parser.add_argument("--max-orders-per-day", type=int, default=live_trader_module.DEFAULT_MAX_ORDERS_PER_DAY)
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


# ============================================================================
# Cycle-file reading -- identical shape/behavior to `.358`'s own reader
# functions, since `.365`'s cycle files are written by the same
# `_stage1_report_to_dict` `.356` already defines.
# ============================================================================


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
# Live account data -- REAL, read-only Alpaca calls. Cached briefly
# (DEFAULT_ACCOUNT_DATA_TTL_SECONDS) so a fast auto-refresh loop doesn't
# hammer the API; "Refresh account data now" bypasses the cache.
# ============================================================================


@st.cache_resource(show_spinner=False)
def _cached_trading_client(_cache_key: str):
    """`_cache_key` is unused except to let the sidebar force a fresh
    client if credentials are re-entered mid-session; the underlying
    `TradingClient` is a lightweight HTTP client, safe to cache."""
    equity_cli = load_equity_cli_module()
    api_key, secret_key = equity_cli.load_equity_paper_credentials()
    return equity_cli.build_trading_client(api_key, secret_key)


def fetch_live_account_data(ttl_seconds: int) -> dict[str, Any]:
    """Returns {"ok": True, "snapshot": PortfolioSnapshot, "market_open": bool|None,
    "market_check_error": str|None} on success, or {"ok": False, "error": str}
    if credentials are missing or the client cannot be built at all. A
    positions/equity fetch failure specifically (vs. a missing-credential
    failure) is still surfaced via the snapshot's own venue_fetch_status
    -- fail-open on DISPLAY (never crash the page), fail-closed on TRUST
    (never fabricate a number; show 'unavailable' instead)."""

    @st.cache_data(ttl=ttl_seconds, show_spinner=False)
    def _fetch(_ttl_bucket: int) -> dict[str, Any]:
        try:
            client = _cached_trading_client("default")
        except Exception as exc:  # noqa: BLE001 -- missing/invalid credentials
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        observability_module = load_observability_module()
        snapshot = observability_module.build_portfolio_snapshot(alpaca_client=client)

        scheduled_runner_module = load_scheduled_runner_module()
        market_open: bool | None
        market_check_error: str | None
        try:
            market_open = scheduled_runner_module.is_market_open(client)
            market_check_error = None
        except Exception as exc:  # noqa: BLE001
            market_open = None
            market_check_error = f"{type(exc).__name__}: {exc}"

        return {
            "ok": True,
            "snapshot": snapshot.to_dict(),
            "market_open": market_open,
            "market_check_error": market_check_error,
        }

    # ttl_seconds is baked into the cache_data decorator itself; the
    # bucket arg just keeps a distinct cache entry per configured TTL.
    return _fetch(ttl_seconds)


# ============================================================================
# CSS -- identical tokens/classes to `.358`, for visual consistency.
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
        .aura-stat-value-warning {{ color: {STATUS_WARNING}; }}
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


def render_badge_row(*, latest: dict[str, Any] | None, cycle_count: int, kill_switch_engaged: bool,
                      market_open: bool | None) -> None:
    badges = [badge("LIVE TRADING · REAL ORDERS POSSIBLE", "warning"), badge("ALPACA PAPER", "muted")]
    if kill_switch_engaged:
        badges.append(badge("KILL SWITCH ENGAGED", "critical"))
    else:
        badges.append(badge("KILL SWITCH NOT ARMED", "good"))
    if market_open is True:
        badges.append(badge("MARKET OPEN", "good"))
    elif market_open is False:
        badges.append(badge("MARKET CLOSED", "muted"))
    else:
        badges.append(badge("MARKET STATUS UNKNOWN", "warning"))
    badges.append(badge(f"{cycle_count} CYCLES RECORDED", "muted"))
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
# Rendering -- cycle history (adapted from `.358`; framing text differs
# since submissions are EXPECTED here, not an anomaly).
# ============================================================================


def render_freshness(latest: dict[str, Any] | None, stale_after_seconds: int) -> None:
    if latest is None:
        st.warning("No cycle data yet -- `.365` (`aura_v05365_stage1b_scheduled_live_trader.py`) has not "
                   "written a `latest.json` in this output directory. Start it to begin seeing cycle data here.")
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
            f"The live loop may be stopped (check the kill switch), the market may be closed, or it's outside "
            f"trading hours."
        )
    else:
        st.success(f"Latest cycle: {age_minutes:.1f} minutes ago ({latest.get('observed_at')})")


def render_cycle_stat_tiles(latest: dict[str, Any] | None, cycle_files: list[Path]) -> None:
    stage1 = (latest.get("stage1_report") or {}) if latest else {}
    submitted_count = stage1.get("submitted_count", 0)
    submitted_value_class = "aura-stat-value-good" if submitted_count else ""

    tiles = [
        stat_tile("Submitted this cycle", str(submitted_count), value_class=submitted_value_class,
                  sub="real orders -- expected to be nonzero sometimes"),
        stat_tile("Cycles recorded", str(len(cycle_files)), sub="all-time, this output directory"),
    ]
    st.markdown(f'<div class="aura-stat-row">{"".join(tiles)}</div>', unsafe_allow_html=True)

    failures = (latest or {}).get("symbol_fetch_failures") or []
    if failures:
        st.warning(f"Symbol fetch failures this cycle: {failures}")


def render_equity_chart(cycle_files: list[Path], max_points: int) -> None:
    st.markdown('<div class="aura-section-title">Account equity at cycle time (recorded cycles)</div>',
                unsafe_allow_html=True)
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
        st.caption("Not enough recorded cycles yet for a chart (need 2+).")
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
            "Cycle stage": o.get("stage"),
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


# ============================================================================
# Rendering -- live account data (positions, equity, market status).
# ============================================================================


def render_live_account_stat_tiles(account_data: dict[str, Any], *, kill_switch_engaged: bool,
                                    submitted_today: int, max_orders_per_day: int) -> None:
    cap_class = "aura-stat-value-critical" if submitted_today >= max_orders_per_day else ""
    tiles = [
        stat_tile("Orders submitted today", f"{submitted_today}/{max_orders_per_day}", value_class=cap_class,
                  sub="UTC calendar day, from .365's own daily order log"),
        stat_tile("Kill switch", "ENGAGED" if kill_switch_engaged else "not armed",
                  value_class="aura-stat-value-critical" if kill_switch_engaged else "aura-stat-value-good"),
    ]

    if account_data.get("ok"):
        snapshot = account_data["snapshot"]
        alpaca_status = (snapshot.get("venue_fetch_status") or {}).get("ALPACA", {})
        positions = [p for p in (snapshot.get("positions") or []) if p.get("venue") == "ALPACA"]
        equity = alpaca_status.get("equity")
        equity_str = f"${equity:,.2f}" if isinstance(equity, (int, float)) else "n/a"
        if alpaca_status.get("status") == "SUCCESS":
            tiles.insert(0, stat_tile("Account equity (live)", equity_str, sub="real ALPACA get_account(), just now"))
            tiles.insert(1, stat_tile("Open positions (live)", str(len(positions)), sub="real ALPACA get_all_positions()"))
        else:
            tiles.insert(0, stat_tile("Account equity (live)", "unavailable",
                                       value_class="aura-stat-value-warning",
                                       sub=alpaca_status.get("error") or "fetch failed"))
    else:
        tiles.insert(0, stat_tile("Live account data", "unavailable", value_class="aura-stat-value-warning",
                                   sub=account_data.get("error", "credentials not found")))

    st.markdown(f'<div class="aura-stat-row">{"".join(tiles)}</div>', unsafe_allow_html=True)


def render_positions_table(account_data: dict[str, Any]) -> None:
    st.markdown('<div class="aura-section-title">Current positions (live, ALPACA)</div>', unsafe_allow_html=True)
    if not account_data.get("ok"):
        st.warning(f"Live account data unavailable: {account_data.get('error', 'unknown error')}. "
                   f"Make sure ALPACA_EQUITY_PAPER_API_KEY / ALPACA_EQUITY_PAPER_SECRET_KEY are set in the "
                   f"environment this dashboard is running from.")
        return
    snapshot = account_data["snapshot"]
    alpaca_status = (snapshot.get("venue_fetch_status") or {}).get("ALPACA", {})
    if alpaca_status.get("status") != "SUCCESS":
        st.warning(f"Positions fetch did not succeed: {alpaca_status.get('error') or alpaca_status.get('status')}")
        return
    positions = [p for p in (snapshot.get("positions") or []) if p.get("venue") == "ALPACA"]
    if not positions:
        st.caption("No open ALPACA positions right now.")
        return
    rows = []
    for p in positions:
        rows.append({
            "Symbol": p.get("symbol"),
            "Direction": p.get("direction"),
            "Quantity": p.get("quantity"),
            "Entry price": p.get("entry_price"),
            "Mark price": p.get("mark_price"),
            "Notional (USD)": p.get("notional_usd"),
            "Unrealized P&L (USD)": p.get("unrealized_pnl_usd"),
        })
    st.dataframe(rows, use_container_width=True, hide_index=True)
    st.caption(f"As of {snapshot.get('as_of', 'n/a')} (this dashboard's own live fetch, cached "
               f"{DEFAULT_ACCOUNT_DATA_TTL_SECONDS}s -- independent of `.365`'s own cycle timing).")


def main() -> None:
    args = parse_args(sys.argv[1:])
    live_trader_module = load_live_trader_module()

    st.set_page_config(page_title="AURA Track B -- Stage 3B Live Trading Monitor", layout="wide")
    inject_css()

    st.title("AURA Track B — Stage 3B Live Trading Monitor")
    st.caption("aura_v05366_live_trader_dashboard.py — reads .365's cycle output AND makes real, read-only "
               "Alpaca calls (account equity, positions, market clock). No order-submission code here.")

    with st.sidebar:
        st.header("Settings")
        output_dir_str = st.text_input("Output directory", value=str(args.output_dir))
        output_dir = Path(output_dir_str)
        kill_switch_str = st.text_input("Kill switch file", value=str(args.kill_switch_file))
        daily_log_str = st.text_input("Daily order log path", value=str(args.daily_order_log_path))
        max_orders_per_day = st.number_input("Max orders per day (for the cap tile)", min_value=1,
                                              value=int(args.max_orders_per_day), step=1)
        stale_after = st.number_input("Stale warning threshold (seconds)", min_value=60,
                                       value=DEFAULT_STALE_WARNING_SECONDS, step=60)
        history_rows = st.number_input("History rows to show", min_value=5, max_value=500, value=50, step=5)
        chart_points = st.number_input("Equity chart points (most recent)", min_value=10, max_value=2000,
                                        value=DEFAULT_EQUITY_CHART_POINTS, step=10)
        account_data_ttl = st.number_input("Live account data cache (seconds)", min_value=5, max_value=300,
                                            value=DEFAULT_ACCOUNT_DATA_TTL_SECONDS, step=5)
        if st.button("Refresh account data now"):
            st.cache_data.clear()
        auto_refresh = st.checkbox("Auto-refresh", value=True, key="auto_refresh")
        refresh_seconds = st.number_input("Refresh interval (seconds)", min_value=5, max_value=300,
                                           value=DEFAULT_REFRESH_SECONDS, step=5, key="refresh_seconds")
        if st.button("Refresh now"):
            st.rerun()
        st.caption(f"Page rendered at {_now_utc().isoformat()}")

    cycle_files = list_cycle_files(output_dir)
    latest = load_latest(output_dir) if output_dir.exists() else None

    kill_switch_engaged = live_trader_module.check_kill_switch(Path(kill_switch_str))
    today_key = live_trader_module._day_key(_now_utc().isoformat())
    submitted_today = live_trader_module.read_todays_submitted_count(Path(daily_log_str), today_key=today_key)

    account_data = fetch_live_account_data(int(account_data_ttl))
    market_open = account_data.get("market_open") if account_data.get("ok") else None

    render_badge_row(latest=latest, cycle_count=len(cycle_files), kill_switch_engaged=kill_switch_engaged,
                      market_open=market_open)

    render_live_account_stat_tiles(account_data, kill_switch_engaged=kill_switch_engaged,
                                    submitted_today=submitted_today, max_orders_per_day=int(max_orders_per_day))
    if account_data.get("market_check_error"):
        st.caption(f"Market clock check failed: {account_data['market_check_error']}")
    render_positions_table(account_data)

    if not output_dir.exists():
        st.warning(f"Cycle output directory does not exist yet: `{output_dir}`. Start the live loop "
                   f"(`aura_v05365_stage1b_scheduled_live_trader.py --output-dir {output_dir} ...`) to create it.")
    else:
        render_freshness(latest, int(stale_after))
        render_cycle_stat_tiles(latest, cycle_files)
        render_equity_chart(cycle_files, int(chart_points))
        render_symbol_table(latest)
        render_history(cycle_files, int(history_rows))

    if auto_refresh:
        import time
        time.sleep(int(refresh_seconds))
        st.rerun()


if __name__ == "__main__":
    main()
