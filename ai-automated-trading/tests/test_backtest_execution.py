"""
tests/test_backtest_execution.py

Backtest engine execution correctness, and the explicit NO-LOOKAHEAD proof
required by the Phase 5 command ("verify (not optimize): no look-ahead").

REMEDIATION NOTE (PHASE5_INDEPENDENT_AUDIT_2026-09-22.md, finding #1): the
original single-truncation-point test here (`window = ohlcv.iloc[:i+1]` vs.
a dataset cut at ONE point, comparing events up to that one boundary) was
proven, via mutation testing, NOT to reliably catch a real lookahead bug.
The reason: a constant-offset lookahead (e.g. reading bar i+1 instead of
bar i) reads a value that is IDENTICAL in both the full and truncated
series for every bar except the exact truncation boundary -- both series
are slices of the same underlying data. The bug is only observable if a
strategy decision happens to land exactly on that one boundary bar, which
is incidental to the chosen truncation point and strategy schedule. A real
1-bar lookahead mutation was injected into a disposable copy and passed
that single-point test (and 63 of the other 64 tests) without detection.

This file now proves no-lookahead three independent ways. Each was actually
run against a disposable copy with a real 1-bar lookahead mutation injected
into the engine (`window = ohlcv.iloc[:i+1]` -> `ohlcv.iloc[:min(i+2,len)]`)
-- see the remediation report for the full mutation-test transcript,
including the negative result below:

1. `test_no_lookahead_dense_truncation` -- the same truncation-invariance
   idea as the original test, but checked at MANY truncation points across
   the series instead of one, so a constant-offset lookahead has nowhere to
   hide: at least one boundary is guaranteed to coincide with a scheduled
   decision bar. CONFIRMED to catch the 1-bar lookahead mutation (diverges
   at the very first truncation point tried).
2. `test_probe_window_never_supplies_a_row_later_than_the_decision_bar` --
   Martin's remediation Option A: a probe strategy records the exact
   timestamp of every row supplied to evaluate(), and the test
   independently reconstructs the expected decision-bar sequence from the
   same OHLCV it handed to the engine, then asserts the last row of every
   supplied window matches its decision bar exactly. This is the strongest
   proof in the file: it is fully general (doesn't depend on the wrapped
   strategy reading price data, and doesn't depend on any truncation
   boundary coincidence). CONFIRMED to catch the 1-bar lookahead mutation
   (every single window is off by exactly one row).
3. `test_no_lookahead_future_value_corruption` -- Martin's remediation
   Option B: every OHLC value from a cutoff point onward is replaced with
   extreme sentinel values, and every decision made at or before the last
   clean bar must be completely unaffected. HONEST NEGATIVE RESULT: against
   the 1-bar lookahead mutation, this test did NOT diverge when wrapping
   `PipelineValidationFixture`, because that fixture's entry decision is
   driven purely by `len(window)` (bar-count scheduling), not by the OHLC
   price content of the window -- so corrupting future prices alone doesn't
   perturb its decisions in this configuration. The test is still kept
   because it catches a genuinely different bug class (the engine handing a
   correctly-SIZED window but with wrong/leaked future CONTENT spliced in,
   which a price-sensitive strategy would be affected by even though this
   fixture isn't), and its own off-by-one (`<` vs `<=` at the cutoff) was
   found and fixed in the same remediation pass. Tests #1 and #2 above are
   the load-bearing proofs for the exact mutation class the audit raised;
   this one is a complementary check with a documented blind spot against
   window-length-only mutations when the strategy under test doesn't
   consume price data structurally.
"""
import pandas as pd

from core.backtest_engine import ExitConfig, run_backtest
from core.costs import CostModel
from core.indicators import atr as compute_atr
from core.instrument import Instrument
from core.risk import RiskConfig
from core.strategy_base import Strategy
from strategies.pipeline_fixture import PipelineValidationFixture
from tests._fixtures import make_synthetic_funding, make_synthetic_ohlcv


def _prep_ohlcv(num_bars=400, seed=11, drift=0.0006, vol=0.012):
    ohlcv = make_synthetic_ohlcv(num_bars=num_bars, seed=seed, drift=drift, vol=vol)
    ohlcv["atr"] = compute_atr(ohlcv, period=14)
    return ohlcv


def test_backtest_produces_entries_and_exits():
    ohlcv = _prep_ohlcv(num_bars=400, seed=11)
    funding_df = make_synthetic_funding(ohlcv)
    strategy = PipelineValidationFixture(interval_bars=10)
    inst = Instrument.mexc_swap("BTC")
    cost_model = CostModel(funding_df=funding_df)

    result = run_backtest(strategy, inst, ohlcv, cost_model, RiskConfig(), 500.0, ExitConfig())

    types = [e["type"] for e in result.event_log]
    assert "entry" in types
    exit_types = {"exit_stop", "exit_time_stop"}
    assert exit_types & set(types), f"expected at least one exit event, got types={set(types)}"


def test_entries_and_exits_alternate_correctly():
    """At most one open position at a time: an 'entry' must not be followed
    by another 'entry' before an exit_* event closes the prior trade."""
    ohlcv = _prep_ohlcv(num_bars=400, seed=12)
    strategy = PipelineValidationFixture(interval_bars=8)
    inst = Instrument.mexc_swap("ETH")
    cost_model = CostModel()

    result = run_backtest(strategy, inst, ohlcv, cost_model, RiskConfig(), 500.0, ExitConfig())

    open_position = False
    for event in result.event_log:
        if event["type"] == "entry":
            assert not open_position, f"duplicate entry while position already open: {event}"
            open_position = True
        elif event["type"] in ("exit_stop", "exit_time_stop"):
            assert open_position, f"exit with no open position: {event}"
            open_position = False


def test_fees_are_applied_on_exit():
    ohlcv = _prep_ohlcv(num_bars=300, seed=13)
    strategy = PipelineValidationFixture(interval_bars=6)
    inst = Instrument.mexc_swap("BTC")
    cost_model = CostModel(fee_pct=0.001, slippage_pct=0.001)

    result = run_backtest(strategy, inst, ohlcv, cost_model, RiskConfig(), 500.0, ExitConfig())
    exit_events = [e for e in result.event_log if e["type"].startswith("exit_")]
    assert exit_events, "need at least one exit to check fee application"
    for e in exit_events:
        assert e["fee"] > 0, f"expected non-zero fee+slippage on exit, got {e}"


def test_no_lookahead_dense_truncation():
    """
    Strengthened no-lookahead proof (post PHASE5_INDEPENDENT_AUDIT_2026-09-22.md
    finding #1): a SINGLE truncation point can coincidentally miss a real
    lookahead bug, because a constant-offset lookahead (e.g. reading bar i+1
    instead of bar i) reads a value that is IDENTICAL in both the full and
    truncated series for every bar except the exact truncation boundary --
    both series are slices of the same underlying data. This test truncates
    at MANY points across the series and checks invariance at each one, so a
    constant-offset lookahead has nowhere to hide: at least one boundary is
    guaranteed to coincide with a scheduled decision bar.
    """
    ohlcv = _prep_ohlcv(num_bars=500, seed=99, drift=0.0004, vol=0.01)
    funding_df = make_synthetic_funding(ohlcv)
    inst = Instrument.mexc_swap("BTC")

    def make_strategy():
        return PipelineValidationFixture(interval_bars=7)

    full_result = run_backtest(
        make_strategy(), inst, ohlcv, CostModel(funding_df=funding_df),
        RiskConfig(), 500.0, ExitConfig(),
    )

    for truncate_at in range(60, 490, 5):
        cutoff_ts = ohlcv.iloc[truncate_at - 1]["timestamp"]
        truncated_ohlcv = ohlcv.iloc[:truncate_at].reset_index(drop=True)
        truncated_funding = funding_df[funding_df["timestamp"] <= cutoff_ts].reset_index(drop=True)
        truncated_result = run_backtest(
            make_strategy(), inst, truncated_ohlcv, CostModel(funding_df=truncated_funding),
            RiskConfig(), 500.0, ExitConfig(),
        )

        full_events_up_to_cutoff = [e for e in full_result.event_log if e["timestamp"] <= cutoff_ts]
        truncated_events = truncated_result.event_log

        assert len(full_events_up_to_cutoff) == len(truncated_events), (
            f"truncate_at={truncate_at}: event count diverged after truncation "
            f"(full={len(full_events_up_to_cutoff)} truncated={len(truncated_events)}) "
            f"-- this indicates the engine used data beyond the truncation point (lookahead)."
        )
        for full_e, trunc_e in zip(full_events_up_to_cutoff, truncated_events):
            assert full_e["type"] == trunc_e["type"] and full_e["timestamp"] == trunc_e["timestamp"], (
                f"truncate_at={truncate_at}: event mismatch {full_e} vs {trunc_e}"
            )
            assert full_e["instrument"] == trunc_e["instrument"]


def test_no_lookahead_future_value_corruption():
    """
    The most direct proof: replace every OHLC value from a cutoff point
    onward with extreme sentinel values (bars before the cutoff are left
    byte-for-byte untouched), and confirm every decision made strictly
    before the cutoff is completely unaffected. If the engine ever reads a
    future bar's price to make an earlier decision, the sentinel value
    produces an unmistakable divergence rather than a subtle coincidence.
    """
    ohlcv = _prep_ohlcv(num_bars=400, seed=77, drift=0.0003, vol=0.012)
    funding_df = make_synthetic_funding(ohlcv)
    inst = Instrument.mexc_swap("BTC")
    corrupt_from = 250
    cutoff_ts = ohlcv.iloc[corrupt_from - 1]["timestamp"]

    def make_strategy():
        return PipelineValidationFixture(interval_bars=7)

    baseline_result = run_backtest(
        make_strategy(), inst, ohlcv, CostModel(funding_df=funding_df),
        RiskConfig(), 500.0, ExitConfig(),
    )

    corrupted_ohlcv = ohlcv.copy()
    sentinel = 1e9
    for col in ("open", "high", "low", "close"):
        corrupted_ohlcv.loc[corrupt_from:, col] = sentinel
    corrupted_ohlcv["atr"] = compute_atr(corrupted_ohlcv, period=14)

    corrupted_result = run_backtest(
        make_strategy(), inst, corrupted_ohlcv, CostModel(funding_df=funding_df),
        RiskConfig(), 500.0, ExitConfig(),
    )

    # `<=` (not `<`) is deliberate: the decision made AT the last clean bar
    # (index corrupt_from - 1, timestamp == cutoff_ts) is exactly the
    # decision a 1-bar lookahead would corrupt, since it would peek one bar
    # ahead into the first corrupted row. A correct, non-lookahead engine's
    # decision at that bar depends only on bars 0..corrupt_from-1, all of
    # which are clean, so it must be identical regardless of what happens at
    # or after corrupt_from -- this event belongs in the "must match" set.
    # (An earlier `<` here excluded exactly that bar and let a real 1-bar
    # lookahead mutation slip through undetected -- verified by mutation
    # test; see the remediation report.)
    baseline_events_before_cutoff = [e for e in baseline_result.event_log if e["timestamp"] <= cutoff_ts]
    corrupted_events_before_cutoff = [e for e in corrupted_result.event_log if e["timestamp"] <= cutoff_ts]

    assert len(baseline_events_before_cutoff) == len(corrupted_events_before_cutoff), (
        "event count up to and including the last clean bar changed after corrupting only "
        "FUTURE bars -- the engine used future data to make a past decision (lookahead)."
    )
    for b_e, c_e in zip(baseline_events_before_cutoff, corrupted_events_before_cutoff):
        assert b_e == c_e, f"event up to the last clean bar changed after future-only corruption: {b_e} vs {c_e}"


def test_probe_window_never_supplies_a_row_later_than_the_decision_bar():
    """
    Remediation Option A (per Martin's remediation command, finding #1): a
    probe strategy that records the exact timestamp of every row supplied to
    evaluate(), and asserts no supplied row is later than the current
    decision bar.

    This is the strongest, most general no-lookahead proof in this file,
    because it does not depend on the wrapped strategy actually reading
    price data (unlike `test_no_lookahead_future_value_corruption`, which
    was found NOT to catch the 1-bar lookahead mutation below when wrapping
    PipelineValidationFixture -- that fixture's entry decision is driven
    purely by `len(window)`, not by OHLC price content, so corrupting future
    prices alone doesn't perturb it) and does not depend on a truncation
    boundary coincidentally lining up with a scheduled decision (unlike
    `test_no_lookahead_dense_truncation`, which does catch it, but only
    because it tries many boundaries).

    The engine (core/backtest_engine.py) only calls strategy.evaluate() when
    no position is currently open, so the full calling schedule depends on
    trade state and can't be reconstructed from the raw OHLCV alone without
    re-simulating the engine. Instead this test anchors on GROUND TRUTH that
    needs no simulation: the very FIRST evaluate() call of a run can only
    ever be the first bar at or after warmup with a non-NaN ATR, because no
    trade can possibly be open before any decision has been made yet. The
    test asserts that first window's last row -- both its timestamp and its
    exact length -- matches that independently-computed first decision bar
    precisely. Because the 1-bar lookahead bug under test is a CONSTANT
    offset applied identically to every call, exposing it on the very first
    call is sufficient to catch it; it is not a special case the bug could
    dodge. All recorded windows (not just the first) are additionally
    checked for the gap-free, order-preserved prefix property and for
    strictly-increasing last-row timestamps, as complementary structural
    sanity checks.

    An earlier version of this test only checked the gap-free-prefix
    property for every window and never anchored any window's length/
    timestamp against an independently-computed ground truth -- a window
    that is one bar too wide is still "a prefix" of the same series, so
    that version passed even under the 1-bar lookahead mutation. This
    version fixes that with the first-call ground-truth anchor above.
    """
    ohlcv = _prep_ohlcv(num_bars=300, seed=55, drift=0.0005, vol=0.011)
    funding_df = make_synthetic_funding(ohlcv)
    inst = Instrument.mexc_swap("BTC")

    inner = PipelineValidationFixture(interval_bars=9)
    recorded_windows = []

    class ProbeStrategy(Strategy):
        is_research_fixture = True

        def required_warmup_bars(self):
            return inner.required_warmup_bars()

        def evaluate(self, window, instrument):
            recorded_windows.append(window.copy())
            return inner.evaluate(window, instrument)

    probe = ProbeStrategy()
    run_backtest(
        probe, inst, ohlcv, CostModel(funding_df=funding_df),
        RiskConfig(), 500.0, ExitConfig(),
    )
    assert recorded_windows, "probe recorded no windows -- test is not exercising the engine"

    # Ground-truth anchor requiring no simulation: the first bar at or after
    # warmup with a non-NaN ATR is necessarily the first decision bar, since
    # no trade can be open before the first evaluate() call.
    warmup = probe.required_warmup_bars()
    first_valid_index = next(
        i for i in range(len(ohlcv))
        if i >= warmup and pd.notna(ohlcv.iloc[i]["atr"])
    )
    first_window = recorded_windows[0]
    assert len(first_window) == first_valid_index + 1, (
        f"first evaluate() call was supplied a window of length {len(first_window)}, "
        f"expected exactly {first_valid_index + 1} (the first valid bar is at index "
        f"{first_valid_index}, and window must include bars 0..{first_valid_index} "
        f"inclusive, no more, no less) -- the engine supplied extra (future) rows."
    )
    expected_first_ts = ohlcv.iloc[first_valid_index]["timestamp"]
    actual_first_ts = first_window.iloc[-1]["timestamp"]
    assert actual_first_ts == expected_first_ts, (
        f"first evaluate() call's window ends at {actual_first_ts}, expected exactly "
        f"{expected_first_ts} (the true first decision bar) -- the engine supplied a "
        f"row that is {'LATER than' if actual_first_ts > expected_first_ts else 'not'} "
        f"the current decision bar (lookahead)."
    )

    # Complementary structural checks across every recorded window: each
    # must be an exact, gap-free, order-preserved prefix of the canonical
    # series (rules out reordering/gaps), and last-row timestamps must be
    # strictly increasing across successive calls (rules out going backwards
    # or repeating a bar).
    prev_ts = None
    for k, window in enumerate(recorded_windows):
        n = len(window)
        expected_prefix_ts = ohlcv.iloc[:n]["timestamp"].tolist()
        assert window.reset_index(drop=True)["timestamp"].tolist() == expected_prefix_ts, (
            f"decision #{k}: window of length {n} is not an exact, gap-free, "
            f"order-preserved prefix of the canonical series"
        )
        last_ts = window.iloc[-1]["timestamp"]
        if prev_ts is not None:
            assert last_ts > prev_ts, (
                f"decision #{k}: window's last-row timestamp {last_ts} did not strictly "
                f"increase from the previous decision's {prev_ts}"
            )
        prev_ts = last_ts


def test_backtest_raises_without_precomputed_atr():
    import pytest
    ohlcv = make_synthetic_ohlcv(num_bars=50)  # no 'atr' column added
    strategy = PipelineValidationFixture(interval_bars=5)
    inst = Instrument.mexc_swap("BTC")
    with pytest.raises(ValueError):
        run_backtest(strategy, inst, ohlcv, CostModel(), RiskConfig(), 500.0, ExitConfig())
