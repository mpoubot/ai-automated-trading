#!/usr/bin/env python3
"""Contract + wiring tests for AURA v0.5.3.63 Stage 1B manual-trigger CLI.

Mirrors `.356`'s own test convention: every real dependency module is
loaded into `sys.modules` under its canonical name BEFORE `.363` (and the
`.356` module it reuses) are loaded, so their internal dynamic imports
resolve to the SAME module objects these tests build fixtures against.

NO LIVE ALPACA ACCESS IS REQUIRED OR ATTEMPTED BY ANY TEST IN THIS FILE.
NO REAL CREDENTIAL VALUE IS USED ANYWHERE IN THIS FILE.

`FakeAlpacaClient`/`FakeAsset`/`FakeAccount`/`FakeOrder` are copied from
`.355`'s own test fixture (`tests/test_aura_v05355_stage1_paper_trading_
runner.py`) -- unlike `.356`'s own dry-run-only fixture, THIS file's fake
client must support `get_asset`/`submit_order`/`get_order_by_client_id`,
since `.363` can genuinely reach `run_stage1b_paper_cycle`.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name, path):
    """Idempotent: reuses an already-registered `sys.modules[name]` when
    present instead of unconditionally overwriting it. Several sibling
    test files in this suite (e.g. `.356`'s) load these SAME canonical
    names and then `monkeypatch.setattr`/spy on THEIR OWN local module
    object; production code under test reaches that module via `__import__`
    (which reads `sys.modules` at call time, not at whichever file happened
    to load it). When multiple test files unconditionally overwrite the
    same canonical name, a spy set up in one file can silently stop being
    the object production code actually calls when run together in one
    pytest session. Reusing whatever is already resident keeps identity
    consistent across every test file collected in the same run -- this
    mirrors production `_load_module()`'s own "prefer an already-imported
    module" precedent, just applied to the test loader too."""
    if name in sys.modules:
        return sys.modules[name]
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
M51 = _load("aura_v05351_live_alpaca_equity_signal_source", ROOT / "aura_v05351_live_alpaca_equity_signal_source.py")
M52 = _load("aura_v05352_stock_etf_short_side_signal", ROOT / "aura_v05352_stock_etf_short_side_signal.py")
CYCLE = _load("aura_v05353_full_paper_orchestration", ROOT / "aura_v05353_full_paper_orchestration.py")
FILL = _load("aura_v05354_alpaca_equity_fill_reconciliation", ROOT / "aura_v05354_alpaca_equity_fill_reconciliation.py")
STUB = _load("aura_v054_llm_stub", ROOT / "aura_v054_llm_stub.py")
SIGSRC = _load("aura_v054_signal_source", ROOT / "aura_v054_signal_source.py")
STAGE1 = _load("aura_v05355_stage1_paper_trading_runner", ROOT / "aura_v05355_stage1_paper_trading_runner.py")
NEWS46 = _load("aura_v05346_news_ingestion_classification", ROOT / "aura_v05346_news_ingestion_classification.py")
WAVE48 = _load("aura_v05348_elliott_wave_research", ROOT / "aura_v05348_elliott_wave_research.py")
ROTATION59 = _load("aura_v05359_sector_rotation", ROOT / "aura_v05359_sector_rotation.py")
BUILDER60 = _load("aura_v05360_research_full_evidence_builder", ROOT / "aura_v05360_research_full_evidence_builder.py")
ORCH362 = _load("aura_v05362_live_evidence_orchestrator", ROOT / "aura_v05362_live_evidence_orchestrator.py")
ATR54 = _load("aura_v054_atr", ROOT / "aura_v054_atr.py")
EXIT54 = _load("aura_v054_exit_engine", ROOT / "aura_v054_exit_engine.py")
SIZING54 = _load("aura_v054_position_sizing", ROOT / "aura_v054_position_sizing.py")
EQUITY_CLI = _load("aura_v05356_stage3_live_equity_cli", ROOT / "aura_v05356_stage3_live_equity_cli.py")
HISTORY64 = _load("aura_v05364_equity_history_log", ROOT / "aura_v05364_equity_history_log.py")
M = _load("aura_v05363_stage1b_manual_trigger_cli", ROOT / "aura_v05363_stage1b_manual_trigger_cli.py")

NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)


# ============================================================================
# Fakes -- outermost edges only. FakeAlpacaClient/FakeAsset/FakeAccount/
# FakeOrder copied from `.355`'s own test fixture (same shape/behavior) --
# unlike `.356`'s dry-run-only fixture, this one supports real submission.
# ============================================================================


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
        self.submitted_at = NOW.isoformat()
        self.filled_at = NOW.isoformat()


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
        self._submitted_ids: set = set()

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


class _FakeBarsResponse:
    def __init__(self, df):
        self.df = df


class FakeBarsClient:
    def __init__(self, bars_by_symbol=None, exception=None):
        self._bars_by_symbol = bars_by_symbol or {}
        self._exception = exception
        self.requests: list = []

    def get_stock_bars(self, request):
        self.requests.append(request)
        if self._exception is not None:
            raise self._exception
        symbol = request.symbol_or_symbols[0]
        if symbol not in self._bars_by_symbol:
            return _FakeBarsResponse(pd.DataFrame())
        return _FakeBarsResponse(self._bars_by_symbol[symbol])


def make_bars(n=60, start_price=100.0) -> pd.DataFrame:
    idx = pd.date_range(end=NOW, periods=n, freq="D", tz="UTC")
    closes = [start_price + i * 0.5 for i in range(n)]
    return pd.DataFrame(
        {
            "open": [c - 0.2 for c in closes], "high": [c + 0.3 for c in closes],
            "low": [c - 0.3 for c in closes], "close": closes,
            "volume": [1_000_000 + i * 100 for i in range(n)],
        },
        index=idx,
    )


def make_symbol_requests_file(tmp_path, entries) -> Path:
    p = tmp_path / "requests.json"
    p.write_text(json.dumps(entries), encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def isolate_claims_and_history_dirs(monkeypatch, tmp_path):
    """`.363` deliberately omits auth_claims_dir/supervisor_claims_dir so
    production runs use `.36`/`.38`'s own real default directories (see
    `.363`'s module docstring) -- for TESTS, redirect those real defaults
    into tmp_path so no test ever writes into the actual repo checkout's
    `regime_output/` directory."""
    monkeypatch.setattr(SUP, "DEFAULT_ALPACA_AUTH_CLAIMS_DIR", tmp_path / "auth_claims")
    monkeypatch.setattr(SUP, "DEFAULT_ALPACA_CLIENT_ORDER_ID_CLAIMS_DIR", tmp_path / "order_claims")


@pytest.fixture(autouse=True)
def clean_credential_env(monkeypatch):
    monkeypatch.delenv(EQUITY_CLI.EQUITY_API_KEY_ENV, raising=False)
    monkeypatch.delenv(EQUITY_CLI.EQUITY_SECRET_KEY_ENV, raising=False)


def make_synthetic_confirmed_technical_regime(signal_score=80.0, status="CONFIRMED"):
    """A hand-built, strongly-bullish `.51` TechnicalRegime -- used to
    prove the wiring reaches a real DECIDE_LONG/submission without
    depending on `.51`'s own indicator math (that's `.51`'s own test
    suite's job, not this file's)."""
    return M51.TechnicalRegime(
        symbol="AAPL", as_of=NOW.isoformat(), last_bar_timestamp=NOW.isoformat(), status=status,
        signal_score=signal_score, persistence_bars=3, rsi_14=60.0, rel_volume=1.5, atr_pct=0.02,
        price_acceleration=0.01, reasons=(), bars_used=60, universe_version=SIGSRC.UNIVERSE_VERSION,
        scoring_params_hash="test-hash",
    )


def make_synthetic_not_usable_short_regime():
    """A short regime whose status is NOT in `.52`'s USABLE set, so the
    short term never fights the forced-bullish long term above."""
    return M52.ShortTechnicalRegime(
        symbol="AAPL", as_of=NOW.isoformat(), last_bar_timestamp=NOW.isoformat(), status="INSUFFICIENT_DATA",
        signal_score=0.0, persistence_bars=0, rsi_14=None, rel_volume=None, atr_pct=None,
        price_acceleration=None, reasons=(), bars_used=0, universe_version=SIGSRC.UNIVERSE_VERSION,
        scoring_params_hash="test-hash",
    )


# ============================================================================
# 1. Confirmation gate -- the primary structural safety guarantee.
# ============================================================================


def test_run_manual_trigger_requires_confirmed_true_raises_before_touching_anything():
    """confirmed=False must raise before any module is loaded or client
    touched -- pass deliberately-poisoned sentinels that would raise
    AttributeError/TypeError if ever actually used, to prove the early
    return."""
    with pytest.raises(M.Stage1BManualTriggerCliError, match="SUBMISSION_NOT_CONFIRMED"):
        M.run_manual_trigger_stage1b_cycle(
            (), confirmed=False, bars_client=object(), alpaca_client=object(),
            max_new_orders_per_cycle=1, lookback_bars=60, universe_version="x",
            max_snapshot_age_seconds=300.0, fill_poll_timeout_seconds=1.0, fill_poll_interval_seconds=1.0,
        )


def test_main_without_confirm_flag_exits_before_running(tmp_path, monkeypatch):
    monkeypatch.setenv(EQUITY_CLI.EQUITY_API_KEY_ENV, "FAKE_KEY")
    monkeypatch.setenv(EQUITY_CLI.EQUITY_SECRET_KEY_ENV, "FAKE_SECRET")
    reqs_path = make_symbol_requests_file(tmp_path, [{"symbol": "AAPL", "asset_class": "STOCK", "quantity": 1}])
    output_path = tmp_path / "out.json"
    argv = [
        "--requests-config", str(reqs_path), "--max-new-orders-per-cycle", "1",
        "--max-snapshot-age-seconds", "300", "--fill-poll-timeout-seconds", "1",
        "--fill-poll-interval-seconds", "1", "--output", str(output_path),
        # deliberately NOT passing --i-confirm-this-submits-real-paper-orders
    ]
    with pytest.raises(SystemExit):
        M.main(argv)
    assert not output_path.exists()


# ============================================================================
# 2. build_live_paper_submission_supervision
# ============================================================================


def test_build_live_paper_submission_supervision_shape():
    sup = M.build_live_paper_submission_supervision()
    assert sup["supervisor_config"] == {"kill_switch": False}
    assert sup["auth_config"]["kill_switch"] is False
    assert sup["auth_config"]["execution_authorized"] is True
    assert sup["auth_config"]["paper_execution_authorized"] is True
    assert sup["auth_config"]["authorization_ttl_seconds"] == 60
    assert sup["auth_config"]["safety_state_ttl_seconds"] == 60
    # deliberately omits claims dirs -- see module docstring
    assert "auth_claims_dir" not in sup
    assert "supervisor_claims_dir" not in sup


# ============================================================================
# 3. Wiring -- confirmed=True actually calls run_stage1b_paper_cycle
#    (never run_stage1a_dry_run), with the right supervision/decide_kwargs.
# ============================================================================


def test_confirmed_true_calls_stage1b_never_stage1a(monkeypatch):
    captured = {}
    real_stage1b = STAGE1.run_stage1b_paper_cycle

    def _spy(symbol_requests, **kwargs):
        captured["symbol_requests"] = symbol_requests
        captured["kwargs"] = kwargs
        return real_stage1b(symbol_requests, **kwargs)

    monkeypatch.setattr(STAGE1, "run_stage1b_paper_cycle", _spy)

    def _fail_if_called(*a, **kw):
        raise AssertionError("run_stage1a_dry_run must never be called by .363")

    monkeypatch.setattr(STAGE1, "run_stage1a_dry_run", _fail_if_called)

    bars = make_bars(n=60)
    bars_client = FakeBarsClient(bars_by_symbol={"AAPL": bars})
    alpaca_client = FakeAlpacaClient(equity=100_000.0, assets={"AAPL": FakeAsset(symbol="AAPL", shortable=True, easy_to_borrow=True)})

    M.run_manual_trigger_stage1b_cycle(
        (EQUITY_CLI.LiveSymbolRequest(symbol="AAPL", asset_class="STOCK", quantity=5),),
        confirmed=True, bars_client=bars_client, alpaca_client=alpaca_client,
        max_new_orders_per_cycle=1, lookback_bars=60, universe_version=SIGSRC.UNIVERSE_VERSION,
        max_snapshot_age_seconds=10**9, fill_poll_timeout_seconds=1.0, fill_poll_interval_seconds=1.0,
        now=NOW,
    )
    assert "symbol_requests" in captured
    assert captured["kwargs"]["decide_kwargs"]["technical_weight"] == 1.0
    assert captured["kwargs"]["supervision_kwargs"]["auth_config"]["execution_authorized"] is True
    assert captured["kwargs"]["supervision_kwargs"]["supervisor_config"]["kill_switch"] is False
    assert captured["kwargs"]["limits"] is None


# ============================================================================
# 4. Full end-to-end: a real submission actually happens.
# ============================================================================


def test_end_to_end_confirmed_run_submits_a_real_order(monkeypatch, tmp_path):
    """Forces a strongly-bullish, USABLE technical regime (see
    `make_synthetic_confirmed_technical_regime`) by monkeypatching `.51`'s
    own `build_technical_regime_from_bars` -- proving `.363`'s WIRING
    (evidence -> sizing -> real submission), not `.51`'s indicator math
    (already covered by `.51`'s own test suite).

    Deliberately does NOT pass a fixed historical `now` here: `.55`'s
    `run_stage1b_paper_cycle` builds its own portfolio snapshot with a
    REAL wall-clock timestamp regardless of any caller-supplied `now`
    (see `.355`'s own module docstring), so this equity-history entry's
    `as_of` must be real-clock-ordered relative to that snapshot too --
    using the fixed `NOW` fixture here would make the logged entry look
    like it's from the future relative to that real snapshot, and .44's
    daily_loss check would then (correctly) treat it as not-yet-prior and
    BLOCK on INSUFFICIENT_HISTORY."""
    monkeypatch.setattr(M51, "build_technical_regime_from_bars", lambda *a, **kw: make_synthetic_confirmed_technical_regime())
    monkeypatch.setattr(M52, "build_short_technical_regime_from_bars", lambda *a, **kw: make_synthetic_not_usable_short_regime())

    bars = make_bars(n=60)
    bars_client = FakeBarsClient(bars_by_symbol={"AAPL": bars})
    alpaca_client = FakeAlpacaClient(equity=100_000.0, assets={"AAPL": FakeAsset(symbol="AAPL", shortable=True, easy_to_borrow=True)})
    history_log = tmp_path / "hist.jsonl"

    result = M.run_manual_trigger_stage1b_cycle(
        (EQUITY_CLI.LiveSymbolRequest(symbol="AAPL", asset_class="STOCK", quantity=None),),
        confirmed=True, bars_client=bars_client, alpaca_client=alpaca_client,
        max_new_orders_per_cycle=1, lookback_bars=60, universe_version=SIGSRC.UNIVERSE_VERSION,
        max_snapshot_age_seconds=10**9, fill_poll_timeout_seconds=1.0, fill_poll_interval_seconds=1.0,
        equity_history_log_path=history_log,
        # now intentionally omitted -> real datetime.now(timezone.utc), consistent with .55's own real snapshot clock
    )

    assert result["stage1_report"] is not None
    assert result["stage1_report"]["submitted_count"] == 1
    assert len(alpaca_client.submit_calls) == 1
    record = result["stage1_report"]["audit_records"][0]
    assert record["signal_outcome"] == "DECIDE_LONG"
    assert record["risk_decision_status"] == "ALLOW"
    # the real equity fetched this run was logged
    assert result["equity_history_observation_count"] == 1
    logged = HISTORY64.read_equity_history(history_log)
    assert len(logged) == 1
    assert logged[0]["venue"] == "ALPACA"
    assert logged[0]["equity"] == 100_000.0


def test_second_same_day_run_sees_prior_run_as_history(monkeypatch, tmp_path):
    """See the note on `test_end_to_end_confirmed_run_submits_a_real_
    order` above for why `now` is left to default to the real clock in
    both calls here."""
    monkeypatch.setattr(M51, "build_technical_regime_from_bars", lambda *a, **kw: make_synthetic_confirmed_technical_regime())
    monkeypatch.setattr(M52, "build_short_technical_regime_from_bars", lambda *a, **kw: make_synthetic_not_usable_short_regime())

    bars = make_bars(n=60)
    bars_client = FakeBarsClient(bars_by_symbol={"AAPL": bars})
    history_log = tmp_path / "hist.jsonl"

    alpaca_client_1 = FakeAlpacaClient(equity=100_000.0, assets={"AAPL": FakeAsset(symbol="AAPL", shortable=True, easy_to_borrow=True)})
    M.run_manual_trigger_stage1b_cycle(
        (EQUITY_CLI.LiveSymbolRequest(symbol="AAPL", asset_class="STOCK", quantity=1),),
        confirmed=True, bars_client=bars_client, alpaca_client=alpaca_client_1,
        max_new_orders_per_cycle=1, lookback_bars=60, universe_version=SIGSRC.UNIVERSE_VERSION,
        max_snapshot_age_seconds=10**9, fill_poll_timeout_seconds=1.0, fill_poll_interval_seconds=1.0,
        equity_history_log_path=history_log,
    )

    alpaca_client_2 = FakeAlpacaClient(equity=99_000.0, assets={"AAPL": FakeAsset(symbol="AAPL", shortable=True, easy_to_borrow=True)})
    result2 = M.run_manual_trigger_stage1b_cycle(
        (EQUITY_CLI.LiveSymbolRequest(symbol="AAPL", asset_class="STOCK", quantity=1),),
        confirmed=True, bars_client=bars_client, alpaca_client=alpaca_client_2,
        max_new_orders_per_cycle=1, lookback_bars=60, universe_version=SIGSRC.UNIVERSE_VERSION,
        max_snapshot_age_seconds=10**9, fill_poll_timeout_seconds=1.0, fill_poll_interval_seconds=1.0,
        equity_history_log_path=history_log,
    )
    assert result2["equity_history_observation_count"] == 2
    history = HISTORY64.read_equity_history(history_log)
    assert [h["equity"] for h in history] == [100_000.0, 99_000.0]


def test_unconfirmed_symbol_still_skipped_not_whole_cycle_blocked():
    """A symbol whose bars fetch fails (`.51.fetch_recent_bars` raises
    `NO_BARS_RETURNED` for an empty response) is skipped, recorded in
    `symbol_fetch_failures`, never blocks a cycle with other symbols --
    mirrors `.356`'s own fail-open-per-symbol discipline, reused here via
    the same `fetch_symbol_evidence`."""
    bars_client = FakeBarsClient(bars_by_symbol={})  # AAPL never has bars -> fetch_recent_bars raises NO_BARS_RETURNED
    alpaca_client = FakeAlpacaClient(equity=100_000.0)
    result = M.run_manual_trigger_stage1b_cycle(
        (EQUITY_CLI.LiveSymbolRequest(symbol="AAPL", asset_class="STOCK", quantity=None),),
        confirmed=True, bars_client=bars_client, alpaca_client=alpaca_client,
        max_new_orders_per_cycle=1, lookback_bars=60, universe_version=SIGSRC.UNIVERSE_VERSION,
        max_snapshot_age_seconds=10**9, fill_poll_timeout_seconds=1.0, fill_poll_interval_seconds=1.0,
        now=NOW,
    )
    assert len(result["symbol_fetch_failures"]) == 1
    assert result["symbol_fetch_failures"][0]["symbol"] == "AAPL"
    assert result["sizing_failures"] == []
    assert result["stage1_report"] is None


def test_unsizeable_symbol_skipped_not_whole_cycle_blocked():
    """Bars fetch succeeds but no account equity is available (get_account
    raises) -> quantity cannot be auto-sized -> the symbol is recorded in
    sizing_failures and skipped, never raised, mirroring `.356`'s own
    `test_run_live_dry_run_cycle_skips_unsizeable_symbol_not_whole_cycle`."""
    bars = make_bars(n=60)
    bars_client = FakeBarsClient(bars_by_symbol={"AAPL": bars})

    class _BrokenEquityClient(FakeAlpacaClient):
        def get_account(self):
            raise RuntimeError("account fetch failed")

    alpaca_client = _BrokenEquityClient()
    result = M.run_manual_trigger_stage1b_cycle(
        (EQUITY_CLI.LiveSymbolRequest(symbol="AAPL", asset_class="STOCK", quantity=None),),
        confirmed=True, bars_client=bars_client, alpaca_client=alpaca_client,
        max_new_orders_per_cycle=1, lookback_bars=60, universe_version=SIGSRC.UNIVERSE_VERSION,
        max_snapshot_age_seconds=10**9, fill_poll_timeout_seconds=1.0, fill_poll_interval_seconds=1.0,
        now=NOW,
    )
    assert result["symbol_fetch_failures"] == []
    assert len(result["sizing_failures"]) == 1
    assert result["sizing_failures"][0]["symbol"] == "AAPL"
    assert result["sizing_failures"][0]["error"] == "NO_REAL_ACCOUNT_EQUITY_AVAILABLE_FOR_SIZING"
    assert result["stage1_report"] is None
    assert result["equity_history_observation_count"] == 0


# ============================================================================
# 5. Strategy labeling -- honest, non-synthetic defaults.
# ============================================================================


def test_default_strategy_id_is_not_355s_synthetic_default():
    assert M.DEFAULT_STRATEGY_ID != STAGE1.DEFAULT_STRATEGY_ID
    assert "SYNTHETIC" not in M.DEFAULT_STRATEGY_ID
    assert M.DEFAULT_STRATEGY_VERSION != STAGE1.DEFAULT_STRATEGY_VERSION
