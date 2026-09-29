"""AURA -- read-only shortability verification for the 12 ETF symbols added
to the pinned universe by the 2026-09-29 ETF curation pass.

WHY THIS SCRIPT EXISTS
------------------------------------------------------------------------
`aura_v05351_equity_universe_v1.json` was extended from v1 (27 symbols) to
v2 (39 symbols) by adding DIA plus all 11 SPDR sector ETFs (Martin,
AskUserQuestion, 2026-09-29), per the ETF curation scope in
`AURA_Options_ETF_Expansion_Scoping_2026-09-24.md` section 1. That JSON
file's own `"source"` field says shortability for those 12 symbols is
"verified separately against the real Alpaca account, not assumed here --
see verify_new_etf_shortability.py". This is that script.

It answers one question per symbol: can `.33`-`.38`'s existing
short-execution path (already built, already tested, gated on
`shortable`/`easy_to_borrow` exactly like every other symbol in the
universe) actually short it on Martin's real Alpaca account, right now.
This script does not change that gate or any code -- it only reports what
the gate would see, so the new symbols' short-side usability is known
rather than assumed.

WHAT THIS SCRIPT IS NOT
------------------------------------------------------------------------
  - It is NOT a new shortability check. It calls the exact same read-only
    `TradingClient.get_asset()` lookup and the exact same
    `alpaca_asset_to_shortability_status()` classification that
    `aura_v05335_alpaca_equity_execution_adapter.py` already uses for
    every other symbol's real-time execution-time gate (see that module's
    docstring for the SHORTABLE / NOT_SHORTABLE / UNKNOWN rule and why
    "shortable=True but easy_to_borrow=False" is conservatively treated as
    UNKNOWN rather than SHORTABLE). Nothing here duplicates or diverges
    from that existing gate's logic.
  - It NEVER submits an order, modifies account state, or writes to the
    universe JSON. Read-only Assets API calls only.
  - It reuses `.356`'s existing `load_equity_paper_credentials()` /
    `build_trading_client()` (the exact same credential path `.356`/`.363`/
    `.365` already use in production) rather than inventing a new
    credential-loading convention. The two production env vars,
    `ALPACA_EQUITY_PAPER_API_KEY` / `ALPACA_EQUITY_PAPER_SECRET_KEY`, are
    read from the environment and never printed, logged, or written to
    the output file.

USAGE
------------------------------------------------------------------------
    python verify_new_etf_shortability.py
    python verify_new_etf_shortability.py --output shortability_report.json

Requires the same two env vars `.365` already needs to run
(`ALPACA_EQUITY_PAPER_API_KEY`, `ALPACA_EQUITY_PAPER_SECRET_KEY`) to be
set in the shell this is run from -- if `.365` is already running there,
they are already set.

Exit code is 0 only if every one of the 12 symbols was successfully
queried (regardless of whether the result is SHORTABLE, NOT_SHORTABLE, or
UNKNOWN -- those are all valid, informative answers). Exit code is 1 if
any symbol's lookup itself failed (bad symbol, network error, credential
problem, etc.) -- fail-closed, matching this repo's existing discipline of
never silently treating a failed lookup as a passing one.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent

VERSION = "AURA verify_new_etf_shortability v1"

# The 12 symbols added by the 2026-09-29 ETF curation pass -- see
# aura_v05351_equity_universe_v1.json's own "source" field for the full
# disclosure of this addition. Deliberately hardcoded here (not read from
# the universe JSON's full 39-symbol list) so this script always checks
# exactly the new additions, never silently expands scope if the universe
# grows again later.
NEW_ETF_SYMBOLS: tuple[str, ...] = (
    "DIA",
    "XLK", "XLF", "XLE", "XLV", "XLI", "XLP", "XLY", "XLB", "XLRE", "XLU", "XLC",
)


def _load_module(module_name: str, filename: str):
    """Same dynamic-import convention every module in this chain already
    uses (see e.g. aura_v05356_stage3_live_equity_cli.py's own
    `_load_module`) -- prefers an installed/importable copy, falls back to
    loading straight from this file's own directory."""
    try:
        return __import__(module_name)
    except ImportError:
        import importlib.util

        spec = importlib.util.spec_from_file_location(module_name, ROOT / filename)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod


def load_equity_cli_module():
    return _load_module("aura_v05356_stage3_live_equity_cli", "aura_v05356_stage3_live_equity_cli.py")


def load_execution_adapter_module():
    return _load_module("aura_v05335_alpaca_equity_execution_adapter", "aura_v05335_alpaca_equity_execution_adapter.py")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def verify_symbol_shortability(
    symbol: str,
    *,
    trading_client: Any,
    execution_adapter_module: Any,
) -> dict[str, Any]:
    """Read-only lookup for one symbol. Never raises on a per-symbol
    failure -- returns an OK=False entry instead, so one bad symbol never
    aborts the rest of the batch (matching `.53`'s own
    "a symbol that fails is recorded, never blocks the others" pattern)."""
    try:
        asset = execution_adapter_module.fetch_asset_metadata(trading_client, symbol)
        status = execution_adapter_module.alpaca_asset_to_shortability_status(
            asset.get("shortable"), asset.get("easy_to_borrow"),
        )
        return {
            "symbol": symbol,
            "ok": True,
            "tradable": asset.get("tradable"),
            "shortable": asset.get("shortable"),
            "easy_to_borrow": asset.get("easy_to_borrow"),
            "shortability_status": status,
            "asset_class": asset.get("asset_class"),
            "exchange": asset.get("exchange"),
            "status_field": asset.get("status"),
        }
    except Exception as exc:  # noqa: BLE001 -- deliberately broad: a bad
        # symbol, a network error, or a malformed response must all be
        # reported the same way (fail-closed for that symbol), never
        # crash the whole batch.
        return {
            "symbol": symbol,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


def build_report(results: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    all_ok = all(r["ok"] for r in results)
    return {
        "script_version": VERSION,
        "generated_at": _now_iso(),
        "paper": True,
        "live": False,
        "universe_version_this_verifies": "v2",
        "symbols_checked": list(NEW_ETF_SYMBOLS),
        "all_lookups_ok": all_ok,
        "results": list(results),
        "summary": {
            "SHORTABLE": sorted(r["symbol"] for r in results if r.get("shortability_status") == "SHORTABLE"),
            "NOT_SHORTABLE": sorted(r["symbol"] for r in results if r.get("shortability_status") == "NOT_SHORTABLE"),
            "UNKNOWN": sorted(r["symbol"] for r in results if r.get("shortability_status") == "UNKNOWN"),
            "LOOKUP_FAILED": sorted(r["symbol"] for r in results if not r["ok"]),
        },
    }


def main(argv: list[str] | None = None) -> int:  # pragma: no cover -- live
    # wiring against a real Alpaca account; the pure functions above
    # (verify_symbol_shortability, build_report) are unit-tested with a
    # fake client instead.
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, default=None,
                         help="Optional path to also write the JSON report to. Always printed to stdout.")
    args = parser.parse_args(argv)

    equity_cli = load_equity_cli_module()
    execution_adapter = load_execution_adapter_module()

    print("=" * 72)
    print("AURA -- read-only shortability check: 12 new ETF symbols (v2 universe)")
    print("=" * 72)

    try:
        api_key, secret_key = equity_cli.load_equity_paper_credentials()
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL-CLOSED: {type(exc).__name__}: {exc}")
        return 1

    trading_client = equity_cli.build_trading_client(api_key, secret_key)

    results = tuple(
        verify_symbol_shortability(symbol, trading_client=trading_client, execution_adapter_module=execution_adapter)
        for symbol in NEW_ETF_SYMBOLS
    )
    report = build_report(results)

    for r in results:
        if r["ok"]:
            print(f"  {r['symbol']:<6} shortability={r['shortability_status']:<14} "
                  f"shortable={r['shortable']} easy_to_borrow={r['easy_to_borrow']} tradable={r['tradable']}")
        else:
            print(f"  {r['symbol']:<6} LOOKUP_FAILED: {r['error']}")

    print("-" * 72)
    print(json.dumps(report["summary"], indent=2))
    print("READ-ONLY CHECK COMPLETE -- NO ORDER WAS PLACED, NOTHING WAS MODIFIED")
    print("=" * 72)

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
        print(f"Report written to {args.output}")

    return 0 if report["all_lookups_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
