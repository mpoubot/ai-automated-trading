#!/usr/bin/env python3
"""AURA v0.5.3.29 — MEXC Intent Ledger.

Durable, `client_order_id`-keyed record of everything AURA believes
happened to one execution intent, from creation through final
reconciliation. Answers, for any intent, at any time including after a
restart: what did AURA believe happened, and what stage is it at.

LOCKED ARCHITECTURE
-------------------
This module does NOT:
    - call MEXC or any exchange (that's v0.5.3.27/.28);
    - decide a reconciliation verdict (that's v0.5.3.30 -- this module
      only *records* the verdict .30 hands it);
    - perform replay protection itself. That remains v0.5.3.27's own
      on-disk claim file (os.O_CREAT|os.O_EXCL), unchanged, already
      proven by test. This module's own CLAIMED event is an
      AUDIT-TRAIL MIRROR of that fact, never a substitute source of
      truth for "has this client_order_id already been consumed" --
      an orchestrator checking that question must go through v0.5.3.27
      itself, never this module alone;
    - mutate Position State (v0.5.3.15, explicitly out of scope);
    - fabricate any fact. Every event this module records traces to
      something an upstream component (.27, .28, .30, or an
      orchestrator) explicitly supplied.

Full design rationale: AURA_v05329_mexc_intent_ledger_spec_2026-09-09.md
and AURA_v05329_30_transition_evidence_test_matrix_2026-09-09.md
(both in this session's project docs).

STATE VOCABULARY
-----------------
    NEW -> CLAIMED -> SUBMISSION_ATTEMPTED -> AWAITING_RECONCILIATION
        -> RECONCILING
        -> RECONCILED_FILLED                          (terminal)
        -> RECONCILED_PARTIALLY_FILLED                (conditional --
               terminal iff the observed order is itself terminal on
               MEXC's side; see order_terminal_on_exchange below)
        -> RECONCILED_CANCELED                         (terminal)
        -> TERMINAL_SUBMISSION_REJECTED                (terminal, via
               .27's own pre-submission SUBMISSION_REJECTED only --
               .30 is never invoked on this path)
        -> ESCALATED_HUMAN_REVIEW                      (terminal-but-
               blocking; a human, not .30, resolves this)

DUPLICATE_CLAIM_REJECTED is legal from ANY state (the anomaly path,
spec Sec.5): it never creates a second record for a client_order_id
that already has one -- it appends to the existing record and routes
it to ESCALATED_HUMAN_REVIEW.

EVIDENCE-AUTHORITY RULE (locked, spec Sec.3): SUBMISSION_ACKNOWLEDGED
can only ever advance a record to AWAITING_RECONCILIATION -- never
straight to a RECONCILED_*/TERMINAL_* state. Only a RECONCILIATION_ATTEMPT
event (i.e. only .30 + .28 observation evidence) may produce one. This
is enforced structurally below, not just documented: no code path in
this module can reach a RECONCILED_*/TERMINAL_SUBMISSION_REJECTED state
except through the specific event that spec Sec.3 names for it.

CRASH-RETRY IDEMPOTENCY RULE (locked 2026-09-09): an orchestrator that
crashes and retries a mutator call must never be able to create a
duplicate lifecycle event through this module. Concretely:
    - a structural mutator (record_claimed, record_submission_attempted)
      called again once its target state has already been reached is a
      silent no-op;
    - an evidence-bearing mutator (record_submission_outcome,
      record_reconciliation_attempt) called again with FIELDS IDENTICAL
      to the event already on record at that logical point is a silent
      no-op (retry-safe);
    - called again with DIFFERENT fields at a logical point the record
      has already moved past is never silently accepted and never
      silently dropped -- it is appended as an ESCALATION event and the
      record moves to ESCALATED_HUMAN_REVIEW. Conflicting evidence about
      the same intent is exactly the kind of thing a human must see.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VERSION = "AURA v0.5.3.29"
ENGINE = "MEXC_INTENT_LEDGER"
SCHEMA_VERSION = "1.0"

DEFAULT_INTENT_LEDGER_DIR = Path(
    os.environ.get(
        "AURA_MEXC_INTENT_LEDGER_DIR",
        r"regime_output\mexc_intent_ledger\intents",
    )
)
# Configurable, not hardcoded -- spec Sec.10.4/Sec.8. The literal-backslash
# default matches every other module's own DEFAULT_* path convention
# (.17/.18/.27/.28) rather than inventing a new style here.

CLIENT_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,48}$")
# Matches .27's own existing client_order_id charset/length validation --
# reused, not reinvented, so a client_order_id already safe as a claim
# filename there is guaranteed safe as this module's filename too.

DIRECTIONS = {"OPEN_LONG", "OPEN_SHORT", "CLOSE_LONG", "CLOSE_SHORT"}
SIDES = {"buy", "sell"}

# The three outcomes that actually advance a normal (non-anomalous)
# submission-outcome transition. DUPLICATE_CLAIM_REJECTED is handled
# separately below as the anomaly path, legal from any state.
SUBMISSION_OUTCOMES = {"SUBMISSION_ACKNOWLEDGED", "SUBMISSION_REJECTED", "EXECUTION_UNCERTAIN"}
DUPLICATE_CLAIM_OUTCOME = "DUPLICATE_CLAIM_REJECTED"

# .28's six-value enum, minus REJECTED: a REJECTED observation is
# evidentially a contradiction with an earlier acknowledgment (spec
# Sec.3/.30 spec Sec.3's REJECTED row) and is handled via
# escalate_to_human() directly by the caller, never via this mutator --
# record_reconciliation_attempt() therefore never accepts REJECTED.
RECONCILIATION_ORDER_STATUSES = {"FILLED", "PARTIALLY_FILLED", "CANCELED", "PENDING", "UNRESOLVED"}

TERMINAL_STATES = {"RECONCILED_FILLED", "RECONCILED_CANCELED", "TERMINAL_SUBMISSION_REJECTED"}
# RECONCILED_PARTIALLY_FILLED's terminality is conditional (see
# _is_terminal below); ESCALATED_HUMAN_REVIEW is terminal-but-blocking,
# handled as its own case throughout.

NON_TERMINAL_LISTABLE_STATES = {
    "NEW",
    "CLAIMED",
    "SUBMISSION_ATTEMPTED",
    "AWAITING_RECONCILIATION",
    "RECONCILING",
    "ESCALATED_HUMAN_REVIEW",
}
# RECONCILED_PARTIALLY_FILLED is added to this set dynamically by
# list_unresolved_intents() only for records where it is non-terminal.


def fail(message: str) -> None:
    raise RuntimeError(message)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def record_path(client_order_id: str, base_dir: Path | None = None) -> Path:
    if not isinstance(client_order_id, str) or not CLIENT_ORDER_ID_RE.match(client_order_id):
        fail(f"INVALID_CLIENT_ORDER_ID:{client_order_id!r}")
    return (base_dir or DEFAULT_INTENT_LEDGER_DIR) / f"{client_order_id}.json"


# --------------------------------------------------------------------- #
# hashing / canonicalization
# --------------------------------------------------------------------- #

def canonical_record_hash(record: dict[str, Any]) -> str:
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
# current_state / partial_fill_terminal / updated_at are deliberately
# EXCLUDED from the hash: they are fully derived from `events` (see
# derive_state below) and recomputed -- never trusted -- on every read,
# so hashing them too would be redundant, not additional protection.
# verify_intent() catches a mismatch between stored and derived state
# independently of the hash check.


# --------------------------------------------------------------------- #
# state machine -- the single source of transition legality
# --------------------------------------------------------------------- #

class IllegalTransitionError(RuntimeError):
    """Raised only for genuine ordering bugs (e.g. submission-attempted
    before claimed) that the crash-retry idempotency logic does not
    apply to. Mutators that DO have idempotency/anomaly handling never
    raise this -- they either no-op, append normally, or escalate."""


def derive_state(events: list[dict[str, Any]]) -> tuple[str, bool | None]:
    """Pure function: replay `events` from scratch and return
    (current_state, partial_fill_terminal). Never trusts a stored
    current_state -- this IS the definition of current_state. Raises
    IllegalTransitionError if the event sequence itself is not a legal
    path through the state machine (used by verify_intent() as the
    sequence-legality check, independent of the hash check)."""
    state = "UNCREATED"
    partial_terminal: bool | None = None

    for ev in events:
        etype = ev.get("event")

        # DUPLICATE_CLAIM_REJECTED and ESCALATION are legal from (almost)
        # any post-creation state -- handled first, uniformly.
        if etype == DUPLICATE_CLAIM_OUTCOME:
            if state == "UNCREATED":
                raise IllegalTransitionError("DUPLICATE_CLAIM_REJECTED_BEFORE_CREATION")
            state = "ESCALATED_HUMAN_REVIEW"
            partial_terminal = None
            continue
        if etype == "ESCALATION":
            # Legal from any state EXCEPT "nothing exists/nothing to
            # escalate yet" (UNCREATED/NEW) -- including already-terminal
            # states and even an already-escalated record (idempotent),
            # same reachability as DUPLICATE_CLAIM_REJECTED just above.
            # A stricter "don't let a plain human-initiated call
            # re-escalate something already closed" rule is enforced one
            # layer up, in escalate_to_human() itself -- NOT here --
            # because the internal conflicting-evidence fallback in
            # record_submission_outcome()/record_reconciliation_attempt()
            # must still be able to flag contradictory evidence arriving
            # against an ALREADY-terminal record (spec: "conflicting
            # evidence about the same intent is exactly the kind of thing
            # a human must see", locked 2026-09-09) without that being
            # blocked by the same restriction that stops a redundant
            # human-initiated re-escalation.
            if state in ("UNCREATED", "NEW"):
                raise IllegalTransitionError(f"ESCALATION_ILLEGAL_FROM:{state}")
            state = "ESCALATED_HUMAN_REVIEW"
            partial_terminal = None
            continue

        if state == "UNCREATED":
            if etype != "INTENT_CREATED":
                raise IllegalTransitionError(f"FIRST_EVENT_NOT_INTENT_CREATED:{etype}")
            state = "NEW"
            continue

        if state == "ESCALATED_HUMAN_REVIEW":
            raise IllegalTransitionError(f"EVENT_AFTER_ESCALATION:{etype}")

        if state == "NEW":
            if etype != "CLAIMED":
                raise IllegalTransitionError(f"ILLEGAL_FROM_NEW:{etype}")
            state = "CLAIMED"
            continue

        if state == "CLAIMED":
            if etype != "SUBMISSION_ATTEMPTED":
                raise IllegalTransitionError(f"ILLEGAL_FROM_CLAIMED:{etype}")
            state = "SUBMISSION_ATTEMPTED"
            continue

        if state == "SUBMISSION_ATTEMPTED":
            if etype == "SUBMISSION_ACKNOWLEDGED" or etype == "EXECUTION_UNCERTAIN":
                state = "AWAITING_RECONCILIATION"
            elif etype == "SUBMISSION_REJECTED":
                state = "TERMINAL_SUBMISSION_REJECTED"
            else:
                raise IllegalTransitionError(f"ILLEGAL_FROM_SUBMISSION_ATTEMPTED:{etype}")
            continue

        if state in ("AWAITING_RECONCILIATION", "RECONCILING") or (
            state == "RECONCILED_PARTIALLY_FILLED" and partial_terminal is False
        ):
            if etype != "RECONCILIATION_ATTEMPT":
                raise IllegalTransitionError(f"ILLEGAL_FROM:{state}:{etype}")
            order_status = ev.get("fields", {}).get("order_status")
            if order_status not in RECONCILIATION_ORDER_STATUSES:
                raise IllegalTransitionError(f"INVALID_ORDER_STATUS_IN_EVENT:{order_status!r}")
            if order_status in ("UNRESOLVED", "PENDING"):
                state = "RECONCILING"
                partial_terminal = None
            elif order_status == "FILLED":
                state = "RECONCILED_FILLED"
                partial_terminal = None
            elif order_status == "CANCELED":
                state = "RECONCILED_CANCELED"
                partial_terminal = None
            elif order_status == "PARTIALLY_FILLED":
                terminal_flag = ev.get("fields", {}).get("order_terminal_on_exchange")
                if not isinstance(terminal_flag, bool):
                    raise IllegalTransitionError("MISSING_ORDER_TERMINAL_ON_EXCHANGE_FOR_PARTIAL_FILL")
                state = "RECONCILED_PARTIALLY_FILLED"
                partial_terminal = terminal_flag
            continue

        # Any other state (terminal states, or a terminal partial-fill)
        # accepts nothing further except DUPLICATE_CLAIM_REJECTED/
        # ESCALATION, already handled above.
        raise IllegalTransitionError(f"EVENT_AFTER_TERMINAL:{state}:{etype}")

    return state, partial_terminal


def _is_open_to_reconciliation(state: str, partial_terminal: bool | None) -> bool:
    return state in ("AWAITING_RECONCILIATION", "RECONCILING") or (
        state == "RECONCILED_PARTIALLY_FILLED" and partial_terminal is False
    )


def _is_terminal_or_escalated(state: str, partial_terminal: bool | None) -> bool:
    return (
        state in TERMINAL_STATES
        or state == "ESCALATED_HUMAN_REVIEW"
        or (state == "RECONCILED_PARTIALLY_FILLED" and partial_terminal is True)
    )


# --------------------------------------------------------------------- #
# record I/O -- atomic create, read-verify-append-atomic-rename
# --------------------------------------------------------------------- #

def _lock_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".lock")


class _FileLock:
    """Advisory O_CREAT|O_EXCL lock guarding the read-modify-write
    window for a single intent's record. Strengthens the spec's
    read-verify-append-atomic-rename discipline against two independent
    processes racing an append (the create race itself is already safe
    via create_intent()'s own O_CREAT|O_EXCL -- this covers the append
    path, which the spec describes as sequential but doesn't itself
    enforce at the OS level)."""

    def __init__(self, path: Path, timeout_seconds: float = 5.0):
        self._lock_path = _lock_path(path)
        self._timeout = timeout_seconds
        self._fd: int | None = None

    def __enter__(self) -> "_FileLock":
        deadline = time.monotonic() + self._timeout
        while True:
            try:
                self._fd = os.open(str(self._lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                if time.monotonic() >= deadline:
                    fail(f"LOCK_TIMEOUT:{self._lock_path}")
                time.sleep(0.02)

    def __exit__(self, *exc_info: Any) -> None:
        if self._fd is not None:
            os.close(self._fd)
        try:
            os.unlink(self._lock_path)
        except FileNotFoundError:
            pass


def _write_record_atomic(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, ensure_ascii=False, allow_nan=False)
        f.write("\n")
    os.replace(tmp_path, path)


def _load_record_raw(path: Path) -> dict[str, Any]:
    if not path.exists():
        fail(f"INTENT_NOT_FOUND:{path.stem}")
    with path.open("r", encoding="utf-8") as f:
        record = json.load(f)
    if not isinstance(record, dict):
        fail(f"INTENT_RECORD_NOT_OBJECT:{path.stem}")
    return record


def verify_intent(record: dict[str, Any]) -> tuple[bool, list[str]]:
    """Recomputes record_hash and checks the event sequence is a legal
    path through the state machine -- hash-consistency alone is not
    sufficient; a structurally-tampered-but-internally-consistent
    events list must also be caught (spec Sec.7)."""
    errors: list[str] = []

    if record.get("engine") != ENGINE:
        errors.append("WRONG_ENGINE")
    if record.get("schema_version") != SCHEMA_VERSION:
        errors.append("WRONG_SCHEMA_VERSION")

    supplied_hash = record.get("record_hash")
    if not isinstance(supplied_hash, str) or not supplied_hash:
        errors.append("MISSING_RECORD_HASH")
    else:
        try:
            calculated = canonical_record_hash(record)
        except (TypeError, ValueError):
            errors.append("RECORD_NOT_HASHABLE")
        else:
            if calculated != supplied_hash:
                errors.append("RECORD_HASH_MISMATCH")

    events = record.get("events")
    if not isinstance(events, list) or not events:
        errors.append("MISSING_OR_EMPTY_EVENTS")
        return False, errors

    try:
        derived_state, derived_partial_terminal = derive_state(events)
    except IllegalTransitionError as exc:
        errors.append(f"ILLEGAL_EVENT_SEQUENCE:{exc}")
        return False, errors

    if record.get("current_state") != derived_state:
        errors.append(
            f"CURRENT_STATE_DRIFT:stored={record.get('current_state')!r}:derived={derived_state!r}"
        )
    if record.get("partial_fill_terminal") != derived_partial_terminal:
        errors.append(
            "PARTIAL_FILL_TERMINAL_DRIFT:"
            f"stored={record.get('partial_fill_terminal')!r}:derived={derived_partial_terminal!r}"
        )

    return len(errors) == 0, errors


def _load_and_verify(path: Path) -> dict[str, Any]:
    record = _load_record_raw(path)
    ok, errors = verify_intent(record)
    if not ok:
        fail(f"INTENT_VERIFICATION_FAILED:{path.stem}:{','.join(errors)}")
    return record


def _finalize(record: dict[str, Any]) -> dict[str, Any]:
    state, partial_terminal = derive_state(record["events"])
    record["current_state"] = state
    record["partial_fill_terminal"] = partial_terminal
    record["updated_at"] = now()
    record["record_hash"] = canonical_record_hash(record)
    return record


# --------------------------------------------------------------------- #
# mutators
# --------------------------------------------------------------------- #

def create_intent(
    client_order_id: str,
    symbol: str,
    side: str,
    quantity: float,
    direction: str,
    spec_fingerprint: str,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    """Atomic create; refuses (does not overwrite) if client_order_id
    already has a record -- always, even on the creator's own retried
    call (see module docstring: create_intent() is the one mutator the
    spec defines as a hard refusal, not an idempotent no-op; a caller
    distinguishing its own crash-retry from a genuine collision does so
    by catching this and calling get_intent() itself)."""
    if side not in SIDES:
        fail(f"INVALID_SIDE:{side!r}")
    if direction not in DIRECTIONS:
        fail(f"INVALID_DIRECTION:{direction!r}")
    if not isinstance(symbol, str) or not symbol.strip():
        fail("MISSING_SYMBOL")
    if not isinstance(quantity, (int, float)) or isinstance(quantity, bool) or quantity <= 0:
        fail(f"INVALID_QUANTITY:{quantity!r}")
    if not isinstance(spec_fingerprint, str) or not spec_fingerprint.strip():
        fail("MISSING_SPEC_FINGERPRINT")

    path = record_path(client_order_id, base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    created_at = now()
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "engine": ENGINE,
        "agent_version": VERSION,
        "client_order_id": client_order_id,
        "symbol": symbol,
        "direction": direction,
        "side": side,
        "quantity": quantity,
        "spec_fingerprint": spec_fingerprint,
        "created_at": created_at,
        "updated_at": created_at,
        "current_state": "NEW",
        "partial_fill_terminal": None,
        "events": [
            {
                "event": "INTENT_CREATED",
                "at": created_at,
                "fields": {
                    "symbol": symbol,
                    "side": side,
                    "quantity": quantity,
                    "direction": direction,
                    "spec_fingerprint": spec_fingerprint,
                },
            }
        ],
        "record_hash": None,
    }
    record["record_hash"] = canonical_record_hash(record)

    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        fail(f"INTENT_ALREADY_EXISTS:{client_order_id}")
        raise  # unreachable
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, ensure_ascii=False, allow_nan=False)
        f.write("\n")
    return record


def record_claimed(client_order_id: str, claimed_at: str | None = None, base_dir: Path | None = None) -> dict[str, Any]:
    path = record_path(client_order_id, base_dir)
    with _FileLock(path):
        record = _load_and_verify(path)
        state, _ = derive_state(record["events"])
        if state != "NEW":
            # Already claimed (or moved further) -- structural mutator,
            # no evidentiary content to conflict on. Safe no-op.
            return record
        record["events"].append({
            "event": "CLAIMED",
            "at": now(),
            "fields": {"claimed_at": claimed_at or now()},
        })
        record = _finalize(record)
        _write_record_atomic(path, record)
        return record


def record_submission_attempted(client_order_id: str, base_dir: Path | None = None) -> dict[str, Any]:
    path = record_path(client_order_id, base_dir)
    with _FileLock(path):
        record = _load_and_verify(path)
        state, _ = derive_state(record["events"])
        if state == "CLAIMED":
            record["events"].append({"event": "SUBMISSION_ATTEMPTED", "at": now(), "fields": {}})
            record = _finalize(record)
            _write_record_atomic(path, record)
            return record
        if state == "NEW":
            # Genuine ordering bug -- attempted before claimed -- not a
            # retry, so this is not treated as idempotent.
            raise IllegalTransitionError(f"SUBMISSION_ATTEMPTED_REQUIRES_CLAIMED:actual={state}")
        # Already attempted (or moved further) -- structural mutator,
        # no evidentiary content to conflict on. Safe no-op.
        return record


def record_submission_outcome(
    client_order_id: str,
    outcome: str,
    mexc_order_id: str | None = None,
    raw_response_ref: str | None = None,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    if outcome != DUPLICATE_CLAIM_OUTCOME and outcome not in SUBMISSION_OUTCOMES:
        fail(f"INVALID_SUBMISSION_OUTCOME:{outcome!r}")

    path = record_path(client_order_id, base_dir)
    fields = {"mexc_order_id": mexc_order_id, "raw_response_ref": raw_response_ref}

    with _FileLock(path):
        record = _load_and_verify(path)
        state, partial_terminal = derive_state(record["events"])

        if outcome == DUPLICATE_CLAIM_OUTCOME:
            last = record["events"][-1] if record["events"] else None
            if last is not None and last.get("event") == DUPLICATE_CLAIM_OUTCOME and last.get("fields") == fields:
                return record  # idempotent retry of the anomaly itself
            record["events"].append({"event": DUPLICATE_CLAIM_OUTCOME, "at": now(), "fields": fields})
            record = _finalize(record)
            _write_record_atomic(path, record)
            return record

        if state == "SUBMISSION_ATTEMPTED":
            record["events"].append({"event": outcome, "at": now(), "fields": fields})
            record = _finalize(record)
            _write_record_atomic(path, record)
            return record

        if state in ("NEW", "CLAIMED"):
            # Genuinely premature -- a submission outcome can't
            # legitimately exist before SUBMISSION_ATTEMPTED was ever
            # recorded, so this cannot be a crash-retry of an earlier
            # successful call. Caller ordering bug -- raise.
            raise IllegalTransitionError(f"SUBMISSION_OUTCOME_TOO_EARLY:{state}")

        # State has already moved past SUBMISSION_ATTEMPTED. Distinguish
        # a harmless crash-retry of the same call from genuinely
        # conflicting evidence.
        last = record["events"][-1] if record["events"] else None
        if last is not None and last.get("event") == outcome and last.get("fields") == fields:
            return record  # idempotent retry

        record["events"].append({
            "event": "ESCALATION",
            "at": now(),
            "fields": {
                "reason": "CONFLICTING_SUBMISSION_OUTCOME_REPLAY",
                "attempted_outcome": outcome,
                "attempted_fields": fields,
                "record_state_at_attempt": state,
            },
        })
        record = _finalize(record)
        _write_record_atomic(path, record)
        return record


def record_reconciliation_attempt(
    client_order_id: str,
    observed_snapshot_hash: str,
    order_status: str,
    order_terminal_on_exchange: bool | None = None,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    if order_status not in RECONCILIATION_ORDER_STATUSES:
        fail(f"INVALID_ORDER_STATUS:{order_status!r}")
    if order_status == "PARTIALLY_FILLED" and not isinstance(order_terminal_on_exchange, bool):
        fail("ORDER_TERMINAL_ON_EXCHANGE_REQUIRED_FOR_PARTIALLY_FILLED")

    path = record_path(client_order_id, base_dir)
    fields = {
        "observed_snapshot_hash": observed_snapshot_hash,
        "order_status": order_status,
        "order_terminal_on_exchange": order_terminal_on_exchange if order_status == "PARTIALLY_FILLED" else None,
    }

    with _FileLock(path):
        record = _load_and_verify(path)
        state, partial_terminal = derive_state(record["events"])

        if _is_open_to_reconciliation(state, partial_terminal):
            record["events"].append({"event": "RECONCILIATION_ATTEMPT", "at": now(), "fields": fields})
            record = _finalize(record)
            _write_record_atomic(path, record)
            return record

        if state in ("NEW", "CLAIMED", "SUBMISSION_ATTEMPTED"):
            # Genuinely premature -- no submission outcome has even been
            # recorded yet, so there is no earlier evidence to compare
            # against and nothing meaningful to escalate either
            # (mirrors escalate_to_human()'s own "nothing to escalate
            # yet" refusal). This is a caller ordering bug, not a
            # crash-retry -- raise, don't silently accept or escalate.
            raise IllegalTransitionError(f"RECONCILIATION_ATTEMPT_TOO_EARLY:{state}")

        # Record already at a settled/terminal point for reconciliation.
        # Same distinction as record_submission_outcome(): identical
        # retry is a no-op, conflicting evidence is an anomaly.
        last_recon = next(
            (e for e in reversed(record["events"]) if e.get("event") == "RECONCILIATION_ATTEMPT"), None
        )
        if last_recon is not None and last_recon.get("fields") == fields:
            return record  # idempotent retry

        record["events"].append({
            "event": "ESCALATION",
            "at": now(),
            "fields": {
                "reason": "CONFLICTING_RECONCILIATION_EVIDENCE_REPLAY",
                "attempted_fields": fields,
                "record_state_at_attempt": state,
            },
        })
        record = _finalize(record)
        _write_record_atomic(path, record)
        return record


def escalate_to_human(client_order_id: str, reason: str, base_dir: Path | None = None) -> dict[str, Any]:
    path = record_path(client_order_id, base_dir)
    with _FileLock(path):
        record = _load_and_verify(path)
        state, partial_terminal = derive_state(record["events"])

        if state == "NEW":
            fail("NOTHING_TO_ESCALATE_YET")
        if _is_terminal_or_escalated(state, partial_terminal):
            fail(f"ALREADY_TERMINAL_OR_ESCALATED:{state}")

        record["events"].append({"event": "ESCALATION", "at": now(), "fields": {"reason": reason}})
        record = _finalize(record)
        _write_record_atomic(path, record)
        return record


# --------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------- #

def get_intent(client_order_id: str, base_dir: Path | None = None) -> dict[str, Any] | None:
    path = record_path(client_order_id, base_dir)
    if not path.exists():
        return None
    return _load_and_verify(path)


def list_unresolved_intents(base_dir: Path | None = None) -> list[dict[str, Any]]:
    """Every record whose current_state is non-terminal -- the
    restart-recovery entry point (spec Sec.6). A single corrupt/
    unverifiable record is surfaced as its own BLOCKED-shaped entry
    rather than raising and hiding every OTHER intent's status behind
    it (mirrors .18's isolate-one-bad-symbol-don't-block-the-rest
    philosophy, applied here to isolate-one-bad-file)."""
    directory = base_dir or DEFAULT_INTENT_LEDGER_DIR
    if not directory.exists():
        return []

    unresolved: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            record = _load_record_raw(path)
            ok, errors = verify_intent(record)
        except (RuntimeError, ValueError, OSError) as exc:
            # Deliberately broad (covers json.JSONDecodeError, a
            # ValueError subclass, alongside our own fail()-raised
            # RuntimeError) -- this loop's entire purpose is to isolate
            # one unreadable/corrupt file so it never blocks visibility
            # into every OTHER intent's status (spec Sec.6, matrix
            # T29-LIST-*), the file-listing analogue of .18's
            # isolate-one-bad-symbol philosophy.
            unresolved.append({
                "client_order_id": path.stem,
                "current_state": "BLOCKED_UNREADABLE",
                "verification_errors": [str(exc)],
            })
            continue
        if not ok:
            unresolved.append({
                "client_order_id": record.get("client_order_id", path.stem),
                "current_state": "BLOCKED_VERIFICATION_FAILED",
                "verification_errors": errors,
            })
            continue

        state = record["current_state"]
        partial_terminal = record["partial_fill_terminal"]
        if state in NON_TERMINAL_LISTABLE_STATES or (
            state == "RECONCILED_PARTIALLY_FILLED" and partial_terminal is False
        ):
            unresolved.append(record)

    return unresolved


# --------------------------------------------------------------------- #
# CLI -- read-only diagnostics/recovery only, never a second execution
# pathway (spec Sec.7's "library-first, CLI optional" decision).
# --------------------------------------------------------------------- #

def print_intent(record: dict[str, Any]) -> None:
    print("=" * 96)
    print(f"{VERSION} — INTENT LEDGER")
    print("=" * 96)
    print(f"CLIENT ORDER ID   : {record.get('client_order_id')}")
    print(f"SYMBOL            : {record.get('symbol')}")
    print(f"DIRECTION         : {record.get('direction')}")
    print(f"CURRENT STATE     : {record.get('current_state')}")
    print(f"PARTIAL TERMINAL  : {record.get('partial_fill_terminal')}")
    print(f"CREATED AT        : {record.get('created_at')}")
    print(f"UPDATED AT        : {record.get('updated_at')}")
    print("EVENTS:")
    for ev in record.get("events", []):
        print(f"  {ev.get('at')}  {ev.get('event'):<28} {ev.get('fields')}")
    print("=" * 96)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-unresolved", action="store_true")
    parser.add_argument("--show", type=str, default=None, metavar="CLIENT_ORDER_ID")
    parser.add_argument("--base-dir", type=Path, default=None)
    args = parser.parse_args()

    if args.show:
        record = get_intent(args.show, args.base_dir)
        if record is None:
            print(f"NOT FOUND: {args.show}")
            return 1
        print_intent(record)
        return 0

    if args.list_unresolved:
        records = list_unresolved_intents(args.base_dir)
        print(f"{len(records)} unresolved intent(s):")
        for record in records:
            print(f"  {record.get('client_order_id')}: {record.get('current_state')}")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
