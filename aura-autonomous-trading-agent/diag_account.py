"""diag_account.py -- READ-ONLY account capability check.

Answers the question the asset-level shortability flags cannot: is THIS
account permitted to open a short at all? Calls get_account() and
get_asset() only. Imports no order-submission module and submits nothing.

    call bot_credentials.bat bot3
    python diag_account.py
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


CLI = _load("aura_v05356_stage3_live_equity_cli", "aura_v05356_stage3_live_equity_cli.py")

ACCOUNT_FIELDS = (
    "account_number", "status", "currency",
    "shorting_enabled", "multiplier", "account_blocked", "trading_blocked",
    "trade_suspended_by_user", "transfers_blocked", "pattern_day_trader",
    "equity", "cash", "buying_power", "regt_buying_power",
    "daytrading_buying_power", "non_marginable_buying_power",
    "long_market_value", "short_market_value", "initial_margin",
    "maintenance_margin", "sma", "daytrade_count",
)

# The four symbols the bot actually tried to short today.
SYMBOLS = ("CMC", "KRYS", "AROC", "MOG.A")
ASSET_FIELDS = ("tradable", "shortable", "easy_to_borrow", "marginable",
                "fractionable", "status", "exchange")


def main() -> int:
    api_key, secret_key = CLI.load_equity_paper_credentials()
    client = CLI.build_trading_client(api_key, secret_key)

    account = client.get_account()
    print("=== ACCOUNT ===")
    for field in ACCOUNT_FIELDS:
        print(f"  {field:30s} {getattr(account, field, '<absent>')}")

    print("\n=== ASSETS (the symbols it tried to short) ===")
    for symbol in SYMBOLS:
        try:
            asset = client.get_asset(symbol)
        except Exception as exc:  # noqa: BLE001 -- diagnostic, report don't raise
            print(f"  {symbol:8s} get_asset failed: {type(exc).__name__}: {exc}")
            continue
        flags = "  ".join(f"{f}={getattr(asset, f, '?')}" for f in ASSET_FIELDS)
        print(f"  {symbol:8s} {flags}")

    print("\n=== READ ===")
    shorting = getattr(account, "shorting_enabled", None)
    multiplier = getattr(account, "multiplier", None)
    # Checked FIRST: this one refuses every order in both directions, so it
    # masks everything below it. (Missed in the first version of this
    # script, which reported "look elsewhere" while printing it above.)
    if getattr(account, "trade_suspended_by_user", False):
        print("  trade_suspended_by_user is True -- 'Trades Suspended' is ON in the Alpaca")
        print("  Configure page for this account. EVERY order is refused, long and short.")
        print("  Turn that toggle off; nothing else here needs changing.")
    elif getattr(account, "trading_blocked", False) or getattr(account, "account_blocked", False):
        print("  The broker has blocked this account (trading_blocked / account_blocked).")
        print("  Not something to fix in code -- take it up with Alpaca.")
    elif shorting is False:
        print("  shorting_enabled is False -- this account cannot open a short at all.")
        print("  Every DECIDE_SHORT will be refused by the broker regardless of the asset's flags.")
    elif str(multiplier) in ("1", "1.0"):
        print("  multiplier is 1 -- this is a CASH account. Shorting requires margin.")
        print("  Every DECIDE_SHORT will be refused regardless of the asset's flags.")
    elif shorting is True:
        print("  Account-level shorting is permitted, so the refusal is something else.")
        print("  Next suspect: buying power, or a per-order field the broker rejects.")
    else:
        print("  shorting_enabled was not reported; check multiplier and buying power above.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
