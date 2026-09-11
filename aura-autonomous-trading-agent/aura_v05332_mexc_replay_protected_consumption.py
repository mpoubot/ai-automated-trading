#!/usr/bin/env python3
"""
AURA v0.5.3.32 -- MEXC Replay-Protected Consumption

Implements `AURA_implementation_spec_execution_authority_and_mexc_adapter`
Sec.3 ("Replay-Protected Consumption") for the MEXC chain:

    ReplayProtection.claim(authorization_id) -> ClaimResult{granted, existing_intent_id}

A claim is never released once granted, regardless of what happens next
(spec Sec.3.2) -- this module has no delete/release function, by design,
matching v0.5.3.27's own claim_client_order_id() and v0.5.3.29's own
create_intent() precedent (neither of those has a release function either).

Why this is a NEW module rather than an extension of v0.5.3.17 (Decision /
Execution Ledger), reconciling the spec's own Sec.3.1 language ("An
extension to the existing .17 Decision/Execution Ledger") against what was
actually built for the MEXC track:

v0.5.3.17 was read directly (not from a prior summary) before writing this
module. It is a pure, stateless, single-shot function -- build_ledger(payload,
input_path) -- that reads exactly one v0.5.3.16 input snapshot and writes
exactly one output snapshot per invocation. It has no persistent store, no
notion of many records accumulating over time, no uniqueness constraint
mechanism, and its REQUIRED_SYMBOLS = ("BTC/USD", "ETH/USD") are hardwired
to the Alpaca/stocks research chain. Sec.3.1's proposal to extend it
predates the MEXC-native v0.5.3.27-.31 lineage that was actually built the
same day; extending .17 today would mean either rewriting it into a
persistent, keyed store (which is not an "extension" -- it is building this
same module under .17's name, and would put the Alpaca/stocks research
track's own module at risk) or faking persistence around it. Neither is
acceptable.

v0.5.3.29 (MEXC Intent Ledger) already provides real, atomic, hash-chain-
verified uniqueness on client_order_id (create_intent()'s O_CREAT|O_EXCL
hard refusal on collision) and is the genuine, already-proven, MEXC-native
analogue of what Sec.3.1 was reaching for. But it has no notion of
authorization_id at all, and the spec's ReplayProtection.claim() interface
is keyed by authorization_id, not client_order_id -- the exact linkage this
module supplies.

This module is therefore the missing, narrow piece: an atomic,
authorization_id-keyed claim store, in the same file-per-key /
O_CREAT|O_EXCL / hash-chain-verified pattern already proven by v0.5.3.27's
claim_client_order_id() and v0.5.3.29's create_intent(). It stores a LINK
(authorization_id -> client_order_id, plus the fingerprints that were bound
at authorization time) -- it never stores or claims to know intent lifecycle
STATE (fills, rejections, reconciliation outcome). v0.5.3.29 remains the
sole source of truth for that; this module never duplicates it. A caller
who receives granted=False can resolve `existing_intent_id` (the
client_order_id of the prior claim) against v0.5.3.29.get_intent() to see
that prior intent's actual current state -- exactly what spec Sec.3.2 asks
for ("surface existing_intent_id so the caller can check that prior
intent's actual state rather than silently dropping the request").

v0.5.3.31 (MEXC Execution Authorization) is updated in this same milestone
to route authorized_submit()'s actual replay-protection decision through
this module instead of its own prior bare marker-file claim
(claim_authorization() itself is left in place, unmodified, as a still-
valid, still-tested, general-purpose atomic-claim primitive -- it is simply
no longer the mechanism authorized_submit() relies on for the
authorization_id claim decision).

No real MEXC credentials and no network call anywhere in this file.
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

VERSION = "AURA v0.5.3.32"
ENGINE = "MEXC_REPLAY_PROTECTED_CONSUMPTION"
SCHEMA_VERSION = "1.0"

DEFAULT_CLAIMS_DIR = Path(
    os.environ.get(
        "AURA_MEXC_REPLAY_PROTECTION_DIR",
        r"regime_output\mexc_replay_protected_consumption\claims",
    )
)

# Matches v0.5.3.29's own CLIENT_ORDER_ID_RE / v0.5.3.31's authorization_id
# shape (f"AUTH-{uuid4().hex}") -- permissive enough to cover both without
# assuming either module's exact generator, since this module must remain
# usable by any future caller that mints its own authorization_id.
AUTHORIZATION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def fail(message: str) -> None:
    raise RuntimeError(message)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def claim_path(authorization_id: str, claims_dir: Path | None = None) -> Path:
    if not isinstance(authorization_id, str) or not AUTHORIZATION_ID_RE.match(authorization_id):
        fail(f"INVALID_AUTHORIZATION_ID:{authorization_id!r}")
    return (claims_dir or DEFAULT_CLAIMS_DIR) / f"{authorization_id}.json"


def canonical_claim_hash(record: dict[str, Any]) -> str:
    canonical = {
        "schema_version": record.get("schema_version"),
        "engine": record.get("engine"),
        "agent_version": record.get("agent_version"),
        "authorization_id": record.get("authorization_id"),
        "client_order_id": record.get("client_order_id"),
        "spec_fingerprint": record.get("spec_fingerprint"),
        "safety_state_fingerprint": record.get("safety_state_fingerprint"),
        "claimed_at": record.get("claimed_at"),
    }
    return sha256_text(stable_json(canonical))


def verify_claim(record: dict[str, Any]) -> tuple[bool, list[str]]:
    """Self-consistency check, mirroring v0.5.3.29's verify_intent() and
    v0.5.3.31's verify_safety_state(): recomputes claim_hash from the
    record's own content and compares."""
    if not isinstance(record, dict):
        return False, ["NOT_A_DICT"]
    expected = canonical_claim_hash(record)
    if record.get("claim_hash") != expected:
        return False, ["CLAIM_HASH_MISMATCH"]
    return True, []


def get_claim(authorization_id: str, claims_dir: Path | None = None) -> dict[str, Any] | None:
    path = claim_path(authorization_id, claims_dir)
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as f:
        record = json.load(f)
    if not isinstance(record, dict):
        fail(f"CLAIM_RECORD_NOT_OBJECT:{authorization_id}")
    return record


def claim(authorization_id: str, client_order_id: str, spec_fingerprint: str | None = None,
          safety_state_fingerprint: str | None = None, claims_dir: Path | None = None) -> dict[str, Any]:
    """ReplayProtection.claim() per spec Sec.3.2.

    Atomic (os.O_CREAT | os.O_EXCL) -- not check-then-write. Returns:
      {
        "granted": bool,
        "authorization_id": str,
        "client_order_id": str | None,        # this call's own client_order_id when granted
        "existing_intent_id": str | None,      # the PRIOR claim's client_order_id when not granted
        "reason": str | None,                  # None | "ALREADY_CLAIMED" | "CLAIM_STORE_UNREACHABLE"
      }

    granted=False with reason="ALREADY_CLAIMED" means this authorization_id
    was already consumed -- existing_intent_id is that prior claim's
    client_order_id, resolvable against v0.5.3.29.get_intent() to inspect
    its actual current state, per spec Sec.3.2. granted=False with
    reason="CLAIM_STORE_UNREACHABLE" means the claim store itself could not
    be written to (fails closed, distinct from "already claimed" so a
    caller/operator can tell the two apart) -- the caller must not submit
    in either case.

    Never releases a granted claim under any outcome -- there is no
    release/delete function in this module, matching v0.5.3.27's
    claim_client_order_id() and v0.5.3.29's create_intent() precedent.
    """
    path = claim_path(authorization_id, claims_dir)

    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "engine": ENGINE,
        "agent_version": VERSION,
        "authorization_id": authorization_id,
        "client_order_id": client_order_id,
        "spec_fingerprint": spec_fingerprint,
        "safety_state_fingerprint": safety_state_fingerprint,
        "claimed_at": now(),
        "claim_hash": None,
    }
    record["claim_hash"] = canonical_claim_hash(record)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        # This authorization_id was already claimed -- exclusivity itself
        # is already decided and correct at this point (O_CREAT|O_EXCL on
        # the target path is what just failed). What remains is reading
        # back WHICH client_order_id won, for the caller's
        # existing_intent_id -- and that read can race the winner's own
        # content write (the winner's os.open() created an empty file an
        # instant before it writes the JSON body), so a bounded retry
        # loop is used here rather than treating a transient empty/
        # unparsable read as "no existing claim." This never affects who
        # WON the claim -- only how quickly this refusal can report back
        # whose claim it was.
        existing_client_order_id = None
        deadline = time.monotonic() + 1.0
        while True:
            try:
                existing = get_claim(authorization_id, claims_dir)
            except (RuntimeError, ValueError, OSError, json.JSONDecodeError):
                existing = None
            if existing is not None and existing.get("client_order_id") is not None:
                existing_client_order_id = existing.get("client_order_id")
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.005)
        return {
            "granted": False,
            "authorization_id": authorization_id,
            "client_order_id": None,
            "existing_intent_id": existing_client_order_id,
            "reason": "ALREADY_CLAIMED",
        }
    except OSError:
        # Store unreachable (permissions, disk, missing mount, etc.) --
        # fail closed, never treat this as "available to claim."
        return {
            "granted": False,
            "authorization_id": authorization_id,
            "client_order_id": None,
            "existing_intent_id": None,
            "reason": "CLAIM_STORE_UNREACHABLE",
        }

    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, ensure_ascii=False, allow_nan=False)
        f.write("\n")

    return {
        "granted": True,
        "authorization_id": authorization_id,
        "client_order_id": client_order_id,
        "existing_intent_id": None,
        "reason": None,
    }
