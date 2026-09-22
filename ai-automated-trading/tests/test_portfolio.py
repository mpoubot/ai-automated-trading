"""
tests/test_portfolio.py

PortfolioAllocator: fixed equal-weight reservations, and the direct
regression test for the shared-equity-pool bug found earlier in this
engagement (aura_v054_backtest.py, AURA's frozen v0.5.4 backtest engine:
BTC_USDT got 0 trades in an 11-symbol combined run sharing one $100,000
equity pool, vs. 69 trades run alone, because symbols shared one shrinking
pool and earlier symbols in iteration order could starve later ones). The
regression test proves a symbol's allocation, and its resulting trade
count, is IDENTICAL regardless of what other symbols do or what order
symbols are processed in.

CORRECTED ATTRIBUTION (PHASE5_INDEPENDENT_AUDIT_2026-09-22.md finding #2):
an earlier version of this docstring incorrectly attributed the finding to
mexc_bot/run_broad_backtest.py. That file already implements independent
per-symbol equity/circuit-breaker state (verified by direct re-inspection)
and documents this design choice in its own docstring -- it was never the
source of the bug. The true source, per
mexc_bot_V054_MEXC_BASELINE_PHASE2_REPORT_2026-09-19.md, is
aura_v054_backtest.py. mexc_bot itself was never modified; only this
project's own documentation was wrong.

REMEDIATION (PHASE5_INDEPENDENT_AUDIT_2026-09-22.md finding #4, LOW/MEDIUM):
the two tests originally described as "the dedicated shared-equity
regression tests" (`test_symbol_result_is_independent_of_other_symbols_in_the_pool`,
`test_symbol_result_is_independent_of_processing_order`) had a proven blind
spot: mutated to a shared/shrinking pool, they could pass anyway by
mathematical coincidence, because their "alone" and "in-pool" branches each
called `reservation_for()` an EQUAL number of times before comparing (500
total capital / 1 symbol, mutated-and-halved, happened to equal 5500 total
/ 11 symbols, mutated-and-halved -- both landed on 250). Both tests below
were strengthened to call `reservation_for()` a deliberately UNEQUAL,
asymmetric number of times per symbol (interleaved, out of order) before
taking the value actually used, which a call-count-symmetric mutation
cannot satisfy by coincidence. `test_reservation_for_is_stable_across_repeated_and_interleaved_calls`
is the most direct version of this idea: it doesn't compare two allocator
instances at all, it just asserts ONE allocator's `reservation_for()`
returns the identical value on repeat, out-of-order, and heavily
asymmetric-count queries -- which a stateful/draining implementation cannot
do regardless of its exact drain formula. All three were re-verified by
mutation testing (disposable copy, shared/shrinking-pool mutation) to
actually fail when the allocator is replaced by shared/shrinking-pool
behavior -- see the remediation report for the transcript.
"""
import pytest

from core.backtest_engine import ExitConfig, run_backtest
from core.costs import CostModel
from core.indicators import atr as compute_atr
from core.instrument import Instrument
from core.portfolio import PortfolioAllocator
from core.risk import RiskConfig
from strategies.pipeline_fixture import PipelineValidationFixture
from tests._fixtures import make_synthetic_ohlcv


def test_allocator_splits_capital_equally():
    alloc = PortfolioAllocator(total_capital=1100.0, instrument_ids=["A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K"])
    for iid in alloc.instrument_ids:
        assert alloc.reservation_for(iid).allocated_capital == pytest.approx(100.0)


def test_allocator_rejects_zero_or_negative_capital():
    with pytest.raises(ValueError):
        PortfolioAllocator(total_capital=0.0, instrument_ids=["A"])
    with pytest.raises(ValueError):
        PortfolioAllocator(total_capital=-100.0, instrument_ids=["A"])


def test_allocator_rejects_empty_symbol_list():
    with pytest.raises(ValueError):
        PortfolioAllocator(total_capital=100.0, instrument_ids=[])


def test_allocator_rejects_duplicate_symbols():
    with pytest.raises(ValueError):
        PortfolioAllocator(total_capital=100.0, instrument_ids=["A", "A"])


def test_reservation_for_unknown_symbol_raises():
    alloc = PortfolioAllocator(total_capital=100.0, instrument_ids=["A", "B"])
    with pytest.raises(KeyError):
        alloc.reservation_for("Z")


def _run_symbol(instrument_id: str, ohlcv, allocated_capital: float, interval_bars: int = 8):
    inst = Instrument.mexc_swap(instrument_id)
    strategy = PipelineValidationFixture(interval_bars=interval_bars)
    cost_model = CostModel()
    return run_backtest(strategy, inst, ohlcv, cost_model, RiskConfig(), allocated_capital, ExitConfig())


def test_symbol_result_is_independent_of_other_symbols_in_the_pool():
    """The direct regression test for the shared-equity-pool bug: run one
    symbol (a) alone against its fixed reservation, and (b) alongside ten
    other symbols in the SAME allocator -- the trade count and every event
    for that symbol must be identical either way, because each symbol's
    backtest only ever sees its own fixed reservation, never a shared pool.

    Strengthened per finding #4: the pool branch below deliberately queries
    `reservation_for()` a different, larger, and asymmetric number of times
    per symbol (1x for SYM0 up through 11x for SYM10, interleaved out of
    numeric order) before ever taking the value actually used for the
    backtest -- unlike the "alone" branch, which queries it exactly once.
    This asymmetry is deliberate: a shared/shrinking-pool mutation was
    found (by mutation testing) to pass the ORIGINAL version of this test
    anyway, because both branches happened to call `reservation_for()` an
    equal number of times, so a call-count-driven drain landed on the same
    number by coincidence. No such coincidence is possible here."""
    symbol_ids = [f"SYM{i}" for i in range(11)]
    target = "SYM0"

    # Build independent synthetic OHLCV per symbol (different seeds so
    # they're not literally identical series).
    ohlcv_by_symbol = {sid: make_synthetic_ohlcv(num_bars=300, seed=i) for i, sid in enumerate(symbol_ids)}
    for df in ohlcv_by_symbol.values():
        df["atr"] = compute_atr(df, period=14)

    alone_capital = PortfolioAllocator(total_capital=500.0, instrument_ids=[target]).reservation_for(target).allocated_capital
    result_alone = _run_symbol(target, ohlcv_by_symbol[target], alone_capital)

    alloc_pool = PortfolioAllocator(total_capital=500.0 * 11, instrument_ids=symbol_ids)

    # Deliberately asymmetric, interleaved, out-of-order querying: symbol
    # SYMk gets queried (k+1) times each, in reverse numeric order, and
    # `target` (SYM0) itself is queried repeatedly throughout, not just
    # once -- 66 total calls across the pool before any symbol is backtested.
    for sid in reversed(symbol_ids):
        k = int(sid.replace("SYM", ""))
        for _ in range(k + 1):
            alloc_pool.reservation_for(sid)
            alloc_pool.reservation_for(target)

    # Run ALL 11 symbols through the pool, in order, simulating the
    # combined-run scenario that previously starved BTC_USDT.
    pool_results = {}
    for sid in symbol_ids:
        pool_results[sid] = _run_symbol(sid, ohlcv_by_symbol[sid], alloc_pool.reservation_for(sid).allocated_capital)

    result_in_pool = pool_results[target]
    pool_capital = alloc_pool.reservation_for(target).allocated_capital

    # Same per-symbol allocated capital in both scenarios (500 total in the
    # "alone" case with 1 symbol == 500*11 total split 11 ways in the pool
    # case) -- this is what "fixed, non-shrinking allocation" means -- and
    # this must hold true even after 66+ asymmetric queries above.
    assert pool_capital == pytest.approx(alone_capital), (
        f"{target}'s allocated capital drifted to {pool_capital} (expected {alone_capital}) "
        f"after asymmetric repeated queries across the pool -- this is exactly the "
        f"shared/shrinking-pool bug this allocator is meant to prevent."
    )

    assert len(result_alone.event_log) == len(result_in_pool.event_log), (
        f"{target}'s event count changed depending on whether other symbols were present "
        f"({len(result_alone.event_log)} alone vs {len(result_in_pool.event_log)} in pool) "
        f"-- this is exactly the shared-equity-pool bug this allocator is meant to prevent."
    )
    for e_alone, e_pool in zip(result_alone.event_log, result_in_pool.event_log):
        # Full event equality (not just type/timestamp) so size-derived
        # fields (quantity, fee, leverage, gross_pnl) also catch a capital
        # drift that this strategy's timing alone wouldn't reveal (see
        # test_symbol_result_is_independent_of_processing_order docstring).
        assert e_alone == e_pool, f"event differs between alone and in-pool runs: {e_alone} vs {e_pool}"


def test_symbol_result_is_independent_of_processing_order():
    """Same pool, symbols processed in a different order -- results for
    every symbol must be identical regardless of order, since each symbol's
    reservation and backtest are fully independent of the others.

    Strengthened per finding #4: before either pass, `reservation_for()` is
    queried a deliberately unequal number of times per symbol (asymmetric
    warm-up queries), and the forward pass additionally re-queries each
    symbol's reservation an extra, varying number of times interleaved with
    the backtests themselves -- so the forward and reversed passes are not
    just order-reversed, they also exercise very different total call
    counts and call orderings against the SAME allocator instance, which a
    call-count- or order-dependent draining mutation cannot satisfy by
    symmetry the way the original (single-query-per-symbol) version could."""
    symbol_ids = [f"SYM{i}" for i in range(6)]
    ohlcv_by_symbol = {sid: make_synthetic_ohlcv(num_bars=250, seed=100 + i) for i, sid in enumerate(symbol_ids)}
    for df in ohlcv_by_symbol.values():
        df["atr"] = compute_atr(df, period=14)

    alloc = PortfolioAllocator(total_capital=3000.0, instrument_ids=symbol_ids)

    # Asymmetric warm-up: SYM0 queried once, SYM1 twice, ... SYM5 six times,
    # in forward order -- 21 total calls before any backtest runs.
    for i, sid in enumerate(symbol_ids):
        for _ in range(i + 1):
            alloc.reservation_for(sid)

    expected_capital = 3000.0 / len(symbol_ids)
    forward_capitals = {}
    forward_results = {}
    for sid in symbol_ids:
        # Extra, varying re-queries interleaved with each backtest.
        for _ in range(3):
            alloc.reservation_for(sid)
        cap = alloc.reservation_for(sid).allocated_capital
        forward_capitals[sid] = cap
        forward_results[sid] = _run_symbol(sid, ohlcv_by_symbol[sid], cap)

    # A second, differently-shaped asymmetric warm-up before the reversed
    # pass: SYM5 queried once, SYM4 twice, ... SYM0 six times, in reverse
    # order -- deliberately the OPPOSITE weighting from the first warm-up.
    for i, sid in enumerate(reversed(symbol_ids)):
        for _ in range(i + 1):
            alloc.reservation_for(sid)

    reversed_capitals = {}
    reversed_results = {}
    for sid in reversed(symbol_ids):
        for _ in range(5):
            alloc.reservation_for(sid)
        cap = alloc.reservation_for(sid).allocated_capital
        reversed_capitals[sid] = cap
        reversed_results[sid] = _run_symbol(sid, ohlcv_by_symbol[sid], cap)

    for sid in symbol_ids:
        # Capital itself must be identical and equal to the fixed
        # equal-weight value in BOTH passes, despite the very different
        # (and by-now large) number of intervening reservation_for() calls.
        assert forward_capitals[sid] == pytest.approx(expected_capital), (
            f"{sid}: forward-pass allocated capital {forward_capitals[sid]} drifted "
            f"from the fixed value {expected_capital}"
        )
        assert reversed_capitals[sid] == pytest.approx(expected_capital), (
            f"{sid}: reversed-pass allocated capital {reversed_capitals[sid]} drifted "
            f"from the fixed value {expected_capital}"
        )
        assert forward_capitals[sid] == pytest.approx(reversed_capitals[sid]), (
            f"{sid}: allocated capital differs between forward ({forward_capitals[sid]}) "
            f"and reversed ({reversed_capitals[sid]}) processing order."
        )

        fwd_events = forward_results[sid].event_log
        rev_events = reversed_results[sid].event_log
        assert len(fwd_events) == len(rev_events), f"{sid}: event count depends on processing order"
        for e_fwd, e_rev in zip(fwd_events, rev_events):
            # Compare the FULL event, not just type/timestamp: this
            # strategy's entry schedule and ATR-based stop distance are
            # both capital-independent (stop distance is a price offset,
            # entries are bar-count scheduled), so type/timestamp alone can
            # stay identical even when allocated CAPITAL silently drifts --
            # only size-derived fields (quantity, position_size_usdt,
            # leverage, fee, gross_pnl) actually reveal a capital-drift bug.
            # This was found directly: a shared/shrinking-pool mutation
            # passed an earlier, type/timestamp-only version of this
            # comparison even though capital had visibly drifted.
            assert e_fwd == e_rev, (
                f"{sid}: event differs between forward and reversed processing order "
                f"(only type/timestamp were compared in an earlier version of this test, "
                f"which missed capital-derived drift): {e_fwd} vs {e_rev}"
            )


def test_reservation_for_is_stable_across_repeated_and_interleaved_calls():
    """
    The most direct, coincidence-proof regression test for the
    shared-equity-pool bug (finding #4): rather than comparing two
    separately-constructed allocator instances (which a call-count-
    symmetric mutation might coincidentally satisfy), this test queries ONE
    allocator instance's `reservation_for()` a grossly unequal number of
    times per symbol, interleaved and out of order, and asserts a single
    symbol's returned reservation is byte-identical on its very first call,
    after 55 queries for other symbols, and after further interleaved
    repeat queries for itself and others. A stateful, shared, or draining
    implementation cannot satisfy this regardless of its exact drain
    formula, because there is no "alone" branch for it to coincidentally
    match against -- only its own claimed invariant (fixed, non-shrinking,
    query-count-independent allocation) is being checked.
    """
    symbol_ids = [f"SYM{i}" for i in range(11)]
    target = "SYM0"
    alloc = PortfolioAllocator(total_capital=5500.0, instrument_ids=symbol_ids)

    first_call = alloc.reservation_for(target).allocated_capital
    assert first_call == pytest.approx(500.0)

    # Query every OTHER symbol a different number of times each (1x, 2x,
    # ..., 10x for the 10 other symbols == 55 total calls), deliberately
    # unequal to the single call used for `target` so far.
    other_symbols = [s for s in symbol_ids if s != target]
    for k, sid in enumerate(other_symbols, start=1):
        for _ in range(k):
            alloc.reservation_for(sid)

    mid_call = alloc.reservation_for(target).allocated_capital
    assert mid_call == pytest.approx(first_call), (
        f"reservation_for({target!r}) returned {mid_call} after 55 queries for other "
        f"symbols, but {first_call} on the very first call -- allocation drifted, "
        f"which is exactly the shared/shrinking-pool bug this allocator exists to "
        f"prevent."
    )

    # More interleaved calls: repeated queries for target itself and for
    # the two symbols at either end of the id range.
    for _ in range(7):
        alloc.reservation_for(target)
        alloc.reservation_for(other_symbols[0])
        alloc.reservation_for(other_symbols[-1])
        alloc.reservation_for(target)

    last_call = alloc.reservation_for(target).allocated_capital
    assert last_call == pytest.approx(first_call)
    assert last_call == pytest.approx(5500.0 / 11)

    # And every OTHER symbol's own reservation must likewise be unaffected
    # by all of the above querying of `target` and of each other.
    for sid in other_symbols:
        assert alloc.reservation_for(sid).allocated_capital == pytest.approx(5500.0 / 11), (
            f"{sid}'s reservation drifted away from the fixed equal-weight value "
            f"after heavy interleaved querying of other symbols."
        )


def test_no_symbol_disappears_because_another_symbol_traded_heavily():
    """A symbol scheduled to enter frequently and lose heavily must not
    reduce another symbol's allocated capital or trade count -- each
    allocator reservation is independent."""
    symbol_ids = ["HEAVY_LOSER", "QUIET_ONE"]
    heavy_ohlcv = make_synthetic_ohlcv(num_bars=400, seed=200, drift=-0.01, vol=0.02)  # sharp downtrend
    quiet_ohlcv = make_synthetic_ohlcv(num_bars=400, seed=201, drift=0.0002, vol=0.005)
    heavy_ohlcv["atr"] = compute_atr(heavy_ohlcv, period=14)
    quiet_ohlcv["atr"] = compute_atr(quiet_ohlcv, period=14)

    alloc = PortfolioAllocator(total_capital=1000.0, instrument_ids=symbol_ids)

    quiet_alone_capital = PortfolioAllocator(total_capital=500.0, instrument_ids=["QUIET_ONE"]) \
        .reservation_for("QUIET_ONE").allocated_capital
    quiet_alone = _run_symbol("QUIET_ONE", quiet_ohlcv, quiet_alone_capital, interval_bars=5)

    # heavy loser runs first and repeatedly opens/loses/re-enters (interval_bars=3
    # keeps re-triggering entries once each trade closes)
    _heavy_in_pool = _run_symbol("HEAVY_LOSER", heavy_ohlcv, alloc.reservation_for("HEAVY_LOSER").allocated_capital, interval_bars=3)
    quiet_in_pool = _run_symbol("QUIET_ONE", quiet_ohlcv, alloc.reservation_for("QUIET_ONE").allocated_capital, interval_bars=5)

    assert alloc.reservation_for("QUIET_ONE").allocated_capital == pytest.approx(500.0)
    assert len(quiet_in_pool.event_log) == len(quiet_alone.event_log), (
        "QUIET_ONE's trade count changed because HEAVY_LOSER traded heavily -- "
        "capital reservations are supposed to be independent."
    )
