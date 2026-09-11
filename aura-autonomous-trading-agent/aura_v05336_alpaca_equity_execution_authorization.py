#!/usr/bin/env python3
"""
AURA v0.5.3.36 -- Alpaca-Native Execution Authorization

Milestone 3 of the approved Alpaca-equity execution floor
(.34 -> .35 -> .36 -> .37 -> .38, per Martin's 2026-09-11 GO messages).
Sits between the canonical .33 execution specification (as narrowed by .34
metadata) and the .35 Alpaca equity/ETF adapter:

    .33 canonical execution specification
            |
            v
    .34 metadata (consumed indirectly, via .35's own re-derivation)
            |
            v
    .36 authorization              <- THIS MODULE
            |
            v
    .35 adapter validation/construction

============================================================================
0. Explicit instruction: do NOT copy .31 and rename MEXC -> Alpaca
============================================================================

.31/.32 are used as ARCHITECTURAL REFERENCE ONLY (read in full before
writing this file). Concretely, this module differs from .31 in every
place where MEXC's and Alpaca-equity's real constraints differ:

  - .31 authorizes .27's MEXC WIRE-format spec (exchange="MEXC",
    account_mode="LIVE"/"PAPER", leverage, reduce_only). This module
    authorizes the .33 CANONICAL spec directly (asset_class STOCK/ETF,
    venue ALPACA) -- .35 has no wire-format translator of its own (by
    design; see .35's docstring), so there is no Alpaca equity "wire spec"
    upstream of .35 to authorize instead.
  - .31 models account_mode as a field ON the spec it authorizes (MEXC's
    wire format carries account_mode itself). The .33 canonical spec has
    NO such field -- environment (PAPER, and only PAPER; see below) is an
    explicit, separate keyword this module's own authorize() takes, never
    read off the spec. This matches how .35's main() decides paper vs.
    anything-else at the execution boundary, not in the decision layer.
  - .31 models live_execution_authorized as a real (if false-by-default)
    config gate, because MEXC's real adapter (.27) has a genuine LIVE
    order-submission path for that gate to eventually guard. v0.5.3.35 has
    NO live-endpoint code path anywhere -- `TradingClient(..., paper=True)`
    is hardcoded in its one submission function. Modeling a
    "live_execution_authorized" boolean here would be worse than useless:
    it would look like a real safety control over something that cannot
    happen regardless of its value, which is its own kind of provenance
    forgery risk. Instead, ALLOWED_ENVIRONMENTS = frozenset({"PAPER"}) --
    LIVE is not a member at all. Requesting LIVE fails closed structurally
    (LIVE_EXECUTION_NOT_SUPPORTED_FOR_ALPACA_EQUITIES), not by an unset
    flag that could someday be flipped to true with nothing behind it.
  - .31's SafetyState includes a genuinely-computed `reconciliation_health`
    sourced from .29's real on-disk MEXC intent ledger (list_unresolved_
    intents()). Repository-wide inspection this session (matching the
    capability audit's own finding) confirms NO equivalent
    "list unresolved Alpaca-equity intents" function exists anywhere --
    .18 is a batch CLI tool over caller-supplied ledger/observed FILES, not
    a live query interface, and no Alpaca-equity intent ledger comparable
    to .29 has been built. Fabricating a `reconciliation_health: HEALTHY`
    signal with nothing real behind it would be exactly the provenance
    forgery this entire module lineage exists to prevent, so this module's
    SafetyState deliberately has NO reconciliation_health field. This is a
    disclosed SCOPE decision, not an oversight -- flagged prominently in
    the implementation report, revisit if/when an Alpaca-equity intent
    ledger (a natural .37-or-later candidate) is built.
  - .31's guardrail evaluation calls .27.validate_spec() only inside
    authorized_submit() (i.e. only at the very end, as a legacy
    defense-in-depth check before a real submission call). This module
    calls .35.validate_equity_spec() (unmodified, dynamically imported)
    as part of ITS OWN guardrail evaluation, in BOTH authorize() and
    revalidate_before_submission() -- because .35.validate_equity_spec()
    is the ONLY place that genuinely re-derives shortability from fresh
    Alpaca asset data and classifies direction capability (.34's job,
    consumed through .35, never re-implemented here -- see item 2 below).
    Reusing it is not "duplicating the adapter validation stack"
    (explicitly forbidden by instruction) -- it is calling the one real
    function that answers "can this instrument technically support this
    order," which .36's own authorization decision needs as an input
    fact, not as a question this module re-answers on its own.

============================================================================
1. What "authorization" means here, distinct from what .35 answers
============================================================================

.35 answers: "Can Alpaca represent and construct this order?" (structural/
capability question -- shortability, tradability, symbol shape, order
type).

.36 (this module) answers: "Is this EXACT execution request authorized to
proceed right now?" -- binding together, in one fingerprinted, TTL-bound
AuthorizationRecord: venue, environment, asset_class, symbol, direction,
position_intent, side, quantity, order_type, limit_price (when
applicable), client_order_id, decision/strategy provenance
(decision_id/strategy_id/strategy_version/source_kind), the .33 spec's own
spec_fingerprint, this module's independently-assembled safety_state
fingerprint, shortability_status, direction_capability, and an explicit
issued_at/expires_at TTL.

position_intent is NOT a field on the .33 canonical spec -- it is derived,
deterministically and purely, from `direction` via .35's own
DIRECTION_TO_SIDE_POSITION_INTENT_STR mapping (reused here, never
reimplemented -- see item 2). Because position_intent is a pure function
of direction, and direction is part of the canonical spec's content
covered by spec_fingerprint, ANY position_intent change is *definitionally*
a direction change -- caught by the single spec_fingerprint equality check
in revalidate_before_submission() with no separate check to forget. A
SECOND, explicit position_intent equality check is still included in
revalidate_before_submission() as defense-in-depth (in case the mapping
function itself is ever edited between authorize() and revalidation), and
gives Martin's required "position-intent modification rejection" test a
directly-named rejection reason (POSITION_INTENT_MISMATCH) rather than
relying solely on the argument above.

Likewise, quantity/symbol/direction/venue/order_type/asset_class are ALL
part of the .33 canonical spec's own content, all covered by ONE
spec_fingerprint (per .33's canonical_spec_fingerprint()). A single
"has the execution_spec's fingerprint changed since authorization"
check in revalidate_before_submission() therefore already catches every
one of Martin's per-field "modification rejection" test items (15-20) --
mirroring .31's own single-spec_fingerprint-check design exactly, not
reinvented.

============================================================================
2. What is reused from .35, never reimplemented
============================================================================

  - `validate_equity_spec(execution_spec, alpaca_asset)` -- the real
    capability/shortability/tradability check, called fresh in both
    authorize() and revalidate_before_submission() (never cached, never
    trusted from a prior call -- same "always re-derive from real asset
    data" discipline .35 itself established for .34's metadata).
  - `DIRECTION_TO_SIDE_POSITION_INTENT_STR` -- the direction -> (side,
    position_intent) mapping, used to compute the values bound into the
    AuthorizationRecord and re-checked at revalidation.

Both are imported dynamically (same `_load_*_module()` pattern already
established by .30/.31/.35), and .35 is NOT modified by this file.

============================================================================
3. Source of a decision confers no execution authority
============================================================================

`_evaluate_guardrails()` never reads `execution_spec.get("source_kind")`
at all -- a DETERMINISTIC_SIGNAL, an AI_PROPOSAL, and a HUMAN_OVERRIDE
flow through the identical guardrail sequence and are authorized or
rejected on identical grounds. A dedicated test
(test_ai_proposal_and_deterministic_signal_authorized_identically) proves
this by construction, not by convention.

============================================================================
4. Claim / replay note -- UPDATED in the .37 milestone (mirrors .31's own
   update in the .32 milestone exactly)
============================================================================

claim_authorization() below is an atomic (os.O_CREAT | os.O_EXCL),
single-use, NEVER-released marker-file claim on an authorization_id --
this module's OWN claims directory, separate from .31's MEXC claims dir,
separate from .22/.26's adapter claim dirs, and separate from .35 (which
has no claims directory of its own). This was, and remains, exactly the
primitive .31 used for its own authorization_id claim BEFORE .32 (MEXC
Replay-Protected Consumption) existed -- it is disclosed in .36's original
(v0.5.3.36) docstring as PROVISIONAL, "expected to be superseded by .37
exactly as .32 superseded .31's original claim_authorization()."

That supersession has now happened. As of this milestone,
authorized_order_request() below claims through
_load_replay_module().claim() (v0.5.3.37 -- Alpaca Replay-Protected
Consumption) instead of calling claim_authorization() directly.
claim_authorization() itself is UNCHANGED, still present, still tested --
it is simply no longer the mechanism the real production path relies on
for the authorization_id consumption decision, exactly mirroring how .32's
own docstring describes .31 being updated in the same way ("claim_
authorization() itself is left in place, unmodified"). This is the ONLY
change made to this file in the .37 milestone: authorized_order_request()'s
claim step (~6 lines) plus this docstring note. No other function, field,
config key, or guardrail in this module is touched. See the .37
implementation report for the full before/after diff and rationale
(Martin's instruction: "if .36 must be minimally changed... explain
exactly why before doing so"). The result is exactly ONE authoritative
definition of "consumed" in the real execution path -- .36's marker file
is never consulted by authorized_order_request() and can never disagree
with .37's durable record in production use.

============================================================================
5. What this module deliberately does NOT do
============================================================================

No replay-protected, ledger-linked claim store (.37's job). No Supervisor/
orchestrator (.38's job). No live execution -- ALLOWED_ENVIRONMENTS has
exactly one member, "PAPER". No modification to .17, .19, .21, .22, .23,
.27, .28, .29, .30, .31, .32, .33, .34, .35 -- all read-only inputs (.33
and .35 dynamically imported and called through their existing public
functions only). No actual Alpaca order submission anywhere in this file
or its tests -- `authorized_order_request()` stops at
`.35.to_alpaca_order_request()` (a pure, non-submitting SDK object
construction call), never at `.35.submit()`.

No real credentials, no network call, anywhere in this file.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

VERSION = "AURA v0.5.3.36"
ENGINE = "ALPACA_EQUITY_EXECUTION_AUTHORIZATION"
SCHEMA_VERSION = "1.0"

EXPECTED_VENUE = "ALPACA"
ALLOWED_ASSET_CLASSES = {"STOCK", "ETF"}

# LIVE is deliberately NOT a member -- see module docstring item 0.
ALLOWED_ENVIRONMENTS = frozenset({"PAPER"})

# Safe-by-default, local, operator-supplied -- mirrors .31's
# DEFAULT_AUTH_CONFIG precedent. No live_execution_authorized (see item 0).
# No reconciliation_staleness_minutes (see item 0 -- no reconciliation
# engine exists for Alpaca equities to source staleness from).
DEFAULT_AUTH_CONFIG: dict[str, Any] = {
    "kill_switch": True,
    "execution_authorized": False,
    "paper_execution_authorized": False,
    "authorization_ttl_seconds": 60,
    "safety_state_ttl_seconds": 60,
}

DEFAULT_AUTHORIZATION_CLAIMS_DIR = Path("regime_output/alpaca_equity_execution_authorization/claims")

# authorized_order_request()'s mapping of a denied .37 claim_result onto
# this module's own status vocabulary (added this milestone). Per Martin's
# .37 instruction to distinguish (1) authorization rejected BEFORE
# consumption, from (2) a replay claim rejected because already consumed,
# from (3) a claim-store outcome that is neither: a record .37 refused
# before ever touching the filesystem (invalid, tampered, or not
# AUTHORIZED) never became a claim at all, so it is reported the same way
# any other pre-consumption rejection is -- REVALIDATION_FAILED -- never
# as AUTHORIZATION_ALREADY_CONSUMED, which is reserved for cases where a
# claim genuinely already exists.
_CLAIM_REJECTED_BEFORE_CONSUMPTION_REASONS = frozenset({
    "INVALID_AUTHORIZATION_RECORD",
    "AUTHORIZATION_RECORD_TAMPERED",
    "AUTHORIZATION_NOT_VALID",
})

ROOT = Path(__file__).resolve().parent


def fail(message: str) -> None:
    raise RuntimeError(message)


# --------------------------------------------------------------------- #
# Canonical JSON / hash helpers -- redefined locally per the repo's
# established per-file convention (same as .19/.27-.32/.33/.34), not
# imported, so this module has zero import-time coupling to any file it
# must not modify.
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
# Dynamic imports of v0.5.3.33 / v0.5.3.35 -- same _load_*_module()
# pattern already established by .30/.31/.35. Neither module is modified;
# both are used strictly through their existing public functions.
# --------------------------------------------------------------------- #

def _load_canonical_spec_module():
    try:
        import aura_v05333_canonical_execution_specification as mod
        return mod
    except ImportError:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "aura_v05333_canonical_execution_specification",
            ROOT / "aura_v05333_canonical_execution_specification.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod


def _load_adapter_module():
    try:
        import aura_v05335_alpaca_equity_execution_adapter as mod
        return mod
    except ImportError:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "aura_v05335_alpaca_equity_execution_adapter",
            ROOT / "aura_v05335_alpaca_equity_execution_adapter.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod


def _load_replay_module():
    """Dynamic import of v0.5.3.37 -- same _load_*_module() pattern as
    every other cross-module reference in this file. Function-scoped and
    lazy (called only from inside authorized_order_request(), never at
    module import time), so this is NOT a circular dependency: by the time
    this is reached, .37 has already been fully loaded (and .37's own
    _load_authorization_module() call into THIS module, made earlier in
    the same call chain, has already returned). See module docstring item
    4 (added this milestone) for why .37, not claim_authorization() below,
    is now the mechanism authorized_order_request() relies on."""
    try:
        import aura_v05337_alpaca_replay_protected_consumption as mod
        return mod
    except ImportError:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "aura_v05337_alpaca_replay_protected_consumption",
            ROOT / "aura_v05337_alpaca_replay_protected_consumption.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod


# --------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------- #

def load_config(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Safe-by-default local config, mirroring .19/.31's load_config()
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
# SafetyState assembly -- deliberately narrower than .31's (see module
# docstring item 0: no reconciliation_health, no live_execution_authorized).
# --------------------------------------------------------------------- #

def _compute_replay_protection(claims_dir: Path | None) -> dict[str, Any]:
    """Genuinely probes whether the authorization claims directory is
    actually usable (creatable + writable) rather than assuming so --
    replay protection cannot be "healthy" if the claim store itself is
    unreachable. Identical technique to .31's own _compute_replay_
    protection(), against this module's own, separate claims directory."""
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
    excludes assembled_at and safety_state_hash themselves, matching .31's
    own _safety_state_content() convention exactly."""
    return {
        "schema_version": safety_state.get("schema_version"),
        "agent_version": safety_state.get("agent_version"),
        "engine": safety_state.get("engine"),
        "kill_switch": safety_state.get("kill_switch"),
        "execution_authorized": safety_state.get("execution_authorized"),
        "paper_execution_authorized": safety_state.get("paper_execution_authorized"),
        "replay_protection": safety_state.get("replay_protection"),
        "guardrails": safety_state.get("guardrails"),
    }


def assemble_safety_state(config: dict[str, Any] | None = None, claims_dir: Path | None = None,
                           now: datetime | None = None) -> dict[str, Any]:
    """Alpaca-equity-native safety-state assembler. config's kill_switch /
    *_authorized booleans are LOCAL, OPERATOR-SUPPLIED values -- recorded
    for auditability, never treated as hash-chain-verified truth on their
    own. What IS genuinely computed here, and cannot be forged by
    hand-editing a config file alone, is replay_protection (from whether
    this module's own claims directory is actually usable). See module
    docstring item 0 for why reconciliation_health is deliberately absent."""
    cfg = load_config(config)
    moment = _now(now)

    replay_protection = _compute_replay_protection(claims_dir)

    content: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "agent_version": VERSION,
        "engine": ENGINE,
        "kill_switch": bool(cfg["kill_switch"]),
        "execution_authorized": bool(cfg["execution_authorized"]),
        "paper_execution_authorized": bool(cfg["paper_execution_authorized"]),
        "replay_protection": replay_protection,
        "guardrails": {
            "single_source_of_truth": True,
            "capability_revalidated_at_authorization": True,
            "capability_revalidated_at_submission": True,
            "live_execution_modeled": False,
            "orders_allowed": False,
            "fail_closed": True,
        },
    }

    safety_state = dict(content)
    safety_state["assembled_at"] = _iso(moment)
    safety_state["safety_state_ttl_seconds"] = int(cfg["safety_state_ttl_seconds"])
    safety_state["safety_state_hash"] = _fingerprint(content)
    return safety_state


def verify_safety_state(safety_state: dict[str, Any]) -> tuple[bool, list[str]]:
    """Self-consistency check, mirroring .31's verify_safety_state()."""
    if not isinstance(safety_state, dict):
        return False, ["NOT_A_DICT"]
    expected = _fingerprint(_safety_state_content(safety_state))
    if safety_state.get("safety_state_hash") != expected:
        return False, ["SAFETY_STATE_HASH_MISMATCH"]
    return True, []


def verify_authorization_record(record: dict[str, Any]) -> tuple[bool, list[str]]:
    """Self-consistency check for an AuthorizationRecord: recomputes
    authorization_hash from the record's own content (excluding the hash
    field itself) and compares. Mirrors .31's verify_authorization_record()."""
    if not isinstance(record, dict):
        return False, ["NOT_A_DICT"]
    content = {k: v for k, v in record.items() if k != "authorization_hash"}
    expected = _fingerprint(content)
    if record.get("authorization_hash") != expected:
        return False, ["AUTHORIZATION_RECORD_HASH_MISMATCH"]
    return True, []


# --------------------------------------------------------------------- #
# Shared guardrail policy -- used by BOTH authorize() and
# revalidate_before_submission() (mirrors .31's constraint 3).
# --------------------------------------------------------------------- #

def _evaluate_guardrails(execution_spec: dict[str, Any], alpaca_asset: dict[str, Any],
                          environment: str, safety_state: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Single shared guardrail policy. revalidate_before_submission() is a
    freshness/identity/safety re-check of this SAME policy, never a
    second, divergent one.

    Deliberately never consults the spec's source-of-decision field at
    all -- see module docstring item 3: the source of a decision confers
    no execution authority on its own."""
    evaluated = {
        "spec_self_consistent": False,
        "spec_schema_supported": False,
        "venue_alpaca": False,
        "asset_class_supported": False,
        "environment_supported": False,
        "kill_switch_clear": False,
        "execution_authorized": False,
        "environment_authorized": False,
        "replay_protection_healthy": False,
        "spec_not_expired": False,
        "adapter_capability_validated": False,
    }

    def blocked(reason: str, detail: Any = None) -> dict[str, Any]:
        return {"blocked": True, "reason": reason, "detail": detail, "evaluated": evaluated,
                "validated": None, "side": None, "position_intent": None}

    if not isinstance(execution_spec, dict):
        return blocked("INVALID_EXECUTION_SPEC", "not a dict")

    canon = _load_canonical_spec_module()
    ok, errors = canon.verify_canonical_specification(execution_spec)
    if not ok:
        return blocked("MALFORMED_CANONICAL_SPEC", ",".join(errors))
    evaluated["spec_self_consistent"] = True

    if execution_spec.get("schema_version") != canon.SCHEMA_VERSION or execution_spec.get("engine") != canon.ENGINE:
        return blocked("UNSUPPORTED_SPEC_SCHEMA_OR_ENGINE",
                        f"{execution_spec.get('schema_version')}/{execution_spec.get('engine')}")
    evaluated["spec_schema_supported"] = True

    if execution_spec.get("venue") != EXPECTED_VENUE:
        return blocked("VENUE_NOT_ALPACA", str(execution_spec.get("venue")))
    evaluated["venue_alpaca"] = True

    if execution_spec.get("asset_class") not in ALLOWED_ASSET_CLASSES:
        return blocked("UNSUPPORTED_ASSET_CLASS_FOR_ALPACA_EQUITY_AUTHORIZATION",
                        str(execution_spec.get("asset_class")))
    evaluated["asset_class_supported"] = True

    if environment not in ALLOWED_ENVIRONMENTS:
        reason = "LIVE_EXECUTION_NOT_SUPPORTED_FOR_ALPACA_EQUITIES" if environment == "LIVE" else "UNSUPPORTED_ENVIRONMENT"
        return blocked(reason, str(environment))
    evaluated["environment_supported"] = True

    if safety_state.get("kill_switch") is True:
        return blocked("KILL_SWITCH_ENGAGED")
    evaluated["kill_switch_clear"] = True

    if safety_state.get("execution_authorized") is not True:
        return blocked("SAFETY_STATE_NOT_AUTHORIZED")
    evaluated["execution_authorized"] = True

    if safety_state.get("paper_execution_authorized") is not True:
        return blocked("PAPER_EXECUTION_NOT_AUTHORIZED")
    evaluated["environment_authorized"] = True

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

    # Reused, not reimplemented -- see module docstring item 2. This is
    # the ONLY place shortability/tradability/direction-capability are
    # decided; always fresh, against the alpaca_asset passed for THIS call.
    adapter35 = _load_adapter_module()
    try:
        validated = adapter35.validate_equity_spec(execution_spec, alpaca_asset)
    except Exception as exc:
        return blocked("ADAPTER_CAPABILITY_VALIDATION_FAILED", f"{type(exc).__name__}:{exc}")
    evaluated["adapter_capability_validated"] = True

    side_str, position_intent_str = adapter35.DIRECTION_TO_SIDE_POSITION_INTENT_STR[validated["direction"]]

    return {
        "blocked": False, "reason": None, "detail": None, "evaluated": evaluated,
        "validated": validated, "side": side_str, "position_intent": position_intent_str,
    }


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

def authorize(execution_spec: dict[str, Any], alpaca_asset: dict[str, Any], environment: str = "PAPER",
              safety_state: dict[str, Any] | None = None, config: dict[str, Any] | None = None,
              claims_dir: Path | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Evaluate execution_spec (the .33 canonical spec) + alpaca_asset
    (real, or faithfully mocked in tests, Alpaca asset metadata) against a
    freshly, independently assembled SafetyState and, if every guardrail
    passes, issue a fingerprint-bound, TTL-bound AuthorizationRecord.

    CRITICAL (mirrors .31's own constraint 1): a caller-supplied
    `safety_state` is NEVER trusted as authoritative on its own. This
    function always recomputes its own fresh safety_state via
    assemble_safety_state(), and:
      - if the supplied safety_state fails its own self-consistency check,
        authorization is rejected with SAFETY_STATE_FINGERPRINT_MISMATCH;
      - if the supplied safety_state is older than its own TTL relative to
        `now`, authorization is rejected with STALE_SAFETY_STATE;
      - if the supplied safety_state's content does not match what was
        just independently, genuinely recomputed, authorization is
        rejected with SAFETY_STATE_FORGED -- regardless of what the
        caller's copy claims.
    Callers do not need to pass safety_state at all; it exists only so a
    caller can assert what it believes the state to be and have that
    belief checked -- never to inject trust.

    `alpaca_asset` is likewise NEVER trusted as a fait accompli -- it is
    passed straight to .35.validate_equity_spec() inside
    _evaluate_guardrails(), which independently re-derives shortability
    and direction capability from it every single call."""
    cfg = load_config(config)
    moment = _now(now)

    fresh_safety_state = assemble_safety_state(cfg, claims_dir=claims_dir, now=moment)

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
            return _reject("SAFETY_STATE_FORGED",
                            "caller-supplied safety_state does not match independently recomputed safety_state")

    guardrail_result = _evaluate_guardrails(execution_spec, alpaca_asset, environment, fresh_safety_state, moment)
    if guardrail_result["blocked"]:
        return _reject(guardrail_result["reason"], guardrail_result.get("detail"))

    validated = guardrail_result["validated"]
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
        "authorization_id": f"ALPACA-EQUITY-AUTH-{uuid.uuid4().hex}",
        "status": "AUTHORIZED",
        "environment": environment,
        "venue": execution_spec.get("venue"),
        "asset_class": execution_spec.get("asset_class"),
        "symbol": validated["symbol"],
        "direction": validated["direction"],
        "side": guardrail_result["side"],
        "position_intent": guardrail_result["position_intent"],
        "quantity": str(validated["quantity"]),
        "order_type": execution_spec.get("order_type"),
        "limit_price": execution_spec.get("limit_price"),
        "client_order_id": execution_spec.get("client_order_id"),
        "decision_id": execution_spec.get("decision_id"),
        "strategy_id": execution_spec.get("strategy_id"),
        "strategy_version": execution_spec.get("strategy_version"),
        "source_kind": execution_spec.get("source_kind"),
        "shortability_status": validated["shortability_status"],
        "direction_capability": validated["direction_capability"],
        "spec_fingerprint": execution_spec.get("spec_fingerprint"),
        "safety_state_fingerprint": safety_state_fingerprint,
        "issued_at": _iso(issued_at),
        "expires_at": _iso(expires_at),
        "guardrails_evaluated": guardrail_result["evaluated"],
    }
    record["authorization_hash"] = _fingerprint(record)
    return record


def claim_authorization(claims_dir: Path, authorization_id: str) -> bool:
    """Atomic, single-use claim on an authorization_id. Mirrors .31's
    original claim_authorization() exactly (os.O_CREAT | os.O_EXCL, never
    released under any outcome). See module docstring item 4 for why this
    -- not a ledger-linked store -- is the correct, disclosed mechanism
    for THIS milestone. Returns True if this call granted the claim, False
    if it was already claimed."""
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
                                  alpaca_asset: dict[str, Any], environment: str = "PAPER",
                                  config: dict[str, Any] | None = None, claims_dir: Path | None = None,
                                  now: datetime | None = None) -> dict[str, Any]:
    """Final freshness/identity/safety re-check, immediately before handing
    off to .35's order-construction functions. Reuses _evaluate_guardrails()
    -- the SAME policy authorize() used -- against a FRESH
    assemble_safety_state() call AND a fresh alpaca_asset (never cached),
    so a shortability change, a trading-status change, or a safety-state
    change between authorize() and this call is caught here, not merely
    assumed unchanged.

    Also re-checks that execution_spec still fingerprints to what was
    recorded on `record` at authorize()-time -- see module docstring item 1
    for why this single check already covers every execution-critical
    field (quantity/symbol/direction/venue/order_type/asset_class)."""
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

    canon = _load_canonical_spec_module()
    ok2, spec_errors = canon.verify_canonical_specification(execution_spec)
    if not ok2:
        return _reject("MALFORMED_CANONICAL_SPEC", ",".join(spec_errors))

    current_spec_fingerprint = execution_spec.get("spec_fingerprint")
    if current_spec_fingerprint != record.get("spec_fingerprint"):
        return _reject("EXECUTION_SPEC_FINGERPRINT_MISMATCH",
                        "execution_spec has changed since authorization was issued")

    if execution_spec.get("client_order_id") != record.get("client_order_id"):
        return _reject("CLIENT_ORDER_ID_MISMATCH",
                        "execution_spec client_order_id does not match the authorized record")

    if environment != record.get("environment"):
        return _reject("ENVIRONMENT_MISMATCH",
                        f"revalidation environment {environment!r} != authorized environment {record.get('environment')!r}")

    fresh_safety_state = assemble_safety_state(cfg, claims_dir=claims_dir, now=moment)

    guardrail_result = _evaluate_guardrails(execution_spec, alpaca_asset, environment, fresh_safety_state, moment)
    if guardrail_result["blocked"]:
        return _reject(guardrail_result["reason"], guardrail_result.get("detail"))

    # Defense-in-depth (see module docstring item 1): position_intent is a
    # pure function of direction, so this is structurally unreachable given
    # the spec_fingerprint check above already passed -- kept anyway as a
    # directly-named guard, and to give Martin's "position-intent
    # modification rejection" test item its own explicit rejection reason.
    if guardrail_result["position_intent"] != record.get("position_intent"):
        return _reject("POSITION_INTENT_MISMATCH",
                        f"{guardrail_result['position_intent']!r} != {record.get('position_intent')!r}")

    return {
        "status": "REVALIDATED",
        "authorization_id": record.get("authorization_id"),
        "revalidated_at": _iso(moment),
        "validated": guardrail_result["validated"],
        "reason": None,
        "detail": None,
    }


def authorized_order_request(record: dict[str, Any], execution_spec: dict[str, Any], alpaca_asset: dict[str, Any],
                              claims_dir: Path, environment: str = "PAPER", config: dict[str, Any] | None = None,
                              now: datetime | None = None) -> dict[str, Any]:
    """The documented, tested integration point between .36 authorization
    and .35 adapter validation/construction (module docstring's opening
    diagram). Sequence:

      1. Claim authorization_id via .37's durable, ledger-linked
         consumption record (_load_replay_module().claim() -- see module
         docstring item 4, updated in the .37 milestone). If already
         claimed (or the record itself is invalid/tampered/reused with
         mismatched content), return AUTHORIZATION_ALREADY_CONSUMED --
         .35's order-construction functions are NEVER called in this case.
         claim_authorization() (this module's own provisional marker-file
         primitive) is NOT called here anymore; it remains defined below,
         unmodified, as a general-purpose primitive only.
      2. revalidate_before_submission() -- a fresh freshness/identity/
         safety re-check, the SAME policy authorize() used. If this fails,
         the authorization_id claim from step 1 remains PERMANENTLY
         consumed -- never released or retried, exactly mirroring .31's
         own authorized_submit() discipline (and .37's own module
         docstring item 2: EXECUTION_UNCERTAIN never becomes permission to
         retry).
      3. .35.build_order_request_spec() + .35.to_alpaca_order_request()
         (both unmodified, both pure/non-submitting) produce the real
         Alpaca SDK request object. This function stops here -- it NEVER
         calls .35.submit() (module docstring item 5). Actual submission
         orchestration is out of scope for .36."""
    moment = _now(now)
    authorization_id = record.get("authorization_id")

    if record.get("status") != "AUTHORIZED" or not isinstance(authorization_id, str) or not authorization_id:
        return {
            "status": "REVALIDATION_FAILED",
            "authorization_id": authorization_id,
            "reason": "AUTHORIZATION_NOT_VALID",
            "detail": f"record status is {record.get('status')!r}",
            "order_spec": None,
            "alpaca_order_request": None,
        }

    replay37 = _load_replay_module()
    claim_result = replay37.claim(record, claims_dir=claims_dir)
    if not claim_result["granted"]:
        claim_reason = claim_result.get("reason")

        if claim_reason in _CLAIM_REJECTED_BEFORE_CONSUMPTION_REASONS:
            # .37 refused the record itself (invalid / tampered / not
            # AUTHORIZED) BEFORE ever touching the claim store -- no claim
            # was created, so nothing is "consumed." Reported identically
            # to any other pre-consumption rejection.
            return {
                "status": "REVALIDATION_FAILED",
                "authorization_id": authorization_id,
                "reason": claim_reason,
                "detail": claim_result.get("detail"),
                "order_spec": None,
                "alpaca_order_request": None,
            }

        if claim_reason == "CLAIM_STORE_UNREACHABLE":
            # Infrastructure failure, not a content judgement either way --
            # fail closed without asserting "already consumed" or
            # "rejected" (Martin's .37 instruction to keep this outcome
            # distinct from both).
            return {
                "status": "CLAIM_STORE_UNREACHABLE",
                "authorization_id": authorization_id,
                "reason": claim_reason,
                "detail": claim_result.get("detail"),
                "order_spec": None,
                "alpaca_order_request": None,
            }

        # ALREADY_CLAIMED / ALREADY_CLAIMED_RECORD_UNVERIFIABLE /
        # AUTHORIZATION_ID_REUSED_WITH_MISMATCHED_CONTENT -- a claim
        # genuinely already exists for this authorization_id.
        return {
            "status": "AUTHORIZATION_ALREADY_CONSUMED",
            "authorization_id": authorization_id,
            "reason": claim_reason,
            "detail": claim_result.get("detail"),
            "existing_client_order_id": claim_result.get("existing_client_order_id"),
            "existing_claim": claim_result.get("existing_claim"),
            "order_spec": None,
            "alpaca_order_request": None,
        }

    revalidation = revalidate_before_submission(
        record, execution_spec, alpaca_asset, environment=environment, config=config,
        claims_dir=claims_dir, now=moment,
    )
    if revalidation["status"] != "REVALIDATED":
        return {
            "status": "REVALIDATION_FAILED",
            "authorization_id": authorization_id,
            "reason": revalidation.get("reason"),
            "detail": revalidation.get("detail"),
            "order_spec": None,
            "alpaca_order_request": None,
        }

    adapter35 = _load_adapter_module()
    order_spec = adapter35.build_order_request_spec(execution_spec, alpaca_asset)
    alpaca_order_request = adapter35.to_alpaca_order_request(order_spec)

    return {
        "status": "AUTHORIZED_ORDER_REQUEST_READY",
        "authorization_id": authorization_id,
        "reason": None,
        "detail": None,
        "order_spec": order_spec,
        "alpaca_order_request": alpaca_order_request,
    }
