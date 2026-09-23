#!/usr/bin/env python3
"""Contract + integration tests for AURA v0.5.3.55 Stage 1 paper-trading runner.

Mirrors `.53`'s own test convention: `.33`/`.34`/`.35`/`.36`/`.37`/`.38`/
`.43`/`.44`/`.47`/`.49`/`.50`/`.53`/`.54` are loaded into `sys.modules`
under their canonical names BEFORE `.55` is loaded, so `.55`'s own dynamic
imports resolve to the SAME module objects these tests build fixtures
against. Only the outermost edges are faked: a `.47` SentimentRegime
fixture (duplicated, matching this project's "each test file builds its
own fixtures" convention), `.54`'s real `NeutralDeterministicLLMClient`
stub (real, not faked -- it IS the Stage 1 LLM client), and one
FakeAlpacaClient standing in for `alpaca-py`'s `TradingClient`.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


CANON = _load("aura_v05333_canonical_execution_specification", ROOT / "aura_v05333_canonical_execution_specification.py")
META = _load("aura_v05334_asset_instrument_metadata", ROOT / "aura_v05334_asset_instrument_metadata.py")
ADAPTER35 = _load("aura_v05335_alpaca_equity_execution_adapter", ROOT / "aura_v05335_alpaca_equity_execution_adapter.py")
AUTH36 = _load("aura_v05336_alpaca_equity_execution_authorization", ROOT / "aura_v05336_alpaca_equity_execution_authorization.py")
REPLAY37 = _load("aura_v05337_alpaca_replay_protected_consumption", ROOT / "aura_v05337_alpaca_replay_protected_consumption.py")
SUP = _load("aura_v05338_common_execution_supervisor", ROOT / "aura_v05338_common_execution_supervisor.py")
OBS = _load("aura_v05343_portfolio_exposure_observability", ROOT / "aura_v05343_portfolio_exposure_observability.py")
ENFORCE = _load("aura_v05344_portfolio_exposure_enforcement", ROOT / "aura_v05344_portfolio_exposure_enforcement.py")
SENT = _load("aura_v05347_market_sentiment_scoring", ROOT / "aura_v05347_market_sentiment_scoring.py")
PROPOSAL = _load("aura_v05349_ai_proposal_pipeline", ROOT / "aura_v05349_ai_proposal_pipeline.py")
ENGINE50 = _load("aura_v05350_decision_engine", ROOT / "aura_v05350_decision_engine.py")
CYCLE = _load("aura_v05353_full_paper_orchestration", ROOT / "aura_v05353_full_paper_orchestration.py")
FILL = _load("aura_v05354_alpaca_equity_fill_reconciliation", ROOT / "aura_v05354_alpaca_equity_fill_reconciliation.py")
STUB = _load("aura_v054_llm_stub", ROOT / "aura_v054_llm_stub.py")
M = _load("aura_v05355_stage1_paper_trading_runner", ROOT / "aura_v05355_stage1_paper_trading_runner.py")

NOW = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)
NOW_ISO = NOW.isoformat()


# ============================================================================
# Shared fixtures
# ============================================================================


def make_sentiment(*, symbol="AAPL", promotable_score=0.6, source_count=3, as_of=NOW_ISO):
    return SENT.SentimentRegime(
        symbol=symbol, as_of=as_of, decay_window_hours=48.0, min_source_count=2,
        items_considered=5, input_event_ids=("e1", "e2", "e3"),
        distinct_origin_sources=("Benzinga", "Reuters", "Bloomberg")[:source_count],
        source_count=source_count, corroboration_status="SUFFICIENT",
        bullish_count=3, bearish_count=0, neutral_count=2, mixed_count=0,
        raw_score=promotable_score, promotable_score=promotable_score,
    )


DECIDE_KWARGS_BASE = dict(
    sentiment_weight=1.0, wave_weight=1.0, technical_weight=0.0, short_technical_weight=0.0,
    decision_threshold=0.1, ai_penalty_per_concern=0.2, critic_penalty_per_issue=0.15,
    proposal_module=PROPOSAL,
)


def decide_kwargs():
    kwargs = dict(DECIDE_KWARGS_BASE)
    kwargs["llm_client"] = STUB.NeutralDeterministicLLMClient()
    return kwargs


def make_request(symbol, *, promotable_score=0.6, quantity="10", asset_class="STOCK", alpaca_asset=None):
    return M.SymbolRequest(
        symbol=symbol, asset_class=asset_class, quantity=quantity,
        sentiment_regime=make_sentiment(symbol=symbol, promotable_score=promotable_score),
        alpaca_asset=alpaca_asset,
    )


def auth_config(**overrides):
    config = {"kill_switch": False, "execution_authorized": True, "paper_execution_authorized": True,
              "authorization_ttl_seconds": 60, "safety_state_ttl_seconds": 60}
    config.update(overrides)
    return config


def supervisor_config(**overrides):
    config = {"kill_switch": False}
    config.update(overrides)
    return config


def dirs_kwargs(tmp_path, tag):
    return dict(auth_claims_dir=tmp_path / f"{tag}-auth", supervisor_claims_dir=tmp_path / f"{tag}-sup")


def reference_price_fn(symbol):
    return 100.0


def equity_history_fixture():
    # A same-day-prior ALPACA equity observation -- without this, .44's
    # own fail-closed daily_loss check (INSUFFICIENT_HISTORY -> BLOCK on a
    # fresh state, per its own documented "not proven safe -> BLOCK"
    # discipline) blocks every hypothetical trade regardless of any other
    # dimension. Seeding this is a test-fixture concern, not something
    # `.55` fabricates on a caller's behalf.
    return [{"venue": "ALPACA", "equity": 100000.0, "as_of": "2026-09-23T08:00:00+00:00"}]


class FakeAsset:
    def __init__(self, *, symbol, shortable, easy_to_borrow, tradable=True, fractionable=True):
        self.symbol = symbol
        self.asset_class = "us_equity"
        self.exchange = "NASDAQ"
        self.status = "active"
        self.tradable = tradable
        self.shortable = shortable
        self.easy_to_borrow = easy_to_borrow
        self.fractionable = fractionable
        self.marginable = True
        self.min_order_size = None
        self.min_trade_increment = None
        self.price_increment = None


class FakeAccount:
    def __init__(self, equity=100000.0):
        self.equity = equity


class FakeOrder:
    def __init__(self, *, status="filled", id="broker-1", filled_qty="10", filled_avg_price="101.5"):
        self.status = status
        self.id = id
        self.filled_qty = filled_qty
        self.filled_avg_price = filled_avg_price
        self.submitted_at = NOW_ISO
        self.filled_at = NOW_ISO


class FakeAlpacaClient:
    def __init__(self, *, assets=None, positions=None, submit_exception=None, order_not_found=False,
                 filled_order_factory=None, equity=100000.0):
        self._assets = assets or {}
        self._positions = positions or []
        self._submit_exception = submit_exception
        self._order_not_found = order_not_found
        self._filled_order_factory = filled_order_factory or (lambda client_order_id: FakeOrder())
        self._equity = equity
        self.submit_calls: list = []
        self._submitted_ids: set[str] = set()

    def get_account(self):
        return FakeAccount(equity=self._equity)

    def get_all_positions(self):
        return self._positions

    def get_asset(self, symbol):
        if symbol not in self._assets:
            raise RuntimeError(f"unknown asset {symbol}")
        return self._assets[symbol]

    def get_order_by_client_id(self, client_order_id):
        if self._order_not_found or client_order_id not in self._submitted_ids:
            raise Exception("order not found")
        return self._filled_order_factory(client_order_id)

    def submit_order(self, order_data):
        self.submit_calls.append(order_data)
        if self._submit_exception:
            raise self._submit_exception
        client_order_id = order_data.client_order_id if hasattr(order_data, "client_order_id") else order_data.get("client_order_id")
        self._submitted_ids.add(client_order_id)

        class _FakeSubmitted:
            id = "broker-123"
            status = "new"

        return _FakeSubmitted()


def base_supervision_kwargs(tmp_path, tag, *, client=None):
    kwargs = dict(auth_config=auth_config(), supervisor_config=supervisor_config(), **dirs_kwargs(tmp_path, tag))
    if client is not None:
        kwargs["alpaca_client"] = client
    return kwargs


# ============================================================================
# build_shortability_audit
# ============================================================================


def test_shortability_audit_no_direction_returns_all_none():
    audit = M.build_shortability_audit(None, None, asset_class="STOCK", adapter_module=ADAPTER35, metadata_module=META)
    assert audit == {
        "raw_shortable": None, "raw_easy_to_borrow": None, "translated_shortability_status": None,
        "direction_capability": None, "short_positively_verified": None,
    }


def test_shortability_audit_short_verified():
    asset = {"shortable": True, "easy_to_borrow": True}
    audit = M.build_shortability_audit(asset, "OPEN_SHORT", asset_class="STOCK", adapter_module=ADAPTER35, metadata_module=META)
    assert audit["raw_shortable"] is True
    assert audit["raw_easy_to_borrow"] is True
    assert audit["translated_shortability_status"] == "SHORTABLE"
    assert audit["direction_capability"] == META.BORROW_CONFIRMED
    assert audit["short_positively_verified"] is True


def test_shortability_audit_short_not_verified_missing_data():
    asset = {"shortable": None, "easy_to_borrow": None}
    audit = M.build_shortability_audit(asset, "OPEN_SHORT", asset_class="STOCK", adapter_module=ADAPTER35, metadata_module=META)
    assert audit["translated_shortability_status"] == "UNKNOWN"
    assert audit["direction_capability"] == META.BORROW_UNKNOWN
    assert audit["short_positively_verified"] is False


def test_shortability_audit_long_never_checks_borrow():
    asset = {"shortable": False, "easy_to_borrow": False}
    audit = M.build_shortability_audit(asset, "OPEN_LONG", asset_class="STOCK", adapter_module=ADAPTER35, metadata_module=META)
    assert audit["short_positively_verified"] is None
    assert audit["direction_capability"] == META.NATIVE


# ============================================================================
# Stage 1A -- dry run, no broker call
# ============================================================================


def test_stage1a_buy_reaches_construction_preview(tmp_path):
    reqs = (make_request("AAPL", promotable_score=0.9, alpaca_asset={"symbol": "AAPL", "tradable": True, "fractionable": True, "shortable": True, "easy_to_borrow": True}),)
    report = M.run_stage1a_dry_run(
        reqs, decide_kwargs=decide_kwargs(), max_new_orders_per_cycle=5, now=NOW, reference_price_fn=reference_price_fn,
        supervision_kwargs=dirs_kwargs(tmp_path, "1a-buy"),
    )
    assert report.stage == "STAGE_1A_DRY_RUN"
    outcome = report.cycle_result.outcomes[0]
    assert outcome.stage == "SUBMITTED_FOR_EXECUTION"
    assert outcome.supervision_result["status"] == "READY_FOR_SUBMISSION"
    record = report.audit_records[0]
    assert record.signal_outcome == "DECIDE_LONG"
    assert record.submission_status == "READY_FOR_SUBMISSION"
    assert record.fill_status is None
    assert record.kill_switch_engaged is False
    assert record.intended_notional_usd == pytest.approx(1000.0)
    assert record.fees_cost_assumptions == M.FEES_COST_DISCLOSURE


def test_stage1a_short_verified_reaches_construction_preview(tmp_path):
    reqs = (make_request("TSLA", promotable_score=-0.9, alpaca_asset={"symbol": "TSLA", "tradable": True, "fractionable": True, "shortable": True, "easy_to_borrow": True}),)
    report = M.run_stage1a_dry_run(
        reqs, decide_kwargs=decide_kwargs(), max_new_orders_per_cycle=5, now=NOW,
        supervision_kwargs=dirs_kwargs(tmp_path, "1a-short-ok"),
    )
    record = report.audit_records[0]
    assert record.signal_outcome == "DECIDE_SHORT"
    assert record.submission_status == "READY_FOR_SUBMISSION"
    assert record.short_positively_verified is True
    assert record.direction_capability == META.BORROW_CONFIRMED


def test_stage1a_short_unverified_is_blocked_not_submitted(tmp_path):
    reqs = (make_request("TSLA", promotable_score=-0.9, alpaca_asset={"symbol": "TSLA", "tradable": True, "fractionable": True, "shortable": False, "easy_to_borrow": False}),)
    report = M.run_stage1a_dry_run(
        reqs, decide_kwargs=decide_kwargs(), max_new_orders_per_cycle=5, now=NOW,
        supervision_kwargs=dirs_kwargs(tmp_path, "1a-short-blocked"),
    )
    record = report.audit_records[0]
    assert record.signal_outcome == "DECIDE_SHORT"
    assert record.short_positively_verified is False
    assert record.submission_status == "BLOCKED"
    assert record.broker_order_id is None


def test_stage1a_kill_switch_rejection_when_explicitly_engaged(tmp_path):
    reqs = (make_request("AAPL", promotable_score=0.9, alpaca_asset={"symbol": "AAPL", "tradable": True, "fractionable": True, "shortable": True, "easy_to_borrow": True}),)
    report = M.run_stage1a_dry_run(
        reqs, decide_kwargs=decide_kwargs(), max_new_orders_per_cycle=5, now=NOW,
        supervision_kwargs={"supervisor_config": {"kill_switch": True}, **dirs_kwargs(tmp_path, "1a-killswitch")},
    )
    record = report.audit_records[0]
    assert record.submission_status == "BLOCKED"
    assert record.kill_switch_engaged is True


def test_stage1a_low_conviction_is_no_trade_decided():
    reqs = (make_request("AAPL", promotable_score=0.02),)
    report = M.run_stage1a_dry_run(reqs, decide_kwargs=decide_kwargs(), max_new_orders_per_cycle=5, now=NOW)
    record = report.audit_records[0]
    assert record.cycle_stage == "NO_TRADE_DECIDED"
    assert record.submission_status is None
    assert record.risk_decision_status == "NOT_EVALUATED"


def test_stage1a_real_enforcement_allows_with_no_limits_configured(tmp_path):
    # A real total-equity figure is required for .44 to even COMPUTE
    # portfolio_heat/directional_exposure at all (see run_stage1a_dry_run's
    # docstring) -- synthetic_account_equity_usd supplies one, disclosed as
    # synthetic, with zero broker contact (no client object is ever built).
    # Making ALPACA a real, synthetic-equity CONFIGURED venue (SUCCESS, not
    # NOT_CONFIGURED) also brings its per-venue daily_loss dimension into
    # play, which needs a same-day-prior equity observation the same way
    # Stage 1B's tests do (see equity_history_fixture's own docstring) --
    # otherwise it fails closed with INSUFFICIENT_HISTORY regardless of
    # portfolio_heat/directional_exposure.
    reqs = (make_request("AAPL", promotable_score=0.9, alpaca_asset={"symbol": "AAPL", "tradable": True, "fractionable": True, "shortable": True, "easy_to_borrow": True}),)
    report = M.run_stage1a_dry_run(
        reqs, decide_kwargs=decide_kwargs(), max_new_orders_per_cycle=5,
        reference_price_fn=reference_price_fn, limits=ENFORCE.PortfolioLimits(),
        equity_history=equity_history_fixture(),
        max_snapshot_age_seconds=10**9, supervision_kwargs=dirs_kwargs(tmp_path, "1a-enforce-allow"),
        synthetic_account_equity_usd=100_000.0,
    )
    record = report.audit_records[0]
    assert record.cycle_stage == "SUBMITTED_FOR_EXECUTION"
    assert record.submission_status == "READY_FOR_SUBMISSION"
    assert record.risk_decision_status == "ALLOW"
    assert record.risk_decision_hash is not None


def test_stage1a_real_enforcement_blocks_on_configured_limit_breach(tmp_path):
    # quantity=1000 @ reference_price_fn's fixed $100 = $100,000 notional
    # against a $100,000 synthetic equity -> heat_ratio == 1.0, genuinely
    # breaching a 0.0001 cap -- a real LIMIT_BREACHED, not merely
    # EXPOSURE_NOT_COMPUTABLE (which a no-equity snapshot would also
    # report as "portfolio_heat" in its reasons, without ever having
    # evaluated the configured limit at all).
    reqs = (make_request("AAPL", promotable_score=0.9, quantity="1000", alpaca_asset={"symbol": "AAPL", "tradable": True, "fractionable": True, "shortable": True, "easy_to_borrow": True}),)
    tiny_limits = ENFORCE.PortfolioLimits(max_portfolio_heat_ratio=0.0001)
    report = M.run_stage1a_dry_run(
        reqs, decide_kwargs=decide_kwargs(), max_new_orders_per_cycle=5,
        reference_price_fn=reference_price_fn, limits=tiny_limits,
        equity_history=equity_history_fixture(),
        max_snapshot_age_seconds=10**9, supervision_kwargs=dirs_kwargs(tmp_path, "1a-enforce-block"),
        synthetic_account_equity_usd=100_000.0,
    )
    record = report.audit_records[0]
    # Blocked by .44 before ever reaching .38 -- never even a construction preview.
    assert record.cycle_stage == "NO_TRADE_DECIDED"
    assert record.submission_status is None
    assert record.risk_decision_status == "BLOCK"
    assert any("portfolio_heat" in r and "LIMIT_BREACHED" in r for r in record.risk_decision_blocked_reasons)


def test_stage1a_real_enforcement_fails_closed_with_no_equity_source(tmp_path):
    # Without synthetic_account_equity_usd, the snapshot has NO configured
    # venue anywhere (zero broker contact, but also zero equity data) --
    # .44 cannot compute portfolio_heat/directional_exposure at all and
    # BLOCKs on EXPOSURE_NOT_COMPUTABLE, regardless of whether a numeric
    # limit was ever configured. This is correct fail-closed behavior,
    # not a defect -- kept as its own explicit scenario.
    reqs = (make_request("AAPL", promotable_score=0.9, alpaca_asset={"symbol": "AAPL", "tradable": True, "fractionable": True, "shortable": True, "easy_to_borrow": True}),)
    report = M.run_stage1a_dry_run(
        reqs, decide_kwargs=decide_kwargs(), max_new_orders_per_cycle=5,
        reference_price_fn=reference_price_fn, limits=ENFORCE.PortfolioLimits(),
        max_snapshot_age_seconds=10**9, supervision_kwargs=dirs_kwargs(tmp_path, "1a-enforce-noequity"),
    )
    record = report.audit_records[0]
    assert record.cycle_stage == "NO_TRADE_DECIDED"
    assert record.submission_status is None
    assert record.risk_decision_status == "BLOCK"
    assert any("EXPOSURE_NOT_COMPUTABLE" in r for r in record.risk_decision_blocked_reasons)


def test_stage1a_enforcement_requires_reference_price_fn_and_max_age():
    reqs = (make_request("AAPL", promotable_score=0.9),)
    with pytest.raises(M.Stage1RunnerError):
        M.run_stage1a_dry_run(
            reqs, decide_kwargs=decide_kwargs(), max_new_orders_per_cycle=5, now=NOW,
            limits=ENFORCE.PortfolioLimits(),  # reference_price_fn/max_snapshot_age_seconds omitted
        )


# ============================================================================
# Stage 1B -- real (fake) Alpaca client, real .44 enforcement, real .54 fills
# ============================================================================


def test_stage1b_buy_submits_and_reconciles_fill(tmp_path):
    client = FakeAlpacaClient(assets={"AAPL": FakeAsset(symbol="AAPL", shortable=True, easy_to_borrow=True)})
    reqs = (make_request("AAPL", promotable_score=0.9),)
    report = M.run_stage1b_paper_cycle(
        reqs, alpaca_client=client, decide_kwargs=decide_kwargs(), max_new_orders_per_cycle=5,
        equity_history=equity_history_fixture(), max_snapshot_age_seconds=10**9, reference_price_fn=reference_price_fn,
        supervision_kwargs=base_supervision_kwargs(tmp_path, "buy"),
        fill_poll_timeout_seconds=1, fill_poll_interval_seconds=1, now=NOW,
    )
    assert report.stage == "STAGE_1B_PAPER_CYCLE"
    record = report.audit_records[0]
    assert record.signal_outcome == "DECIDE_LONG"
    assert record.submission_status == "SUBMITTED"
    assert record.broker_order_id == "broker-123"
    assert record.fill_status == "FILLED"
    assert record.fill_price == "101.5"
    assert record.risk_decision_status == "ALLOW"
    assert record.risk_decision_hash is not None
    assert len(client.submit_calls) == 1


def test_stage1b_short_verified_submits(tmp_path):
    client = FakeAlpacaClient(assets={"TSLA": FakeAsset(symbol="TSLA", shortable=True, easy_to_borrow=True)})
    reqs = (make_request("TSLA", promotable_score=-0.9),)
    report = M.run_stage1b_paper_cycle(
        reqs, alpaca_client=client, decide_kwargs=decide_kwargs(), max_new_orders_per_cycle=5,
        equity_history=equity_history_fixture(), max_snapshot_age_seconds=10**9, reference_price_fn=reference_price_fn,
        supervision_kwargs=base_supervision_kwargs(tmp_path, "short-ok"),
        fill_poll_timeout_seconds=1, fill_poll_interval_seconds=1, now=NOW,
    )
    record = report.audit_records[0]
    assert record.signal_outcome == "DECIDE_SHORT"
    assert record.short_positively_verified is True
    assert record.submission_status == "SUBMITTED"
    assert len(client.submit_calls) == 1


def test_stage1b_short_unverified_never_submits(tmp_path):
    client = FakeAlpacaClient(assets={"TSLA": FakeAsset(symbol="TSLA", shortable=False, easy_to_borrow=False)})
    reqs = (make_request("TSLA", promotable_score=-0.9),)
    report = M.run_stage1b_paper_cycle(
        reqs, alpaca_client=client, decide_kwargs=decide_kwargs(), max_new_orders_per_cycle=5,
        equity_history=equity_history_fixture(), max_snapshot_age_seconds=10**9, reference_price_fn=reference_price_fn,
        supervision_kwargs=base_supervision_kwargs(tmp_path, "short-blocked"),
        fill_poll_timeout_seconds=1, fill_poll_interval_seconds=1, now=NOW,
    )
    record = report.audit_records[0]
    assert record.short_positively_verified is False
    assert record.submission_status != "SUBMITTED"
    assert record.broker_order_id is None
    assert client.submit_calls == []


def test_stage1b_asset_metadata_fetch_failure_excludes_symbol(tmp_path):
    client = FakeAlpacaClient(assets={})  # get_asset("AAPL") raises
    reqs = (make_request("AAPL", promotable_score=0.9),)
    report = M.run_stage1b_paper_cycle(
        reqs, alpaca_client=client, decide_kwargs=decide_kwargs(), max_new_orders_per_cycle=5,
        equity_history=equity_history_fixture(), max_snapshot_age_seconds=10**9, reference_price_fn=reference_price_fn,
        supervision_kwargs=base_supervision_kwargs(tmp_path, "fetchfail"),
        fill_poll_timeout_seconds=1, fill_poll_interval_seconds=1, now=NOW,
    )
    assert report.cycle_result is None
    assert len(report.metadata_fetch_failures) == 1
    assert report.metadata_fetch_failures[0]["symbol"] == "AAPL"
    assert report.audit_records[0].cycle_stage == "ASSET_METADATA_FETCH_FAILED"
    assert client.submit_calls == []


def test_stage1b_kill_switch_rejection(tmp_path):
    client = FakeAlpacaClient(assets={"AAPL": FakeAsset(symbol="AAPL", shortable=True, easy_to_borrow=True)})
    reqs = (make_request("AAPL", promotable_score=0.9),)
    report = M.run_stage1b_paper_cycle(
        reqs, alpaca_client=client, decide_kwargs=decide_kwargs(), max_new_orders_per_cycle=5,
        equity_history=equity_history_fixture(), max_snapshot_age_seconds=10**9, reference_price_fn=reference_price_fn,
        supervision_kwargs=base_supervision_kwargs(tmp_path, "killswitch", client=None) | {"supervisor_config": supervisor_config(kill_switch=True)},
        fill_poll_timeout_seconds=1, fill_poll_interval_seconds=1, now=NOW,
    )
    record = report.audit_records[0]
    assert record.submission_status == "BLOCKED"
    assert record.kill_switch_engaged is True
    assert client.submit_calls == []


def test_stage1b_enforcement_blocks_on_configured_limit(tmp_path):
    client = FakeAlpacaClient(assets={"AAPL": FakeAsset(symbol="AAPL", shortable=True, easy_to_borrow=True)})
    reqs = (make_request("AAPL", promotable_score=0.9, quantity="1000"),)
    tiny_limits = ENFORCE.PortfolioLimits(max_portfolio_heat_ratio=0.0001)
    report = M.run_stage1b_paper_cycle(
        reqs, alpaca_client=client, decide_kwargs=decide_kwargs(), max_new_orders_per_cycle=5,
        equity_history=equity_history_fixture(), max_snapshot_age_seconds=10**9, reference_price_fn=reference_price_fn,
        supervision_kwargs=base_supervision_kwargs(tmp_path, "enforce"),
        fill_poll_timeout_seconds=1, fill_poll_interval_seconds=1, now=NOW, limits=tiny_limits,
    )
    record = report.audit_records[0]
    assert record.cycle_stage == "NO_TRADE_DECIDED"
    assert record.risk_decision_status == "BLOCK"
    assert any("portfolio_heat" in r for r in record.risk_decision_blocked_reasons)
    assert client.submit_calls == []


def test_stage1b_broker_submit_failure_is_execution_uncertain_and_poll_error(tmp_path):
    client = FakeAlpacaClient(
        assets={"AAPL": FakeAsset(symbol="AAPL", shortable=True, easy_to_borrow=True)},
        submit_exception=RuntimeError("network down"), order_not_found=True,
    )
    reqs = (make_request("AAPL", promotable_score=0.9),)
    report = M.run_stage1b_paper_cycle(
        reqs, alpaca_client=client, decide_kwargs=decide_kwargs(), max_new_orders_per_cycle=5,
        equity_history=equity_history_fixture(), max_snapshot_age_seconds=10**9, reference_price_fn=reference_price_fn,
        supervision_kwargs=base_supervision_kwargs(tmp_path, "submitfail"),
        fill_poll_timeout_seconds=1, fill_poll_interval_seconds=1, now=NOW,
    )
    record = report.audit_records[0]
    assert record.submission_status == "EXECUTION_UNCERTAIN"
    assert record.fill_status == "POLL_ERROR"


def test_stage1b_duplicate_order_protection_second_attempt_blocked(tmp_path):
    client = FakeAlpacaClient(assets={"AAPL": FakeAsset(symbol="AAPL", shortable=True, easy_to_borrow=True)})
    reqs = (make_request("AAPL", promotable_score=0.9),)
    kwargs = dict(
        decide_kwargs=decide_kwargs(), max_new_orders_per_cycle=5, equity_history=equity_history_fixture(),
        max_snapshot_age_seconds=10**9, reference_price_fn=reference_price_fn,
        supervision_kwargs=base_supervision_kwargs(tmp_path, "dup"),
        fill_poll_timeout_seconds=1, fill_poll_interval_seconds=1, now=NOW,
    )
    first = M.run_stage1b_paper_cycle(reqs, alpaca_client=client, **kwargs)
    second = M.run_stage1b_paper_cycle(reqs, alpaca_client=client, **kwargs)
    assert first.audit_records[0].submission_status == "SUBMITTED"
    assert second.audit_records[0].submission_status != "SUBMITTED"
    assert len(client.submit_calls) == 1  # second attempt never reached .35.submit()


def test_stage1b_resulting_position_logged_after_submission(tmp_path):
    position = OBS.PositionRecord(
        venue="ALPACA", symbol="AAPL", direction="LONG", quantity=10.0, entry_price=100.0,
        leverage=None, mark_price=101.0, notional_usd=1010.0, notional_basis="MARK_TO_MARKET",
        unrealized_pnl_usd=10.0, liquidation_price=None, raw_source_id="asset-1", as_of=NOW_ISO,
    )

    class _AttrPos:
        symbol = "AAPL"
        side = "long"
        qty = "10"
        avg_entry_price = "100.0"
        current_price = "101.0"
        market_value = "1010.0"
        unrealized_pl = "10.0"
        asset_id = "asset-1"

    client = FakeAlpacaClient(
        assets={"MSFT": FakeAsset(symbol="MSFT", shortable=True, easy_to_borrow=True)},
        positions=[_AttrPos()],
    )
    reqs = (make_request("MSFT", promotable_score=0.9),)
    report = M.run_stage1b_paper_cycle(
        reqs, alpaca_client=client, decide_kwargs=decide_kwargs(), max_new_orders_per_cycle=5,
        equity_history=equity_history_fixture(), max_snapshot_age_seconds=10**9, reference_price_fn=reference_price_fn,
        supervision_kwargs=base_supervision_kwargs(tmp_path, "position"),
        fill_poll_timeout_seconds=1, fill_poll_interval_seconds=1, now=NOW,
    )
    # MSFT itself never held before/after (position fixture is for AAPL) -- resulting_position is None,
    # honestly reflecting that this fake client's post-submission state wasn't updated by the fake submit.
    record = [r for r in report.audit_records if r.symbol == "MSFT"][0]
    assert record.resulting_position is None


# ============================================================================
# build_enforcement_check_fn -- direct unit coverage (flat book, no limits)
# ============================================================================


def test_enforcement_check_fn_allows_when_no_limits_configured():
    client = FakeAlpacaClient()
    snapshot = OBS.build_portfolio_snapshot(alpaca_client=client)
    decisions: dict = {}
    check = M.build_enforcement_check_fn(
        snapshot=snapshot, equity_history=equity_history_fixture(), limits=ENFORCE.PortfolioLimits(),
        max_snapshot_age_seconds=10**9, reference_price_fn=reference_price_fn, now=NOW,
        decisions_by_symbol=decisions, enforcement_module=ENFORCE, observability_module=OBS,
    )
    result = check("AAPL", "OPEN_LONG", "10", None)
    assert result.allowed is True
    assert decisions["AAPL"].overall_verdict == "ALLOW"


def test_enforcement_check_fn_rejects_bad_quantity():
    client = FakeAlpacaClient()
    snapshot = OBS.build_portfolio_snapshot(alpaca_client=client)
    check = M.build_enforcement_check_fn(
        snapshot=snapshot, equity_history=equity_history_fixture(), limits=ENFORCE.PortfolioLimits(),
        max_snapshot_age_seconds=10**9, reference_price_fn=reference_price_fn, now=NOW,
        decisions_by_symbol={}, enforcement_module=ENFORCE, observability_module=OBS,
    )
    with pytest.raises(M.Stage1RunnerError):
        check("AAPL", "OPEN_LONG", "not-a-number", None)
