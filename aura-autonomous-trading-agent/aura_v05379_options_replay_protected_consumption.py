#!/usr/bin/env python3
"""
AURA v0.5.3.79 -- Options Replay-Protected Consumption (O5, part 2 of 2)

The durable, atomic, integrity-verified authorization_id -> execution-
identity consumption record for the options vertical-spread chain --
architecturally equivalent in strength to `.337` (Alpaca Replay-Protected
Consumption) and `.332` (MEXC Replay-Protected Consumption), per the same
"inspect the proven precedent in full, adapt deliberately, do not copy-
and-rename" discipline used for every module in this lineage. `.337` was
read in full before writing this file.

    Options Execution Authorization (.378)
            |
            v
    Replay-Protected Consumption (.379)    <- THIS MODULE
            |
            v
    [future O9 orchestrator]
            |
            v
    O4 (.376) order construction / submission

============================================================================
0. Reused AS-IS from .337 (proven, venue-neutral principles)
============================================================================

  - Atomic uniqueness: os.O_CREAT | os.O_EXCL on a per-key file. This is
    the ONLY thing that decides who wins a race -- never check-then-write.
  - Never released: no delete/release/unclaim function exists in this
    module, structurally, matching .337's own "no release function, by
    design" (checked directly by a dedicated test).
  - Crash/restart durability: the claim is a plain file on disk; a fresh
    process sees the exact same claim, with no in-memory state to lose.
  - The race-then-read-back pattern for reporting who won: the winner's
    os.open() call creates an (initially empty) file an instant before it
    writes the JSON body, so a loser reading back "who won" retries for a
    bounded window rather than treating a transient empty/unparsable read
    as "no existing claim."
  - The DENIED/UNREACHABLE distinction: "already claimed" (an exclusivity
    loss) and "claim store unreachable" (an OSError) are reported as
    distinct, named reasons -- never conflated.
  - Full-record re-verification before ever touching the filesystem:
    takes the FULL `.378` AuthorizationRecord and independently
    re-verifies it (via `.378.verify_authorization_record()`, dynamically
    imported, unmodified) and its status (AUTHORIZED) before any claim
    attempt -- exactly `.337`'s own improvement over `.332`'s looser,
    caller-trusted-kwargs design.
  - Persisted-record integrity on READ, not just on write: `get_claim()`
    re-verifies via `verify_claim()` on every read and FAILS CLOSED
    (raises) if the content is unparsable or its claim_hash does not
    match.
  - Authorization-identity confusion detection: a second claim() attempt
    presenting the SAME authorization_id but a DIFFERENT (self-
    consistent, differently-hashed) record is detected and reported as
    AUTHORIZATION_ID_REUSED_WITH_MISMATCHED_CONTENT, distinct from an
    ordinary ALREADY_CLAIMED duplicate.
  - The Windows PermissionError/FileExistsError disambiguation fix
    (2026-10-03, applied across `.329`/`.332`/`.337`/`.327`/`.331`/`.336`
    after the real Windows concurrency findings on Martin's machine):
    applied here from day one rather than retrofitted later, since this
    module is being built fresh, on the same mechanism, for a repo that
    is known to run on Windows in production.

============================================================================
1. Adapted for options: WHAT is bound into the persisted record
============================================================================

`.337`'s record binds venue/environment/asset_class/symbol/direction/
position_intent -- a single-leg shape. Options vertical spreads are
2-leg: this module's record instead binds venue, environment,
asset_class, order_class ("MLEG"), underlying_symbol, and the FULL `legs`
list (each leg's occ_symbol/side/position_intent/ratio_qty/direction/
contract_fingerprint, copied directly from the already-verified `.378`
record -- never re-derived or guessed here), plus qty/limit_price/
time_in_force/execution_spec_fingerprint. Every field is copied straight
from the verified AuthorizationRecord, same discipline as `.337`'s own
"only include fields justified by the existing contracts."

============================================================================
2. What durable identity is claimed, and why no new identity is invented
============================================================================

Same as `.337`: `authorization_id` is the claimed key, `client_order_id`
is the recorded, already-canonical identity carried through from `.378`
(itself carried through from the caller's own spec, unchanged) -- no new
identity is invented. For MLEG orders there is exactly ONE
client_order_id for the whole parent order (both legs share it; O4's own
`validate_vertical_spec()` requires exactly one), so this module's
identity model needs no change from `.337`'s single-client_order_id
assumption despite the 2-leg structure underneath it.

============================================================================
3. EXECUTION_UNCERTAIN never becomes permission to retry
============================================================================

Same as `.337`: this module has no concept of a downstream submission
outcome at all -- it only ever answers "has this authorization_id been
consumed." Once claim() grants, nothing in this file can un-consume it.

============================================================================
4. What this module deliberately does NOT do
============================================================================

No new intent ledger. Stores exactly one thing: an authorization_id ->
execution-identity consumption LINK, never lifecycle STATE (fills,
rejections, reconciliation outcome -- that remains O4B's (`.377`) job,
which this module does not touch, import, or duplicate). No modification
to `.373`/`.374`/`.375`/`.376`/`.377`/`.378` -- all untouched. No live
execution. No Supervisor/orchestrator. No actual Alpaca order submission
anywhere in this file or its tests.

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

VERSION = "AURA v0.5.3.79"
ENGINE = "OPTIONS_REPLAY_PROTECTED_CONSUMPTION"
SCHEMA_VERSION = "1.0"

ROOT = Path(__file__).resolve().parent

DEFAULT_CLAIMS_DIR = Path(
    os.environ.get(
        "AURA_OPTIONS_REPLAY_PROTECTION_DIR",
        "regime_output/options_replay_protected_consumption/claims",
    )
)

# Matches .378's authorization_id shape
# (f"OPTIONS-VERTICAL-AUTH-{uuid4().hex}", 49 chars) -- kept permissive
# (same bound as .332/.337's own AUTHORIZATION_ID_RE) rather than
# assuming an exact length.
AUTHORIZATION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

DEFAULT_RETRY_DEADLINE_SECONDS = 1.0

# This module has ZERO network-capable functions -- mirrors .378's own
# disclosed, empty NETWORK_CAPABLE_FUNCTIONS exactly.
NETWORK_CAPABLE_FUNCTIONS: frozenset[str] = frozenset()


def fail(message: str) -> None:
    raise RuntimeError(message)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------- #
# Dynamic import of v0.5.3.78 -- same _load_*_module() pattern already
# established throughout this lineage. Used only to call
# verify_authorization_record() (unmodified, read-only) against a record
# THIS module was given -- never to construct or authorize anything itself.
# --------------------------------------------------------------------- #

def _load_authorization_module():
    try:
        import aura_v05378_options_execution_authorization as mod
        return mod
    except ImportError:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "aura_v05378_options_execution_authorization",
            ROOT / "aura_v05378_options_execution_authorization.py",
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
        "order_class": record.get("order_class"),
        "underlying_symbol": record.get("underlying_symbol"),
        "legs": record.get("legs"),
        "qty": record.get("qty"),
        "limit_price": record.get("limit_price"),
        "time_in_force": record.get("time_in_force"),
        "execution_spec_fingerprint": record.get("execution_spec_fingerprint"),
        "safety_state_fingerprint": record.get("safety_state_fingerprint"),
        "claimed_at": record.get("claimed_at"),
    }
    return sha256_text(stable_json(canonical))


def verify_claim(record: dict[str, Any]) -> tuple[bool, list[str]]:
    """Self-consistency check, mirroring .337's own verify_claim()
    convention: recomputes claim_hash from the record's own content and
    compares."""
    if not isinstance(record, dict):
        return False, ["NOT_A_DICT"]
    expected = canonical_claim_hash(record)
    if record.get("claim_hash") != expected:
        return False, ["CLAIM_HASH_MISMATCH"]
    return True, []


def get_claim(authorization_id: str, claims_dir: Path | None = None) -> dict[str, Any] | None:
    """Loads and returns the persisted claim record for authorization_id,
    or None if no claim file exists at all. ALWAYS self-verifies the
    record it reads (module docstring item 0) and FAILS CLOSED (raises
    RuntimeError) if the file exists but its content is unparsable, not
    an object, or fails verify_claim() -- a corrupted or tampered
    persisted record is never silently treated as "no claim" (which
    would wrongly make the authorization_id look available again) and
    never silently trusted as-is."""
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
    already-verified `.378` AuthorizationRecord -- every field is copied
    directly, never re-derived or guessed (module docstring item 1)."""
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
        "order_class": authorization_record.get("order_class"),
        "underlying_symbol": authorization_record.get("underlying_symbol"),
        "legs": authorization_record.get("legs"),
        "qty": authorization_record.get("qty"),
        "limit_price": authorization_record.get("limit_price"),
        "time_in_force": authorization_record.get("time_in_force"),
        "execution_spec_fingerprint": authorization_record.get("execution_spec_fingerprint"),
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
    """ReplayProtection.claim() for the options vertical-spread chain --
    the ONE authoritative consumption decision for an authorization_id
    (module docstring item 3 of `.378`: no provisional primitive).

    Takes the FULL `.378` AuthorizationRecord and independently
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
    release/delete function anywhere in this module (checked directly by
    a dedicated structural test)."""
    if not isinstance(authorization_record, dict):
        return _denied(None, "INVALID_AUTHORIZATION_RECORD", "not a dict")

    auth78 = _load_authorization_module()
    ok, errors = auth78.verify_authorization_record(authorization_record)
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

    # Windows store-unreachable misclassification fix (same pattern as
    # .332/.337, applied here from day one -- see module docstring item
    # 0): the directory-creation step and the claim-file-open step are
    # TWO separate try/except blocks, not one. Any failure to even
    # create/reach the claims directory is unconditionally
    # CLAIM_STORE_UNREACHABLE; only a FileExistsError on the actual
    # target claim file (from os.open()) can mean an exclusivity-race
    # outcome.
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return _denied(authorization_id, "CLAIM_STORE_UNREACHABLE", None)

    def _resolve_race_outcome() -> dict[str, Any]:
        # Exclusivity is already decided at the point this is called --
        # this process lost the race. What remains is reading back WHO
        # won (module docstring item 0: the winner's os.open() creates an
        # empty file an instant before it writes the JSON body, so a
        # bounded retry loop is used rather than treating a transient
        # empty/unparsable read as "no claim").
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
            # The file exists but its content never became verifiable
            # within the retry window -- either still racing indefinitely
            # (should not happen in practice) or genuinely corrupted/
            # tampered. Either way: fail closed, never silently grant,
            # and be honest that the prior claimant's identity could not
            # be confirmed.
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

    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        # Exclusivity is already decided -- this call lost the race.
        return _resolve_race_outcome()
    except PermissionError:
        # On Windows, the identical O_CREAT|O_EXCL race that raises
        # FileExistsError on POSIX can instead raise PermissionError
        # (WinError 5) in the narrow window before the winning thread's
        # handle is released. This is genuinely ambiguous -- it could
        # mean a real race-win (the same thing as FileExistsError above)
        # OR an unrelated permission/disk failure, which must still
        # surface as CLAIM_STORE_UNREACHABLE, never be silently read as
        # "already claimed." A bounded existence check on the real
        # target path decides which it is.
        existence_deadline = time.monotonic() + retry_deadline_seconds
        claim_exists = False
        while True:
            if path.exists():
                claim_exists = True
                break
            if time.monotonic() >= existence_deadline:
                break
            time.sleep(0.005)
        if not claim_exists:
            return _denied(authorization_id, "CLAIM_STORE_UNREACHABLE", None)
        return _resolve_race_outcome()
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
