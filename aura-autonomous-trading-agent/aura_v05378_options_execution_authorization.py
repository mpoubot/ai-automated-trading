#!/usr/bin/env python3
"""
AURA v0.5.3.78 -- Options Execution Authorization (O5, part 1 of 2)

Sits between O4's pure vertical-spread validation/construction (.376) and
a future consuming orchestrator (O9, which extends .338 -- no new module
number of its own). Mirrors the proven equity lineage
(.33 -> .34 -> .35 -> .36 -> .37) and its MEXC counterpart
(.27 -> .31 -> .32), adapted specifically where options' real constraints
differ from both. Read in full, and .337 (Alpaca Replay-Protected
Consumption) read in full, before writing this file -- used as
ARCHITECTURAL REFERENCE ONLY, never copied-and-renamed.

    O4 (.376) pure validation: validate_vertical_spec()
            |
            v
    O5a authorization                  <- THIS MODULE
            |
            v
    O5b replay-protected consumption (.379)
            |
            v
    O4 (.376) pure construction: build_mleg_order_request_spec() /
                                  to_alpaca_mleg_order_request()
            |
            v
    [future O9 orchestrator -- actual .376.submit() call, out of scope here]

============================================================================
0. The one structural difference from .336 that reshapes this module:
   there is no .33-equivalent canonical-spec layer upstream of options
============================================================================

.336 authorizes a spec that ALREADY carries a pre-computed, trusted
`spec_fingerprint` -- .33's canonical_spec_fingerprint(), computed once,
upstream, before the spec ever reaches .36. Options has no such layer:
O4's `validate_vertical_spec()` takes a raw caller-supplied dict (legs,
order_type, limit_price, qty, client_order_id, time_in_force) with no
fingerprint of its own anywhere on it.

So, UNLIKE .336, this module is itself the first and only place a stable,
whole-spec fingerprint for an options vertical spread is ever computed.
Two consequences, both disclosed rather than silently assumed:

  - The fingerprint is computed over O4's own VALIDATED/normalized output
    (`validate_vertical_spec()`'s return value), not the caller's raw
    input dict -- this avoids representation drift (e.g. a strike passed
    as `150` vs `"150.00"` vs `Decimal("150")` across two calls) by
    reusing O4's own canonicalization (via `.373.build_option_structure()`)
    rather than re-inventing a second one here.
  - `execution_spec_fingerprint` covers every field `.376`'s own
    `build_mleg_order_request_spec()` consumes: the leg structure (via
    `.373`'s own `structure_fingerprint`), both legs' directions,
    order_type, limit_price, qty, client_order_id, and time_in_force --
    mirroring .33's single-fingerprint-covers-every-execution-critical-
    field design (see .336's own docstring item 1), just computed locally
    instead of trusted from an upstream module.

============================================================================
1. What is reused from O4 (.376), never reimplemented
============================================================================

  - `validate_vertical_spec(spec)` -- the ONE place vertical-shape
    validity (same right, same expiry, different strikes, 1:1 ratio, one
    long + one short leg, no mixed open/close, LIMIT-only, positive qty)
    is decided. Called FRESH in both authorize() and
    revalidate_before_submission() -- never cached, never trusted from a
    prior call. Confirmed PURE (no network call, no live asset lookup) by
    direct reading of `.376` before writing this file -- see item 2 below
    for what that means for this module's own scope.
  - `LEG_DIRECTION_TO_SIDE_POSITION_INTENT` -- the per-leg direction ->
    (side, position_intent) wire-format mapping, used to build the
    AuthorizationRecord's own `legs` field.
  - `build_mleg_order_request_spec()` / `to_alpaca_mleg_order_request()`
    (both pure, non-submitting) -- called only from
    `authorized_order_request()`, only AFTER a claim has been won, exactly
    mirroring `.336.authorized_order_request()`'s own sequencing.

All three are imported dynamically (the repo's established
`_load_*_module()` pattern); `.376` is NOT modified by this file.

============================================================================
2. Explicit scope decision: purely structural, no live recheck
   (confirmed with Martin, 2026-10-07, before writing any code)
============================================================================

`.336` re-derives shortability/tradability from a live `alpaca_asset`
input on every call (via `.35.validate_equity_spec()`), because that
capability genuinely changes over time and a stale answer is a real risk.
Options has no equivalent live-capability function anywhere in the O1-O4
build -- O4's own `validate_vertical_spec()` is deliberately pure. O2
(`.374`) DOES have real network functions (`fetch_option_chain()` etc.)
that could technically serve a "is this contract still listed/quotable"
role, but wiring that into authorization was explicitly decided AGAINST
for this phase: this module stays purely structural, with zero network
calls anywhere in it (see `NETWORK_CAPABLE_FUNCTIONS` below -- empty,
unlike every other module in this lineage), matching O4's own discipline
exactly. A live freshness/quotability check is deferred to a later phase
if it turns out to be needed; it is not silently assumed unnecessary, it
was explicitly asked about and explicitly deferred.

TTLs (`authorization_ttl_seconds` / `safety_state_ttl_seconds`, both 60)
reuse `.336`'s own defaults as-is -- also confirmed, not guessed.

No optional live-price revalidation hook (`.336`'s `.40`-era
`price_fetch_fn` extension) -- also confirmed deferred; O4's spec has no
`reference_price` field yet for such a hook to check against.

============================================================================
3. Deliberate simplification vs. `.336`: no provisional claim primitive
============================================================================

`.336` still carries a provisional, marker-file-only `claim_authorization()`
function, kept only because it predates `.337` historically (the .37
milestone superseded it, but left it in place, unmodified, as a legacy
general-purpose primitive). This module and `.379` are being designed and
built TOGETHER, from scratch, in the same session -- there is no
predecessor to preserve. `authorized_order_request()` below claims
through `.379.claim()` directly, as the one and only consumption
mechanism from day one. No provisional primitive, no legacy code path, no
"supersedes" note required.

============================================================================
4. Source of a decision confers no execution authority
============================================================================

`_evaluate_guardrails()` never reads any decision/strategy provenance
field from the spec. Optional `decision_id`/`strategy_id`/
`strategy_version`/`source_kind` passthrough fields (accepted on the
input spec, bound into the AuthorizationRecord for audit purposes only,
never read by O4's own `validate_vertical_spec()` either) flow through
identically regardless of value -- mirroring `.336`'s item 3 exactly.

============================================================================
5. What this module deliberately does NOT do
============================================================================

No live chain/quote recheck (item 2). No audit-trail integration with
`.340`'s unified execution audit trail -- `.340`'s `record_authorized()`/
`record_revalidated()`/`record_consumed()` functions were built and
proven against the equity/MEXC shape; wiring options into the SAME shared
audit trail without first confirming it is genuinely venue-agnostic is a
real risk of silently corrupting or conflating that trail, not something
to guess at under this module's own time budget. Deliberately deferred,
not silently skipped -- a disclosed decision for a later phase (most
naturally O9, alongside the rest of the orchestration wiring). No
Supervisor/orchestrator (O9's job). No live execution -- mirrors O4's own
single-member `ALLOWED_ENVIRONMENTS`. No modification to `.373`/`.374`/
`.375`/`.376`/`.377` -- all read-only inputs, `.376` dynamically imported
and called through its existing public functions only. No actual Alpaca
order submission anywhere in this file or its tests --
`authorized_order_request()` stops at `.376.to_alpaca_mleg_order_request()`
(a pure, non-submitting SDK object construction call), never at
`.376.submit()`.

No real credentials, no network call, anywhere in this file.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

VERSION = "AURA v0.5.3.78"
ENGINE = "OPTIONS_EXECUTION_AUTHORIZATION"
SCHEMA_VERSION = "1.0"

EXPECTED_VENUE = "ALPACA"
EXPECTED_ASSET_CLASS = "OPTION"
EXPECTED_ORDER_CLASS = "MLEG"  # v1 scope: O4 only supports 2-leg verticals

# LIVE is deliberately NOT a member -- mirrors .336 item 0 and O4's own
# single-member ALLOWED_ENVIRONMENTS discipline exactly.
ALLOWED_ENVIRONMENTS = frozenset({"PAPER"})

# Safe-by-default, local, operator-supplied -- mirrors .336's
# DEFAULT_AUTH_CONFIG precedent exactly (same key names, same semantics).
DEFAULT_AUTH_CONFIG: dict[str, Any] = {
    "kill_switch": True,
    "execution_authorized": False,
    "paper_execution_authorized": False,
    "authorization_ttl_seconds": 60,
    "safety_state_ttl_seconds": 60,
}

DEFAULT_AUTHORIZATION_CLAIMS_DIR = Path("regime_output/options_execution_authorization/claims")

# This module has ZERO network-capable functions -- a strictly stronger
# property than every other module in this lineage (.27/.31/.35/.36 all
# have at least one). Kept as an explicit, empty, disclosed constant
# rather than simply omitted, so a structural test can assert it directly
# rather than relying on its absence being meaningful.
NETWORK_CAPABLE_FUNCTIONS: frozenset[str] = frozenset()

ROOT = Path(__file__).resolve().parent


def fail(message: str) -> None:
    raise RuntimeError(message)


# --------------------------------------------------------------------- #
# Canonical JSON / hash helpers -- redefined locally per the repo's
# established per-file convention, not imported, so this module has zero
# import-time coupling to any file it must not modify.
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
# Dynamic import of v0.5.3.76 -- same _load_*_module() pattern already
# established by .30/.31/.35/.36/.376. Not modified; used strictly
# through its existing public functions.
# --------------------------------------------------------------------- #

def _load_adapter_module():
    try:
        import aura_v05376_options_execution_adapter as mod
        return mod
    except ImportError:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "aura_v05376_options_execution_adapter",
            ROOT / "aura_v05376_options_execution_adapter.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod


def _load_replay_module():
    """Dynamic import of v0.5.3.79 -- the ONE consumption mechanism this
    module relies on (item 3 above: no provisional primitive). Function-
    scoped and lazy (called only from inside authorized_order_request(),
    never at module import time) -- not a circular dependency, mirroring
    .336's own identical note about its .337 import."""
    try:
        import aura_v05379_options_replay_protected_consumption as mod
        return mod
    except ImportError:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "aura_v05379_options_replay_protected_consumption",
            ROOT / "aura_v05379_options_replay_protected_consumption.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod


# --------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------- #

def load_config(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Safe-by-default local config, mirroring .336's load_config()
    precedent exactly. Only known keys may override the safe-closed
    defaults (kill_switch True, every *_authorized False); unknown keys
    in `overrides` are ignored rather than silently accepted."""
    config = dict(DEFAULT_AUTH_CONFIG)
    if overrides:
        for key in DEFAULT_AUTH_CONFIG:
            if key in overrides:
                config[key] = overrides[key]
    return config


# --------------------------------------------------------------------- #
# SafetyState assembly -- mirrors .336's assemble_safety_state() shape
# exactly (no reconciliation_health here either -- O4B (.377) is a
# position-level, post-fill reconciliation tool, not a pre-trade
# authorization-health source; wiring it in here would be the same kind
# of provenance forgery risk .336 itself declines for the identical
# reason).
# --------------------------------------------------------------------- #

def _compute_replay_protection(claims_dir: Path | None) -> dict[str, Any]:
    """Genuinely probes whether the authorization claims directory is
    actually usable (creatable + writable) rather than assuming so.
    Identical technique to .336's own _compute_replay_protection(),
    against this module's own, separate claims directory."""
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
    excludes assembled_at and safety_state_hash themselves, matching
    .336's own _safety_state_content() convention exactly."""
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
    """Options-native safety-state assembler. config's kill_switch /
    *_authorized booleans are LOCAL, OPERATOR-SUPPLIED values -- recorded
    for auditability, never treated as hash-chain-verified truth on their
    own. What IS genuinely computed here, and cannot be forged by
    hand-editing a config file alone, is replay_protection (from whether
    this module's own claims directory is actually usable)."""
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
            "structure_revalidated_at_authorization": True,
            "structure_revalidated_at_submission": True,
            "live_recheck_modeled": False,  # item 2 -- deliberately absent
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
    """Self-consistency check, mirroring .336's verify_safety_state()."""
    if not isinstance(safety_state, dict):
        return False, ["NOT_A_DICT"]
    expected = _fingerprint(_safety_state_content(safety_state))
    if safety_state.get("safety_state_hash") != expected:
        return False, ["SAFETY_STATE_HASH_MISMATCH"]
    return True, []


def verify_authorization_record(record: dict[str, Any]) -> tuple[bool, list[str]]:
    """Self-consistency check for an AuthorizationRecord: recomputes
    authorization_hash from the record's own content (excluding the hash
    field itself) and compares. Mirrors .336's verify_authorization_record()."""
    if not isinstance(record, dict):
        return False, ["NOT_A_DICT"]
    content = {k: v for k, v in record.items() if k != "authorization_hash"}
    expected = _fingerprint(content)
    if record.get("authorization_hash") != expected:
        return False, ["AUTHORIZATION_RECORD_HASH_MISMATCH"]
    return True, []


# --------------------------------------------------------------------- #
# The locally-computed whole-spec fingerprint -- see module docstring
# item 0 for why this exists here (and only here) rather than being
# trusted from an upstream canonical-spec module.
# --------------------------------------------------------------------- #

def _execution_spec_fingerprint(validated: dict[str, Any]) -> str:
    """Computed over O4's own VALIDATED/normalized output -- never the
    caller's raw input dict (see item 0: avoids representation drift by
    reusing O4's own canonicalization instead of inventing a second one).
    Covers every field `.376.build_mleg_order_request_spec()` consumes:
    the leg structure (via `.373`'s own structure_fingerprint, which
    already covers both legs' full contract identity + side + ratio),
    both legs' directions (open/close, long/short), order_type,
    limit_price, qty, client_order_id, and time_in_force. Any change to
    any of these between authorize() and revalidate_before_submission()
    changes this ONE fingerprint -- mirroring .33's single-fingerprint-
    covers-everything design (.336 docstring item 1), just computed here
    instead of trusted from upstream."""
    content = {
        "structure_fingerprint": validated["structure"]["structure_fingerprint"],
        "directions": validated["directions"],
        "order_type": validated["order_type"],
        "limit_price": str(validated["limit_price"]),
        "qty": validated["qty"],
        "client_order_id": validated["client_order_id"],
        "time_in_force": validated["time_in_force"],
    }
    return _fingerprint(content)


# --------------------------------------------------------------------- #
# Shared guardrail policy -- used by BOTH authorize() and
# revalidate_before_submission() (mirrors .336's identical constraint).
# --------------------------------------------------------------------- #

def _evaluate_guardrails(spec: dict[str, Any], environment: str, safety_state: dict[str, Any],
                          now: datetime) -> dict[str, Any]:
    """Single shared guardrail policy. revalidate_before_submission() is a
    freshness/identity/safety re-check of this SAME policy, never a
    second, divergent one.

    Deliberately never consults any decision/strategy provenance field at
    all -- see module docstring item 4."""
    evaluated = {
        "spec_is_dict": False,
        "environment_supported": False,
        "kill_switch_clear": False,
        "execution_authorized": False,
        "environment_authorized": False,
        "replay_protection_healthy": False,
        "spec_not_expired": False,
        "structure_validated": False,
    }

    def blocked(reason: str, detail: Any = None) -> dict[str, Any]:
        return {"blocked": True, "reason": reason, "detail": detail, "evaluated": evaluated,
                "validated": None, "legs_out": None, "fingerprint": None}

    if not isinstance(spec, dict):
        return blocked("INVALID_EXECUTION_SPEC", "not a dict")
    evaluated["spec_is_dict"] = True

    if environment not in ALLOWED_ENVIRONMENTS:
        reason = "LIVE_EXECUTION_NOT_SUPPORTED_FOR_OPTIONS" if environment == "LIVE" else "UNSUPPORTED_ENVIRONMENT"
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

    # Optional, opt-in field -- O4's own spec shape has no expires_at, but
    # a caller (a future orchestrator) may still supply one, mirroring
    # .33's optional expires_at exactly.
    raw_expiry = spec.get("expires_at")
    if raw_expiry is not None:
        spec_expiry = _parse_iso(raw_expiry)
        if spec_expiry is None:
            return blocked("INVALID_EXPIRES_AT", str(raw_expiry))
        if spec_expiry <= now:
            return blocked("EXPIRED_EXECUTION_SPEC", _iso(spec_expiry))
    evaluated["spec_not_expired"] = True

    # Reused, not reimplemented -- see module docstring item 1. The ONLY
    # place vertical-shape validity is decided; always fresh, never
    # cached. Purely structural (item 2) -- no live data passed in or
    # consulted.
    adapter = _load_adapter_module()
    try:
        validated = adapter.validate_vertical_spec(spec)
    except RuntimeError as exc:
        return blocked("STRUCTURE_VALIDATION_FAILED", str(exc))
    evaluated["structure_validated"] = True

    legs_out = []
    for leg, direction in zip(validated["structure"]["legs"], validated["directions"]):
        side_str, position_intent_str = adapter.LEG_DIRECTION_TO_SIDE_POSITION_INTENT[direction]
        legs_out.append({
            "occ_symbol": leg["contract"]["occ_symbol"],
            "ratio_qty": leg["ratio"],
            "side": side_str,
            "position_intent": position_intent_str,
            "direction": direction,
            "contract_fingerprint": leg["contract"]["contract_fingerprint"],
        })

    fingerprint = _execution_spec_fingerprint(validated)

    return {
        "blocked": False, "reason": None, "detail": None, "evaluated": evaluated,
        "validated": validated, "legs_out": legs_out, "fingerprint": fingerprint,
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

def authorize(spec: dict[str, Any], environment: str = "PAPER", safety_state: dict[str, Any] | None = None,
              config: dict[str, Any] | None = None, claims_dir: Path | None = None,
              now: datetime | None = None) -> dict[str, Any]:
    """Evaluate `spec` (O4's raw vertical-spread input shape: legs,
    order_type, limit_price, qty, client_order_id, time_in_force, plus
    optional expires_at/decision_id/strategy_id/strategy_version/
    source_kind) against a freshly, independently assembled SafetyState
    and, if every guardrail passes, issue a fingerprint-bound, TTL-bound
    AuthorizationRecord.

    CRITICAL (mirrors .336's own constraint): a caller-supplied
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
    belief checked -- never to inject trust."""
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

    guardrail_result = _evaluate_guardrails(spec, environment, fresh_safety_state, moment)
    if guardrail_result["blocked"]:
        return _reject(guardrail_result["reason"], guardrail_result.get("detail"))

    validated = guardrail_result["validated"]
    legs_out = guardrail_result["legs_out"]
    safety_state_fingerprint = _fingerprint(_safety_state_content(fresh_safety_state))

    issued_at = moment
    ttl_seconds = min(int(cfg["authorization_ttl_seconds"]), int(fresh_safety_state["safety_state_ttl_seconds"]))
    expires_at = issued_at + timedelta(seconds=ttl_seconds)

    # Weakest-link TTL: also bounded by the spec's own expires_at, if
    # present (already confirmed not yet expired above).
    spec_expiry = _parse_iso(spec.get("expires_at"))
    if spec_expiry is not None and spec_expiry < expires_at:
        expires_at = spec_expiry

    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "agent_version": VERSION,
        "engine": ENGINE,
        "authorization_id": f"OPTIONS-VERTICAL-AUTH-{uuid.uuid4().hex}",
        "status": "AUTHORIZED",
        "environment": environment,
        "venue": EXPECTED_VENUE,
        "asset_class": EXPECTED_ASSET_CLASS,
        "order_class": EXPECTED_ORDER_CLASS,
        "underlying_symbol": validated["structure"]["underlying_symbol"],
        "legs": legs_out,
        "qty": str(validated["qty"]),
        "order_type": validated["order_type"],
        "limit_price": str(validated["limit_price"]),
        "time_in_force": validated["time_in_force"],
        "client_order_id": validated["client_order_id"],
        "decision_id": spec.get("decision_id"),
        "strategy_id": spec.get("strategy_id"),
        "strategy_version": spec.get("strategy_version"),
        "source_kind": spec.get("source_kind"),
        "execution_spec_fingerprint": guardrail_result["fingerprint"],
        "safety_state_fingerprint": safety_state_fingerprint,
        "issued_at": _iso(issued_at),
        "expires_at": _iso(expires_at),
        "guardrails_evaluated": guardrail_result["evaluated"],
    }
    record["authorization_hash"] = _fingerprint(record)
    return record


def revalidate_before_submission(record: dict[str, Any], spec: dict[str, Any], environment: str = "PAPER",
                                  config: dict[str, Any] | None = None, claims_dir: Path | None = None,
                                  now: datetime | None = None) -> dict[str, Any]:
    """Final freshness/identity/safety re-check, immediately before
    handing off to O4's order-construction functions. Reuses
    _evaluate_guardrails() -- the SAME policy authorize() used -- against
    a FRESH assemble_safety_state() call AND a fresh
    adapter.validate_vertical_spec() call (never cached), so a safety-
    state change between authorize() and this call is caught here, not
    merely assumed unchanged.

    Also re-checks that `spec` still fingerprints to what was recorded on
    `record` at authorize()-time -- see module docstring item 0 for why
    this single check already covers every execution-critical field
    (leg structure/directions/order_type/limit_price/qty/client_order_id/
    time_in_force)."""
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

    if environment != record.get("environment"):
        return _reject("ENVIRONMENT_MISMATCH",
                        f"revalidation environment {environment!r} != authorized environment {record.get('environment')!r}")

    fresh_safety_state = assemble_safety_state(cfg, claims_dir=claims_dir, now=moment)

    guardrail_result = _evaluate_guardrails(spec, environment, fresh_safety_state, moment)
    if guardrail_result["blocked"]:
        return _reject(guardrail_result["reason"], guardrail_result.get("detail"))

    if guardrail_result["fingerprint"] != record.get("execution_spec_fingerprint"):
        return _reject("EXECUTION_SPEC_FINGERPRINT_MISMATCH",
                        "spec has changed since authorization was issued")

    if guardrail_result["validated"]["client_order_id"] != record.get("client_order_id"):
        return _reject("CLIENT_ORDER_ID_MISMATCH",
                        "spec client_order_id does not match the authorized record")

    # Defense-in-depth (see module docstring item 0): leg directions are
    # already covered by the fingerprint check above, so this is
    # structurally unreachable given that check already passed -- kept
    # anyway as a directly-named guard with its own rejection reason,
    # mirroring .336's identical POSITION_INTENT_MISMATCH defense-in-
    # depth check.
    if guardrail_result["legs_out"] != record.get("legs"):
        return _reject("LEG_DIRECTION_MISMATCH",
                        "leg side/position_intent no longer matches the authorized record")

    return {
        "status": "REVALIDATED",
        "authorization_id": record.get("authorization_id"),
        "revalidated_at": _iso(moment),
        "validated": guardrail_result["validated"],
        "reason": None,
        "detail": None,
    }


def authorized_order_request(record: dict[str, Any], spec: dict[str, Any], claims_dir: Path,
                              environment: str = "PAPER", config: dict[str, Any] | None = None,
                              now: datetime | None = None) -> dict[str, Any]:
    """The documented, tested integration point between O5 authorization
    and O4 adapter construction (module docstring's opening diagram).

    Sequence (mirrors .336.authorized_order_request()'s v0.5.3.40
    reordering exactly -- revalidate BEFORE claim, so a spec that is
    already stale never burns a .379 claim slot):
      1. revalidate_before_submission() -- a fresh freshness/identity/
         safety re-check. If this fails, NOTHING has been claimed.
      2. Claim authorization_id via .379's durable, ledger-linked
         consumption record (_load_replay_module().claim() -- item 3: the
         ONE consumption mechanism, no provisional primitive). If already
         claimed (or the record itself is invalid/tampered/reused with
         mismatched content), return AUTHORIZATION_ALREADY_CONSUMED --
         O4's order-construction functions are NEVER called in this case.
         If a claim IS won after this point, it remains PERMANENTLY
         consumed regardless of anything that happens later -- never
         released or retried.
      3. `.376.build_mleg_order_request_spec()` + `.376.to_alpaca_mleg_order_request()`
         (both unmodified, both pure/non-submitting) produce the real
         Alpaca SDK request object. This function stops here -- it NEVER
         calls `.376.submit()` (module docstring item 5)."""
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

    revalidation = revalidate_before_submission(
        record, spec, environment=environment, config=config, claims_dir=claims_dir, now=moment,
    )
    if revalidation["status"] != "REVALIDATED":
        # Nothing claimed yet -- no CONSUMED event, consumption was never
        # attempted.
        return {
            "status": "REVALIDATION_FAILED",
            "authorization_id": authorization_id,
            "reason": revalidation.get("reason"),
            "detail": revalidation.get("detail"),
            "order_spec": None,
            "alpaca_order_request": None,
        }

    replay79 = _load_replay_module()
    claim_result = replay79.claim(record, claims_dir=claims_dir)
    if not claim_result["granted"]:
        claim_reason = claim_result.get("reason")

        if claim_reason in ("INVALID_AUTHORIZATION_RECORD", "AUTHORIZATION_RECORD_TAMPERED",
                             "AUTHORIZATION_NOT_VALID"):
            # .379 refused the record itself BEFORE ever touching the
            # claim store -- no claim was created, so nothing is
            # "consumed." Reported identically to any other
            # pre-consumption rejection.
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
            # "rejected."
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

    adapter = _load_adapter_module()
    order_spec = adapter.build_mleg_order_request_spec(spec)
    alpaca_order_request = adapter.to_alpaca_mleg_order_request(order_spec)

    return {
        "status": "AUTHORIZED_ORDER_REQUEST_READY",
        "authorization_id": authorization_id,
        "reason": None,
        "detail": None,
        "order_spec": order_spec,
        "alpaca_order_request": alpaca_order_request,
    }
