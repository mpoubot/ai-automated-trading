#!/usr/bin/env python3
"""Contract + wiring tests for AURA v0.5.3.56 Stage 3A live-wiring CLI.

Mirrors `.55`'s own test convention (see `tests/test_aura_v05355_...py`'s
module docstring): every real dependency module is loaded into
`sys.modules` under its canonical name BEFORE `.56` is loaded, so `.56`'s
own dynamic imports resolve to the SAME module objects these tests build
fixtures against. Only the outermost edges are faked: a bars-client stand-
in for `alpaca-py`'s `StockHistoricalDataClient` (via `.51`'s
`AlpacaHistoricalBarsClient`, whose constructor is never exercised here --
only `fetch_recent_bars`/`build_technical_regime_from_bars` etc., which
take an already-constructed client), and `FakeAlpacaClient` (copied from
`.55`'s own test fixture) standing in for `TradingClient`.

NO LIVE ALPACA ACCESS IS REQUIRED OR ATTEMPTED BY ANY TEST IN THIS FILE.
NO REAL CREDENTIAL VALUE IS USED ANYWHERE IN THIS FILE -- every credential
env var set by a test is an obviously-fake placeholder string.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
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
M = _load("aura_v05356_stage3_live_equity_cli", ROOT / "aura_v05356_stage3_live_equity_cli.py")

NOW = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)
EQUITY_API_KEY_ENV = M.EQUITY_API_KEY_ENV
EQUITY_SECRET_KEY_ENV = M.EQUITY_SECRET_KEY_ENV


# ============================================================================
# Fakes -- outermost edges only.
# ============================================================================


class FakeAccount:
    def __init__(self, equity):
        self.equity = equity


class FakeAlpacaClient:
    """Copied from `.55`'s own test fixture (same shape/behavior) -- a
    stand-in for `alpaca-py`'s `TradingClient`. This test file only ever
    calls read-only methods on it (`get_account`, `get_all_positions`);
    `submit_order` exists only so a mistaken call would be visible/
    countable, never expected to be hit."""

    def __init__(self, *, equity=100_000.0, positions=None, account_exception=None):
        self._equity = equity
        self._positions = positions or []
        self._account_exception = account_exception
        self.submit_calls: list = []

    def get_account(self):
        if self._account_exception:
            raise self._account_exception
        return FakeAccount(equity=self._equity)

    def get_all_positions(self):
        return self._positions

    def get_asset(self, symbol):  # pragma: no cover -- not exercised by this CLI
        raise AssertionError("get_asset should never be called by the Stage 3A dry-run CLI")

    def submit_order(self, order_data):  # pragma: no cover -- must never be reached
        self.submit_calls.append(order_data)
        raise AssertionError("submit_order must NEVER be called by the Stage 3A dry-run CLI")


class _FakeBarsResponse:
    def __init__(self, df):
        self.df = df


class FakeBarsClient:
    """Stand-in for `.51`'s `AlpacaHistoricalBarsClient` -- implements
    only `get_stock_bars`, exactly the `BarsClient` Protocol `.51`'s
    `fetch_recent_bars` requires."""

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


class FakeArticle:
    def __init__(self, id, headline, summary="", url=None, author="Staff",
                 created_at=None, updated_at=None, symbols=None, source="Benzinga"):
        self.id = id
        self.headline = headline
        self.summary = summary
        self.url = url
        self.author = author
        self.created_at = created_at or NOW
        self.updated_at = updated_at or NOW
        self.symbols = symbols if symbols is not None else []
        self.source = source


class FakeNewsSet:
    def __init__(self, articles):
        self.data = {"news": list(articles)}


class FakeNewsClient:
    """Duck-typed stand-in for `.46`'s real `NewsClient` (matches `.46`'s
    own test fixture)."""

    def __init__(self, articles=None, raise_exc=None):
        self._articles = articles or []
        self._raise_exc = raise_exc
        self.calls: list = []

    def get_news(self, request_params):
        self.calls.append(request_params)
        if self._raise_exc is not None:
            raise self._raise_exc
        return FakeNewsSet(self._articles)


def make_bars(n=60, start_price=100.0, tz_index=True) -> pd.DataFrame:
    """Minimal, deterministic, ascending-time OHLCV frame -- enough rows
    to clear `FROZEN_TECHNICAL_PARAMS.min_bars_required` (55). Values are
    a mild uptrend; this file does not assert on the resulting
    TechnicalRegime's `signal_score`/`status`, only that real evidence
    flows through the wiring correctly -- so the exact shape doesn't need
    to be curated for a specific outcome."""
    idx = pd.date_range(end=NOW, periods=n, freq="D", tz="UTC" if tz_index else None)
    closes = [start_price + i * 0.5 for i in range(n)]
    df = pd.DataFrame(
        {
            "open": [c - 0.2 for c in closes],
            "high": [c + 0.3 for c in closes],
            "low": [c - 0.3 for c in closes],
            "close": closes,
            "volume": [1_000_000 + i * 100 for i in range(n)],
        },
        index=idx,
    )
    return df


def make_symbol_requests_file(tmp_path, entries) -> Path:
    p = tmp_path / "requests.json"
    p.write_text(json.dumps(entries), encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def clean_credential_env(monkeypatch):
    monkeypatch.delenv(EQUITY_API_KEY_ENV, raising=False)
    monkeypatch.delenv(EQUITY_SECRET_KEY_ENV, raising=False)
    yield


# ============================================================================
# 1. Credential loading
# ============================================================================


def test_load_credentials_success(monkeypatch):
    monkeypatch.setenv(EQUITY_API_KEY_ENV, "FAKE_TEST_KEY_ID_12345")
    monkeypatch.setenv(EQUITY_SECRET_KEY_ENV, "FAKE_TEST_SECRET_VALUE_67890")
    key, secret = M.load_equity_paper_credentials()
    assert key == "FAKE_TEST_KEY_ID_12345"
    assert secret == "FAKE_TEST_SECRET_VALUE_67890"


def test_load_credentials_missing_both():
    with pytest.raises(M.Stage3CliError) as exc:
        M.load_equity_paper_credentials()
    assert EQUITY_API_KEY_ENV in str(exc.value)
    assert EQUITY_SECRET_KEY_ENV in str(exc.value)


def test_load_credentials_missing_one(monkeypatch):
    monkeypatch.setenv(EQUITY_API_KEY_ENV, "FAKE_TEST_KEY_ID_12345")
    with pytest.raises(M.Stage3CliError) as exc:
        M.load_equity_paper_credentials()
    assert EQUITY_SECRET_KEY_ENV in str(exc.value)


def test_load_credentials_empty_string_counts_as_missing(monkeypatch):
    monkeypatch.setenv(EQUITY_API_KEY_ENV, "")
    monkeypatch.setenv(EQUITY_SECRET_KEY_ENV, "FAKE_TEST_SECRET_VALUE_67890")
    with pytest.raises(M.Stage3CliError):
        M.load_equity_paper_credentials()


def test_load_credentials_never_falls_back_to_crypto_pair(monkeypatch):
    """The crypto account's ALPACA_PAPER_API_KEY/SECRET_KEY being set must
    NOT satisfy this CLI's credential requirement -- Martin's explicit
    Stage 3 decision was a dedicated, non-overlapping pair."""
    monkeypatch.setenv("ALPACA_PAPER_API_KEY", "FAKE_CRYPTO_KEY")
    monkeypatch.setenv("ALPACA_PAPER_SECRET_KEY", "FAKE_CRYPTO_SECRET")
    with pytest.raises(M.Stage3CliError):
        M.load_equity_paper_credentials()


# ============================================================================
# 2. Symbol-requests file loading
# ============================================================================


def test_load_symbol_requests_success(tmp_path):
    p = make_symbol_requests_file(tmp_path, [{"symbol": "AAPL", "asset_class": "STOCK", "quantity": 10}])
    reqs = M.load_symbol_requests(p)
    assert len(reqs) == 1
    assert reqs[0].symbol == "AAPL" and reqs[0].asset_class == "STOCK" and reqs[0].quantity == 10


def test_load_symbol_requests_file_missing(tmp_path):
    with pytest.raises(M.Stage3CliError):
        M.load_symbol_requests(tmp_path / "nope.json")


def test_load_symbol_requests_empty_list(tmp_path):
    p = make_symbol_requests_file(tmp_path, [])
    with pytest.raises(M.Stage3CliError):
        M.load_symbol_requests(p)


def test_load_symbol_requests_missing_field(tmp_path):
    p = make_symbol_requests_file(tmp_path, [{"symbol": "AAPL", "asset_class": "STOCK"}])
    with pytest.raises(M.Stage3CliError):
        M.load_symbol_requests(p)


def test_load_symbol_requests_duplicate_symbol(tmp_path):
    p = make_symbol_requests_file(tmp_path, [
        {"symbol": "AAPL", "asset_class": "STOCK", "quantity": 10},
        {"symbol": "AAPL", "asset_class": "STOCK", "quantity": 5},
    ])
    with pytest.raises(M.Stage3CliError):
        M.load_symbol_requests(p)


def test_load_symbol_requests_invalid_asset_class(tmp_path):
    p = make_symbol_requests_file(tmp_path, [{"symbol": "AAPL", "asset_class": "CRYPTO", "quantity": 10}])
    with pytest.raises(M.Stage3CliError):
        M.load_symbol_requests(p)


# ============================================================================
# 3. Client construction (mocked -- never touches the network)
# ============================================================================


def test_build_bars_client_wraps_alpaca_historical_bars_client(monkeypatch):
    constructed = {}

    class _FakeHistClient:
        def __init__(self, api_key, secret_key):
            constructed["api_key"] = api_key
            constructed["secret_key"] = secret_key

    monkeypatch.setattr(M51, "AlpacaHistoricalBarsClient", _FakeHistClient)
    client = M.build_bars_client("FAKE_KEY", "FAKE_SECRET", M51)
    assert isinstance(client, _FakeHistClient)
    assert constructed == {"api_key": "FAKE_KEY", "secret_key": "FAKE_SECRET"}


def test_build_trading_client_passes_paper_true(monkeypatch):
    import types

    captured = {}

    class _FakeTradingClient:
        def __init__(self, key, secret, paper):
            captured.update(key=key, secret=secret, paper=paper)

    fake_alpaca_trading = types.ModuleType("alpaca.trading.client")
    fake_alpaca_trading.TradingClient = _FakeTradingClient
    monkeypatch.setitem(sys.modules, "alpaca.trading.client", fake_alpaca_trading)

    client = M.build_trading_client("FAKE_KEY", "FAKE_SECRET")
    assert isinstance(client, _FakeTradingClient)
    assert captured["paper"] is True


# ============================================================================
# 4. Real .51/.52 fetch -> SymbolEvidence translation
# ============================================================================


def test_fetch_symbol_evidence_success_builds_both_regimes_and_last_close():
    bars = make_bars(n=60)
    bars_client = FakeBarsClient(bars_by_symbol={"AAPL": bars})
    evidence = M.fetch_symbol_evidence(
        "AAPL", bars_client=bars_client, technical_module=M51, short_technical_module=M52,
        frozen_technical_params=SIGSRC.FROZEN_TECHNICAL_PARAMS,
        frozen_short_technical_params=SIGSRC.FROZEN_SHORT_TECHNICAL_PARAMS,
        lookback_bars=60, universe_version=SIGSRC.UNIVERSE_VERSION, now=NOW,
    )
    assert evidence.fetch_error is None
    assert evidence.technical_regime is not None
    assert evidence.short_technical_regime is not None
    assert evidence.last_close == pytest.approx(bars["close"].iloc[-1])
    # real regime objects, not stand-ins -- confirms .51/.52's own typed output reaches here
    assert isinstance(evidence.technical_regime, M51.TechnicalRegime)
    assert isinstance(evidence.short_technical_regime, M52.ShortTechnicalRegime)


def test_fetch_symbol_evidence_populates_bars_as_dicts_when_orchestrator_supplied():
    bars = make_bars(n=60)
    bars_client = FakeBarsClient(bars_by_symbol={"AAPL": bars})
    evidence = M.fetch_symbol_evidence(
        "AAPL", bars_client=bars_client, technical_module=M51, short_technical_module=M52,
        frozen_technical_params=SIGSRC.FROZEN_TECHNICAL_PARAMS,
        frozen_short_technical_params=SIGSRC.FROZEN_SHORT_TECHNICAL_PARAMS,
        lookback_bars=60, universe_version=SIGSRC.UNIVERSE_VERSION, now=NOW,
        orchestrator_module=ORCH362,
    )
    assert len(evidence.bars_as_dicts) == 60
    assert evidence.bars_as_dicts[-1]["close"] == pytest.approx(bars["close"].iloc[-1])


def test_fetch_symbol_evidence_bars_as_dicts_empty_without_orchestrator():
    bars = make_bars(n=60)
    bars_client = FakeBarsClient(bars_by_symbol={"AAPL": bars})
    evidence = M.fetch_symbol_evidence(
        "AAPL", bars_client=bars_client, technical_module=M51, short_technical_module=M52,
        frozen_technical_params=SIGSRC.FROZEN_TECHNICAL_PARAMS,
        frozen_short_technical_params=SIGSRC.FROZEN_SHORT_TECHNICAL_PARAMS,
        lookback_bars=60, universe_version=SIGSRC.UNIVERSE_VERSION, now=NOW,
    )
    assert evidence.bars_as_dicts == ()


def test_build_news_client_constructs_real_news_client(monkeypatch):
    import types

    constructed = {}

    class _FakeRealNewsClient:
        def __init__(self, key, secret):
            constructed.update(key=key, secret=secret)

    fake_alpaca_news = types.ModuleType("alpaca.data.historical.news")
    fake_alpaca_news.NewsClient = _FakeRealNewsClient
    monkeypatch.setitem(sys.modules, "alpaca.data.historical.news", fake_alpaca_news)

    client = M.build_news_client("FAKE_KEY", "FAKE_SECRET")
    assert isinstance(client, _FakeRealNewsClient)
    assert constructed == {"key": "FAKE_KEY", "secret": "FAKE_SECRET"}


def test_fetch_symbol_evidence_network_failure_never_raises():
    bars_client = FakeBarsClient(exception=RuntimeError("connection reset"))
    evidence = M.fetch_symbol_evidence(
        "AAPL", bars_client=bars_client, technical_module=M51, short_technical_module=M52,
        frozen_technical_params=SIGSRC.FROZEN_TECHNICAL_PARAMS,
        frozen_short_technical_params=SIGSRC.FROZEN_SHORT_TECHNICAL_PARAMS,
        lookback_bars=60, universe_version=SIGSRC.UNIVERSE_VERSION, now=NOW,
    )
    assert evidence.fetch_error is not None
    assert evidence.technical_regime is None
    assert evidence.last_close is None


def test_build_reference_price_fn_uses_real_last_close():
    ev = M.SymbolEvidence(symbol="AAPL", technical_regime=None, short_technical_regime=None, last_close=123.45)
    fn = M.build_reference_price_fn({"AAPL": ev})
    assert fn("AAPL") == 123.45


def test_build_reference_price_fn_raises_for_unknown_symbol():
    fn = M.build_reference_price_fn({})
    with pytest.raises(M.Stage3CliError):
        fn("AAPL")


# ============================================================================
# 5. Real (read-only) account equity fetch
# ============================================================================


def test_fetch_real_account_equity_usd_success():
    client = FakeAlpacaClient(equity=54321.0)
    equity = M.fetch_real_account_equity_usd(client, observability_module=OBS)
    assert equity == 54321.0


def test_fetch_real_account_equity_usd_unavailable_on_account_failure():
    client = FakeAlpacaClient(account_exception=RuntimeError("auth failed"))
    equity = M.fetch_real_account_equity_usd(client, observability_module=OBS)
    assert equity is None


# ============================================================================
# 6. Full cycle wiring -- .51/.52 -> SymbolCycleInput -> existing .55/.53,
#    dry-run enforcement, .44 preservation, no accidental submission.
# ============================================================================


def test_run_live_dry_run_cycle_never_touches_stage1b_or_submission(monkeypatch):
    """Static + behavioral guarantee: this module never calls
    run_stage1b_paper_cycle and never passes attempt_submission=True
    anywhere. Source-level check first (cheap, catches an accidental
    future addition), then a spy confirms the actual call at runtime.

    The static check parses the AST rather than doing a raw substring
    search, so it flags a real import/attribute-access/call of
    `run_stage1b_paper_cycle` or an `attempt_submission=` keyword actually
    passed in code, without tripping on the module's own docstring/comment
    prose that documents this exact guarantee (e.g. "never
    `run_stage1b_paper_cycle`" in a comment)."""
    import ast

    source = (ROOT / "aura_v05356_stage3_live_equity_cli.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    referenced_names = set()
    submission_keywords = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            referenced_names.add(node.id)
        elif isinstance(node, ast.Attribute):
            referenced_names.add(node.attr)
        elif isinstance(node, ast.keyword) and node.arg == "attempt_submission":
            submission_keywords.append(node)

    assert "run_stage1b_paper_cycle" not in referenced_names, (
        "run_stage1b_paper_cycle is referenced as actual code (import/"
        "attribute/call), not just in a comment or docstring"
    )
    assert not submission_keywords, (
        "attempt_submission= is passed as an actual keyword argument in code"
    )

    calls = []
    real_run_stage1a = STAGE1.run_stage1a_dry_run

    def _spy(*args, **kwargs):
        calls.append(kwargs)
        return real_run_stage1a(*args, **kwargs)

    monkeypatch.setattr(STAGE1, "run_stage1a_dry_run", _spy)

    bars = make_bars(n=60)
    bars_client = FakeBarsClient(bars_by_symbol={"AAPL": bars})
    alpaca_client = FakeAlpacaClient(equity=100_000.0)
    requests = (M.LiveSymbolRequest(symbol="AAPL", asset_class="STOCK", quantity=10),)

    result = M.run_live_dry_run_cycle(
        requests, bars_client=bars_client, alpaca_client=alpaca_client,
        max_new_orders_per_cycle=5, lookback_bars=60, universe_version=SIGSRC.UNIVERSE_VERSION,
        max_snapshot_age_seconds=300.0, skip_account_equity_fetch=False, now=NOW,
    )

    assert len(calls) == 1
    assert (result["stage1_report"] or {}).get("submitted_count", 0) == 0
    # FakeAlpacaClient.submit_order raises AssertionError if ever called --
    # the fact this line is reached without raising is itself part of the
    # "no accidental submission" guarantee.
    assert alpaca_client.submit_calls == []


def test_run_live_dry_run_cycle_uses_live_evidence_decide_kwargs_not_frozen(monkeypatch):
    """As of the 2026-09-25 live-evidence extension, this CLI's decide_
    kwargs come from `.362.LIVE_EVIDENCE_DECIDE_KWARGS` (technical_weight
    = 1.0), NOT `.054.FROZEN_DECIDE_KWARGS` (technical_weight = 0.0).
    `.054`'s own frozen constant and its own test suite are untouched --
    this only confirms `.356` no longer reads it for this purpose."""
    captured = {}
    real_run_stage1a = STAGE1.run_stage1a_dry_run

    def _spy(*args, **kwargs):
        captured.update(kwargs)
        return real_run_stage1a(*args, **kwargs)

    monkeypatch.setattr(STAGE1, "run_stage1a_dry_run", _spy)

    bars_client = FakeBarsClient(bars_by_symbol={"AAPL": make_bars(n=60)})
    result = M.run_live_dry_run_cycle(
        (M.LiveSymbolRequest(symbol="AAPL", asset_class="STOCK", quantity=10),),
        bars_client=bars_client, alpaca_client=None, max_new_orders_per_cycle=5,
        lookback_bars=60, universe_version=SIGSRC.UNIVERSE_VERSION,
        max_snapshot_age_seconds=300.0, skip_account_equity_fetch=True, now=NOW,
    )
    assert captured["decide_kwargs"]["technical_weight"] == 1.0
    assert captured["decide_kwargs"]["short_technical_weight"] == 1.0
    assert captured["decide_kwargs"]["sentiment_weight"] == 1.0
    assert captured["decide_kwargs"]["wave_weight"] == 1.0
    assert captured["decide_kwargs"]["sector_rotation_weight"] == 0.0
    assert result["decide_kwargs_source"] == "aura_v05362_live_evidence_orchestrator.LIVE_EVIDENCE_DECIDE_KWARGS"
    # .054's own frozen constant is completely untouched by this change
    assert SIGSRC.FROZEN_DECIDE_KWARGS["technical_weight"] == 0.0
    assert SIGSRC.FROZEN_DECIDE_KWARGS["short_technical_weight"] == 0.0


def test_run_live_dry_run_cycle_without_news_client_leaves_sentiment_wave_none(monkeypatch):
    """Backward compatibility: omitting news_client/news_state_dir (the
    pre-extension call shape) must behave exactly as before -- sentiment_
    regime/wave_result stay None, nothing raises."""
    captured_args = {}
    real_run_stage1a = STAGE1.run_stage1a_dry_run

    def _spy(symbol_requests, **kwargs):
        captured_args["symbol_requests"] = symbol_requests
        return real_run_stage1a(symbol_requests, **kwargs)

    monkeypatch.setattr(STAGE1, "run_stage1a_dry_run", _spy)

    bars_client = FakeBarsClient(bars_by_symbol={"AAPL": make_bars(n=60)})
    result = M.run_live_dry_run_cycle(
        (M.LiveSymbolRequest(symbol="AAPL", asset_class="STOCK", quantity=10),),
        bars_client=bars_client, alpaca_client=None, max_new_orders_per_cycle=5,
        lookback_bars=60, universe_version=SIGSRC.UNIVERSE_VERSION,
        max_snapshot_age_seconds=300.0, skip_account_equity_fetch=True, now=NOW,
    )
    reqs = captured_args["symbol_requests"]
    assert len(reqs) == 1
    assert reqs[0].sentiment_regime is None
    assert reqs[0].wave_result is None
    assert result["news_ingestion_status"] is None
    assert result["sector_rotation_by_symbol"] == {}


def test_run_live_dry_run_cycle_with_news_populates_sentiment_and_wave(monkeypatch, tmp_path):
    captured_args = {}
    real_run_stage1a = STAGE1.run_stage1a_dry_run

    def _spy(symbol_requests, **kwargs):
        captured_args["symbol_requests"] = symbol_requests
        return real_run_stage1a(symbol_requests, **kwargs)

    monkeypatch.setattr(STAGE1, "run_stage1a_dry_run", _spy)

    bars_client = FakeBarsClient(bars_by_symbol={"AAPL": make_bars(n=60)})
    news_client = FakeNewsClient(articles=[
        FakeArticle(id=1, headline="Acme beats estimates", symbols=["AAPL"], source="Reuters",
                    created_at=NOW.isoformat()),
        FakeArticle(id=2, headline="Acme guidance raised", symbols=["AAPL"], source="Bloomberg",
                    created_at=NOW.isoformat()),
    ])

    result = M.run_live_dry_run_cycle(
        (M.LiveSymbolRequest(symbol="AAPL", asset_class="STOCK", quantity=10),),
        bars_client=bars_client, alpaca_client=None, max_new_orders_per_cycle=5,
        lookback_bars=60, universe_version=SIGSRC.UNIVERSE_VERSION,
        max_snapshot_age_seconds=300.0, skip_account_equity_fetch=True,
        news_client=news_client, news_state_dir=tmp_path / "news_state", now=NOW,
    )
    reqs = captured_args["symbol_requests"]
    assert len(reqs) == 1
    assert reqs[0].sentiment_regime is not None
    assert reqs[0].wave_result is not None
    assert result["news_ingestion_status"]["fetch_status"]["status"] == "SUCCESS"
    assert result["sentiment_wave_params"] == dict(ORCH362.SENTIMENT_WAVE_PARAMS)
    # no SPY/GLD/SLV bars supplied in this single-symbol test -> sector rotation logged as skipped
    assert result["sector_rotation_by_symbol"]["AAPL"] is None
    assert "AAPL" in result["live_evidence_build_errors"]


def test_run_live_dry_run_cycle_news_failure_fails_open(tmp_path):
    """A broken news fetch must never block the technical-only path --
    the cycle still runs, sentiment/wave simply stay unavailable for this
    cycle, mirroring every other fail-open discipline in this module."""
    bars_client = FakeBarsClient(bars_by_symbol={"AAPL": make_bars(n=60)})
    news_client = FakeNewsClient(raise_exc=RuntimeError("network down"))
    news_state_dir = tmp_path / "news_state"

    result = M.run_live_dry_run_cycle(
        (M.LiveSymbolRequest(symbol="AAPL", asset_class="STOCK", quantity=10),),
        bars_client=bars_client, alpaca_client=None, max_new_orders_per_cycle=5,
        lookback_bars=60, universe_version=SIGSRC.UNIVERSE_VERSION,
        max_snapshot_age_seconds=300.0, skip_account_equity_fetch=True,
        news_client=news_client, news_state_dir=news_state_dir, now=NOW,
    )
    assert result["news_ingestion_status"]["fetch_status"]["status"] == "FAILED"
    assert result["stage1_report"] is not None  # cycle still ran


def test_run_live_dry_run_cycle_preserves_44_enforcement_wiring(monkeypatch):
    """Reaching a real ALLOW/BLOCK from `.44` requires a non-ABSTAIN
    decision, which the currently-frozen technical_weight=0.0 makes
    unreachable via real `.51`/`.52` evidence alone (documented, not a
    test gap) -- so this test asserts the WIRING itself: `limits` and a
    real `reference_price_fn` reach `run_stage1a_dry_run` (which is what
    actually builds and calls the real `.44` `enforcement_check_fn`
    internally), rather than asserting a specific ALLOW/BLOCK outcome."""
    captured = {}
    real_run_stage1a = STAGE1.run_stage1a_dry_run

    def _spy(*args, **kwargs):
        captured.update(kwargs)
        return real_run_stage1a(*args, **kwargs)

    monkeypatch.setattr(STAGE1, "run_stage1a_dry_run", _spy)

    bars = make_bars(n=60)
    bars_client = FakeBarsClient(bars_by_symbol={"AAPL": bars})
    alpaca_client = FakeAlpacaClient(equity=100_000.0)

    M.run_live_dry_run_cycle(
        (M.LiveSymbolRequest(symbol="AAPL", asset_class="STOCK", quantity=10),),
        bars_client=bars_client, alpaca_client=alpaca_client, max_new_orders_per_cycle=5,
        lookback_bars=60, universe_version=SIGSRC.UNIVERSE_VERSION,
        max_snapshot_age_seconds=300.0, skip_account_equity_fetch=False, now=NOW,
    )

    assert captured["limits"] is not None
    assert callable(captured["reference_price_fn"])
    assert captured["reference_price_fn"]("AAPL") == pytest.approx(bars["close"].iloc[-1])
    assert captured["synthetic_account_equity_usd"] == 100_000.0  # real, read-only-fetched equity


def test_run_live_dry_run_cycle_skip_account_equity_fetch_flag():
    bars = make_bars(n=60)
    bars_client = FakeBarsClient(bars_by_symbol={"AAPL": bars})
    result = M.run_live_dry_run_cycle(
        (M.LiveSymbolRequest(symbol="AAPL", asset_class="STOCK", quantity=10),),
        bars_client=bars_client, alpaca_client=None, max_new_orders_per_cycle=5,
        lookback_bars=60, universe_version=SIGSRC.UNIVERSE_VERSION,
        max_snapshot_age_seconds=300.0, skip_account_equity_fetch=True, now=NOW,
    )
    assert result["account_equity_usd_source"] == "SKIPPED_BY_FLAG"
    assert result["account_equity_usd"] is None


def test_run_live_dry_run_cycle_symbol_fetch_failure_recorded_not_raised():
    bars_client = FakeBarsClient(exception=RuntimeError("network down"))
    result = M.run_live_dry_run_cycle(
        (M.LiveSymbolRequest(symbol="AAPL", asset_class="STOCK", quantity=10),),
        bars_client=bars_client, alpaca_client=None, max_new_orders_per_cycle=5,
        lookback_bars=60, universe_version=SIGSRC.UNIVERSE_VERSION,
        max_snapshot_age_seconds=300.0, skip_account_equity_fetch=True, now=NOW,
    )
    assert len(result["symbol_fetch_failures"]) == 1
    assert result["symbol_fetch_failures"][0]["symbol"] == "AAPL"
    assert result["stage1_report"] is None  # no usable symbols reached .55 at all


# ============================================================================
# 7. main() -- error handling at the CLI boundary
# ============================================================================


def test_main_fails_closed_on_missing_credentials(tmp_path, capsys):
    p = make_symbol_requests_file(tmp_path, [{"symbol": "AAPL", "asset_class": "STOCK", "quantity": 10}])
    out = tmp_path / "out.json"
    rc = M.main([
        "--requests-config", str(p), "--max-new-orders-per-cycle", "5",
        "--max-snapshot-age-seconds", "300", "--skip-account-equity-fetch",
        "--output", str(out),
    ])
    assert rc == 1
    captured = capsys.readouterr()
    assert "FAIL-CLOSED" in captured.err
    assert EQUITY_API_KEY_ENV in captured.err
    assert not out.exists()


def test_main_fails_closed_on_missing_requests_config(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(EQUITY_API_KEY_ENV, "FAKE_KEY")
    monkeypatch.setenv(EQUITY_SECRET_KEY_ENV, "FAKE_SECRET")
    out = tmp_path / "out.json"
    rc = M.main([
        "--requests-config", str(tmp_path / "missing.json"), "--max-new-orders-per-cycle", "5",
        "--max-snapshot-age-seconds", "300", "--skip-account-equity-fetch",
        "--output", str(out),
    ])
    assert rc == 1
    assert not out.exists()
