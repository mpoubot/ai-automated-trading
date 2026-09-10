#!/usr/bin/env python3
"""AURA v0.5.3.30 — MEXC Intent Reconciliation.

A deterministic reconciliation engine, not a service: v0.5.3.29 intent
+ v0.5.3.28 observation in, a verdict out. No storage of its own, no
MEXC/ccxt calls, no exchange writes. See:
    AURA_v05330_mexc_intent_reconciliation_spec_2026-09-09.md
    AURA_v05329_30_transition_evidence_test_matrix_2026-09-09.md

LOCKED ARCHITECTURE
-------------------
This module does NOT:
    - call MEXC or any exchange (that's .27/.28 -- this module takes an
      already-produced .28 snapshot as a plain input);
    - persist anything. v0.5.3.29's event log is the SOLE authoritative
      record of every reconciliation this module has ever performed for
      a given intent -- writing a second copy here would recreate the
      exact "three sources of truth that can drift" failure mode this
      whole design exists to avoid (locked 2026-09-09);
    - write to v0.5.3.29's storage directly. apply_verdict() below is a
      thin wrapper that calls exactly one of .29's own mutators -- it
      never touches .29's on-disk files itself.

Per the established cross-module convention already used by .18 (which
re-derives .17's canonical hash itself rather than importing .17's
Python module) and .28 (which reads .27's OUTPUT FILE, never imports
.27's module), this module treats a .29 intent record and a .28
snapshot purely as DATA -- plain dicts, verified by re-deriving their
canonical hashes independently, not by importing .27/.28's modules.
The one deliberate exception is apply_verdict(), which -- per its own
spec'd role as "the thing that calls .29's own mutators" -- does import
v0.5.3.29 directly, since that is precisely its job.

ARCHITECTURAL PRINCIPLE: DETERMINISTIC, EVIDENCE-DRIVEN, NEVER INFERRED
-------------------------------------------------------------------------
This module never asks "what probably happened?" It only asks "does the
supplied evidence satisfy the exact rule for a reconciled state?" Every
verdict below traces to a specific, named rule over specific input
fields. Anything not explicitly covered becomes exactly one of two
things: stay RECONCILING (evidence incomplete, not contradictory), or
ESCALATE (evidence contradictory, or no defined rule covers it). Never
an inferred success.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

VERSION = "AURA v0.5.3.30"
ENGINE = "MEXC_INTENT_RECONCILIATION"

EXPECTED_INTENT_SCHEMA_VERSION = "1.0"
EXPECTED_INTENT_ENGINE = "MEXC_INTENT_LEDGER"
EXPECTED_SNAPSHOT_ENGINE = "OBSERVED_EXECUTION_MEXC_V1"

RAW_STATE_OPEN = "2"
RAW_STATE_CLOSED = "3"
RAW_STATE_CANCELED = "4"


def stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not d.is_finite():
        return None
    return d


def _parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# --------------------------------------------------------------------- #
# input verification -- re-derives each canonical hash independently,
# matching .29's/.28's own canonicalization exactly (deliberately
# duplicated, not imported -- see module docstring)
# --------------------------------------------------------------------- #

def _canonical_intent_record_hash(record: dict[str, Any]) -> str:
    canonical = {
        "schema_version": record.get("schema_version"),
        "engine": record.get("engine"),
        "agent_version": record.get("agent_version"),
        "client_order_id": record.get("client_order_id"),
        "symbol": record.get("symbol"),
        "direction": record.get("direction"),
        "side": record.get("side"),
        "quantity": record.get("quantity"),
        "spec_fingerprint": record.get("spec_fingerprint"),
        "events": record.get("events"),
    }
    return sha256_text(stable_json(canonical))


def _verify_intent_record(record: dict[str, Any]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if not isinstance(record, dict):
        return False, ["INTENT_RECORD_NOT_OBJECT"]
    if record.get("engine") != EXPECTED_INTENT_ENGINE:
        errors.append("WRONG_INTENT_ENGINE")
    if record.get("schema_version") != EXPECTED_INTENT_SCHEMA_VERSION:
        errors.append("WRONG_INTENT_SCHEMA_VERSION")
    supplied_hash = record.get("record_hash")
    if not isinstance(supplied_hash, str) or not supplied_hash:
        errors.append("MISSING_INTENT_RECORD_HASH")
    else:
        try:
            calculated = _canonical_intent_record_hash(record)
        except (TypeError, ValueError):
            errors.append("INTENT_RECORD_NOT_HASHABLE")
        else:
            if calculated != supplied_hash:
                errors.append("INTENT_RECORD_HASH_MISMATCH")
    if not isinstance(record.get("client_order_id"), str) or not record.get("client_order_id"):
        errors.append("MISSING_CLIENT_ORDER_ID")
    return len(errors) == 0, errors


def _canonical_snapshot_hash(snapshot: dict[str, Any]) -> str:
    # Mirrors .28's own canonical_snapshot_hash() field set exactly.
    canonical = {
        "agent_version": snapshot.get("agent_version"),
        "engine": snapshot.get("engine"),
        "snapshot_status": snapshot.get("snapshot_status"),
        "client_order_id": snapshot.get("client_order_id"),
        "symbol": snapshot.get("symbol"),
        "mexc_order_id": snapshot.get("mexc_order_id"),
        "order_status": snapshot.get("order_status"),
        "raw_status": snapshot.get("raw_status"),
        "match_count": snapshot.get("match_count"),
        "position_exists": snapshot.get("position_exists"),
        "position_id": snapshot.get("position_id"),
        "fill_price": snapshot.get("fill_price"),
        "fill_timestamp": snapshot.get("fill_timestamp"),
        "observed_fills": snapshot.get("observed_fills"),
    }
    return sha256_text(stable_json(canonical))


def _verify_observed_snapshot(snapshot: dict[str, Any]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if not isinstance(snapshot, dict):
        return False, ["SNAPSHOT_NOT_OBJECT"]
    if snapshot.get("engine") != EXPECTED_SNAPSHOT_ENGINE:
        errors.append("WRONG_SNAPSHOT_ENGINE")
    if snapshot.get("snapshot_status") != "OBSERVED":
        errors.append("SNAPSHOT_NOT_MARKED_OBSERVED")
    supplied_hash = snapshot.get("snapshot_hash")
    if not isinstance(supplied_hash, str) or not supplied_hash:
        errors.append("MISSING_SNAPSHOT_HASH")
    else:
        try:
            calculated = _canonical_snapshot_hash(snapshot)
        except (TypeError, ValueError):
            errors.append("SNAPSHOT_NOT_HASHABLE")
        else:
            if calculated != supplied_hash:
                errors.append("SNAPSHOT_HASH_MISMATCH")
    return len(errors) == 0, errors


def _is_open_to_reconciliation(state: str, partial_terminal: bool | None) -> bool:
    return state in ("AWAITING_RECONCILIATION", "RECONCILING") or (
        state == "RECONCILED_PARTIALLY_FILLED" and partial_terminal is False
    )


# --------------------------------------------------------------------- #
# Sec.3 Step 1 -- dealVol-first classification of a terminal order,
# never trusting .28's own order_status label for this (locked
# 2026-09-09, the specific fix for the CANCELED-mislabeling bug class)
# --------------------------------------------------------------------- #

def classify_terminal_order(raw_order_info: dict[str, Any] | None) -> str | None:
    """Returns "FILLED_CLEAN" | "PARTIAL_ON_TERMINAL_ORDER" |
    "CANCELED_CLEAN" when the raw MEXC order is confidently terminal
    (state 3 or 4) AND dealVol/vol both parse cleanly. Returns None
    otherwise (order still open, raw state unmapped, or the numbers
    themselves don't parse) -- the caller then falls through to .28's
    own order_status-driven handling. Never guesses when the numbers
    are unusable."""
    if not isinstance(raw_order_info, dict):
        return None
    raw_state = raw_order_info.get("state")
    raw_state_str = str(raw_state) if raw_state is not None else None
    if raw_state_str not in (RAW_STATE_CLOSED, RAW_STATE_CANCELED):
        return None

    deal_vol = _to_decimal(raw_order_info.get("dealVol"))
    vol = _to_decimal(raw_order_info.get("vol"))
    if deal_vol is None or vol is None or vol <= 0:
        return None

    if deal_vol <= 0:
        return "CANCELED_CLEAN"
    if deal_vol >= vol:
        return "FILLED_CLEAN"
    return "PARTIAL_ON_TERMINAL_ORDER"


def classify_unresolved(observed_snapshot: dict[str, Any]) -> tuple[int, str]:
    """Sub-classifies .28's UNRESOLVED order_status into escalation
    case 1 (escalate now) or case 2 (stay RECONCILING) per the evidence
    matrix. Factored out separately -- this is the piece of logic most
    likely to need revision as real MEXC behavior is observed."""
    match_count = observed_snapshot.get("match_count")
    reason = observed_snapshot.get("reason") or ""

    if match_count == 0:
        return 2, "NO_MATCHING_ORDER_FOUND_IN_WINDOW"
    if isinstance(match_count, int) and match_count > 1:
        return 1, "MULTIPLE_MATCHING_ORDERS_FOUND"
    if reason == "UNRECOGNIZED_ORDER_STATUS_VALUE":
        return 1, "UNRECOGNIZED_ORDER_STATUS_VALUE"
    if reason == "FILL_STATUS_WITHOUT_SUBSTANTIATING_PRICE_OR_TIMESTAMP":
        return 2, "FILL_STATUS_WITHOUT_SUBSTANTIATING_PRICE_OR_TIMESTAMP"
    # Everything else with match_count == 1 and no more specific reason
    # recognized above: an unmapped/unconfirmed raw MEXC state (e.g.
    # state '1'/'5', or a closed order whose own numbers didn't parse
    # cleanly enough for classify_terminal_order() to resolve). Could
    # still resolve with more time -- conservative default, case 2.
    return 2, f"UNMAPPED_OR_UNCONFIRMED_ORDER_STATE:{reason}"


# --------------------------------------------------------------------- #
# staleness -- mechanism locked, threshold value deliberately not
# fixed here (spec Sec.9)
# --------------------------------------------------------------------- #

def check_staleness(intent_record: dict[str, Any], staleness_policy: dict[str, Any] | None, now: datetime | None = None) -> bool:
    """True when the record has been open to reconciliation long
    enough (clock starts at AWAITING_RECONCILIATION entry -- the
    SUBMISSION_ACKNOWLEDGED/EXECUTION_UNCERTAIN event's own timestamp,
    NOT the time of this function's first invocation) that a case-2
    "stay RECONCILING" verdict should be upgraded to case-3 (escalate)
    instead of standing as-is."""
    if not staleness_policy:
        return False
    threshold_minutes = staleness_policy.get("reconciliation_staleness_minutes")
    if threshold_minutes is None:
        return False

    entry_event = next(
        (e for e in intent_record.get("events", []) if e.get("event") in ("SUBMISSION_ACKNOWLEDGED", "EXECUTION_UNCERTAIN")),
        None,
    )
    if entry_event is None or not isinstance(entry_event.get("at"), str):
        return False

    try:
        entered_at = _parse_iso(entry_event["at"])
    except ValueError:
        return False

    now_dt = now if now is not None else datetime.now(timezone.utc)
    elapsed_minutes = (now_dt - entered_at).total_seconds() / 60.0
    return elapsed_minutes > threshold_minutes


# --------------------------------------------------------------------- #
# the evidence matrix itself
# --------------------------------------------------------------------- #

def reconcile_intent(
    intent_record: dict[str, Any],
    observed_snapshot: dict[str, Any],
    staleness_policy: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Pure function -- no I/O, no MEXC calls, no writes to .29 itself.
    Verifies both inputs before reasoning about them; a verification
    failure produces a BLOCKED verdict, not a best-effort guess."""
    intent_ok, intent_errors = _verify_intent_record(intent_record)
    if not intent_ok:
        return {"action": "BLOCKED", "reason": "INTENT_VERIFICATION_FAILED:" + ",".join(intent_errors)}

    snap_ok, snap_errors = _verify_observed_snapshot(observed_snapshot)
    if not snap_ok:
        return {"action": "BLOCKED", "reason": "SNAPSHOT_VERIFICATION_FAILED:" + ",".join(snap_errors)}

    if intent_record.get("client_order_id") != observed_snapshot.get("client_order_id"):
        return {"action": "BLOCKED", "reason": "CLIENT_ORDER_ID_MISMATCH_BETWEEN_INTENT_AND_SNAPSHOT"}

    state = intent_record.get("current_state")
    partial_terminal = intent_record.get("partial_fill_terminal")
    if not _is_open_to_reconciliation(state, partial_terminal):
        return {"action": "BLOCKED", "reason": f"PRECONDITION_VIOLATION:current_state={state!r}"}

    direction = intent_record.get("direction")
    position_exists = observed_snapshot.get("position_exists")
    evidence_base = {"direction": direction, "position_exists": position_exists}
    order_status = observed_snapshot.get("order_status")
    snapshot_hash = observed_snapshot.get("snapshot_hash")

    # Step 1: dealVol-first classification of a resolved, terminal order
    # -- never trusts .28's own order_status label for this (Sec.3).
    raw_order = None
    raw_evidence = observed_snapshot.get("raw_evidence")
    if isinstance(raw_evidence, dict):
        candidate = raw_evidence.get("order")
        if isinstance(candidate, dict):
            raw_order = candidate

    classified = classify_terminal_order(raw_order)

    if classified == "FILLED_CLEAN":
        # Order/fill evidence alone proves execution -- a surviving
        # position is not required (locked decision, spec Sec.3/4b).
        return {
            "action": "RECORD_ATTEMPT",
            "order_status": "FILLED",
            "target_state": "RECONCILED_FILLED",
            "order_terminal_on_exchange": None,
            "reason": "FILLED_CLEAN_DEALVOL_GE_VOL",
            "observed_snapshot_hash": snapshot_hash,
            "evidence": {**evidence_base, "dealVol_check": "dealVol>=vol"},
        }
    if classified == "CANCELED_CLEAN":
        return {
            "action": "RECORD_ATTEMPT",
            "order_status": "CANCELED",
            "target_state": "RECONCILED_CANCELED",
            "order_terminal_on_exchange": None,
            "reason": "CANCELED_CLEAN_DEALVOL_ZERO",
            "observed_snapshot_hash": snapshot_hash,
            "evidence": {**evidence_base, "dealVol_check": "dealVol==0"},
        }
    if classified == "PARTIAL_ON_TERMINAL_ORDER":
        # No more fills are coming -- the order is already
        # canceled/closed on MEXC's side. Terminal per the 2026-09-09
        # refinement to .29's RECONCILED_PARTIALLY_FILLED rule.
        return {
            "action": "RECORD_ATTEMPT",
            "order_status": "PARTIALLY_FILLED",
            "target_state": "RECONCILED_PARTIALLY_FILLED",
            "order_terminal_on_exchange": True,
            "reason": "PARTIAL_ON_TERMINAL_ORDER_DEALVOL",
            "observed_snapshot_hash": snapshot_hash,
            "evidence": {**evidence_base, "dealVol_check": "0<dealVol<vol,terminal"},
        }

    # classified is None: order not confidently terminal by raw
    # evidence (still open, unmapped state, or unparseable numbers) --
    # fall through to .28's own order_status field.
    if order_status == "PARTIALLY_FILLED":
        # A still-open order's partial fill -- more execution can still
        # occur. Non-terminal.
        return {
            "action": "RECORD_ATTEMPT",
            "order_status": "PARTIALLY_FILLED",
            "target_state": "RECONCILED_PARTIALLY_FILLED",
            "order_terminal_on_exchange": False,
            "reason": "PARTIAL_ON_OPEN_ORDER",
            "observed_snapshot_hash": snapshot_hash,
            "evidence": {**evidence_base, "dealVol_check": "0<dealVol<vol,open"},
        }

    if order_status == "PENDING":
        raw_verdict = {
            "action": "RECORD_ATTEMPT",
            "order_status": "PENDING",
            "target_state": "RECONCILING",
            "order_terminal_on_exchange": None,
            "reason": "PENDING_NOT_ALARMING",
            "observed_snapshot_hash": snapshot_hash,
            "evidence": evidence_base,
        }
    elif order_status == "REJECTED":
        # Contradicts an earlier SUBMISSION_ACKNOWLEDGED/EXECUTION_UNCERTAIN
        # -- never silently converted to TERMINAL_SUBMISSION_REJECTED
        # (locked 2026-09-09). That state is reached only directly from
        # .27's own pre-submission SUBMISSION_REJECTED, never via .30.
        return {
            "action": "ESCALATE",
            "reason": "POST_ACKNOWLEDGMENT_REJECTED_CONTRADICTS_EARLIER_EVIDENCE",
            "evidence": evidence_base,
        }
    elif order_status == "UNRESOLVED":
        case, sub_reason = classify_unresolved(observed_snapshot)
        if case == 1:
            return {"action": "ESCALATE", "reason": sub_reason, "evidence": evidence_base}
        raw_verdict = {
            "action": "RECORD_ATTEMPT",
            "order_status": "UNRESOLVED",
            "target_state": "RECONCILING",
            "order_terminal_on_exchange": None,
            "reason": sub_reason,
            "observed_snapshot_hash": snapshot_hash,
            "evidence": evidence_base,
        }
    else:
        # order_status is FILLED/CANCELED but classify_terminal_order()
        # could not confirm it from raw evidence (missing/unparseable),
        # or is some other value entirely unrecognized here. Per the
        # "never an inferred success" principle: never trust .28's
        # label alone when .30's own independent check couldn't
        # substantiate it -- escalate rather than silently accept.
        return {
            "action": "ESCALATE",
            "reason": f"UNHANDLED_OR_UNSUBSTANTIATED_ORDER_STATUS:{order_status!r}",
            "evidence": evidence_base,
        }

    # raw_verdict is a case-2 "stay RECONCILING" outcome -- apply the
    # staleness check before returning it as final.
    if check_staleness(intent_record, staleness_policy, now=now):
        return {
            "action": "ESCALATE",
            "reason": f"STALENESS_THRESHOLD_EXCEEDED:{raw_verdict['reason']}",
            "evidence": evidence_base,
        }
    return raw_verdict


# --------------------------------------------------------------------- #
# apply_verdict -- the one place this module calls .29's own mutators
# --------------------------------------------------------------------- #

def _load_ledger_module():
    try:
        import aura_v05329_mexc_intent_ledger as ledger29  # type: ignore
        return ledger29
    except ImportError:
        import importlib.util
        from pathlib import Path
        module_path = Path(__file__).resolve().parent / "aura_v05329_mexc_intent_ledger.py"
        spec = importlib.util.spec_from_file_location("aura_v05329_mexc_intent_ledger", module_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod


def apply_verdict(client_order_id: str, verdict: dict[str, Any], base_dir: Any = None) -> dict[str, Any]:
    """Thin wrapper: calls exactly one of .29's own mutators based on
    verdict["action"]. Never writes .29's storage directly -- this
    function's entire body is delegation."""
    ledger29 = _load_ledger_module()

    if verdict["action"] == "RECORD_ATTEMPT":
        return ledger29.record_reconciliation_attempt(
            client_order_id,
            observed_snapshot_hash=verdict.get("observed_snapshot_hash"),
            order_status=verdict["order_status"],
            order_terminal_on_exchange=verdict.get("order_terminal_on_exchange"),
            base_dir=base_dir,
        )
    if verdict["action"] == "ESCALATE":
        return ledger29.escalate_to_human(client_order_id, verdict["reason"], base_dir=base_dir)
    if verdict["action"] == "BLOCKED":
        # Orchestrator's own concern -- retry, alert, or halt. .29/.30
        # make no decision here, since verification failed, not evidence.
        raise RuntimeError(f"CANNOT_APPLY_BLOCKED_VERDICT:{verdict.get('reason')}")
    raise RuntimeError(f"UNKNOWN_VERDICT_ACTION:{verdict.get('action')!r}")
