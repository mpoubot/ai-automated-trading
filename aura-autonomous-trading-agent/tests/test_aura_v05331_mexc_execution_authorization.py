#!/usr/bin/env python3
"""Unit tests for AURA v0.5.3.31 (MEXC Execution Authorization).

Covers Martin's 18 numbered test requirements (items 1-12 and 18 -- items
13-17, the EXECUTION_UNCERTAIN/reconciliation outcomes, are proven
end-to-end through the real v0.5.3.27-.30 chain in
tests/test_aura_v05331_authorization_chain_integration.py), plus the two
regression tests explicitly required by Martin's 2026-09-10 GO message:

  Constraint 1: "Add a regression test proving a forged config/spec cannot
  create valid authorization."
    -> test_forged_execution_spec_authorized_flag_alone_is_insufficient
    -> test_forged_safety_state_cross_check_rejected

  Constraint 2: "Explicitly define and test the interaction between the
  new authorization claim and .27's existing client_order_id claim."
    -> the "claim interaction" section below.

No real MEXC credentials, no network call, anywhere in this file.
"""
from __future__ import annotations

import importlib.util
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


AUTH = _load("aura_v05331_mexc_execution_authorization", "aura_v05331_mexc_execution_authorization.py")
ADAPTER = _load("aura_v05327_mexc_execution_adapter", "aura_v05327_mexc_execution_adapter.py")
REPLAY = _load("aura_v05332_mexc_replay_protected_consumption", "aura_v05332_mexc_replay_protected_consumption.py")


def expect(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"PASS: {name}")


# --------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------- #

BASE_SPEC = {
    "execution_spec_version": "1.0",
    "exchange": "MEXC",
    "account_mode": "LIVE",
    "market_type": "swap",
    "kill_switch": False,
    "execution_authorized": True,
    "live_execution_authorized": True,
    "symbol": "BTC/USDT:USDT",
    "side": "BUY",
    "quantity": "0.01",
    "order_type": "MARKET",
    "reduce_only": False,
    "leverage": 3,
    "client_order_id": "aura-test-05331-001",
}

ALLOW_ALL_CONFIG = {
    "kill_switch": False,
    "execution_authorized": True,
    "paper_execution_authorized": True,
    "live_execution_authorized": True,
    "authorization_ttl_seconds": 60,
    "safety_state_ttl_seconds": 60,
}


class FakeExchange:
    """Same shape as v0.5.3.27's own test suite's FakeExchange."""

    def __init__(self, create_order_result=None, create_order_exception=None, set_leverage_exception=None):
        self.create_order_calls: list[tuple] = []
        self.set_leverage_calls: list[tuple] = []
        self._create_order_result = create_order_result
        self._create_order_exception = create_order_exception
        self._set_leverage_exception = set_leverage_exception

    def set_leverage(self, leverage, symbol):
        self.set_leverage_calls.append((leverage, symbol))
        if self._set_leverage_exception:
            raise self._set_leverage_exception

    def create_order(self, symbol, type, side, amount, params=None):
        self.create_order_calls.append((symbol, type, side, amount, params))
        if self._create_order_exception:
            raise self._create_order_exception
        return self._create_order_result or {"id": "mexc-fake-order-1", "status": "closed"}


def empty_ledger_dir(tmp: Path) -> Path:
    d = tmp / "ledger"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ======================================================================= #
# 1. Valid authorization
# ======================================================================= #

def test_valid_authorization() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        record = AUTH.authorize(BASE_SPEC, config=ALLOW_ALL_CONFIG,
                                 ledger_base_dir=empty_ledger_dir(tmp), claims_dir=tmp / "auth_claims")
        expect("1: valid authorization succeeds", record["status"] == "AUTHORIZED")
        expect("1: authorization_id present", isinstance(record.get("authorization_id"), str) and record["authorization_id"].startswith("AUTH-"))
        expect("1: spec_fingerprint and safety_state_fingerprint present", record.get("spec_fingerprint") and record.get("safety_state_fingerprint"))
        expect("1: expires_at is after issued_at",
               AUTH._parse_iso(record["expires_at"]) > AUTH._parse_iso(record["issued_at"]))
        ok, errors = AUTH.verify_authorization_record(record)
        expect("1: record self-verifies", ok and not errors)


# ======================================================================= #
# 2. Kill switch blocks
# ======================================================================= #

def test_kill_switch_blocks() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        config = dict(ALLOW_ALL_CONFIG, kill_switch=True)
        record = AUTH.authorize(BASE_SPEC, config=config, ledger_base_dir=empty_ledger_dir(tmp), claims_dir=tmp / "auth_claims")
        expect("2: kill switch blocks authorization", record["status"] == "AUTHORIZATION_REJECTED" and record["reason"] == "KILL_SWITCH_ENGAGED")


# ======================================================================= #
# 3. execution_authorized=False blocks
# ======================================================================= #

def test_execution_authorized_false_blocks() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        config = dict(ALLOW_ALL_CONFIG, execution_authorized=False)
        record = AUTH.authorize(BASE_SPEC, config=config, ledger_base_dir=empty_ledger_dir(tmp), claims_dir=tmp / "auth_claims")
        expect("3: execution_authorized=False blocks", record["status"] == "AUTHORIZATION_REJECTED" and record["reason"] == "SAFETY_STATE_NOT_AUTHORIZED")

        # Also the safe-by-default DEFAULT_AUTH_CONFIG on its own (no overrides at all)
        record2 = AUTH.authorize(BASE_SPEC, config=None, ledger_base_dir=empty_ledger_dir(tmp), claims_dir=tmp / "auth_claims2")
        expect("3: default config (no explicit authorization) blocks by default", record2["status"] == "AUTHORIZATION_REJECTED")


# ======================================================================= #
# 4. Paper/live mismatch blocks
# ======================================================================= #

def test_paper_live_mismatch_blocks() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        config = dict(ALLOW_ALL_CONFIG, live_execution_authorized=False)
        live_spec = dict(BASE_SPEC, account_mode="LIVE")
        record = AUTH.authorize(live_spec, config=config, ledger_base_dir=empty_ledger_dir(tmp), claims_dir=tmp / "auth_claims")
        expect("4: LIVE spec blocked when only paper is authorized", record["status"] == "AUTHORIZATION_REJECTED" and record["reason"] == "LIVE_EXECUTION_NOT_AUTHORIZED")

        config2 = dict(ALLOW_ALL_CONFIG, paper_execution_authorized=False)
        paper_spec = dict(BASE_SPEC, account_mode="PAPER")
        record2 = AUTH.authorize(paper_spec, config=config2, ledger_base_dir=empty_ledger_dir(tmp), claims_dir=tmp / "auth_claims2")
        expect("4: PAPER spec blocked when only live is authorized", record2["status"] == "AUTHORIZATION_REJECTED" and record2["reason"] == "PAPER_EXECUTION_NOT_AUTHORIZED")


# ======================================================================= #
# 5. Expired authorization blocks
# ======================================================================= #

def test_expired_authorization_blocks() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        config = dict(ALLOW_ALL_CONFIG, authorization_ttl_seconds=1)
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        record = AUTH.authorize(BASE_SPEC, config=config, ledger_base_dir=empty_ledger_dir(tmp), claims_dir=tmp / "auth_claims", now=t0)
        expect("5: authorization issued", record["status"] == "AUTHORIZED")

        later = t0 + timedelta(seconds=10)
        result = AUTH.revalidate_before_submission(record, BASE_SPEC, config=config, ledger_base_dir=empty_ledger_dir(tmp), claims_dir=tmp / "auth_claims", now=later)
        expect("5: expired authorization blocks revalidation", result["status"] == "AUTHORIZATION_REJECTED" and result["reason"] == "EXPIRED_AUTHORIZATION")


# ======================================================================= #
# 6. Stale execution spec blocks
# ======================================================================= #

def test_stale_execution_spec_blocks() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        stale_spec = dict(BASE_SPEC, expires_at=AUTH._iso(t0 - timedelta(seconds=5)))
        record = AUTH.authorize(stale_spec, config=ALLOW_ALL_CONFIG, ledger_base_dir=empty_ledger_dir(tmp), claims_dir=tmp / "auth_claims", now=t0)
        expect("6: stale (already-expired) execution spec blocks authorization",
               record["status"] == "AUTHORIZATION_REJECTED" and record["reason"] == "EXPIRED_EXECUTION_SPEC")


# ======================================================================= #
# 7. Stale safety state blocks
# ======================================================================= #

def test_stale_safety_state_blocks() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        config = dict(ALLOW_ALL_CONFIG, safety_state_ttl_seconds=1)
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        state = AUTH.assemble_safety_state(config, ledger_base_dir=empty_ledger_dir(tmp), claims_dir=tmp / "auth_claims", now=t0)

        later = t0 + timedelta(seconds=30)
        record = AUTH.authorize(BASE_SPEC, safety_state=state, config=config, ledger_base_dir=empty_ledger_dir(tmp), claims_dir=tmp / "auth_claims", now=later)
        expect("7: stale safety_state blocks authorization", record["status"] == "AUTHORIZATION_REJECTED" and record["reason"] == "STALE_SAFETY_STATE")


# ======================================================================= #
# 8. Modified execution spec -> fingerprint mismatch
# ======================================================================= #

def test_modified_execution_spec_fingerprint_mismatch() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        record = AUTH.authorize(BASE_SPEC, config=ALLOW_ALL_CONFIG, ledger_base_dir=empty_ledger_dir(tmp), claims_dir=tmp / "auth_claims")
        expect("8: authorization issued", record["status"] == "AUTHORIZED")

        mutated_spec = dict(BASE_SPEC, quantity="999")
        result = AUTH.revalidate_before_submission(record, mutated_spec, config=ALLOW_ALL_CONFIG, ledger_base_dir=empty_ledger_dir(tmp), claims_dir=tmp / "auth_claims")
        expect("8: a modified execution_spec fails revalidation with a fingerprint mismatch",
               result["status"] == "AUTHORIZATION_REJECTED" and result["reason"] == "EXECUTION_SPEC_FINGERPRINT_MISMATCH")


# ======================================================================= #
# 9. Modified safety state -> fingerprint mismatch
# ======================================================================= #

def test_modified_safety_state_fingerprint_mismatch() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        state = AUTH.assemble_safety_state(ALLOW_ALL_CONFIG, ledger_base_dir=empty_ledger_dir(tmp), claims_dir=tmp / "auth_claims")
        ok, _ = AUTH.verify_safety_state(state)
        expect("9: genuine safety_state self-verifies before tampering", ok)

        tampered = dict(state)
        tampered["execution_authorized"] = True if state["execution_authorized"] is False else False
        # deliberately do NOT recompute safety_state_hash -- this is the tamper
        ok2, errors2 = AUTH.verify_safety_state(tampered)
        expect("9: tampered safety_state fails self-verification", not ok2 and "SAFETY_STATE_HASH_MISMATCH" in errors2)

        record = AUTH.authorize(BASE_SPEC, safety_state=tampered, config=ALLOW_ALL_CONFIG,
                                 ledger_base_dir=empty_ledger_dir(tmp), claims_dir=tmp / "auth_claims")
        expect("9: a modified (tampered) safety_state is rejected with a fingerprint mismatch",
               record["status"] == "AUTHORIZATION_REJECTED" and record["reason"] == "SAFETY_STATE_FINGERPRINT_MISMATCH")


# ======================================================================= #
# 10. Cannot be consumed twice
# ======================================================================= #

def test_cannot_be_consumed_twice() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        claims = Path(d) / "auth_claims"
        first = AUTH.claim_authorization(claims, "AUTH-dup-test")
        second = AUTH.claim_authorization(claims, "AUTH-dup-test")
        expect("10: first claim is granted", first is True)
        expect("10: second claim on the same authorization_id is refused", second is False)


# ======================================================================= #
# 11. Concurrent/repeated consumption atomic
# ======================================================================= #

def test_concurrent_consumption_atomic() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        claims = Path(d) / "auth_claims"
        results: list[bool] = []
        lock = threading.Lock()

        def attempt():
            granted = AUTH.claim_authorization(claims, "AUTH-concurrent-test")
            with lock:
                results.append(granted)

        threads = [threading.Thread(target=attempt) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        expect("11: exactly one of 20 concurrent claims is granted", results.count(True) == 1)
        expect("11: the other 19 are refused", results.count(False) == 19)


# ======================================================================= #
# 12. Final revalidation occurs immediately before submission
# ======================================================================= #

def test_final_revalidation_occurs_before_submission() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        record = AUTH.authorize(BASE_SPEC, config=ALLOW_ALL_CONFIG, ledger_base_dir=empty_ledger_dir(tmp), claims_dir=tmp / "auth_claims")
        expect("12: authorization issued", record["status"] == "AUTHORIZED")

        fake = FakeExchange(create_order_result={"id": "mexc-order-99", "status": "closed"})
        result = AUTH.authorized_submit(
            record, BASE_SPEC, tmp / "auth_claims", tmp / "adapter_claims",
            config=ALLOW_ALL_CONFIG, ledger_base_dir=empty_ledger_dir(tmp), exchange=fake,
        )
        expect("12: submission proceeds when revalidation passes", result["status"] == "SUBMISSION_ATTEMPTED")
        expect("12: the real .27 create_order was actually called", len(fake.create_order_calls) == 1)


# ======================================================================= #
# 18. Kill switch during final submission boundary prevents submission
# ======================================================================= #

def test_kill_switch_at_final_boundary_prevents_submission() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        # Authorize while clear...
        record = AUTH.authorize(BASE_SPEC, config=ALLOW_ALL_CONFIG, ledger_base_dir=empty_ledger_dir(tmp), claims_dir=tmp / "auth_claims")
        expect("18: authorization issued while kill switch clear", record["status"] == "AUTHORIZED")

        # ...but the operator engages the kill switch before submission.
        engaged_config = dict(ALLOW_ALL_CONFIG, kill_switch=True)
        fake = FakeExchange(create_order_result={"id": "mexc-order-should-not-happen", "status": "closed"})
        result = AUTH.authorized_submit(
            record, BASE_SPEC, tmp / "auth_claims", tmp / "adapter_claims",
            config=engaged_config, ledger_base_dir=empty_ledger_dir(tmp), exchange=fake,
        )
        expect("18: revalidation fails once the kill switch is engaged", result["status"] == "REVALIDATION_FAILED" and result["reason"] == "KILL_SWITCH_ENGAGED")
        expect("18: the exchange was never called", fake.create_order_calls == [])

        # The authorization_id claim is still permanently consumed -- no
        # retry. authorized_submit() claims through v0.5.3.32 now (not the
        # bare marker file), so the retry check goes through .32 too.
        retry = REPLAY.claim(record["authorization_id"], BASE_SPEC["client_order_id"], claims_dir=tmp / "auth_claims")
        expect("18: the authorization_id claim remains permanently consumed after the blocked revalidation",
               retry["granted"] is False and retry["reason"] == "ALREADY_CLAIMED")


# ======================================================================= #
# Constraint 1 regression: forged config/spec cannot create valid
# authorization.
# ======================================================================= #

def test_forged_execution_spec_authorized_flag_alone_is_insufficient() -> None:
    """Reproduces the exact provenance gap v0.5.3.27 had on its own:
    execution_spec carries execution_authorized=True / live_execution_
    authorized=True / kill_switch=False directly in the spec dict (exactly
    what an attacker or a stale/incorrect artifact could fabricate). With
    no real authorization configured (safe-by-default config), authorize()
    must still reject -- proving these spec-embedded fields have ZERO
    authority over this module's decision."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        forged_spec = dict(
            BASE_SPEC,
            execution_authorized=True,
            live_execution_authorized=True,
            kill_switch=False,
        )
        record = AUTH.authorize(forged_spec, config=None, ledger_base_dir=empty_ledger_dir(tmp), claims_dir=tmp / "auth_claims")
        expect("forged-spec: a spec claiming its own authorization is still rejected under a safe-by-default config",
               record["status"] == "AUTHORIZATION_REJECTED")


def test_forged_safety_state_cross_check_rejected() -> None:
    """A fully self-consistent (correctly hashed) but FALSE safety_state --
    i.e. an attacker who has learned the hashing scheme and forges a whole,
    internally-consistent safety_state claiming full authorization -- must
    still be rejected, because authorize() checks it against an
    independently, freshly recomputed safety_state, not merely its own
    internal consistency."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        forged_content = {
            "schema_version": AUTH.SCHEMA_VERSION,
            "agent_version": AUTH.VERSION,
            "engine": AUTH.ENGINE,
            "kill_switch": False,
            "execution_authorized": True,
            "paper_execution_authorized": True,
            "live_execution_authorized": True,
            "reconciliation_health": {"status": "HEALTHY", "reason": None, "unresolved_count": 0, "escalated_count": 0, "blocked_unreadable_count": 0},
            "replay_protection": {"status": "HEALTHY", "reason": None},
            "guardrails": {
                "single_source_of_truth": True, "reconciliation_only": True,
                "position_creation_from_observation": False, "exchange_state_mutation": False,
                "orders_allowed": False, "live_execution": False, "fail_closed": True,
            },
        }
        forged_state = dict(forged_content)
        forged_state["assembled_at"] = AUTH._iso(AUTH._now())
        forged_state["safety_state_ttl_seconds"] = 600
        forged_state["safety_state_hash"] = AUTH._fingerprint(forged_content)  # correctly self-consistent

        ok, _ = AUTH.verify_safety_state(forged_state)
        expect("forged-safety-state: the forged state is internally self-consistent (attacker did the hashing correctly)", ok)

        # Real config on disk denies everything (safe-by-default).
        record = AUTH.authorize(BASE_SPEC, safety_state=forged_state, config=None,
                                 ledger_base_dir=empty_ledger_dir(tmp), claims_dir=tmp / "auth_claims")
        expect("forged-safety-state: still rejected because it does not match the independently recomputed real state",
               record["status"] == "AUTHORIZATION_REJECTED" and record["reason"] == "SAFETY_STATE_FORGED")


# ======================================================================= #
# Constraint 2: claim interaction between this module's authorization_id
# claim and .27's own client_order_id claim.
# ======================================================================= #

def test_claim_interaction_authorization_succeeds_adapter_claim_fails_no_submission() -> None:
    """authorization claim succeeds -> .27's client_order_id claim fails
    -> no submission occurs, and the authorization_id claim remains
    permanently consumed (Martin constraint 2)."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        adapter_claims = tmp / "adapter_claims"
        auth_claims = tmp / "auth_claims"

        # Pre-claim the client_order_id directly against .27's own claim
        # store, simulating a prior submission attempt that already
        # consumed it.
        granted = ADAPTER.claim_client_order_id(adapter_claims, BASE_SPEC["client_order_id"])
        expect("claim-interaction: pre-claim of client_order_id succeeds", granted is True)

        record = AUTH.authorize(BASE_SPEC, config=ALLOW_ALL_CONFIG, ledger_base_dir=empty_ledger_dir(tmp), claims_dir=auth_claims)
        expect("claim-interaction: authorization itself succeeds", record["status"] == "AUTHORIZED")

        fake = FakeExchange(create_order_result={"id": "should-not-be-reached", "status": "closed"})
        result = AUTH.authorized_submit(
            record, BASE_SPEC, auth_claims, adapter_claims,
            config=ALLOW_ALL_CONFIG, ledger_base_dir=empty_ledger_dir(tmp), exchange=fake,
        )
        expect("claim-interaction: result is clearly labeled AUTHORIZED_BUT_ADAPTER_CLAIM_REJECTED, never silently stranded",
               result["status"] == "AUTHORIZED_BUT_ADAPTER_CLAIM_REJECTED")
        expect("claim-interaction: the exchange was never called -- no submission occurred", fake.create_order_calls == [])

        retry = REPLAY.claim(record["authorization_id"], BASE_SPEC["client_order_id"], claims_dir=auth_claims)
        expect("claim-interaction: the authorization_id claim remains permanently consumed, never released for retry", retry["granted"] is False)
        expect("claim-interaction: existing_intent_id resolves back to the same client_order_id this authorization was for",
               retry["existing_intent_id"] == BASE_SPEC["client_order_id"])


def test_claim_interaction_authorization_claim_already_consumed_never_calls_adapter() -> None:
    """authorization claim itself already consumed -> .27.submit() must
    never even be called."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        auth_claims = tmp / "auth_claims"
        adapter_claims = tmp / "adapter_claims"

        record = AUTH.authorize(BASE_SPEC, config=ALLOW_ALL_CONFIG, ledger_base_dir=empty_ledger_dir(tmp), claims_dir=auth_claims)
        expect("claim-interaction-2: authorization issued", record["status"] == "AUTHORIZED")

        # Pre-consume the authorization_id claim directly through v0.5.3.32
        # (the store authorized_submit() actually claims through),
        # simulating a first authorized_submit() attempt already in
        # flight/complete.
        REPLAY.claim(record["authorization_id"], BASE_SPEC["client_order_id"], claims_dir=auth_claims)

        fake = FakeExchange()
        result = AUTH.authorized_submit(
            record, BASE_SPEC, auth_claims, adapter_claims,
            config=ALLOW_ALL_CONFIG, ledger_base_dir=empty_ledger_dir(tmp), exchange=fake,
        )
        expect("claim-interaction-2: AUTHORIZATION_ALREADY_CONSUMED returned", result["status"] == "AUTHORIZATION_ALREADY_CONSUMED")
        expect("claim-interaction-2: existing_intent_id surfaced so the caller can inspect the prior intent",
               result["existing_intent_id"] == BASE_SPEC["client_order_id"])
        expect("claim-interaction-2: .27.submit() was never reached", fake.create_order_calls == [])


def test_claim_interaction_never_retried_after_execution_uncertain() -> None:
    """After an EXECUTION_UNCERTAIN outcome from .27, the authorization_id
    claim must remain permanently unreleased -- no retry path exists for
    the same authorization_id."""
    import tempfile
    import ccxt
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        auth_claims = tmp / "auth_claims"
        adapter_claims = tmp / "adapter_claims"

        record = AUTH.authorize(BASE_SPEC, config=ALLOW_ALL_CONFIG, ledger_base_dir=empty_ledger_dir(tmp), claims_dir=auth_claims)
        expect("claim-interaction-3: authorization issued", record["status"] == "AUTHORIZED")

        uncertain_fake = FakeExchange(create_order_exception=ccxt.RequestTimeout("mexc: timed out"))
        result = AUTH.authorized_submit(
            record, BASE_SPEC, auth_claims, adapter_claims,
            config=ALLOW_ALL_CONFIG, ledger_base_dir=empty_ledger_dir(tmp), exchange=uncertain_fake,
        )
        expect("claim-interaction-3: submission attempted and .27 classifies the timeout as EXECUTION_UNCERTAIN",
               result["status"] == "SUBMISSION_ATTEMPTED" and result["submission_result"]["status"] == "EXECUTION_UNCERTAIN")

        retry = REPLAY.claim(record["authorization_id"], BASE_SPEC["client_order_id"], claims_dir=auth_claims)
        expect("claim-interaction-3: the authorization_id claim is never released/retried after EXECUTION_UNCERTAIN", retry["granted"] is False)

        # A second authorized_submit() attempt with the SAME record must
        # also refuse, never re-touching the exchange.
        second_fake = FakeExchange()
        result2 = AUTH.authorized_submit(
            record, BASE_SPEC, auth_claims, adapter_claims,
            config=ALLOW_ALL_CONFIG, ledger_base_dir=empty_ledger_dir(tmp), exchange=second_fake,
        )
        expect("claim-interaction-3: a second attempt with the same authorization_id is refused",
               result2["status"] == "AUTHORIZATION_ALREADY_CONSUMED")
        expect("claim-interaction-3: the second attempt never reaches the exchange", second_fake.create_order_calls == [])


# --------------------------------------------------------------------- #
# reconciliation_health is genuinely computed, not fabricated
# --------------------------------------------------------------------- #

def test_reconciliation_health_genuinely_reflects_the_real_ledger() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        ledger_dir = empty_ledger_dir(tmp)
        LEDGER = _load("aura_v05329_mexc_intent_ledger", "aura_v05329_mexc_intent_ledger.py")

        state_clean = AUTH.assemble_safety_state(ALLOW_ALL_CONFIG, ledger_base_dir=ledger_dir, claims_dir=tmp / "claims1")
        expect("reconciliation-health: an empty ledger is HEALTHY", state_clean["reconciliation_health"]["status"] == "HEALTHY")

        LEDGER.create_intent("recon-health-1", "BTC/USDT:USDT", "buy", 1.0, "OPEN_LONG", "sha256:fp", base_dir=ledger_dir)
        LEDGER.record_claimed("recon-health-1", base_dir=ledger_dir)
        LEDGER.escalate_to_human("recon-health-1", "TEST_ESCALATION", base_dir=ledger_dir)

        state_degraded = AUTH.assemble_safety_state(ALLOW_ALL_CONFIG, ledger_base_dir=ledger_dir, claims_dir=tmp / "claims2")
        expect("reconciliation-health: an escalated intent genuinely degrades health (not fabricated)",
               state_degraded["reconciliation_health"]["status"] == "DEGRADED" and state_degraded["reconciliation_health"]["escalated_count"] == 1)

        record = AUTH.authorize(BASE_SPEC, config=ALLOW_ALL_CONFIG, ledger_base_dir=ledger_dir, claims_dir=tmp / "claims3")
        expect("reconciliation-health: authorization is blocked while reconciliation is degraded",
               record["status"] == "AUTHORIZATION_REJECTED" and record["reason"] == "RECONCILIATION_NOT_HEALTHY")


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_")]
    for t in tests:
        t()
    print(f"AURA v0.5.3.31 UNIT TEST CONTRACT: ALL {len(tests)} PASS")
