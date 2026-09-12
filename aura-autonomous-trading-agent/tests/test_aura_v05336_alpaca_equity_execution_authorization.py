#!/usr/bin/env python3
"""Unit + integration tests for AURA v0.5.3.36 (Alpaca-Native Execution
Authorization).

Covers Martin's full explicit .36 test list (items 1-27): valid
STOCK/ETF x LONG/SHORT x OPEN/CLOSE authorization, shortability UNKNOWN/
DENIED rejection, hard-to-borrow rejection for a NEW short,
CLOSE_SHORT remaining possible without fresh borrow confirmation,
execution-spec and safety-state fingerprint mismatches, per-field
modification rejection (quantity/symbol/direction/position-intent/venue/
order-type), expiry rejection, kill-switch rejection,
execution-authorized=false rejection, paper/live mismatch rejection,
malformed authorization record rejection, replay/duplicate claim
handling, and claim concurrency behaviour -- plus a dedicated
.33 -> .35 -> .36 integration chain test and a test proving a decision's
source_kind (deterministic vs. AI-proposed) confers no execution
authority on its own.

Repo root is on sys.path when running under pytest from the repo root
(verified this session), so plain `import aura_v0533x_...` here resolves
to the SAME cached module objects the modules under test dynamically
import internally -- required for the position-intent-mismatch and
no-submission-call tests below, which monkeypatch/inspect those exact
objects.

No real credentials, no network call, no actual Alpaca order submission
anywhere in this file.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import aura_v05333_canonical_execution_specification as CANON
import aura_v05335_alpaca_equity_execution_adapter as ADAPTER
import aura_v05336_alpaca_equity_execution_authorization as AUTH

ROOT = Path(__file__).resolve().parents[1]


def expect(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"PASS: {name}")


# ======================================================================= #
# Fixtures / builders
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


def tamper_spec(spec: dict, **overrides) -> dict:
    tampered = dict(spec)
    tampered.update(overrides)
    tampered["spec_fingerprint"] = CANON.canonical_spec_fingerprint(tampered)
    return tampered


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


def claims(tmp_path: Path) -> Path:
    return tmp_path / "auth-claims"


# ======================================================================= #
# 1-8: valid STOCK/ETF x LONG/SHORT x OPEN/CLOSE authorization
# ======================================================================= #

def test_valid_stock_long_open_authorization(tmp_path) -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL")
    asset = make_asset(symbol="AAPL")
    record = AUTH.authorize(spec, asset, config=authorized_config(), claims_dir=claims(tmp_path))
    expect("stock long open: AUTHORIZED", record["status"] == "AUTHORIZED")
    expect("stock long open: side buy", record["side"] == "buy")
    expect("stock long open: position_intent buy_to_open", record["position_intent"] == "buy_to_open")
    expect("stock long open: environment PAPER", record["environment"] == "PAPER")


def test_valid_stock_long_close_authorization(tmp_path) -> None:
    spec = make_spec(asset_class="STOCK", direction="CLOSE_LONG", symbol="AAPL")
    asset = make_asset(symbol="AAPL", shortable=False, easy_to_borrow=False)
    record = AUTH.authorize(spec, asset, config=authorized_config(), claims_dir=claims(tmp_path))
    expect("stock long close: AUTHORIZED", record["status"] == "AUTHORIZED")
    expect("stock long close: side sell", record["side"] == "sell")
    expect("stock long close: position_intent sell_to_close", record["position_intent"] == "sell_to_close")


def test_valid_stock_short_open_authorization(tmp_path) -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_SHORT", symbol="TSLA")
    asset = make_asset(symbol="TSLA", shortable=True, easy_to_borrow=True)
    record = AUTH.authorize(spec, asset, config=authorized_config(), claims_dir=claims(tmp_path))
    expect("stock short open: AUTHORIZED", record["status"] == "AUTHORIZED")
    expect("stock short open: side sell", record["side"] == "sell")
    expect("stock short open: position_intent sell_to_open", record["position_intent"] == "sell_to_open")
    expect("stock short open: shortability SHORTABLE", record["shortability_status"] == "SHORTABLE")
    expect("stock short open: capability BORROW_CONFIRMED", record["direction_capability"] == "BORROW_CONFIRMED")


def test_valid_stock_short_close_authorization(tmp_path) -> None:
    spec = make_spec(asset_class="STOCK", direction="CLOSE_SHORT", symbol="TSLA")
    asset = make_asset(symbol="TSLA", shortable=True, easy_to_borrow=True)
    record = AUTH.authorize(spec, asset, config=authorized_config(), claims_dir=claims(tmp_path))
    expect("stock short close: AUTHORIZED", record["status"] == "AUTHORIZED")
    expect("stock short close: side buy", record["side"] == "buy")
    expect("stock short close: position_intent buy_to_close", record["position_intent"] == "buy_to_close")


def test_valid_etf_long_open_authorization(tmp_path) -> None:
    spec = make_spec(asset_class="ETF", direction="OPEN_LONG", symbol="SPY")
    asset = make_asset(symbol="SPY")
    record = AUTH.authorize(spec, asset, config=authorized_config(), claims_dir=claims(tmp_path))
    expect("etf long open: AUTHORIZED", record["status"] == "AUTHORIZED")
    expect("etf long open: position_intent buy_to_open", record["position_intent"] == "buy_to_open")


def test_valid_etf_long_close_authorization(tmp_path) -> None:
    spec = make_spec(asset_class="ETF", direction="CLOSE_LONG", symbol="SPY")
    asset = make_asset(symbol="SPY", shortable=False, easy_to_borrow=False)
    record = AUTH.authorize(spec, asset, config=authorized_config(), claims_dir=claims(tmp_path))
    expect("etf long close: AUTHORIZED", record["status"] == "AUTHORIZED")
    expect("etf long close: position_intent sell_to_close", record["position_intent"] == "sell_to_close")


def test_valid_etf_short_open_authorization(tmp_path) -> None:
    spec = make_spec(asset_class="ETF", direction="OPEN_SHORT", symbol="IWM")
    asset = make_asset(symbol="IWM", shortable=True, easy_to_borrow=True)
    record = AUTH.authorize(spec, asset, config=authorized_config(), claims_dir=claims(tmp_path))
    expect("etf short open: AUTHORIZED", record["status"] == "AUTHORIZED")
    expect("etf short open: position_intent sell_to_open", record["position_intent"] == "sell_to_open")


def test_valid_etf_short_close_authorization(tmp_path) -> None:
    spec = make_spec(asset_class="ETF", direction="CLOSE_SHORT", symbol="IWM")
    asset = make_asset(symbol="IWM", shortable=True, easy_to_borrow=True)
    record = AUTH.authorize(spec, asset, config=authorized_config(), claims_dir=claims(tmp_path))
    expect("etf short close: AUTHORIZED", record["status"] == "AUTHORIZED")
    expect("etf short close: position_intent buy_to_close", record["position_intent"] == "buy_to_close")


# ======================================================================= #
# 9-12: shortability gating
# ======================================================================= #

def test_shortability_unknown_rejection(tmp_path) -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_SHORT", symbol="GME")
    asset = make_asset(symbol="GME", shortable=None, easy_to_borrow=None)
    record = AUTH.authorize(spec, asset, config=authorized_config(), claims_dir=claims(tmp_path))
    expect("shortable=unknown rejected", record["status"] == "AUTHORIZATION_REJECTED")
    expect("shortable=unknown: capability validation failure reason", record["reason"] == "ADAPTER_CAPABILITY_VALIDATION_FAILED")
    expect("shortable=unknown: detail names BORROW_UNKNOWN", "BORROW_UNKNOWN" in str(record["detail"]))


def test_shortability_denied_rejection(tmp_path) -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_SHORT", symbol="GME")
    asset = make_asset(symbol="GME", shortable=False, easy_to_borrow=False)
    record = AUTH.authorize(spec, asset, config=authorized_config(), claims_dir=claims(tmp_path))
    expect("shortable=denied rejected", record["status"] == "AUTHORIZATION_REJECTED")
    expect("shortable=denied: detail names BORROW_DENIED", "BORROW_DENIED" in str(record["detail"]))


def test_hard_to_borrow_rejection_for_new_short(tmp_path) -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_SHORT", symbol="AMC")
    asset = make_asset(symbol="AMC", shortable=True, easy_to_borrow=False)
    record = AUTH.authorize(spec, asset, config=authorized_config(), claims_dir=claims(tmp_path))
    expect("hard-to-borrow OPEN_SHORT rejected", record["status"] == "AUTHORIZATION_REJECTED")
    expect("hard-to-borrow: detail names BORROW_UNKNOWN (conservative reading)", "BORROW_UNKNOWN" in str(record["detail"]))


def test_close_short_remains_possible_without_new_borrow_confirmation(tmp_path) -> None:
    spec = make_spec(asset_class="STOCK", direction="CLOSE_SHORT", symbol="AMC")
    asset = make_asset(symbol="AMC", shortable=False, easy_to_borrow=False)
    record = AUTH.authorize(spec, asset, config=authorized_config(), claims_dir=claims(tmp_path))
    expect("CLOSE_SHORT authorized despite NOT_SHORTABLE asset", record["status"] == "AUTHORIZED")
    expect("CLOSE_SHORT: shortability recorded honestly as NOT_SHORTABLE", record["shortability_status"] == "NOT_SHORTABLE")


# ======================================================================= #
# 13-14: fingerprint mismatches
# ======================================================================= #

def test_execution_spec_fingerprint_mismatch(tmp_path) -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL")
    asset = make_asset(symbol="AAPL")
    record = AUTH.authorize(spec, asset, config=authorized_config(), claims_dir=claims(tmp_path))
    expect("baseline authorized", record["status"] == "AUTHORIZED")

    tampered_spec = tamper_spec(spec, quantity="999")
    result = AUTH.revalidate_before_submission(record, tampered_spec, asset, claims_dir=claims(tmp_path))
    expect("tampered spec -> revalidation rejected", result["status"] == "AUTHORIZATION_REJECTED")
    expect("tampered spec -> EXECUTION_SPEC_FINGERPRINT_MISMATCH", result["reason"] == "EXECUTION_SPEC_FINGERPRINT_MISMATCH")


def test_safety_state_fingerprint_mismatch(tmp_path) -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL")
    asset = make_asset(symbol="AAPL")
    forged_safety_state = {
        "schema_version": AUTH.SCHEMA_VERSION,
        "agent_version": AUTH.VERSION,
        "engine": AUTH.ENGINE,
        "kill_switch": False,
        "execution_authorized": True,
        "paper_execution_authorized": True,
        "replay_protection": {"status": "HEALTHY", "reason": None},
        "guardrails": {},
        "assembled_at": "2026-09-11T12:00:00+00:00",
        "safety_state_ttl_seconds": 60,
        "safety_state_hash": "0" * 64,  # deliberately wrong -- never recomputed
    }
    record = AUTH.authorize(
        spec, asset, safety_state=forged_safety_state, config=authorized_config(), claims_dir=claims(tmp_path),
    )
    expect("forged safety_state hash rejected", record["status"] == "AUTHORIZATION_REJECTED")
    expect("forged safety_state -> SAFETY_STATE_FINGERPRINT_MISMATCH", record["reason"] == "SAFETY_STATE_FINGERPRINT_MISMATCH")


def test_safety_state_forged_but_self_consistent_rejected(tmp_path) -> None:
    """A caller can compute a correct hash over FORGED content (e.g.
    claiming execution_authorized=True while the real config says False)
    -- self-consistency alone is not trust. authorize() must still reject
    it because it never matches the independently, freshly recomputed
    safety_state (mirrors .31's own forged-safety-state test)."""
    forged_content = {
        "schema_version": AUTH.SCHEMA_VERSION,
        "agent_version": AUTH.VERSION,
        "engine": AUTH.ENGINE,
        "kill_switch": False,
        "execution_authorized": True,
        "paper_execution_authorized": True,
        "replay_protection": {"status": "HEALTHY", "reason": None},
        "guardrails": {},
    }
    forged_safety_state = dict(forged_content)
    forged_safety_state["assembled_at"] = AUTH._iso(AUTH._now())
    forged_safety_state["safety_state_ttl_seconds"] = 60
    forged_safety_state["safety_state_hash"] = AUTH._fingerprint(forged_content)

    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL")
    asset = make_asset(symbol="AAPL")
    # Real config is fully BLOCKED (defaults) -- forged safety_state claims
    # otherwise. Must still be rejected, because it disagrees with what a
    # fresh assemble_safety_state() actually computes.
    record = AUTH.authorize(spec, asset, safety_state=forged_safety_state, claims_dir=claims(tmp_path))
    expect("self-consistent-but-forged safety_state rejected", record["status"] == "AUTHORIZATION_REJECTED")
    expect("self-consistent-but-forged -> SAFETY_STATE_FORGED", record["reason"] == "SAFETY_STATE_FORGED")


# ======================================================================= #
# 15-20: per-field modification rejection (all funnel through the single
# spec_fingerprint check -- see module docstring item 1)
# ======================================================================= #

def _authorize_then_tamper_and_revalidate(tmp_path, **tamper_overrides):
    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL", quantity="10")
    asset = make_asset(symbol="AAPL")
    record = AUTH.authorize(spec, asset, config=authorized_config(), claims_dir=claims(tmp_path))
    expect("baseline authorized", record["status"] == "AUTHORIZED")
    tampered_spec = tamper_spec(spec, **tamper_overrides)
    return AUTH.revalidate_before_submission(record, tampered_spec, asset, claims_dir=claims(tmp_path))


def test_quantity_modification_rejection(tmp_path) -> None:
    result = _authorize_then_tamper_and_revalidate(tmp_path, quantity="500")
    expect("quantity modification rejected", result["status"] == "AUTHORIZATION_REJECTED")
    expect("quantity modification -> fingerprint mismatch", result["reason"] == "EXECUTION_SPEC_FINGERPRINT_MISMATCH")


def test_symbol_modification_rejection(tmp_path) -> None:
    result = _authorize_then_tamper_and_revalidate(tmp_path, symbol="MSFT")
    expect("symbol modification rejected", result["status"] == "AUTHORIZATION_REJECTED")
    expect("symbol modification -> fingerprint mismatch", result["reason"] == "EXECUTION_SPEC_FINGERPRINT_MISMATCH")


def test_direction_modification_rejection(tmp_path) -> None:
    result = _authorize_then_tamper_and_revalidate(tmp_path, direction="CLOSE_LONG")
    expect("direction modification rejected", result["status"] == "AUTHORIZATION_REJECTED")
    expect("direction modification -> fingerprint mismatch", result["reason"] == "EXECUTION_SPEC_FINGERPRINT_MISMATCH")


def test_venue_modification_rejection(tmp_path) -> None:
    result = _authorize_then_tamper_and_revalidate(tmp_path, venue="MEXC")
    expect("venue modification rejected", result["status"] == "AUTHORIZATION_REJECTED")
    expect("venue modification -> fingerprint mismatch", result["reason"] == "EXECUTION_SPEC_FINGERPRINT_MISMATCH")


def test_order_type_modification_rejection(tmp_path) -> None:
    result = _authorize_then_tamper_and_revalidate(tmp_path, order_type="LIMIT", limit_price="150.00")
    expect("order_type modification rejected", result["status"] == "AUTHORIZATION_REJECTED")
    expect("order_type modification -> fingerprint mismatch", result["reason"] == "EXECUTION_SPEC_FINGERPRINT_MISMATCH")


def test_position_intent_modification_rejection(tmp_path, monkeypatch) -> None:
    """Simulates a hypothetical future bug where the direction ->
    (side, position_intent) mapping itself changes between authorize()
    and revalidation, with the execution_spec (and therefore its
    fingerprint) left UNCHANGED -- isolating the defense-in-depth
    position_intent equality check from the fingerprint check above."""
    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL")
    asset = make_asset(symbol="AAPL")
    record = AUTH.authorize(spec, asset, config=authorized_config(), claims_dir=claims(tmp_path))
    expect("baseline authorized", record["status"] == "AUTHORIZED")
    expect("baseline position_intent buy_to_open", record["position_intent"] == "buy_to_open")

    monkeypatch.setitem(ADAPTER.DIRECTION_TO_SIDE_POSITION_INTENT_STR, "OPEN_LONG", ("buy", "buy_to_close"))
    result = AUTH.revalidate_before_submission(record, spec, asset, config=authorized_config(), claims_dir=claims(tmp_path))
    expect("position_intent drift rejected", result["status"] == "AUTHORIZATION_REJECTED")
    expect("position_intent drift -> POSITION_INTENT_MISMATCH", result["reason"] == "POSITION_INTENT_MISMATCH")


# ======================================================================= #
# 21: expiry rejection (execution-spec expiry AND authorization TTL expiry)
# ======================================================================= #

def test_execution_spec_expiry_rejection(tmp_path) -> None:
    from datetime import datetime, timedelta, timezone
    # Anchored to the real wall clock rather than the fixed SIGNAL_TS
    # constant: build_canonical_execution_specification() stamps
    # specification_timestamp from now() internally, so an expiry window
    # computed off a stale fixed timestamp can drift into the past and
    # make this spec look already-expired at construction time, before
    # the intended AUTH.authorize() expiry check is even exercised.
    spec_time = datetime.now(timezone.utc)
    spec = make_spec(
        asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL",
        expires_at=AUTH._iso(spec_time + timedelta(seconds=5)),
    )
    asset = make_asset(symbol="AAPL")
    later = spec_time + timedelta(seconds=30)
    record = AUTH.authorize(spec, asset, config=authorized_config(), claims_dir=claims(tmp_path), now=later)
    expect("expired execution spec rejected", record["status"] == "AUTHORIZATION_REJECTED")
    expect("expired execution spec -> EXPIRED_EXECUTION_SPEC", record["reason"] == "EXPIRED_EXECUTION_SPEC")


def test_authorization_record_ttl_expiry_rejection(tmp_path) -> None:
    from datetime import timedelta
    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL")
    asset = make_asset(symbol="AAPL")
    record = AUTH.authorize(
        spec, asset, config=authorized_config(authorization_ttl_seconds=1), claims_dir=claims(tmp_path),
    )
    expect("baseline authorized with short TTL", record["status"] == "AUTHORIZED")
    issued_at = AUTH._parse_iso(record["issued_at"])
    later = issued_at + timedelta(seconds=5)
    result = AUTH.revalidate_before_submission(record, spec, asset, claims_dir=claims(tmp_path), now=later)
    expect("stale authorization rejected", result["status"] == "AUTHORIZATION_REJECTED")
    expect("stale authorization -> EXPIRED_AUTHORIZATION", result["reason"] == "EXPIRED_AUTHORIZATION")


# ======================================================================= #
# 22-24: kill-switch / execution-authorized / paper-live boundary
# ======================================================================= #

def test_kill_switch_rejection(tmp_path) -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL")
    asset = make_asset(symbol="AAPL")
    config = authorized_config(kill_switch=True)
    record = AUTH.authorize(spec, asset, config=config, claims_dir=claims(tmp_path))
    expect("kill switch engaged rejects", record["status"] == "AUTHORIZATION_REJECTED")
    expect("kill switch -> KILL_SWITCH_ENGAGED", record["reason"] == "KILL_SWITCH_ENGAGED")


def test_execution_authorized_false_rejection(tmp_path) -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL")
    asset = make_asset(symbol="AAPL")
    config = authorized_config(execution_authorized=False)
    record = AUTH.authorize(spec, asset, config=config, claims_dir=claims(tmp_path))
    expect("execution_authorized=False rejects", record["status"] == "AUTHORIZATION_REJECTED")
    expect("execution_authorized=False -> SAFETY_STATE_NOT_AUTHORIZED", record["reason"] == "SAFETY_STATE_NOT_AUTHORIZED")


def test_default_config_is_fully_blocked() -> None:
    """load_config() with no overrides must be exactly as safe-closed as
    .19/.31's own defaults."""
    cfg = AUTH.load_config()
    expect("default kill_switch True", cfg["kill_switch"] is True)
    expect("default execution_authorized False", cfg["execution_authorized"] is False)
    expect("default paper_execution_authorized False", cfg["paper_execution_authorized"] is False)


def test_paper_live_mismatch_rejection_via_environment(tmp_path) -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL")
    asset = make_asset(symbol="AAPL")
    record = AUTH.authorize(
        spec, asset, environment="LIVE", config=authorized_config(), claims_dir=claims(tmp_path),
    )
    expect("LIVE environment rejected", record["status"] == "AUTHORIZATION_REJECTED")
    expect("LIVE environment -> LIVE_EXECUTION_NOT_SUPPORTED_FOR_ALPACA_EQUITIES",
           record["reason"] == "LIVE_EXECUTION_NOT_SUPPORTED_FOR_ALPACA_EQUITIES")


def test_paper_live_mismatch_rejection_via_unauthorized_paper_flag(tmp_path) -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL")
    asset = make_asset(symbol="AAPL")
    config = authorized_config(paper_execution_authorized=False)
    record = AUTH.authorize(spec, asset, config=config, claims_dir=claims(tmp_path))
    expect("paper_execution_authorized=False rejected", record["status"] == "AUTHORIZATION_REJECTED")
    expect("-> PAPER_EXECUTION_NOT_AUTHORIZED", record["reason"] == "PAPER_EXECUTION_NOT_AUTHORIZED")


def test_environment_defaults_to_paper(tmp_path) -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL")
    asset = make_asset(symbol="AAPL")
    record = AUTH.authorize(spec, asset, config=authorized_config(), claims_dir=claims(tmp_path))
    expect("default environment is PAPER", record["environment"] == "PAPER")


def test_live_not_a_member_of_allowed_environments() -> None:
    expect("ALLOWED_ENVIRONMENTS is exactly {PAPER}", AUTH.ALLOWED_ENVIRONMENTS == frozenset({"PAPER"}))


# ======================================================================= #
# 25: malformed authorization record rejection
# ======================================================================= #

def test_malformed_authorization_record_rejected(tmp_path) -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL")
    asset = make_asset(symbol="AAPL")
    record = AUTH.authorize(spec, asset, config=authorized_config(), claims_dir=claims(tmp_path))
    tampered_record = dict(record)
    tampered_record["quantity"] = "999999"  # hash NOT recomputed
    result = AUTH.revalidate_before_submission(tampered_record, spec, asset, claims_dir=claims(tmp_path))
    expect("tampered record rejected", result["status"] == "AUTHORIZATION_REJECTED")
    expect("tampered record -> AUTHORIZATION_RECORD_TAMPERED", result["reason"] == "AUTHORIZATION_RECORD_TAMPERED")


def test_malformed_authorization_record_rejected_via_authorized_order_request(tmp_path) -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL")
    asset = make_asset(symbol="AAPL")
    record = AUTH.authorize(spec, asset, config=authorized_config(), claims_dir=claims(tmp_path))
    tampered_record = dict(record)
    tampered_record["status"] = "AUTHORIZED"  # unchanged, but hash now stale regardless
    tampered_record["symbol"] = "MSFT"
    result = AUTH.authorized_order_request(tampered_record, spec, asset, claims(tmp_path))
    expect("tampered record via authorized_order_request rejected",
           result["status"] in ("REVALIDATION_FAILED",))
    expect("no order_spec produced for a tampered record", result["order_spec"] is None)


def test_not_a_dict_record_rejected() -> None:
    ok, errors = AUTH.verify_authorization_record("not a dict")
    expect("non-dict record fails self-consistency", ok is False and errors == ["NOT_A_DICT"])


def test_malformed_canonical_execution_spec_rejected_at_authorize(tmp_path) -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL")
    broken = dict(spec)
    broken["spec_fingerprint"] = "0" * 64
    asset = make_asset(symbol="AAPL")
    record = AUTH.authorize(broken, asset, config=authorized_config(), claims_dir=claims(tmp_path))
    expect("malformed canonical spec rejected", record["status"] == "AUTHORIZATION_REJECTED")
    expect("malformed canonical spec -> MALFORMED_CANONICAL_SPEC", record["reason"] == "MALFORMED_CANONICAL_SPEC")


# ======================================================================= #
# 26-27: replay/duplicate claim handling + concurrency behaviour
# ======================================================================= #

def test_claim_authorization_is_single_use(tmp_path) -> None:
    claims_dir = claims(tmp_path)
    first = AUTH.claim_authorization(claims_dir, "AUTH-TEST-1")
    second = AUTH.claim_authorization(claims_dir, "AUTH-TEST-1")
    expect("first claim granted", first is True)
    expect("second claim on same id denied", second is False)


def test_claim_authorization_concurrent_processes_only_one_wins(tmp_path) -> None:
    """Simulates two separate callers racing the SAME authorization_id
    against the SAME on-disk claims directory (the atomic O_CREAT|O_EXCL
    primitive is what actually enforces single-winner semantics under
    real concurrency; here we assert that property holds across two
    independent claim_authorization() calls against one shared path)."""
    shared_claims_dir = claims(tmp_path)
    results = [AUTH.claim_authorization(shared_claims_dir, "AUTH-RACE-1") for _ in range(5)]
    expect("exactly one of five concurrent-style claims wins", results.count(True) == 1)
    expect("the remaining four are denied", results.count(False) == 4)


def test_authorized_order_request_replay_returns_already_consumed(tmp_path) -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL")
    asset = make_asset(symbol="AAPL")
    claims_dir = claims(tmp_path)
    record = AUTH.authorize(spec, asset, config=authorized_config(), claims_dir=claims_dir)
    first = AUTH.authorized_order_request(record, spec, asset, claims_dir, config=authorized_config())
    second = AUTH.authorized_order_request(record, spec, asset, claims_dir, config=authorized_config())
    expect("first authorized_order_request succeeds", first["status"] == "AUTHORIZED_ORDER_REQUEST_READY")
    expect("second (replay) is AUTHORIZATION_ALREADY_CONSUMED", second["status"] == "AUTHORIZATION_ALREADY_CONSUMED")
    expect("replay produces no order_spec", second["order_spec"] is None)


def test_revalidation_failure_before_claim_leaves_authorization_retryable(tmp_path) -> None:
    """v0.5.3.40 REORDERING: authorized_order_request() now revalidates
    BEFORE claiming (previously claim -> revalidate -> construct; now
    revalidate -> claim -> construct), specifically so a spec that is
    already stale/blocked at revalidation time never burns a .37 claim
    slot at all (Martin's explicit .40 instruction: "Do not permanently
    consume an authorization merely because an already-stale
    authorization reached the revalidation stage"). Since revalidation
    fails here (expired), NOTHING is ever claimed -- the
    authorization_id is therefore still genuinely available afterward, so
    a direct claim attempt on it must succeed, not report
    ALREADY_CLAIMED. This intentionally reverses this test's
    pre-v0.5.3.40 expectation (previously: a blocked revalidation left a
    claim permanently consumed with no retry path, because under the OLD
    claim -> revalidate -> construct order the claim was taken before
    revalidation ever ran)."""
    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL")
    asset = make_asset(symbol="AAPL")
    claims_dir = claims(tmp_path)
    record = AUTH.authorize(spec, asset, config=authorized_config(authorization_ttl_seconds=1), claims_dir=claims_dir)

    from datetime import timedelta
    issued_at = AUTH._parse_iso(record["issued_at"])
    later = issued_at + timedelta(seconds=5)

    first = AUTH.authorized_order_request(record, spec, asset, claims_dir, now=later)
    expect("first attempt fails on expiry", first["status"] == "REVALIDATION_FAILED")
    expect("first attempt fails specifically on expiry, not some other guardrail",
           first["reason"] == "EXPIRED_AUTHORIZATION")

    replay37 = AUTH._load_replay_module()
    retry = replay37.claim(record, claims_dir=claims_dir)
    expect("nothing was claimed when revalidation was blocked before the claim step (v0.5.3.40)",
           retry["granted"] is True)


# ======================================================================= #
# Integration chain: .33 -> .35 -> .36, and .36 -> .35 construction
# ======================================================================= #

def test_compatibility_chain_33_35_36(tmp_path) -> None:
    spec = CANON.build_canonical_execution_specification(
        asset_class="STOCK", venue="ALPACA", direction="OPEN_SHORT", symbol="TSLA",
        quantity="5", decision_id="CHAIN-1", strategy_id="STRAT-CHAIN", strategy_version="v1",
        signal_timestamp=SIGNAL_TS, order_type="MARKET",
    )
    asset = make_asset(symbol="TSLA", shortable=True, easy_to_borrow=True)
    claims_dir = claims(tmp_path)

    record = AUTH.authorize(spec, asset, config=authorized_config(), claims_dir=claims_dir)
    expect("chain: authorized", record["status"] == "AUTHORIZED")

    result = AUTH.authorized_order_request(record, spec, asset, claims_dir, config=authorized_config())
    expect("chain: order request ready", result["status"] == "AUTHORIZED_ORDER_REQUEST_READY")
    expect("chain: order_spec built via .35", result["order_spec"]["direction"] == "OPEN_SHORT")

    request = result["alpaca_order_request"]
    expect("chain: real Alpaca SDK request object", request.__class__.__name__ == "MarketOrderRequest")
    expect("chain: side matches", request.side.value == "sell")
    expect("chain: position_intent matches", request.position_intent.value == "sell_to_open")


def test_authorized_order_request_never_calls_submit(tmp_path, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(ADAPTER, "submit", lambda *a, **k: calls.append((a, k)) or (_ for _ in ()).throw(AssertionError("submit() must never be called by .36")))

    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL")
    asset = make_asset(symbol="AAPL")
    claims_dir = claims(tmp_path)
    record = AUTH.authorize(spec, asset, config=authorized_config(), claims_dir=claims_dir)
    result = AUTH.authorized_order_request(record, spec, asset, claims_dir, config=authorized_config())
    expect("order request succeeds without ever calling submit()", result["status"] == "AUTHORIZED_ORDER_REQUEST_READY")
    expect("submit() was never invoked", calls == [])


def test_no_submit_reference_in_authorization_functions() -> None:
    for name in ("authorize", "revalidate_before_submission", "authorized_order_request", "_evaluate_guardrails"):
        source = inspect.getsource(getattr(AUTH, name))
        expect(f"{name}(): never calls adapter35.submit(", "adapter35.submit(" not in source)
        expect(f"{name}(): never references TradingClient(", "TradingClient(" not in source)


# ======================================================================= #
# Source of a decision confers no execution authority
# ======================================================================= #

def test_ai_proposal_and_deterministic_signal_authorized_identically(tmp_path) -> None:
    asset = make_asset(symbol="AAPL")
    config = authorized_config()

    deterministic_spec = make_spec(
        asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL",
        decision_id="D-DET", source_kind="DETERMINISTIC_SIGNAL",
    )
    ai_spec = make_spec(
        asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL",
        decision_id="D-AI", source_kind="AI_PROPOSAL",
    )

    det_record = AUTH.authorize(deterministic_spec, asset, config=config, claims_dir=claims(tmp_path))
    ai_record = AUTH.authorize(ai_spec, asset, config=config, claims_dir=claims(tmp_path))

    expect("deterministic-sourced spec authorized", det_record["status"] == "AUTHORIZED")
    expect("AI-proposed spec authorized identically", ai_record["status"] == "AUTHORIZED")
    expect("both evaluated identical guardrails", det_record["guardrails_evaluated"] == ai_record["guardrails_evaluated"])
    expect("source_kind recorded but never gates", ai_record["source_kind"] == "AI_PROPOSAL")


def test_ai_proposal_rejected_identically_when_unauthorized(tmp_path) -> None:
    asset = make_asset(symbol="AAPL")
    blocked_config = authorized_config(execution_authorized=False)

    human_spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL",
                            decision_id="D-HUMAN", source_kind="HUMAN_OVERRIDE")
    ai_spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL",
                         decision_id="D-AI-2", source_kind="AI_PROPOSAL")

    human_record = AUTH.authorize(human_spec, asset, config=blocked_config, claims_dir=claims(tmp_path))
    ai_record = AUTH.authorize(ai_spec, asset, config=blocked_config, claims_dir=claims(tmp_path))

    expect("HUMAN_OVERRIDE source gets no special pass", human_record["status"] == "AUTHORIZATION_REJECTED")
    expect("AI_PROPOSAL source gets no special pass either", ai_record["status"] == "AUTHORIZATION_REJECTED")
    expect("identical rejection reason regardless of source_kind", human_record["reason"] == ai_record["reason"])


def test_source_kind_never_read_by_guardrail_evaluation() -> None:
    source = inspect.getsource(AUTH._evaluate_guardrails)
    expect("_evaluate_guardrails() never reads source_kind", "source_kind" not in source)


# ======================================================================= #
# Structural: asset-class / venue guards
# ======================================================================= #

def test_unsupported_asset_class_rejected_at_authorization(tmp_path) -> None:
    spec = CANON.build_canonical_execution_specification(
        asset_class="CRYPTO_SPOT", venue="ALPACA", direction="OPEN_LONG", symbol="BTC/USD",
        quantity="1", decision_id="D-CRYPTO", strategy_id="S1", strategy_version="v1",
        signal_timestamp=SIGNAL_TS,
    )
    asset = make_asset(symbol="BTC/USD")
    record = AUTH.authorize(spec, asset, config=authorized_config(), claims_dir=claims(tmp_path))
    expect("CRYPTO_SPOT rejected by equity authorization", record["status"] == "AUTHORIZATION_REJECTED")
    expect("-> UNSUPPORTED_ASSET_CLASS_FOR_ALPACA_EQUITY_AUTHORIZATION",
           record["reason"] == "UNSUPPORTED_ASSET_CLASS_FOR_ALPACA_EQUITY_AUTHORIZATION")


def test_venue_not_alpaca_rejected_at_authorization(tmp_path) -> None:
    spec = make_spec(asset_class="STOCK", direction="OPEN_LONG", symbol="AAPL")
    tampered = tamper_spec(spec, venue="MEXC")
    asset = make_asset(symbol="AAPL")
    record = AUTH.authorize(tampered, asset, config=authorized_config(), claims_dir=claims(tmp_path))
    expect("venue != ALPACA rejected", record["status"] == "AUTHORIZATION_REJECTED")
    expect("-> VENUE_NOT_ALPACA", record["reason"] == "VENUE_NOT_ALPACA")
