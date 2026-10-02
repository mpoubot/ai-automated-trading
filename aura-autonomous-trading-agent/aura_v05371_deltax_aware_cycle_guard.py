#!/usr/bin/env python3
"""
AURA v0.5.3.71 -- DELTAX cross-awareness cycle guard.

Additive, opt-in layer that sits BETWEEN `.365`'s own loop and `.363`'s
`run_one_live_cycle()` call, unmodified. It does not touch `.343`'s or
`.344`'s internals, and it does not touch `.363`/`.355`/`.353`'s trading
logic -- it only:

  1. Before a cycle: reads how much ALPACA notional DELTAX has told the
     shared intent log (`.370`) it is about to submit (recent minutes
     only), adds it to AURA's OWN currently-known ALPACA notional (via
     `.343`'s already-tested `fetch_alpaca_portfolio`, called with the
     SAME `alpaca_client` `.365` already holds), and -- only if the
     caller supplied a configured ratio limit -- skips this one cycle (no
     new orders attempted) when the COMBINED figure would already be
     over that ratio. Exactly `.344`'s own "limit is None -> never
     blocking" discipline: omit the limit, this never skips anything.

  2. After a cycle: re-reads AURA's own ALPACA notional and, if it rose
     (meaning the cycle actually opened something new), writes ONE intent
     record to the same shared log so DELTAX's very next cycle already
     knows about it, even before the position is old enough for either
     side's own broker-reconciliation to see it.

Neither step can touch `.344`'s own enforcement decision -- this runs
entirely OUTSIDE that path, as an extra, independent, skip-the-whole-
cycle check layered on top, the same way `.365`'s own kill switch and
daily order cap already skip a cycle without touching `.344` at all.

WIRING (the only change `.365` itself needs -- see the setup notes
shipped alongside this file for the exact lines)
------------------------------------------------------------------------
    guard = _load_module("aura_v05371_deltax_aware_cycle_guard",
                          "aura_v05371_deltax_aware_cycle_guard.py")
    observability = _load_module("aura_v05343_portfolio_exposure_observability",
                                  "aura_v05343_portfolio_exposure_observability.py")
    shared_intent = _load_module("aura_v05370_shared_execution_intent",
                                  "aura_v05370_shared_execution_intent.py")

    # ... inside the while loop, AFTER the existing daily-cap check and
    # BEFORE `result = run_one_live_cycle(...)`:
    proceed, reason, evidence = guard.pre_cycle_check(
        observability_module=observability, shared_intent_module=shared_intent,
        alpaca_client=alpaca_client, shared_intent_path=shared_intent_path,
        max_age_seconds=shared_intent_max_age_seconds,
        combined_alpaca_notional_ratio_limit=combined_alpaca_notional_ratio_limit,
        now=now,
    )
    if not proceed:
        log_fn(f"{_now_iso(now)} CROSS_AWARENESS_SKIP: {reason} | {evidence}")
        sleep_fn(interval_seconds)
        continue
    before_snapshot = guard.snapshot_alpaca_notional(observability, alpaca_client)

    result = run_one_live_cycle(...)   # <-- UNCHANGED, existing call

    guard.post_cycle_record(
        observability_module=observability, shared_intent_module=shared_intent,
        alpaca_client=alpaca_client, shared_intent_path=shared_intent_path,
        before_snapshot=before_snapshot, now=now,
    )

`shared_intent_path`, `shared_intent_max_age_seconds`, and
`combined_alpaca_notional_ratio_limit` are new, OPTIONAL parameters --
when `shared_intent_path` is None (the default), both functions are a
complete no-op (`pre_cycle_check` always returns proceed=True,
`post_cycle_record` always returns None), so a deployment that doesn't
set them up behaves exactly as `.365` already does today.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any


def snapshot_alpaca_notional(observability_module: Any, alpaca_client: Any) -> dict:
    """One call to `.343`'s own, already-tested `fetch_alpaca_portfolio`.
    Returns {"status", "total_notional_usd", "equity"} -- never raises;
    `.343`'s own function already fails closed (returns a FAILED
    VenueFetchStatus) on any broker error, which this just carries
    through as status != "SUCCESS" rather than fabricating a number."""
    try:
        records, status = observability_module.fetch_alpaca_portfolio(alpaca_client)
    except Exception as exc:  # noqa: BLE001 -- broker call; never raise out of a guard
        return {"status": f"FETCH_FAILED_{type(exc).__name__}", "total_notional_usd": None, "equity": None}
    if status.status != "SUCCESS":
        return {"status": status.status, "total_notional_usd": None, "equity": status.equity}
    total = sum(abs(r.notional_usd) for r in records if r.notional_usd is not None)
    return {"status": "SUCCESS", "total_notional_usd": total, "equity": status.equity}


def pre_cycle_check(
    *, observability_module: Any, shared_intent_module: Any, alpaca_client: Any,
    shared_intent_path: Path | None, max_age_seconds: float = 600.0,
    combined_alpaca_notional_ratio_limit: float | None = None,
    now: datetime | None = None,
) -> tuple[bool, str, dict]:
    """Returns (proceed, reason, evidence). proceed=False means: skip
    this cycle entirely, call run_one_live_cycle() zero times this pass.

    Never raises -- any internal failure defaults to proceed=True (the
    same fail-OPEN-on-infrastructure-error reasoning `.365` itself
    already uses for its own market-clock check: an unreadable cross-
    awareness signal must never be the reason real trading logic never
    runs at all; `.344`'s own enforcement, completely unaffected by any
    of this, still runs normally inside the cycle and is what actually
    blocks an individual unsafe order)."""
    if combined_alpaca_notional_ratio_limit is None or shared_intent_path is None:
        return True, "NOT_CONFIGURED", {}
    try:
        snap = snapshot_alpaca_notional(observability_module, alpaca_client)
        if snap["status"] != "SUCCESS" or not snap["equity"]:
            return True, f"SNAPSHOT_UNAVAILABLE_{snap['status']}", snap
        foreign = shared_intent_module.sum_recent_foreign_risk(
            shared_intent_path, own_system="AURA", venue="ALPACA",
            max_age_seconds=max_age_seconds, now=now)
        combined_ratio = (snap["total_notional_usd"] + foreign) / snap["equity"]
        evidence = {
            "own_notional_usd": round(snap["total_notional_usd"], 2),
            "equity": round(snap["equity"], 2),
            "foreign_pending_risk_usd": round(foreign, 2),
            "combined_ratio": round(combined_ratio, 4),
            "limit": combined_alpaca_notional_ratio_limit,
        }
        if combined_ratio > combined_alpaca_notional_ratio_limit:
            return False, "CROSS_AWARENESS_COMBINED_RATIO_WOULD_BREACH", evidence
        return True, "WITHIN_LIMIT", evidence
    except Exception as exc:  # noqa: BLE001
        return True, f"CHECK_FAILED_{type(exc).__name__}", {"error": str(exc)[:160]}


def post_cycle_record(
    *, observability_module: Any, shared_intent_module: Any, alpaca_client: Any,
    shared_intent_path: Path | None, before_snapshot: dict | None,
    now: datetime | None = None,
) -> dict | None:
    """Writes ONE intent record for the ALPACA-notional delta this cycle
    produced, if any (positive delta only -- a net reduction, e.g. a
    closed position, has nothing to warn DELTAX about, same reasoning
    DELTAX's own side uses for closes -- see `execute.py`'s own
    cross-awareness comment). Returns the record written, or None if
    nothing was written. Never raises."""
    if shared_intent_path is None or before_snapshot is None:
        return None
    if before_snapshot.get("status") != "SUCCESS":
        return None
    try:
        after = snapshot_alpaca_notional(observability_module, alpaca_client)
        if after["status"] != "SUCCESS" or after["total_notional_usd"] is None:
            return None
        delta = after["total_notional_usd"] - (before_snapshot.get("total_notional_usd") or 0.0)
        if delta <= 0:
            return None
        return shared_intent_module.append_intent(
            shared_intent_path, system="AURA", venue="ALPACA", symbol="MULTIPLE",
            estimated_risk_usd=delta, now=now)
    except Exception:  # noqa: BLE001
        return None
