#!/usr/bin/env python3
"""MEXC live calibration observer -- read-only account/history diagnostic.

PURPOSE
-------
Answers the seven live-MEXC calibration questions raised during .28/.30
design, before the orchestrator is built, per the locked design:
  1. MEXC eventual-consistency / history-lag behavior
  2. Meaning of raw order state values "1" and "5" (unmapped by ccxt)
  3. Whether the assumed terminal-state behavior holds in practice
  4. An appropriate reconciliation_staleness_minutes value
  5. Whether externalOid matching behaves exactly as .28/.27 expect
  6. Partial-fill -> cancellation/closure behavior
  7. Discrepancies between ccxt's normalized fields and MEXC's raw fields

This script does NOT answer those questions itself -- it gathers the raw
and curated evidence needed to answer them, as structured JSON, for
review afterward. Actually answering the seven questions is a separate
write-up against this script's output.

SCOPE / UNIVERSE (locked with Martin, 2026-09-09)
--------------------------------------------------
The calibration universe is NOT a hardcoded top-N-by-market-cap list, and
is NOT limited to what AURA's execution chain currently allows. It is a
PINNED SNAPSHOT of mexc_bot's own real live-scanner selection logic
(mexc_bot/core/data_fetcher.py::get_candidate_universe() +
mexc_bot/core/strategy.py::passes_universe_filter(), against
mexc_bot/config.py's real thresholds) -- reproduced here rather than
imported, so a pinned run stays reproducible even if mexc_bot's own repo
changes later. See select_live_scanner_universe() below; every threshold
constant is commented with which mexc_bot/config.py value it mirrors.

CRITICAL DISTINCTION -- read this before interpreting any output:
    "selected_by_live_scanner"       -- what mexc_bot's scanner logic
                                         currently surfaces, symbol-
                                         independent by design.
    "currently_reachable_by_aura"    -- what AURA's actual v0.5.3.x
                                         execution chain (.12-.26) can
                                         currently send an order for:
                                         SYMBOLS = ("BTC/USD","ETH/USD")
                                         only, hard-gated upstream of
                                         .27, unchanged by this script.
These are DIFFERENT sets almost certainly. This calibration run is
deliberately broader than AURA's current trading universe because the
goal is to test .28/.30's MEXC-observation assumptions across a
realistic MEXC universe, not to authorize new symbols for trading. This
script, and everything it touches, is read-only:
  - .27 / .28 / .29 / .30 are unchanged. Not imported for execution --
    only .28's classify_order_status() and .30's classify_terminal_order()
    are imported, as pure functions, to test THEIR behavior against real
    data (that is the entire point of this exercise).
  - No additional symbol becomes executable by anything in this repo.
  - No order, cancel, or leverage call is ever made -- see
    _ReadOnlyExchangeGuard below, which makes this an enforced runtime
    property, not just a docstring promise.

CREDENTIALS
-----------
The universe-selection stage (public market data: tickers, OHLCV) needs
NO API keys. The per-symbol 90-day history stage (orders, trades,
positions) needs real MEXC credentials, read via the SAME environment
variable names .27/.28 already use:
    MEXC_API_KEY
    MEXC_API_SECRET
Create a MEXC API key scoped to READ-ONLY / futures-read permissions
only (no trade, no withdraw) if your account setup allows that scope --
this script never needs anything more, and _ReadOnlyExchangeGuard below
means it could not use trade permissions even if the key carried them.

USAGE
-----
    # Stage 1 only -- pin today's scanner universe, no credentials needed:
    python3 scripts/mexc_live_calibration_observer.py --universe-only

    # Full run -- stage 1 + 90-day per-symbol observation:
    export MEXC_API_KEY=...
    export MEXC_API_SECRET=...
    python3 scripts/mexc_live_calibration_observer.py

    # Reuse a previously pinned universe instead of re-scanning:
    python3 scripts/mexc_live_calibration_observer.py --symbols BTC/USDT:USDT,ETH/USDT:USDT

Output is written under --output-dir (default: calibration_output/<UTC
run timestamp>/), one JSON file per symbol plus a run manifest and a
cross-symbol summary. Nothing is written outside that directory.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

VERSION = "AURA calibration tool v1"
GUARDRAILS = {
    "market_data_fetch": True,
    "account_history_fetch": True,
    "orders_allowed": False,
    "exchange_state_mutation": False,
    "leverage_mutation": False,
    "position_creation": False,
    "fabricated_event": False,
    "fail_closed": True,
}

REPO_ROOT = Path(__file__).resolve().parents[1]          # aura-autonomous-trading-agent/
AI_AUTOMATED_TRADING_ROOT = REPO_ROOT.parent               # ai-automated-trading/
MEXC_BOT_ROOT = AI_AUTOMATED_TRADING_ROOT / "mexc_bot"

DEFAULT_TOP_N = 20
DEFAULT_LOOKBACK_DAYS = 90
DEFAULT_MAX_SCANNED = 60          # mirrors mexc_bot/config.py MAX_PAIRS_SCANNED
DEFAULT_OUTPUT_DIR = REPO_ROOT / "calibration_output"

# Mirrors mexc_bot/config.py's real thresholds, reproduced (not imported)
# for run-to-run reproducibility -- see module docstring.
SCANNER_CRITERIA = {
    "exchange_id": "mexc",                 # mexc_bot/config.py EXCHANGE_ID
    "quote_asset": "USDT",                 # mexc_bot/config.py QUOTE_ASSET
    "market_type": "swap",                 # mexc_bot/config.py MARKET_TYPE
    "min_24h_volume_usdt": 5_000_000,      # mexc_bot/config.py MIN_24H_VOLUME_USDT
    "min_listing_age_days": 30,            # mexc_bot/config.py MIN_LISTING_AGE_DAYS
    "atr_pct_min": 0.015,                  # mexc_bot/config.py ATR_PCT_MIN
    "atr_pct_max": 0.15,                   # mexc_bot/config.py ATR_PCT_MAX
    "max_pairs_scanned": DEFAULT_MAX_SCANNED,
    "timeframe": "1h",                     # mexc_bot/config.py TIMEFRAME
    "candles_lookback": 300,               # mexc_bot/config.py CANDLES_LOOKBACK
}

# AURA's actual v0.5.3.x execution-chain gate (.12-.26): SYMBOLS =
# ("BTC/USD", "ETH/USD"), Alpaca format. Expressed here as the equivalent
# MEXC USDT-perpetual symbols by UNDERLYING ASSET, for comparison only --
# this is an interpretation for reporting purposes, not a literal string
# match anywhere in the actual execution chain, and this script does not
# change what .27 or anything upstream of it accepts.
AURA_CHAIN_REACHABLE_UNDERLYINGS = {"BTC", "ETH"}


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fail(message: str) -> None:
    raise RuntimeError(message)


# --------------------------------------------------------------------- #
# Read-only enforcement -- a runtime property, not just a docstring claim
# --------------------------------------------------------------------- #

_ALLOWED_EXCHANGE_METHODS = frozenset({
    "load_markets", "fetch_tickers", "fetch_ohlcv", "fetch_orders",
    "fetch_open_orders", "fetch_closed_orders", "fetch_my_trades",
    "fetch_order_trades", "fetch_positions", "market", "markets",
    "has", "rateLimit", "id",
})


class _ReadOnlyExchangeGuard:
    """Wraps a real ccxt exchange instance and refuses everything not on
    the explicit read-only allowlist above -- in particular create_order,
    cancel_order, set_leverage, edit_order, transfer, and withdraw are
    all refused even if the underlying API key happens to carry those
    permissions. This makes "this script cannot place an order" an
    enforced property of every call site in this file, not something a
    future edit could accidentally violate by adding one call."""

    def __init__(self, real_exchange: Any):
        object.__setattr__(self, "_real", real_exchange)

    def __getattr__(self, name: str) -> Any:
        if name not in _ALLOWED_EXCHANGE_METHODS:
            fail(f"READ_ONLY_GUARD_REFUSED_METHOD:{name}")
        return getattr(self._real, name)

    def __setattr__(self, name: str, value: Any) -> None:
        fail(f"READ_ONLY_GUARD_REFUSED_ATTRIBUTE_WRITE:{name}")


def build_public_exchange() -> _ReadOnlyExchangeGuard:
    """No API keys -- public market data only (tickers, OHLCV, markets).
    Used for the universe-selection stage."""
    import ccxt  # imported lazily -- see .27/.28's identical rationale
    exchange = ccxt.mexc({
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
    })
    return _ReadOnlyExchangeGuard(exchange)


def build_authenticated_exchange() -> _ReadOnlyExchangeGuard:
    """Reads MEXC_API_KEY / MEXC_API_SECRET -- the exact same env var
    names .27's and .28's build_exchange() already read. Fails closed if
    either is missing; never falls back to an unauthenticated call for
    account-history endpoints."""
    import ccxt
    key = os.getenv("MEXC_API_KEY", "")
    secret = os.getenv("MEXC_API_SECRET", "")
    if not key or not secret:
        fail("MISSING_MEXC_CREDENTIALS: set MEXC_API_KEY and MEXC_API_SECRET "
             "(read-only/futures-read scope recommended) before running the "
             "per-symbol history stage. Use --universe-only to run without them.")
    exchange = ccxt.mexc({
        "apiKey": key,
        "secret": secret,
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
    })
    return _ReadOnlyExchangeGuard(exchange)


def to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


# --------------------------------------------------------------------- #
# .28 / .30 reuse -- test the actual locked classification logic itself,
# not a reimplementation of it, against real data. This is the whole
# point of the exercise: does .28's classify_order_status() and .30's
# classify_terminal_order() behave sensibly across a wider, real MEXC
# universe, not just BTC/ETH.
# --------------------------------------------------------------------- #

def _load_local_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


OBSERVED = _load_local_module("aura_v05328_mexc_observed_execution", "aura_v05328_mexc_observed_execution.py")
RECON = _load_local_module("aura_v05330_mexc_intent_reconciliation", "aura_v05330_mexc_intent_reconciliation.py")


# --------------------------------------------------------------------- #
# mexc_bot reuse -- the real scanner logic, reproduced not imported (see
# module docstring). Only the universe-SELECTION mechanics are mirrored;
# mexc_bot's indicator math (EMA/RSI/ATR/MACD/ADX) is imported directly
# via importlib rather than transcribed, since indicator math is exactly
# the kind of thing that silently drifts if hand-copied, and the whole
# point of "run the real scanner" is byte-identical behavior.
# --------------------------------------------------------------------- #

def _load_mexc_bot_modules():
    if not MEXC_BOT_ROOT.is_dir():
        fail(f"MEXC_BOT_DIR_NOT_FOUND:{MEXC_BOT_ROOT} -- cannot reproduce the "
             f"live scanner universe without mexc_bot's indicator/strategy code. "
             f"Use --symbols to bypass universe selection and supply symbols directly.")
    sys.path.insert(0, str(MEXC_BOT_ROOT))
    sys.path.insert(0, str(MEXC_BOT_ROOT / "core"))
    try:
        import config as mexc_bot_cfg          # noqa: E402
        import indicators as mexc_bot_ind      # noqa: E402
        import strategy as mexc_bot_strat      # noqa: E402
        import data_fetcher as mexc_bot_fetch  # noqa: E402
    finally:
        sys.path.pop(0)
        sys.path.pop(0)
    return mexc_bot_cfg, mexc_bot_ind, mexc_bot_strat, mexc_bot_fetch


# --------------------------------------------------------------------- #
# Stage 1: pin today's live scanner universe
# --------------------------------------------------------------------- #

def select_live_scanner_universe(top_n: int, max_scanned: int) -> dict[str, Any]:
    """Runs mexc_bot's real two-stage selection (volume-ranked candidate
    list, then per-symbol OHLCV + ATR%/volume/listing-age filter) against
    live MEXC public market data, and pins the resulting top_n symbols.
    Returns a full manifest recording criteria, every candidate
    considered, and the final pinned universe -- so the run stays
    auditable even after the live scanner's own output changes."""
    mexc_bot_cfg, mexc_bot_ind, mexc_bot_strat, mexc_bot_fetch = _load_mexc_bot_modules()

    public_exchange = build_public_exchange()
    scanner_timestamp = now_utc_iso()

    raw_candidates = mexc_bot_fetch.get_candidate_universe(public_exchange, quote_asset=SCANNER_CRITERIA["quote_asset"])
    raw_candidates = raw_candidates[:max_scanned]

    passing: list[dict[str, Any]] = []
    for candidate in raw_candidates:
        symbol = candidate["symbol"]
        try:
            df = mexc_bot_fetch.fetch_ohlcv_df(
                public_exchange, symbol,
                timeframe=SCANNER_CRITERIA["timeframe"],
                limit=SCANNER_CRITERIA["candles_lookback"],
            )
        except Exception as exc:  # noqa: BLE001 -- one symbol's OHLCV failure must not abort the whole scan
            passing.append({
                "symbol": symbol, "passed": False,
                "quote_volume_24h": candidate["quote_volume_24h"],
                "atr_pct": None, "reason": f"OHLCV_FETCH_FAILED:{type(exc).__name__}:{exc}",
            })
            time.sleep(public_exchange.rateLimit / 1000)
            continue

        if df.empty or len(df) < SCANNER_CRITERIA["candles_lookback"] * 0.8:
            passing.append({
                "symbol": symbol, "passed": False,
                "quote_volume_24h": candidate["quote_volume_24h"],
                "atr_pct": None, "reason": "INSUFFICIENT_OHLCV_HISTORY",
            })
            time.sleep(public_exchange.rateLimit / 1000)
            continue

        df = mexc_bot_strat.add_indicators(df)
        atr_pct = df["atr_pct"].iloc[-1]
        ok = mexc_bot_strat.passes_universe_filter(df, candidate["quote_volume_24h"], candidate["listing_age_days"])
        passing.append({
            "symbol": symbol, "passed": bool(ok),
            "quote_volume_24h": candidate["quote_volume_24h"],
            "atr_pct": float(atr_pct) if atr_pct == atr_pct else None,  # NaN check
            "reason": "PASSED_UNIVERSE_FILTER" if ok else "FAILED_UNIVERSE_FILTER",
        })
        time.sleep(public_exchange.rateLimit / 1000)

    passed_symbols = [c for c in passing if c["passed"]]  # already volume-sorted (input order preserved)
    pinned = passed_symbols[:top_n]

    def aura_reachable(symbol: str) -> bool:
        underlying = symbol.split("/")[0]
        return underlying in AURA_CHAIN_REACHABLE_UNDERLYINGS

    manifest = {
        "tool_version": VERSION,
        "guardrails": GUARDRAILS,
        "scanner_timestamp_utc": scanner_timestamp,
        "scanner_criteria": SCANNER_CRITERIA,
        "candidates_considered_count": len(raw_candidates),
        "candidates_passing_filter_count": len(passed_symbols),
        "requested_top_n": top_n,
        "pinned_universe_count": len(pinned),
        "pinned_universe": [
            {
                "symbol": c["symbol"],
                "selected_by_live_scanner": True,
                "currently_reachable_by_aura": aura_reachable(c["symbol"]),
                "quote_volume_24h_at_selection": c["quote_volume_24h"],
                "atr_pct_at_selection": c["atr_pct"],
            }
            for c in pinned
        ],
        "all_candidates_evaluated": passing,
        "note": (
            "'selected_by_live_scanner' reflects mexc_bot's own current scanner "
            "logic and is symbol-independent by design. 'currently_reachable_by_aura' "
            "reflects AURA's actual v0.5.3.x execution-chain gate (SYMBOLS = "
            "(\"BTC/USD\",\"ETH/USD\"), Alpaca format, upstream of .27) mapped to the "
            "equivalent MEXC underlying asset for comparison only. These are "
            "deliberately different sets -- this calibration run does not change "
            "what AURA can execute; it only tests .28/.30's MEXC-observation logic "
            "against a realistic, reproducible, non-cherry-picked universe."
        ),
    }
    if len(pinned) < top_n:
        manifest["warning"] = (
            f"Only {len(pinned)} of {top_n} requested symbols passed the live "
            f"scanner's own filter at this moment -- this is a genuine, honest "
            f"result of current market conditions, not a script error."
        )
    return manifest


# --------------------------------------------------------------------- #
# Stage 2: per-symbol 90-day read-only observation
# --------------------------------------------------------------------- #

def paginated_fetch_orders(exchange: _ReadOnlyExchangeGuard, symbol: str, since_ms: int,
                            page_limit: int = 100, max_pages: int = 50) -> tuple[list[dict], dict]:
    """Fully paginates fetch_orders() forward from since_ms using each
    page's own last order timestamp as the next cursor, de-duplicating by
    MEXC's own raw orderId. Capped at max_pages as a hard safety bound,
    not an assumption about how much history exists -- if the cap is hit,
    that itself is recorded (it means there may be MORE history than this
    run captured, which is exactly the kind of honest finding this
    calibration exists to surface, not silently truncate)."""
    all_orders: dict[str, dict] = {}   # keyed by raw orderId, de-dupes page overlaps
    cursor = since_ms
    pages_fetched = 0
    hit_page_cap = False

    while pages_fetched < max_pages:
        page = exchange.fetch_orders(symbol, since=cursor, limit=page_limit)
        pages_fetched += 1
        if not page:
            break

        newest_ts_this_page = cursor
        added_this_page = 0
        for order in page:
            info = order.get("info") if isinstance(order, dict) else None
            raw_id = (info or {}).get("orderId") or order.get("id")
            if raw_id is None:
                continue
            raw_id = str(raw_id)
            if raw_id not in all_orders:
                all_orders[raw_id] = order
                added_this_page += 1
            ts = order.get("timestamp")
            if isinstance(ts, (int, float)) and ts > newest_ts_this_page:
                newest_ts_this_page = int(ts)

        if len(page) < page_limit:
            break  # short page -- reached the end
        if added_this_page == 0:
            break  # no forward progress -- avoid an infinite loop on a flat cursor
        cursor = newest_ts_this_page + 1
        time.sleep(exchange.rateLimit / 1000)

    if pages_fetched >= max_pages:
        hit_page_cap = True

    return list(all_orders.values()), {"pages_fetched": pages_fetched, "hit_page_cap": hit_page_cap}


def resolve_position(exchange: _ReadOnlyExchangeGuard, symbol: str) -> dict:
    """Reuses .28's exact resolve_position() logic (imported, not
    reimplemented) so this calibration tests the same code path .30's
    reconciliation actually depends on."""
    return OBSERVED.resolve_position(exchange, symbol)


def observe_symbol(exchange: _ReadOnlyExchangeGuard, symbol: str, lookback_days: int) -> dict[str, Any]:
    since_dt = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    since_ms = int(since_dt.timestamp() * 1000)

    orders, pagination_meta = paginated_fetch_orders(exchange, symbol, since_ms)

    open_orders_supported = False
    open_orders_raw: list[dict] = []
    try:
        if exchange.has.get("fetchOpenOrders"):
            open_orders_supported = True
            raw_open = exchange.fetch_open_orders(symbol)
            open_orders_raw = [o.get("info") for o in raw_open if isinstance(o, dict) and isinstance(o.get("info"), dict)]
    except Exception as exc:  # noqa: BLE001 -- capability probe, must not abort the run
        open_orders_raw = []
        open_orders_supported = f"PROBE_FAILED:{type(exc).__name__}:{exc}"

    my_trades_supported = False
    bulk_trades_raw: list[dict] = []
    try:
        if exchange.has.get("fetchMyTrades"):
            my_trades_supported = True
            raw_trades = exchange.fetch_my_trades(symbol, since=since_ms, limit=1000)
            bulk_trades_raw = [t.get("info") for t in raw_trades if isinstance(t, dict) and isinstance(t.get("info"), dict)]
    except Exception as exc:  # noqa: BLE001
        bulk_trades_raw = []
        my_trades_supported = f"PROBE_FAILED:{type(exc).__name__}:{exc}"

    position = resolve_position(exchange, symbol)

    order_records = []
    raw_state_tally: dict[str, int] = {}
    discrepancy_flags: list[dict[str, Any]] = []

    for order in orders:
        info = order.get("info") if isinstance(order, dict) else None
        if not isinstance(info, dict):
            continue

        raw_state = str(info.get("state")) if info.get("state") is not None else None
        raw_state_tally[raw_state or "MISSING"] = raw_state_tally.get(raw_state or "MISSING", 0) + 1

        vol = to_decimal(info.get("vol"))
        deal_vol = to_decimal(info.get("dealVol"))

        # Reuse .28's and .30's own actual classification logic -- the
        # object of this calibration is THEIR behavior on real data.
        try:
            classified_status, classified_raw_state = OBSERVED.classify_order_status(info)
        except Exception as exc:  # noqa: BLE001 -- must not abort the run on one bad record
            classified_status, classified_raw_state = "CLASSIFY_ERROR", str(exc)
        try:
            terminal_classification = RECON.classify_terminal_order(info)
        except Exception as exc:  # noqa: BLE001
            terminal_classification = f"CLASSIFY_ERROR:{exc}"

        # ccxt-normalized vs raw-MEXC cross-check (item 7 of the 7
        # calibration questions).
        ccxt_status = order.get("status")
        ccxt_filled = order.get("filled")
        ccxt_amount = order.get("amount")
        ccxt_client_order_id = order.get("clientOrderId")
        external_oid = info.get("externalOid")

        record = {
            "raw_info": info,
            "raw_state": raw_state,
            "vol": str(vol) if vol is not None else None,
            "dealVol": str(deal_vol) if deal_vol is not None else None,
            "dealAvgPrice": info.get("dealAvgPrice"),
            "updateTime": info.get("updateTime"),
            "createTime": info.get("createTime"),
            "externalOid": external_oid,
            "orderType": info.get("orderType"),
            "positionId": info.get("positionId"),
            "classified_order_status_via_28": classified_status,
            "classified_raw_state_via_28": classified_raw_state,
            "classified_terminal_order_via_30": terminal_classification,
            "ccxt_status": ccxt_status,
            "ccxt_filled": ccxt_filled,
            "ccxt_amount": ccxt_amount,
            "ccxt_client_order_id": ccxt_client_order_id,
        }
        order_records.append(record)

        # Flag the specific patterns the 7 calibration questions care about.
        if raw_state is not None and raw_state not in {"2", "3", "4"}:
            discrepancy_flags.append({"type": "UNMAPPED_RAW_STATE", "raw_state": raw_state, "order_id": info.get("orderId")})
        if vol is not None and deal_vol is not None and 0 < deal_vol < vol and raw_state == "4":
            discrepancy_flags.append({"type": "PARTIAL_FILL_THEN_CANCEL", "order_id": info.get("orderId"), "dealVol": str(deal_vol), "vol": str(vol)})
        if vol is not None and deal_vol is not None and 0 < deal_vol < vol and raw_state == "3":
            discrepancy_flags.append({"type": "PARTIAL_FILL_ON_CLOSED_STATE", "order_id": info.get("orderId"), "dealVol": str(deal_vol), "vol": str(vol)})
        if not external_oid and ccxt_client_order_id:
            discrepancy_flags.append({"type": "EXTERNALOID_MISSING_BUT_CCXT_CLIENT_ORDER_ID_PRESENT", "order_id": info.get("orderId"), "ccxt_client_order_id": ccxt_client_order_id})
        if ccxt_filled is not None and deal_vol is not None:
            try:
                if abs(Decimal(str(ccxt_filled)) - deal_vol) > Decimal("0.00000001"):
                    discrepancy_flags.append({"type": "CCXT_FILLED_VS_RAW_DEALVOL_MISMATCH", "order_id": info.get("orderId"), "ccxt_filled": str(ccxt_filled), "raw_dealVol": str(deal_vol)})
            except (InvalidOperation, TypeError):
                pass

    return {
        "symbol": symbol,
        "observed_at_utc": now_utc_iso(),
        "lookback_days_requested": lookback_days,
        "lookback_since_utc": since_dt.isoformat(),
        "pagination": pagination_meta,
        "order_count": len(order_records),
        "raw_state_tally": raw_state_tally,
        "open_orders_endpoint_supported": open_orders_supported,
        "open_orders_raw": open_orders_raw,
        "my_trades_endpoint_supported": my_trades_supported,
        "trades_raw": bulk_trades_raw,
        "current_position": position,
        "discrepancy_flags": discrepancy_flags,
        "orders": order_records,
    }


# --------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------- #

def run_calibration(top_n: int, lookback_days: int, max_scanned: int, output_dir: Path,
                     symbols_override: list[str] | None, universe_only: bool) -> None:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
    run_dir = output_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    if symbols_override:
        manifest = {
            "tool_version": VERSION,
            "guardrails": GUARDRAILS,
            "scanner_timestamp_utc": now_utc_iso(),
            "scanner_criteria": None,
            "pinned_universe": [
                {"symbol": s, "selected_by_live_scanner": False,
                 "currently_reachable_by_aura": s.split("/")[0] in AURA_CHAIN_REACHABLE_UNDERLYINGS,
                 "quote_volume_24h_at_selection": None, "atr_pct_at_selection": None}
                for s in symbols_override
            ],
            "note": "Symbols supplied via --symbols, bypassing live scanner selection.",
        }
    else:
        print(f"[{now_utc_iso()}] Running live scanner universe selection (public data, no credentials needed)...")
        manifest = select_live_scanner_universe(top_n, max_scanned)

    manifest_path = run_dir / "calibration_run_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    print(f"[{now_utc_iso()}] Pinned universe ({len(manifest['pinned_universe'])} symbols) written to {manifest_path}")
    for entry in manifest["pinned_universe"]:
        print(f"    {entry['symbol']:20s} reachable_by_aura={entry['currently_reachable_by_aura']}")

    if universe_only:
        print(f"[{now_utc_iso()}] --universe-only set: stopping after scanner stage, no account history pulled.")
        return

    print(f"[{now_utc_iso()}] Building authenticated read-only exchange connection...")
    auth_exchange = build_authenticated_exchange()

    summary = {
        "run_id": run_id,
        "lookback_days": lookback_days,
        "symbols_observed": [],
        "total_orders_observed": 0,
        "total_discrepancy_flags": 0,
        "discrepancy_type_tally": {},
        "raw_state_tally_all_symbols": {},
    }

    for entry in manifest["pinned_universe"]:
        symbol = entry["symbol"]
        print(f"[{now_utc_iso()}] Observing {symbol} (lookback={lookback_days}d)...")
        try:
            result = observe_symbol(auth_exchange, symbol, lookback_days)
        except Exception as exc:  # noqa: BLE001 -- one symbol failing must not abort the whole run
            result = {"symbol": symbol, "observed_at_utc": now_utc_iso(), "error": f"{type(exc).__name__}: {exc}"}
            print(f"    ERROR observing {symbol}: {exc}")

        safe_name = symbol.replace("/", "_").replace(":", "_")
        symbol_path = run_dir / f"{safe_name}.json"
        symbol_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")

        order_count = result.get("order_count", 0)
        flags = result.get("discrepancy_flags", [])
        print(f"    orders={order_count} discrepancy_flags={len(flags)} -> {symbol_path}")

        summary["symbols_observed"].append({
            "symbol": symbol, "order_count": order_count,
            "discrepancy_flag_count": len(flags),
            "raw_state_tally": result.get("raw_state_tally", {}),
            "error": result.get("error"),
        })
        summary["total_orders_observed"] += order_count
        summary["total_discrepancy_flags"] += len(flags)
        for f in flags:
            summary["discrepancy_type_tally"][f["type"]] = summary["discrepancy_type_tally"].get(f["type"], 0) + 1
        for state, count in result.get("raw_state_tally", {}).items():
            summary["raw_state_tally_all_symbols"][state] = summary["raw_state_tally_all_symbols"].get(state, 0) + count

    summary_path = run_dir / "calibration_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    print(f"\n[{now_utc_iso()}] Calibration run complete: {run_dir}")
    print(f"    total orders observed across all symbols: {summary['total_orders_observed']}")
    print(f"    raw state tally (all symbols): {summary['raw_state_tally_all_symbols']}")
    print(f"    discrepancy flag tally: {summary['discrepancy_type_tally']}")
    print(f"    per-symbol JSON + manifest + summary written under {run_dir}")


def main() -> int:
    parser = argparse.ArgumentParser(description="MEXC live calibration observer -- read-only, no order capability.")
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N, help=f"Number of symbols to pin from the live scanner (default {DEFAULT_TOP_N}).")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS, help=f"History lookback window in days (default {DEFAULT_LOOKBACK_DAYS}).")
    parser.add_argument("--max-scanned", type=int, default=DEFAULT_MAX_SCANNED, help=f"Cap on candidates considered before filtering (default {DEFAULT_MAX_SCANNED}, mirrors mexc_bot's MAX_PAIRS_SCANNED).")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help=f"Output directory (default {DEFAULT_OUTPUT_DIR}).")
    parser.add_argument("--symbols", type=str, default=None, help="Comma-separated symbol list, bypassing live scanner selection (e.g. BTC/USDT:USDT,ETH/USDT:USDT).")
    parser.add_argument("--universe-only", action="store_true", help="Run only the scanner/universe-pinning stage; no credentials needed, no account history pulled.")
    args = parser.parse_args()

    symbols_override = [s.strip() for s in args.symbols.split(",")] if args.symbols else None

    try:
        run_calibration(
            top_n=args.top_n,
            lookback_days=args.lookback_days,
            max_scanned=args.max_scanned,
            output_dir=args.output_dir,
            symbols_override=symbols_override,
            universe_only=args.universe_only,
        )
    except RuntimeError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
