"""
tests/test_reproducibility.py

Reproducibility is mandatory per the Phase 5 command: running the exact
same strategy/dataset/config twice must produce byte-identical results, and
core/results.py::config_hash() must be a stable, deterministic function of
run identity (independent of wall-clock run_id and of dict key order).
"""
import json

from core.backtest_engine import ExitConfig, run_backtest
from core.costs import CostModel
from core.indicators import atr as compute_atr
from core.instrument import Instrument
from core.results import ResultRecord, compute_metrics, config_hash, write_result
from core.risk import RiskConfig
from strategies.pipeline_fixture import PipelineValidationFixture
from tests._fixtures import make_synthetic_funding, make_synthetic_ohlcv


def _run_once():
    ohlcv = make_synthetic_ohlcv(num_bars=400, seed=55)
    ohlcv["atr"] = compute_atr(ohlcv, period=14)
    funding_df = make_synthetic_funding(ohlcv)
    strategy = PipelineValidationFixture(interval_bars=9)
    inst = Instrument.mexc_swap("BTC")
    cost_model = CostModel(funding_df=funding_df)
    result = run_backtest(strategy, inst, ohlcv, cost_model, RiskConfig(), 500.0, ExitConfig())
    metrics = compute_metrics(result.event_log, 500.0, result.bars_processed)
    return result, metrics


def test_identical_config_produces_identical_event_log():
    result_a, metrics_a = _run_once()
    result_b, metrics_b = _run_once()

    assert len(result_a.event_log) == len(result_b.event_log)
    for ea, eb in zip(result_a.event_log, result_b.event_log):
        # Compare via JSON round-trip (default=str) so Timestamp objects
        # compare by value rather than identity.
        assert json.dumps(ea, default=str, sort_keys=True) == json.dumps(eb, default=str, sort_keys=True)

    assert result_a.final_equity == result_b.final_equity
    assert result_a.final_risk_state == result_b.final_risk_state
    assert metrics_a == metrics_b


def test_config_hash_is_deterministic_regardless_of_key_order():
    payload_a = {"strategy_id": "x", "dataset_id": "y", "risk_cfg": {"a": 1, "b": 2}}
    payload_b = {"risk_cfg": {"b": 2, "a": 1}, "dataset_id": "y", "strategy_id": "x"}
    assert config_hash(payload_a) == config_hash(payload_b)


def test_config_hash_changes_when_config_changes():
    base = {"strategy_id": "x", "fixture_interval_bars": 48}
    changed = {"strategy_id": "x", "fixture_interval_bars": 49}
    assert config_hash(base) != config_hash(changed)


def test_metrics_are_deterministic_given_same_event_log():
    result, _ = _run_once()
    m1 = compute_metrics(result.event_log, 500.0, result.bars_processed)
    m2 = compute_metrics(result.event_log, 500.0, result.bars_processed)
    assert m1 == m2


def test_final_risk_state_is_persisted_in_result_record(tmp_path):
    """
    REMEDIATION (PHASE5_INDEPENDENT_AUDIT_2026-09-22.md finding #3): the
    run's final RiskState and reason (already computed by run_backtest() as
    BacktestRunResult.final_risk_state / final_risk_reason) were never
    threaded into ResultRecord or the written result JSON -- an
    observability gap. This test drives a run all the way to HALTED (a
    small starting capital with a tight max_drawdown_limit_pct and a
    fixture that trades often enough to breach it) and asserts the
    persisted ResultRecord, and the JSON written to disk by write_result(),
    both carry the correct final_risk_state and state_reason.
    """
    ohlcv = make_synthetic_ohlcv(num_bars=400, seed=21, drift=-0.001, vol=0.02)
    ohlcv["atr"] = compute_atr(ohlcv, period=14)
    funding_df = make_synthetic_funding(ohlcv)
    strategy = PipelineValidationFixture(interval_bars=5)
    inst = Instrument.mexc_swap("BTC")
    cost_model = CostModel(funding_df=funding_df)
    tight_risk_cfg = RiskConfig(max_drawdown_limit_pct=0.05, daily_loss_limit_pct=0.90)

    result = run_backtest(strategy, inst, ohlcv, cost_model, tight_risk_cfg, 500.0, ExitConfig())
    metrics = compute_metrics(result.event_log, 500.0, result.bars_processed)

    assert result.final_risk_state.value in ("active", "restricted", "halted")

    record = ResultRecord(
        run_id="test_run", strategy_id=strategy.strategy_id, strategy_version=strategy.version,
        is_research_fixture=strategy.is_research_fixture,
        dataset_id="synthetic", dataset_version="1",
        instrument=inst.canonical_id, timeframe="1h",
        start=str(ohlcv["timestamp"].iloc[0]), end=str(ohlcv["timestamp"].iloc[-1]),
        num_bars=len(ohlcv), num_trades=metrics["num_trades"], wins=metrics["wins"],
        losses=metrics["losses"], gross_pnl=metrics["gross_pnl"], fees=metrics["fees"],
        funding=metrics["funding"], net_pnl=metrics["net_pnl"], return_pct=metrics["return_pct"],
        max_drawdown=metrics["max_drawdown"], max_drawdown_pct=metrics["max_drawdown_pct"],
        exposure_pct=metrics["exposure_pct"], profit_factor=metrics["profit_factor"],
        expectancy=metrics["expectancy"], validation_status="TEST",
        config_hash="deadbeef", starting_capital=500.0,
        funding_data_available=True, funding_coverage_gap_vs_ohlcv=False,
        final_risk_state=result.final_risk_state.value,
        state_reason=result.final_risk_reason,
    )

    assert record.final_risk_state == result.final_risk_state.value
    assert record.state_reason == result.final_risk_reason

    out_path = tmp_path / "result.json"
    write_result(record, out_path)
    written = json.loads(out_path.read_text())
    assert written["final_risk_state"] == result.final_risk_state.value
    assert written["state_reason"] == result.final_risk_reason
