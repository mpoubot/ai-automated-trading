#!/usr/bin/env python3
"""Contract + unit tests for AURA v0.5.3.53 Full paper orchestration.

`.33`, `.49`, `.50`, `.38` are loaded into `sys.modules` under their
canonical names BEFORE `.53` is loaded, so `.53`'s own internal dynamic
imports (via `__import__`) resolve to the SAME module objects these tests
construct fixtures against -- mirroring `.50`'s own test convention for
its dependency chain, and `.38`'s test convention one layer further down.

`.53`'s own `run_cycle` is exercised end-to-end against REAL `.50`/`.33`/
`.38` code (not mocks of them) with only the outermost edges faked: a
`.47` `SentimentRegime` fixture (duplicated from `.50`'s own test module,
matching this project's established "each test file builds its own
fixtures" convention -- see `.52`'s own test file doing the same for
`.51`'s bar-generation helper), a fake LLM client for `.49`'s proposal/
challenge step, and a fake Alpaca client for `.38`'s actual-submission
path. This gives real confidence `.53` is wired correctly to its
dependencies' ACTUAL contracts, not to a hand-rolled stand-in that could
silently drift from them.
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
SENT = _load("aura_v05347_market_sentiment_scoring", ROOT / "aura_v05347_market_sentiment_scoring.py")
PROPOSAL = _load("aura_v05349_ai_proposal_pipeline", ROOT / "aura_v05349_ai_proposal_pipeline.py")
ENGINE = _load("aura_v05350_decision_engine", ROOT / "aura_v05350_decision_engine.py")
SUP = _load("aura_v05338_common_execution_supervisor", ROOT / "aura_v05338_common_execution_supervisor.py")
M = _load("aura_v05353_full_paper_orchestration", ROOT / "aura_v05353_full_paper_orchestration.py")

NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)
NOW_ISO = NOW.isoformat()


# ============================================================================
# Shared fixtures -- duplicated from `.50`'s own test module (a test-file
# concern, not production code; see module docstring).
# ============================================================================


def make_sentiment(*, symbol="AAPL", promotable_score=0.6, corroboration_status="SUFFICIENT", source_count=3, as_of=NOW_ISO):
    return SENT.SentimentRegime(
        symbol=symbol, as_of=as_of, decay_window_hours=48.0, min_source_count=2,
        items_considered=5, input_event_ids=("e1", "e2", "e3"),
        distinct_origin_sources=("Benzinga", "Reuters", "Bloomberg")[:source_count],
        source_count=source_count, corroboration_status=corroboration_status,
        bullish_count=3, bearish_count=0, neutral_count=2, mixed_count=0,
        raw_score=promotable_score if promotable_score is not None else 0.0,
        promotable_score=promotable_score,
    )


class FakeLLMClient:
    def __init__(self, response, *, name="fake-model", model="fake-model-v1"):
        self._response = response
        self._name = name
        self._model = model

    def complete(self, system, user, *, json_mode=False):
        if isinstance(self._response, BaseException):
            raise self._response
        return self._response

    @property
    def model(self):
        return self._model

    @property
    def name(self):
        return self._name


def proposal_json(candidate_id, thesis="a thesis", confidence=0.9):
    return json.dumps({"candidate_id": candidate_id, "thesis": thesis, "confidence": confidence})


def make_alpaca_asset(*, symbol="AAPL", shortable=True, easy_to_borrow=True):
    return {
        "symbol": symbol, "asset_class": "us_equity", "exchange": "NASDAQ", "status": "active",
        "tradable": True, "shortable": shortable, "easy_to_borrow": easy_to_borrow,
        "fractionable": True, "marginable": True,
        "min_order_size": None, "min_trade_increment": None, "price_increment": None,
    }


def alpaca_auth_config(**overrides):
    config = {"kill_switch": False, "execution_authorized": True, "paper_execution_authorized": True,
               "authorization_ttl_seconds": 60, "safety_state_ttl_seconds": 60}
    config.update(overrides)
    return config


def sup_open(**overrides):
    config = {"kill_switch": False}
    config.update(overrides)
    return config


class FakeAlpacaClient:
    """Same shape as `.38`'s own test suite's FakeAlpacaClient."""

    def __init__(self, existing_order=None, submit_exception=None):
        self._existing = existing_order
        self._submit_exception = submit_exception
        self.submit_calls: list = []

    def get_order_by_client_id(self, client_order_id):
        if self._existing is None:
            raise Exception("order not found")
        return self._existing

    def submit_order(self, order_data):
        self.submit_calls.append(order_data)
        if self._submit_exception:
            raise self._submit_exception

        class _FakeOrder:
            id = "broker-123"
            status = "new"

        return _FakeOrder()


DECIDE_KWARGS_BASE = dict(
    sentiment_weight=1.0,
    wave_weight=1.0,
    technical_weight=0.0,
    short_technical_weight=0.0,
    decision_threshold=0.1,
    ai_penalty_per_concern=0.2,
    critic_penalty_per_issue=0.15,
    proposal_module=PROPOSAL,
)


def decide_kwargs(llm_client):
    kwargs = dict(DECIDE_KWARGS_BASE)
    kwargs["llm_client"] = llm_client
    return kwargs


def make_input(symbol, *, promotable_score=0.6, already_held=False, quantity="10", asset_class="STOCK"):
    return M.SymbolCycleInput(
        symbol=symbol, asset_class=asset_class, quantity=quantity, already_held=already_held,
        sentiment_regime=make_sentiment(symbol=symbol, promotable_score=promotable_score),
        alpaca_asset=make_alpaca_asset(symbol=symbol),
    )


def dirs_kwargs(tmp_path, tag):
    return dict(
        auth_claims_dir=tmp_path / f"{tag}-auth-claims",
        supervisor_claims_dir=tmp_path / f"{tag}-supervisor-claims",
    )


# ============================================================================
# 1. Skip-if-already-held
# ============================================================================


def test_already_held_symbol_is_skipped_no_decision_attempted(tmp_path):
    inputs = (make_input("AAPL", already_held=True),)
    result = M.run_cycle(
        inputs, max_new_orders_per_cycle=5, decide_kwargs=decide_kwargs(FakeLLMClient(RuntimeError("must not be called"))),
        strategy_id="S", strategy_version="v1", now=NOW,
    )
    assert len(result.outcomes) == 1
    outcome = result.outcomes[0]
    assert outcome.stage == "SKIPPED_ALREADY_HELD"
    assert outcome.decision is None
    assert outcome.supervision_result is None


# ============================================================================
# 2. Not shortlisted (no usable evidence at all)
# ============================================================================


def test_no_evidence_symbol_is_not_shortlisted():
    inputs = (M.SymbolCycleInput(symbol="ZZZZ", asset_class="STOCK", quantity="1"),)
    result = M.run_cycle(
        inputs, max_new_orders_per_cycle=5, decide_kwargs=decide_kwargs(FakeLLMClient(RuntimeError("must not be called"))),
        strategy_id="S", strategy_version="v1", now=NOW,
    )
    assert result.outcomes[0].stage == "NOT_SHORTLISTED"
    assert result.outcomes[0].decision is None


# ============================================================================
# 3. NO_TRADE_DECIDED (evidence present, but no actionable decision)
# ============================================================================


def test_low_conviction_symbol_is_no_trade_decided():
    inputs = (make_input("AAPL", promotable_score=0.05),)  # below decision_threshold=0.1
    client = FakeLLMClient(proposal_json("x"))
    result = M.run_cycle(
        inputs, max_new_orders_per_cycle=5, decide_kwargs=decide_kwargs(client), strategy_id="S", strategy_version="v1", now=NOW,
    )
    outcome = result.outcomes[0]
    assert outcome.stage == "NO_TRADE_DECIDED"
    assert outcome.decision is not None
    assert outcome.decision.outcome == "NO_TRADE"
    assert outcome.supervision_result is None


def test_news_only_symbol_abstains_and_is_no_trade_decided():
    inputs = (M.SymbolCycleInput(symbol="AAPL", asset_class="STOCK", quantity="1", news_item_count=2),)
    client = FakeLLMClient(RuntimeError("must not be called -- no directional evidence, .50 skips AI"))
    result = M.run_cycle(
        inputs, max_new_orders_per_cycle=5, decide_kwargs=decide_kwargs(client), strategy_id="S", strategy_version="v1", now=NOW,
    )
    outcome = result.outcomes[0]
    assert outcome.stage == "NO_TRADE_DECIDED"
    assert outcome.decision.outcome == "ABSTAIN"


# ============================================================================
# 4. DECIDE_LONG / DECIDE_SHORT reach the supervisor (dry-run preview)
# ============================================================================


def test_decide_long_reaches_supervisor_as_dry_run_preview(tmp_path):
    inputs = (make_input("AAPL", promotable_score=0.9),)
    client = FakeLLMClient(proposal_json("x"))
    result = M.run_cycle(
        inputs, max_new_orders_per_cycle=5, decide_kwargs=decide_kwargs(client), strategy_id="S", strategy_version="v1",
        now=NOW, supervision_kwargs=dict(auth_config=alpaca_auth_config(), supervisor_config=sup_open(), **dirs_kwargs(tmp_path, "a")),
    )
    outcome = result.outcomes[0]
    assert outcome.stage == "SUBMITTED_FOR_EXECUTION"
    assert outcome.decision.outcome == "DECIDE_LONG"
    assert outcome.supervision_result["status"] == "READY_FOR_SUBMISSION"
    assert outcome.supervision_result["order_spec"]["symbol"] == "AAPL"
    assert outcome.supervision_result["order_spec"]["side"] == "buy"
    assert outcome.supervision_result["order_spec"]["direction"] == "OPEN_LONG"


def test_decide_short_builds_open_short_direction(tmp_path):
    inputs = (make_input("TSLA", promotable_score=-0.9),)
    client = FakeLLMClient(proposal_json("x"))
    result = M.run_cycle(
        inputs, max_new_orders_per_cycle=5, decide_kwargs=decide_kwargs(client), strategy_id="S", strategy_version="v1",
        now=NOW, supervision_kwargs=dict(auth_config=alpaca_auth_config(), supervisor_config=sup_open(), **dirs_kwargs(tmp_path, "b")),
    )
    outcome = result.outcomes[0]
    assert outcome.stage == "SUBMITTED_FOR_EXECUTION"
    assert outcome.decision.outcome == "DECIDE_SHORT"
    assert outcome.supervision_result["status"] == "READY_FOR_SUBMISSION"


# ============================================================================
# 5. Ranking + per-cycle cap
# ============================================================================


def test_cap_defers_lowest_conviction_candidates(tmp_path):
    inputs = (
        make_input("AAA", promotable_score=0.95),
        make_input("BBB", promotable_score=0.80),
        make_input("CCC", promotable_score=0.30),  # weakest, above decision_threshold but lowest score
    )
    client = FakeLLMClient(proposal_json("x"))
    result = M.run_cycle(
        inputs, max_new_orders_per_cycle=2, decide_kwargs=decide_kwargs(client), strategy_id="S", strategy_version="v1",
        now=NOW, supervision_kwargs=dict(auth_config=alpaca_auth_config(), supervisor_config=sup_open(), **dirs_kwargs(tmp_path, "c")),
    )
    stages = {o.symbol: o.stage for o in result.outcomes}
    assert stages["AAA"] == "SUBMITTED_FOR_EXECUTION"
    assert stages["BBB"] == "SUBMITTED_FOR_EXECUTION"
    assert stages["CCC"] == "DEFERRED_CYCLE_CAP"
    assert result.submitted_count == 2


def test_outcomes_returned_in_original_input_order():
    inputs = (
        make_input("ZZZ", promotable_score=0.9),
        make_input("AAA", already_held=True),
        make_input("MMM", promotable_score=0.05),
    )
    client = FakeLLMClient(proposal_json("x"))
    result = M.run_cycle(
        inputs, max_new_orders_per_cycle=1, decide_kwargs=decide_kwargs(client), strategy_id="S", strategy_version="v1", now=NOW,
    )
    assert [o.symbol for o in result.outcomes] == ["ZZZ", "AAA", "MMM"]


# ============================================================================
# 6. Deterministic repeatability
# ============================================================================


def test_run_cycle_deterministic_same_inputs_same_stages():
    inputs = (make_input("AAA", promotable_score=0.9), make_input("BBB", promotable_score=0.2))
    client_factory = lambda: FakeLLMClient(proposal_json("x"))
    r1 = M.run_cycle(inputs, max_new_orders_per_cycle=5, decide_kwargs=decide_kwargs(client_factory()), strategy_id="S", strategy_version="v1", now=NOW)
    r2 = M.run_cycle(inputs, max_new_orders_per_cycle=5, decide_kwargs=decide_kwargs(client_factory()), strategy_id="S", strategy_version="v1", now=NOW)
    assert [(o.symbol, o.stage) for o in r1.outcomes] == [(o.symbol, o.stage) for o in r2.outcomes]


# ============================================================================
# 7. Fail-closed submission gate
# ============================================================================


def test_attempt_submission_without_enforcement_check_fails_closed():
    inputs = (make_input("AAPL", promotable_score=0.9),)
    client = FakeLLMClient(proposal_json("x"))
    with pytest.raises(M.PaperOrchestrationError):
        M.run_cycle(
            inputs, max_new_orders_per_cycle=5, decide_kwargs=decide_kwargs(client), strategy_id="S", strategy_version="v1",
            now=NOW, attempt_submission=True, enforcement_check_fn=None,
        )


def test_dry_run_cycle_does_not_require_enforcement_check_fn():
    inputs = (make_input("AAPL", promotable_score=0.9),)
    client = FakeLLMClient(proposal_json("x"))
    # attempt_submission defaults to False -- must NOT raise even with no enforcement_check_fn.
    result = M.run_cycle(
        inputs, max_new_orders_per_cycle=5, decide_kwargs=decide_kwargs(client), strategy_id="S", strategy_version="v1", now=NOW,
    )
    assert result.outcomes[0].stage == "SUBMITTED_FOR_EXECUTION"


# ============================================================================
# 8. Portfolio exposure enforcement gate (object and dict verdict shapes)
# ============================================================================


class _Verdict:
    def __init__(self, allowed, reasons=()):
        self.allowed = allowed
        self.reasons = reasons


def test_enforcement_check_blocks_via_object_verdict(tmp_path):
    inputs = (make_input("AAPL", promotable_score=0.9),)
    client = FakeLLMClient(proposal_json("x"))
    alpaca_client = FakeAlpacaClient()

    def blocker(symbol, direction, quantity, decision):
        return _Verdict(allowed=False, reasons=("exceeds per-symbol exposure limit",))

    result = M.run_cycle(
        inputs, max_new_orders_per_cycle=5, decide_kwargs=decide_kwargs(client), strategy_id="S", strategy_version="v1",
        now=NOW, attempt_submission=True, enforcement_check_fn=blocker,
        supervision_kwargs=dict(auth_config=alpaca_auth_config(), supervisor_config=sup_open(), alpaca_client=alpaca_client, **dirs_kwargs(tmp_path, "d")),
    )
    outcome = result.outcomes[0]
    assert outcome.stage == "NO_TRADE_DECIDED"
    assert "exceeds per-symbol exposure limit" in outcome.reasons
    assert outcome.supervision_result is None
    assert alpaca_client.submit_calls == []  # .38 was never reached


def test_enforcement_check_blocks_via_dict_verdict(tmp_path):
    inputs = (make_input("AAPL", promotable_score=0.9),)
    client = FakeLLMClient(proposal_json("x"))
    alpaca_client = FakeAlpacaClient()

    def blocker(symbol, direction, quantity, decision):
        return {"allowed": False, "reasons": ["blocked via dict shape"]}

    result = M.run_cycle(
        inputs, max_new_orders_per_cycle=5, decide_kwargs=decide_kwargs(client), strategy_id="S", strategy_version="v1",
        now=NOW, attempt_submission=True, enforcement_check_fn=blocker,
        supervision_kwargs=dict(auth_config=alpaca_auth_config(), supervisor_config=sup_open(), alpaca_client=alpaca_client, **dirs_kwargs(tmp_path, "e")),
    )
    assert result.outcomes[0].stage == "NO_TRADE_DECIDED"
    assert alpaca_client.submit_calls == []


def test_enforcement_check_allows_real_submission(tmp_path):
    inputs = (make_input("AAPL", promotable_score=0.9),)
    client = FakeLLMClient(proposal_json("x"))
    alpaca_client = FakeAlpacaClient()

    def allower(symbol, direction, quantity, decision):
        return _Verdict(allowed=True)

    result = M.run_cycle(
        inputs, max_new_orders_per_cycle=5, decide_kwargs=decide_kwargs(client), strategy_id="S", strategy_version="v1",
        now=NOW, attempt_submission=True, enforcement_check_fn=allower,
        supervision_kwargs=dict(auth_config=alpaca_auth_config(), supervisor_config=sup_open(), alpaca_client=alpaca_client, **dirs_kwargs(tmp_path, "f")),
    )
    outcome = result.outcomes[0]
    assert outcome.stage == "SUBMITTED_FOR_EXECUTION"
    assert outcome.supervision_result["status"] == "SUBMITTED"
    assert outcome.supervision_result["submission_result"]["broker_order_id"] == "broker-123"
    assert len(alpaca_client.submit_calls) == 1


# ============================================================================
# 9. Input validation / governance
# ============================================================================


def test_rejects_non_positive_max_new_orders_per_cycle():
    with pytest.raises(M.PaperOrchestrationError):
        M.run_cycle((), max_new_orders_per_cycle=0, decide_kwargs={}, strategy_id="S", strategy_version="v1", now=NOW)


def test_rejects_duplicate_symbol_in_cycle_input():
    inputs = (make_input("AAPL"), make_input("AAPL"))
    with pytest.raises(M.PaperOrchestrationError):
        M.run_cycle(inputs, max_new_orders_per_cycle=5, decide_kwargs=decide_kwargs(FakeLLMClient(proposal_json("x"))), strategy_id="S", strategy_version="v1", now=NOW)


def test_symbol_cycle_outcome_rejects_invalid_stage():
    with pytest.raises(M.PaperOrchestrationError):
        M.SymbolCycleOutcome(symbol="AAPL", stage="NOT_A_REAL_STAGE", decision=None, supervision_result=None, reasons=())


def test_cycle_result_submitted_count_property():
    inputs = (make_input("AAA", promotable_score=0.9), make_input("BBB", already_held=True))
    client = FakeLLMClient(proposal_json("x"))
    result = M.run_cycle(inputs, max_new_orders_per_cycle=5, decide_kwargs=decide_kwargs(client), strategy_id="S", strategy_version="v1", now=NOW)
    assert result.submitted_count == 1


# ============================================================================
# 10. Persistence
# ============================================================================


def test_persist_cycle_state_writes_latest_and_archived_files(tmp_path):
    inputs = (make_input("AAA", promotable_score=0.9),)
    client = FakeLLMClient(proposal_json("x"))
    result = M.run_cycle(inputs, max_new_orders_per_cycle=5, decide_kwargs=decide_kwargs(client), strategy_id="S", strategy_version="v1", now=NOW)

    state_dir = tmp_path / "state"
    latest_path = M.persist_cycle_state(result, state_dir=state_dir)
    assert latest_path == state_dir / "latest_cycle.json"
    assert latest_path.exists()
    archived = state_dir / "cycles" / f"{result.cycle_id}.json"
    assert archived.exists()

    payload = json.loads(latest_path.read_text(encoding="utf-8"))
    assert payload["cycle_id"] == result.cycle_id
    assert payload["submitted_count"] == 1
    assert payload["outcomes"][0]["symbol"] == "AAA"
    assert payload["outcomes"][0]["stage"] == "SUBMITTED_FOR_EXECUTION"


def test_persist_cycle_state_overwrites_latest_across_cycles(tmp_path):
    state_dir = tmp_path / "state"
    client = FakeLLMClient(proposal_json("x"))

    r1 = M.run_cycle((make_input("AAA", promotable_score=0.9),), max_new_orders_per_cycle=5, decide_kwargs=decide_kwargs(client), strategy_id="S", strategy_version="v1", now=NOW)
    M.persist_cycle_state(r1, state_dir=state_dir)

    later = datetime(2026, 9, 13, 13, 0, 0, tzinfo=timezone.utc)
    r2 = M.run_cycle((make_input("BBB", promotable_score=0.9),), max_new_orders_per_cycle=5, decide_kwargs=decide_kwargs(FakeLLMClient(proposal_json("y"))), strategy_id="S", strategy_version="v1", now=later)
    M.persist_cycle_state(r2, state_dir=state_dir)

    payload = json.loads((state_dir / "latest_cycle.json").read_text(encoding="utf-8"))
    assert payload["cycle_id"] == r2.cycle_id  # overwritten, reflects the LATEST cycle
    assert (state_dir / "cycles" / f"{r1.cycle_id}.json").exists()  # both archives retained
    assert (state_dir / "cycles" / f"{r2.cycle_id}.json").exists()


# ============================================================================
# 11. Scope invariant -- .53 never builds a CLOSE_* direction
# ============================================================================


def test_module_never_builds_a_close_direction():
    """AST-based, not a raw substring search -- this module's own
    docstring legitimately discusses CLOSE_LONG/CLOSE_SHORT in prose
    (explaining the entries-only scope), which would false-positive a
    literal grep exactly like the known false-positive class this
    project has already fixed in `.49`'s and `.51`'s own test suites.
    This checks only string CONSTANTS used outside docstrings -- i.e.
    actual code, not documentation."""
    import ast

    source = Path(M.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    docstring_ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                docstring_ids.add(id(body[0].value))

    offenders = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstring_ids
        and node.value in ("CLOSE_LONG", "CLOSE_SHORT")
    ]
    assert not offenders, f"unexpected CLOSE_* direction literal(s) in code (not docstring): {offenders}"
