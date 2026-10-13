"""verify_concentration_fix.py -- READ-ONLY verification of the 2026-10-13
asset-concentration re-basing and projected per-symbol gate.

Makes exactly the two read-only Alpaca calls `.43.build_portfolio_snapshot`
already makes (get_account, get_all_positions). Imports no order-submission
module and cannot place, modify or cancel an order.

Run it once per bot, in a shell that has THAT bot's credentials:

    call bot_credentials.bat bot3
    python verify_concentration_fix.py

Exit code 0 = the gate behaves as designed; 1 = something is off.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, REPO / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


OBS = _load("aura_v05343_portfolio_exposure_observability", "aura_v05343_portfolio_exposure_observability.py")
ENF = _load("aura_v05344_portfolio_exposure_enforcement", "aura_v05344_portfolio_exposure_enforcement.py")
CLI = _load("aura_v05356_stage3_live_equity_cli", "aura_v05356_stage3_live_equity_cli.py")

# The exact limits .365 builds (build_default_limits, 2026-09-29).
LIMITS = ENF.PortfolioLimits(
    max_daily_loss_pct_by_venue={"ALPACA": 0.02},
    max_drawdown_pct_by_venue={"ALPACA": 0.05},
    max_asset_concentration_ratio=0.10,
    max_net_exposure_ratio=0.50,
)
PROBE_SYMBOL = "ZZZZ_PROBE"  # deliberately not a real holding

# daily_loss / max_drawdown need the bot's own persisted equity history to be
# meaningful. This probe passes none, so they report NOT_YET_AVAILABLE and
# BLOCK -- correct fail-closed behavior, and nothing to do with the change
# under test. They are reported separately rather than silently hidden.
HISTORY_DEPENDENT = {"daily_loss", "max_drawdown"}


def main() -> int:
    api_key, secret_key = CLI.load_equity_paper_credentials()
    client = CLI.build_trading_client(api_key, secret_key)

    snapshot = OBS.build_portfolio_snapshot(alpaca_client=client)
    status = snapshot.venue_fetch_status.get("ALPACA")
    print(f"account         : {status.status}  equity=${status.equity:,.2f}" if status.equity is not None
          else f"account         : {status.status}  equity=UNAVAILABLE")
    if status.status != "SUCCESS":
        print(f"  fetch error   : {status.error}")
        return 1

    report = OBS.compute_exposure_dimensions(snapshot, [])
    conc = report["asset_concentration"]
    print(f"concentration   : status={conc['status']}  basis={conc.get('basis')}")
    if conc["status"] != "COMPUTABLE":
        print(f"  reason        : {conc.get('reason')}  <-- fail-closed; the gate will BLOCK")
        return 1

    limit = LIMITS.max_asset_concentration_ratio
    if not conc["by_symbol"]:
        print("positions       : none held")
    for key, share in sorted(conc["by_symbol"].items(), key=lambda kv: -kv[1]):
        print(f"  {key:16s} {share:7.3f} of equity   {'OVER' if share > limit else 'within'} {limit:.2f}")
    print(f"net exposure    : {report['directional_exposure'].get('net_exposure_ratio'):7.3f}  (limit {LIMITS.max_net_exposure_ratio:.2f})")

    equity = status.equity
    ok = True
    print("\nprobe trades (evaluated, never submitted):")
    for pct, expect in ((0.01, "ALLOW"), (0.15, "BLOCK")):
        hypothetical = OBS.PositionRecord(
            venue="ALPACA", symbol=PROBE_SYMBOL, direction="LONG", quantity=1.0,
            entry_price=equity * pct, leverage=None, mark_price=equity * pct,
            notional_usd=equity * pct, notional_basis="ENTRY_PRICE_ESTIMATE",
            unrealized_pnl_usd=None, liquidation_price=None, raw_source_id=None,
            as_of=snapshot.as_of,
        )
        decision = ENF.evaluate_hypothetical_trade(
            snapshot, [], hypothetical, LIMITS, max_snapshot_age_seconds=86400,
        )
        verdict = next(v for v in decision.dimension_verdicts if v.dimension == "asset_concentration")
        blocked = {v.dimension for v in decision.dimension_verdicts if v.verdict == "BLOCK"}
        blockers = sorted(blocked - HISTORY_DEPENDENT)
        history_blocked = sorted(blocked & HISTORY_DEPENDENT)
        got = "BLOCK" if verdict.verdict == "BLOCK" else "ALLOW"
        flag = "ok " if got == expect else "!! "
        ok = ok and got == expect
        print(f"  {flag}{pct:.0%} of equity -> concentration={verdict.verdict:20s} expected {expect}")
        if blockers:
            print(f"       other blocking dimensions: {blockers}")
        if history_blocked:
            print(f"       (ignored here, no equity history passed: {history_blocked})")

    if not ok:
        print("\nFAIL: the concentration gate did not behave as designed.")
        return 1
    print("\nPASS: oversized legacy holdings no longer veto unrelated symbols;")
    print("      a trade that would itself breach 10% of equity is still refused.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
