#!/usr/bin/env python3
"""
AURA v0.5.3.31 -- MEXC Execution Authorization

Closes the provenance gap identified in v0.5.3.27: v0.5.3.27's
validate_spec() checks execution_authorized / live_execution_authorized /
kill_switch directly on a caller-supplied execution_spec dict, with no
cryptographic binding to any real, evaluated safety state. Any JSON file
with the "right" booleans satisfies v0.5.3.27 regardless of whether a real
evaluator ever produced it.

This module is the missing evaluator + fingerprint-binding layer for the
MEXC chain:

    execution_spec (candidate) + safety_state (assembled)
        -> authorize()                  -> AuthorizationRecord (fingerprint-
                                            bound, TTL-bound) or a rejection
        -> claim_authorization()        -> atomic, single-use, never released
        -> revalidate_before_submission -> same policy, re-checked fresh,
                                            immediately before submission
        -> authorized_submit()          -> v0.5.3.27.validate_spec() +
                                            v0.5.3.27.submit() (unmodified)

Design constraints (Martin, 2026-09-10 GO message):

  1. CRITICAL -- raw config booleans are NEVER treated as authoritative
     SafetyState on their own, and an execution_spec's own embedded
     execution_authorized / live_execution_authorized / kill_switch fields
     are NEVER trusted by this module's guardrail evaluation (they remain
     checked, unmodified, by v0.5.3.27.validate_spec() itself as a
     defense-in-depth legacy gate -- see authorized_submit()). authorize()
     always independently RECOMPUTES its own safety_state via
     assemble_safety_state(); a caller-supplied safety_state is only ever
     used as an assertion to be *checked* against that fresh
     recomputation, never as trusted input. See
     test_forged_execution_spec_authorized_flag_alone_is_insufficient and
     test_forged_safety_state_cross_check_rejected in the unit test file.

  2. The interaction between this module's authorization_id claim and
     v0.5.3.27's own client_order_id claim is explicit and tested --
     see authorized_submit()'s docstring below and the "claim interaction"
     section of tests/test_aura_v05331_mexc_execution_authorization.py.

  3. revalidate_before_submission() reuses the exact same guardrail
     evaluation authorize() used (_evaluate_guardrails()) -- it is a
     freshness/identity/safety re-check of the SAME policy, never a
     second, divergent authorization/trading policy.

  4. Scope: authorization boundary + claim/replay protection + revalidation
     + tests only. No new MEXC execution-specification producer, no
     orchestrator, no live execution, no modification to v0.5.3.19,
     v0.5.3.27, v0.5.3.29, v0.5.3.30, or v0.5.3.23.

This module does not import, modify, or route data through v0.5.3.19.
v0.5.3.19 is permanently research/paper-only by its own design
(execution_authorized / paper_execution_authorized / live_execution_authorized
are hardcoded False in every branch of build_safety(), and its
REQUIRED_SYMBOLS are the Alpaca pair BTC/USD, ETH/USD -- not MEXC's) -- it
cannot be literally reused for MEXC data without either modifying it
(forbidden) or fabricating fake Alpaca-symbol entries, which would itself be
exactly the kind of provenance forgery this module exists to prevent.
Instead, this module builds a new, honest, MEXC-native safety-state
assembler (assemble_safety_state()) that mirrors v0.5.3.19's rigor and
pattern: safe-by-default local config booleans (recorded for auditability,
never trusted blindly on their own) plus a genuinely computed
reconciliation_health sourced from v0.5.3.29's real on-disk intent ledger
(list_unresolved_intents()), never fabricated.

No real MEXC credentials and no network call anywhere in this file.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

VERSION = "AURA v0.5.3.31"
ENGINE = "MEXC_EXECUTION_AUTHORIZATION"
SCHEMA_VERSION = "1.0"

EXPECTED_SPEC_VERSION = "1.0"
EXPECTED_EXCHANGE = "MEXC"
ALLOWED_ACCOUNT_MODES = {"PAPER", "LIVE"}

# Safe-by-default, local, operator-supplied -- mirrors v0.5.3.19's
# DEFAULT_SAFETY_CONFIG precedent exactly. These booleans are NOT
# hash-chain verified on their own; they are recorded for auditability and
# gate nothing by themselves in isolation from the rest of the assembled
# safety_state (reconciliation_health, replay_protection).
DEFAULT_AUTH_CONFIG: dict[str, Any] = {
    "kill_switch": True,
    "execution_authorized": False,
    "paper_execution_authorized": False,
    "live_execution_authorized": False,
    "authorization_ttl_seconds": 60,
    "safety_state_ttl_seconds": 60,
    "reconciliation_staleness_minutes": 5,
}

DEFAULT_AUTHORIZATION_CLAIMS_DIR = Path("regime_output/mexc_execution_authorization/claims")


# --------------------------------------------------------------------- #
# Canonical JSON / hash helpers -- same pattern used throughout the AURA
# codebase (v0.5.3.19, v0.5.3.27-.30, the safety-chain validation harness).
# Redefined locally per the repo's established per-file convention rather
# than imported, so this module has zero import-time coupling to any file
# it must not modify.
# --------------------------------------------------------------------- #

def stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fingerprint(obj: dict[str, Any]) -> str:
    return sha256_text(stable_json(obj))


def _now(now: datetime | None = None) -> datetime:
    return now or datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(text: Any) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


# --------------------------------------------------------------------- #
# Dynamic imports of v0.5.3.27 / v0.5.3.29 -- exactly mirrors v0.5.3.30's
# own _load_ledger_module() pattern (read directly from v0.5.3.30's source
# before writing this file). Neither module is modified; both are used
# strictly through their existing public functions.
# --------------------------------------------------------------------- #

def _load_ledger_module():
    """Dynamic import of v0.5.3.29. Read-only use here: only
    list_unresolved_intents() is called, to compute a genuine
    reconciliation_health."""
    try:
        import aura_v05329_mexc_intent_ledger as ledger29  # type: ignore
        return ledger29
    except ImportError:
        import importlib.util
        module_path = Path(__file__).resolve().parent / "aura_v05329_mexc_intent_ledger.py"
        spec = importlib.util.spec_from_file_location("aura_v05329_mexc_intent_ledger", module_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod


def _load_adapter_module():
    """Dynamic import of v0.5.3.27. Used only by authorized_submit() to
    call the unmodified validate_spec() / submit()."""
    try:
        import aura_v05327_mexc_execution_adapter as adapter27  # type: ignore
        return adapter27
    except ImportError:
        import importlib.util
        module_path = Path(__file__).resolve().parent / "aura_v05327_mexc_execution_adapter.py"
        spec = importlib.util.spec_from_file_location("aura_v05327_mexc_execution_adapter", module_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod


def _load_revalidation_module():
    """Dynamic import of v0.5.3.40 (Pre-Submission Revalidation +
    Unified Execution Audit Trail), same pattern as the other
    _load_*_module() helpers above. Used by revalidate_before_submission()
    for the live-price check (opt-in, see that function's docstring) and
    by authorized_submit() for the audit-trail writes (also opt-in, via
    audit_dir)."""
    try:
        import aura_v05340_pre_submission_revalidation as revalidation40  # type: ignore
        return revalidation40
    except ImportError:
        import importlib.util
        module_path = Path(__file__).resolve().parent / "aura_v05340_pre_submission_revalidation.py"
        spec = importlib.util.spec_from_file_location(
            "aura_v05340_pre_submission_revalidation", module_path
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod


def _audit_write_best_effort(revalidation40: Any, fn_name: str, *args: Any, **kwargs: Any) -> None:
    """v0.5.3.40 audit-trail writes are best-effort, never safety-
    relevant -- found and resolved this way per Martin's explicit
    decision on the .40 completion report ("best-effort audit writes"),
    after discovering during this milestone's own end-to-end testing that
    a genuine retry of a decision whose audit trail already reached a
    terminal state (e.g. a prior REVALIDATION_FAILED), or two callers
    racing to append the SAME event to the SAME client_order_id's record
    (only one of which can ever be the real claim winner at the .32/.37
    layer -- the audit trail itself has no concept of arbitrating that),
    would otherwise raise an unhandled IllegalTransitionError straight
    out of authorize()/authorized_submit() -- an audit-trail bookkeeping
    conflict was crashing a real authorization/claim/submission decision
    that was itself entirely correct. This wrapper swallows ONLY
    IllegalTransitionError (an event that is structurally well-formed but
    illegal to append given the record's CURRENT state -- exactly the
    expected-under-concurrency-or-retry case) -- never AuditTrailError
    (which also covers a genuinely corrupted/tampered record failing its
    own hash self-verification, or an already-exists conflict on
    record_decision() -- neither of those is a benign race, and neither
    is swallowed here). The claim/replay-protection layers (.32/.37) and
    the .29 ledger remain the sole source of truth for what was actually
    claimed/submitted either way -- this only ever affects whether the
    SEPARATE .40 audit trail captures every single attempt, never
    execution safety itself."""
    fn = getattr(revalidation40, fn_name)
    try:
        fn(*args, **kwargs)
    except revalidation40.IllegalTransitionError:
        pass


def _load_replay_module():
    """Dynamic import of v0.5.3.32 (MEXC Replay-Protected Consumption),
    same pattern as _load_ledger_module()/_load_adapter_module(). Used by
    authorized_submit() as the authoritative, durable, authorization_id ->
    client_order_id claim store -- see module docstring and
    authorized_submit()'s own docstring for why this replaced the bare
    marker-file claim this function used before v0.5.3.32 existed."""
    try:
        import aura_v05332_mexc_replay_protected_consumption as replay32  # type: ignore
        return replay32
    except ImportError:
        import importlib.util
        module_path = Path(__file__).resolve().parent / "aura_v05332_mexc_replay_protected_consumption.py"
        spec = importlib.util.spec_from_file_location("aura_v05332_mexc_replay_protected_consumption", module_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod


# --------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------- #

def load_config(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Safe-by-default local config, mirroring v0.5.3.19's load_config()
    precedent. Only known keys may override the safe-closed defaults
    (kill_switch True, every *_authorized False); unknown keys in
    `overrides` are ignored rather than silently accepted into the
    evaluated state."""
    config = dict(DEFAULT_AUTH_CONFIG)
    if overrides:
        for key in DEFAULT_AUTH_CONFIG:
            if key in overrides:
                config[key] = overrides[key]
    return config


# --------------------------------------------------------------------- #
# SafetyState assembly -- the MEXC-native replacement for literally
# reusing v0.5.3.19.build_safety() (see module docstring for why that is
# not viable).
# --------------------------------------------------------------------- #

def _compute_reconciliation_health(ledger_base_dir: Any) -> dict[str, Any]:
    """Genuinely computed from v0.5.3.29's real on-disk intent ledger --
    never fabricated. list_unresolved_intents() already isolates
    unreadable/corrupt records as their own BLOCKED_UNREADABLE /
    BLOCKED_VERIFICATION_FAILED entries rather than raising, so a ledger
    read failure surfaces as BLOCKED health here too -- fail closed, never
    silently HEALTHY."""
    ledger29 = _load_ledger_module()
    try:
        unresolved = ledger29.list_unresolved_intents(base_dir=ledger_base_dir)
    except Exception as exc:
        return {
            "status": "BLOCKED",
            "reason": f"LEDGER_UNREADABLE:{type(exc).__name__}",
            "unresolved_count": None,
            "escalated_count": None,
            "blocked_unreadable_count": None,
        }

    escalated = [r for r in unresolved if r.get("current_state") == "ESCALATED_HUMAN_REVIEW"]
    blocked_unreadable = [
        r for r in unresolved
        if r.get("current_state") in ("BLOCKED_UNREADABLE", "BLOCKED_VERIFICATION_FAILED")
    ]

    if blocked_unreadable:
        status = "BLOCKED"
        reason = "UNREADABLE_LEDGER_RECORDS_PRESENT"
    elif escalated:
        status = "DEGRADED"
        reason = "ESCALATED_INTENTS_PRESENT"
    else:
        status = "HEALTHY"
        reason = None

    return {
        "status": status,
        "reason": reason,
        "unresolved_count": len(unresolved),
        "escalated_count": len(escalated),
        "blocked_unreadable_count": len(blocked_unreadable),
    }


def _compute_replay_protection(claims_dir: Path | None) -> dict[str, Any]:
    """Genuinely probes whether the authorization claims directory is
    actually usable (creatable + writable) rather than assuming so --
    replay protection cannot be "healthy" if the claim store itself is
    unreachable."""
    claims_root = claims_dir or DEFAULT_AUTHORIZATION_CLAIMS_DIR
    try:
        claims_root.mkdir(parents=True, exist_ok=True)
        probe = claims_root / f".probe-{uuid.uuid4().hex}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return {"status": "HEALTHY", "reason": None}
    except OSError as exc:
        return {"status": "BLOCKED", "reason": f"CLAIMS_DIR_UNUSABLE:{type(exc).__name__}"}


def _safety_state_content(safety_state: dict[str, Any]) -> dict[str, Any]:
    """The fingerprint-relevant subset of a safety_state. Deliberately
    excludes assembled_at and safety_state_hash themselves (a timestamp
    and a self-hash are not part of the content they describe) so that
    two assemble_safety_state() calls against unchanged underlying state
    produce the SAME content fingerprint even though their assembled_at
    timestamps differ."""
    return {
        "schema_version": safety_state.get("schema_version"),
        "agent_version": safety_state.get("agent_version"),
        "engine": safety_state.get("engine"),
        "kill_switch": safety_state.get("kill_switch"),
        "execution_authorized": safety_state.get("execution_authorized"),
        "paper_execution_authorized": safety_state.get("paper_execution_authorized"),
        "live_execution_authorized": safety_state.get("live_execution_authorized"),
        "reconciliation_health": safety_state.get("reconciliation_health"),
        "replay_protection": safety_state.get("replay_protection"),
        "guardrails": safety_state.get("guardrails"),
    }


def assemble_safety_state(config: dict[str, Any] | None = None, ledger_base_dir: Any = None,
                           claims_dir: Path | None = None, now: datetime | None = None) -> dict[str, Any]:
    """MEXC-native safety-state assembler. Mirrors v0.5.3.19's rigor and
    pattern (safe-by-default config, explicit guardrail dict, canonical
    fingerprint) without literally routing MEXC data through v0.5.3.19's
    Alpaca-symbol-hardwired build_safety() -- see module docstring.

    config's kill_switch / *_authorized booleans are LOCAL, OPERATOR-
    SUPPLIED values (exactly like v0.5.3.19's kill_switch / orders_enabled
    / *_execution_enabled) -- recorded for auditability, never treated as
    hash-chain-verified truth on their own. What IS genuinely computed
    here, and cannot be forged by hand-editing a config file alone, is
    reconciliation_health (from v0.5.3.29's real on-disk ledger) and
    replay_protection (from whether the claims directory is actually
    usable).
    """
    cfg = load_config(config)
    moment = _now(now)

    reconciliation_health = _compute_reconciliation_health(ledger_base_dir)
    replay_protection = _compute_replay_protection(claims_dir)

    content: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "agent_version": VERSION,
        "engine": ENGINE,
        "kill_switch": bool(cfg["kill_switch"]),
        "execution_authorized": bool(cfg["execution_authorized"]),
        "paper_execution_authorized": bool(cfg["paper_execution_authorized"]),
        "live_execution_authorized": bool(cfg["live_execution_authorized"]),
        "reconciliation_health": reconciliation_health,
        "replay_protection": replay_protection,
        "guardrails": {
            "single_source_of_truth": True,
            "reconciliation_only": True,
            "position_creation_from_observation": False,
            "exchange_state_mutation": False,
            "orders_allowed": False,
            "live_execution": False,
            "fail_closed": True,
        },
    }

    safety_state = dict(content)
    safety_state["assembled_at"] = _iso(moment)
    safety_state["safety_state_ttl_seconds"] = int(cfg["safety_state_ttl_seconds"])
    safety_state["safety_state_hash"] = _fingerprint(content)
    return safety_state


def verify_safety_state(safety_state: dict[str, Any]) -> tuple[bool, list[str]]:
    """Self-consistency check: does the stored safety_state_hash match a
    fresh recomputation from the state's own content? Catches direct
    tampering of a serialized safety_state (e.g. an attacker flipping
    execution_authorized to true in a stored JSON file without
    recomputing the hash) -- the exact provenance gap this module exists
    to close."""
    if not isinstance(safety_state, dict):
        return False, ["NOT_A_DICT"]
    expected = _fingerprint(_safety_state_content(safety_state))
    if safety_state.get("safety_state_hash") != expected:
        return False, ["SAFETY_STATE_HASH_MISMATCH"]
    return True, []


def verify_authorization_record(record: dict[str, Any]) -> tuple[bool, list[str]]:
    """Self-consistency check for an AuthorizationRecord: recomputes
    authorization_hash from the record's own content (excluding the hash
    field itself) and compares."""
    if not isinstance(record, dict):
        return False, ["NOT_A_DICT"]
    content = {k: v for k, v in record.items() if k != "authorization_hash"}
    expected = _fingerprint(content)
    if record.get("authorization_hash") != expected:
        return False, ["AUTHORIZATION_RECORD_HASH_MISMATCH"]
    return True, []


# --------------------------------------------------------------------- #
# Shared guardrail policy -- used by BOTH authorize() and
# revalidate_before_submission() (Martin constraint 3).
# --------------------------------------------------------------------- #

def _evaluate_guardrails(execution_spec: dict[str, Any], safety_state: dict[str, Any],
                          now: datetime) -> dict[str, Any]:
    """Single shared guardrail policy. revalidate_before_submission() is a
    freshness/identity/safety re-check of this SAME policy, never a
    second, divergent one (Martin constraint 3).

    Deliberately never reads execution_spec's own kill_switch /
    execution_authorized / live_execution_authorized fields -- those
    remain v0.5.3.27.validate_spec()'s own unmodified, legacy,
    defense-in-depth check. This function's authorization decision comes
    entirely from the independently-assembled safety_state (Martin
    constraint 1)."""
    evaluated = {
        "kill_switch_clear": False,
        "execution_authorized": False,
        "mode_authorized": False,
        "reconciliation_healthy": False,
        "replay_protection_healthy": False,
        "spec_not_expired": False,
    }

    def blocked(reason: str, detail: Any = None) -> dict[str, Any]:
        return {"blocked": True, "reason": reason, "detail": detail, "evaluated": evaluated}

    if not isinstance(execution_spec, dict):
        return blocked("INVALID_EXECUTION_SPEC", "not a dict")

    if execution_spec.get("exchange") != EXPECTED_EXCHANGE:
        return blocked("UNEXPECTED_EXCHANGE", str(execution_spec.get("exchange")))

    if execution_spec.get("execution_spec_version") != EXPECTED_SPEC_VERSION:
        return blocked("UNSUPPORTED_SPEC_VERSION", str(execution_spec.get("execution_spec_version")))

    if safety_state.get("kill_switch") is True:
        return blocked("KILL_SWITCH_ENGAGED")
    evaluated["kill_switch_clear"] = True

    if safety_state.get("execution_authorized") is not True:
        return blocked("SAFETY_STATE_NOT_AUTHORIZED")
    evaluated["execution_authorized"] = True

    account_mode = execution_spec.get("account_mode")
    if account_mode not in ALLOWED_ACCOUNT_MODES:
        return blocked("INVALID_ACCOUNT_MODE", str(account_mode))

    if account_mode == "LIVE":
        if safety_state.get("live_execution_authorized") is not True:
            return blocked("LIVE_EXECUTION_NOT_AUTHORIZED")
    else:  # PAPER
        if safety_state.get("paper_execution_authorized") is not True:
            return blocked("PAPER_EXECUTION_NOT_AUTHORIZED")
    evaluated["mode_authorized"] = True

    reconciliation_health = safety_state.get("reconciliation_health") or {}
    if reconciliation_health.get("status") != "HEALTHY":
        return blocked("RECONCILIATION_NOT_HEALTHY", reconciliation_health.get("status"))
    evaluated["reconciliation_healthy"] = True

    replay_protection = safety_state.get("replay_protection") or {}
    if replay_protection.get("status") != "HEALTHY":
        return blocked("REPLAY_PROTECTION_NOT_HEALTHY", replay_protection.get("status"))
    evaluated["replay_protection_healthy"] = True

    raw_expiry = execution_spec.get("expires_at")
    if raw_expiry is not None:
        spec_expiry = _parse_iso(raw_expiry)
        if spec_expiry is None:
            return blocked("INVALID_EXPIRES_AT", str(raw_expiry))
        if spec_expiry <= now:
            return blocked("EXPIRED_EXECUTION_SPEC", _iso(spec_expiry))
    evaluated["spec_not_expired"] = True

    return {"blocked": False, "reason": None, "detail": None, "evaluated": evaluated}


def _reject(reason: str, detail: Any = None) -> dict[str, Any]:
    return {
        "status": "AUTHORIZATION_REJECTED",
        "reason": reason,
        "detail": detail,
        "authorization_id": None,
    }


# --------------------------------------------------------------------- #
# Authorization
# --------------------------------------------------------------------- #

def authorize(execution_spec: dict[str, Any], safety_state: dict[str, Any] | None = None,
              config: dict[str, Any] | None = None, ledger_base_dir: Any = None,
              claims_dir: Path | None = None, now: datetime | None = None,
              audit_dir: Path | None = None) -> dict[str, Any]:
    """Evaluate execution_spec against a freshly, independently assembled
    SafetyState and, if every guardrail passes, issue a fingerprint-bound,
    TTL-bound AuthorizationRecord.

    CRITICAL (Martin constraint 1): a caller-supplied `safety_state` is
    NEVER trusted as authoritative on its own. This function always
    recomputes its own fresh safety_state via assemble_safety_state()
    using the same config/ledger_base_dir/claims_dir/now, and:
      - if the supplied safety_state fails its own self-consistency check
        (verify_safety_state), authorization is rejected with
        SAFETY_STATE_FINGERPRINT_MISMATCH;
      - if the supplied safety_state is older than its own TTL relative
        to `now`, authorization is rejected with STALE_SAFETY_STATE;
      - if the supplied safety_state's content does not match what was
        just independently, genuinely recomputed, authorization is
        rejected with SAFETY_STATE_FORGED -- regardless of what the
        caller's copy claims.
    Callers do not need to pass safety_state at all; it exists only so a
    caller can assert what it believes the state to be and have that
    belief checked -- never to inject trust. The guardrail decision itself
    (_evaluate_guardrails) always runs against the freshly recomputed
    safety_state, never the caller-supplied one.
    """
    cfg = load_config(config)
    moment = _now(now)

    fresh_safety_state = assemble_safety_state(cfg, ledger_base_dir=ledger_base_dir, claims_dir=claims_dir, now=moment)

    if safety_state is not None:
        ok, verify_errors = verify_safety_state(safety_state)
        if not ok:
            return _reject("SAFETY_STATE_FINGERPRINT_MISMATCH", ",".join(verify_errors))

        assembled_at = _parse_iso(safety_state.get("assembled_at"))
        ttl = safety_state.get("safety_state_ttl_seconds")
        if assembled_at is None or not isinstance(ttl, int):
            return _reject("MALFORMED_SAFETY_STATE", "missing assembled_at or safety_state_ttl_seconds")
        if assembled_at + timedelta(seconds=ttl) <= moment:
            return _reject("STALE_SAFETY_STATE", safety_state.get("assembled_at"))

        supplied_fp = _fingerprint(_safety_state_content(safety_state))
        fresh_fp = _fingerprint(_safety_state_content(fresh_safety_state))
        if supplied_fp != fresh_fp:
            return _reject("SAFETY_STATE_FORGED", "caller-supplied safety_state does not match independently recomputed safety_state")

    guardrail_result = _evaluate_guardrails(execution_spec, fresh_safety_state, moment)
    if guardrail_result["blocked"]:
        return _reject(guardrail_result["reason"], guardrail_result.get("detail"))

    spec_fingerprint = _fingerprint(execution_spec)
    safety_state_fingerprint = _fingerprint(_safety_state_content(fresh_safety_state))

    issued_at = moment
    ttl_seconds = min(int(cfg["authorization_ttl_seconds"]), int(fresh_safety_state["safety_state_ttl_seconds"]))
    expires_at = issued_at + timedelta(seconds=ttl_seconds)

    # Weakest-link TTL: also bounded by the execution_spec's own
    # expires_at, if present (already confirmed not yet expired above).
    spec_expiry = _parse_iso(execution_spec.get("expires_at"))
    if spec_expiry is not None and spec_expiry < expires_at:
        expires_at = spec_expiry

    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "agent_version": VERSION,
        "engine": ENGINE,
        "authorization_id": f"AUTH-{uuid.uuid4().hex}",
        "status": "AUTHORIZED",
        "account_mode": execution_spec.get("account_mode"),
        "client_order_id": execution_spec.get("client_order_id"),
        "spec_fingerprint": spec_fingerprint,
        "safety_state_fingerprint": safety_state_fingerprint,
        "issued_at": _iso(issued_at),
        "expires_at": _iso(expires_at),
        "guardrails_evaluated": guardrail_result["evaluated"],
    }
    record["authorization_hash"] = _fingerprint(record)
    if audit_dir is not None:
        # Opt-in, v0.5.3.40: only a genuine AUTHORIZED outcome is ever
        # recorded (design doc Section 2.2) -- a rejected authorization
        # is never written as an event, because it was never real. The
        # DECISION event for this client_order_id must already exist
        # (v0.5.3.38 writes it at orchestration start); a genuinely
        # missing DECISION record still raises (AUDIT_RECORD_NOT_FOUND,
        # an AuditTrailError -- a real caller-usage defect, never
        # swallowed). What IS swallowed, via _audit_write_best_effort
        # (best-effort audit writes, Martin's explicit .40 decision): a
        # retry or a concurrent race appending AUTHORIZED to a
        # client_order_id whose audit record already moved past that
        # point -- an audit-trail bookkeeping conflict, not a reason to
        # fail a real authorization that already succeeded.
        client_order_id = execution_spec.get("client_order_id")
        if client_order_id:
            _audit_write_best_effort(_load_revalidation_module(), "record_authorized",
                                      client_order_id, base_dir=audit_dir)
    return record


def claim_authorization(claims_dir: Path, authorization_id: str) -> bool:
    """Atomic, single-use claim on an authorization_id. Mirrors
    v0.5.3.27's claim_client_order_id() exactly (os.O_CREAT | os.O_EXCL,
    never released under any outcome), but is a SEPARATE, independently
    owned claims directory -- see authorized_submit() for how this claim
    and v0.5.3.27's own client_order_id claim interact (Martin
    constraint 2). Returns True if this call granted the claim, False if
    it was already claimed.

    NOTE (v0.5.3.32 milestone): authorized_submit() no longer calls this
    function -- it now claims through v0.5.3.32 (MEXC Replay-Protected
    Consumption), which stores a durable, ledger-linked
    authorization_id -> client_order_id record instead of this bare
    marker file, per the implementation spec's Sec.3. This function is
    left in place unmodified as a still-correct, still-tested,
    general-purpose atomic-claim primitive -- existing callers and tests
    against it are unaffected."""
    claims_dir.mkdir(parents=True, exist_ok=True)
    claim_path = claims_dir / f"{authorization_id}.claimed"
    try:
        fd = os.open(str(claim_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as f:
            f.write(_iso(_now()))
        return True
    except FileExistsError:
        return False


def revalidate_before_submission(record: dict[str, Any], execution_spec: dict[str, Any],
                                  config: dict[str, Any] | None = None, ledger_base_dir: Any = None,
                                  claims_dir: Path | None = None, now: datetime | None = None,
                                  price_fetch_fn: Any = None, max_quote_age_seconds: float | None = None,
                                  max_price_drift_bps: float | None = None) -> dict[str, Any]:
    """Final freshness/identity/safety re-check, immediately before
    handing execution_spec to v0.5.3.27.submit(). Reuses
    _evaluate_guardrails() -- the SAME policy authorize() used (Martin
    constraint 3) -- against a FRESH assemble_safety_state() call, never a
    cached or passed-in one. Also re-checks that execution_spec still
    fingerprints to what was recorded on `record` at authorize()-time, so
    a spec modified after authorization is caught here even if it would
    otherwise still pass the guardrails on its own new merits.

    v0.5.3.40 EXTENSION (additive, opt-in): when price_fetch_fn is
    supplied, this function ALSO performs a genuine live-price
    revalidation (v0.5.3.40.revalidate_market_state()) against
    execution_spec's reference_price, as the last check before returning
    REVALIDATED -- Martin's approved Option B. When price_fetch_fn is
    None (the default, and every caller before v0.5.3.40 existed),
    behavior is byte-for-byte unchanged from before v0.5.3.40. A price
    rejection is reported through the exact same _reject() shape as
    every other rejection reason in this function, with
    price_check_failed=True added so a caller can distinguish it if it
    needs to."""
    cfg = load_config(config)
    moment = _now(now)

    ok, record_errors = verify_authorization_record(record)
    if not ok:
        return _reject("AUTHORIZATION_RECORD_TAMPERED", ",".join(record_errors))

    if record.get("status") != "AUTHORIZED":
        return _reject("AUTHORIZATION_NOT_VALID", f"record status is {record.get('status')!r}")

    expires_at = _parse_iso(record.get("expires_at"))
    if expires_at is None or expires_at <= moment:
        return _reject("EXPIRED_AUTHORIZATION", record.get("expires_at"))

    current_spec_fingerprint = _fingerprint(execution_spec)
    if current_spec_fingerprint != record.get("spec_fingerprint"):
        return _reject("EXECUTION_SPEC_FINGERPRINT_MISMATCH", "execution_spec has changed since authorization was issued")

    if execution_spec.get("client_order_id") != record.get("client_order_id"):
        return _reject("CLIENT_ORDER_ID_MISMATCH", "execution_spec client_order_id does not match the authorized record")

    fresh_safety_state = assemble_safety_state(cfg, ledger_base_dir=ledger_base_dir, claims_dir=claims_dir, now=moment)

    guardrail_result = _evaluate_guardrails(execution_spec, fresh_safety_state, moment)
    if guardrail_result["blocked"]:
        return _reject(guardrail_result["reason"], guardrail_result.get("detail"))

    price_check: dict[str, Any] | None = None
    if price_fetch_fn is not None:
        revalidation40 = _load_revalidation_module()
        kwargs: dict[str, Any] = {}
        if max_quote_age_seconds is not None:
            kwargs["max_quote_age_seconds"] = max_quote_age_seconds
        if max_price_drift_bps is not None:
            kwargs["max_price_drift_bps"] = max_price_drift_bps
        try:
            price_check = revalidation40.revalidate_market_state(
                reference_price=execution_spec.get("reference_price"),
                price_fetch_fn=price_fetch_fn,
                now_dt=moment,
                **kwargs,
            )
        except revalidation40.RevalidationRejected as exc:
            rejected = _reject(exc.reason, exc.detail)
            rejected["price_check_failed"] = True
            return rejected

    result = {
        "status": "REVALIDATED",
        "authorization_id": record.get("authorization_id"),
        "revalidated_at": _iso(moment),
        "reason": None,
        "detail": None,
    }
    if price_check is not None:
        result["price_check"] = price_check
    return result


def authorized_submit(record: dict[str, Any], execution_spec: dict[str, Any],
                       authorization_claims_dir: Path, adapter_claims_dir: Path,
                       config: dict[str, Any] | None = None, ledger_base_dir: Any = None,
                       exchange: Any = None, now: datetime | None = None,
                       price_fetch_fn: Any = None, max_quote_age_seconds: float | None = None,
                       max_price_drift_bps: float | None = None,
                       max_revalidation_to_submission_seconds: float | None = None,
                       audit_dir: Path | None = None) -> dict[str, Any]:
    """The documented, tested interaction between this module's
    authorization_id claim and v0.5.3.27's own client_order_id claim
    (Martin constraint 2).

    v0.5.3.40 REORDERING (approved design, "revalidate -> claim ->
    submit where architecturally possible"): the sequence below claims
    AFTER revalidation now, reversed from the pre-v0.5.3.40 order
    (claim -> revalidate -> submit), specifically so a spec that is
    already stale (expired, guardrail-failed, or -- new in v0.5.3.40 --
    already drifted in price) never burns an authorization_id claim
    slot at all. The claim itself is UNCHANGED: still atomic
    (O_CREAT|O_EXCL via v0.5.3.32), still single-winner, still NEVER
    released under any outcome once granted -- this reordering changes
    only what happens *before* a claim is attempted, never what the
    claim itself guarantees.

    Sequence:
      1. revalidate_before_submission() -- a fresh freshness/identity/
         safety re-check, the SAME policy authorize() used, NOW ALSO
         including the v0.5.3.40 live-price check when price_fetch_fn is
         supplied (opt-in; see that function's docstring). If this
         fails, NOTHING has been claimed anywhere -- the authorization
         is simply rejected, exactly like an authorize()-time rejection
         (the v0.5.3.29 MEXC intent, if one already exists for this
         client_order_id, stays at its existing NEW/open state --
         confirmed safe by the v0.5.3.40 pre-implementation
         investigation; no v0.5.3.29 change was needed).
      2. Claim authorization_id via v0.5.3.32 (MEXC Replay-Protected
         Consumption), against `authorization_claims_dir` -- always
         separate from v0.5.3.27's own DEFAULT_CLAIMS_DIR /
         `adapter_claims_dir`. This is the durable, ledger-linked claim
         required by the implementation spec's Sec.3 (v0.5.3.32 stores the
         authorization_id -> client_order_id link; see v0.5.3.32's module
         docstring for why it -- not v0.5.3.17, not a bare marker file --
         is the correct store). If the authorization_id is already
         claimed, return AUTHORIZATION_ALREADY_CONSUMED with
         `existing_intent_id` set to the prior claim's client_order_id
         (resolvable against v0.5.3.29.get_intent()) -- v0.5.3.27.submit()
         is NEVER called in this case: this is the fail-closed race
         outcome when a concurrent process already won the claim after
         both processes independently passed revalidation (design doc
         Section 5) -- a clean loss, not a corruption, and the losing
         process's own revalidation result is simply discarded (it was a
         pure read with no side effects). If the claim store itself is
         unreachable, return AUTHORIZATION_ALREADY_CONSUMED with reason
         CLAIM_STORE_UNREACHABLE -- fails closed identically, v0.5.3.27.
         submit() is NEVER called either way.

         NOTE: v0.5.3.31's own claim_authorization() (the bare O_CREAT|
         O_EXCL marker-file primitive from the prior milestone) is left in
         place, unmodified, as a still-valid, still-tested, general-
         purpose atomic-claim utility -- it is simply no longer the
         mechanism this function relies on for the authorization_id claim
         decision, now that v0.5.3.32 provides a durable, ledger-linked
         equivalent per the specification.
      3. IF the claim was won: a v0.5.3.40 ceiling check
         (check_revalidation_to_submission_ceiling(), opt-in, only run
         when a price check happened in step 1) immediately before the
         actual submission call -- the fail-closed backstop for the
         residual gap between "price observed" and "order actually
         submitted" (design doc Section 5). If this fires, the claim
         from step 2 remains PERMANENTLY consumed -- the no-release
         invariant is never violated by this check; it only blocks the
         submit call itself from proceeding.
      4. v0.5.3.27.validate_spec() (unmodified -- its own kill_switch /
         execution_authorized / live_execution_authorized / symbol / etc.
         checks on execution_spec still apply in full as a legacy,
         defense-in-depth gate) followed by v0.5.3.27.submit() (unmodified),
         which makes its OWN, separate, atomic client_order_id claim.
           - If v0.5.3.27's client_order_id claim fails
             (DUPLICATE_CLAIM_REJECTED), no order is submitted. The
             authorization_id claim from step 2 remains permanently
             consumed -- it is NOT released or retried. This state
             (authorization succeeded, but v0.5.3.27's own independent
             replay guard refused the same client_order_id) is returned
             as its own explicit, clearly labeled status
             (AUTHORIZED_BUT_ADAPTER_CLAIM_REJECTED) so it is never
             silently stranded.
           - Otherwise v0.5.3.27's result (SUBMITTED / REJECTED /
             EXECUTION_UNCERTAIN) is returned as-is. In every case,
             including EXECUTION_UNCERTAIN, the authorization_id claim
             remains permanently consumed -- there is no retry path for
             the same authorization_id under any outcome. A genuine retry
             requires a brand-new authorize() call (a new
             authorization_id), which will itself be blocked by
             v0.5.3.27's OWN claim if client_order_id is reused, exactly
             as intended.

    audit_dir (opt-in, v0.5.3.40): when supplied, REVALIDATED/CONSUMED/
    SUBMISSION_ATTEMPTED/OUTCOME events are written to the unified audit
    trail for this client_order_id (which must already have a DECISION
    event recorded -- v0.5.3.38 does this at orchestration start). When
    None (the default), no audit-trail writes happen at all -- behavior
    is byte-for-byte unchanged from before v0.5.3.40.

    claim_granted (added v0.5.3.38 wiring, NOT part of the original
    v0.5.3.40 design doc): every returned dict now also carries a plain
    boolean saying whether v0.5.3.32's authorization_id claim was
    actually granted during THIS call. This is necessary, not
    cosmetic: since the v0.5.3.40 reordering, a "REVALIDATION_FAILED"
    status is ambiguous on its own -- it now covers BOTH a pre-claim
    rejection (structural guard or revalidate_before_submission()
    failure, step 1 -- nothing claimed) AND a post-claim ceiling-check
    rejection (step 3 -- the claim WAS granted moments earlier and
    stays permanently consumed). A caller (v0.5.3.38) that maps this
    function's outcome onto v0.5.3.29's ledger MUST know which case it
    is: calling v0.5.3.29.record_claimed() for a REVALIDATION_FAILED
    outcome that never actually claimed anything would incorrectly
    transition a v0.5.3.29 intent to CLAIMED (and then immediately
    escalate it) for a decision that is genuinely still safe to retry
    -- directly violating Martin's own explicit v0.5.3.40 instruction
    ("do not permanently consume an authorization merely because an
    already-stale authorization reached the revalidation stage"). This
    field is what lets v0.5.3.38 avoid that. claim_granted is False on
    every return path above this point in the function and True on
    every return path below the v0.5.3.32 claim() call that actually
    won (AUTHORIZATION_ALREADY_CONSUMED is also False -- THIS call's own
    claim attempt did not succeed, even though some other call's did).
    """
    cfg = load_config(config)
    moment = _now(now)
    authorization_id = record.get("authorization_id")
    client_order_id = execution_spec.get("client_order_id") if isinstance(execution_spec, dict) else None
    revalidation40 = _load_revalidation_module() if audit_dir is not None else None

    # Structural guard: a rejection dict (from authorize()) or any other
    # malformed record must never even reach the revalidation/claim
    # steps -- there is nothing valid to claim on behalf of. This is
    # checked here, not just relied upon at the caller, because
    # authorized_submit() is itself part of the fail-closed boundary.
    if record.get("status") != "AUTHORIZED" or not isinstance(authorization_id, str) or not authorization_id:
        return {
            "status": "REVALIDATION_FAILED",
            "authorization_id": authorization_id,
            "reason": "AUTHORIZATION_NOT_VALID",
            "detail": f"record status is {record.get('status')!r}",
            "existing_intent_id": None,
            "submission_result": None,
            "claim_granted": False,
        }

    revalidation = revalidate_before_submission(
        record, execution_spec, config=cfg, ledger_base_dir=ledger_base_dir,
        claims_dir=authorization_claims_dir, now=moment,
        price_fetch_fn=price_fetch_fn, max_quote_age_seconds=max_quote_age_seconds,
        max_price_drift_bps=max_price_drift_bps,
    )
    if audit_dir is not None and client_order_id:
        _audit_write_best_effort(
            revalidation40, "record_revalidated", client_order_id,
            passed=revalidation["status"] == "REVALIDATED",
            reason=revalidation.get("reason"),
            detail=revalidation.get("detail"),
            price_result=revalidation.get("price_check"),
            base_dir=audit_dir,
        )
    if revalidation["status"] != "REVALIDATED":
        # Nothing claimed yet -- exactly like an authorize()-time
        # rejection, the v0.5.3.29 intent (if any) stays at its existing
        # open state, safe to retry on a future cycle. No CONSUMED event
        # is written -- consumption was never attempted.
        return {
            "status": "REVALIDATION_FAILED",
            "authorization_id": authorization_id,
            "reason": revalidation.get("reason"),
            "detail": revalidation.get("detail"),
            "existing_intent_id": None,
            "submission_result": None,
            "claim_granted": False,
        }

    replay32 = _load_replay_module()
    claim_result = replay32.claim(
        authorization_id, client_order_id,
        spec_fingerprint=record.get("spec_fingerprint"),
        safety_state_fingerprint=record.get("safety_state_fingerprint"),
        claims_dir=authorization_claims_dir,
    )
    if audit_dir is not None and client_order_id:
        _audit_write_best_effort(
            revalidation40, "record_consumed", client_order_id,
            granted=bool(claim_result["granted"]),
            reason=claim_result.get("reason"), base_dir=audit_dir,
        )
    if not claim_result["granted"]:
        return {
            "status": "AUTHORIZATION_ALREADY_CONSUMED",
            "authorization_id": authorization_id,
            "reason": claim_result.get("reason", "AUTHORIZATION_ID_ALREADY_CLAIMED"),
            "detail": None,
            "existing_intent_id": claim_result.get("existing_intent_id"),
            "submission_result": None,
            "claim_granted": False,
        }

    # v0.5.3.40 ceiling check -- fail-closed backstop for the residual
    # claim-to-submit race, only meaningful when a price check actually
    # happened above. The claim just granted stays consumed regardless
    # of this check's outcome -- never released.
    price_check = revalidation.get("price_check")
    if price_check is not None:
        kwargs: dict[str, Any] = {}
        if max_revalidation_to_submission_seconds is not None:
            kwargs["max_seconds"] = max_revalidation_to_submission_seconds
        v40 = revalidation40 or _load_revalidation_module()
        try:
            v40.check_revalidation_to_submission_ceiling(
                price_checked_at=price_check["price_checked_at"], now_dt=moment, **kwargs
            )
        except v40.RevalidationRejected as exc:
            return {
                "status": "REVALIDATION_FAILED",
                "authorization_id": authorization_id,
                "reason": exc.reason,
                "detail": exc.detail,
                "existing_intent_id": None,
                "submission_result": None,
                # Unlike the pre-claim REVALIDATION_FAILED above, the
                # authorization_id claim WAS just granted (the ceiling
                # check runs only after a successful claim, step 3) --
                # it remains permanently consumed. See claim_granted's
                # docstring entry above the structural guard for why this
                # distinction matters to callers (v0.5.3.38).
                "claim_granted": True,
            }

    adapter27 = _load_adapter_module()
    try:
        validated_spec = adapter27.validate_spec(execution_spec)
    except Exception as exc:
        return {
            "status": "ADAPTER_SPEC_REJECTED",
            "authorization_id": authorization_id,
            "reason": f"ADAPTER_VALIDATE_SPEC_FAILED:{exc}",
            "detail": None,
            "existing_intent_id": None,
            "submission_result": None,
            "claim_granted": True,
        }

    if audit_dir is not None and client_order_id:
        _audit_write_best_effort(revalidation40, "record_submission_attempted",
                                  client_order_id, base_dir=audit_dir)

    submission_result = adapter27.submit(validated_spec, adapter_claims_dir, exchange=exchange)

    if audit_dir is not None and client_order_id:
        _audit_write_best_effort(
            revalidation40, "record_outcome", client_order_id,
            outcome=submission_result.get("status", "UNKNOWN"),
            detail=submission_result.get("reason"),
            base_dir=audit_dir,
        )

    if submission_result.get("status") == "DUPLICATE_CLAIM_REJECTED":
        return {
            "status": "AUTHORIZED_BUT_ADAPTER_CLAIM_REJECTED",
            "authorization_id": authorization_id,
            "reason": "CLIENT_ORDER_ID_ALREADY_CLAIMED_BY_ADAPTER",
            "detail": "authorization_id claim is permanently consumed and will not be released or retried",
            "existing_intent_id": None,
            "submission_result": submission_result,
            "claim_granted": True,
        }

    return {
        "status": "SUBMISSION_ATTEMPTED",
        "authorization_id": authorization_id,
        "reason": None,
        "detail": None,
        "existing_intent_id": None,
        "submission_result": submission_result,
        "claim_granted": True,
    }
