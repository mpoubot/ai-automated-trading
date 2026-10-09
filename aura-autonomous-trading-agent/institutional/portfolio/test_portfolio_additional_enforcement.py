#!/usr/bin/env python3
"""
Tests for portfolio/{macro_buckets, validation_throttle, greeks_limits,
portfolio_additional_enforcement}.py.

Builds fabricated `PortfolioSnapshot`/`PositionRecord` objects using the
REAL `.343` classes (loaded, not mocked, from the staged source file) to
test real interop -- including real composition with `.368`'s REAL
`combine_enforcement_check_fns()`.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import macro_buckets
import validation_throttle
import greeks_limits
import portfolio_additional_enforcement as pae

OBS = macro_buckets.OBS
PositionRecord = macro_buckets.PositionRecord
PortfolioSnapshot = macro_buckets.PortfolioSnapshot
DimensionVerdict = macro_buckets.DimensionVerdict
EnforcementDecision = macro_buckets.EnforcementDecision
PASS = macro_buckets.PASS
BLOCK = macro_buckets.BLOCK
LIMIT_NOT_CONFIGURED = macro_buckets.LIMIT_NOT_CONFIGURED
NOT_COMPUTABLE = macro_buckets.NOT_COMPUTABLE
CORE_DIR = macro_buckets._CORE_DIR


def _load_module_from_file(module_name: str, file_path: Path):
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# .368 loaded directly (real, not via this package) to test real composition.
GATE_368 = _load_module_from_file(
    "aura_v05368_earnings_blackout_gate", CORE_DIR / "aura_v05368_earnings_blackout_gate.py",
)
combine_enforcement_check_fns = GATE_368.combine_enforcement_check_fns


# ------------------------------------------------------------------------
# Fixture helpers.
# ------------------------------------------------------------------------

def make_position(**kwargs) -> PositionRecord:
    defaults = dict(
        venue="MEXC", symbol="BTC/USDT:USDT", direction="LONG", quantity=1.0, entry_price=50000.0,
        leverage=1.0, mark_price=50000.0, notional_usd=50000.0, notional_basis="MARK_TO_MARKET",
        unrealized_pnl_usd=0.0, liquidation_price=None, raw_source_id=None,
        as_of="2026-10-09T12:00:00+00:00", option_detail=None, structure_group_id=None,
    )
    defaults.update(kwargs)
    return PositionRecord(**defaults)


def _venue_status(venue: str, positions_count: int, equity: float) -> "OBS.VenueFetchStatus":
    return OBS.VenueFetchStatus(
        venue=venue, status="SUCCESS", error=None, fetched_at="2026-10-09T12:00:00+00:00",
        positions_count=positions_count, equity=equity,
    )


def make_snapshot(positions: list[PositionRecord], *, mexc_equity: float = 10000.0, alpaca_equity: float = 10000.0) -> PortfolioSnapshot:
    vfs = {
        "MEXC": _venue_status("MEXC", len([p for p in positions if p.venue == "MEXC"]), mexc_equity),
        "ALPACA": _venue_status("ALPACA", len([p for p in positions if p.venue == "ALPACA"]), alpaca_equity),
    }
    return OBS._build_snapshot(as_of="2026-10-09T12:00:00+00:00", positions=positions, venue_fetch_status=vfs)


# ------------------------------------------------------------------------
# macro_buckets.py
# ------------------------------------------------------------------------

def test_macro_bucket_aggregation_crosses_mexc_and_alpaca():
    positions = [
        make_position(venue="MEXC", symbol="BTC/USDT:USDT", direction="LONG", notional_usd=6000.0),
        make_position(venue="ALPACA", symbol="SPY", direction="LONG", quantity=10, entry_price=400.0,
                      mark_price=400.0, notional_usd=4000.0),
    ]
    snapshot = make_snapshot(positions)
    config = macro_buckets.MacroBucketConfig(
        bucket_membership={"RISK_SENTIMENT_BETA": {"BTC/USDT:USDT": 1.0, "SPY": 0.7}},
    )
    result = macro_buckets.compute_bucket_exposure(snapshot, config, account_equity_usd=20000.0)
    bucket = result["RISK_SENTIMENT_BETA"]
    assert bucket["net_notional_usd"] == pytest.approx(6000.0 + 4000.0 * 0.7)
    assert bucket["gross_notional_usd"] == pytest.approx(6000.0 + 2800.0)
    assert bucket["exposure_ratio"] == pytest.approx((6000.0 + 2800.0) / 20000.0)


def test_macro_bucket_dimension_short_leg_offsets_net_but_not_gross():
    positions = [
        make_position(venue="MEXC", symbol="BTC/USDT:USDT", direction="LONG", notional_usd=5000.0),
        make_position(venue="MEXC", symbol="ETH/USDT:USDT", direction="SHORT", notional_usd=5000.0),
    ]
    snapshot = make_snapshot(positions)
    config = macro_buckets.MacroBucketConfig(
        bucket_membership={"RISK_SENTIMENT_BETA": {"BTC/USDT:USDT": 1.0, "ETH/USDT:USDT": 1.0}},
    )
    result = macro_buckets.compute_bucket_exposure(snapshot, config, account_equity_usd=10000.0)
    bucket = result["RISK_SENTIMENT_BETA"]
    assert bucket["net_notional_usd"] == pytest.approx(0.0)
    assert bucket["gross_notional_usd"] == pytest.approx(10000.0)


def test_macro_bucket_dimension_not_configured_then_pass_then_block():
    # BTC long $5000 / $10000 equity -> long_stacking_ratio = 0.5, well
    # over the hard 0.15 stacking threshold -- so the stacking verdicts
    # (always present, unconditionally, regardless of
    # max_bucket_exposure_ratio -- see module docstring) BLOCK on the
    # LONG side in every one of the three cases below, independent of
    # whatever `max_bucket_exposure_ratio` is set to.
    positions = [make_position(venue="MEXC", symbol="BTC/USDT:USDT", notional_usd=5000.0)]
    snapshot = make_snapshot(positions)
    membership = {"RISK_SENTIMENT_BETA": {"BTC/USDT:USDT": 1.0}}

    def _by_dimension(verdicts):
        exposure = next(v for v in verdicts if v.dimension == "macro_bucket_exposure")
        stacking = {v.evidence["direction"]: v for v in verdicts if v.dimension == "macro_bucket_same_direction_stacking"}
        return exposure, stacking

    unset = macro_buckets.MacroBucketConfig(bucket_membership=membership)
    verdicts = macro_buckets.evaluate_macro_bucket_dimension(snapshot, unset, account_equity_usd=10000.0)
    assert len(verdicts) == 3
    exposure, stacking = _by_dimension(verdicts)
    assert exposure.verdict == LIMIT_NOT_CONFIGURED
    assert stacking["LONG"].verdict == BLOCK and stacking["LONG"].reason == "LIMIT_BREACHED"
    assert stacking["SHORT"].verdict == PASS

    passing = macro_buckets.MacroBucketConfig(bucket_membership=membership, max_bucket_exposure_ratio=0.9)
    verdicts = macro_buckets.evaluate_macro_bucket_dimension(snapshot, passing, account_equity_usd=10000.0)
    exposure, stacking = _by_dimension(verdicts)
    assert exposure.verdict == PASS
    assert stacking["LONG"].verdict == BLOCK  # stacking is hard/unconditional -- still blocks here

    blocking = macro_buckets.MacroBucketConfig(bucket_membership=membership, max_bucket_exposure_ratio=0.1)
    verdicts = macro_buckets.evaluate_macro_bucket_dimension(snapshot, blocking, account_equity_usd=10000.0)
    exposure, stacking = _by_dimension(verdicts)
    assert exposure.verdict == BLOCK
    assert exposure.reason == "LIMIT_BREACHED"


def test_macro_bucket_exposure_ratio_uses_gross_not_net():
    # Two offsetting positions (long BTC, short ETH, same weight/bucket,
    # equal size) net to ~$0 but gross to the full $10000 -- per Martin's
    # explicit directive (2026-10-09), exposure_ratio must be gross-based
    # so this does NOT read as "no exposure".
    positions = [
        make_position(venue="MEXC", symbol="BTC/USDT:USDT", direction="LONG", notional_usd=5000.0),
        make_position(venue="MEXC", symbol="ETH/USDT:USDT", direction="SHORT", notional_usd=5000.0),
    ]
    snapshot = make_snapshot(positions)
    config = macro_buckets.MacroBucketConfig(
        bucket_membership={"RISK_SENTIMENT_BETA": {"BTC/USDT:USDT": 1.0, "ETH/USDT:USDT": 1.0}},
        max_bucket_exposure_ratio=0.5,
    )
    result = macro_buckets.compute_bucket_exposure(snapshot, config, account_equity_usd=10000.0)
    bucket = result["RISK_SENTIMENT_BETA"]
    assert bucket["net_notional_usd"] == pytest.approx(0.0)
    assert bucket["exposure_ratio"] == pytest.approx(1.0)  # gross($10000)/equity($10000), not net($0)/equity

    verdicts = macro_buckets.evaluate_macro_bucket_dimension(snapshot, config, account_equity_usd=10000.0)
    exposure = next(v for v in verdicts if v.dimension == "macro_bucket_exposure")
    assert exposure.verdict == BLOCK  # would have wrongly PASSed under the old net-based formula


def test_macro_bucket_same_direction_stacking_hard_threshold():
    assert macro_buckets.SAME_DIRECTION_STACKING_HARD_THRESHOLD == 0.15

    # $1400 long BTC / $10000 equity = 0.14 -> under the 0.15 hard floor.
    under = macro_buckets.MacroBucketConfig(bucket_membership={"RISK_SENTIMENT_BETA": {"BTC/USDT:USDT": 1.0}})
    snapshot = make_snapshot([make_position(venue="MEXC", symbol="BTC/USDT:USDT", notional_usd=1400.0)])
    result = macro_buckets.compute_bucket_exposure(snapshot, under, account_equity_usd=10000.0)
    assert result["RISK_SENTIMENT_BETA"]["long_stacking_ratio"] == pytest.approx(0.14)
    verdicts = macro_buckets.evaluate_macro_bucket_dimension(snapshot, under, account_equity_usd=10000.0)
    long_stack = next(v for v in verdicts if v.dimension == "macro_bucket_same_direction_stacking" and v.evidence["direction"] == "LONG")
    assert long_stack.verdict == PASS

    # $1600 long BTC / $10000 equity = 0.16 -> over the 0.15 hard floor.
    over_snapshot = make_snapshot([make_position(venue="MEXC", symbol="BTC/USDT:USDT", notional_usd=1600.0)])
    verdicts = macro_buckets.evaluate_macro_bucket_dimension(snapshot=over_snapshot, config=under, account_equity_usd=10000.0)
    long_stack = next(v for v in verdicts if v.dimension == "macro_bucket_same_direction_stacking" and v.evidence["direction"] == "LONG")
    assert long_stack.verdict == BLOCK
    assert long_stack.reason == "LIMIT_BREACHED"
    assert long_stack.evidence["threshold"] == 0.15


def test_macro_bucket_stacking_is_per_direction_not_net():
    # $5000 long BTC + $5000 short ETH, same bucket, $10000 equity:
    # long_stacking_ratio = 0.5 (blocks LONG entries), short_stacking_ratio
    # = 0.5 (blocks SHORT entries) -- both directions independently over
    # threshold even though net exposure is ~$0, proving this is NOT a
    # net-based check.
    positions = [
        make_position(venue="MEXC", symbol="BTC/USDT:USDT", direction="LONG", notional_usd=5000.0),
        make_position(venue="MEXC", symbol="ETH/USDT:USDT", direction="SHORT", notional_usd=5000.0),
    ]
    snapshot = make_snapshot(positions)
    config = macro_buckets.MacroBucketConfig(
        bucket_membership={"RISK_SENTIMENT_BETA": {"BTC/USDT:USDT": 1.0, "ETH/USDT:USDT": 1.0}},
    )
    result = macro_buckets.compute_bucket_exposure(snapshot, config, account_equity_usd=10000.0)
    bucket = result["RISK_SENTIMENT_BETA"]
    assert bucket["long_stacking_ratio"] == pytest.approx(0.5)
    assert bucket["short_stacking_ratio"] == pytest.approx(0.5)


def test_evaluate_stacking_pretrade_direction_blocks_new_long_when_bucket_already_stacked():
    # Existing book already breaches the 0.15 LONG stacking floor for
    # RISK_SENTIMENT_BETA -- a NEW candidate OPEN_LONG order in ANY
    # symbol belonging to that bucket must be hard-blocked, even a tiny
    # one, because this check reflects the bucket's CURRENT state, not a
    # projection of the new order's own size.
    positions = [make_position(venue="MEXC", symbol="BTC/USDT:USDT", notional_usd=2000.0)]  # 0.20 > 0.15
    snapshot = make_snapshot(positions)
    config = macro_buckets.MacroBucketConfig(
        bucket_membership={"RISK_SENTIMENT_BETA": {"BTC/USDT:USDT": 1.0, "ETH/USDT:USDT": 1.0}},
    )

    verdicts = macro_buckets.evaluate_stacking_pretrade_direction(
        snapshot, config, "ETH/USDT:USDT", "OPEN_LONG", account_equity_usd=10000.0,
    )
    assert len(verdicts) == 1
    assert verdicts[0].verdict == BLOCK
    assert verdicts[0].evidence["symbol"] == "ETH/USDT:USDT"
    assert verdicts[0].evidence["direction"] == "LONG"

    # A new OPEN_SHORT in the same bucket is unaffected -- short_stacking_ratio is 0.0.
    short_verdicts = macro_buckets.evaluate_stacking_pretrade_direction(
        snapshot, config, "ETH/USDT:USDT", "OPEN_SHORT", account_equity_usd=10000.0,
    )
    assert short_verdicts[0].verdict == PASS

    # Exits are never gated by this check.
    assert macro_buckets.evaluate_stacking_pretrade_direction(
        snapshot, config, "ETH/USDT:USDT", "CLOSE_LONG", account_equity_usd=10000.0,
    ) == ()

    # A symbol in no configured bucket produces no verdicts at all.
    assert macro_buckets.evaluate_stacking_pretrade_direction(
        snapshot, config, "AAPL", "OPEN_LONG", account_equity_usd=10000.0,
    ) == ()


def test_macro_bucket_dimension_not_computable_when_equity_missing():
    positions = [make_position(venue="MEXC", symbol="BTC/USDT:USDT", notional_usd=5000.0)]
    snapshot = make_snapshot(positions)
    config = macro_buckets.MacroBucketConfig(
        bucket_membership={"RISK_SENTIMENT_BETA": {"BTC/USDT:USDT": 1.0}}, max_bucket_exposure_ratio=0.3,
    )
    verdicts = macro_buckets.evaluate_macro_bucket_dimension(snapshot, config, account_equity_usd=0.0)
    assert verdicts[0].verdict == NOT_COMPUTABLE


def test_proposed_macro_bucket_config_covers_all_11_track_a_pairs():
    assert len(macro_buckets.TRACK_A_CRYPTO_PAIRS) == 11
    membership = macro_buckets.PROPOSED_MACRO_BUCKET_CONFIG.bucket_membership["RISK_SENTIMENT_BETA"]
    for pair in macro_buckets.TRACK_A_CRYPTO_PAIRS:
        assert membership[pair] == 1.0
    assert macro_buckets.PROPOSED_MACRO_BUCKET_CONFIG.max_bucket_exposure_ratio == 0.30


# ------------------------------------------------------------------------
# greeks_limits.py
# ------------------------------------------------------------------------

def test_greeks_dollarization_matches_martins_exact_formulas():
    # Dollar Gamma = 0.5 * Position Gamma * (0.01 * Spot Price)^2 * 100
    # Dollar Vega  = Position Vega * 0.01 * Spot Price * 100
    # One long leg, spot ("Spot Price" == mark_price) = 400.0, multiplier
    # = 100 (the literal "* 100" in both formulas), gamma=0.02, vega=0.30.
    leg = make_position(
        venue="ALPACA", symbol="SPY_CALL_410", direction="LONG", quantity=3, mark_price=400.0,
        option_detail={"strike": 410, "expiry": "2026-12-18", "right": "CALL", "delta": 0.4, "gamma": 0.02, "vega": 0.30, "multiplier": 100},
    )
    snapshot = make_snapshot([leg])
    agg = greeks_limits.aggregate_portfolio_greeks(snapshot)

    expected_gamma = 0.5 * 0.02 * (0.01 * 400.0) ** 2 * 3 * 100
    expected_vega = 0.30 * 0.01 * 400.0 * 3 * 100
    assert agg["gamma_impact_usd_per_1pct_move"] == pytest.approx(expected_gamma)
    assert agg["vega_usd_per_vol_point"] == pytest.approx(expected_vega)
    assert agg["status"] == "COMPUTABLE"


def test_greeks_vega_now_requires_mark_price_per_updated_formula():
    # Since Dollar Vega now has a Spot Price term, a leg with a valid
    # vega but no usable mark_price can no longer be dollarized -- this
    # is a disclosed behavior change from the pre-2026-10-09 formula.
    leg = make_position(
        venue="ALPACA", symbol="SPY_CALL_410", direction="LONG", quantity=1, mark_price=None,
        option_detail={"strike": 410, "expiry": "2026-12-18", "right": "CALL", "delta": 0.4, "gamma": 0.02, "vega": 0.30, "multiplier": 100},
    )
    snapshot = make_snapshot([leg])
    agg = greeks_limits.aggregate_portfolio_greeks(snapshot)
    assert "ALPACA:SPY_CALL_410" in agg["vega_legs_missing_vega"]
    assert agg["vega_usd_per_vol_point"] is None


def test_greeks_aggregation_empty_book_is_computable_zero():
    snapshot = make_snapshot([])
    agg = greeks_limits.aggregate_portfolio_greeks(snapshot)
    assert agg["status"] == "COMPUTABLE"
    assert agg["net_delta_usd"] == 0.0
    assert agg["legs_included"] == 0


def test_greeks_aggregation_partial_status_missing_gamma_vega():
    leg1 = make_position(
        venue="ALPACA", symbol="SPY_CALL_410", direction="LONG", quantity=1, mark_price=400.0,
        option_detail={"strike": 410, "expiry": "2026-12-18", "right": "CALL", "delta": 0.4, "gamma": 0.01, "vega": 0.2, "multiplier": 100},
    )
    leg2 = make_position(
        venue="ALPACA", symbol="SPY_CALL_420", direction="LONG", quantity=1, mark_price=400.0,
        option_detail={"strike": 420, "expiry": "2026-12-18", "right": "CALL", "delta": 0.3},  # no gamma/vega
    )
    snapshot = make_snapshot([leg1, leg2])
    agg = greeks_limits.aggregate_portfolio_greeks(snapshot)
    assert agg["status"] == "PARTIAL"
    assert agg["legs_included"] == 2
    assert agg["legs_excluded_missing_data"] == []
    assert "ALPACA:SPY_CALL_420" in agg["gamma_legs_missing_gamma"]
    assert "ALPACA:SPY_CALL_420" in agg["vega_legs_missing_vega"]
    assert agg["net_delta_usd"] is not None
    assert agg["gamma_impact_usd_per_1pct_move"] is not None  # leg1 alone still contributes


def test_greeks_aggregation_not_computable_when_all_legs_missing_delta():
    leg = make_position(
        venue="ALPACA", symbol="SPY_CALL_410", direction="LONG", quantity=1, mark_price=400.0,
        option_detail={"strike": 410, "expiry": "2026-12-18", "right": "CALL"},  # no delta at all
    )
    snapshot = make_snapshot([leg])
    agg = greeks_limits.aggregate_portfolio_greeks(snapshot)
    assert agg["status"] == "NOT_COMPUTABLE"
    assert agg["net_delta_usd"] is None
    assert "ALPACA:SPY_CALL_410" in agg["legs_excluded_missing_data"]


def test_greeks_dimension_limit_not_configured_vs_pass():
    # Full detail (delta + gamma + vega all present) so all three
    # dimensions have a real value to compare against a limit, not just
    # delta -- isolates "limit not configured" from "greek not available".
    leg = make_position(
        venue="ALPACA", symbol="SPY_CALL_410", direction="LONG", quantity=1, mark_price=400.0,
        option_detail={"strike": 410, "expiry": "2026-12-18", "right": "CALL", "delta": 0.1, "gamma": 0.01, "vega": 0.05, "multiplier": 100},
    )
    snapshot = make_snapshot([leg])
    unset = greeks_limits.GreeksLimitsConfig()
    verdicts = greeks_limits.evaluate_greeks_dimension(snapshot, unset, account_equity_usd=100000.0)
    assert all(v.verdict == LIMIT_NOT_CONFIGURED for v in verdicts)

    generous = greeks_limits.GreeksLimitsConfig(max_net_delta_ratio=0.99)
    verdicts = greeks_limits.evaluate_greeks_dimension(snapshot, generous, account_equity_usd=100000.0)
    delta_verdict = next(v for v in verdicts if v.dimension == "greeks_net_delta")
    assert delta_verdict.verdict == PASS


def test_greeks_dimension_not_computable_when_greek_unavailable_even_with_limit_set():
    # Leg has delta but no gamma/vega -- the gamma/vega dimensions must
    # report NOT_COMPUTABLE (never a guessed PASS) even though a limit
    # IS configured, because there is no value to compare against it.
    leg = make_position(
        venue="ALPACA", symbol="SPY_CALL_410", direction="LONG", quantity=1, mark_price=400.0,
        option_detail={"strike": 410, "expiry": "2026-12-18", "right": "CALL", "delta": 0.1, "multiplier": 100},
    )
    snapshot = make_snapshot([leg])
    config = greeks_limits.GreeksLimitsConfig(max_gamma_impact_ratio=0.5, max_vega_loss_ratio=0.5)
    verdicts = greeks_limits.evaluate_greeks_dimension(snapshot, config, account_equity_usd=100000.0)
    gamma_verdict = next(v for v in verdicts if v.dimension == "greeks_gamma_impact")
    vega_verdict = next(v for v in verdicts if v.dimension == "greeks_vega_loss")
    assert gamma_verdict.verdict == NOT_COMPUTABLE
    assert vega_verdict.verdict == NOT_COMPUTABLE


def test_greeks_would_breach_with_new_position_blocks():
    snapshot = make_snapshot([])
    config = greeks_limits.GreeksLimitsConfig(max_net_delta_ratio=0.01)
    proposed = make_position(
        venue="ALPACA", symbol="SPY_CALL_410", direction="LONG", quantity=100, mark_price=400.0,
        option_detail={"strike": 410, "expiry": "2026-12-18", "right": "CALL", "delta": 0.5, "multiplier": 100},
    )
    verdicts = greeks_limits.would_breach_with_new_position(snapshot, proposed, config, account_equity_usd=10000.0)
    delta_verdict = next(v for v in verdicts if v.dimension == "greeks_net_delta")
    assert delta_verdict.verdict == BLOCK
    # The real original snapshot must be unaffected by the hypothetical projection.
    assert snapshot.positions == ()


def test_proposed_greeks_limits_config_values():
    cfg = greeks_limits.PROPOSED_GREEKS_LIMITS_CONFIG
    assert cfg.max_net_delta_ratio == 0.15
    assert cfg.max_gamma_impact_ratio == 0.05
    assert cfg.max_vega_loss_ratio == 0.005


# ------------------------------------------------------------------------
# greeks_limits.py -- Task 2 (2026-10-09): mark-price fallback / wide-
# spread handling.
# ------------------------------------------------------------------------

import datetime as _dt

_NOW = _dt.datetime(2026, 10, 9, 12, 0, 0, tzinfo=_dt.timezone.utc)


def test_greeks_no_fallback_collaborators_reproduces_old_behavior():
    # Leaving all three Task-2 collaborators at their None defaults must
    # behave byte-for-byte like the pre-2026-10-09 function -- a leg with
    # no mark_price and nowhere to fall back to is excluded, exactly as
    # before.
    leg = make_position(
        venue="ALPACA", symbol="SPY_CALL_410", direction="LONG", quantity=1, mark_price=None,
        option_detail={"strike": 410, "expiry": "2026-12-18", "right": "CALL", "delta": 0.4, "gamma": 0.02, "vega": 0.30, "multiplier": 100},
    )
    snapshot = make_snapshot([leg])
    agg = greeks_limits.aggregate_portfolio_greeks(snapshot, now=_NOW)
    assert agg["status"] == "NOT_COMPUTABLE"
    assert "ALPACA:SPY_CALL_410" in agg["legs_excluded_missing_data"]
    assert agg["leg_price_basis"]["ALPACA:SPY_CALL_410"] == "UNAVAILABLE"


def test_greeks_mark_price_zero_treated_same_as_missing():
    # mark_price=0 must trigger the fallback path exactly like None --
    # Martin's spec: "missing or resolves to None/0".
    leg = make_position(
        venue="ALPACA", symbol="SPY_CALL_410", direction="LONG", quantity=1, mark_price=0.0,
        option_detail={"strike": 410, "expiry": "2026-12-18", "right": "CALL", "delta": 0.4, "multiplier": 100},
    )
    snapshot = make_snapshot([leg])
    agg = greeks_limits.aggregate_portfolio_greeks(snapshot, now=_NOW)
    assert agg["leg_price_basis"]["ALPACA:SPY_CALL_410"] == "UNAVAILABLE"
    assert "ALPACA:SPY_CALL_410" in agg["legs_excluded_missing_data"]


class _FixedUnderlyingPriceProvider:
    def __init__(self, price):
        self.price = price

    def get_underlying_price(self, underlying_symbol, *, now):
        return self.price


def test_greeks_falls_back_to_underlying_price_proxy_without_double_counting_multiplier():
    # mark_price missing; underlying_price_provider supplies 400.0.
    # The resulting Dollar Gamma/Vega must match the SAME formula as a
    # clean mark_price=400.0 print -- i.e. the raw underlying price feeds
    # the Spot-Price slot, NOT underlying_price * multiplier (which would
    # inflate the result 100x and silently double-count the contract
    # multiplier).
    leg_fallback = make_position(
        venue="ALPACA", symbol="SPY_CALL_410", direction="LONG", quantity=3, mark_price=None,
        option_detail={"strike": 410, "expiry": "2026-12-18", "right": "CALL", "delta": 0.4, "gamma": 0.02, "vega": 0.30, "multiplier": 100},
    )
    leg_clean = make_position(
        venue="ALPACA", symbol="SPY_CALL_999", direction="LONG", quantity=3, mark_price=400.0,
        option_detail={"strike": 410, "expiry": "2026-12-18", "right": "CALL", "delta": 0.4, "gamma": 0.02, "vega": 0.30, "multiplier": 100},
    )
    provider = _FixedUnderlyingPriceProvider(400.0)

    agg_fallback = greeks_limits.aggregate_portfolio_greeks(
        make_snapshot([leg_fallback]), now=_NOW, underlying_price_provider=provider,
    )
    agg_clean = greeks_limits.aggregate_portfolio_greeks(make_snapshot([leg_clean]), now=_NOW)

    assert agg_fallback["leg_price_basis"]["ALPACA:SPY_CALL_410"] == "UNDERLYING_PRICE_PROXY"
    assert agg_fallback["gamma_impact_usd_per_1pct_move"] == pytest.approx(agg_clean["gamma_impact_usd_per_1pct_move"])
    assert agg_fallback["vega_usd_per_vol_point"] == pytest.approx(agg_clean["vega_usd_per_vol_point"])
    assert agg_fallback["net_delta_usd"] == pytest.approx(agg_clean["net_delta_usd"])
    # The full "underlying price * multiplier" figure is still surfaced,
    # evidence-only, for audit visibility -- just never fed into the formula.
    evidence = agg_fallback["leg_price_evidence"]["ALPACA:SPY_CALL_410"]
    assert evidence["underlying_notional_proxy_usd"] == pytest.approx(400.0 * 100)


def test_greeks_falls_back_to_cached_prior_cycle_mark_price_within_15min():
    leg_observed = make_position(
        venue="ALPACA", symbol="SPY_CALL_410", direction="LONG", quantity=1, mark_price=400.0,
        option_detail={"strike": 410, "expiry": "2026-12-18", "right": "CALL", "delta": 0.4, "multiplier": 100},
    )
    cache = greeks_limits.MarkPriceCache()
    # Cycle 1: observed mark_price=400.0 -- records into the cache.
    greeks_limits.aggregate_portfolio_greeks(make_snapshot([leg_observed]), now=_NOW, mark_price_cache=cache)

    # Cycle 2, 10 minutes later: mark_price now missing (stale feed).
    leg_stale = make_position(
        venue="ALPACA", symbol="SPY_CALL_410", direction="LONG", quantity=1, mark_price=None,
        option_detail={"strike": 410, "expiry": "2026-12-18", "right": "CALL", "delta": 0.4, "multiplier": 100},
    )
    later = _NOW + _dt.timedelta(minutes=10)
    agg = greeks_limits.aggregate_portfolio_greeks(make_snapshot([leg_stale]), now=later, mark_price_cache=cache)
    assert agg["leg_price_basis"]["ALPACA:SPY_CALL_410"] == "CACHED_PRIOR_CYCLE_MARK"
    assert agg["net_delta_usd"] == pytest.approx(0.4 * 1 * 100 * 400.0)


def test_greeks_cached_mark_price_expires_after_15_minutes():
    leg_observed = make_position(
        venue="ALPACA", symbol="SPY_CALL_410", direction="LONG", quantity=1, mark_price=400.0,
        option_detail={"strike": 410, "expiry": "2026-12-18", "right": "CALL", "delta": 0.4, "multiplier": 100},
    )
    cache = greeks_limits.MarkPriceCache()
    greeks_limits.aggregate_portfolio_greeks(make_snapshot([leg_observed]), now=_NOW, mark_price_cache=cache)

    leg_stale = make_position(
        venue="ALPACA", symbol="SPY_CALL_410", direction="LONG", quantity=1, mark_price=None,
        option_detail={"strike": 410, "expiry": "2026-12-18", "right": "CALL", "delta": 0.4, "multiplier": 100},
    )
    too_late = _NOW + _dt.timedelta(minutes=16)
    agg = greeks_limits.aggregate_portfolio_greeks(make_snapshot([leg_stale]), now=too_late, mark_price_cache=cache)
    assert agg["leg_price_basis"]["ALPACA:SPY_CALL_410"] == "UNAVAILABLE"
    assert "ALPACA:SPY_CALL_410" in agg["legs_excluded_missing_data"]


def test_greeks_underlying_price_proxy_takes_priority_over_stale_cache():
    # Per the disclosed, deterministic fallback order: a live underlying-
    # price proxy beats a cached prior-cycle mark price when both are
    # available (the live signal is fresher).
    leg_observed = make_position(
        venue="ALPACA", symbol="SPY_CALL_410", direction="LONG", quantity=1, mark_price=350.0,
        option_detail={"strike": 410, "expiry": "2026-12-18", "right": "CALL", "delta": 0.4, "multiplier": 100},
    )
    cache = greeks_limits.MarkPriceCache()
    greeks_limits.aggregate_portfolio_greeks(make_snapshot([leg_observed]), now=_NOW, mark_price_cache=cache)

    leg_stale = make_position(
        venue="ALPACA", symbol="SPY_CALL_410", direction="LONG", quantity=1, mark_price=None,
        option_detail={"strike": 410, "expiry": "2026-12-18", "right": "CALL", "delta": 0.4, "multiplier": 100},
    )
    later = _NOW + _dt.timedelta(minutes=5)
    agg = greeks_limits.aggregate_portfolio_greeks(
        make_snapshot([leg_stale]), now=later, mark_price_cache=cache,
        underlying_price_provider=_FixedUnderlyingPriceProvider(400.0),
    )
    assert agg["leg_price_basis"]["ALPACA:SPY_CALL_410"] == "UNDERLYING_PRICE_PROXY"
    assert agg["net_delta_usd"] == pytest.approx(0.4 * 1 * 100 * 400.0)  # uses 400.0, not the cached 350.0


class _FixedQuoteProvider:
    def __init__(self, bid, ask):
        self.bid = bid
        self.ask = ask

    def get_option_quote(self, position, *, now):
        return {"bid": self.bid, "ask": self.ask}


def test_greeks_wide_spread_leg_excluded_never_becomes_effective_risk():
    # bid/ask spread = (10.0 - 8.0) / 9.0 = 22.2%, well over the 10%
    # liquidity floor -- this leg's Greeks must be excluded from the sum
    # even though it HAS a clean, valid mark_price.
    leg = make_position(
        venue="ALPACA", symbol="SPY_CALL_410", direction="LONG", quantity=1, mark_price=400.0,
        option_detail={"strike": 410, "expiry": "2026-12-18", "right": "CALL", "delta": 0.4, "gamma": 0.02, "vega": 0.3, "multiplier": 100},
    )
    snapshot = make_snapshot([leg])
    agg = greeks_limits.aggregate_portfolio_greeks(
        snapshot, now=_NOW, quote_provider=_FixedQuoteProvider(bid=8.0, ask=10.0),
    )
    assert "ALPACA:SPY_CALL_410" in agg["legs_excluded_wide_spread"]
    assert agg["net_delta_usd"] is None
    assert agg["gamma_impact_usd_per_1pct_move"] is None
    assert agg["vega_usd_per_vol_point"] is None
    assert agg["status"] == "NOT_COMPUTABLE"  # the only leg in the book was excluded


def test_greeks_tight_spread_leg_not_excluded():
    # bid/ask spread = (9.2 - 8.8) / 9.0 = 4.4%, comfortably inside the
    # 10% floor -- must NOT be excluded.
    leg = make_position(
        venue="ALPACA", symbol="SPY_CALL_410", direction="LONG", quantity=1, mark_price=400.0,
        option_detail={"strike": 410, "expiry": "2026-12-18", "right": "CALL", "delta": 0.4, "multiplier": 100},
    )
    snapshot = make_snapshot([leg])
    agg = greeks_limits.aggregate_portfolio_greeks(
        snapshot, now=_NOW, quote_provider=_FixedQuoteProvider(bid=8.8, ask=9.2),
    )
    assert agg["legs_excluded_wide_spread"] == []
    assert agg["net_delta_usd"] is not None


def test_greeks_no_quote_provider_skips_wide_spread_check_entirely():
    # Disclosed gap, not a guess: omitting quote_provider must never
    # itself cause an exclusion.
    leg = make_position(
        venue="ALPACA", symbol="SPY_CALL_410", direction="LONG", quantity=1, mark_price=400.0,
        option_detail={"strike": 410, "expiry": "2026-12-18", "right": "CALL", "delta": 0.4, "multiplier": 100},
    )
    agg = greeks_limits.aggregate_portfolio_greeks(make_snapshot([leg]), now=_NOW)
    assert agg["legs_excluded_wide_spread"] == []


def test_would_breach_with_new_position_threads_task2_collaborators():
    # The pre-trade projection path (the one actually wired into
    # portfolio_additional_enforcement.py's check_fn) must honor the same
    # fallback -- a proposed leg with no mark_price but a live underlying
    # price must still be evaluated, not silently skipped.
    snapshot = make_snapshot([])
    config = greeks_limits.GreeksLimitsConfig(max_net_delta_ratio=0.01)
    proposed = make_position(
        venue="ALPACA", symbol="SPY_CALL_410", direction="LONG", quantity=100, mark_price=None,
        option_detail={"strike": 410, "expiry": "2026-12-18", "right": "CALL", "delta": 0.5, "multiplier": 100},
    )
    verdicts = greeks_limits.would_breach_with_new_position(
        snapshot, proposed, config, account_equity_usd=10000.0, now=_NOW,
        underlying_price_provider=_FixedUnderlyingPriceProvider(400.0),
    )
    delta_verdict = next(v for v in verdicts if v.dimension == "greeks_net_delta")
    assert delta_verdict.verdict == BLOCK


def test_build_additional_portfolio_check_fn_threads_task2_collaborators_through():
    # End-to-end: the actual check_fn wired into the live choke point
    # must accept and use the Task-2 collaborators, not just the raw
    # greeks_limits functions in isolation.
    snapshot = make_snapshot([])
    cache = greeks_limits.MarkPriceCache()
    check_fn = pae.build_additional_portfolio_check_fn(
        snapshot_provider=lambda: snapshot,
        greeks_config=greeks_limits.GreeksLimitsConfig(max_net_delta_ratio=0.01),
        account_equity_lookup=lambda: 10000.0,
        now_fn=lambda: _NOW,
        mark_price_cache=cache,
        underlying_price_provider=_FixedUnderlyingPriceProvider(400.0),
    )
    decision = type("D", (), {
        "outcome": "DECIDE_LONG",
        "option_detail": {"strike": 410, "expiry": "2026-12-18", "right": "CALL", "delta": 0.5, "multiplier": 100},
    })()
    result = check_fn("SPY", "OPEN_LONG", 100, decision)
    assert result.allowed is False
    assert any("greeks_net_delta" in r for r in result.reasons)


# ------------------------------------------------------------------------
# validation_throttle.py
# ------------------------------------------------------------------------

def test_classify_track_validation_state():
    config = validation_throttle.ValidationThrottleConfig()
    good_windows = [{"profit_factor": 1.5, "p_value": 0.02, "n_trades": 40}] * 3
    assert validation_throttle.classify_track_validation_state(good_windows, config=config) == "PAPER_VALIDATED"

    too_few = good_windows[:2]
    assert validation_throttle.classify_track_validation_state(too_few, config=config) == "UNVALIDATED"

    one_bad = good_windows[:2] + [{"profit_factor": 0.8, "p_value": 0.5, "n_trades": 10}]
    assert validation_throttle.classify_track_validation_state(one_bad, config=config) == "UNVALIDATED"


def test_classify_live_validation():
    config = validation_throttle.ValidationThrottleConfig()
    assert validation_throttle.classify_live_validation({"profit_factor": 2.0, "p_value": 0.01}, config=config) is True
    assert validation_throttle.classify_live_validation({"profit_factor": 0.9, "p_value": 0.5}, config=config) is False
    assert validation_throttle.classify_live_validation({}, config=config) is False


def test_compute_capital_allocation_multiplier():
    config = validation_throttle.ValidationThrottleConfig()
    assert validation_throttle.compute_capital_allocation_multiplier("UNVALIDATED", config=config) == 0.0
    assert validation_throttle.compute_capital_allocation_multiplier("PAPER_VALIDATED", config=config) == config.paper_pilot_allocation_pct
    assert validation_throttle.compute_capital_allocation_multiplier("LIVE_VALIDATED", config=config, live_validated_stage_index=0) == config.live_validated_scale_stages[0]
    # Out-of-range stage index clamps to the last stage rather than raising.
    assert validation_throttle.compute_capital_allocation_multiplier("LIVE_VALIDATED", config=config, live_validated_stage_index=99) == config.live_validated_scale_stages[-1]


def test_validation_throttle_dimension_always_pass_with_correct_multiplier():
    config = validation_throttle.ValidationThrottleConfig()
    records = {
        "track_unvalidated": validation_throttle.TrackValidationRecord(
            track_id="track_unvalidated", state="UNVALIDATED", as_of="2026-10-09T00:00:00+00:00", supporting_metrics={},
        ),
        "track_paper": validation_throttle.TrackValidationRecord(
            track_id="track_paper", state="PAPER_VALIDATED", as_of="2026-10-09T00:00:00+00:00", supporting_metrics={},
        ),
        "track_live": validation_throttle.TrackValidationRecord(
            track_id="track_live", state="LIVE_VALIDATED", as_of="2026-10-09T00:00:00+00:00",
            supporting_metrics={"live_validated_stage_index": 1},
        ),
    }
    verdicts = validation_throttle.evaluate_validation_throttle_dimension(records, config)
    assert len(verdicts) == 3
    assert all(v.verdict == PASS for v in verdicts)

    by_track = {v.evidence["track_id"]: v for v in verdicts}
    assert by_track["track_unvalidated"].evidence["allocation_multiplier"] == 0.0
    assert by_track["track_paper"].evidence["allocation_multiplier"] == pytest.approx(config.paper_pilot_allocation_pct)
    assert by_track["track_live"].evidence["allocation_multiplier"] == pytest.approx(config.live_validated_scale_stages[1])


# ------------------------------------------------------------------------
# portfolio_additional_enforcement.py
# ------------------------------------------------------------------------

def test_evaluate_additional_portfolio_dimensions_returns_real_enforcement_decision():
    positions = [make_position(venue="MEXC", symbol="BTC/USDT:USDT", notional_usd=1000.0)]
    snapshot = make_snapshot(positions)
    macro_config = macro_buckets.MacroBucketConfig(
        bucket_membership={"RISK_SENTIMENT_BETA": {"BTC/USDT:USDT": 1.0}}, max_bucket_exposure_ratio=0.9,
    )
    greeks_config = greeks_limits.GreeksLimitsConfig()
    validation_config = validation_throttle.ValidationThrottleConfig()

    decision = pae.evaluate_additional_portfolio_dimensions(
        snapshot,
        macro_config=macro_config,
        greeks_config=greeks_config,
        validation_records={},
        validation_config=validation_config,
        account_equity_usd=10000.0,
    )

    assert isinstance(decision, EnforcementDecision)
    assert decision.overall_verdict == "ALLOW"
    assert decision.decision_hash and isinstance(decision.decision_hash, str)
    assert decision.snapshot_as_of == snapshot.as_of
    assert decision.snapshot_state_hash == snapshot.state_hash
    dims = {v.dimension for v in decision.dimension_verdicts}
    assert "macro_bucket_exposure" in dims


def test_evaluate_additional_portfolio_dimensions_blocks_on_macro_breach():
    positions = [make_position(venue="MEXC", symbol="BTC/USDT:USDT", notional_usd=9000.0)]
    snapshot = make_snapshot(positions)
    macro_config = macro_buckets.MacroBucketConfig(
        bucket_membership={"RISK_SENTIMENT_BETA": {"BTC/USDT:USDT": 1.0}}, max_bucket_exposure_ratio=0.1,
    )
    decision = pae.evaluate_additional_portfolio_dimensions(
        snapshot,
        macro_config=macro_config,
        greeks_config=greeks_limits.GreeksLimitsConfig(),
        validation_records={},
        validation_config=validation_throttle.ValidationThrottleConfig(),
        account_equity_usd=10000.0,
    )
    assert decision.overall_verdict == "BLOCK"


def test_build_additional_portfolio_check_fn_composes_with_real_combinator():
    class DummyVerdict:
        def __init__(self, allowed):
            self.allowed = allowed
            self.reasons = ()

    def dummy_existing_check_fn(symbol, direction, quantity, decision):
        return DummyVerdict(True)

    snapshot = make_snapshot([])
    greeks_config = greeks_limits.GreeksLimitsConfig(max_net_delta_ratio=0.01)

    additional_fn = pae.build_additional_portfolio_check_fn(
        snapshot_provider=lambda: snapshot,
        greeks_config=greeks_config,
        account_equity_lookup=lambda: 10000.0,
    )

    combined = combine_enforcement_check_fns(dummy_existing_check_fn, additional_fn)
    assert combined is not None

    class DummyDecision:
        option_detail = {"strike": 410, "expiry": "2026-12-18", "right": "CALL", "delta": 0.5, "multiplier": 100, "mark_price": 400.0}

    result = combined("SPY_CALL_410", "OPEN_LONG", 100, DummyDecision())
    assert result.allowed is False
    assert any("greeks_net_delta" in reason for reason in result.reasons)


def test_build_additional_portfolio_check_fn_allows_non_options_order():
    def dummy_existing_check_fn(symbol, direction, quantity, decision):
        class V:
            allowed = True
            reasons = ()
        return V()

    snapshot = make_snapshot([])
    additional_fn = pae.build_additional_portfolio_check_fn(
        snapshot_provider=lambda: snapshot,
        greeks_config=greeks_limits.GreeksLimitsConfig(max_net_delta_ratio=0.01),
        account_equity_lookup=lambda: 10000.0,
    )
    combined = combine_enforcement_check_fns(dummy_existing_check_fn, additional_fn)

    class DummyDecisionNoOptions:
        pass

    result = combined("BTC/USDT:USDT", "OPEN_LONG", 1.0, DummyDecisionNoOptions())
    assert result.allowed is True


def test_build_additional_portfolio_check_fn_skips_close_directions():
    snapshot = make_snapshot([])
    additional_fn = pae.build_additional_portfolio_check_fn(
        snapshot_provider=lambda: snapshot,
        greeks_config=greeks_limits.GreeksLimitsConfig(max_net_delta_ratio=0.01),
        account_equity_lookup=lambda: 10000.0,
    )
    result = additional_fn("SPY_CALL_410", "CLOSE_LONG", 100, object())
    assert result.allowed is True
    assert result.dimension_verdicts == ()


def test_record_additional_portfolio_decision_uses_real_journal(tmp_path):
    positions = [make_position(venue="MEXC", symbol="BTC/USDT:USDT", notional_usd=1000.0)]
    snapshot = make_snapshot(positions)
    decision = pae.evaluate_additional_portfolio_dimensions(
        snapshot,
        macro_config=macro_buckets.MacroBucketConfig(bucket_membership={}),
        greeks_config=greeks_limits.GreeksLimitsConfig(),
        validation_records={},
        validation_config=validation_throttle.ValidationThrottleConfig(),
        account_equity_usd=10000.0,
    )
    journal_path = tmp_path / "decisions.jsonl"
    entry = pae.record_additional_portfolio_decision(journal_path, decision)
    assert entry.context["source"] == "additional_portfolio_dimensions"
    assert journal_path.exists()

    entries = pae._JOURNAL.read_journal(journal_path)
    assert len(entries) == 1
    assert entries[0]["decision_hash"] == decision.decision_hash


def test_build_additional_portfolio_check_fn_blocks_on_macro_stacking_breach():
    # Book already over the 0.15 LONG stacking floor for RISK_SENTIMENT_BETA
    # ($2000 / $10000 = 0.20). A candidate OPEN_LONG in ETH (same bucket)
    # must be blocked by the stacking gate even though it carries no
    # option_detail at all -- proving this gate is not options-specific
    # and does not depend on the Greeks half of this check_fn.
    positions = [make_position(venue="MEXC", symbol="BTC/USDT:USDT", notional_usd=2000.0)]
    snapshot = make_snapshot(positions)
    macro_config = macro_buckets.MacroBucketConfig(
        bucket_membership={"RISK_SENTIMENT_BETA": {"BTC/USDT:USDT": 1.0, "ETH/USDT:USDT": 1.0}},
    )

    def dummy_existing_check_fn(symbol, direction, quantity, decision):
        class V:
            allowed = True
            reasons = ()
        return V()

    additional_fn = pae.build_additional_portfolio_check_fn(
        snapshot_provider=lambda: snapshot,
        greeks_config=greeks_limits.GreeksLimitsConfig(),
        account_equity_lookup=lambda: 10000.0,
        macro_config=macro_config,
    )
    combined = combine_enforcement_check_fns(dummy_existing_check_fn, additional_fn)

    result = combined("ETH/USDT:USDT", "OPEN_LONG", 1.0, object())
    assert result.allowed is False
    assert any("macro_bucket_same_direction_stacking" in reason for reason in result.reasons)


def test_build_additional_portfolio_check_fn_macro_config_none_preserves_old_behavior():
    # Backward compatibility: omitting macro_config (the default) must
    # behave exactly as before this change -- no stacking check runs.
    positions = [make_position(venue="MEXC", symbol="BTC/USDT:USDT", notional_usd=2000.0)]
    snapshot = make_snapshot(positions)

    def dummy_existing_check_fn(symbol, direction, quantity, decision):
        class V:
            allowed = True
            reasons = ()
        return V()

    additional_fn = pae.build_additional_portfolio_check_fn(
        snapshot_provider=lambda: snapshot,
        greeks_config=greeks_limits.GreeksLimitsConfig(),
        account_equity_lookup=lambda: 10000.0,
    )
    combined = combine_enforcement_check_fns(dummy_existing_check_fn, additional_fn)
    result = combined("ETH/USDT:USDT", "OPEN_LONG", 1.0, object())
    assert result.allowed is True

    direct = additional_fn("ETH/USDT:USDT", "OPEN_LONG", 1.0, object())
    assert direct.allowed is True
    assert direct.dimension_verdicts == ()
