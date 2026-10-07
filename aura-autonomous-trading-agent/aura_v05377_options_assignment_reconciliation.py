#!/usr/bin/env python3
"""
AURA v0.5.3.77 -- Options Assignment / Exercise Reconciliation (Phase O4B)

What this module is
------------------------------------------------------------------------
Phase O4B of the options build (`AURA_Options_ETF_Expansion_Scoping_2026-
09-24.md`, added 2026-09-24 following the Lablab hackathon 26-repo
competitor audit, ranked finding #2). Depends on `.373` (O1, contract
metadata) and `.376` (O4, execution adapter) existing -- there is nothing
to reconcile against before options positions can actually be opened.

Two independently-built hackathon systems audited earlier this project --
ThetaTrap and optionwright, both with passing test suites -- converge on
the same answer for options assignment/exercise: **never try to trade out
of it.** This module detects the mismatch between AURA's own expected
options book and the broker's real account state (an assignment, an
exercised contract, an expiry-driven auto-liquidation, or anything else
unexplained), and reports exactly which underlying(s) must be frozen to
manual-only until Martin resolves them. It is a DETECTION module. It
never attempts remediation -- no cancel, no auto-trade, no book "fix" of
any kind, anywhere in this file.

Per Martin's 2026-10-06 design decisions (all confirmed via explicit
clarifying questions before any code was written, grounded in direct
SDK introspection -- see §0):

  1. EVIDENCE SOURCES: both live positions AND account activities.
     Positions (`TradingClient.get_all_positions()`) give the primary
     "something changed" structural diff signal -- simple, always typed.
     Activities (assignment/exercise/expiration codes) explain WHY, when
     available.
  2. STATELESS: this module owns no persisted ledger. The caller supplies
     AURA's expected-position snapshot as a plain argument on every call
     -- consistent with every module built in this phase so far (O1-O4
     are all pure logic plus, at most, a network call; nothing in the
     options build persists state yet). Where that expected snapshot
     comes from, and whether/how it is kept current, is explicitly out
     of scope for this module.
  3. NO AUTO-ADJUSTMENT: unlike the ThetaTrap/optionwright precedent
     (which auto-adjusts the book on a SYMMETRIC partial fill and only
     routes ASYMMETRIC partials to human review), this module freezes on
     ANY detected mismatch, symmetric or not, with zero automated book
     changes of any kind. This is both the simpler design and the only
     one coherent with decision #2 -- there is no persisted book here to
     "adjust."
  4. LOOKBACK WINDOW: `lookback_days` is a REQUIRED caller-supplied
     parameter with no built-in default, matching this project's house
     convention throughout the options build (O3's `risk_free_rate`/
     `num_steps`, O1's explicit `multiplier`) -- never invent a magic
     number.

============================================================================
0. Pre-code inspection (done before writing anything below, per explicit
   instruction) -- alpaca-py 0.44.0, the version pinned in
   requirements.txt, directly imported and introspected this session,
   not assumed:
============================================================================

- **Critical finding**: `alpaca.trading.client.TradingClient` (the
  single-account client `.22`/`.335`/`.376` all already use) has NO
  `get_account_activities()` method. That typed convenience method only
  exists on the separate `alpaca.broker.client.BrokerClient` -- a
  different product (Alpaca's Broker API, for building a brokerage on
  top of Alpaca for many end customers), not what this single-account
  paper setup uses. Confirmed by direct `dir()` introspection of both
  classes.
- `alpaca.trading.client.TradingClient` DOES expose a generic
  `get(path, data=None, **kwargs)` escape hatch that can reach any raw
  Alpaca Trading-API REST endpoint, including `/account/activities`.
  This module uses that escape hatch (`fetch_broker_activities()` below)
  rather than fabricating a typed method that doesn't exist.
- `alpaca.trading.models.NonTradeActivity` IS a real, usable Pydantic
  model (fields: `id`, `account_id`, `activity_type`, `date`,
  `net_amount`, `description`, `status`, `symbol`, `qty`, `price`,
  `per_share_amount`) -- confirmed present even though no convenience
  method constructs it automatically from `TradingClient`. This module
  parses the raw REST response into it by hand.
- `alpaca.trading.enums.ActivityType` genuinely includes `OPASN`
  (options assignment), `OPEXC` (options exercise), and `OPEXP` (options
  expiration) -- confirmed by direct enumeration of all 35 values. These
  three are exactly the codes this module treats as assignment/exercise
  evidence (`ASSIGNMENT_EXERCISE_ACTIVITY_TYPES` below).
- `alpaca.trading.enums.NonTradeActivityStatus` is `{EXECUTED, CORRECT,
  CANCELED}`. A `CANCELED` activity is excluded from being treated as
  evidence -- it is not proof anything actually happened.
- `alpaca.trading.models.Position` fields include `symbol`, `asset_class`,
  `qty`, `side` (confirmed `PositionSide` is `{LONG: "long", SHORT:
  "short"}`). `asset_class` genuinely distinguishes `US_OPTION` from
  `US_EQUITY`/`CRYPTO`/`CRYPTO_PERP` -- this module filters to
  `us_option` only; non-option positions are not this module's concern
  and are silently excluded (not an anomaly).
- **UNVERIFIED, disclosed honestly rather than assumed**: whether
  `Position.symbol` for an options position uses the same OCC symbol
  format `.373`'s `parse_occ_symbol()` expects is assumed by convention
  (Alpaca uses OCC symbols for options contracts everywhere else
  confirmed this project -- `OptionLegRequest.symbol`, `Order.legs[].
  symbol`) but was not independently re-confirmed against a real
  position, since no options position exists in the account yet to
  inspect. If this assumption is wrong, `normalize_broker_position()`
  below surfaces it loudly as `UNPARSEABLE_BROKER_POSITION_SYMBOL`
  rather than silently mis-reading it -- see §2.
  Also UNVERIFIED: the exact query-parameter name(s) `/account/
  activities` actually honors for date-range narrowing. This module
  sends a best-effort `after=` param (§3) but never RELIES on the server
  honoring it -- the real `lookback_days` guarantee is enforced
  client-side regardless of what the server returns.

============================================================================
1. What gets compared, and how
============================================================================

    AURA's expected positions (caller-supplied, plain list of dicts)
            |
            v
    normalize_expected_position()    <- pure, re-derives underlying_symbol
                                         from occ_symbol via .373, never
                                         trusts a caller-supplied one
            |                                      Alpaca (live, paper)
            |                                              |
            |                                   fetch_broker_positions()
            |                                   fetch_broker_activities()
            |                                              |
            |                                   normalize_broker_position()
            |                                   normalize_activity()
            v                                              v
    reconcile_options_positions()  <- PURE. Diffs expected vs. broker
                                        qty per contract; classifies any
                                        mismatch using activity evidence;
                                        groups by underlying; any mismatch
                                        anywhere under an underlying
                                        freezes that whole underlying.

`reconcile()` is the one network-capable orchestrator that fetches both
evidence sources fresh (no caching -- matching optionwright's own choice
not to cache this specific read, and this project's "never fabricate or
reuse stale evidence" principle) and calls the pure core.

============================================================================
2. Classification -- loud, never silently resolved
============================================================================

Per contract (occ_symbol), comparing AURA's expected signed quantity
against the broker's live signed quantity:

  - `CONSISTENT`                      -- expected and broker quantities
                                          match exactly.
  - `ASSIGNMENT_OR_EXERCISE_DETECTED` -- quantities differ, AND at least
                                          one OPASN/OPEXC/OPEXP activity
                                          for this contract was found
                                          within the lookback window.
                                          Evidence is attached verbatim.
  - `UNEXPLAINED_POSITION_MISMATCH`   -- quantities differ and NO
                                          activity evidence explains it
                                          within the window -- the
                                          louder, more alarming case.
  - `UNPARSEABLE_BROKER_POSITION`     -- the broker reported an options
                                          position whose symbol or qty/
                                          side this module could not
                                          parse at all. Never silently
                                          skipped -- surfaced as its own
                                          anomaly against whatever
                                          underlying can be salvaged, or
                                          against `"UNKNOWN"` if even
                                          that can't be determined.

Any classification other than `CONSISTENT`, anywhere under an underlying,
freezes that entire underlying (`frozen_underlyings` in the result). A
single bad/unparseable broker record never aborts the whole reconciliation
run -- every other contract and underlying is still reconciled and
reported; the bad record becomes its own loud finding instead.

============================================================================
3. What this module deliberately does NOT do
============================================================================

No modification to `.373` or `.376` -- both read-only/imported-as-
vocabulary inputs, verified untouched after this file was written. No
automated remediation of any kind -- no cancel, no unwind, no trade, no
book adjustment, not even for a "clearly symmetric" partial (see
module docstring point 3). No persisted ledger or storage of any kind --
this module is pure/stateless end-to-end except for its two read-only
network calls. No enforcement of a freeze decision -- this module
produces data describing which underlyings must be frozen; actually
gating new order flow on that data is O9's job (the supervisor), not yet
built. No live execution of any kind -- this module never submits an
order, cancels one, or mutates account state in any way; both network
functions are READ-ONLY (confirmed: `get_all_positions()` and the raw
`get()` escape hatch against `/account/activities` are both GET-style
reads). No real MEXC credentials anywhere in this file (out of scope
entirely).
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

VERSION = "AURA v0.5.3.77"
ENGINE = "OPTIONS_ASSIGNMENT_EXERCISE_RECONCILIATION"
SCHEMA_VERSION = "1.0"

ROOT = Path(__file__).resolve().parent

ASSIGNMENT_EXERCISE_ACTIVITY_TYPES = {"OPASN", "OPEXC", "OPEXP"}
_EXCLUDED_ACTIVITY_STATUSES = {"canceled"}


def fail(message: str) -> None:
    raise RuntimeError(message)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


# --------------------------------------------------------------------- #
# Dynamic import of .373 -- same _load_*_module() try-import-first
# pattern already established by .30/.31/.335/.376. .373 is used
# strictly as a data/vocabulary source and is never modified.
# --------------------------------------------------------------------- #

def _load_options_metadata_module():
    try:
        import aura_v05373_options_instrument_metadata as mod
        return mod
    except ImportError:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "aura_v05373_options_instrument_metadata",
            ROOT / "aura_v05373_options_instrument_metadata.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod


# --------------------------------------------------------------------- #
# PURE: normalization. See module docstring §2.
# --------------------------------------------------------------------- #

def _signed_qty(qty: Any, side: Any) -> Decimal | None:
    """`side` is authoritative for sign (PositionSide LONG/SHORT);
    `abs()` on qty makes this robust regardless of whether the broker's
    own qty field happens to already carry a sign. Returns None (never
    raises) on anything unparseable -- the caller decides how to
    surface that."""
    try:
        magnitude = abs(Decimal(str(qty)))
    except (InvalidOperation, TypeError, ValueError):
        return None
    side_value = _enum_value(side)
    if side_value == "short":
        return -magnitude
    if side_value == "long":
        return magnitude
    return None


def normalize_expected_position(entry: Any, meta: Any) -> dict[str, Any]:
    """Caller-supplied: {"occ_symbol": str, "qty": signed number}. Fails
    closed (RuntimeError) on malformed CALLER input -- this is AURA's
    OWN book; a malformed entry here is a caller bug, not a broker-side
    mystery, so it is treated like every other module's input
    validation (O1/O3/O4), not like §2's broker-side anomaly handling."""
    if not isinstance(entry, dict):
        fail(f"INVALID_EXPECTED_POSITION_ENTRY:{entry!r}")
    occ_symbol = entry.get("occ_symbol")
    if not isinstance(occ_symbol, str):
        fail(f"MISSING_OCC_SYMBOL_IN_EXPECTED_POSITION:{entry!r}")
    parsed = meta.parse_occ_symbol(occ_symbol)  # raises via meta.fail on malformed shape
    try:
        qty = Decimal(str(entry.get("qty")))
    except (InvalidOperation, TypeError, ValueError):
        fail(f"INVALID_EXPECTED_QTY:{entry!r}")
    if not qty.is_finite():
        fail(f"INVALID_EXPECTED_QTY:{entry!r}")
    return {
        "occ_symbol": occ_symbol,
        "underlying_symbol": parsed["underlying_symbol"],
        "qty": qty,
    }


def normalize_broker_position(position: Any, meta: Any) -> dict[str, Any] | None:
    """`position` is a real alpaca-py Position (or a faked test double)
    exposing `.symbol`/`.asset_class`/`.qty`/`.side`. Returns None for a
    non-option position (not this module's concern -- not an anomaly).
    Returns a PROBLEM record (never raises) for an options position
    whose symbol or qty/side this module can't parse -- see module
    docstring §2: one bad broker record must never abort reconciliation
    of everything else."""
    asset_class = _enum_value(getattr(position, "asset_class", None))
    if asset_class != "us_option":
        return None

    symbol = getattr(position, "symbol", None)
    try:
        parsed = meta.parse_occ_symbol(symbol)
    except RuntimeError as exc:
        return {
            "occ_symbol": symbol,
            "underlying_symbol": None,
            "qty": None,
            "problem": f"UNPARSEABLE_BROKER_POSITION_SYMBOL:{exc}",
        }

    signed = _signed_qty(getattr(position, "qty", None), getattr(position, "side", None))
    if signed is None:
        return {
            "occ_symbol": symbol,
            "underlying_symbol": parsed["underlying_symbol"],
            "qty": None,
            "problem": (
                "UNPARSEABLE_BROKER_POSITION_QTY_OR_SIDE:"
                f"qty={getattr(position, 'qty', None)!r},side={getattr(position, 'side', None)!r}"
            ),
        }

    return {
        "occ_symbol": symbol,
        "underlying_symbol": parsed["underlying_symbol"],
        "qty": signed,
        "problem": None,
    }


def normalize_activity(activity: Any) -> dict[str, Any] | None:
    """`activity` is a real alpaca-py NonTradeActivity (or a faked test
    double) exposing `.activity_type`/`.symbol`/`.date`/`.status`/`.qty`/
    `.net_amount`. Returns None for anything that isn't an assignment/
    exercise/expiration code, or that's CANCELED (not real evidence)."""
    activity_type = _enum_value(getattr(activity, "activity_type", None))
    if activity_type not in ASSIGNMENT_EXERCISE_ACTIVITY_TYPES:
        return None
    status = _enum_value(getattr(activity, "status", None))
    if status in _EXCLUDED_ACTIVITY_STATUSES:
        return None
    return {
        "activity_type": activity_type,
        "occ_symbol": getattr(activity, "symbol", None),
        "date": getattr(activity, "date", None),
        "status": status,
        "qty": getattr(activity, "qty", None),
        "net_amount": getattr(activity, "net_amount", None),
    }


def _within_lookback(activity_date: Any, as_of: datetime, lookback_days: int) -> bool:
    if activity_date is None:
        return False
    cutoff_date = (as_of - timedelta(days=lookback_days)).date()
    if isinstance(activity_date, datetime):
        activity_date = activity_date.date()
    try:
        return activity_date >= cutoff_date
    except TypeError:
        return False


def index_activities_by_occ_symbol(
    broker_activities: list[Any],
    *,
    lookback_days: int,
    as_of: datetime | None = None,
) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(lookback_days, int) or isinstance(lookback_days, bool) or lookback_days <= 0:
        fail(f"INVALID_LOOKBACK_DAYS:{lookback_days!r}")
    if not isinstance(broker_activities, list):
        fail("BROKER_ACTIVITIES_NOT_A_LIST")
    as_of = as_of or datetime.now(timezone.utc)

    index: dict[str, list[dict[str, Any]]] = {}
    for raw in broker_activities:
        normalized = normalize_activity(raw)
        if normalized is None:
            continue
        if not _within_lookback(normalized["date"], as_of, lookback_days):
            continue
        index.setdefault(normalized["occ_symbol"], []).append(normalized)
    return index


# --------------------------------------------------------------------- #
# PURE: the reconciliation core. See module docstring §1-2.
# --------------------------------------------------------------------- #

def reconcile_options_positions(
    expected_positions: list[dict[str, Any]],
    broker_positions: list[Any],
    broker_activities: list[Any],
    *,
    lookback_days: int,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    meta = _load_options_metadata_module()

    if not isinstance(expected_positions, list):
        fail("EXPECTED_POSITIONS_NOT_A_LIST")
    if not isinstance(broker_positions, list):
        fail("BROKER_POSITIONS_NOT_A_LIST")

    expected_by_symbol: dict[str, dict[str, Any]] = {}
    for entry in expected_positions:
        normalized = normalize_expected_position(entry, meta)
        occ_symbol = normalized["occ_symbol"]
        if occ_symbol in expected_by_symbol:
            fail(f"DUPLICATE_EXPECTED_POSITION:{occ_symbol}")
        expected_by_symbol[occ_symbol] = normalized

    broker_by_symbol: dict[str, dict[str, Any]] = {}
    broker_problems: list[dict[str, Any]] = []
    for raw in broker_positions:
        normalized = normalize_broker_position(raw, meta)
        if normalized is None:
            continue  # not an options position -- not this module's concern
        if normalized.get("problem"):
            broker_problems.append(normalized)
            continue
        broker_by_symbol[normalized["occ_symbol"]] = normalized

    activities_index = index_activities_by_occ_symbol(
        broker_activities, lookback_days=lookback_days, as_of=as_of,
    )

    all_occ_symbols = set(expected_by_symbol) | set(broker_by_symbol)

    legs: dict[str, dict[str, Any]] = {}
    for occ_symbol in sorted(all_occ_symbols):
        expected_entry = expected_by_symbol.get(occ_symbol)
        broker_entry = broker_by_symbol.get(occ_symbol)
        expected_qty = expected_entry["qty"] if expected_entry else Decimal(0)
        broker_qty = broker_entry["qty"] if broker_entry else Decimal(0)
        underlying_symbol = (expected_entry or broker_entry)["underlying_symbol"]

        if expected_qty == broker_qty:
            legs[occ_symbol] = {
                "underlying_symbol": underlying_symbol,
                "classification": "CONSISTENT",
                "expected_qty": str(expected_qty),
                "broker_qty": str(broker_qty),
            }
            continue

        evidence = activities_index.get(occ_symbol, [])
        classification = "ASSIGNMENT_OR_EXERCISE_DETECTED" if evidence else "UNEXPLAINED_POSITION_MISMATCH"
        legs[occ_symbol] = {
            "underlying_symbol": underlying_symbol,
            "classification": classification,
            "expected_qty": str(expected_qty),
            "broker_qty": str(broker_qty),
            "evidence": [
                {**e, "date": e["date"].isoformat() if hasattr(e["date"], "isoformat") else e["date"]}
                for e in evidence
            ],
        }

    underlyings: dict[str, dict[str, Any]] = {}
    for occ_symbol, leg in legs.items():
        underlying = leg["underlying_symbol"] or "UNKNOWN"
        bucket = underlyings.setdefault(underlying, {"status": "CONSISTENT", "legs": {}, "freeze_reasons": []})
        bucket["legs"][occ_symbol] = leg
        if leg["classification"] != "CONSISTENT":
            bucket["status"] = "FROZEN"
            bucket["freeze_reasons"].append({"occ_symbol": occ_symbol, "classification": leg["classification"]})

    for problem in broker_problems:
        underlying = problem["underlying_symbol"] or "UNKNOWN"
        bucket = underlyings.setdefault(underlying, {"status": "CONSISTENT", "legs": {}, "freeze_reasons": []})
        bucket["status"] = "FROZEN"
        bucket["freeze_reasons"].append({
            "occ_symbol": problem["occ_symbol"],
            "classification": "UNPARSEABLE_BROKER_POSITION",
            "detail": problem["problem"],
        })

    frozen_underlyings = sorted(u for u, bucket in underlyings.items() if bucket["status"] == "FROZEN")

    return {
        "adapter_version": VERSION,
        "engine": ENGINE,
        "schema_version": SCHEMA_VERSION,
        "observed_at": now(),
        "lookback_days": lookback_days,
        "underlyings": underlyings,
        "frozen_underlyings": frozen_underlyings,
        "requires_human_attention": len(frozen_underlyings) > 0,
    }


# --------------------------------------------------------------------- #
# NETWORK-CAPABLE, READ-ONLY. See module docstring §0/§3 -- neither
# function here ever submits, cancels, or mutates anything.
# --------------------------------------------------------------------- #

def fetch_broker_positions(client: Any) -> list[Any]:
    """Wraps TradingClient.get_all_positions() -- a READ-ONLY call.
    Returns the raw SDK list, unfiltered; all filtering/classification
    logic lives in the pure reconcile_options_positions() layer."""
    return client.get_all_positions()


def fetch_broker_activities(client: Any, lookback_days: int) -> list[Any]:
    """NETWORK-CAPABLE, READ-ONLY. See module docstring §0:
    TradingClient has no typed get_account_activities() -- this uses its
    raw get(path, data) escape hatch against /account/activities,
    narrowed by a best-effort `after=` param (UNVERIFIED against a live
    account this session) and parsed into NonTradeActivity by hand. The
    real lookback guarantee is still enforced client-side in
    index_activities_by_occ_symbol(), regardless of what the server
    actually honors for this param."""
    if not isinstance(lookback_days, int) or isinstance(lookback_days, bool) or lookback_days <= 0:
        fail(f"INVALID_LOOKBACK_DAYS:{lookback_days!r}")
    from alpaca.trading.models import NonTradeActivity

    after = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).date().isoformat()
    raw = client.get("/account/activities", {"after": after})
    if not isinstance(raw, list):
        fail(f"UNEXPECTED_ACTIVITIES_RESPONSE_SHAPE:{type(raw)}")

    activities: list[Any] = []
    for item in raw:
        if not isinstance(item, dict) or item.get("activity_type") not in ASSIGNMENT_EXERCISE_ACTIVITY_TYPES:
            continue
        activities.append(NonTradeActivity(**item))
    return activities


def reconcile(
    expected_positions: list[dict[str, Any]],
    client: Any,
    *,
    lookback_days: int,
) -> dict[str, Any]:
    """NETWORK-CAPABLE orchestrator: fetches both evidence sources fresh
    every call (no caching, matching optionwright's own choice not to
    cache this specific read) and runs the pure reconciliation core."""
    broker_positions = fetch_broker_positions(client)
    broker_activities = fetch_broker_activities(client, lookback_days)
    return reconcile_options_positions(
        expected_positions, broker_positions, broker_activities,
        lookback_days=lookback_days,
    )


# --------------------------------------------------------------------- #
# CLI -- read-only reconciliation sweep. No paper-orders env-var gate is
# needed (unlike .335/.376's submit() path) -- this module never
# submits, cancels, or mutates anything; see module docstring §3.
# --------------------------------------------------------------------- #

def main() -> int:
    from alpaca.trading.client import TradingClient

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-input", required=True, type=Path,
                         help="JSON list of AURA's expected options positions: [{occ_symbol, qty}, ...]")
    parser.add_argument("--lookback-days", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    result: dict[str, Any] = {
        "adapter_version": VERSION,
        "observed_at": now(),
        "status": "BLOCKED",
    }
    try:
        expected_positions = json.loads(args.expected_input.read_text(encoding="utf-8"))

        key = os.getenv("ALPACA_PAPER_API_KEY")
        secret = os.getenv("ALPACA_PAPER_SECRET_KEY")
        if not key or not secret:
            fail("MISSING_ALPACA_PAPER_CREDENTIALS")
        client = TradingClient(key, secret, paper=True)

        reconciliation = reconcile(expected_positions, client, lookback_days=args.lookback_days)
        result.update({"status": "RECONCILED", "reconciliation": reconciliation})

        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")
        print(json.dumps(result, indent=2, default=str))
        return 0
    except Exception as exc:
        result.update({"status": "FAIL_CLOSED", "error": f"{type(exc).__name__}: {exc}"})
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")
        print(f"FAIL-CLOSED: {result['error']}")
        return 1


# Disclosure list, per the requirement to confirm exactly which functions
# in this module are network-capable. Checked directly by a dedicated
# test that this list is complete (no other module-level function
# references `client.` or `TradingClient(`). All three are READ-ONLY --
# see module docstring §3.
NETWORK_CAPABLE_FUNCTIONS = frozenset({"fetch_broker_positions", "fetch_broker_activities", "reconcile", "main"})


if __name__ == "__main__":
    raise SystemExit(main())
