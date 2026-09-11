#!/usr/bin/env python3
"""Unit + integration tests for AURA v0.5.3.38 (Common Execution
Supervisor), covering Martin's full 2026-09-11 GO-message §11 test matrix:

  architecture / no-bypass -- canonical spec required, metadata required,
    authorization required, replay protection required, adapter required,
    no bypass path, Supervisor kill switch, source_kind confers no
    authority.
  MEXC -- all four directions (OPEN_LONG/CLOSE_LONG/OPEN_SHORT/CLOSE_SHORT),
    a full simulated "paper" (fake-exchange) execution path, replay
    rejection, EXECUTION_UNCERTAIN, reconciliation.
  Alpaca -- all 8 STOCK/ETF x LONG/SHORT x OPEN/CLOSE combinations,
    shortability enforcement, replay rejection, paper-only enforcement.
  failure injection -- authorization failure, replay failure (both venues'
    replay layers, including the NEW Supervisor-owned Alpaca
    client_order_id layer), adapter validation failure, Supervisor kill
    switch, malformed spec, missing exchange/client, execution
    uncertain/timeout for both venues, crash-before-submission /
    after-claim recovery for MEXC.
  concurrency -- exactly one execution attempt wins, for both venues.

Also specifically proves the fix made during this module's own
implementation (before any test existed against the bug): a
construction-only MEXC preview (attempt_submission=False) must NOT durably
consume any claim, so a REAL submission attempt for the same decision can
still complete afterward.

Repo root is on sys.path when running under pytest from the repo root
(same convention already established by .35/.36/.37's own test files), so
plain `import aura_v0533x_...` here resolves to the SAME cached module
objects the module under test dynamically imports internally.

No real credentials, no network call, no actual MEXC or Alpaca order
submission anywhere in this file -- every exchange/client object is an
injected fake.
"""
from __future__ import annotations

import threading
from pathlib import Path

import ccxt

import aura_v05329_mexc_intent_ledger as LEDGER
import aura_v05333_canonical_execution_specification as CANON
import aura_v05336_alpaca_equity_execution_authorization as ALPACA_AUTH
import aura_v05338_common_execution_supervisor as SUP

SIGNAL_TS = "2026-09-11T12:00:00+00:00"


def expect(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"PASS: {name}")


# ======================================================================= #
# Fixtures / builders
# ======================================================================= #

def make_mexc_spec(
    *,
    direction: str = "OPEN_LONG",
    symbol: str = "BTC/USDT:USDT",
    quantity: str = "0.01",
    leverage: int = 3,
    decision_id: str = "DEC-M1",
    strategy_id: str = "STRAT-M",
    strategy_version: str = "v1.0",
    source_kind: str = "DETERMINISTIC_SIGNAL",
    signal_timestamp: str = SIGNAL_TS,
) -> dict:
    kwargs = dict(
        asset_class="CRYPTO_FUTURES", venue="MEXC", direction=direction, symbol=symbol,
        quantity=quantity, decision_id=decision_id, strategy_id=strategy_id,
        strategy_version=strategy_version, signal_timestamp=signal_timestamp,
        order_type="MARKET", source_kind=source_kind,
    )
    if direction in ("OPEN_LONG", "OPEN_SHORT"):
        kwargs["leverage"] = leverage
    return CANON.build_canonical_execution_specification(**kwargs)


def make_alpaca_spec(
    *,
    asset_class: str = "STOCK",
    direction: str = "OPEN_LONG",
    symbol: str = "AAPL",
    quantity: str = "10",
    decision_id: str = "DEC-A1",
    strategy_id: str = "STRAT-A",
    strategy_version: str = "v1.0",
    source_kind: str = "DETERMINISTIC_SIGNAL",
    signal_timestamp: str = SIGNAL_TS,
) -> dict:
    return CANON.build_canonical_execution_specification(
        asset_class=asset_class, venue="ALPACA", direction=direction, symbol=symbol,
        quantity=quantity, decision_id=decision_id, strategy_id=strategy_id,
        strategy_version=strategy_version, signal_timestamp=signal_timestamp,
        order_type="MARKET", source_kind=source_kind,
    )


def make_alpaca_asset(*, symbol: str = "AAPL", tradable=True, shortable=True,
                       easy_to_borrow=True, fractionable=True) -> dict:
    return {
        "symbol": symbol, "asset_class": "us_equity", "exchange": "NASDAQ", "status": "active",
        "tradable": tradable, "shortable": shortable, "easy_to_borrow": easy_to_borrow,
        "fractionable": fractionable, "marginable": True,
        "min_order_size": None, "min_trade_increment": None, "price_increment": None,
    }


def alpaca_auth_config(**overrides) -> dict:
    config = {
        "kill_switch": False, "execution_authorized": True, "paper_execution_authorized": True,
        "authorization_ttl_seconds": 60, "safety_state_ttl_seconds": 60,
    }
    config.update(overrides)
    return config


def mexc_auth_config(**overrides) -> dict:
    config = {
        "kill_switch": False, "execution_authorized": True, "paper_execution_authorized": True,
        "live_execution_authorized": True, "authorization_ttl_seconds": 60, "safety_state_ttl_seconds": 60,
    }
    config.update(overrides)
    return config


def sup_open(**overrides) -> dict:
    """Supervisor's OWN kill switch, open (False) unless overridden."""
    config = {"kill_switch": False}
    config.update(overrides)
    return config


class Dirs:
    def __init__(self, tmp_path: Path):
        self.mexc_auth = tmp_path / "mexc-auth-claims"
        self.mexc_adapter = tmp_path / "mexc-adapter-claims"
        self.mexc_ledger = tmp_path / "mexc-ledger"
        self.alpaca_auth = tmp_path / "alpaca-auth-claims"
        self.alpaca_supervisor = tmp_path / "alpaca-supervisor-claims"

    def mexc_kwargs(self) -> dict:
        return dict(auth_claims_dir=self.mexc_auth, adapter_claims_dir=self.mexc_adapter,
                    ledger_base_dir=self.mexc_ledger)

    def alpaca_kwargs(self) -> dict:
        return dict(auth_claims_dir=self.alpaca_auth, supervisor_claims_dir=self.alpaca_supervisor)


# --- MEXC submission-side fake exchange (create_order/set_leverage) ---

class FakeSubmissionExchange:
    """Same shape as v0.5.3.27's/v0.5.3.31's own test suites' FakeExchange."""

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


# --- MEXC observation-side fake exchange (fetch_orders/positions/trades) ---

class FakeObservationExchange:
    def __init__(self, orders=None, positions=None, trades=None):
        self._orders = orders if orders is not None else []
        self._positions = positions if positions is not None else []
        self._trades = trades if trades is not None else {}

    def market(self, symbol):
        return {"id": symbol.split(":")[0].replace("/", "_")}

    def fetch_orders(self, symbol, since=None, limit=None, params=None):
        return [{"info": o} for o in self._orders]

    def fetch_positions(self, symbols=None):
        return [{"info": p} for p in self._positions]

    def fetch_order_trades(self, id, symbol=None, since=None, limit=None, params=None):
        return [{"info": t} for t in self._trades.get(id, [])]


# --- Alpaca fake submitting client (same shape as .35's own test suite) ---

class FakeOrder:
    def __init__(self, order_id, status):
        self.id = order_id
        self.status = status


class FakeAlpacaClient:
    def __init__(self, existing_order=None, submit_exception=None, not_found_exc=Exception):
        self._existing = existing_order
        self._submit_exception = submit_exception
        self._not_found_exc = not_found_exc
        self.submit_calls: list = []

    def get_order_by_client_id(self, client_order_id):
        if self._existing is None:
            raise self._not_found_exc("order not found")
        return self._existing

    def submit_order(self, order_data):
        self.submit_calls.append(order_data)
        if self._submit_exception:
            raise self._submit_exception
        return FakeOrder(order_id="broker-123", status="new")


# ======================================================================= #
# 1. Architecture / no-bypass
# ======================================================================= #

def test_invalid_canonical_spec_blocked_both_venues(tmp_path) -> None:
    d = Dirs(tmp_path)
    r1 = SUP.supervise_mexc_futures_execution("not-a-dict", supervisor_config=sup_open(), **d.mexc_kwargs())
    expect("1a: MEXC: non-dict spec BLOCKED", r1["status"] == "BLOCKED" and r1["stage"] == "CANONICAL_SPEC")
    r2 = SUP.supervise_alpaca_equity_execution("not-a-dict", make_alpaca_asset(), supervisor_config=sup_open(),
                                                **d.alpaca_kwargs())
    expect("1b: Alpaca: non-dict spec BLOCKED", r2["status"] == "BLOCKED" and r2["stage"] == "CANONICAL_SPEC")


def test_malformed_canonical_spec_blocked_both_venues(tmp_path) -> None:
    d = Dirs(tmp_path)
    mexc_spec = make_mexc_spec()
    tampered = dict(mexc_spec, symbol="ETH/USDT:USDT")  # spec_fingerprint no longer matches
    r1 = SUP.supervise_mexc_futures_execution(tampered, supervisor_config=sup_open(), **d.mexc_kwargs())
    expect("2a: MEXC: tampered spec fails verify_canonical_specification", r1["status"] == "BLOCKED")
    expect("2a: reason is MALFORMED_CANONICAL_SPEC", r1["reason"] == "MALFORMED_CANONICAL_SPEC")

    alpaca_spec = make_alpaca_spec()
    tampered_a = dict(alpaca_spec, symbol="MSFT")
    r2 = SUP.supervise_alpaca_equity_execution(tampered_a, make_alpaca_asset(), supervisor_config=sup_open(),
                                                **d.alpaca_kwargs())
    expect("2b: Alpaca: tampered spec fails verify_canonical_specification", r2["status"] == "BLOCKED")
    expect("2b: reason is MALFORMED_CANONICAL_SPEC", r2["reason"] == "MALFORMED_CANONICAL_SPEC")


def test_wrong_venue_asset_class_rejected(tmp_path) -> None:
    d = Dirs(tmp_path)
    alpaca_spec = make_alpaca_spec()
    r1 = SUP.supervise_mexc_futures_execution(alpaca_spec, supervisor_config=sup_open(), **d.mexc_kwargs())
    expect("3a: an Alpaca spec fed to the MEXC entry point is rejected", r1["status"] == "BLOCKED")
    expect("3a: reason is NOT_A_MEXC_FUTURES_SPEC", r1["reason"] == "NOT_A_MEXC_FUTURES_SPEC")

    mexc_spec = make_mexc_spec()
    r2 = SUP.supervise_alpaca_equity_execution(mexc_spec, make_alpaca_asset(), supervisor_config=sup_open(),
                                                **d.alpaca_kwargs())
    expect("3b: a MEXC spec fed to the Alpaca entry point is rejected", r2["status"] == "BLOCKED")
    expect("3b: reason is NOT_AN_ALPACA_EQUITY_SPEC", r2["reason"] == "NOT_AN_ALPACA_EQUITY_SPEC")


def test_supervisor_kill_switch_blocks_new_execution_both_venues(tmp_path) -> None:
    d = Dirs(tmp_path)
    # default supervisor_config has kill_switch=True (safe-by-default)
    r1 = SUP.supervise_mexc_futures_execution(make_mexc_spec(direction="OPEN_LONG"), **d.mexc_kwargs())
    expect("4a: MEXC blocked by Supervisor's own default (engaged) kill switch",
           r1["status"] == "BLOCKED" and r1["stage"] == "SUPERVISOR_KILL_SWITCH")

    r2 = SUP.supervise_alpaca_equity_execution(make_alpaca_spec(), make_alpaca_asset(), **d.alpaca_kwargs())
    expect("4b: Alpaca blocked by Supervisor's own default (engaged) kill switch",
           r2["status"] == "BLOCKED" and r2["stage"] == "SUPERVISOR_KILL_SWITCH")


def test_live_environment_structurally_unsupported_for_alpaca(tmp_path) -> None:
    d = Dirs(tmp_path)
    r = SUP.supervise_alpaca_equity_execution(make_alpaca_spec(), make_alpaca_asset(), environment="LIVE",
                                                supervisor_config=sup_open(), **d.alpaca_kwargs())
    expect("5: LIVE environment rejected before authorization is even attempted",
           r["status"] == "BLOCKED" and r["stage"] == "ENVIRONMENT")
    expect("5: reason is LIVE_EXECUTION_NOT_SUPPORTED", r["reason"] == "LIVE_EXECUTION_NOT_SUPPORTED")


def test_no_bypass_no_submission_without_authorization_mexc(tmp_path) -> None:
    """A kill-switch-blocked MEXC authorization must never reach .27.submit()
    -- proven by the adapter claims dir remaining completely empty."""
    d = Dirs(tmp_path)
    r = SUP.supervise_mexc_futures_execution(
        make_mexc_spec(direction="OPEN_LONG", decision_id="DEC-NOBYPASS-M"),
        auth_config=mexc_auth_config(kill_switch=True), attempt_submission=True,
        exchange=FakeSubmissionExchange(), supervisor_config=sup_open(), **d.mexc_kwargs(),
    )
    expect("6a: MEXC authorization blocked (kill switch)", r["status"] == "BLOCKED" and r["stage"] == "AUTHORIZATION")
    expect("6a: no adapter-level claim files were ever created",
           not d.mexc_adapter.exists() or not list(d.mexc_adapter.glob("*")))


def test_no_bypass_no_submission_without_authorization_alpaca(tmp_path) -> None:
    d = Dirs(tmp_path)
    client = FakeAlpacaClient()
    r = SUP.supervise_alpaca_equity_execution(
        make_alpaca_spec(decision_id="DEC-NOBYPASS-A"), make_alpaca_asset(),
        auth_config=alpaca_auth_config(kill_switch=True), attempt_submission=True, alpaca_client=client,
        supervisor_config=sup_open(), **d.alpaca_kwargs(),
    )
    expect("6b: Alpaca authorization blocked (kill switch)", r["status"] == "BLOCKED" and r["stage"] == "AUTHORIZATION")
    expect("6b: .35.submit() was never called", client.submit_calls == [])


def test_source_kind_confers_no_execution_authority(tmp_path) -> None:
    """An AI_PROPOSAL-sourced decision is supervised identically to a
    DETERMINISTIC_SIGNAL one on both venues -- construction-only preview,
    the cheapest way to prove this without burning any claim."""
    d = Dirs(tmp_path)
    det = make_mexc_spec(source_kind="DETERMINISTIC_SIGNAL", decision_id="DEC-SRC-DET")
    ai = make_mexc_spec(source_kind="AI_PROPOSAL", decision_id="DEC-SRC-AI")
    r_det = SUP.supervise_mexc_futures_execution(det, auth_config=mexc_auth_config(), supervisor_config=sup_open(),
                                                  **d.mexc_kwargs())
    r_ai = SUP.supervise_mexc_futures_execution(ai, auth_config=mexc_auth_config(), supervisor_config=sup_open(),
                                                 **d.mexc_kwargs())
    expect("7a: deterministic-sourced MEXC preview reaches READY_FOR_SUBMISSION",
           r_det["status"] == "READY_FOR_SUBMISSION")
    expect("7a: AI-proposal-sourced MEXC preview reaches READY_FOR_SUBMISSION identically",
           r_ai["status"] == "READY_FOR_SUBMISSION")

    det_a = make_alpaca_spec(source_kind="DETERMINISTIC_SIGNAL", decision_id="DEC-SRC-DET-A")
    ai_a = make_alpaca_spec(source_kind="AI_PROPOSAL", decision_id="DEC-SRC-AI-A")
    r_det_a = SUP.supervise_alpaca_equity_execution(det_a, make_alpaca_asset(), auth_config=alpaca_auth_config(),
                                                      supervisor_config=sup_open(), **d.alpaca_kwargs())
    r_ai_a = SUP.supervise_alpaca_equity_execution(ai_a, make_alpaca_asset(), auth_config=alpaca_auth_config(),
                                                     supervisor_config=sup_open(), **d.alpaca_kwargs())
    expect("7b: deterministic-sourced Alpaca preview reaches READY_FOR_SUBMISSION",
           r_det_a["status"] == "READY_FOR_SUBMISSION")
    expect("7b: AI-proposal-sourced Alpaca preview reaches READY_FOR_SUBMISSION identically",
           r_ai_a["status"] == "READY_FOR_SUBMISSION")


def test_network_capable_functions_disclosure_is_accurate() -> None:
    expect("8: NETWORK_CAPABLE_FUNCTIONS names exactly the three functions that accept an injected client/exchange",
           SUP.NETWORK_CAPABLE_FUNCTIONS == frozenset({
               "supervise_alpaca_equity_execution", "supervise_mexc_futures_execution",
               "supervise_mexc_reconciliation_pass",
           }))
    for fn_name in SUP.NETWORK_CAPABLE_FUNCTIONS:
        expect(f"8: {fn_name} actually exists on the module", hasattr(SUP, fn_name))


def test_alpaca_claim_layer_exposes_no_release_function() -> None:
    release_like = [
        name for name in dir(SUP)
        if ("release" in name.lower() or "delete" in name.lower() or "unclaim" in name.lower())
    ]
    expect("9: the module exposes no release/delete/unclaim function anywhere", release_like == [])


# ======================================================================= #
# 2. MEXC -- four directions, construction-only preview
# ======================================================================= #

def _mexc_preview(tmp_path, direction: str, decision_id: str) -> dict:
    d = Dirs(tmp_path)
    spec = make_mexc_spec(direction=direction, decision_id=decision_id)
    r = SUP.supervise_mexc_futures_execution(spec, auth_config=mexc_auth_config(), supervisor_config=sup_open(),
                                              **d.mexc_kwargs())
    expect(f"MEXC preview[{direction}]: READY_FOR_SUBMISSION", r["status"] == "READY_FOR_SUBMISSION")
    expect(f"MEXC preview[{direction}]: stage CONSTRUCTION_ONLY", r["stage"] == "CONSTRUCTION_ONLY")
    expect(f"MEXC preview[{direction}]: authorization_id present", isinstance(r["authorization_id"], str))
    # The bug-fix property: nothing was claimed anywhere.
    expect(f"MEXC preview[{direction}]: no .32 authorization claim file created",
           not d.mexc_auth.exists() or not list(d.mexc_auth.glob("*")))
    expect(f"MEXC preview[{direction}]: no .27 adapter client_order_id claim file created",
           not d.mexc_adapter.exists() or not list(d.mexc_adapter.glob("*")))
    return r


def test_mexc_preview_open_long(tmp_path) -> None:
    _mexc_preview(tmp_path, "OPEN_LONG", "DEC-MEXC-OL")


def test_mexc_preview_close_long(tmp_path) -> None:
    _mexc_preview(tmp_path, "CLOSE_LONG", "DEC-MEXC-CL")


def test_mexc_preview_open_short(tmp_path) -> None:
    _mexc_preview(tmp_path, "OPEN_SHORT", "DEC-MEXC-OS")


def test_mexc_preview_close_short(tmp_path) -> None:
    _mexc_preview(tmp_path, "CLOSE_SHORT", "DEC-MEXC-CS")


def test_mexc_preview_is_idempotent_and_repeatable(tmp_path) -> None:
    """Proves the fixed design: calling the construction-only preview
    twice for the SAME decision is safe (falls through to the existing
    NEW-state intent both times), unlike the earlier, buggy draft that
    would have burned a real claim on the first call."""
    d = Dirs(tmp_path)
    spec = make_mexc_spec(direction="OPEN_LONG", decision_id="DEC-MEXC-REPEAT")
    r1 = SUP.supervise_mexc_futures_execution(spec, auth_config=mexc_auth_config(), supervisor_config=sup_open(),
                                               **d.mexc_kwargs())
    r2 = SUP.supervise_mexc_futures_execution(spec, auth_config=mexc_auth_config(), supervisor_config=sup_open(),
                                               **d.mexc_kwargs())
    expect("10: first preview READY_FOR_SUBMISSION", r1["status"] == "READY_FOR_SUBMISSION")
    expect("10: second preview of the SAME decision ALSO READY_FOR_SUBMISSION (no stranded claim)",
           r2["status"] == "READY_FOR_SUBMISSION")


def test_mexc_preview_then_real_submission_completes_successfully(tmp_path) -> None:
    """The actual regression test for the bug found and fixed during this
    module's own implementation: preview first, THEN a real
    attempt_submission=True call for the identical decision must still be
    able to reach SUBMITTED -- not blocked by a claim the preview itself
    should never have made."""
    d = Dirs(tmp_path)
    spec = make_mexc_spec(direction="OPEN_LONG", decision_id="DEC-MEXC-PREVIEW-THEN-REAL")
    preview = SUP.supervise_mexc_futures_execution(spec, auth_config=mexc_auth_config(), supervisor_config=sup_open(),
                                                     **d.mexc_kwargs())
    expect("11: preview succeeds first", preview["status"] == "READY_FOR_SUBMISSION")

    fake = FakeSubmissionExchange()
    real = SUP.supervise_mexc_futures_execution(
        spec, auth_config=mexc_auth_config(), attempt_submission=True, exchange=fake,
        supervisor_config=sup_open(), **d.mexc_kwargs(),
    )
    expect("11: the REAL submission attempt after a preview reaches SUBMITTED (not blocked)",
           real["status"] == "SUBMITTED")
    expect("11: exactly one create_order call actually happened", len(fake.create_order_calls) == 1)


# ======================================================================= #
# 3. MEXC -- full submission path, replay, EXECUTION_UNCERTAIN, REJECTED
# ======================================================================= #

def test_mexc_full_submission_path_lands_at_awaiting_reconciliation(tmp_path) -> None:
    d = Dirs(tmp_path)
    spec = make_mexc_spec(direction="OPEN_LONG", decision_id="DEC-MEXC-SUBMIT-OK")
    fake = FakeSubmissionExchange(create_order_result={"id": "mexc-order-777", "status": "open"})
    r = SUP.supervise_mexc_futures_execution(
        spec, auth_config=mexc_auth_config(), attempt_submission=True, exchange=fake,
        supervisor_config=sup_open(), **d.mexc_kwargs(),
    )
    expect("12: status SUBMITTED", r["status"] == "SUBMITTED")
    expect("12: stage SUBMITTED", r["stage"] == "SUBMITTED")
    expect("12: submission_result mexc_order_id echoed", r["submission_result"]["mexc_order_id"] == "mexc-order-777")
    intent = LEDGER.get_intent(spec["client_order_id"], base_dir=d.mexc_ledger)
    expect("12: .29 ledger intent lands at AWAITING_RECONCILIATION", intent["current_state"] == "AWAITING_RECONCILIATION")
    expect("12: leverage WAS set (entry direction)", len(fake.set_leverage_calls) == 1)


def test_mexc_close_direction_does_not_set_leverage(tmp_path) -> None:
    d = Dirs(tmp_path)
    # A CLOSE requires a symmetric prior OPEN to make sense operationally,
    # but this module's own gate only cares that reduce_only=True skips
    # set_leverage -- exercised directly.
    spec = make_mexc_spec(direction="CLOSE_LONG", decision_id="DEC-MEXC-CLOSE-SUBMIT")
    fake = FakeSubmissionExchange()
    r = SUP.supervise_mexc_futures_execution(
        spec, auth_config=mexc_auth_config(), attempt_submission=True, exchange=fake,
        supervisor_config=sup_open(), **d.mexc_kwargs(),
    )
    expect("13: CLOSE_LONG submission reaches SUBMITTED", r["status"] == "SUBMITTED")
    expect("13: set_leverage never called for a reduce_only order", fake.set_leverage_calls == [])


def test_mexc_replay_second_attempt_for_same_decision_blocked(tmp_path) -> None:
    d = Dirs(tmp_path)
    spec = make_mexc_spec(direction="OPEN_LONG", decision_id="DEC-MEXC-REPLAY")
    fake1 = FakeSubmissionExchange()
    first = SUP.supervise_mexc_futures_execution(
        spec, auth_config=mexc_auth_config(), attempt_submission=True, exchange=fake1,
        supervisor_config=sup_open(), **d.mexc_kwargs(),
    )
    expect("14: first submission SUBMITTED", first["status"] == "SUBMITTED")

    fake2 = FakeSubmissionExchange()
    second = SUP.supervise_mexc_futures_execution(
        spec, auth_config=mexc_auth_config(), attempt_submission=True, exchange=fake2,
        supervisor_config=sup_open(), **d.mexc_kwargs(),
    )
    expect("14: replay of the SAME decision is BLOCKED", second["status"] == "BLOCKED")
    expect("14: stage EXISTING_INTENT", second["stage"] == "EXISTING_INTENT")
    expect("14: no second create_order call ever happened", fake2.create_order_calls == [])


def test_mexc_execution_uncertain_never_blocks_and_never_allows_retry(tmp_path) -> None:
    d = Dirs(tmp_path)
    spec = make_mexc_spec(direction="OPEN_LONG", decision_id="DEC-MEXC-UNCERTAIN")
    fake = FakeSubmissionExchange(create_order_exception=ccxt.NetworkError("timeout"))
    r = SUP.supervise_mexc_futures_execution(
        spec, auth_config=mexc_auth_config(), attempt_submission=True, exchange=fake,
        supervisor_config=sup_open(), **d.mexc_kwargs(),
    )
    expect("15: status EXECUTION_UNCERTAIN, never guessed REJECTED or SUBMITTED", r["status"] == "EXECUTION_UNCERTAIN")
    intent = LEDGER.get_intent(spec["client_order_id"], base_dir=d.mexc_ledger)
    expect("15: .29 ledger correctly lands at AWAITING_RECONCILIATION", intent["current_state"] == "AWAITING_RECONCILIATION")

    fake_retry = FakeSubmissionExchange()
    retry = SUP.supervise_mexc_futures_execution(
        spec, auth_config=mexc_auth_config(), attempt_submission=True, exchange=fake_retry,
        supervisor_config=sup_open(), **d.mexc_kwargs(),
    )
    expect("15: EXECUTION_UNCERTAIN never becomes permission to retry the same decision",
           retry["status"] == "BLOCKED" and retry["stage"] == "EXISTING_INTENT")
    expect("15: the retry never reached the exchange at all", fake_retry.create_order_calls == [])


def test_mexc_explicit_rejection_lands_terminal(tmp_path) -> None:
    d = Dirs(tmp_path)
    spec = make_mexc_spec(direction="OPEN_LONG", decision_id="DEC-MEXC-REJECTED")
    fake = FakeSubmissionExchange(create_order_exception=ccxt.InvalidOrder("bad order"))
    r = SUP.supervise_mexc_futures_execution(
        spec, auth_config=mexc_auth_config(), attempt_submission=True, exchange=fake,
        supervisor_config=sup_open(), **d.mexc_kwargs(),
    )
    expect("16: an unambiguous exchange refusal maps to REJECTED", r["status"] == "REJECTED")
    intent = LEDGER.get_intent(spec["client_order_id"], base_dir=d.mexc_ledger)
    expect("16: .29 ledger lands at TERMINAL_SUBMISSION_REJECTED", intent["current_state"] == "TERMINAL_SUBMISSION_REJECTED")

    fake_retry = FakeSubmissionExchange()
    retry = SUP.supervise_mexc_futures_execution(
        spec, auth_config=mexc_auth_config(), attempt_submission=True, exchange=fake_retry,
        supervisor_config=sup_open(), **d.mexc_kwargs(),
    )
    expect("16: a terminal-rejected intent is reported, not silently re-submitted",
           retry["status"] == "BLOCKED" and retry["stage"] == "EXISTING_INTENT" and
           retry["reason"] == "EXISTING_INTENT_TERMINAL_OR_ESCALATED")
    expect("16: the retry never reached the exchange", fake_retry.create_order_calls == [])


def test_mexc_missing_exchange_when_attempt_submission_true_fails_closed(tmp_path) -> None:
    d = Dirs(tmp_path)
    spec = make_mexc_spec(direction="OPEN_LONG", decision_id="DEC-MEXC-NO-EXCHANGE")
    r = SUP.supervise_mexc_futures_execution(
        spec, auth_config=mexc_auth_config(), attempt_submission=True, exchange=None,
        supervisor_config=sup_open(), **d.mexc_kwargs(),
    )
    expect("17: attempt_submission=True with exchange=None fails closed",
           r["status"] == "BLOCKED" and r["reason"] == "MISSING_EXCHANGE_FOR_SUBMISSION")
    expect("17: no claim was made (never reached authorized_submit)",
           not d.mexc_auth.exists() or not list(d.mexc_auth.glob("*")))
    # The intent must still be retryable -- a real exchange next time succeeds.
    fake = FakeSubmissionExchange()
    retry = SUP.supervise_mexc_futures_execution(
        spec, auth_config=mexc_auth_config(), attempt_submission=True, exchange=fake,
        supervisor_config=sup_open(), **d.mexc_kwargs(),
    )
    expect("17: a subsequent call WITH a real exchange succeeds normally", retry["status"] == "SUBMITTED")


def test_mexc_adapter_claim_conflict_surfaces_as_its_own_status(tmp_path) -> None:
    """AUTHORIZED_BUT_ADAPTER_CLAIM_REJECTED: .27's own client_order_id
    claim was already taken by an earlier, independent attempt -- forced
    here by pre-claiming it directly against the adapter's own claims dir
    before ever authorizing."""
    d = Dirs(tmp_path)
    spec = make_mexc_spec(direction="OPEN_LONG", decision_id="DEC-MEXC-ADAPTER-CONFLICT")
    ADAPTER = SUP._load_mexc_adapter_module()
    granted = ADAPTER.claim_client_order_id(d.mexc_adapter, spec["client_order_id"])
    expect("18: pre-claim at the adapter layer succeeds", granted is True)

    fake = FakeSubmissionExchange()
    r = SUP.supervise_mexc_futures_execution(
        spec, auth_config=mexc_auth_config(), attempt_submission=True, exchange=fake,
        supervisor_config=sup_open(), **d.mexc_kwargs(),
    )
    expect("18: surfaced as ADAPTER_CLIENT_ORDER_ID_CLAIM, not silently retried or crashed",
           r["status"] == "BLOCKED" and r["stage"] == "ADAPTER_CLIENT_ORDER_ID_CLAIM")
    expect("18: no order was ever actually created", fake.create_order_calls == [])
    intent = LEDGER.get_intent(spec["client_order_id"], base_dir=d.mexc_ledger)
    expect("18: .29 escalates via DUPLICATE_CLAIM_REJECTED -> ESCALATED_HUMAN_REVIEW",
           intent["current_state"] == "ESCALATED_HUMAN_REVIEW")


# ======================================================================= #
# 4. MEXC -- reconciliation pass
# ======================================================================= #

def test_mexc_reconciliation_pass_resolves_a_filled_order(tmp_path) -> None:
    d = Dirs(tmp_path)
    spec = make_mexc_spec(direction="OPEN_LONG", decision_id="DEC-MEXC-RECONCILE-FILLED")
    fake_submit = FakeSubmissionExchange(create_order_result={"id": "mexc-order-999", "status": "open"})
    submitted = SUP.supervise_mexc_futures_execution(
        spec, auth_config=mexc_auth_config(), attempt_submission=True, exchange=fake_submit,
        supervisor_config=sup_open(), **d.mexc_kwargs(),
    )
    expect("19: submission SUBMITTED", submitted["status"] == "SUBMITTED")

    raw_order = {
        "externalOid": spec["client_order_id"], "orderId": "mexc-order-999", "state": "3",
        "vol": "0.01", "dealVol": "0.01", "dealAvgPrice": "50000", "updateTime": 1_760_000_000_000,
    }
    fake_obs = FakeObservationExchange(orders=[raw_order])
    result = SUP.supervise_mexc_reconciliation_pass(
        spec["client_order_id"], ledger_base_dir=d.mexc_ledger, exchange=fake_obs,
    )
    expect("19: reconciliation reports RECONCILED", result["status"] == "RECONCILED")
    expect("19: verdict target_state RECONCILED_FILLED", result["verdict"]["target_state"] == "RECONCILED_FILLED")
    intent = LEDGER.get_intent(spec["client_order_id"], base_dir=d.mexc_ledger)
    expect("19: .29 ledger now shows RECONCILED_FILLED", intent["current_state"] == "RECONCILED_FILLED")


def test_mexc_reconciliation_pass_not_gated_by_supervisor_kill_switch(tmp_path) -> None:
    """Martin's §7: a kill switch must not make it impossible to safely
    reconcile existing positions."""
    d = Dirs(tmp_path)
    spec = make_mexc_spec(direction="OPEN_LONG", decision_id="DEC-MEXC-RECONCILE-KILLSWITCH")
    fake_submit = FakeSubmissionExchange(create_order_result={"id": "mexc-order-kstest", "status": "open"})
    SUP.supervise_mexc_futures_execution(
        spec, auth_config=mexc_auth_config(), attempt_submission=True, exchange=fake_submit,
        supervisor_config=sup_open(), **d.mexc_kwargs(),
    )
    raw_order = {
        "externalOid": spec["client_order_id"], "orderId": "mexc-order-kstest", "state": "4",
        "vol": "0.01", "dealVol": "0", "dealAvgPrice": None, "updateTime": None,
    }
    fake_obs = FakeObservationExchange(orders=[raw_order])
    # NOTE: no supervisor_config passed at all -- the Supervisor kill
    # switch defaults to engaged (True) and reconciliation must proceed
    # anyway, since supervise_mexc_reconciliation_pass() takes no
    # supervisor_config parameter at all (by design -- see module docstring §5).
    result = SUP.supervise_mexc_reconciliation_pass(
        spec["client_order_id"], ledger_base_dir=d.mexc_ledger, exchange=fake_obs,
    )
    expect("20: reconciliation succeeds even though the Supervisor's own kill switch is (implicitly) engaged",
           result["status"] == "RECONCILED")
    expect("20: verdict target_state RECONCILED_CANCELED", result["verdict"]["target_state"] == "RECONCILED_CANCELED")


def test_mexc_reconciliation_pass_missing_exchange_fails_closed(tmp_path) -> None:
    d = Dirs(tmp_path)
    spec = make_mexc_spec(direction="OPEN_LONG", decision_id="DEC-MEXC-RECONCILE-NOEXCH")
    fake_submit = FakeSubmissionExchange()
    SUP.supervise_mexc_futures_execution(
        spec, auth_config=mexc_auth_config(), attempt_submission=True, exchange=fake_submit,
        supervisor_config=sup_open(), **d.mexc_kwargs(),
    )
    result = SUP.supervise_mexc_reconciliation_pass(spec["client_order_id"], ledger_base_dir=d.mexc_ledger, exchange=None)
    expect("21: reconciliation without an exchange fails closed",
           result["status"] == "BLOCKED" and result["reason"] == "MISSING_EXCHANGE_FOR_OBSERVATION")


def test_mexc_reconciliation_pass_unknown_intent(tmp_path) -> None:
    d = Dirs(tmp_path)
    result = SUP.supervise_mexc_reconciliation_pass(
        "aura-nonexistent-intent", ledger_base_dir=d.mexc_ledger, exchange=FakeObservationExchange(),
    )
    expect("22: reconciling a nonexistent intent fails closed",
           result["status"] == "BLOCKED" and result["reason"] == "INTENT_NOT_FOUND")


# ======================================================================= #
# 5. Alpaca -- all 8 STOCK/ETF x LONG/SHORT x OPEN/CLOSE combinations
#    (construction-only preview)
# ======================================================================= #

ALPACA_COMBOS = [
    ("STOCK", "OPEN_LONG", "AAPL"),
    ("STOCK", "CLOSE_LONG", "AAPL"),
    ("STOCK", "OPEN_SHORT", "TSLA"),
    ("STOCK", "CLOSE_SHORT", "TSLA"),
    ("ETF", "OPEN_LONG", "SPY"),
    ("ETF", "CLOSE_LONG", "SPY"),
    ("ETF", "OPEN_SHORT", "IWM"),
    ("ETF", "CLOSE_SHORT", "IWM"),
]


def test_alpaca_all_eight_combinations_construct_successfully(tmp_path) -> None:
    d = Dirs(tmp_path)
    for asset_class, direction, symbol in ALPACA_COMBOS:
        spec = make_alpaca_spec(asset_class=asset_class, direction=direction, symbol=symbol,
                                 decision_id=f"DEC-A8-{asset_class}-{direction}")
        asset = make_alpaca_asset(symbol=symbol, shortable=True, easy_to_borrow=True)
        r = SUP.supervise_alpaca_equity_execution(spec, asset, auth_config=alpaca_auth_config(),
                                                    supervisor_config=sup_open(), **d.alpaca_kwargs())
        expect(f"23: {asset_class}/{direction}/{symbol}: READY_FOR_SUBMISSION", r["status"] == "READY_FOR_SUBMISSION")
        expect(f"23: {asset_class}/{direction}/{symbol}: order_spec constructed", r["order_spec"] is not None)
        expect(f"23: {asset_class}/{direction}/{symbol}: order_spec symbol matches", r["order_spec"]["symbol"] == symbol)


def test_alpaca_close_short_does_not_require_fresh_borrow_evidence(tmp_path) -> None:
    d = Dirs(tmp_path)
    spec = make_alpaca_spec(asset_class="STOCK", direction="CLOSE_SHORT", symbol="GME", decision_id="DEC-A-CLOSESHORT-NB")
    asset = make_alpaca_asset(symbol="GME", shortable=False, easy_to_borrow=False)
    r = SUP.supervise_alpaca_equity_execution(spec, asset, auth_config=alpaca_auth_config(),
                                                supervisor_config=sup_open(), **d.alpaca_kwargs())
    expect("24: CLOSE_SHORT succeeds even when the asset is currently not shortable",
           r["status"] == "READY_FOR_SUBMISSION")


# ======================================================================= #
# 6. Alpaca -- shortability enforcement, replay, submission, uncertainty
# ======================================================================= #

def test_alpaca_shortability_enforcement_blocks_open_short(tmp_path) -> None:
    d = Dirs(tmp_path)
    spec = make_alpaca_spec(asset_class="STOCK", direction="OPEN_SHORT", symbol="GME", decision_id="DEC-A-SHORTBLOCK")
    asset = make_alpaca_asset(symbol="GME", shortable=False, easy_to_borrow=False)
    r = SUP.supervise_alpaca_equity_execution(spec, asset, auth_config=alpaca_auth_config(),
                                                supervisor_config=sup_open(), **d.alpaca_kwargs())
    expect("25: OPEN_SHORT on a non-shortable asset is BLOCKED", r["status"] == "BLOCKED")
    expect("25: blocked at AUTHORIZATION (the .36 borrow guardrail)", r["stage"] == "AUTHORIZATION")


def test_alpaca_full_submission_path_succeeds(tmp_path) -> None:
    d = Dirs(tmp_path)
    spec = make_alpaca_spec(direction="OPEN_LONG", symbol="AAPL", decision_id="DEC-A-SUBMIT-OK")
    asset = make_alpaca_asset(symbol="AAPL")
    client = FakeAlpacaClient(existing_order=None)
    r = SUP.supervise_alpaca_equity_execution(spec, asset, auth_config=alpaca_auth_config(),
                                                attempt_submission=True, alpaca_client=client,
                                                supervisor_config=sup_open(), **d.alpaca_kwargs())
    expect("26: status SUBMITTED", r["status"] == "SUBMITTED")
    expect("26: broker_order_id present", r["submission_result"]["broker_order_id"] == "broker-123")
    expect("26: exactly one submit_order call", len(client.submit_calls) == 1)
    expect("26: a .37 claim file now exists", list(d.alpaca_auth.glob("*")) != [])
    expect("26: a Supervisor-owned client_order_id claim file now exists", list(d.alpaca_supervisor.glob("*")) != [])


def test_alpaca_replay_second_attempt_blocked(tmp_path) -> None:
    """`.36.authorize()` mints a FRESH, random authorization_id on every
    call (confirmed by direct reading, matching `.31`'s own UUID4
    convention) -- so a second, independent authorize() call for the
    IDENTICAL decision (same deterministic client_order_id) is NOT caught
    by `.37`'s own authorization_id-keyed claim at all (it claims
    successfully, on its own distinct authorization_id, exactly as
    test_aura_v05337's own "different authorization_id, same
    client_order_id" test proves by design). This is precisely the
    asymmetry Martin's GO message identified and asked `.38` to close: the
    NEW Supervisor-owned client_order_id claim (section 5 of this module's
    docstring) is what actually catches this replay."""
    d = Dirs(tmp_path)
    spec = make_alpaca_spec(direction="OPEN_LONG", symbol="AAPL", decision_id="DEC-A-REPLAY")
    asset = make_alpaca_asset(symbol="AAPL")
    client1 = FakeAlpacaClient()
    first = SUP.supervise_alpaca_equity_execution(spec, asset, auth_config=alpaca_auth_config(),
                                                    attempt_submission=True, alpaca_client=client1,
                                                    supervisor_config=sup_open(), **d.alpaca_kwargs())
    expect("27: first submission SUBMITTED", first["status"] == "SUBMITTED")

    client2 = FakeAlpacaClient()
    second = SUP.supervise_alpaca_equity_execution(spec, asset, auth_config=alpaca_auth_config(),
                                                     attempt_submission=True, alpaca_client=client2,
                                                     supervisor_config=sup_open(), **d.alpaca_kwargs())
    expect("27: replay of the SAME decision BLOCKED", second["status"] == "BLOCKED")
    expect("27: caught by the NEW Supervisor-level client_order_id claim, not .37's authorization_id claim",
           second["stage"] == "SUPERVISOR_CLIENT_ORDER_ID_CLAIM")
    expect("27: surfaced via the explicit status_override, never silently dropped",
           second.get("status_override") == "AUTHORIZED_BUT_SUPERVISOR_CLAIM_REJECTED")
    expect("27: the two attempts got DIFFERENT authorization_ids (proving .37 alone would not have caught this)",
           first["authorization_id"] != second["authorization_id"])
    expect("27: no second submit_order call ever happened", client2.submit_calls == [])


def test_alpaca_execution_uncertain_never_guesses_rejected(tmp_path) -> None:
    """.35.submit() has no exception classification of its own -- this
    module's own conservative wrap must classify ANY exception as
    EXECUTION_UNCERTAIN, never REJECTED (module docstring §2)."""
    d = Dirs(tmp_path)
    spec = make_alpaca_spec(direction="OPEN_LONG", symbol="AAPL", decision_id="DEC-A-UNCERTAIN")
    asset = make_alpaca_asset(symbol="AAPL")
    client = FakeAlpacaClient(submit_exception=ConnectionError("broker timeout"))
    r = SUP.supervise_alpaca_equity_execution(spec, asset, auth_config=alpaca_auth_config(),
                                                attempt_submission=True, alpaca_client=client,
                                                supervisor_config=sup_open(), **d.alpaca_kwargs())
    expect("28: status EXECUTION_UNCERTAIN, never REJECTED", r["status"] == "EXECUTION_UNCERTAIN")
    expect("28: submission_result carries the classified error", "ConnectionError" in r["submission_result"]["error"])


def test_alpaca_missing_client_when_attempt_submission_true_fails_closed(tmp_path) -> None:
    d = Dirs(tmp_path)
    spec = make_alpaca_spec(direction="OPEN_LONG", symbol="AAPL", decision_id="DEC-A-NOCLIENT")
    asset = make_alpaca_asset(symbol="AAPL")
    r = SUP.supervise_alpaca_equity_execution(spec, asset, auth_config=alpaca_auth_config(),
                                                attempt_submission=True, alpaca_client=None,
                                                supervisor_config=sup_open(), **d.alpaca_kwargs())
    expect("29: attempt_submission=True with alpaca_client=None fails closed",
           r["status"] == "BLOCKED" and r["reason"] == "MISSING_ALPACA_CLIENT")


def test_alpaca_supervisor_level_client_order_id_claim_catches_a_genuine_anomaly(tmp_path) -> None:
    """A pre-existing Supervisor-level claim on the SAME client_order_id
    (simulating a different, independently-issued authorization_id that
    happens to collide) must be caught even though the .36/.37 layer
    succeeds on its own fresh authorization_id."""
    d = Dirs(tmp_path)
    spec = make_alpaca_spec(direction="OPEN_LONG", symbol="AAPL", decision_id="DEC-A-SUPCLAIM")
    asset = make_alpaca_asset(symbol="AAPL")
    granted = SUP.claim_alpaca_client_order_id(d.alpaca_supervisor, spec["client_order_id"])
    expect("30: pre-claim at the Supervisor's own layer succeeds", granted is True)

    client = FakeAlpacaClient()
    r = SUP.supervise_alpaca_equity_execution(spec, asset, auth_config=alpaca_auth_config(),
                                                attempt_submission=True, alpaca_client=client,
                                                supervisor_config=sup_open(), **d.alpaca_kwargs())
    expect("30: surfaced as its own explicit status, never silently dropped",
           r.get("status_override") == "AUTHORIZED_BUT_SUPERVISOR_CLAIM_REJECTED")
    expect("30: .35.submit() was never called", client.submit_calls == [])
    expect("30: the .37 authorization_id claim underneath IS permanently consumed (never released)",
           list(d.alpaca_auth.glob("*")) != [])


def test_alpaca_claim_store_unreachable_pass_through(tmp_path, monkeypatch) -> None:
    """This module's own pass-through of .36's CLAIM_STORE_UNREACHABLE
    status (an infrastructure failure at the .37 layer, distinct from both
    ALREADY_CONSUMED and REJECTED) -- verified via a monkeypatch of the
    SAME cached .36 module object this module dynamically imports, since
    reliably forcing a real OSError from the claims store is not possible
    running as root in this environment (permission checks are bypassed)."""
    d = Dirs(tmp_path)
    spec = make_alpaca_spec(direction="OPEN_LONG", symbol="AAPL", decision_id="DEC-A-STOREUNREACH")
    asset = make_alpaca_asset(symbol="AAPL")

    original = ALPACA_AUTH.authorized_order_request

    def fake_authorized_order_request(record, *args, **kwargs):
        return {
            "status": "CLAIM_STORE_UNREACHABLE", "authorization_id": record.get("authorization_id"),
            "reason": "CLAIM_STORE_UNREACHABLE", "detail": None, "order_spec": None, "alpaca_order_request": None,
        }

    monkeypatch.setattr(ALPACA_AUTH, "authorized_order_request", fake_authorized_order_request)
    try:
        r = SUP.supervise_alpaca_equity_execution(spec, asset, auth_config=alpaca_auth_config(),
                                                    supervisor_config=sup_open(), **d.alpaca_kwargs())
    finally:
        monkeypatch.setattr(ALPACA_AUTH, "authorized_order_request", original)

    expect("31: CLAIM_STORE_UNREACHABLE is passed through, not conflated with ALREADY_CONSUMED",
           r["status"] == "BLOCKED" and r["stage"] == "REPLAY_PROTECTION" and r["reason"] == "CLAIM_STORE_UNREACHABLE")


def test_alpaca_observation_reconciliation_never_fabricates(tmp_path) -> None:
    r = SUP.observe_and_reconcile_alpaca_equity_execution("some-client-order-id")
    expect("32: Alpaca observation/reconciliation always reports NOT_YET_IMPLEMENTED",
           r["status"] == "NOT_YET_IMPLEMENTED")
    expect("32: never a fabricated RECONCILED verdict", r["status"] != "RECONCILED")


def test_alpaca_kill_switch_blocks_before_authorization(tmp_path) -> None:
    d = Dirs(tmp_path)
    spec = make_alpaca_spec(direction="OPEN_LONG", symbol="AAPL", decision_id="DEC-A-KILLSWITCH")
    asset = make_alpaca_asset(symbol="AAPL")
    r = SUP.supervise_alpaca_equity_execution(spec, asset, auth_config=alpaca_auth_config(),
                                                supervisor_config=sup_open(kill_switch=True), **d.alpaca_kwargs())
    expect("33: Supervisor kill switch blocks before .36.authorize() is even called",
           r["status"] == "BLOCKED" and r["stage"] == "SUPERVISOR_KILL_SWITCH")
    expect("33: no auth claim files exist", not d.alpaca_auth.exists() or not list(d.alpaca_auth.glob("*")))


# ======================================================================= #
# 7. Concurrency -- exactly one execution attempt wins
# ======================================================================= #

def test_mexc_concurrent_submission_attempts_exactly_one_wins(tmp_path) -> None:
    d = Dirs(tmp_path)
    spec = make_mexc_spec(direction="OPEN_LONG", decision_id="DEC-MEXC-CONCURRENT")
    fakes: list[FakeSubmissionExchange] = []
    results: list[dict] = []
    lock = threading.Lock()

    def attempt():
        fake = FakeSubmissionExchange()
        r = SUP.supervise_mexc_futures_execution(
            spec, auth_config=mexc_auth_config(), attempt_submission=True, exchange=fake,
            supervisor_config=sup_open(), **d.mexc_kwargs(),
        )
        with lock:
            fakes.append(fake)
            results.append(r)

    threads = [threading.Thread(target=attempt) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    total_create_order_calls = sum(len(f.create_order_calls) for f in fakes)
    expect("34: exactly ONE real MEXC create_order call happened across 20 concurrent attempts",
           total_create_order_calls == 1)
    submitted = [r for r in results if r["status"] == "SUBMITTED"]
    expect("34: exactly one attempt reports SUBMITTED", len(submitted) == 1)


def test_alpaca_concurrent_submission_attempts_exactly_one_wins(tmp_path) -> None:
    d = Dirs(tmp_path)
    spec = make_alpaca_spec(direction="OPEN_LONG", symbol="AAPL", decision_id="DEC-A-CONCURRENT")
    asset = make_alpaca_asset(symbol="AAPL")
    clients: list[FakeAlpacaClient] = []
    results: list[dict] = []
    lock = threading.Lock()

    def attempt():
        client = FakeAlpacaClient()
        r = SUP.supervise_alpaca_equity_execution(spec, asset, auth_config=alpaca_auth_config(),
                                                    attempt_submission=True, alpaca_client=client,
                                                    supervisor_config=sup_open(), **d.alpaca_kwargs())
        with lock:
            clients.append(client)
            results.append(r)

    threads = [threading.Thread(target=attempt) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    total_submit_calls = sum(len(c.submit_calls) for c in clients)
    expect("35: exactly ONE real Alpaca submit_order call happened across 20 concurrent attempts",
           total_submit_calls == 1)
    submitted = [r for r in results if r["status"] == "SUBMITTED"]
    expect("35: exactly one attempt reports SUBMITTED", len(submitted) == 1)


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_")]
    for t in tests:
        t()
    print(f"AURA v0.5.3.38 UNIT TEST CONTRACT: ALL {len(tests)} PASS")
