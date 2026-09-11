#!/usr/bin/env python3
"""
AURA v0.5.3.37 -- Alpaca Replay-Protected Consumption

Milestone 4 of the approved Alpaca-equity execution floor
(.34 -> .35 -> .36 -> .37 -> .38, per Martin's 2026-09-11 GO messages).
Supersedes .36's provisional, marker-file-only claim_authorization() with
a durable, atomic, integrity-verified authorization_id -> execution-identity
consumption record -- architecturally equivalent in strength to the MEXC
v0.5.3.32 mechanism, per explicit instruction to inspect .32 in full and
adapt its proven principles rather than build a second, competing
definition of "consumed."

    Canonical Execution Spec (.33)
            |
            v
    Asset Metadata (.34)
            |
            v
    Alpaca Adapter (.35)
            |
            v
    Authorization (.36)
            |
            v
    Replay-Protected Consumption (.37)    <- THIS MODULE
            |
            v
    [future Supervisor .38]
            |
            v
    Alpaca submission
            |
            v
    future observation/reconciliation

============================================================================
0. .32 inspected in full before writing this file -- what is reused
   as-is, and what is deliberately adapted
============================================================================

Reused AS-IS (proven, venue-neutral principles):
  - Atomic uniqueness: os.O_CREAT | os.O_EXCL on a per-key file. This is
    the ONLY thing that decides who wins a race -- never check-then-write.
  - Never released: no delete/release/unclaim function exists in this
    module, structurally, matching .32's own "no release function, by
    design" (checked directly by a dedicated test, mirroring .32's own
    test_module_exposes_no_release_or_delete_function()).
  - Crash/restart durability: the claim is a plain file on disk; a fresh
    process (fresh module load, in this test harness) sees the exact same
    claim, with no in-memory state to lose.
  - The race-then-read-back pattern for reporting who won: the winner's
    os.open() call creates an (initially empty) file an instant before it
    writes the JSON body, so a loser reading back "who won" retries for a
    bounded window rather than treating a transient empty/unparsable read
    as "no existing claim."
  - The DENIED/UNREACHABLE distinction: "already claimed" (an exclusivity
    loss) and "claim store unreachable" (an OSError) are reported as
    distinct, named reasons -- never conflated.

Adapted (per explicit instruction: determine what actually differs
before copying):
  - WHAT gets claimed. .32.claim() accepts loose, caller-trusted kwargs
    (authorization_id, client_order_id, spec_fingerprint,
    safety_state_fingerprint) -- .31 is trusted to have already verified
    its own AuthorizationRecord before extracting those fields. This
    module's claim() instead accepts the FULL .36 AuthorizationRecord and
    independently re-verifies it (via .36.verify_authorization_record(),
    dynamically imported, unmodified) and its status (AUTHORIZED) BEFORE
    ever touching the filesystem. This is a genuine integrity improvement
    over .32's pattern, not novelty for its own sake: without it, a caller
    could hand this module a hand-edited or malformed record and it would
    blindly persist a claim built from forged data -- exactly the failure
    mode this entire module lineage exists to prevent. The cost is a real,
    disclosed .37 -> .36 dependency (function-scoped, lazy-imported,
    non-circular -- by the time authorized_order_request() below calls
    into .37, .36 is already fully loaded, so this never faces an actual
    import cycle) that .32 deliberately avoided by trusting its caller.
    That tradeoff is made deliberately here, not by accident.
  - WHAT is bound into the persisted record. .32's canonical_claim_hash()
    covers authorization_id/client_order_id/spec_fingerprint/
    safety_state_fingerprint/claimed_at -- MEXC has no "environment"
    concept and no position_intent (its side/reduce_only pairing already
    fully encodes open/close direction at the wire level). Per explicit
    instruction, this module's record additionally binds venue,
    environment, asset_class, symbol, direction, position_intent, and the
    AuthorizationRecord's own authorization_hash -- every field is copied
    directly from the already-verified .36 record, never re-derived or
    guessed, so "only include fields justified by the existing contracts"
    is satisfied structurally, not by editorial judgment.
  - Persisted-record integrity on READ, not just on write. .32's
    get_claim() loads and returns a record with no re-verification --a
    tampered claim file would be trusted as-is by any caller. This
    module's get_claim() re-verifies via verify_claim() on every read and
    FAILS CLOSED (raises) if the content is unparsable or its claim_hash
    does not match -- a corrupted or tampered persisted record is never
    silently treated as "no claim" (which would wrongly appear to make
    the authorization_id available again) and never silently trusted
    as-is either.
  - Authorization-identity confusion detection. Because this module binds
    the FULL execution identity (not just client_order_id) into the
    claim, a second claim() attempt presenting the SAME authorization_id
    but a DIFFERENT (self-consistent, differently-hashed) record -- e.g.
    a forged record that reuses a real authorization_id with a different
    venue/symbol/direction/etc -- is detected and reported as
    AUTHORIZATION_ID_REUSED_WITH_MISMATCHED_CONTENT, a distinct, more
    severe outcome than an ordinary ALREADY_CLAIMED duplicate. .32 has no
    equivalent check (it never compares the new attempt's content against
    the existing claim's content at all).

============================================================================
1. What durable identity is claimed, and why no new identity is invented
============================================================================

MEXC links authorization_id -> client_order_id (spec Sec.3, .32's own
docstring). For Alpaca, client_order_id is likewise the correct, already-
canonical identity: it exists on the .33 canonical spec, is carried
unchanged through .34/.35/.36, and IS what Alpaca's own API keys order
idempotency on (.35.submit()'s get_order_by_client_id() preflight). No new
identity is invented -- this module claims authorization_id and records
client_order_id (plus the rest of the execution identity Martin's
instruction lists), exactly mirroring .32's own choice, just with a
richer, self-sufficient record.

DISCLOSED ASYMMETRY WITH MEXC, worth stating plainly rather than glossing
over: MEXC has TWO independent, local, atomic, offline claim layers --
.32 (authorization_id) and .27's own claim_client_order_id()
(client_order_id) -- providing defense-in-depth entirely without a
network call. Alpaca's client_order_id-level duplicate defense is NOT
local -- .35.submit()'s only duplicate guard at that layer is a live
Alpaca API call (get_order_by_client_id()), inherited unchanged from .22's
own design and NOT something this milestone was asked to add a local
claim file for. This module closes the authorization_id layer to the same
strength as MEXC's; the client_order_id layer for Alpaca remains
live-API-dependent, a pre-existing, disclosed property of the .22/.35
lineage, not a new gap introduced here.

============================================================================
2. EXECUTION_UNCERTAIN never becomes permission to retry
============================================================================

This module has no concept of a downstream submission outcome at all --
it only ever answers "has this authorization_id been consumed." Once
claim() grants, there is no code path anywhere in this file that can
un-consume it, regardless of what a caller later learns about the
downstream order's status. A dedicated test
(test_uncertain_downstream_outcome_does_not_release_the_claim) proves this
by simulating an "uncertain" outcome and confirming a second claim
attempt on the same authorization_id is still refused, identically to any
other duplicate.

============================================================================
3. Relationship with .36 (why .36 is modified in this milestone)
============================================================================

.36's own docstring already discloses claim_authorization() as
PROVISIONAL, "expected to be superseded by .37 exactly as .32 superseded
.31's original claim primitive." This mirrors .32's own precedent exactly
(.32's docstring: "v0.5.3.31 ... is updated in this same milestone to
route authorized_submit()'s actual replay-protection decision through
this module instead of its own prior bare marker-file claim
(claim_authorization() itself is left in place, unmodified ...)").

Concretely: .36.authorized_order_request() is updated in THIS milestone
to claim through THIS module (.37.claim()) instead of its own
claim_authorization(). claim_authorization() itself is left in place in
.36, completely unmodified, still tested, still a valid general-purpose
atomic-claim primitive -- it is simply no longer the mechanism the actual
production path (authorized_order_request()) relies on for the
authorization_id claim decision. This is the ONLY change made to .36 in
this milestone (a ~6-line internal swap inside authorized_order_request()
plus a widened docstring note); no other function, field, or behavior of
.36 is touched. This keeps exactly ONE authoritative definition of
"consumed" in the real execution path -- there is no scenario where both
.36's marker file AND .37's durable record are consulted, or could
disagree, in production use of authorized_order_request(). See the
implementation report for the explicit before/after diff and rationale,
per instruction to explain exactly why before making this change.

============================================================================
4. What this module deliberately does NOT do
============================================================================

No new intent ledger (explicit instruction: ".37 must NOT become a second
intent ledger"). This module stores exactly one thing: an
authorization_id -> execution-identity consumption LINK, never lifecycle
STATE (fills, rejections, reconciliation outcome) -- there is no
equivalent Alpaca intent ledger to be the source of truth for that, and
building one is out of scope here, exactly as .32 never became a second
copy of .29. No modification to .31, .32, MEXC, .27, .28, .29, .30, .33,
.34, .35 -- all untouched (verified via git diff). No live execution. No
Supervisor/orchestrator. No actual Alpaca order submission anywhere in
this file or its tests.

No real credentials, no network call, anywhere in this file.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VERSION = "AURA v0.5.3.37"
ENGINE = "ALPACA_EQUITY_REPLAY_PROTECTED_CONSUMPTION"
SCHEMA_VERSION = "1.0"

ROOT = Path(__file__).resolve().parent

DEFAULT_CLAIMS_DIR = Path(
    os.environ.get(
        "AURA_ALPACA_EQUITY_REPLAY_PROTECTION_DIR",
        "regime_output/alpaca_equity_replay_protected_consumption/claims",
    )
)

# Matches .36's authorization_id shape (f"ALPACA-EQUITY-AUTH-{uuid4().hex}",
# 51 chars) -- same bound as .32's own AUTHORIZATION_ID_RE, kept identical
# for consistency and because it is permissive enough to cover both without
# assuming either module's exact generator.
AUTHORIZATION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

DEFAULT_RETRY_DEADLINE_SECONDS = 1.0


def fail(message: str) -> None:
    raise RuntimeError(message)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------- #
# Dynamic import of v0.5.3.36 -- same _load_*_module() pattern already
# established by .30/.31/.35/.36. Used only to call
# verify_authorization_record() (unmodified, read-only) against a record
# THIS module was given -- never to construct or authorize anything itself.
# --------------------------------------------------------------------- #

def _load_authorization_module():
    try:
        import aura_v05336_alpaca_equity_execution_authorization as mod
        return mod
    except ImportError:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "aura_v05336_alpaca_equity_execution_authorization",
            ROOT / "aura_v05336_alpaca_equity_execution_authorization.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod


def claim_path(authorization_id: str, claims_dir: Path | None = None) -> Path:
    if not isinstance(authorization_id, str) or not AUTHORIZATION_ID_RE.match(authorization_id):
        fail(f"INVALID_AUTHORIZATION_ID:{authorization_id!r}")
    return (claims_dir or DEFAULT_CLAIMS_DIR) / f"{authorization_id}.json"


# --------------------------------------------------------------------- #
# Persisted claim record -- content, hash, self-verification
# --------------------------------------------------------------------- #

def canonical_claim_hash(record: dict[str, Any]) -> str:
    canonical = {
        "schema_version": record.get("schema_version"),
        "engine": record.get("engine"),
        "agent_version": record.get("agent_version"),
        "authorization_id": record.get("authorization_id"),
        "authorization_hash": record.get("authorization_hash"),
        "client_order_id": record.get("client_order_id"),
        "venue": record.get("venue"),
        "environment": record.get("environment"),
        "asset_class": record.get("asset_class"),
        "symbol": record.get("symbol"),
        "direction": record.get("direction"),
        "position_intent": record.get("position_intent"),
        "spec_fingerprint": record.get("spec_fingerprint"),
        "safety_state_fingerprint": record.get("safety_state_fingerprint"),
        "claimed_at": record.get("claimed_at"),
    }
    return sha256_text(stable_json(canonical))


def verify_claim(record: dict[str, Any]) -> tuple[bool, list[str]]:
    """Self-consistency check, mirroring .29/.31/.32/.33/.34/.36's own
    verify_*() convention: recomputes claim_hash from the record's own
    content and compares."""
    if not isinstance(record, dict):
        return False, ["NOT_A_DICT"]
    expected = canonical_claim_hash(record)
    if record.get("claim_hash") != expected:
        return False, ["CLAIM_HASH_MISMATCH"]
    return True, []


def get_claim(authorization_id: str, claims_dir: Path | None = None) -> dict[str, Any] | None:
    """Loads and returns the persisted claim record for authorization_id,
    or None if no claim file exists at all. UNLIKE .32's get_claim(), this
    ALWAYS self-verifies the record it reads (module docstring item 0) and
    FAILS CLOSED (raises RuntimeError) if the file exists but its content
    is unparsable, not an object, or fails verify_claim() -- a corrupted
    or tampered persisted record is never silently treated as "no claim"
    (which would wrongly make the authorization_id look available again)
    and never silently trusted as-is."""
    path = claim_path(authorization_id, claims_dir)
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as f:
        raw = f.read()
    try:
        record = json.loads(raw) if raw.strip() else None
    except json.JSONDecodeError as exc:
        fail(f"CLAIM_RECORD_UNPARSEABLE:{authorization_id}:{exc}")
    if record is None:
        fail(f"CLAIM_RECORD_EMPTY:{authorization_id}")
    if not isinstance(record, dict):
        fail(f"CLAIM_RECORD_NOT_OBJECT:{authorization_id}")
    ok, errors = verify_claim(record)
    if not ok:
        fail(f"CLAIM_RECORD_TAMPERED:{authorization_id}:{','.join(errors)}")
    return record


def _build_claim_record(authorization_record: dict[str, Any], claimed_at: str) -> dict[str, Any]:
    """Builds this module's persisted claim record content FROM an
    already-verified .36 AuthorizationRecord -- every field is copied
    directly, never re-derived or guessed (module docstring item 0:
    "only include fields justified by the existing contracts")."""
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "engine": ENGINE,
        "agent_version": VERSION,
        "authorization_id": authorization_record.get("authorization_id"),
        "authorization_hash": authorization_record.get("authorization_hash"),
        "client_order_id": authorization_record.get("client_order_id"),
        "venue": authorization_record.get("venue"),
        "environment": authorization_record.get("environment"),
        "asset_class": authorization_record.get("asset_class"),
        "symbol": authorization_record.get("symbol"),
        "direction": authorization_record.get("direction"),
        "position_intent": authorization_record.get("position_intent"),
        "spec_fingerprint": authorization_record.get("spec_fingerprint"),
        "safety_state_fingerprint": authorization_record.get("safety_state_fingerprint"),
        "claimed_at": claimed_at,
        "claim_hash": None,
    }
    record["claim_hash"] = canonical_claim_hash(record)
    return record


def _denied(authorization_id: Any, reason: str, detail: Any = None,
            existing_claim: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "granted": False,
        "authorization_id": authorization_id,
        "client_order_id": None,
        "reason": reason,
        "detail": detail,
        "existing_client_order_id": existing_claim.get("client_order_id") if existing_claim else None,
        "existing_claim": existing_claim,
    }


# --------------------------------------------------------------------- #
# The one authoritative claim operation
# --------------------------------------------------------------------- #

def claim(authorization_record: dict[str, Any], claims_dir: Path | None = None,
          retry_deadline_seconds: float = DEFAULT_RETRY_DEADLINE_SECONDS) -> dict[str, Any]:
    """ReplayProtection.claim() for the Alpaca-equity chain -- the ONE
    authoritative consumption decision for an authorization_id, replacing
    .36's provisional claim_authorization() in the real execution path
    (module docstring item 3).

    UNLIKE .32.claim() (which trusts its caller's loose kwargs), this
    function takes the FULL .36 AuthorizationRecord and independently
    re-verifies it (self-consistency + status == "AUTHORIZED") before
    ever touching the filesystem -- a malformed or tampered record is
    refused before any claim attempt is made, and can never result in a
    persisted claim built from forged data.

    Returns:
      {
        "granted": bool,
        "authorization_id": str | None,
        "client_order_id": str | None,     # this call's own, when granted
        "reason": str | None,               # None on success, else one of:
                                             #   INVALID_AUTHORIZATION_RECORD
                                             #   AUTHORIZATION_RECORD_TAMPERED
                                             #   AUTHORIZATION_NOT_VALID
                                             #   ALREADY_CLAIMED
                                             #   ALREADY_CLAIMED_RECORD_UNVERIFIABLE
                                             #   AUTHORIZATION_ID_REUSED_WITH_MISMATCHED_CONTENT
                                             #   CLAIM_STORE_UNREACHABLE
        "detail": Any,
        "existing_client_order_id": str | None,  # the PRIOR claim's client_order_id, when not granted
        "existing_claim": dict | None,           # the PRIOR claim's full record, when available
      }

    Never releases a granted claim under any outcome -- there is no
    release/delete function anywhere in this module (module docstring
    item 0, checked directly by a dedicated structural test)."""
    if not isinstance(authorization_record, dict):
        return _denied(None, "INVALID_AUTHORIZATION_RECORD", "not a dict")

    auth36 = _load_authorization_module()
    ok, errors = auth36.verify_authorization_record(authorization_record)
    if not ok:
        return _denied(authorization_record.get("authorization_id"),
                        "AUTHORIZATION_RECORD_TAMPERED", ",".join(errors))

    if authorization_record.get("status") != "AUTHORIZED":
        return _denied(authorization_record.get("authorization_id"),
                        "AUTHORIZATION_NOT_VALID", f"status={authorization_record.get('status')!r}")

    authorization_id = authorization_record.get("authorization_id")
    claimed_at = now()
    record = _build_claim_record(authorization_record, claimed_at)

    try:
        path = claim_path(authorization_id, claims_dir)
    except RuntimeError as exc:
        return _denied(authorization_id, "INVALID_AUTHORIZATION_RECORD", str(exc))

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        # Exclusivity is already decided -- this call lost the race. What
        # remains is reading back WHO won (module docstring item 0: the
        # winner's os.open() creates an empty file an instant before it
        # writes the JSON body, so a bounded retry loop is used rather
        # than treating a transient empty/unparsable read as "no claim").
        existing: dict[str, Any] | None = None
        deadline = time.monotonic() + retry_deadline_seconds
        while True:
            try:
                existing = get_claim(authorization_id, claims_dir)
            except RuntimeError:
                existing = None
            if existing is not None:
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.005)

        if existing is None:
            # The file exists (FileExistsError proves that) but its
            # content never became verifiable within the retry window --
            # either still racing indefinitely (should not happen in
            # practice) or genuinely corrupted/tampered. Either way: fail
            # closed, never silently grant, and be honest that the prior
            # claimant's identity could not be confirmed.
            return _denied(authorization_id, "ALREADY_CLAIMED_RECORD_UNVERIFIABLE",
                            "existing claim file could not be read and verified within the retry window")

        if existing.get("authorization_hash") != authorization_record.get("authorization_hash"):
            # Same authorization_id, but the content it was issued for
            # differs from what is already on record -- module docstring
            # item 0's "authorization-identity confusion detection."
            return _denied(authorization_id, "AUTHORIZATION_ID_REUSED_WITH_MISMATCHED_CONTENT",
                            "a claim already exists for this authorization_id with different bound content",
                            existing_claim=existing)

        return _denied(authorization_id, "ALREADY_CLAIMED", None, existing_claim=existing)
    except OSError:
        # Store unreachable (permissions, disk, missing mount, etc.) --
        # fail closed, never treat this as "available to claim."
        return _denied(authorization_id, "CLAIM_STORE_UNREACHABLE", None)

    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, ensure_ascii=False, allow_nan=False)
        f.write("\n")

    return {
        "granted": True,
        "authorization_id": authorization_id,
        "client_order_id": record["client_order_id"],
        "reason": None,
        "detail": None,
        "existing_client_order_id": None,
        "existing_claim": None,
    }
