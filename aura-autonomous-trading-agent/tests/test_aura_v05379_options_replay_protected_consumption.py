#!/usr/bin/env python3
"""Tests for AURA v0.5.3.79 -- Options Replay-Protected Consumption (O5b).

No live Alpaca call anywhere in this file.
"""
from __future__ import annotations

import os
import sys
import threading
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aura_v05378_options_execution_authorization as AUTH  # noqa: E402
import aura_v05379_options_replay_protected_consumption as REPLAY  # noqa: E402

AUTHORIZED_CFG = {"kill_switch": False, "execution_authorized": True, "paper_execution_authorized": True}
EXPIRY = date.today() + timedelta(days=30)


def make_spec(client_order_id="co-1"):
    return {
        "legs": [
            {"underlying_symbol": "SPY", "strike": "450", "expiry": EXPIRY, "right": "CALL", "direction": "OPEN_LONG"},
            {"underlying_symbol": "SPY", "strike": "455", "expiry": EXPIRY, "right": "CALL", "direction": "OPEN_SHORT"},
        ],
        "order_type": "LIMIT",
        "limit_price": "1.50",
        "qty": 1,
        "client_order_id": client_order_id,
        "time_in_force": "DAY",
    }


def make_record(tmp_path, client_order_id="co-1"):
    spec = make_spec(client_order_id)
    return AUTH.authorize(spec, environment="PAPER", config=AUTHORIZED_CFG, claims_dir=tmp_path)


# ======================================================================
# claim_path()
# ======================================================================

def test_claim_path_rejects_invalid_authorization_id(tmp_path):
    with pytest.raises(RuntimeError, match="INVALID_AUTHORIZATION_ID"):
        REPLAY.claim_path("not valid!! id", claims_dir=tmp_path)


def test_claim_path_accepts_real_authorization_id_shape(tmp_path):
    path = REPLAY.claim_path("OPTIONS-VERTICAL-AUTH-abc123", claims_dir=tmp_path)
    assert path.name == "OPTIONS-VERTICAL-AUTH-abc123.json"


# ======================================================================
# canonical_claim_hash() / verify_claim()
# ======================================================================

def test_verify_claim_self_consistent_after_build(tmp_path):
    record = make_record(tmp_path)
    built = REPLAY._build_claim_record(record, REPLAY.now())
    ok, errs = REPLAY.verify_claim(built)
    assert ok, errs


def test_verify_claim_detects_tampering(tmp_path):
    record = make_record(tmp_path)
    built = REPLAY._build_claim_record(record, REPLAY.now())
    tampered = dict(built)
    tampered["qty"] = "999"
    ok, errs = REPLAY.verify_claim(tampered)
    assert not ok
    assert "CLAIM_HASH_MISMATCH" in errs


def test_verify_claim_rejects_non_dict():
    ok, errs = REPLAY.verify_claim("nope")
    assert not ok
    assert errs == ["NOT_A_DICT"]


# ======================================================================
# get_claim() -- fail-closed on read
# ======================================================================

def test_get_claim_missing_file_returns_none(tmp_path):
    assert REPLAY.get_claim("OPTIONS-VERTICAL-AUTH-does-not-exist", claims_dir=tmp_path) is None


def test_get_claim_corrupt_json_fails_closed(tmp_path):
    record = make_record(tmp_path)
    REPLAY.claim(record, claims_dir=tmp_path)
    path = REPLAY.claim_path(record["authorization_id"], claims_dir=tmp_path)
    path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="CLAIM_RECORD_UNPARSEABLE"):
        REPLAY.get_claim(record["authorization_id"], claims_dir=tmp_path)


def test_get_claim_tampered_content_fails_closed(tmp_path):
    record = make_record(tmp_path)
    REPLAY.claim(record, claims_dir=tmp_path)
    path = REPLAY.claim_path(record["authorization_id"], claims_dir=tmp_path)
    import json
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["qty"] = "999"  # mutate without fixing claim_hash
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(RuntimeError, match="CLAIM_RECORD_TAMPERED"):
        REPLAY.get_claim(record["authorization_id"], claims_dir=tmp_path)


def test_get_claim_empty_file_fails_closed(tmp_path):
    record = make_record(tmp_path)
    REPLAY.claim(record, claims_dir=tmp_path)
    path = REPLAY.claim_path(record["authorization_id"], claims_dir=tmp_path)
    path.write_text("", encoding="utf-8")
    with pytest.raises(RuntimeError, match="CLAIM_RECORD_EMPTY"):
        REPLAY.get_claim(record["authorization_id"], claims_dir=tmp_path)


# ======================================================================
# claim() -- the one authoritative operation
# ======================================================================

def test_claim_happy_path_granted(tmp_path):
    record = make_record(tmp_path)
    result = REPLAY.claim(record, claims_dir=tmp_path)
    assert result["granted"] is True
    assert result["client_order_id"] == "co-1"
    assert result["reason"] is None


def test_claim_persists_full_options_identity(tmp_path):
    record = make_record(tmp_path)
    REPLAY.claim(record, claims_dir=tmp_path)
    persisted = REPLAY.get_claim(record["authorization_id"], claims_dir=tmp_path)
    assert persisted["order_class"] == "MLEG"
    assert persisted["underlying_symbol"] == "SPY"
    assert len(persisted["legs"]) == 2
    assert persisted["execution_spec_fingerprint"] == record["execution_spec_fingerprint"]


def test_claim_rejects_non_dict_record():
    result = REPLAY.claim("not a dict")
    assert result["granted"] is False
    assert result["reason"] == "INVALID_AUTHORIZATION_RECORD"


def test_claim_rejects_tampered_authorization_record(tmp_path):
    record = make_record(tmp_path)
    tampered = dict(record)
    tampered["qty"] = "999"
    result = REPLAY.claim(tampered, claims_dir=tmp_path)
    assert result["granted"] is False
    assert result["reason"] == "AUTHORIZATION_RECORD_TAMPERED"


def test_claim_rejects_non_authorized_status(tmp_path):
    record = make_record(tmp_path)
    revoked = dict(record)
    revoked["status"] = "REVOKED"
    revoked["authorization_hash"] = AUTH._fingerprint({k: v for k, v in revoked.items() if k != "authorization_hash"})
    result = REPLAY.claim(revoked, claims_dir=tmp_path)
    assert result["granted"] is False
    assert result["reason"] == "AUTHORIZATION_NOT_VALID"


def test_claim_second_attempt_on_same_authorization_id_denied(tmp_path):
    record = make_record(tmp_path)
    first = REPLAY.claim(record, claims_dir=tmp_path)
    assert first["granted"] is True
    second = REPLAY.claim(record, claims_dir=tmp_path)
    assert second["granted"] is False
    assert second["reason"] == "ALREADY_CLAIMED"
    assert second["existing_client_order_id"] == "co-1"
    assert second["existing_claim"] is not None


def test_claim_detects_authorization_id_reused_with_mismatched_content(tmp_path):
    record = make_record(tmp_path)
    REPLAY.claim(record, claims_dir=tmp_path)
    # Forge a second record with the SAME authorization_id but different
    # (self-consistent) content.
    forged = dict(record)
    forged["qty"] = "7"
    forged["authorization_hash"] = AUTH._fingerprint({k: v for k, v in forged.items() if k != "authorization_hash"})
    result = REPLAY.claim(forged, claims_dir=tmp_path)
    assert result["granted"] is False
    assert result["reason"] == "AUTHORIZATION_ID_REUSED_WITH_MISMATCHED_CONTENT"


def test_claim_store_unreachable(tmp_path):
    if os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0):
        pytest.skip("permission-based unreachable simulation is POSIX-specific and ineffective for root")
    record = make_record(tmp_path)
    claims_dir = tmp_path / "unreachable"
    claims_dir.mkdir()
    os.chmod(claims_dir, 0o500)
    try:
        result = REPLAY.claim(record, claims_dir=claims_dir / "nested")
        assert result["granted"] is False
        assert result["reason"] == "CLAIM_STORE_UNREACHABLE"
    finally:
        os.chmod(claims_dir, 0o700)


def test_uncertain_downstream_outcome_does_not_release_the_claim(tmp_path):
    """This module has no concept of a downstream submission outcome at
    all -- simulating "the order later came back uncertain" changes
    nothing: a second claim attempt is still refused identically."""
    record = make_record(tmp_path)
    first = REPLAY.claim(record, claims_dir=tmp_path)
    assert first["granted"] is True
    # No release/unclaim function exists to call here, by design (see
    # the next test) -- the only thing left to prove is that nothing
    # implicitly un-consumes it.
    second = REPLAY.claim(record, claims_dir=tmp_path)
    assert second["granted"] is False
    assert second["reason"] == "ALREADY_CLAIMED"


# ======================================================================
# Structural safety properties
# ======================================================================

def test_module_exposes_no_release_or_delete_function():
    forbidden_names = {"release", "release_claim", "unclaim", "delete_claim", "revoke", "free"}
    exposed = set(dir(REPLAY))
    assert not (forbidden_names & exposed)


def test_network_capable_functions_is_empty():
    assert REPLAY.NETWORK_CAPABLE_FUNCTIONS == frozenset()


def test_module_has_no_live_credentials_or_network_imports():
    source = Path(REPLAY.__file__).read_text(encoding="utf-8")
    for token in ("requests.", "urlopen", "TradingClient(", "OptionHistoricalDataClient(", "StockHistoricalDataClient("):
        assert token not in source, f"unexpected live/network construction token found: {token}"


# ======================================================================
# Concurrency -- exactly one of N racing claim() calls on the SAME
# authorization_id wins (mirrors the proven MEXC/.337 pattern)
# ======================================================================

def test_concurrent_claim_exactly_one_wins(tmp_path):
    record = make_record(tmp_path)
    results = []
    lock = threading.Lock()

    def attempt():
        r = REPLAY.claim(record, claims_dir=tmp_path)
        with lock:
            results.append(r["granted"])

    threads = [threading.Thread(target=attempt) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(True) == 1
    assert results.count(False) == 15
