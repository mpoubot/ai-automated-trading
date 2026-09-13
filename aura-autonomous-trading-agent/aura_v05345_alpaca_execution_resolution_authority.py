#!/usr/bin/env python3
"""
AURA v0.5.3.45 — Alpaca Execution Resolution Authority.

Resolves an Alpaca order's ambiguous `.40`-recorded `OUTCOME` (both a clean
`SUBMITTED` acknowledgment and a genuine `EXECUTION_UNCERTAIN`) to a
definitive terminal state, or explicitly escalates when the evidence
cannot support one -- the equivalent, for Alpaca, of what `.29`/`.30`
already do for MEXC. This module NEVER submits, cancels, or modifies an
order, and NEVER auto-retries a submission, per Martin's explicit scoping
answers (2026-09-13, AskUserQuestion):

  1. **Scope**: resolve BOTH a clean `OUTCOME:SUBMITTED` and an
     `OUTCOME:EXECUTION_UNCERTAIN` through one pipeline — mirroring
     MEXC's own `.29`/`.30`, where `SUBMISSION_ACKNOWLEDGED` and
     `EXECUTION_UNCERTAIN` funnel into the identical
     `AWAITING_RECONCILIATION` treatment (`aura_v05329...py:263`).
  2. **Storage**: extend `.40`'s existing per-`client_order_id` audit
     trail with one new terminal event, `RESOLVED` — no second Alpaca
     ledger was built. `.40`'s `derive_state()`/`EVENT_TYPES` were
     extended additively (see that module's own comments at the change
     site) and its full 23-test suite re-verified unchanged.
  3. **No auto-retry**: this module only ever produces a `RESOLVE`
     (definitive terminal state) or `ESCALATE` verdict, exactly mirroring
     `.30.reconcile_intent()`'s own `RECORD_ATTEMPT`/`ESCALATE`/`BLOCKED`
     shape. A "proven-failed" submission (broker confirms nothing
     executed) is surfaced as an honest `proven_failed` evidence flag,
     never acted on — the roadmap's literal "retry only on proven-failed
     submission" step is explicitly NOT implemented, per Martin's answer.

Reuse scan (Martin's A/B/C/D framework)
------------------------------------------------------------------------
  - `.30.reconcile_intent()`/`classify_terminal_order()`/
    `classify_unresolved()`/`check_staleness()`/`apply_verdict()`: (B)
    the SHAPE is adapted directly — a pure, evidence-only decision
    function returning `{"action": ..., "reason": ..., "evidence": ...}`,
    with a separate thin `apply_verdict()` that is the sole writer, and a
    staleness check keyed off the ORIGINAL uncertain event's own
    timestamp, not the time of the current resolution attempt. Nothing
    from `.30` is imported or modified — Alpaca's evidence shape (an
    `alpaca-py` `Order`, not a MEXC raw order dict) is different enough
    that copying the function bodies would be copying the wrong thing;
    the pure-function/never-infer/verdict-then-apply DISCIPLINE is what
    was reused, per Martin's own instruction not to import functionality
    merely because it exists elsewhere.
  - `.40`'s audit trail (`record_outcome`, `verify_record`,
    `derive_state`, atomic-write/lock conventions): (A) directly reused
    and extended in place — see point 2 above. This module imports `.40`
    (dynamic file-load, matching this repo's own convention) rather than
    re-implementing any part of its storage.
  - `alpaca-py`'s `OrderStatus` enum and `Order` model, and
    `alpaca.common.exceptions.APIError`: verified directly against the
    installed package source (`inspect.getsource`), not assumed. `Order`
    has no ambiguous-outcome concept of its own — AURA's own resolution
    logic (this module) is what disambiguates its 17 status values.
    `APIError.status_code` is what distinguishes "order genuinely not
    found" (404) from any other query failure.
  - No sibling project (CAURA/BABIL/DELTAX×3/`mexc_bot`) has any Alpaca-
    specific or broker-order-resolution logic at all — confirmed absent
    by the `.44`/`.45` reuse scans; (D) not applicable.

Evidence matrix (mirrors `.30`'s dealVol-first-then-status structure,
adapted to Alpaca's `status`/`filled_qty`/`qty` fields)
------------------------------------------------------------------------
`classify_alpaca_order_state()` buckets a queried Alpaca order (or a
confirmed "not found" result) into exactly one of:
  FILLED                  -> RESOLVE / RESOLVED_FILLED
  REJECTED                -> RESOLVE / RESOLVED_REJECTED (proven_failed=True)
  CANCELED, filled_qty==0 -> RESOLVE / RESOLVED_CANCELED (proven_failed=True)
  CANCELED/EXPIRED, filled_qty>0
                           -> RESOLVE / RESOLVED_PARTIALLY_FILLED (proven_failed=False)
  PARTIALLY_FILLED (still open, more fills possible)
                           -> STAY_PENDING (order not yet terminal)
  NEW/PENDING_NEW/ACCEPTED/ACCEPTED_FOR_BIDDING/PENDING_CANCEL/
  PENDING_REPLACE/CALCULATED/HELD/STOPPED/SUSPENDED/PENDING_REVIEW/
  DONE_FOR_DAY             -> STAY_PENDING, or ESCALATE if the staleness
                              threshold (keyed off the original OUTCOME
                              event's timestamp) has been exceeded --
                              exactly `.30.check_staleness()`'s pattern.
  order not found at broker (404)
                           -> STAY_PENDING (conservative -- eventual-
                              consistency lag is possible; never
                              immediately concluded "never submitted"),
                              or ESCALATE if stale -- mirrors `.30`'s own
                              conservative `match_count == 0` handling.
  REPLACED, or any status value not recognized above
                           -> ESCALATE (AURA never replaces its own
                              orders; an unrecognized value is evidence
                              this module's mapping is incomplete, never
                              silently guessed past).

`STAY_PENDING` writes nothing to `.40` — the record simply stays at
`OUTCOME:*` until a later resolution pass has something new to report,
matching this repo's "only write what actually changed" convention
elsewhere (unlike `.29`/`.30`, which append a ledger event on every
reconciliation pass including non-terminal ones; disclosed as a
deliberate simplification in the completion report, not an oversight).
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
AUDIT_MODULE_PATH = ROOT / "aura_v05340_pre_submission_revalidation.py"


def _load_audit_module():
    spec = importlib.util.spec_from_file_location("aura_v05340_pre_submission_revalidation", AUDIT_MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


AUDIT = _load_audit_module()

VERSION = "AURA v0.5.3.45"

TERMINAL_ALPACA_STATUSES_ZERO_FILL_OK = frozenset({"canceled", "expired"})
STILL_OPEN_ALPACA_STATUSES = frozenset({
    "new", "pending_new", "accepted", "accepted_for_bidding", "pending_cancel",
    "pending_replace", "calculated", "held", "stopped", "suspended", "pending_review",
    "done_for_day",
})


class ResolutionQueryError(Exception):
    """Raised by fetch_alpaca_order_state() for any query failure OTHER
    than a confirmed 404 (order not found) -- a genuine "I don't know"
    that must never be treated as evidence of anything. Callers should
    treat this the same as STAY_PENDING (try again later), never as a
    resolution."""


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not result.is_finite():
        return None
    return result


# ------------------------------------------------------------------------
# Query layer -- injectable client, exactly .40/.35's convention. Never
# constructs a real client; never falls back to a cached/assumed order
# state on any ambiguous failure.
# ------------------------------------------------------------------------

def fetch_alpaca_order_state(client: Any, client_order_id: str) -> dict[str, Any]:
    """client: an alpaca-py TradingClient (or an injected test double
    exposing the same get_order_by_client_id(client_id) method).
    Returns {"found": True, "status": <str>, "filled_qty": Decimal|None,
    "qty": Decimal|None} on success, or {"found": False} on a confirmed
    404 (order genuinely not found at the broker under this
    client_order_id). Any other failure raises ResolutionQueryError --
    never silently treated as "not found" or as any other bucket."""
    try:
        order = client.get_order_by_client_id(client_order_id)
    except Exception as exc:  # noqa: BLE001 -- classified below, never silently swallowed
        status_code = getattr(exc, "status_code", None)
        if status_code == 404:
            return {"found": False}
        raise ResolutionQueryError(f"{type(exc).__name__}:{exc}") from exc

    status = getattr(order, "status", None)
    status_value = getattr(status, "value", status)  # OrderStatus enum -> its .value, or a plain string
    if not isinstance(status_value, str) or not status_value:
        raise ResolutionQueryError(f"ALPACA_ORDER_MISSING_STATUS:{client_order_id}")

    return {
        "found": True,
        "status": status_value,
        "filled_qty": _to_decimal(getattr(order, "filled_qty", None)),
        "qty": _to_decimal(getattr(order, "qty", None)),
    }


# ------------------------------------------------------------------------
# Evidence matrix -- pure, deterministic, evidence-driven, never inferred.
# ------------------------------------------------------------------------

def classify_alpaca_order_state(order_state: dict[str, Any]) -> tuple[str, str]:
    """Returns (bucket, reason). Never raises -- an order_state this
    function cannot confidently place is bucketed UNRECOGNIZED, not
    guessed into something more specific."""
    if not order_state.get("found"):
        return "NOT_FOUND", "ORDER_NOT_FOUND_AT_BROKER"

    status = order_state.get("status")
    filled_qty = order_state.get("filled_qty")

    if status == "filled":
        return "FILLED", "STATUS_FILLED"
    if status == "rejected":
        return "REJECTED", "STATUS_REJECTED"
    if status in TERMINAL_ALPACA_STATUSES_ZERO_FILL_OK:
        if filled_qty is not None and filled_qty > 0:
            return "PARTIALLY_FILLED_TERMINAL", f"STATUS_{status.upper()}_WITH_PARTIAL_FILL"
        return "CANCELED", f"STATUS_{status.upper()}_ZERO_FILL"
    if status == "partially_filled":
        return "PARTIALLY_FILLED_OPEN", "STATUS_PARTIALLY_FILLED_STILL_OPEN"
    if status in STILL_OPEN_ALPACA_STATUSES:
        return "STILL_OPEN", f"STATUS_{status.upper()}"
    if status == "replaced":
        return "UNHANDLED", "STATUS_REPLACED_AURA_NEVER_REPLACES_ORDERS"
    return "UNRECOGNIZED", f"UNRECOGNIZED_ALPACA_ORDER_STATUS:{status!r}"


def check_staleness(audit_record: dict[str, Any], staleness_policy: dict[str, Any] | None, now: datetime | None = None) -> bool:
    """Mirrors .30.check_staleness() exactly: off by default (no
    invented threshold), keyed off the ORIGINAL OUTCOME event's own
    timestamp (when this order first became something to resolve), not
    the time of the current resolution attempt."""
    if not staleness_policy:
        return False
    threshold_minutes = staleness_policy.get("resolution_staleness_minutes")
    if threshold_minutes is None:
        return False

    outcome_event = next((e for e in audit_record.get("events", []) if e.get("event") == "OUTCOME"), None)
    if outcome_event is None or not isinstance(outcome_event.get("recorded_at"), str):
        return False
    entered_at = AUDIT._parse_iso(outcome_event["recorded_at"])
    if entered_at is None:
        return False

    now_dt = now if now is not None else datetime.now(timezone.utc)
    elapsed_minutes = (now_dt - entered_at).total_seconds() / 60.0
    return elapsed_minutes > threshold_minutes


def resolve_alpaca_execution(
    audit_record: dict[str, Any],
    order_state: dict[str, Any],
    staleness_policy: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Pure function -- no I/O, no Alpaca calls, no writes to `.40`
    itself. Verifies the audit record before reasoning about it; a
    verification failure produces a BLOCKED verdict, not a best-effort
    guess. `order_state` is whatever fetch_alpaca_order_state() (or an
    equivalent already-fetched dict, in tests) returned."""
    ok, errors = AUDIT.verify_record(audit_record)
    if not ok:
        return {"action": "BLOCKED", "reason": "AUDIT_RECORD_VERIFICATION_FAILED:" + ",".join(errors)}

    if audit_record.get("venue") != "ALPACA":
        return {"action": "BLOCKED", "reason": f"NOT_AN_ALPACA_RECORD:{audit_record.get('venue')!r}"}

    current_state = audit_record.get("current_state", "")
    if not current_state.startswith("OUTCOME:"):
        return {"action": "BLOCKED", "reason": f"PRECONDITION_VIOLATION:current_state={current_state!r}"}

    bucket, reason = classify_alpaca_order_state(order_state)
    evidence = {"order_state": order_state, "outcome_at_resolution_time": current_state}

    if bucket == "FILLED":
        return {"action": "RESOLVE", "resolution": "RESOLVED_FILLED", "proven_failed": False, "reason": reason, "evidence": evidence}
    if bucket == "REJECTED":
        return {"action": "RESOLVE", "resolution": "RESOLVED_REJECTED", "proven_failed": True, "reason": reason, "evidence": evidence}
    if bucket == "CANCELED":
        return {"action": "RESOLVE", "resolution": "RESOLVED_CANCELED", "proven_failed": True, "reason": reason, "evidence": evidence}
    if bucket == "PARTIALLY_FILLED_TERMINAL":
        return {"action": "RESOLVE", "resolution": "RESOLVED_PARTIALLY_FILLED", "proven_failed": False, "reason": reason, "evidence": evidence}
    if bucket == "PARTIALLY_FILLED_OPEN":
        return {"action": "STAY_PENDING", "reason": reason, "evidence": evidence}
    if bucket in ("STILL_OPEN", "NOT_FOUND"):
        if check_staleness(audit_record, staleness_policy, now=now):
            return {"action": "ESCALATE", "reason": f"STALENESS_THRESHOLD_EXCEEDED:{reason}", "evidence": evidence}
        return {"action": "STAY_PENDING", "reason": reason, "evidence": evidence}
    # UNHANDLED or UNRECOGNIZED
    return {"action": "ESCALATE", "reason": reason, "evidence": evidence}


# ------------------------------------------------------------------------
# apply_verdict -- the one place this module writes to .40's storage.
# ------------------------------------------------------------------------

def apply_verdict(client_order_id: str, verdict: dict[str, Any], base_dir: Path | None = None) -> dict[str, Any] | None:
    """RESOLVE -> .40.record_resolved() with the proven resolution.
    ESCALATE -> .40.record_resolved(resolution="ESCALATED_HUMAN_REVIEW") --
    a terminal stop for AUTOMATED resolution; a human resolves it from
    here by whatever process AURA's operators use outside this module
    (no automated "un-escalate" path exists, matching .30/.29's own
    ESCALATE having no automated reversal either).
    STAY_PENDING / BLOCKED -> writes nothing, returns None (nothing
    changed; a later resolution pass may reach a different verdict)."""
    if verdict["action"] == "RESOLVE":
        return AUDIT.record_resolved(
            client_order_id, resolution=verdict["resolution"],
            detail=verdict.get("reason"), proven_failed=verdict.get("proven_failed"),
            base_dir=base_dir,
        )
    if verdict["action"] == "ESCALATE":
        return AUDIT.record_resolved(
            client_order_id, resolution="ESCALATED_HUMAN_REVIEW",
            detail=verdict.get("reason"), base_dir=base_dir,
        )
    return None
