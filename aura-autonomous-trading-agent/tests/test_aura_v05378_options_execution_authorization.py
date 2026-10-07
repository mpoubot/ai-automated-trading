#!/usr/bin/env python3
"""Tests for AURA v0.5.3.78 -- Options Execution Authorization (O5a).

No live Alpaca call anywhere in this file. `.376`'s `validate_vertical_spec()`
is exercised for real (not mocked) -- that is the one thing this module
reuses rather than reimplements, so tests must prove the real integration
works, not a stand-in for it.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aura_v05378_options_execution_authorization as AUTH  # noqa: E402

AUTHORIZED_CFG = {"kill_switch": False, "execution_authorized": True, "paper_execution_authorized": True}
EXPIRY = date.today() + timedelta(days=30)


def make_spec(client_order_id="co-1", qty=1, limit_price="1.50", order_type="LIMIT",
              time_in_force="DAY", expiry=EXPIRY, **extra):
    spec = {
        "legs": [
            {"underlying_symbol": "SPY", "strike": "450", "expiry": expiry, "right": "CALL", "direction": "OPEN_LONG"},
            {"underlying_symbol": "SPY", "strike": "455", "expiry": expiry, "right": "CALL", "direction": "OPEN_SHORT"},
        ],
        "order_type": order_type,
        "limit_price": limit_price,
        "qty": qty,
        "client_order_id": client_order_id,
        "time_in_force": time_in_force,
    }
    spec.update(extra)
    return spec


# ======================================================================
# authorize() -- happy path and field-level content
# ======================================================================

def test_authorize_happy_path_fields(tmp_path):
    record = AUTH.authorize(make_spec(), environment="PAPER", config=AUTHORIZED_CFG, claims_dir=tmp_path)
    assert record["status"] == "AUTHORIZED"
    assert record["venue"] == "ALPACA"
    assert record["asset_class"] == "OPTION"
    assert record["order_class"] == "MLEG"
    assert record["underlying_symbol"] == "SPY"
    assert len(record["legs"]) == 2
    assert record["qty"] == "1"
    assert record["order_type"] == "LIMIT"
    assert record["limit_price"] == "1.50"
    assert record["time_in_force"] == "DAY"
    assert record["client_order_id"] == "co-1"
    assert record["authorization_id"].startswith("OPTIONS-VERTICAL-AUTH-")
    ok, errs = AUTH.verify_authorization_record(record)
    assert ok, errs


def test_authorize_legs_have_correct_side_and_position_intent(tmp_path):
    record = AUTH.authorize(make_spec(), environment="PAPER", config=AUTHORIZED_CFG, claims_dir=tmp_path)
    long_leg = next(leg for leg in record["legs"] if leg["direction"] == "OPEN_LONG")
    short_leg = next(leg for leg in record["legs"] if leg["direction"] == "OPEN_SHORT")
    assert long_leg["side"] == "buy"
    assert long_leg["position_intent"] == "buy_to_open"
    assert short_leg["side"] == "sell"
    assert short_leg["position_intent"] == "sell_to_open"


def test_authorize_decision_provenance_passthrough_never_affects_outcome(tmp_path):
    """Mirrors .336's item 3: source of a decision confers no execution
    authority. All three source kinds authorize identically."""
    results = {}
    for source_kind in ("DETERMINISTIC_SIGNAL", "AI_PROPOSAL", "HUMAN_OVERRIDE"):
        spec = make_spec(client_order_id=f"co-{source_kind}", decision_id="d1", strategy_id="s1",
                          strategy_version="v1", source_kind=source_kind)
        record = AUTH.authorize(spec, environment="PAPER", config=AUTHORIZED_CFG, claims_dir=tmp_path)
        results[source_kind] = record
    assert all(r["status"] == "AUTHORIZED" for r in results.values())
    assert results["AI_PROPOSAL"]["source_kind"] == "AI_PROPOSAL"
    assert results["DETERMINISTIC_SIGNAL"]["decision_id"] == "d1"


def test_authorize_decision_provenance_omitted_defaults_to_none(tmp_path):
    record = AUTH.authorize(make_spec(), environment="PAPER", config=AUTHORIZED_CFG, claims_dir=tmp_path)
    assert record["decision_id"] is None
    assert record["strategy_id"] is None
    assert record["strategy_version"] is None
    assert record["source_kind"] is None


# ======================================================================
# authorize() -- rejections
# ======================================================================

def test_authorize_rejects_non_dict_spec(tmp_path):
    record = AUTH.authorize("not a dict", environment="PAPER", config=AUTHORIZED_CFG, claims_dir=tmp_path)
    assert record["status"] == "AUTHORIZATION_REJECTED"
    assert record["reason"] == "INVALID_EXECUTION_SPEC"


def test_authorize_rejects_live_environment(tmp_path):
    record = AUTH.authorize(make_spec(), environment="LIVE", config=AUTHORIZED_CFG, claims_dir=tmp_path)
    assert record["status"] == "AUTHORIZATION_REJECTED"
    assert record["reason"] == "LIVE_EXECUTION_NOT_SUPPORTED_FOR_OPTIONS"


def test_authorize_rejects_unsupported_environment_string(tmp_path):
    record = AUTH.authorize(make_spec(), environment="SANDBOX", config=AUTHORIZED_CFG, claims_dir=tmp_path)
    assert record["status"] == "AUTHORIZATION_REJECTED"
    assert record["reason"] == "UNSUPPORTED_ENVIRONMENT"


def test_authorize_rejects_kill_switch_engaged(tmp_path):
    record = AUTH.authorize(make_spec(), environment="PAPER", config={"kill_switch": True}, claims_dir=tmp_path)
    assert record["status"] == "AUTHORIZATION_REJECTED"
    assert record["reason"] == "KILL_SWITCH_ENGAGED"


def test_authorize_rejects_execution_not_authorized(tmp_path):
    cfg = {"kill_switch": False, "execution_authorized": False}
    record = AUTH.authorize(make_spec(), environment="PAPER", config=cfg, claims_dir=tmp_path)
    assert record["status"] == "AUTHORIZATION_REJECTED"
    assert record["reason"] == "SAFETY_STATE_NOT_AUTHORIZED"


def test_authorize_rejects_paper_not_authorized(tmp_path):
    cfg = {"kill_switch": False, "execution_authorized": True, "paper_execution_authorized": False}
    record = AUTH.authorize(make_spec(), environment="PAPER", config=cfg, claims_dir=tmp_path)
    assert record["status"] == "AUTHORIZATION_REJECTED"
    assert record["reason"] == "PAPER_EXECUTION_NOT_AUTHORIZED"


def test_authorize_rejects_unusable_claims_dir(tmp_path):
    blocked = tmp_path / "blocked"
    blocked.write_text("i am a file, not a directory")
    record = AUTH.authorize(make_spec(), environment="PAPER", config=AUTHORIZED_CFG, claims_dir=blocked)
    assert record["status"] == "AUTHORIZATION_REJECTED"
    assert record["reason"] == "REPLAY_PROTECTION_NOT_HEALTHY"


def test_authorize_rejects_already_expired_spec(tmp_path):
    past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    record = AUTH.authorize(make_spec(expires_at=past), environment="PAPER", config=AUTHORIZED_CFG, claims_dir=tmp_path)
    assert record["status"] == "AUTHORIZATION_REJECTED"
    assert record["reason"] == "EXPIRED_EXECUTION_SPEC"


def test_authorize_rejects_invalid_expires_at(tmp_path):
    record = AUTH.authorize(make_spec(expires_at="not-a-timestamp"), environment="PAPER", config=AUTHORIZED_CFG, claims_dir=tmp_path)
    assert record["status"] == "AUTHORIZATION_REJECTED"
    assert record["reason"] == "INVALID_EXPIRES_AT"


@pytest.mark.parametrize("mutation,expected_substring", [
    (lambda s: s["legs"].append(dict(s["legs"][0])), "V1_SCOPE_REQUIRES_EXACTLY_2_LEGS"),
    (lambda s: s["legs"].__setitem__(1, {**s["legs"][1], "right": "PUT"}), "NOT_A_VERTICAL_MIXED_RIGHTS"),
    (lambda s: s["legs"].__setitem__(1, {**s["legs"][1], "strike": "450"}), "NOT_A_VERTICAL_IDENTICAL_STRIKES"),
    (lambda s: s["legs"].__setitem__(1, {**s["legs"][1], "direction": "OPEN_LONG"}), "NOT_A_VERTICAL_REQUIRES_ONE_LONG_ONE_SHORT_LEG"),
    (lambda s: s.__setitem__("order_type", "MARKET"), "V1_SCOPE_LIMIT_ORDERS_ONLY"),
    (lambda s: s.__setitem__("limit_price", "not-a-number"), "INVALID_LIMIT_PRICE"),
    (lambda s: s.__setitem__("qty", 0), "INVALID_QTY"),
    (lambda s: s.__setitem__("client_order_id", ""), "MISSING_CLIENT_ORDER_ID"),
    (lambda s: s.__setitem__("time_in_force", "FOK"), "INVALID_TIME_IN_FORCE"),
])
def test_authorize_rejects_every_structural_violation_via_o4_reuse(tmp_path, mutation, expected_substring):
    """Confirms .378 genuinely reuses .376.validate_vertical_spec() --
    not a reimplementation that could silently drift from it."""
    spec = make_spec()
    mutation(spec)
    record = AUTH.authorize(spec, environment="PAPER", config=AUTHORIZED_CFG, claims_dir=tmp_path)
    assert record["status"] == "AUTHORIZATION_REJECTED"
    assert record["reason"] == "STRUCTURE_VALIDATION_FAILED"
    assert expected_substring in record["detail"]


# ======================================================================
# Caller-supplied safety_state -- never trusted as authoritative
# ======================================================================

def test_authorize_accepts_genuinely_matching_caller_safety_state(tmp_path):
    fresh = AUTH.assemble_safety_state(AUTHORIZED_CFG, claims_dir=tmp_path)
    record = AUTH.authorize(make_spec(), environment="PAPER", safety_state=fresh, config=AUTHORIZED_CFG, claims_dir=tmp_path)
    assert record["status"] == "AUTHORIZED"


def test_authorize_rejects_tampered_safety_state(tmp_path):
    fresh = AUTH.assemble_safety_state(AUTHORIZED_CFG, claims_dir=tmp_path)
    tampered = dict(fresh)
    tampered["kill_switch"] = True  # mutated without recomputing hash
    record = AUTH.authorize(make_spec(), environment="PAPER", safety_state=tampered, config=AUTHORIZED_CFG, claims_dir=tmp_path)
    assert record["status"] == "AUTHORIZATION_REJECTED"
    assert record["reason"] == "SAFETY_STATE_FINGERPRINT_MISMATCH"


def test_authorize_rejects_stale_safety_state(tmp_path):
    old_moment = datetime.now(timezone.utc) - timedelta(seconds=120)
    stale = AUTH.assemble_safety_state(AUTHORIZED_CFG, claims_dir=tmp_path, now=old_moment)
    record = AUTH.authorize(make_spec(), environment="PAPER", safety_state=stale, config=AUTHORIZED_CFG, claims_dir=tmp_path)
    assert record["status"] == "AUTHORIZATION_REJECTED"
    assert record["reason"] == "STALE_SAFETY_STATE"


def test_authorize_rejects_forged_safety_state_content(tmp_path):
    fresh = AUTH.assemble_safety_state({"kill_switch": True}, claims_dir=tmp_path)
    # Caller claims kill_switch is False but their own supplied record
    # says True -- rebuild a self-consistent-but-wrong record.
    forged_content = dict(fresh)
    forged_content["kill_switch"] = False
    forged_content["safety_state_hash"] = AUTH._fingerprint(AUTH._safety_state_content(forged_content))
    record = AUTH.authorize(make_spec(), environment="PAPER", safety_state=forged_content, config={"kill_switch": True}, claims_dir=tmp_path)
    assert record["status"] == "AUTHORIZATION_REJECTED"
    assert record["reason"] == "SAFETY_STATE_FORGED"


def test_authorize_rejects_malformed_safety_state(tmp_path):
    malformed = {"schema_version": "1.0"}
    malformed["safety_state_hash"] = AUTH._fingerprint(AUTH._safety_state_content(malformed))
    record = AUTH.authorize(make_spec(), environment="PAPER", safety_state=malformed, config=AUTHORIZED_CFG, claims_dir=tmp_path)
    assert record["status"] == "AUTHORIZATION_REJECTED"
    assert record["reason"] == "MALFORMED_SAFETY_STATE"


# ======================================================================
# revalidate_before_submission()
# ======================================================================

def test_revalidate_happy_path(tmp_path):
    spec = make_spec()
    record = AUTH.authorize(spec, environment="PAPER", config=AUTHORIZED_CFG, claims_dir=tmp_path)
    result = AUTH.revalidate_before_submission(record, spec, environment="PAPER", config=AUTHORIZED_CFG, claims_dir=tmp_path)
    assert result["status"] == "REVALIDATED"


def test_revalidate_rejects_tampered_record(tmp_path):
    spec = make_spec()
    record = AUTH.authorize(spec, environment="PAPER", config=AUTHORIZED_CFG, claims_dir=tmp_path)
    tampered = dict(record)
    tampered["qty"] = "999"
    result = AUTH.revalidate_before_submission(tampered, spec, environment="PAPER", config=AUTHORIZED_CFG, claims_dir=tmp_path)
    assert result["status"] == "AUTHORIZATION_REJECTED"
    assert result["reason"] == "AUTHORIZATION_RECORD_TAMPERED"


def test_revalidate_rejects_non_authorized_status(tmp_path):
    spec = make_spec()
    record = AUTH.authorize(spec, environment="PAPER", config=AUTHORIZED_CFG, claims_dir=tmp_path)
    revoked = dict(record)
    revoked["status"] = "REVOKED"
    revoked["authorization_hash"] = AUTH._fingerprint({k: v for k, v in revoked.items() if k != "authorization_hash"})
    result = AUTH.revalidate_before_submission(revoked, spec, environment="PAPER", config=AUTHORIZED_CFG, claims_dir=tmp_path)
    assert result["status"] == "AUTHORIZATION_REJECTED"
    assert result["reason"] == "AUTHORIZATION_NOT_VALID"


def test_revalidate_rejects_expired_authorization(tmp_path):
    spec = make_spec()
    record = AUTH.authorize(spec, environment="PAPER", config=AUTHORIZED_CFG, claims_dir=tmp_path)
    expired = dict(record)
    expired["expires_at"] = AUTH._iso(datetime.now(timezone.utc) - timedelta(seconds=1))
    expired["authorization_hash"] = AUTH._fingerprint({k: v for k, v in expired.items() if k != "authorization_hash"})
    result = AUTH.revalidate_before_submission(expired, spec, environment="PAPER", config=AUTHORIZED_CFG, claims_dir=tmp_path)
    assert result["status"] == "AUTHORIZATION_REJECTED"
    assert result["reason"] == "EXPIRED_AUTHORIZATION"


def test_revalidate_rejects_environment_mismatch(tmp_path):
    spec = make_spec()
    record = AUTH.authorize(spec, environment="PAPER", config=AUTHORIZED_CFG, claims_dir=tmp_path)
    result = AUTH.revalidate_before_submission(record, spec, environment="LIVE", config=AUTHORIZED_CFG, claims_dir=tmp_path)
    assert result["status"] == "AUTHORIZATION_REJECTED"
    assert result["reason"] == "ENVIRONMENT_MISMATCH"


@pytest.mark.parametrize("mutate", [
    lambda s: s.__setitem__("qty", 5),
    lambda s: s.__setitem__("limit_price", "9.99"),
    lambda s: s.__setitem__("client_order_id", "co-DIFFERENT"),
    lambda s: s.__setitem__("time_in_force", "GTC"),
])
def test_revalidate_rejects_every_execution_critical_field_change(tmp_path, mutate):
    """Confirms the single execution_spec_fingerprint check already
    catches every execution-critical field, mirroring .336's own
    single-spec_fingerprint-check design (module docstring item 0)."""
    spec = make_spec()
    record = AUTH.authorize(spec, environment="PAPER", config=AUTHORIZED_CFG, claims_dir=tmp_path)
    mutated = make_spec()
    mutate(mutated)
    result = AUTH.revalidate_before_submission(record, mutated, environment="PAPER", config=AUTHORIZED_CFG, claims_dir=tmp_path)
    assert result["status"] == "AUTHORIZATION_REJECTED"
    assert result["reason"] in ("EXECUTION_SPEC_FINGERPRINT_MISMATCH", "CLIENT_ORDER_ID_MISMATCH")


def test_revalidate_leg_direction_mismatch_defense_in_depth(tmp_path):
    """Structurally unreachable via spec mutation alone (fingerprint
    always catches it first) -- reached here only by tampering with
    record['legs'] directly while recomputing the hash, proving the
    defense-in-depth check fires on its own rejection reason."""
    spec = make_spec()
    record = AUTH.authorize(spec, environment="PAPER", config=AUTHORIZED_CFG, claims_dir=tmp_path)
    tampered = dict(record)
    tampered["legs"] = [dict(leg) for leg in record["legs"]]
    tampered["legs"][0]["side"] = "sell"  # flip a leg's recorded side
    tampered["authorization_hash"] = AUTH._fingerprint({k: v for k, v in tampered.items() if k != "authorization_hash"})
    result = AUTH.revalidate_before_submission(tampered, spec, environment="PAPER", config=AUTHORIZED_CFG, claims_dir=tmp_path)
    assert result["status"] == "AUTHORIZATION_REJECTED"
    assert result["reason"] == "LEG_DIRECTION_MISMATCH"


# ======================================================================
# authorized_order_request() -- end-to-end integration with .379
# ======================================================================

def test_authorized_order_request_happy_path(tmp_path):
    spec = make_spec()
    record = AUTH.authorize(spec, environment="PAPER", config=AUTHORIZED_CFG, claims_dir=tmp_path)
    result = AUTH.authorized_order_request(record, spec, claims_dir=tmp_path, environment="PAPER", config=AUTHORIZED_CFG)
    assert result["status"] == "AUTHORIZED_ORDER_REQUEST_READY"
    assert result["order_spec"]["order_class"] == "MLEG"
    assert result["alpaca_order_request"].symbol is None  # top-level symbol is None for MLEG, per O4
    assert len(result["alpaca_order_request"].legs) == 2


def test_authorized_order_request_second_call_blocked_as_already_consumed(tmp_path):
    spec = make_spec()
    record = AUTH.authorize(spec, environment="PAPER", config=AUTHORIZED_CFG, claims_dir=tmp_path)
    first = AUTH.authorized_order_request(record, spec, claims_dir=tmp_path, environment="PAPER", config=AUTHORIZED_CFG)
    assert first["status"] == "AUTHORIZED_ORDER_REQUEST_READY"
    second = AUTH.authorized_order_request(record, spec, claims_dir=tmp_path, environment="PAPER", config=AUTHORIZED_CFG)
    assert second["status"] == "AUTHORIZATION_ALREADY_CONSUMED"
    assert second["reason"] == "ALREADY_CLAIMED"
    assert second["order_spec"] is None
    assert second["alpaca_order_request"] is None


def test_authorized_order_request_failed_revalidation_never_claims(tmp_path):
    spec = make_spec()
    record = AUTH.authorize(spec, environment="PAPER", config=AUTHORIZED_CFG, claims_dir=tmp_path)
    mutated = make_spec(qty=99)
    result = AUTH.authorized_order_request(record, mutated, claims_dir=tmp_path, environment="PAPER", config=AUTHORIZED_CFG)
    assert result["status"] == "REVALIDATION_FAILED"
    # Since nothing was claimed, the SAME record can still be consumed
    # against the ORIGINAL, unmutated spec afterwards.
    result2 = AUTH.authorized_order_request(record, spec, claims_dir=tmp_path, environment="PAPER", config=AUTHORIZED_CFG)
    assert result2["status"] == "AUTHORIZED_ORDER_REQUEST_READY"


def test_authorized_order_request_rejects_non_authorized_record(tmp_path):
    bogus = {"status": "AUTHORIZATION_REJECTED", "authorization_id": None}
    result = AUTH.authorized_order_request(bogus, make_spec(), claims_dir=tmp_path, environment="PAPER", config=AUTHORIZED_CFG)
    assert result["status"] == "REVALIDATION_FAILED"
    assert result["reason"] == "AUTHORIZATION_NOT_VALID"


def test_authorized_order_request_claim_store_unreachable(tmp_path):
    spec = make_spec()
    claims_dir = tmp_path / "claims"
    record = AUTH.authorize(spec, environment="PAPER", config=AUTHORIZED_CFG, claims_dir=claims_dir)
    assert record["status"] == "AUTHORIZED"
    if os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0):
        pytest.skip("permission-based unreachable simulation is POSIX-specific and ineffective for root")
    os.chmod(claims_dir, 0o500)  # read+execute only, no write -- os.open(O_CREAT) inside .379 will fail
    try:
        result = AUTH.authorized_order_request(record, spec, claims_dir=claims_dir, environment="PAPER", config=AUTHORIZED_CFG)
        assert result["status"] == "CLAIM_STORE_UNREACHABLE"
    finally:
        os.chmod(claims_dir, 0o700)


def test_authorized_order_request_never_calls_submit(tmp_path, monkeypatch):
    spec = make_spec()
    record = AUTH.authorize(spec, environment="PAPER", config=AUTHORIZED_CFG, claims_dir=tmp_path)
    adapter = AUTH._load_adapter_module()

    def _poison(*args, **kwargs):
        raise AssertionError(".378 must never call .376.submit()")

    monkeypatch.setattr(adapter, "submit", _poison)
    result = AUTH.authorized_order_request(record, spec, claims_dir=tmp_path, environment="PAPER", config=AUTHORIZED_CFG)
    assert result["status"] == "AUTHORIZED_ORDER_REQUEST_READY"


# ======================================================================
# Module-level disclosure / structural safety properties
# ======================================================================

def test_network_capable_functions_is_empty():
    assert AUTH.NETWORK_CAPABLE_FUNCTIONS == frozenset()


def test_allowed_environments_has_only_paper():
    assert AUTH.ALLOWED_ENVIRONMENTS == frozenset({"PAPER"})


def test_no_claim_authorization_legacy_primitive_exists():
    """Deliberate simplification vs .336 (module docstring item 3) -- no
    provisional marker-file primitive carried into this module."""
    assert not hasattr(AUTH, "claim_authorization")


def test_module_has_no_live_credentials_or_network_imports():
    source = Path(AUTH.__file__).read_text(encoding="utf-8")
    for token in ("requests.", "urlopen", "TradingClient(", "OptionHistoricalDataClient(", "StockHistoricalDataClient("):
        assert token not in source, f"unexpected live/network construction token found: {token}"


# ======================================================================
# Concurrency -- exactly one of N racing authorized_order_request()
# calls on the SAME record wins (mirrors the proven MEXC/.337 pattern)
# ======================================================================

def test_concurrent_authorized_order_request_exactly_one_wins(tmp_path):
    import threading

    spec = make_spec()
    record = AUTH.authorize(spec, environment="PAPER", config=AUTHORIZED_CFG, claims_dir=tmp_path)
    results = []
    lock = threading.Lock()

    def attempt():
        r = AUTH.authorized_order_request(record, spec, claims_dir=tmp_path, environment="PAPER", config=AUTHORIZED_CFG)
        with lock:
            results.append(r["status"])

    threads = [threading.Thread(target=attempt) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count("AUTHORIZED_ORDER_REQUEST_READY") == 1
    assert results.count("AUTHORIZATION_ALREADY_CONSUMED") == 11
