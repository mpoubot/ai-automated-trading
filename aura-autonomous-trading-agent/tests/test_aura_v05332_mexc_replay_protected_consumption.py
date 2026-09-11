#!/usr/bin/env python3
"""Unit tests for AURA v0.5.3.32 (MEXC Replay-Protected Consumption).

Covers Martin's item F for this milestone:
  - duplicate authorization
  - concurrent claim race
  - restart/recovery
  - claim-store failure
  - EXECUTION_UNCERTAIN (covered end to end via v0.5.3.31's own
    authorized_submit() tests -- test_aura_v05331_mexc_execution_authorization.py
    and the chain integration file -- since .32 itself has no notion of
    submission outcomes, only the authorization_id -> client_order_id claim)
  - adapter-claim failure / interaction with .27's existing client_order_id
    claim (same -- covered at the .31 authorized_submit() level, where the
    interaction actually happens; .32 provides no adapter-facing surface)

No real MEXC credentials, no network call, anywhere in this file.
"""
from __future__ import annotations

import importlib.util
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


REPLAY = _load("aura_v05332_mexc_replay_protected_consumption", "aura_v05332_mexc_replay_protected_consumption.py")


def expect(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"PASS: {name}")


# ======================================================================= #
# Basic claim/grant behavior
# ======================================================================= #

def test_first_claim_granted() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        claims = Path(d) / "claims"
        result = REPLAY.claim("AUTH-basic-1", "cid-basic-1", claims_dir=claims)
        expect("basic: first claim is granted", result["granted"] is True)
        expect("basic: client_order_id echoed back", result["client_order_id"] == "cid-basic-1")
        expect("basic: existing_intent_id is None on a granted claim", result["existing_intent_id"] is None)
        expect("basic: reason is None on a granted claim", result["reason"] is None)


def test_claim_is_durably_persisted_and_self_verifies() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        claims = Path(d) / "claims"
        REPLAY.claim("AUTH-persist-1", "cid-persist-1", spec_fingerprint="sha256:spec-fp",
                     safety_state_fingerprint="sha256:safety-fp", claims_dir=claims)
        record = REPLAY.get_claim("AUTH-persist-1", claims_dir=claims)
        expect("persist: claim record exists on disk", record is not None)
        expect("persist: client_order_id persisted", record["client_order_id"] == "cid-persist-1")
        expect("persist: spec_fingerprint persisted", record["spec_fingerprint"] == "sha256:spec-fp")
        ok, errors = REPLAY.verify_claim(record)
        expect("persist: claim record self-verifies", ok and not errors)


# ======================================================================= #
# Duplicate authorization
# ======================================================================= #

def test_duplicate_authorization_refused_with_existing_intent_id() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        claims = Path(d) / "claims"
        first = REPLAY.claim("AUTH-dup-1", "cid-dup-original", claims_dir=claims)
        expect("duplicate: first claim granted", first["granted"] is True)

        second = REPLAY.claim("AUTH-dup-1", "cid-dup-retry-attempt", claims_dir=claims)
        expect("duplicate: second claim on the same authorization_id refused", second["granted"] is False)
        expect("duplicate: reason is ALREADY_CLAIMED", second["reason"] == "ALREADY_CLAIMED")
        expect("duplicate: existing_intent_id resolves to the ORIGINAL client_order_id, not the retry's",
               second["existing_intent_id"] == "cid-dup-original")


def test_duplicate_claim_does_not_overwrite_the_original_record() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        claims = Path(d) / "claims"
        REPLAY.claim("AUTH-dup-2", "cid-first", claims_dir=claims)
        REPLAY.claim("AUTH-dup-2", "cid-second-attempt", claims_dir=claims)
        record = REPLAY.get_claim("AUTH-dup-2", claims_dir=claims)
        expect("duplicate-no-overwrite: the persisted record is still the FIRST claim's", record["client_order_id"] == "cid-first")


# ======================================================================= #
# Concurrent claim race
# ======================================================================= #

def test_concurrent_claim_race_exactly_one_wins() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        claims = Path(d) / "claims"
        results: list[dict] = []
        lock = threading.Lock()

        def attempt(n: int):
            r = REPLAY.claim("AUTH-race-1", f"cid-race-{n}", claims_dir=claims)
            with lock:
                results.append(r)

        threads = [threading.Thread(target=attempt, args=(n,)) for n in range(25)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        granted = [r for r in results if r["granted"] is True]
        refused = [r for r in results if r["granted"] is False]
        expect("race: exactly one of 25 concurrent claims on the same authorization_id is granted", len(granted) == 1)
        expect("race: the other 24 are refused", len(refused) == 24)
        expect("race: every refusal resolves existing_intent_id back to the SAME winning client_order_id",
               len({r["existing_intent_id"] for r in refused}) == 1)
        winner_cid = granted[0]["client_order_id"]
        expect("race: the refusals' existing_intent_id matches the actual winner",
               refused[0]["existing_intent_id"] == winner_cid)


# ======================================================================= #
# Restart / recovery
# ======================================================================= #

def test_restart_recovery_claim_survives_a_fresh_process_view() -> None:
    """Simulates a restart: the claim was made, "the process dies", and a
    fresh caller (here: a fresh module load, the closest this test harness
    gets to a real process restart) must see the SAME durable claim -- not
    lose it, and not allow a second grant."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        claims = Path(d) / "claims"
        first = REPLAY.claim("AUTH-restart-1", "cid-restart-1", claims_dir=claims)
        expect("restart: original claim granted", first["granted"] is True)

        # "restart": a fresh module load, no in-memory state carried over.
        fresh_replay = _load("aura_v05332_mexc_replay_protected_consumption_restart_check",
                              "aura_v05332_mexc_replay_protected_consumption.py")

        recovered = fresh_replay.get_claim("AUTH-restart-1", claims_dir=claims)
        expect("restart: the claim record is recovered intact after a simulated restart", recovered is not None and recovered["client_order_id"] == "cid-restart-1")
        ok, errors = fresh_replay.verify_claim(recovered)
        expect("restart: the recovered record still self-verifies", ok and not errors)

        retry = fresh_replay.claim("AUTH-restart-1", "cid-restart-retry", claims_dir=claims)
        expect("restart: a retry after the simulated restart is still refused", retry["granted"] is False)
        expect("restart: existing_intent_id still resolves correctly post-restart", retry["existing_intent_id"] == "cid-restart-1")


# ======================================================================= #
# Claim-store failure
# ======================================================================= #

def test_claim_store_unreachable_fails_closed() -> None:
    """When the claims directory cannot be created/written to (e.g. its
    parent is actually a file, not a directory -- a simple, portable way
    to force an OSError without relying on filesystem permissions), claim()
    must fail closed with a distinct reason, never silently behave as if
    the authorization_id were available."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        blocking_file = Path(d) / "not_a_directory"
        blocking_file.write_text("this is a file, not a directory", encoding="utf-8")
        unusable_claims_dir = blocking_file / "claims"  # cannot mkdir under a file

        result = REPLAY.claim("AUTH-store-fail-1", "cid-store-fail-1", claims_dir=unusable_claims_dir)
        expect("claim-store-failure: claim fails closed rather than granting", result["granted"] is False)
        expect("claim-store-failure: reason is CLAIM_STORE_UNREACHABLE, distinct from ALREADY_CLAIMED", result["reason"] == "CLAIM_STORE_UNREACHABLE")
        expect("claim-store-failure: no existing_intent_id is fabricated", result["existing_intent_id"] is None)


def test_claim_store_recovers_once_reachable_again() -> None:
    """A prior CLAIM_STORE_UNREACHABLE result must never itself count as a
    consumed claim -- once the store is reachable, the SAME authorization_id
    can still be genuinely claimed (this is fail-closed on availability,
    not a permanent poison of the authorization_id)."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        blocking_file = Path(d) / "not_a_directory"
        blocking_file.write_text("blocking", encoding="utf-8")
        unusable_claims_dir = blocking_file / "claims"
        first = REPLAY.claim("AUTH-recover-1", "cid-recover-1", claims_dir=unusable_claims_dir)
        expect("claim-store-recovers: first attempt fails closed", first["granted"] is False and first["reason"] == "CLAIM_STORE_UNREACHABLE")

        usable_claims_dir = Path(d) / "usable_claims"
        second = REPLAY.claim("AUTH-recover-1", "cid-recover-1", claims_dir=usable_claims_dir)
        expect("claim-store-recovers: the same authorization_id can still be granted once the store is reachable", second["granted"] is True)


# ======================================================================= #
# No release under any outcome
# ======================================================================= #

def test_module_exposes_no_release_or_delete_function() -> None:
    """Structural guarantee, not just a behavioral test: a claim is never
    released once granted, regardless of what happens next (spec Sec.3.2)
    -- this module simply has no function capable of undoing a claim."""
    release_like_names = [name for name in dir(REPLAY) if "release" in name.lower() or "delete" in name.lower() or "unclaim" in name.lower()]
    expect("no-release: the module exposes no release/delete/unclaim function at all", release_like_names == [])


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_")]
    for t in tests:
        t()
    print(f"AURA v0.5.3.32 UNIT TEST CONTRACT: ALL {len(tests)} PASS")
