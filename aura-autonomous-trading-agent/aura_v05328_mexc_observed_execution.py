#!/usr/bin/env python3
"""AURA v0.5.3.28 — MEXC Observed Execution Reader.

Read-only. Queries MEXC's actual order/position/trade state for exactly
one ExecutionIntent (one client_order_id + symbol pair, as produced by
v0.5.3.27's submission result) and emits a hash-chain-verified snapshot
in a NEW, versioned schema: engine == "OBSERVED_EXECUTION_MEXC_V1".

WHY A NEW ENGINE NAME, NOT v0.5.3.18's EXISTING "OBSERVED_EXECUTION"
SCHEMA
-----------------------------------------------------------------------
v0.5.3.18's observed-execution schema (order_status limited to exactly
FILLED / PARTIALLY_FILLED / REJECTED / CANCELED / PENDING) was designed
around a broker that gives clean client-order-id lookups. Direct
verification of the installed ccxt 4.5.78 MEXC implementation (this
session, 2026-09-09; see AURA_v05328_MEXC_OBSERVED_EXECUTION_SPEC.md)
established that MEXC genuinely does not: a swap order's client id only
ever appears in the raw response's `externalOid` field, never in ccxt's
own unified `clientOrderId` output; two of MEXC's raw contract order
states (`1`, `5`) are not confidently mapped by ccxt itself (its own
source comments read "# TODO: wt?"); and a fully-closed position simply
disappears from `fetch_positions()`'s response rather than reporting a
closed status. Those are real, MEXC-specific uncertainty shapes that
v0.5.3.18's five-value enum has no room for, and forcing them into
PENDING would hide the difference between "MEXC confirms this is still
open" and "this reader found no conclusive evidence yet".

Per Martin's explicit decision (2026-09-09): a sixth state, UNRESOLVED,
distinct from PENDING, carries that difference. UNRESOLVED means "AURA
queried MEXC but reconciliation has not yet established the truth" — it
must remain reconciliation/safety-blocking, exactly like
EXECUTION_UNCERTAIN upstream of it. It must NEVER be treated as
"probably rejected, safe to retry" — that would defeat the entire point
of v0.5.3.27's claim-never-released replay protection.

Wiring this into v0.5.3.18 (so Reconciliation can actually consume it)
is explicitly OUT OF SCOPE here — a separate, later step, per Martin's
own sequencing (".28 implementation -> .28 tests -> .18 integration ->
..."). This module's default output path is therefore deliberately NOT
v0.5.3.18's default observed-execution input path — writing there before
.18 understands this schema could let an unrelated/stale file be picked
up silently, which is exactly the kind of manufactured evidence this
whole chain exists to prevent.

WHAT THIS MODULE DOES AND DOES NOT DO
-----------------------------------------------------------------------
    - Reads v0.5.3.27's own submission-result JSON (client_order_id,
      symbol, observed_at) as its only input describing WHICH intent to
      observe. It does not decide which intents exist to check.
    - Makes exactly four kinds of read-only MEXC/ccxt calls:
      fetch_orders() (or fetch_open_orders(), see NOTE below),
      fetch_order_trades(), fetch_positions(). NEVER create_order(),
      cancel_order(), set_leverage(), or any state-mutating call.
    - Never fabricates a fill, a position, or a price. Every fact in its
      output traces to a specific MEXC API response, preserved verbatim
      in `raw_evidence`.
    - Matches strictly on the raw `externalOid` field (see
      resolve_order() below) — never on ccxt's unified `clientOrderId`,
      which the capability verification confirmed is always None for
      swap orders regardless of what was submitted.
    - Reports `position_exists` as a plain, symbol-level fact from
      fetch_positions() — supporting evidence only. It is NEVER treated
      as proof that THIS intent specifically filled: MEXC positions
      aggregate every fill for a symbol with no order/client-id
      linkage at all, so a downstream consumer must not attribute an
      existing position to one intent on the strength of
      position_exists alone.

NOTE on state-code mapping (raw MEXC `state`, not ccxt's unified
`status`): '2' == open, '3' == closed, '4' == canceled are the only
codes ccxt itself maps with any confidence (contract v1). Whether raw
state '3' can ever mean anything other than "closed via full fill" is
UNVERIFIED against live MEXC data or MEXC's own docs (see spec doc
Sec.4) — this module treats it conservatively: '3' maps to FILLED only
when dealVol == vol (within tolerance); any other combination, or any
state outside {2,3,4}, maps to UNRESOLVED rather than being guessed.

NOTE on the live-query gate's actual boundary (2026-09-09 code review):
AURA_MEXC_OBSERVATION_ENABLED is checked ONLY inside main() (the CLI
entrypoint) — read_observed_execution() itself does not check it. This
is intentional, and matches v0.5.3.27's identical property (its own
AURA_MEXC_LIVE_ORDERS_ENABLED gate lives only in ITS main(), not in
submit()): the CLI is the production entry point and carries the
double-gate; read_observed_execution()/submit() are lower-level
primitives that do NOT independently enforce the environment gate. A
caller that imports and calls either function directly is responsible
for enforcing authorization itself — this module does not, and should
not be described as if it does. When an AURA execution orchestrator is
eventually built to call .27/.28 directly (not through their CLIs), that
orchestrator must enforce the authorization/safety boundary itself
before calling either one — it must not rely on CLI conventions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

VERSION = "AURA v0.5.3.28"
ENGINE = "OBSERVED_EXECUTION_MEXC_V1"
EXPECTED_SOURCE_VERSION = "AURA v0.5.3.27"

SUPPORTED_MARKET_TYPE = "swap"

ALLOWED_ORDER_STATUSES = {
    "FILLED",
    "PARTIALLY_FILLED",
    "REJECTED",
    "CANCELED",
    "PENDING",
    "UNRESOLVED",
}

# Raw MEXC contract-v1 order state codes ccxt itself maps with confidence.
# Anything else (including '1' and '5', which ccxt's own source leaves
# unmapped) is UNRESOLVED, never guessed.
RAW_STATE_OPEN = "2"
RAW_STATE_CLOSED = "3"
RAW_STATE_CANCELED = "4"

DEFAULT_LOOKBACK_MINUTES = 60

DEFAULT_OUTPUT = Path(
    r"regime_output\mexc_observed_execution\mexc_observed_execution.json"
)
# Deliberately NOT v0.5.3.18's DEFAULT_OBSERVED_INPUT
# (regime_input\observed_execution\observed_execution.json) -- see module
# docstring. That path stays reserved for whatever, later, actually
# speaks v0.5.3.18's existing five-value schema.


# --------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------- #

def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def fail(message: str) -> None:
    raise RuntimeError(message)


def stable_json(obj: Any) -> str:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_json(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        fail("SUBMISSION_RESULT_NOT_OBJECT")
    return payload


def to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not d.is_finite():
        return None
    return d


# --------------------------------------------------------------------- #
# input: v0.5.3.27's own submission result
# --------------------------------------------------------------------- #

def load_submission_result(path: Path) -> dict:
    """Validates just enough of v0.5.3.27's output to know WHICH intent to
    observe. Deliberately does not require status == SUBMITTED -- a
    REJECTED or EXECUTION_UNCERTAIN result is exactly the case this
    reader exists to help resolve, not something to refuse to look at.
    A DUPLICATE_CLAIM_REJECTED or FAIL_CLOSED result has no meaningful
    client_order_id/symbol pair to observe and is refused."""
    payload = load_json(path)
    if payload.get("adapter_version") != EXPECTED_SOURCE_VERSION:
        fail("WRONG_SUBMISSION_SOURCE_VERSION")
    status = payload.get("status")
    if status in {"DUPLICATE_CLAIM_REJECTED", "FAIL_CLOSED", "BLOCKED", "VALIDATED_NO_SUBMISSION"}:
        fail(f"NOTHING_TO_OBSERVE_FOR_STATUS:{status}")

    client_order_id = payload.get("client_order_id")
    symbol = payload.get("symbol")
    if not isinstance(client_order_id, str) or not client_order_id.strip():
        fail("MISSING_CLIENT_ORDER_ID_IN_SUBMISSION_RESULT")
    if not isinstance(symbol, str) or not symbol.strip():
        fail("MISSING_SYMBOL_IN_SUBMISSION_RESULT")

    submitted_at = payload.get("observed_at")
    if not isinstance(submitted_at, str) or not submitted_at:
        fail("MISSING_OBSERVED_AT_IN_SUBMISSION_RESULT")
    try:
        submitted_dt = datetime.fromisoformat(submitted_at.replace("Z", "+00:00"))
    except ValueError:
        fail("INVALID_OBSERVED_AT_IN_SUBMISSION_RESULT")
        raise  # unreachable
    if submitted_dt.tzinfo is None:
        submitted_dt = submitted_dt.replace(tzinfo=timezone.utc)

    return {
        "client_order_id": client_order_id,
        "symbol": symbol,
        "submitted_at": submitted_at,
        "submitted_at_dt": submitted_dt,
        "submission_status": status,
        "submission_mexc_order_id": payload.get("mexc_order_id"),
    }


# --------------------------------------------------------------------- #
# exchange construction -- read credentials only, no order-placement path
# --------------------------------------------------------------------- #

def build_exchange(api_key: str = "", api_secret: str = ""):
    import ccxt  # imported here, not at module load time -- see v0.5.3.27's
    # identical rationale: keeps this module importable/testable without
    # ccxt being exercised against MEXC.

    exchange_class = getattr(ccxt, "mexc")
    exchange = exchange_class({
        "apiKey": api_key,
        "secret": api_secret,
        "enableRateLimit": True,
        "options": {"defaultType": SUPPORTED_MARKET_TYPE},
    })
    # Load markets here, explicitly, rather than relying on resolve_order()'s
    # call to fetch_orders() (which self-guards with "if self.markets is
    # None: self.load_markets()") happening to run before resolve_position()'s
    # direct exchange.market() call -- ccxt's bare market() raises
    # ExchangeError("markets not loaded") rather than auto-loading (confirmed
    # by reading ccxt/base/exchange.py directly). Doing it once, up front,
    # here, makes this module's exchange safe to use in any call order,
    # not just the order this module currently happens to use internally.
    exchange.load_markets()
    return exchange


# --------------------------------------------------------------------- #
# order resolution -- match strictly on raw externalOid, never ccxt's
# unified clientOrderId (confirmed always None for swap -- see spec doc)
# --------------------------------------------------------------------- #

def resolve_order(exchange: Any, symbol: str, client_order_id: str, since_ms: int) -> dict:
    """Lists orders in the lookback window and filters client-side on
    order['info']['externalOid']. Returns {"match_count": N,
    "order": <raw info dict or None>, "reason": <str>}. Never raises on
    zero/multiple matches -- those are legitimate, reportable outcomes,
    not adapter failures."""
    orders = exchange.fetch_orders(symbol, since=since_ms)
    matches = []
    for order in orders:
        info = order.get("info") if isinstance(order, dict) else None
        if not isinstance(info, dict):
            continue
        if info.get("externalOid") == client_order_id:
            matches.append(info)

    if len(matches) == 0:
        return {"match_count": 0, "order": None, "reason": "NO_MATCHING_ORDER_FOUND_IN_WINDOW"}
    if len(matches) > 1:
        return {"match_count": len(matches), "order": matches[0], "reason": "MULTIPLE_MATCHING_ORDERS_FOUND"}
    return {"match_count": 1, "order": matches[0], "reason": "SINGLE_MATCH"}


def classify_order_status(raw_order: dict) -> tuple[str, str]:
    """Maps one raw MEXC swap order dict to (order_status, raw_state),
    per the module docstring's NOTE. Returns UNRESOLVED, never a guess,
    on anything not confidently recognized."""
    raw_state = raw_order.get("state")
    raw_state_str = str(raw_state) if raw_state is not None else None

    vol = to_decimal(raw_order.get("vol"))
    deal_vol = to_decimal(raw_order.get("dealVol"))

    if raw_state_str == RAW_STATE_CANCELED:
        return "CANCELED", raw_state_str

    if raw_state_str == RAW_STATE_OPEN:
        if deal_vol is not None and deal_vol > 0 and vol is not None and deal_vol < vol:
            return "PARTIALLY_FILLED", raw_state_str
        return "PENDING", raw_state_str

    if raw_state_str == RAW_STATE_CLOSED:
        if vol is not None and deal_vol is not None and deal_vol == vol and vol > 0:
            return "FILLED", raw_state_str
        # "closed" but volumes don't confirm a full fill -- conservatively
        # unresolved rather than assumed. See module docstring NOTE.
        return "UNRESOLVED", raw_state_str

    # Anything else, including MEXC's own '1'/'5' (unmapped even by ccxt
    # itself) or a missing state entirely.
    return "UNRESOLVED", raw_state_str


# --------------------------------------------------------------------- #
# position lookup -- symbol-level, absence == closed, no order linkage
# --------------------------------------------------------------------- #

def resolve_position(exchange: Any, symbol: str) -> dict:
    """fetch_positions() returns only currently-OPEN positions -- a
    closed position is never reported, it simply does not appear. So
    position_exists=False is inferred from absence in a fresh call, not
    from any status field. Matches on the raw info dict's `symbol`
    (MEXC's own id form, e.g. "BTC_USDT"), since ccxt's own unified
    position `id` field is hardcoded None (confirmed by direct reading
    of parse_position())."""
    market = exchange.market(symbol)
    market_id = market.get("id") if isinstance(market, dict) else None

    positions = exchange.fetch_positions([symbol])
    for pos in positions:
        info = pos.get("info") if isinstance(pos, dict) else None
        if not isinstance(info, dict):
            continue
        if info.get("symbol") == market_id:
            hold_vol = to_decimal(info.get("holdVol"))
            if hold_vol is not None and hold_vol > 0:
                position_id = info.get("positionId")
                return {
                    "position_exists": True,
                    "position_id": str(position_id) if position_id is not None else None,
                    "raw_position": info,
                }
    return {"position_exists": False, "position_id": None, "raw_position": None}


# --------------------------------------------------------------------- #
# trade evidence -- supporting fill detail once a MEXC order id is known
# --------------------------------------------------------------------- #

def resolve_trades(exchange: Any, mexc_order_id: str, symbol: str) -> list[dict]:
    """Raw trade records (verbatim `info` dicts, unmodified) for one
    resolved MEXC order id. Never discard exchange evidence: the caller
    stores these verbatim under raw_evidence["trades"] AND derives the
    normalized `observed_fills` from them via curate_fills() -- so the
    original MEXC response is always recoverable, not just the curated
    subset. fill_price/fill_timestamp in the top-level snapshot come from
    the order's own dealAvgPrice/updateTime (MEXC's own authoritative
    fields), never from an average this module computes itself."""
    trades = exchange.fetch_order_trades(mexc_order_id, symbol)
    raw: list[dict] = []
    for t in trades:
        info = t.get("info") if isinstance(t, dict) else None
        if isinstance(info, dict):
            raw.append(info)
    return raw


def curate_fills(raw_trades: list[dict]) -> list[dict]:
    """Normalized subset of raw trade evidence for `observed_fills` --
    convenient for AURA's own logic. The raw dicts this was derived from
    are preserved separately (see resolve_trades()), never discarded."""
    return [
        {
            "trade_id": info.get("id"),
            "price": info.get("price"),
            "vol": info.get("vol"),
            "fee": info.get("fee"),
            "timestamp": info.get("timestamp"),
        }
        for info in raw_trades
    ]


# --------------------------------------------------------------------- #
# snapshot assembly + hash chain
# --------------------------------------------------------------------- #

def base_snapshot() -> dict[str, Any]:
    return {
        "agent_version": VERSION,
        "engine": ENGINE,
        "snapshot_status": "BLOCKED",
        "client_order_id": None,
        "symbol": None,
        "mexc_order_id": None,
        "order_status": None,
        "raw_status": None,
        "match_count": None,
        "position_exists": False,
        "position_id": None,
        "fill_price": None,
        "fill_timestamp": None,
        "observed_fills": [],
        "lookback_window_start": None,
        "queried_at": None,
        "source_submission_status": None,
        "reason": None,
        "raw_evidence": {},
        "guardrails": {
            "read_only": True,
            "orders_allowed": False,
            "exchange_state_mutation": False,
            "position_creation": False,
            "live_execution": False,
            "fabricated_fill": False,
            "fabricated_position": False,
            "client_order_id_source": "raw_info_externalOid_only",
            "fail_closed": True,
        },
    }


def canonical_snapshot_hash(payload: dict[str, Any]) -> str:
    canonical = {
        "agent_version": payload.get("agent_version"),
        "engine": payload.get("engine"),
        "snapshot_status": payload.get("snapshot_status"),
        "client_order_id": payload.get("client_order_id"),
        "symbol": payload.get("symbol"),
        "mexc_order_id": payload.get("mexc_order_id"),
        "order_status": payload.get("order_status"),
        "raw_status": payload.get("raw_status"),
        "match_count": payload.get("match_count"),
        "position_exists": payload.get("position_exists"),
        "position_id": payload.get("position_id"),
        "fill_price": payload.get("fill_price"),
        "fill_timestamp": payload.get("fill_timestamp"),
        "observed_fills": payload.get("observed_fills"),
    }
    return sha256_text(stable_json(canonical))


def finalize(snapshot: dict[str, Any]) -> dict[str, Any]:
    """The single funnel point every code path passes through before
    output. Two things are enforced here, unconditionally, so neither can
    be skipped by a future call site: the hash is always freshly
    recomputed (see module docstring / code review sec.4), and an
    OBSERVED snapshot's order_status is always a recognized value.

    The second check is defense in depth, not currently reachable through
    this module's own logic (every assignment site already only ever sets
    a member of ALLOWED_ORDER_STATUSES) -- but ALLOWED_ORDER_STATUSES
    existed as a documented constraint without an active check until this
    fix. No future ccxt/MEXC change, or bug, gets to silently introduce a
    status this module has not deliberately verified: anything outside
    the allowlist is normalized to UNRESOLVED here, with the original
    value preserved in raw_evidence for audit -- never silently dropped,
    never silently trusted."""
    if snapshot.get("snapshot_status") == "OBSERVED" and snapshot.get("order_status") not in ALLOWED_ORDER_STATUSES:
        snapshot.setdefault("raw_evidence", {})["unrecognized_order_status"] = snapshot.get("order_status")
        snapshot["order_status"] = "UNRESOLVED"
        snapshot["fill_price"] = None
        snapshot["fill_timestamp"] = None
        snapshot["reason"] = "UNRECOGNIZED_ORDER_STATUS_VALUE"
    snapshot["snapshot_hash"] = canonical_snapshot_hash(snapshot)
    return snapshot


def read_observed_execution(
    submission: dict,
    lookback_minutes: int = DEFAULT_LOOKBACK_MINUTES,
    exchange: Any = None,
) -> dict[str, Any]:
    """The canonical read -- injectable `exchange` for direct unit
    testing against a fake, matching v0.5.3.27's submit() pattern.
    Never raises on any MEXC-side ambiguity; every ambiguous outcome is
    represented as UNRESOLVED in the returned, hash-finalized snapshot."""
    snapshot = base_snapshot()
    snapshot["client_order_id"] = submission["client_order_id"]
    snapshot["symbol"] = submission["symbol"]
    snapshot["source_submission_status"] = submission["submission_status"]

    since_dt = submission["submitted_at_dt"] - timedelta(minutes=lookback_minutes)
    since_ms = int(since_dt.timestamp() * 1000)
    snapshot["lookback_window_start"] = since_dt.isoformat()

    if exchange is None:
        key = os.getenv("MEXC_API_KEY", "")
        secret = os.getenv("MEXC_API_SECRET", "")
        if not key or not secret:
            fail("MISSING_MEXC_CREDENTIALS")
        exchange = build_exchange(key, secret)

    resolved = resolve_order(exchange, submission["symbol"], submission["client_order_id"], since_ms)
    snapshot["match_count"] = resolved["match_count"]
    snapshot["raw_evidence"]["order_match_reason"] = resolved["reason"]

    if resolved["match_count"] != 1:
        snapshot["order_status"] = "UNRESOLVED"
        snapshot["reason"] = resolved["reason"]
    else:
        raw_order = resolved["order"]
        snapshot["raw_evidence"]["order"] = raw_order
        snapshot["mexc_order_id"] = raw_order.get("orderId")
        order_status, raw_state = classify_order_status(raw_order)
        snapshot["order_status"] = order_status
        snapshot["raw_status"] = raw_state
        snapshot["reason"] = f"ORDER_STATE_{raw_state}" if raw_state is not None else "ORDER_STATE_MISSING"

        if order_status in {"FILLED", "PARTIALLY_FILLED"}:
            deal_avg_price = to_decimal(raw_order.get("dealAvgPrice"))
            snapshot["fill_price"] = float(deal_avg_price) if deal_avg_price is not None and deal_avg_price > 0 else None
            update_time_ms = raw_order.get("updateTime")
            if update_time_ms is not None:
                try:
                    snapshot["fill_timestamp"] = datetime.fromtimestamp(
                        int(update_time_ms) / 1000, tz=timezone.utc
                    ).isoformat()
                except (ValueError, TypeError, OverflowError):
                    snapshot["fill_timestamp"] = None
            if snapshot["fill_price"] is None or snapshot["fill_timestamp"] is None:
                # dealAvgPrice/updateTime absent despite a FILLED/PARTIALLY_FILLED
                # classification -- inconsistent evidence, fail closed rather
                # than report a fill AURA can't actually substantiate.
                snapshot["order_status"] = "UNRESOLVED"
                snapshot["reason"] = "FILL_STATUS_WITHOUT_SUBSTANTIATING_PRICE_OR_TIMESTAMP"
                snapshot["fill_price"] = None
                snapshot["fill_timestamp"] = None
            else:
                try:
                    raw_trades = resolve_trades(
                        exchange, str(raw_order.get("orderId")), submission["symbol"]
                    )
                    snapshot["raw_evidence"]["trades"] = raw_trades
                    snapshot["observed_fills"] = curate_fills(raw_trades)
                except Exception as exc:  # noqa: BLE001 -- supporting evidence only, never fail the snapshot over it
                    snapshot["raw_evidence"]["trade_lookup_error"] = f"{type(exc).__name__}: {exc}"

    position = resolve_position(exchange, submission["symbol"])
    snapshot["position_exists"] = position["position_exists"]
    snapshot["position_id"] = position["position_id"]
    if position["raw_position"] is not None:
        snapshot["raw_evidence"]["position"] = position["raw_position"]

    snapshot["queried_at"] = now()
    snapshot["snapshot_status"] = "OBSERVED"
    return finalize(snapshot)


# --------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--submission-input", required=True, type=Path,
                         help="v0.5.3.27's own output JSON for the intent to observe.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--lookback-minutes", type=int, default=DEFAULT_LOOKBACK_MINUTES)
    parser.add_argument("--query-live", action="store_true",
                         help="Actually query MEXC; otherwise validate input only, no network call.")
    args = parser.parse_args()

    snapshot = base_snapshot()
    try:
        submission = load_submission_result(args.submission_input)
        snapshot["client_order_id"] = submission["client_order_id"]
        snapshot["symbol"] = submission["symbol"]
        snapshot["source_submission_status"] = submission["submission_status"]

        if not args.query_live:
            snapshot["snapshot_status"] = "VALIDATED_NO_QUERY"
            snapshot["reason"] = "QUERY_NOT_REQUESTED"
        elif os.getenv("AURA_MEXC_OBSERVATION_ENABLED", "false").lower() != "true":
            fail("LIVE_OBSERVATION_QUERY_NOT_ENABLED")
        else:
            snapshot = read_observed_execution(submission, args.lookback_minutes)

        snapshot = finalize(snapshot)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(snapshot, indent=2, default=str) + "\n", encoding="utf-8")
        print(json.dumps(snapshot, indent=2, default=str))
        return 0
    except Exception as exc:
        snapshot["snapshot_status"] = "BLOCKED"
        snapshot["reason"] = f"{type(exc).__name__}: {exc}"
        snapshot = finalize(snapshot)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(snapshot, indent=2, default=str) + "\n", encoding="utf-8")
        print(f"FAIL-CLOSED: {snapshot['reason']}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
