#!/usr/bin/env python3
"""Contract + unit tests for AURA v0.5.3.44 Portfolio Exposure Enforcement.

Covers, per Martin's explicit .44 scoping instruction: clean portfolio,
each individual limit breach, multiple simultaneous breaches, stale
snapshot, partial/failed venue, NOT_COMPUTABLE dimensions, missing
configuration (LIMIT_NOT_CONFIGURED), positions outside AURA's own intent
ledger, fail-closed behavior, and deterministic/reproducible decisions.

Builds real `.43` PortfolioSnapshot/VenueFetchStatus/PositionRecord
objects directly (no mocks) and feeds them through `.44`'s real
`evaluate_portfolio_enforcement`/`evaluate_hypothetical_trade` -- these
are the actual .43 data structures, not stand-ins, so this exercises the
true cross-module contract.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OBS_PATH = ROOT / "aura_v05343_portfolio_exposure_observability.py"
ENF_PATH = ROOT / "aura_v05344_portfolio_exposure_enforcement.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


OBS = _load("aura_v05343_portfolio_exposure_observability", OBS_PATH)
ENF = _load("aura_v05344_portfolio_exposure_enforcement", ENF_PATH)

NOW = datetime(2026, 9, 12, 12, 0, 0, tzinfo=timezone.utc)
AS_OF = NOW.isoformat()


def _status(venue, status="SUCCESS", equity=10000.0, positions_count=0, error=None):
    return OBS.VenueFetchStatus(venue=venue, status=status, error=error, fetched_at=AS_OF,
                                 positions_count=positions_count, equity=equity)


def _pos(venue="MEXC", symbol="BTC_USDT", direction="LONG", notional=1000.0, leverage=None,
         raw_source_id=None, as_of=AS_OF):
    return OBS.PositionRecord(
        venue=venue, symbol=symbol, direction=direction, quantity=1.0, entry_price=100.0,
        leverage=leverage, mark_price=100.0, notional_usd=notional, notional_basis="MARK_TO_MARKET",
        unrealized_pnl_usd=0.0, liquidation_price=None, raw_source_id=raw_source_id, as_of=as_of,
    )


def _snapshot(positions, mexc_status=None, alpaca_status=None, as_of=AS_OF):
    mexc_status = mexc_status if mexc_status is not None else _status("MEXC", positions_count=len([p for p in positions if p.venue == "MEXC"]))
    alpaca_status = alpaca_status if alpaca_status is not None else _status("ALPACA", positions_count=len([p for p in positions if p.venue == "ALPACA"]))
    return OBS._build_snapshot(as_of, positions, {"MEXC": mexc_status, "ALPACA": alpaca_status})


def _history_with_prior_day(venue, day_start_equity, as_of=AS_OF):
    """A prior same-day equity snapshot so daily_loss becomes COMPUTABLE
    instead of NOT_YET_AVAILABLE (which is a distinct, always-BLOCKing
    case tested separately)."""
    return [{"venue": venue, "equity": day_start_equity, "as_of": as_of[:10] + "T00:00:00+00:00"}]


def _both_venue_history(mexc_equity=4000.0, alpaca_equity=10000.0):
    return _history_with_prior_day("MEXC", mexc_equity) + _history_with_prior_day("ALPACA", alpaca_equity)


def _evaluate(snapshot, limits, history=None, now=NOW, max_age=300):
    history = history if history is not None else _both_venue_history()
    report = OBS.compute_exposure_dimensions(snapshot, history, mexc_leverage_cap=limits.mexc_leverage_cap)
    return ENF.evaluate_portfolio_enforcement(snapshot, report, limits, max_snapshot_age_seconds=max_age, now=now)


def _verdict_map(decision):
    return {(v.dimension, v.venue): v.verdict for v in decision.dimension_verdicts}


# ---------------------------------------------------------------------------
# Clean portfolio.
# ---------------------------------------------------------------------------

def test_clean_flat_portfolio_no_limits_configured_allows():
    snap = _snapshot([])
    limits = ENF.PortfolioLimits()
    decision = _evaluate(snap, limits)
    assert decision.overall_verdict == "ALLOW"
    vmap = _verdict_map(decision)
    assert vmap[("portfolio_heat", None)] == "LIMIT_NOT_CONFIGURED"
    assert vmap[("asset_concentration", None)] == "LIMIT_NOT_CONFIGURED"
    assert vmap[("leverage_exposure", "MEXC")] == "LIMIT_NOT_CONFIGURED"  # flat book, limit unset


def test_clean_portfolio_within_all_configured_limits_allows():
    positions = [_pos(venue="MEXC", symbol="BTC_USDT", notional=1000.0, leverage=2.0),
                 _pos(venue="MEXC", symbol="ETH_USDT", notional=1000.0, leverage=2.0)]
    snap = _snapshot(positions)
    limits = ENF.PortfolioLimits(
        max_portfolio_heat_ratio=0.5, max_asset_concentration_ratio=0.9, max_net_exposure_ratio=0.9,
        max_leverage_ratio_by_venue={"MEXC": 1.0, "ALPACA": 1.0}, mexc_leverage_cap=10.0,
        max_daily_loss_pct_by_venue={"MEXC": 0.1, "ALPACA": 0.1}, max_drawdown_pct_by_venue={"MEXC": 0.3, "ALPACA": 0.3},
        correlated_groups={"majors": ("MEXC:BTC_USDT",)}, max_correlation_group_concentration_ratio=0.9,
    )
    decision = _evaluate(snap, limits)
    assert decision.overall_verdict == "ALLOW"
    for v in decision.dimension_verdicts:
        assert v.verdict in ("PASS", "NOT_COMPUTABLE"), (v.dimension, v.venue, v.verdict, v.reason)


# ---------------------------------------------------------------------------
# Each individual limit breach.
# ---------------------------------------------------------------------------

def test_portfolio_heat_breach_blocks():
    """Heat is computed against TOTAL equity across all configured
    venues (matching .43's own aggregation) -- both venues' equity must
    be accounted for when picking numbers that actually breach."""
    positions = [_pos(venue="MEXC", notional=6000.0)]
    snap = _snapshot(positions, mexc_status=_status("MEXC", equity=4000.0, positions_count=1),
                      alpaca_status=_status("ALPACA", equity=1000.0, positions_count=0))
    limits = ENF.PortfolioLimits(max_portfolio_heat_ratio=0.5)
    decision = _evaluate(snap, limits)
    assert decision.overall_verdict == "BLOCK"
    v = next(v for v in decision.dimension_verdicts if v.dimension == "portfolio_heat")
    assert v.verdict == "BLOCK" and v.reason == "LIMIT_BREACHED"


def test_asset_concentration_breach_blocks():
    positions = [_pos(venue="MEXC", symbol="BTC_USDT", notional=9000.0), _pos(venue="MEXC", symbol="ETH_USDT", notional=1000.0)]
    snap = _snapshot(positions, mexc_status=_status("MEXC", equity=4000.0, positions_count=2))
    limits = ENF.PortfolioLimits(max_asset_concentration_ratio=0.5)
    decision = _evaluate(snap, limits)
    assert decision.overall_verdict == "BLOCK"
    v = next(v for v in decision.dimension_verdicts if v.dimension == "asset_concentration")
    assert v.verdict == "BLOCK" and v.reason == "LIMIT_BREACHED"
    assert v.evidence["max_symbol_share"] == 0.9


def test_directional_exposure_breach_blocks():
    positions = [_pos(venue="MEXC", direction="LONG", notional=5000.0)]
    snap = _snapshot(positions, mexc_status=_status("MEXC", equity=4000.0, positions_count=1))
    limits = ENF.PortfolioLimits(max_net_exposure_ratio=0.2)
    decision = _evaluate(snap, limits)
    assert decision.overall_verdict == "BLOCK"
    v = next(v for v in decision.dimension_verdicts if v.dimension == "directional_exposure")
    assert v.verdict == "BLOCK"


def test_leverage_exposure_breach_blocks_for_that_venue_only():
    positions = [_pos(venue="MEXC", notional=8000.0)]
    snap = _snapshot(positions, mexc_status=_status("MEXC", equity=4000.0, positions_count=1))
    limits = ENF.PortfolioLimits(max_leverage_ratio_by_venue={"MEXC": 1.0, "ALPACA": 1.0})
    decision = _evaluate(snap, limits)
    vmap = {(v.dimension, v.venue): v for v in decision.dimension_verdicts}
    assert vmap[("leverage_exposure", "MEXC")].verdict == "BLOCK"
    assert vmap[("leverage_exposure", "ALPACA")].verdict == "LIMIT_NOT_CONFIGURED" or vmap[("leverage_exposure", "ALPACA")].verdict == "PASS"


def test_mexc_leverage_cap_breach_blocks():
    positions = [_pos(venue="MEXC", leverage=15.0)]
    snap = _snapshot(positions, mexc_status=_status("MEXC", equity=4000.0, positions_count=1))
    limits = ENF.PortfolioLimits(mexc_leverage_cap=10.0)
    decision = _evaluate(snap, limits)
    assert decision.overall_verdict == "BLOCK"
    v = next(v for v in decision.dimension_verdicts if v.dimension == "mexc_leverage_cap")
    assert v.verdict == "BLOCK" and v.reason == "LIMIT_BREACHED"


def test_daily_loss_breach_blocks():
    positions = [_pos(venue="MEXC", notional=1000.0)]
    snap = _snapshot(positions, mexc_status=_status("MEXC", equity=3600.0, positions_count=1))
    limits = ENF.PortfolioLimits(max_daily_loss_pct_by_venue={"MEXC": 0.05})
    history = _history_with_prior_day("MEXC", 4000.0)  # 10% intraday loss vs 5% limit
    decision = _evaluate(snap, limits, history=history)
    v = next(v for v in decision.dimension_verdicts if v.dimension == "daily_loss" and v.venue == "MEXC")
    assert v.verdict == "BLOCK" and v.reason == "LIMIT_BREACHED"
    assert decision.overall_verdict == "BLOCK"


def test_max_drawdown_breach_blocks():
    positions = [_pos(venue="MEXC", notional=1000.0)]
    snap = _snapshot(positions, mexc_status=_status("MEXC", equity=8000.0, positions_count=1))
    limits = ENF.PortfolioLimits(max_drawdown_pct_by_venue={"MEXC": 0.1})
    history = [{"venue": "MEXC", "equity": 12000.0, "as_of": "2026-09-10T00:00:00+00:00"}]  # peak 12000, now 8000 -> 33% DD
    decision = _evaluate(snap, limits, history=history)
    v = next(v for v in decision.dimension_verdicts if v.dimension == "max_drawdown" and v.venue == "MEXC")
    assert v.verdict == "BLOCK" and v.reason == "LIMIT_BREACHED"
    assert decision.overall_verdict == "BLOCK"


# ---------------------------------------------------------------------------
# Multiple simultaneous breaches.
# ---------------------------------------------------------------------------

def test_multiple_simultaneous_breaches_all_reported():
    positions = [_pos(venue="MEXC", symbol="BTC_USDT", notional=9000.0, leverage=20.0)]
    snap = _snapshot(positions, mexc_status=_status("MEXC", equity=4000.0, positions_count=1))
    limits = ENF.PortfolioLimits(max_portfolio_heat_ratio=0.5, max_asset_concentration_ratio=0.5,
                                  max_leverage_ratio_by_venue={"MEXC": 1.0}, mexc_leverage_cap=10.0)
    decision = _evaluate(snap, limits)
    assert decision.overall_verdict == "BLOCK"
    blocked_dims = {v.dimension for v in decision.dimension_verdicts if v.verdict == "BLOCK"}
    assert {"portfolio_heat", "asset_concentration", "leverage_exposure", "mexc_leverage_cap"}.issubset(blocked_dims)


# ---------------------------------------------------------------------------
# Stale snapshot.
# ---------------------------------------------------------------------------

def test_stale_snapshot_blocks_everything_immediately():
    old_as_of = (NOW - timedelta(seconds=1000)).isoformat()
    snap = _snapshot([], as_of=old_as_of)
    limits = ENF.PortfolioLimits(max_portfolio_heat_ratio=0.5)
    decision = _evaluate(snap, limits, now=NOW, max_age=300)
    assert decision.overall_verdict == "BLOCK"
    assert len(decision.dimension_verdicts) == 1
    assert decision.dimension_verdicts[0].reason == "SNAPSHOT_STALE"


def test_snapshot_within_freshness_window_is_not_blocked_for_staleness():
    recent_as_of = (NOW - timedelta(seconds=60)).isoformat()
    snap = _snapshot([], as_of=recent_as_of)
    limits = ENF.PortfolioLimits()
    decision = _evaluate(snap, limits, now=NOW, max_age=300)
    assert not any(v.reason == "SNAPSHOT_STALE" for v in decision.dimension_verdicts)


def test_snapshot_timestamped_in_future_is_blocked():
    future_as_of = (NOW + timedelta(seconds=60)).isoformat()
    snap = _snapshot([], as_of=future_as_of)
    limits = ENF.PortfolioLimits()
    decision = _evaluate(snap, limits, now=NOW, max_age=300)
    assert decision.overall_verdict == "BLOCK"
    assert decision.dimension_verdicts[0].reason == "SNAPSHOT_TIMESTAMP_IN_FUTURE"


# ---------------------------------------------------------------------------
# Partial / failed venue.
# ---------------------------------------------------------------------------

def test_failed_venue_blocks_aggregate_dimensions_never_treated_as_zero():
    """MEXC fails to fetch -- its real (unknown) positions must not be
    silently excluded from portfolio_heat/asset_concentration/directional
    as if MEXC held nothing."""
    positions = [_pos(venue="ALPACA", notional=500.0)]
    snap = _snapshot(positions, mexc_status=_status("MEXC", status="FAILED", equity=None, error="ConnectionError: down"))
    limits = ENF.PortfolioLimits(max_portfolio_heat_ratio=0.99, max_asset_concentration_ratio=0.99, max_net_exposure_ratio=0.99)
    decision = _evaluate(snap, limits)
    assert decision.overall_verdict == "BLOCK"
    for dim in ("portfolio_heat", "asset_concentration", "directional_exposure"):
        v = next(v for v in decision.dimension_verdicts if v.dimension == dim)
        assert v.verdict == "BLOCK" and v.reason == "VENUE_DATA_INCOMPLETE"


def test_failed_venue_only_blocks_that_venues_own_per_venue_dimensions():
    """Alpaca succeeds -- its own daily_loss/max_drawdown/leverage must
    still be evaluatable even though MEXC failed."""
    positions = [_pos(venue="ALPACA", notional=500.0, leverage=None)]
    snap = _snapshot(positions, mexc_status=_status("MEXC", status="FAILED", equity=None, error="down"),
                      alpaca_status=_status("ALPACA", equity=9000.0, positions_count=1))
    limits = ENF.PortfolioLimits(max_daily_loss_pct_by_venue={"ALPACA": 0.5, "MEXC": 0.5})
    history = _history_with_prior_day("ALPACA", 9500.0)
    decision = _evaluate(snap, limits, history=history)
    vmap = {(v.dimension, v.venue): v for v in decision.dimension_verdicts}
    assert vmap[("daily_loss", "ALPACA")].verdict in ("PASS", "BLOCK")  # evaluated normally, not skipped
    assert vmap[("daily_loss", "ALPACA")].reason != "VENUE_FETCH_FAILED"
    assert vmap[("daily_loss", "MEXC")].verdict == "BLOCK"
    assert vmap[("daily_loss", "MEXC")].reason == "VENUE_FETCH_FAILED"


def test_not_configured_venue_is_not_applicable_not_a_failure():
    positions = [_pos(venue="MEXC", notional=500.0)]
    snap = _snapshot(positions, alpaca_status=_status("ALPACA", status="NOT_CONFIGURED", equity=None))
    limits = ENF.PortfolioLimits(max_daily_loss_pct_by_venue={"MEXC": 0.5, "ALPACA": 0.5})
    history = _history_with_prior_day("MEXC", 4000.0)
    decision = _evaluate(snap, limits, history=history)
    vmap = {(v.dimension, v.venue): v for v in decision.dimension_verdicts}
    assert vmap[("daily_loss", "ALPACA")].verdict == "NOT_APPLICABLE"
    assert vmap[("daily_loss", "ALPACA")].reason == "VENUE_NOT_CONFIGURED"
    # aggregate dims are NOT penalized just because a venue was never configured
    assert vmap[("portfolio_heat", None)].verdict != "BLOCK" or vmap[("portfolio_heat", None)].reason != "VENUE_DATA_INCOMPLETE"


def test_positions_exist_but_not_priced_blocks_asset_concentration():
    """A position exists but notional could not be determined (e.g. a
    ticker outage at .43 fetch time) -- this is NOT the same as a flat
    book and must not be treated as safe."""
    unpriced = OBS.PositionRecord(venue="MEXC", symbol="XRP_USDT", direction="LONG", quantity=1.0,
                                   entry_price=1.0, leverage=None, mark_price=None, notional_usd=None,
                                   notional_basis=None, unrealized_pnl_usd=None, liquidation_price=None,
                                   raw_source_id=None, as_of=AS_OF)
    snap = _snapshot([unpriced], mexc_status=_status("MEXC", equity=4000.0, positions_count=1))
    limits = ENF.PortfolioLimits(max_asset_concentration_ratio=0.9)
    decision = _evaluate(snap, limits)
    v = next(v for v in decision.dimension_verdicts if v.dimension == "asset_concentration")
    assert v.verdict == "BLOCK" and v.reason == "EXPOSURE_NOT_COMPUTABLE"


# ---------------------------------------------------------------------------
# NOT_COMPUTABLE dimensions.
# ---------------------------------------------------------------------------

def test_structurally_not_computable_dimensions_are_reported_and_never_block():
    snap = _snapshot([])
    limits = ENF.PortfolioLimits()
    decision = _evaluate(snap, limits)
    vmap = _verdict_map(decision)
    for dim in ("correlation", "liquidity_concentration", "equity_sector_concentration", "per_trade_risk", "consecutive_loss_limit"):
        assert vmap[(dim, None)] == "NOT_COMPUTABLE"
    assert decision.overall_verdict == "ALLOW"


# ---------------------------------------------------------------------------
# Missing configuration.
# ---------------------------------------------------------------------------

def test_no_limits_configured_anywhere_never_blocks_on_that_basis():
    positions = [_pos(venue="MEXC", notional=999999.0, leverage=100.0)]  # would breach any real limit
    snap = _snapshot(positions, mexc_status=_status("MEXC", equity=4000.0, positions_count=1))
    limits = ENF.PortfolioLimits()  # nothing configured
    decision = _evaluate(snap, limits)
    for v in decision.dimension_verdicts:
        assert v.verdict != "BLOCK" or v.reason in ("VENUE_DATA_INCOMPLETE", "INSUFFICIENT_HISTORY", "EXPOSURE_NOT_COMPUTABLE", "SNAPSHOT_STALE")
    heat_verdict = next(v for v in decision.dimension_verdicts if v.dimension == "portfolio_heat")
    assert heat_verdict.verdict == "LIMIT_NOT_CONFIGURED"


# ---------------------------------------------------------------------------
# Positions existing outside AURA's own intent ledger.
# ---------------------------------------------------------------------------

def test_positions_outside_intent_ledger_are_still_enforced():
    """This module has no concept of "in the ledger" at all -- it operates
    purely on .43's broker-truth snapshot. A position with a raw_source_id
    matching no AURA convention must still be fully subject to enforcement."""
    foreign = _pos(venue="MEXC", notional=9000.0, raw_source_id="999999-manual-not-in-ledger")
    snap = _snapshot([foreign], mexc_status=_status("MEXC", equity=4000.0, positions_count=1))
    limits = ENF.PortfolioLimits(max_portfolio_heat_ratio=0.5)
    decision = _evaluate(snap, limits)
    assert decision.overall_verdict == "BLOCK"
    v = next(v for v in decision.dimension_verdicts if v.dimension == "portfolio_heat")
    assert v.verdict == "BLOCK"


# ---------------------------------------------------------------------------
# Fail-closed behavior (aggregated checks across several scenarios).
# ---------------------------------------------------------------------------

def test_insufficient_history_fails_closed_even_with_generous_limit():
    positions = [_pos(venue="MEXC", notional=100.0)]
    snap = _snapshot(positions, mexc_status=_status("MEXC", equity=4000.0, positions_count=1))
    limits = ENF.PortfolioLimits(max_daily_loss_pct_by_venue={"MEXC": 0.99})
    decision = _evaluate(snap, limits, history=[])  # no prior snapshot at all -> NOT_YET_AVAILABLE
    v = next(v for v in decision.dimension_verdicts if v.dimension == "daily_loss" and v.venue == "MEXC")
    assert v.verdict == "BLOCK" and v.reason == "INSUFFICIENT_HISTORY"
    assert decision.overall_verdict == "BLOCK"


def test_zero_positions_venue_leverage_is_pass_not_block():
    """A flat book (zero positions in a venue) must not be penalized as a
    data-quality failure -- there is nothing to leverage."""
    snap = _snapshot([], mexc_status=_status("MEXC", equity=4000.0, positions_count=0))
    limits = ENF.PortfolioLimits(max_leverage_ratio_by_venue={"MEXC": 1.0})
    decision = _evaluate(snap, limits)
    v = next(v for v in decision.dimension_verdicts if v.dimension == "leverage_exposure" and v.venue == "MEXC")
    assert v.verdict == "PASS" and v.reason == "FLAT_BOOK"


# ---------------------------------------------------------------------------
# Deterministic / reproducible enforcement decisions.
# ---------------------------------------------------------------------------

def test_same_inputs_produce_identical_decision_hash():
    positions = [_pos(venue="MEXC", notional=2000.0, leverage=5.0)]
    snap = _snapshot(positions, mexc_status=_status("MEXC", equity=4000.0, positions_count=1))
    limits = ENF.PortfolioLimits(max_portfolio_heat_ratio=0.3, mexc_leverage_cap=3.0)
    d1 = _evaluate(snap, limits)
    d2 = _evaluate(snap, limits)
    assert d1.decision_hash == d2.decision_hash
    assert d1.overall_verdict == d2.overall_verdict == "BLOCK"


def test_different_limits_produce_different_decision_hash():
    positions = [_pos(venue="MEXC", notional=2000.0)]
    snap = _snapshot(positions, mexc_status=_status("MEXC", equity=4000.0, positions_count=1))
    d1 = _evaluate(snap, ENF.PortfolioLimits(max_portfolio_heat_ratio=0.9))
    d2 = _evaluate(snap, ENF.PortfolioLimits(max_portfolio_heat_ratio=0.1))
    assert d1.decision_hash != d2.decision_hash
    assert d1.overall_verdict == "ALLOW" and d2.overall_verdict == "BLOCK"


# ---------------------------------------------------------------------------
# Hypothetical-trade (authorization-time) path.
# ---------------------------------------------------------------------------

def test_evaluate_hypothetical_trade_blocks_when_projected_heat_breaches():
    snap = _snapshot([], mexc_status=_status("MEXC", equity=4000.0, positions_count=0))
    limits = ENF.PortfolioLimits(max_portfolio_heat_ratio=0.1)
    hypothetical = _pos(venue="MEXC", symbol="ETH_USDT", notional=3000.0)
    history = _both_venue_history()
    decision = ENF.evaluate_hypothetical_trade(snap, history, hypothetical, limits, max_snapshot_age_seconds=300, now=NOW)
    assert decision.overall_verdict == "BLOCK"
    v = next(v for v in decision.dimension_verdicts if v.dimension == "portfolio_heat")
    assert v.verdict == "BLOCK"


def test_evaluate_hypothetical_trade_allows_when_within_limits():
    snap = _snapshot([], mexc_status=_status("MEXC", equity=4000.0, positions_count=0))
    limits = ENF.PortfolioLimits(max_portfolio_heat_ratio=0.9, mexc_leverage_cap=10.0)
    hypothetical = _pos(venue="MEXC", symbol="ETH_USDT", notional=500.0, leverage=2.0)
    history = _both_venue_history()
    decision = ENF.evaluate_hypothetical_trade(snap, history, hypothetical, limits, max_snapshot_age_seconds=300, now=NOW)
    assert decision.overall_verdict == "ALLOW"


def test_evaluate_hypothetical_trade_does_not_mutate_original_snapshot():
    snap = _snapshot([], mexc_status=_status("MEXC", equity=4000.0, positions_count=0))
    limits = ENF.PortfolioLimits()
    hypothetical = _pos(venue="MEXC", symbol="ETH_USDT", notional=500.0)
    original_len = len(snap.positions)
    ENF.evaluate_hypothetical_trade(snap, _both_venue_history(), hypothetical, limits, max_snapshot_age_seconds=300, now=NOW)
    assert len(snap.positions) == original_len


def test_evaluate_hypothetical_trade_stale_snapshot_blocks():
    old_as_of = (NOW - timedelta(seconds=1000)).isoformat()
    snap = _snapshot([], as_of=old_as_of)
    limits = ENF.PortfolioLimits()
    hypothetical = _pos(venue="MEXC", notional=500.0)
    decision = ENF.evaluate_hypothetical_trade(snap, [], hypothetical, limits, max_snapshot_age_seconds=300, now=NOW)
    assert decision.overall_verdict == "BLOCK"
    assert decision.dimension_verdicts[0].reason == "SNAPSHOT_STALE"


# ---------------------------------------------------------------------------
# Static guard: this module must remain enforcement-only, never execution.
# ---------------------------------------------------------------------------

def test_module_never_places_or_modifies_orders_or_positions():
    source = ENF_PATH.read_text(encoding="utf-8")
    code_only = source.split('"""', 2)[-1]
    forbidden = ["place_order(", "submit_order(", "cancel_order(", "authorized_submit(", "authorized_order_request("]
    for token in forbidden:
        assert token not in code_only, f"enforcement module must not reference {token!r}"


# ---------------------------------------------------------------------------
# Section 2 — 2026-09-24 Lablab-audit extension: correlation-group
# concentration (item 9, optionwright / Alpacaruns). See .44's module
# docstring addendum for full provenance/rationale.
# ---------------------------------------------------------------------------

def _group_verdicts(decision, group=None):
    verdicts = [v for v in decision.dimension_verdicts if v.dimension == "correlation_group_concentration"]
    if group is None:
        return verdicts
    return [v for v in verdicts if v.evidence.get("group") == group]


def test_correlation_group_no_groups_configured_reports_single_limit_not_configured():
    positions = [_pos(venue="MEXC", symbol="BTC_USDT", notional=4000.0)]
    snap = _snapshot(positions, mexc_status=_status("MEXC", equity=10000.0, positions_count=1))
    limits = ENF.PortfolioLimits()  # no correlated_groups at all
    decision = _evaluate(snap, limits)
    verdicts = _group_verdicts(decision)
    assert len(verdicts) == 1
    assert verdicts[0].verdict == "LIMIT_NOT_CONFIGURED"
    assert verdicts[0].reason == "NO_GROUPS_CONFIGURED"


def test_correlation_group_configured_without_ratio_is_limit_not_configured_per_group():
    positions = [_pos(venue="MEXC", symbol="BTC_USDT", notional=4000.0), _pos(venue="MEXC", symbol="ETH_USDT", notional=4000.0)]
    snap = _snapshot(positions, mexc_status=_status("MEXC", equity=10000.0, positions_count=2))
    limits = ENF.PortfolioLimits(correlated_groups={"majors": ("MEXC:BTC_USDT", "MEXC:ETH_USDT")})  # no ratio set
    decision = _evaluate(snap, limits)
    verdicts = _group_verdicts(decision, "majors")
    assert len(verdicts) == 1
    assert verdicts[0].verdict == "LIMIT_NOT_CONFIGURED"
    assert verdicts[0].reason == "NO_LIMIT_CONFIGURED"


def test_correlation_group_breach_blocks_even_though_no_single_symbol_breaches():
    """Neither BTC nor ETH alone exceeds a 50% single-symbol cap (each is
    40%), but held together they are 80% of the book -- exactly the gap
    audit item 9 exists to close."""
    positions = [_pos(venue="MEXC", symbol="BTC_USDT", notional=4000.0), _pos(venue="MEXC", symbol="ETH_USDT", notional=4000.0),
                 _pos(venue="MEXC", symbol="SOL_USDT", notional=2000.0)]
    snap = _snapshot(positions, mexc_status=_status("MEXC", equity=10000.0, positions_count=3))
    limits = ENF.PortfolioLimits(
        max_asset_concentration_ratio=0.5,  # each symbol individually passes this
        correlated_groups={"majors": ("MEXC:BTC_USDT", "MEXC:ETH_USDT")},
        max_correlation_group_concentration_ratio=0.5,
    )
    decision = _evaluate(snap, limits)
    single_symbol = next(v for v in decision.dimension_verdicts if v.dimension == "asset_concentration")
    assert single_symbol.verdict == "PASS"
    group = _group_verdicts(decision, "majors")[0]
    assert group.verdict == "BLOCK" and group.reason == "LIMIT_BREACHED"
    assert group.evidence["group_share"] == 0.8
    assert decision.overall_verdict == "BLOCK"


def test_correlation_group_within_limit_passes():
    positions = [_pos(venue="MEXC", symbol="BTC_USDT", notional=4000.0), _pos(venue="MEXC", symbol="ETH_USDT", notional=4000.0),
                 _pos(venue="MEXC", symbol="SOL_USDT", notional=2000.0)]
    snap = _snapshot(positions, mexc_status=_status("MEXC", equity=10000.0, positions_count=3))
    limits = ENF.PortfolioLimits(correlated_groups={"majors": ("MEXC:BTC_USDT", "MEXC:ETH_USDT")},
                                  max_correlation_group_concentration_ratio=0.9)
    decision = _evaluate(snap, limits)
    group = _group_verdicts(decision, "majors")[0]
    assert group.verdict == "PASS" and group.reason == "WITHIN_LIMIT"
    assert decision.overall_verdict == "ALLOW"


def test_correlation_group_member_not_held_treated_as_zero_share_not_error():
    positions = [_pos(venue="MEXC", symbol="BTC_USDT", notional=3000.0), _pos(venue="MEXC", symbol="SOL_USDT", notional=7000.0)]
    snap = _snapshot(positions, mexc_status=_status("MEXC", equity=10000.0, positions_count=2))
    limits = ENF.PortfolioLimits(correlated_groups={"majors": ("MEXC:BTC_USDT", "MEXC:XRP_USDT")},  # XRP not held at all
                                  max_correlation_group_concentration_ratio=0.9)
    decision = _evaluate(snap, limits)
    group = _group_verdicts(decision, "majors")[0]
    assert group.verdict == "PASS"
    assert group.evidence["group_share"] == 0.3
    assert group.evidence["members"]["MEXC:XRP_USDT"] == 0.0


def test_correlation_group_multiple_groups_all_independently_reported():
    positions = [_pos(venue="MEXC", symbol="BTC_USDT", notional=4000.0), _pos(venue="MEXC", symbol="ETH_USDT", notional=4000.0),
                 _pos(venue="MEXC", symbol="SOL_USDT", notional=2000.0)]
    snap = _snapshot(positions, mexc_status=_status("MEXC", equity=10000.0, positions_count=3))
    limits = ENF.PortfolioLimits(
        correlated_groups={"majors": ("MEXC:BTC_USDT", "MEXC:ETH_USDT"), "alts": ("MEXC:SOL_USDT",)},
        max_correlation_group_concentration_ratio=0.3,
    )
    decision = _evaluate(snap, limits)
    verdicts = _group_verdicts(decision)
    assert len(verdicts) == 2
    majors = next(v for v in verdicts if v.evidence["group"] == "majors")
    alts = next(v for v in verdicts if v.evidence["group"] == "alts")
    assert majors.verdict == "BLOCK"   # 0.8 > 0.3
    assert alts.verdict == "PASS"      # 0.2 <= 0.3


def test_correlation_group_failed_venue_blocks_as_data_quality_not_a_breach():
    """A failed venue blocks the WHOLE dimension as one verdict (data
    quality, not a per-group breach) -- same shape as asset_concentration's
    own aggregate data-quality block, which isn't per-symbol either."""
    positions = [_pos(venue="ALPACA", symbol="SPY", notional=500.0)]
    snap = _snapshot(positions, mexc_status=_status("MEXC", status="FAILED", equity=None, error="down"))
    limits = ENF.PortfolioLimits(correlated_groups={"g": ("ALPACA:SPY",)}, max_correlation_group_concentration_ratio=0.99)
    decision = _evaluate(snap, limits)
    verdicts = _group_verdicts(decision)
    assert len(verdicts) == 1
    assert verdicts[0].verdict == "BLOCK" and verdicts[0].reason == "VENUE_DATA_INCOMPLETE"


def test_correlation_group_flat_book_is_pass_not_block():
    snap = _snapshot([], mexc_status=_status("MEXC", equity=10000.0, positions_count=0))
    limits = ENF.PortfolioLimits(correlated_groups={"g": ("MEXC:BTC_USDT",)}, max_correlation_group_concentration_ratio=0.5)
    decision = _evaluate(snap, limits)
    group = _group_verdicts(decision, "g")[0]
    assert group.verdict == "PASS"
    assert group.evidence["group_share"] == 0.0


def test_correlation_group_unpriced_position_blocks_as_not_computable():
    unpriced = OBS.PositionRecord(venue="MEXC", symbol="XRP_USDT", direction="LONG", quantity=1.0, entry_price=1.0,
                                   leverage=None, mark_price=None, notional_usd=None, notional_basis=None,
                                   unrealized_pnl_usd=None, liquidation_price=None, raw_source_id=None, as_of=AS_OF)
    snap = _snapshot([unpriced], mexc_status=_status("MEXC", equity=4000.0, positions_count=1))
    limits = ENF.PortfolioLimits(correlated_groups={"g": ("MEXC:XRP_USDT",)}, max_correlation_group_concentration_ratio=0.5)
    decision = _evaluate(snap, limits)
    verdicts = _group_verdicts(decision)
    assert len(verdicts) == 1
    assert verdicts[0].verdict == "BLOCK" and verdicts[0].reason == "EXPOSURE_NOT_COMPUTABLE"


def test_correlation_group_evaluated_in_hypothetical_trade_as_current_state_only():
    """Matches asset_concentration's own documented behavior: current-state
    only, unaffected by the hypothetical order under evaluation."""
    positions = [_pos(venue="MEXC", symbol="BTC_USDT", notional=8000.0), _pos(venue="MEXC", symbol="ETH_USDT", notional=2000.0)]
    snap = _snapshot(positions, mexc_status=_status("MEXC", equity=10000.0, positions_count=2))
    limits = ENF.PortfolioLimits(correlated_groups={"majors": ("MEXC:BTC_USDT", "MEXC:ETH_USDT")},
                                  max_correlation_group_concentration_ratio=0.5)
    hypothetical = _pos(venue="ALPACA", symbol="SPY", notional=100.0)
    decision = ENF.evaluate_hypothetical_trade(snap, _both_venue_history(), hypothetical, limits, max_snapshot_age_seconds=300, now=NOW)
    group = _group_verdicts(decision, "majors")[0]
    assert group.verdict == "BLOCK"  # pre-existing 100% concentration in majors, untouched by the unrelated hypothetical
    assert group.evidence["group_share"] == 1.0


def test_correlation_group_participates_in_decision_hash():
    positions = [_pos(venue="MEXC", symbol="BTC_USDT", notional=4000.0), _pos(venue="MEXC", symbol="SOL_USDT", notional=6000.0)]
    snap = _snapshot(positions, mexc_status=_status("MEXC", equity=10000.0, positions_count=2))
    d1 = _evaluate(snap, ENF.PortfolioLimits(correlated_groups={"g": ("MEXC:BTC_USDT",)}, max_correlation_group_concentration_ratio=0.9))
    d2 = _evaluate(snap, ENF.PortfolioLimits(correlated_groups={"g": ("MEXC:BTC_USDT",)}, max_correlation_group_concentration_ratio=0.1))
    assert d1.decision_hash != d2.decision_hash
    assert d1.overall_verdict == "ALLOW" and d2.overall_verdict == "BLOCK"


def test_pre_existing_behavior_unaffected_when_no_correlated_groups_configured():
    """Backward-compatibility proof, same shape as .350's own
    default-weight-is-zero-no-op proof: a caller that never learns
    correlated_groups exists gets the exact same overall_verdict on every
    OTHER dimension as before this extension -- the new dimension only
    ever adds a LIMIT_NOT_CONFIGURED placeholder verdict, never changes
    an existing one."""
    positions = [_pos(venue="MEXC", symbol="BTC_USDT", notional=9000.0), _pos(venue="MEXC", symbol="ETH_USDT", notional=1000.0)]
    snap = _snapshot(positions, mexc_status=_status("MEXC", equity=4000.0, positions_count=2))
    limits = ENF.PortfolioLimits(max_asset_concentration_ratio=0.5)  # correlated_groups untouched (default)
    decision = _evaluate(snap, limits)
    assert decision.overall_verdict == "BLOCK"
    non_group_verdicts = [v for v in decision.dimension_verdicts if v.dimension != "correlation_group_concentration"]
    asset_conc = next(v for v in non_group_verdicts if v.dimension == "asset_concentration")
    assert asset_conc.verdict == "BLOCK" and asset_conc.reason == "LIMIT_BREACHED"
