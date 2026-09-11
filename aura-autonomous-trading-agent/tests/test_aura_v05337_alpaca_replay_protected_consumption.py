#!/usr/bin/env python3
"""Unit + integration tests for AURA v0.5.3.37 (Alpaca Replay-Protected
Consumption).

Covers Martin's full explicit .37 test list: first authorization
consumption succeeds; second consumption rejected; duplicate returns
original execution identity; concurrent claims (exactly one winner) and
concurrent losers see the winning identity; process/restart persistence;
authorization_id-reuse-with-mismatched-content detection for every bound
field (venue/environment/symbol/direction/position_intent/spec_fingerprint/
client_order_id); malformed persisted record; tampered persisted record;
corrupted hash-chain/integrity data (an unverifiable existing claim under
a racing second attempt); uncertain downstream execution does NOT release
the claim; rejected authorization does not create a consumed claim;
different authorization_ids consumed independently; same client_order_id
with different authorization_id follows the correct (per-authorization_id)
contract; deterministic record/fingerprint behaviour; no release/delete
function anywhere in the module (mirrors .32's own structural test) --
plus integration tests proving the real .33 -> .34(stand-in) -> .36 -> .37
-> .35 chain for STOCK/ETF x LONG/SHORT, preserving CLOSE_SHORT not
requiring fresh borrow evidence, and an AI-sourced decision passing
through the identical chain as a deterministic signal.

Repo root is on sys.path when running under pytest from the repo root
(same convention already established by .35/.36's own test files), so
plain `import aura_v0533x_...` here resolves to the SAME cached module
objects the modules under test dynamically import internally.

No real credentials, no network call, no actual Alpaca order submission
anywhere in this file.
"""
from __future__ import annotations

import importlib.util
import json
import threading
from pathlib import Path

import aura_v05333_canonical_execution_specification as CANON
import aura_v05336_alpaca_equity_execution_authorization as AUTH
import aura_v05337_alpaca_replay_protected_consumption as REPLAY

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def expect(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"PASS: {name}")


# ======================================================================= #
# Fixtures / builders -- same shape as test_aura_v05336's own, so a real,
# self-consistent .36 AuthorizationRecord can be built end to end.
# ======================================================================= #

SIGNAL_TS = "2026-09-11T12:00:00+00:00"


def make_spec(
    *,
    asset_class: str = "STOCK",
    direction: str = "OPEN_LONG",
    symbol: str = "AAPL",
    quantity="10",
    order_type: str = "MARKET",
    limit_price=None,
    venue: str = "ALPACA",
    decision_id: str = "DEC-1",
    strategy_id: str = "STRAT-1",
    strategy_version: str = "v1.0",
    source_kind: str = "DETERMINISTIC_SIGNAL",
    signal_timestamp: str = SIGNAL_TS,
    expires_at=None,
) -> dict:
    return CANON.build_canonical_execution_specification(
        asset_class=asset_class,
        venue=venue,
        direction=direction,
        symbol=symbol,
        quantity=quantity,
        decision_id=decision_id,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        signal_timestamp=signal_timestamp,
        order_type=order_type,
        limit_price=limit_price,
        source_kind=source_kind,
        expires_at=expires_at,
    )


def make_asset(
    *,
    symbol: str = "AAPL",
    tradable=True,
    shortable=True,
    easy_to_borrow=True,
    fractionable=True,
) -> dict:
    return {
        "symbol": symbol,
        "asset_class": "us_equity",
        "exchange": "NASDAQ",
        "status": "active",
        "tradable": tradable,
        "shortable": shortable,
        "easy_to_borrow": easy_to_borrow,
        "fractionable": fractionable,
        "marginable": True,
        "min_order_size": None,
        "min_trade_increment": None,
        "price_increment": None,
    }


def authorized_config(**overrides) -> dict:
    config = {
        "kill_switch": False,
        "execution_authorized": True,
        "paper_execution_authorized": True,
        "authorization_ttl_seconds": 60,
        "safety_state_ttl_seconds": 60,
    }
    config.update(overrides)
    return config


def auth_claims(tmp_path: Path) -> Path:
    return tmp_path / "auth-claims"


def replay_claims(tmp_path: Path) -> Path:
    return tmp_path / "replay-claims"


def make_authorized_record(tmp_path: Path, **spec_overrides) -> dict:
    """Builds a REAL, self-consistent, AUTHORIZED .36 AuthorizationRecord
    via AUTH.authorize() -- never a hand-built stand-in -- so .37's tests
    exercise the actual contract .37.claim() depends on."""
    asset_kwargs = {}
    for key in ("shortable", "easy_to_borrow", "tradable", "fractionable"):
        if key in spec_overrides:
            asset_kwargs[key] = spec_overrides.pop(key)
    spec = make_spec(**spec_overrides)
    asset = make_asset(symbol=spec_overrides.get("symbol", "AAPL"), **asset_kwargs)
    record = AUTH.authorize(spec, asset, config=authorized_config(), claims_dir=auth_claims(tmp_path))
    assert record["status"] == "AUTHORIZED", record
    return record


def forged_record(base_record: dict, **overrides) -> dict:
    """Builds a second, SELF-CONSISTENT AuthorizationRecord that reuses the
    SAME authorization_id as base_record but differs in one or more bound
    fields -- the only realistic way a "same authorization_id, different
    content" scenario can arise (a deliberately-forged/re-issued record),
    used to exercise claim()'s AUTHORIZATION_ID_REUSED_WITH_MISMATCHED_
    CONTENT detection for each Martin-listed field."""
    forged = dict(base_record)
    forged.update(overrides)
    content = {k: v for k, v in forged.items() if k != "authorization_hash"}
    forged["authorization_hash"] = AUTH._fingerprint(content)
    return forged


# ======================================================================= #
# 1-2: first consumption succeeds, second is rejected
# ======================================================================= #

def test_first_claim_consumption_succeeds(tmp_path) -> None:
    record = make_authorized_record(tmp_path)
    result = REPLAY.claim(record, claims_dir=replay_claims(tmp_path))
    expect("first claim granted", result["granted"] is True)
    expect("client_order_id echoed back", result["client_order_id"] == record["client_order_id"])
    expect("reason is None on a granted claim", result["reason"] is None)


def test_second_claim_on_same_authorization_id_rejected(tmp_path) -> None:
    record = make_authorized_record(tmp_path)
    claims_dir = replay_claims(tmp_path)
    first = REPLAY.claim(record, claims_dir=claims_dir)
    second = REPLAY.claim(record, claims_dir=claims_dir)
    expect("first claim granted", first["granted"] is True)
    expect("second claim on the same authorization_id refused", second["granted"] is False)
    expect("second claim reason is ALREADY_CLAIMED", second["reason"] == "ALREADY_CLAIMED")


# ======================================================================= #
# 3: duplicate returns original execution identity
# ======================================================================= #

def test_duplicate_claim_returns_original_execution_identity(tmp_path) -> None:
    record = make_authorized_record(tmp_path, symbol="TSLA", direction="OPEN_SHORT")
    claims_dir = replay_claims(tmp_path)
    REPLAY.claim(record, claims_dir=claims_dir)
    second = REPLAY.claim(record, claims_dir=claims_dir)
    expect("duplicate refused", second["granted"] is False)
    expect("existing_client_order_id resolves to the ORIGINAL client_order_id",
           second["existing_client_order_id"] == record["client_order_id"])
    expect("existing_claim carries the original bound symbol", second["existing_claim"]["symbol"] == "TSLA")
    expect("existing_claim carries the original bound direction", second["existing_claim"]["direction"] == "OPEN_SHORT")
    expect("existing_claim does not overwrite -- still matches original authorization_hash",
           second["existing_claim"]["authorization_hash"] == record["authorization_hash"])


# ======================================================================= #
# 4-5: concurrent claims -- exactly one winner, losers see the winner
# ======================================================================= #

def test_concurrent_claims_exactly_one_winner(tmp_path) -> None:
    record = make_authorized_record(tmp_path)
    claims_dir = replay_claims(tmp_path)
    results: list[dict] = []
    lock = threading.Lock()

    def attempt():
        r = REPLAY.claim(record, claims_dir=claims_dir)
        with lock:
            results.append(r)

    threads = [threading.Thread(target=attempt) for _ in range(25)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    granted = [r for r in results if r["granted"] is True]
    refused = [r for r in results if r["granted"] is False]
    expect("exactly one of 25 concurrent claims on the same authorization_id is granted", len(granted) == 1)
    expect("the other 24 are refused", len(refused) == 24)
    expect("every refusal is ALREADY_CLAIMED (never a fabricated success)",
           all(r["reason"] == "ALREADY_CLAIMED" for r in refused))
    expect("every refusal's existing_client_order_id resolves to the SAME winning client_order_id",
           len({r["existing_client_order_id"] for r in refused}) == 1)
    expect("the refusals' existing_client_order_id matches the actual winner",
           refused[0]["existing_client_order_id"] == record["client_order_id"])


# ======================================================================= #
# 6: process/restart persistence
# ======================================================================= #

def test_restart_recovery_claim_survives_a_fresh_process_view(tmp_path) -> None:
    record = make_authorized_record(tmp_path)
    claims_dir = replay_claims(tmp_path)
    first = REPLAY.claim(record, claims_dir=claims_dir)
    expect("original claim granted", first["granted"] is True)

    fresh_replay = _load(
        "aura_v05337_alpaca_replay_protected_consumption_restart_check",
        "aura_v05337_alpaca_replay_protected_consumption.py",
    )

    recovered = fresh_replay.get_claim(record["authorization_id"], claims_dir=claims_dir)
    expect("the claim record is recovered intact after a simulated restart",
           recovered is not None and recovered["client_order_id"] == record["client_order_id"])
    ok, errors = fresh_replay.verify_claim(recovered)
    expect("the recovered record still self-verifies", ok and not errors)

    retry = fresh_replay.claim(record, claims_dir=claims_dir)
    expect("a retry after the simulated restart is still refused", retry["granted"] is False)
    expect("existing_client_order_id still resolves correctly post-restart",
           retry["existing_client_order_id"] == record["client_order_id"])


# ======================================================================= #
# 7-13: authorization-identity-confusion detection, per bound field
# ======================================================================= #

def test_claim_detects_symbol_mismatch_with_same_authorization_id(tmp_path) -> None:
    record = make_authorized_record(tmp_path, symbol="AAPL")
    claims_dir = replay_claims(tmp_path)
    REPLAY.claim(record, claims_dir=claims_dir)
    forged = forged_record(record, symbol="MSFT")
    result = REPLAY.claim(forged, claims_dir=claims_dir)
    expect("symbol-mismatched reuse refused", result["granted"] is False)
    expect("reason is AUTHORIZATION_ID_REUSED_WITH_MISMATCHED_CONTENT",
           result["reason"] == "AUTHORIZATION_ID_REUSED_WITH_MISMATCHED_CONTENT")


def test_claim_detects_direction_mismatch_with_same_authorization_id(tmp_path) -> None:
    record = make_authorized_record(tmp_path, direction="OPEN_LONG")
    claims_dir = replay_claims(tmp_path)
    REPLAY.claim(record, claims_dir=claims_dir)
    forged = forged_record(record, direction="CLOSE_LONG")
    result = REPLAY.claim(forged, claims_dir=claims_dir)
    expect("direction-mismatched reuse refused", result["granted"] is False)
    expect("reason is AUTHORIZATION_ID_REUSED_WITH_MISMATCHED_CONTENT",
           result["reason"] == "AUTHORIZATION_ID_REUSED_WITH_MISMATCHED_CONTENT")


def test_claim_detects_position_intent_mismatch_with_same_authorization_id(tmp_path) -> None:
    record = make_authorized_record(tmp_path, direction="OPEN_LONG")
    claims_dir = replay_claims(tmp_path)
    REPLAY.claim(record, claims_dir=claims_dir)
    forged = forged_record(record, position_intent="SELL_SHORT")
    result = REPLAY.claim(forged, claims_dir=claims_dir)
    expect("position_intent-mismatched reuse refused", result["granted"] is False)
    expect("reason is AUTHORIZATION_ID_REUSED_WITH_MISMATCHED_CONTENT",
           result["reason"] == "AUTHORIZATION_ID_REUSED_WITH_MISMATCHED_CONTENT")


def test_claim_detects_venue_mismatch_with_same_authorization_id(tmp_path) -> None:
    record = make_authorized_record(tmp_path)
    claims_dir = replay_claims(tmp_path)
    REPLAY.claim(record, claims_dir=claims_dir)
    forged = forged_record(record, venue="MEXC")
    result = REPLAY.claim(forged, claims_dir=claims_dir)
    expect("venue-mismatched reuse refused", result["granted"] is False)
    expect("reason is AUTHORIZATION_ID_REUSED_WITH_MISMATCHED_CONTENT",
           result["reason"] == "AUTHORIZATION_ID_REUSED_WITH_MISMATCHED_CONTENT")


def test_claim_detects_environment_mismatch_with_same_authorization_id(tmp_path) -> None:
    record = make_authorized_record(tmp_path)
    claims_dir = replay_claims(tmp_path)
    REPLAY.claim(record, claims_dir=claims_dir)
    forged = forged_record(record, environment="LIVE")
    result = REPLAY.claim(forged, claims_dir=claims_dir)
    expect("environment-mismatched reuse refused", result["granted"] is False)
    expect("reason is AUTHORIZATION_ID_REUSED_WITH_MISMATCHED_CONTENT",
           result["reason"] == "AUTHORIZATION_ID_REUSED_WITH_MISMATCHED_CONTENT")


def test_claim_detects_spec_fingerprint_mismatch_with_same_authorization_id(tmp_path) -> None:
    record = make_authorized_record(tmp_path)
    claims_dir = replay_claims(tmp_path)
    REPLAY.claim(record, claims_dir=claims_dir)
    forged = forged_record(record, spec_fingerprint="0" * 64)
    result = REPLAY.claim(forged, claims_dir=claims_dir)
    expect("spec_fingerprint-mismatched reuse refused", result["granted"] is False)
    expect("reason is AUTHORIZATION_ID_REUSED_WITH_MISMATCHED_CONTENT",
           result["reason"] == "AUTHORIZATION_ID_REUSED_WITH_MISMATCHED_CONTENT")


def test_claim_detects_client_order_id_mismatch_with_same_authorization_id(tmp_path) -> None:
    record = make_authorized_record(tmp_path)
    claims_dir = replay_claims(tmp_path)
    REPLAY.claim(record, claims_dir=claims_dir)
    forged = forged_record(record, client_order_id="AURA-forged-client-order-id")
    result = REPLAY.claim(forged, claims_dir=claims_dir)
    expect("client_order_id-mismatched reuse refused", result["granted"] is False)
    expect("reason is AUTHORIZATION_ID_REUSED_WITH_MISMATCHED_CONTENT",
           result["reason"] == "AUTHORIZATION_ID_REUSED_WITH_MISMATCHED_CONTENT")


# ======================================================================= #
# 14: malformed persisted record
# ======================================================================= #

def test_malformed_persisted_record_fails_closed_on_read(tmp_path) -> None:
    claims_dir = replay_claims(tmp_path)
    claims_dir.mkdir(parents=True, exist_ok=True)
    authorization_id = "ALPACA-EQUITY-AUTH-" + "a" * 32
    path = REPLAY.claim_path(authorization_id, claims_dir)
    path.write_text("{not valid json", encoding="utf-8")

    raised = False
    try:
        REPLAY.get_claim(authorization_id, claims_dir=claims_dir)
    except RuntimeError as exc:
        raised = True
        expect("unparsable record error names the failure", "CLAIM_RECORD_UNPARSEABLE" in str(exc))
    expect("get_claim raises (fails closed) on unparsable content", raised)


# ======================================================================= #
# 15: tampered persisted record
# ======================================================================= #

def test_tampered_persisted_record_fails_closed_on_read(tmp_path) -> None:
    record = make_authorized_record(tmp_path)
    claims_dir = replay_claims(tmp_path)
    REPLAY.claim(record, claims_dir=claims_dir)

    path = REPLAY.claim_path(record["authorization_id"], claims_dir)
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    on_disk["symbol"] = "TAMPERED"  # claim_hash NOT recomputed
    path.write_text(json.dumps(on_disk), encoding="utf-8")

    raised = False
    try:
        REPLAY.get_claim(record["authorization_id"], claims_dir=claims_dir)
    except RuntimeError as exc:
        raised = True
        expect("tampered record error names the failure", "CLAIM_RECORD_TAMPERED" in str(exc))
    expect("get_claim raises (fails closed) on tampered content", raised)


# ======================================================================= #
# 16: corrupted hash-chain/integrity data encountered mid-race
# ======================================================================= #

def test_claim_against_unverifiable_existing_record_fails_closed(tmp_path) -> None:
    """A second claim() attempt that loses the O_CREAT|O_EXCL race but then
    finds the existing on-disk record corrupted (never verifiable within
    the retry window) must fail closed with a distinct, honest reason --
    never silently grant, and never silently claim ALREADY_CLAIMED for an
    identity it could not actually confirm."""
    record = make_authorized_record(tmp_path)
    claims_dir = replay_claims(tmp_path)
    REPLAY.claim(record, claims_dir=claims_dir)

    path = REPLAY.claim_path(record["authorization_id"], claims_dir)
    path.write_text("{still not valid json", encoding="utf-8")

    result = REPLAY.claim(record, claims_dir=claims_dir, retry_deadline_seconds=0.05)
    expect("claim against a corrupted existing record is refused", result["granted"] is False)
    expect("reason is ALREADY_CLAIMED_RECORD_UNVERIFIABLE",
           result["reason"] == "ALREADY_CLAIMED_RECORD_UNVERIFIABLE")
    expect("no existing_client_order_id is fabricated", result["existing_client_order_id"] is None)


# ======================================================================= #
# 17: EXECUTION_UNCERTAIN never becomes permission to retry
# ======================================================================= #

def test_uncertain_downstream_outcome_does_not_release_the_claim(tmp_path) -> None:
    """This module has no concept of a downstream submission outcome at
    all -- simulate an "uncertain" result by simply attempting a second
    claim with no special signal (there is none to give): it must be
    refused identically to any other duplicate, proving there is no code
    path that could un-consume a granted claim."""
    record = make_authorized_record(tmp_path)
    claims_dir = replay_claims(tmp_path)
    first = REPLAY.claim(record, claims_dir=claims_dir)
    expect("first claim granted", first["granted"] is True)

    # "the downstream Alpaca submission outcome is uncertain" -- nothing
    # about that changes what claim() does; there is no release/uncertain
    # parameter anywhere in its signature.
    second = REPLAY.claim(record, claims_dir=claims_dir)
    expect("a second attempt after an 'uncertain' downstream outcome is still refused",
           second["granted"] is False)
    expect("still reported as an ordinary ALREADY_CLAIMED, never released", second["reason"] == "ALREADY_CLAIMED")


# ======================================================================= #
# 18: rejected authorization does not create a consumed claim
# ======================================================================= #

def test_rejection_dict_from_authorize_produces_no_claim(tmp_path) -> None:
    spec = make_spec()
    asset = make_asset()
    blocked_result = AUTH.authorize(spec, asset, config=authorized_config(kill_switch=True), claims_dir=auth_claims(tmp_path))
    expect("authorize() rejected (kill switch engaged)", blocked_result["status"] == "AUTHORIZATION_REJECTED")

    claims_dir = replay_claims(tmp_path)
    result = REPLAY.claim(blocked_result, claims_dir=claims_dir)
    expect("claim() refuses a rejection dict (not a valid AuthorizationRecord)", result["granted"] is False)
    expect("no claim file is left behind", not list(claims_dir.glob("*")) if claims_dir.exists() else True)


def test_non_authorized_status_record_rejected_before_claiming(tmp_path) -> None:
    record = make_authorized_record(tmp_path)
    revoked = forged_record(record, status="REVOKED")
    claims_dir = replay_claims(tmp_path)
    result = REPLAY.claim(revoked, claims_dir=claims_dir)
    expect("a self-consistent but non-AUTHORIZED record is refused", result["granted"] is False)
    expect("reason is AUTHORIZATION_NOT_VALID", result["reason"] == "AUTHORIZATION_NOT_VALID")
    expect("no claim file is created for a rejected record",
           not REPLAY.claim_path(record["authorization_id"], claims_dir).exists())


# ======================================================================= #
# 19: different authorization_ids consumed independently
# ======================================================================= #

def test_different_authorization_ids_consumed_independently(tmp_path) -> None:
    claims_dir = replay_claims(tmp_path)
    record_a = make_authorized_record(tmp_path, symbol="AAPL", decision_id="DEC-A")
    record_b = make_authorized_record(tmp_path, symbol="MSFT", decision_id="DEC-B")
    expect("two independently authorized records get distinct authorization_ids",
           record_a["authorization_id"] != record_b["authorization_id"])

    result_a = REPLAY.claim(record_a, claims_dir=claims_dir)
    result_b = REPLAY.claim(record_b, claims_dir=claims_dir)
    expect("record A claims independently", result_a["granted"] is True)
    expect("record B claims independently", result_b["granted"] is True)

    replay_a = REPLAY.claim(record_a, claims_dir=claims_dir)
    replay_b = REPLAY.claim(record_b, claims_dir=claims_dir)
    expect("record A's own replay is refused", replay_a["granted"] is False)
    expect("record B's own replay is refused", replay_b["granted"] is False)


# ======================================================================= #
# 20: same client_order_id, different authorization_id
# ======================================================================= #

def test_same_client_order_id_different_authorization_id_both_claim_independently(tmp_path) -> None:
    """.37 protects the authorization_id layer only (module docstring item
    1's disclosed asymmetry) -- it does not, and was not asked to, enforce
    global client_order_id uniqueness (that remains .35/.22's live-Alpaca-
    API-dependent duplicate guard). Two DIFFERENT, independently-issued
    authorization_ids that happen to share a client_order_id must each
    still be claimable at .37's own layer -- proving .37 does the one job
    it owns, correctly, without silently overreaching into a job it does
    not own."""
    claims_dir = replay_claims(tmp_path)
    record_a = make_authorized_record(tmp_path, symbol="AAPL")
    record_b_base = make_authorized_record(tmp_path, symbol="MSFT")
    record_b = forged_record(record_b_base, client_order_id=record_a["client_order_id"])
    expect("record A and forged record B share a client_order_id",
           record_a["client_order_id"] == record_b["client_order_id"])
    expect("record A and record B have distinct authorization_ids",
           record_a["authorization_id"] != record_b["authorization_id"])

    result_a = REPLAY.claim(record_a, claims_dir=claims_dir)
    result_b = REPLAY.claim(record_b, claims_dir=claims_dir)
    expect("record A claims successfully at the authorization_id layer", result_a["granted"] is True)
    expect("record B (different authorization_id) also claims successfully at its own layer",
           result_b["granted"] is True)


# ======================================================================= #
# 21: deterministic record/fingerprint behaviour
# ======================================================================= #

def test_canonical_claim_hash_is_deterministic(tmp_path) -> None:
    record = make_authorized_record(tmp_path)
    built = REPLAY._build_claim_record(record, "2026-09-11T12:00:00+00:00")
    hash_1 = REPLAY.canonical_claim_hash(built)
    hash_2 = REPLAY.canonical_claim_hash(built)
    expect("canonical_claim_hash is deterministic across repeated calls", hash_1 == hash_2)

    reordered = dict(reversed(list(built.items())))
    expect("canonical_claim_hash is independent of dict key order", REPLAY.canonical_claim_hash(reordered) == hash_1)

    mutated = dict(built)
    mutated["symbol"] = "DIFFERENT"
    expect("canonical_claim_hash changes when a bound field changes",
           REPLAY.canonical_claim_hash(mutated) != hash_1)


# ======================================================================= #
# 22: no release/delete function anywhere in the module
# ======================================================================= #

def test_module_exposes_no_release_or_delete_function() -> None:
    release_like_names = [
        name for name in dir(REPLAY)
        if "release" in name.lower() or "delete" in name.lower() or "unclaim" in name.lower()
    ]
    expect("the module exposes no release/delete/unclaim function at all", release_like_names == [])


# ======================================================================= #
# Integration: real .33 -> .36 -> .37 -> .35 chain construction
# ======================================================================= #

def _run_chain(tmp_path: Path, **spec_overrides) -> dict:
    asset_kwargs = {}
    for key in ("shortable", "easy_to_borrow", "tradable", "fractionable"):
        if key in spec_overrides:
            asset_kwargs[key] = spec_overrides.pop(key)
    spec = make_spec(**spec_overrides)
    asset = make_asset(symbol=spec_overrides.get("symbol", "AAPL"), **asset_kwargs)
    auth_claims_dir = auth_claims(tmp_path)

    record = AUTH.authorize(spec, asset, config=authorized_config(), claims_dir=auth_claims_dir)
    expect(f"chain[{spec_overrides}]: authorized", record["status"] == "AUTHORIZED")

    result = AUTH.authorized_order_request(record, spec, asset, auth_claims_dir, config=authorized_config())
    expect(f"chain[{spec_overrides}]: order request ready", result["status"] == "AUTHORIZED_ORDER_REQUEST_READY")
    expect(f"chain[{spec_overrides}]: alpaca_order_request constructed", result["alpaca_order_request"] is not None)
    return {"record": record, "result": result}


def test_full_chain_stock_long_open(tmp_path) -> None:
    _run_chain(tmp_path, asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL")


def test_full_chain_stock_short_open_with_borrow_evidence(tmp_path) -> None:
    _run_chain(tmp_path, asset_class="STOCK", direction="OPEN_SHORT", symbol="TSLA",
               shortable=True, easy_to_borrow=True)


def test_full_chain_etf_long_open(tmp_path) -> None:
    _run_chain(tmp_path, asset_class="ETF", direction="OPEN_LONG", symbol="SPY")


def test_full_chain_etf_short_open_with_borrow_evidence(tmp_path) -> None:
    _run_chain(tmp_path, asset_class="ETF", direction="OPEN_SHORT", symbol="IWM",
               shortable=True, easy_to_borrow=True)


def test_full_chain_close_short_does_not_require_fresh_borrow_evidence(tmp_path) -> None:
    """CLOSE_SHORT must remain possible even when the asset is currently
    NOT shortable / not easy-to-borrow -- closing an existing short is not
    a new short, and .37's claim layer must not add any borrow-related
    gate on top of what .35/.36 already decide (module docstring item 4:
    this module stores an identity link only, never a lifecycle-state
    judgement)."""
    _run_chain(tmp_path, asset_class="STOCK", direction="CLOSE_SHORT", symbol="GME",
               shortable=False, easy_to_borrow=False)


def test_chain_persists_a_matching_37_claim_record(tmp_path) -> None:
    outcome = _run_chain(tmp_path, asset_class="STOCK", direction="OPEN_LONG", symbol="NVDA")
    record = outcome["record"]
    persisted = REPLAY.get_claim(record["authorization_id"], claims_dir=auth_claims(tmp_path))
    expect(".37 has a persisted claim after authorized_order_request() ran", persisted is not None)
    expect("persisted claim's authorization_hash matches the .36 record",
           persisted["authorization_hash"] == record["authorization_hash"])
    expect("persisted claim's symbol matches", persisted["symbol"] == "NVDA")
    expect("persisted claim's client_order_id matches", persisted["client_order_id"] == record["client_order_id"])


def test_chain_replay_via_authorized_order_request_is_already_consumed(tmp_path) -> None:
    outcome = _run_chain(tmp_path, asset_class="STOCK", direction="OPEN_LONG", symbol="AMD")
    record = outcome["record"]
    # Re-deriving the exact same spec content used inside _run_chain is not
    # possible byte-for-byte (timestamps), so instead directly replay via
    # .37 at its own layer -- the scenario authorized_order_request()
    # itself already proved once via test_authorized_order_request_replay_
    # returns_already_consumed in the .36 test file; here we confirm .37's
    # OWN record for this chain refuses a second direct claim too.
    result = REPLAY.claim(record, claims_dir=auth_claims(tmp_path))
    expect("a second direct .37 claim on the chain's own record is refused", result["granted"] is False)
    expect("reason is ALREADY_CLAIMED", result["reason"] == "ALREADY_CLAIMED")


def test_ai_proposal_and_deterministic_signal_pass_through_the_identical_chain(tmp_path) -> None:
    """The source of a decision confers no execution authority -- an
    AI_PROPOSAL-sourced spec must produce a claim through the exact same
    .33 -> .36 -> .37 chain as a DETERMINISTIC_SIGNAL, with no special
    treatment anywhere in this module (which never reads source_kind at
    all -- it is not even a field this module's claim record binds)."""
    outcome_deterministic = _run_chain(
        tmp_path, asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL",
        source_kind="DETERMINISTIC_SIGNAL", decision_id="DEC-DET",
    )
    outcome_ai = _run_chain(
        tmp_path, asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL",
        source_kind="AI_PROPOSAL", decision_id="DEC-AI",
    )
    expect("deterministic-sourced chain reaches AUTHORIZED_ORDER_REQUEST_READY",
           outcome_deterministic["result"]["status"] == "AUTHORIZED_ORDER_REQUEST_READY")
    expect("AI-proposal-sourced chain reaches AUTHORIZED_ORDER_REQUEST_READY identically",
           outcome_ai["result"]["status"] == "AUTHORIZED_ORDER_REQUEST_READY")

    claims_dir = auth_claims(tmp_path)
    persisted_ai = REPLAY.get_claim(outcome_ai["record"]["authorization_id"], claims_dir=claims_dir)
    expect("source_kind is not among the fields .37 binds into its claim record",
           "source_kind" not in persisted_ai)


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_")]
    for t in tests:
        t()
    print(f"AURA v0.5.3.37 UNIT TEST CONTRACT: ALL {len(tests)} PASS")
