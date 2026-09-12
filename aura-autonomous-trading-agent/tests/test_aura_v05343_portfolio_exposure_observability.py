#!/usr/bin/env python3
"""Contract + unit tests for AURA v0.5.3.43 Portfolio Exposure Observability.

Mirrors v0.5.3.28's test style: direct-import unit tests against injected
FakeMexcExchange / FakeAlpacaClient stand-ins (no real network call, no
real credentials, anywhere in this file — matching this session's disclosed
network-egress limitation and this repo's established test convention).

Covers, per Martin's explicit .43 scoping instruction: real-path tests for
both venues, cross-venue aggregation, missing/stale/partial broker data,
and positions existing outside AURA's own intent ledger (this module reads
broker truth directly via fetch_positions()/get_all_positions() — it has no
concept of "in the ledger" at all, so any broker-reported open position is
captured regardless of whether AURA's own intent ledger has a matching
record; this is exercised explicitly below).
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "aura_v05343_portfolio_exposure_observability.py"


def _load_module():
    """This module (unlike its siblings) defines frozen dataclasses under
    `from __future__ import annotations`; dataclasses's own field-type
    resolution looks the module up via sys.modules[cls.__module__], so it
    must be registered there before exec_module runs, or dataclass()
    raises AttributeError on a NoneType module lookup."""
    spec = importlib.util.spec_from_file_location("aura_v05343_portfolio_exposure_observability", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


MOD = _load_module()


# ---------------------------------------------------------------------------
# Fakes -- no network I/O, mirror the verified ccxt/alpaca-py shapes exactly.
# ---------------------------------------------------------------------------

def mexc_position(**overrides) -> dict:
    """A raw ccxt position dict (as returned by exchange.fetch_positions()),
    with the actual MEXC info sub-dict shape verified against ccxt 4.5.78's
    installed MexcClass.parse_position() source."""
    info = {
        "positionId": "1001",
        "symbol": "BTC_USDT",
        "positionType": "1",  # '1' == LONG per verified ccxt source
        "openType": "1",
        "state": "2",
        "holdVol": "2",
        "frozenVol": "0",
        "closeVol": "0",
        "holdAvgPrice": "50000",
        "openAvgPrice": "50000",
        "closeAvgPrice": "0",
        "liquidatePrice": "40000",
        "oim": "100",
        "im": "100",
        "holdFee": "0",
        "realised": "0",
        "leverage": "5",
        "createTime": "1700000000000",
        "updateTime": "1700000001000",
        "autoAddIm": False,
    }
    info.update(overrides.pop("info_overrides", {}))
    base = {"info": info, "symbol": "BTC/USDT:USDT", "notional": None, "markPrice": None, "unrealizedPnl": None}
    base.update(overrides)
    return base


class FakeMexcExchange:
    """Injectable stand-in for ccxt.mexc. Every method returns pre-canned
    data or raises to model a broker/network failure, matching this repo's
    established FakeExchange convention (see test_aura_v05328)."""

    def __init__(self, positions=None, balance=None, tickers=None, contract_sizes=None,
                 funding_rates=None, raise_on_positions=False, raise_on_balance=False,
                 raise_on_ticker_symbols=None, raise_on_funding_symbols=None,
                 markets=None):
        self._positions = positions if positions is not None else []
        self._balance = balance if balance is not None else {"USDT": {"total": 10000.0}}
        self._tickers = tickers or {}
        self._contract_sizes = contract_sizes or {}
        self._funding_rates = funding_rates or {}
        self.raise_on_positions = raise_on_positions
        self.raise_on_balance = raise_on_balance
        self.raise_on_ticker_symbols = raise_on_ticker_symbols or set()
        self.raise_on_funding_symbols = raise_on_funding_symbols or set()
        self._markets = markets or {}

    def fetch_positions(self):
        if self.raise_on_positions:
            raise ConnectionError("simulated MEXC outage")
        return self._positions

    def fetch_balance(self):
        if self.raise_on_balance:
            raise ConnectionError("simulated balance-fetch failure")
        return self._balance

    def market(self, symbol):
        if symbol in self._markets:
            return self._markets[symbol]
        if symbol in self._contract_sizes:
            return {"contractSize": self._contract_sizes[symbol]}
        return {}

    def fetch_ticker(self, symbol):
        if symbol in self.raise_on_ticker_symbols:
            raise ConnectionError("simulated ticker outage")
        if symbol in self._tickers:
            return self._tickers[symbol]
        return {}

    def fetch_funding_rate(self, symbol):
        if symbol in self.raise_on_funding_symbols:
            raise ConnectionError("simulated funding-rate outage")
        return self._funding_rates.get(symbol, {})


class FakeAlpacaPosition:
    def __init__(self, **overrides):
        base = dict(
            asset_id="asset-aapl-1", symbol="AAPL", side="long", qty="10",
            avg_entry_price="150.0", current_price="155.0", market_value="1550.0",
            unrealized_pl="50.0",
        )
        base.update(overrides)
        for k, v in base.items():
            setattr(self, k, v)


class FakeAlpacaClient:
    def __init__(self, equity="20000.0", positions=None, raise_on_account=False, raise_on_positions=False):
        self._equity = equity
        self._positions = positions if positions is not None else []
        self.raise_on_account = raise_on_account
        self.raise_on_positions = raise_on_positions

    def get_account(self):
        if self.raise_on_account:
            raise ConnectionError("simulated Alpaca account-fetch failure")
        return type("Account", (), {"equity": self._equity})()

    def get_all_positions(self):
        if self.raise_on_positions:
            raise ConnectionError("simulated Alpaca positions-fetch failure")
        return self._positions


# ---------------------------------------------------------------------------
# MEXC fetcher.
# ---------------------------------------------------------------------------

def test_fetch_mexc_portfolio_success_long_and_short():
    positions = [
        mexc_position(),  # LONG BTC
        mexc_position(info_overrides={"positionId": "1002", "symbol": "ETH_USDT", "positionType": "2",
                                       "holdVol": "10", "openAvgPrice": "3000", "leverage": "3",
                                       "liquidatePrice": "3600"}),
    ]
    ex = FakeMexcExchange(positions=positions, contract_sizes={"BTC_USDT": 0.0001, "ETH_USDT": 0.001},
                           tickers={"BTC_USDT": {"last": 51000}, "ETH_USDT": {"last": 2900}})
    records, status = MOD.fetch_mexc_portfolio(ex)
    assert status.status == "SUCCESS"
    assert status.positions_count == 2
    assert status.equity == 10000.0
    by_symbol = {r.symbol: r for r in records}
    assert by_symbol["BTC_USDT"].direction == "LONG"
    assert by_symbol["ETH_USDT"].direction == "SHORT"
    assert by_symbol["BTC_USDT"].leverage == 5.0
    assert by_symbol["BTC_USDT"].liquidation_price == 40000.0
    assert by_symbol["BTC_USDT"].notional_basis == "MARK_TO_MARKET"
    assert by_symbol["BTC_USDT"].notional_usd == 51000 * 2 * 0.0001
    # unrealized pnl sign: LONG, mark > entry -> positive
    assert by_symbol["BTC_USDT"].unrealized_pnl_usd > 0
    # SHORT, mark < entry -> positive pnl too (price fell in short's favor)
    assert by_symbol["ETH_USDT"].unrealized_pnl_usd > 0


def test_fetch_mexc_portfolio_zero_holdvol_excluded():
    positions = [mexc_position(info_overrides={"holdVol": "0"})]
    ex = FakeMexcExchange(positions=positions)
    records, status = MOD.fetch_mexc_portfolio(ex)
    assert records == []
    assert status.positions_count == 0
    assert status.status == "SUCCESS"


def test_fetch_mexc_portfolio_fails_closed_on_positions_exception():
    ex = FakeMexcExchange(raise_on_positions=True)
    records, status = MOD.fetch_mexc_portfolio(ex)
    assert records == []
    assert status.status == "FAILED"
    assert "ConnectionError" in status.error
    assert status.equity is None


def test_fetch_mexc_portfolio_equity_best_effort_does_not_fail_positions():
    """Broker returns positions fine but the balance call fails -- this is a
    partial/stale-data scenario: position data must still be reported, with
    equity honestly None, never fabricated as 0."""
    ex = FakeMexcExchange(positions=[mexc_position()], raise_on_balance=True,
                           contract_sizes={"BTC_USDT": 0.0001})
    records, status = MOD.fetch_mexc_portfolio(ex)
    assert status.status == "SUCCESS"
    assert len(records) == 1
    assert status.equity is None


def test_mexc_mark_and_notional_mark_to_market():
    ex = FakeMexcExchange(contract_sizes={"BTC_USDT": 0.0001}, tickers={"BTC_USDT": {"last": 52000}})
    mark, notional, basis = MOD._mexc_mark_and_notional(ex, "BTC_USDT", 2.0, 50000.0)
    assert mark == 52000
    assert notional == 52000 * 2.0 * 0.0001
    assert basis == "MARK_TO_MARKET"


def test_mexc_mark_and_notional_falls_back_to_entry_price_estimate():
    """Ticker fetch fails (stale/unavailable price feed) but contractSize is
    known and an entry price exists -- notional is an explicit ESTIMATE, not
    a silently-labeled mark-to-market figure."""
    ex = FakeMexcExchange(contract_sizes={"BTC_USDT": 0.0001}, raise_on_ticker_symbols={"BTC_USDT"})
    mark, notional, basis = MOD._mexc_mark_and_notional(ex, "BTC_USDT", 2.0, 50000.0)
    assert mark is None
    assert notional == 50000.0 * 2.0 * 0.0001
    assert basis == "ENTRY_PRICE_ESTIMATE"


def test_mexc_mark_and_notional_none_when_contract_size_unknown():
    """contractSize genuinely unavailable (market metadata missing) -- must
    never guess contractSize=1; notional stays honestly None."""
    ex = FakeMexcExchange(tickers={"BTC_USDT": {"last": 52000}})  # no contract_sizes configured
    mark, notional, basis = MOD._mexc_mark_and_notional(ex, "BTC_USDT", 2.0, 50000.0)
    assert notional is None
    assert basis is None


def test_fetch_mexc_funding_exposure_computable_and_not_computable():
    full_positions = [
        mexc_position(),
        mexc_position(info_overrides={"positionId": "1002", "symbol": "ETH_USDT", "positionType": "2",
                                       "holdVol": "10", "openAvgPrice": "3000"}),
    ]
    ex = FakeMexcExchange(positions=full_positions, contract_sizes={"BTC_USDT": 0.0001, "ETH_USDT": 0.001},
                           tickers={"BTC_USDT": {"last": 51000}, "ETH_USDT": {"last": 2900}},
                           funding_rates={"BTC_USDT": {"fundingRate": "0.0001"}},
                           raise_on_funding_symbols={"ETH_USDT"})
    all_records, _ = MOD.fetch_mexc_portfolio(ex)
    funding = MOD.fetch_mexc_funding_exposure(ex, all_records)
    assert funding["BTC_USDT"]["status"] == "COMPUTABLE"
    assert funding["BTC_USDT"]["basis_status"] == "NOT_COMPUTABLE"
    assert funding["ETH_USDT"]["status"] == "NOT_COMPUTABLE"
    assert "ConnectionError" in funding["ETH_USDT"]["reason"]


# ---------------------------------------------------------------------------
# Alpaca fetcher.
# ---------------------------------------------------------------------------

def test_fetch_alpaca_portfolio_success():
    positions = [FakeAlpacaPosition(), FakeAlpacaPosition(asset_id="asset-tsla-1", symbol="TSLA", side="short",
                                                            qty="-5", avg_entry_price="200.0", current_price="190.0",
                                                            market_value="-950.0", unrealized_pl="50.0")]
    client = FakeAlpacaClient(equity="20000.0", positions=positions)
    records, status = MOD.fetch_alpaca_portfolio(client)
    assert status.status == "SUCCESS"
    assert status.equity == 20000.0
    assert status.positions_count == 2
    by_symbol = {r.symbol: r for r in records}
    assert by_symbol["AAPL"].direction == "LONG"
    assert by_symbol["AAPL"].leverage is None  # never fabricated
    assert by_symbol["TSLA"].direction == "SHORT"
    assert by_symbol["TSLA"].notional_usd == 950.0
    assert by_symbol["TSLA"].quantity == 5.0  # abs()


def test_fetch_alpaca_portfolio_zero_qty_excluded():
    positions = [FakeAlpacaPosition(qty="0")]
    client = FakeAlpacaClient(positions=positions)
    records, status = MOD.fetch_alpaca_portfolio(client)
    assert records == []
    assert status.positions_count == 0


def test_fetch_alpaca_portfolio_fails_closed_on_account_exception():
    client = FakeAlpacaClient(raise_on_account=True)
    records, status = MOD.fetch_alpaca_portfolio(client)
    assert records == []
    assert status.status == "FAILED"
    assert status.equity is None


def test_fetch_alpaca_portfolio_fails_closed_on_positions_exception_but_equity_known():
    """Account call succeeds (equity known) but positions call fails -- a
    partial-data scenario: must fail closed (empty positions, FAILED status)
    rather than silently reporting zero positions as if that were true."""
    client = FakeAlpacaClient(equity="20000.0", raise_on_positions=True)
    records, status = MOD.fetch_alpaca_portfolio(client)
    assert records == []
    assert status.status == "FAILED"
    assert status.equity == 20000.0  # what WAS learned before the failure is preserved


# ---------------------------------------------------------------------------
# Cross-venue aggregation.
# ---------------------------------------------------------------------------

def test_build_portfolio_snapshot_not_configured_when_no_clients():
    snap = MOD.build_portfolio_snapshot()
    assert snap.positions == ()
    assert snap.venue_fetch_status["MEXC"].status == "NOT_CONFIGURED"
    assert snap.venue_fetch_status["ALPACA"].status == "NOT_CONFIGURED"
    # NOT_CONFIGURED venues don't count against completeness
    assert snap.is_complete is False  # no configured venues at all -> not complete (nothing to observe)


def test_build_portfolio_snapshot_both_venues_success_is_complete():
    mexc_ex = FakeMexcExchange(positions=[mexc_position()], contract_sizes={"BTC_USDT": 0.0001},
                                tickers={"BTC_USDT": {"last": 51000}})
    alpaca_client = FakeAlpacaClient(positions=[FakeAlpacaPosition()])
    snap = MOD.build_portfolio_snapshot(mexc_exchange=mexc_ex, alpaca_client=alpaca_client)
    assert snap.is_complete is True
    assert len(snap.positions) == 2
    venues_present = {p.venue for p in snap.positions}
    assert venues_present == {"MEXC", "ALPACA"}


def test_build_portfolio_snapshot_partial_failure_not_complete():
    """One venue fails, the other succeeds -- cross-venue aggregation must
    still surface the succeeding venue's real data (never dropped just
    because the other venue is stale/unreachable), while is_complete
    correctly reflects the partial state."""
    mexc_ex = FakeMexcExchange(raise_on_positions=True)
    alpaca_client = FakeAlpacaClient(positions=[FakeAlpacaPosition()])
    snap = MOD.build_portfolio_snapshot(mexc_exchange=mexc_ex, alpaca_client=alpaca_client)
    assert snap.is_complete is False
    assert snap.venue_fetch_status["MEXC"].status == "FAILED"
    assert snap.venue_fetch_status["ALPACA"].status == "SUCCESS"
    assert len(snap.positions) == 1
    assert snap.positions[0].venue == "ALPACA"


def test_build_portfolio_snapshot_one_configured_one_not_is_complete_if_success():
    """Only MEXC configured (Alpaca not passed at all) and it succeeds --
    NOT_CONFIGURED must not count against completeness."""
    mexc_ex = FakeMexcExchange(positions=[mexc_position()], contract_sizes={"BTC_USDT": 0.0001},
                                tickers={"BTC_USDT": {"last": 51000}})
    snap = MOD.build_portfolio_snapshot(mexc_exchange=mexc_ex)
    assert snap.is_complete is True
    assert snap.venue_fetch_status["ALPACA"].status == "NOT_CONFIGURED"


def test_positions_outside_intent_ledger_are_still_captured():
    """This module reads broker truth directly, with no reference to
    AURA's own intent ledger at all -- a position the broker reports that
    AURA's own ledger has no record of (e.g. manually opened, or opened by
    a process outside this session) must still appear in the snapshot.
    Simulated here via a raw_source_id that matches no known AURA
    client_order_id/positionId convention used anywhere else in this repo."""
    foreign_position = mexc_position(info_overrides={"positionId": "999999-manual-not-in-ledger"})
    ex = FakeMexcExchange(positions=[foreign_position], contract_sizes={"BTC_USDT": 0.0001},
                           tickers={"BTC_USDT": {"last": 51000}})
    records, status = MOD.fetch_mexc_portfolio(ex)
    assert status.status == "SUCCESS"
    assert len(records) == 1
    assert records[0].raw_source_id == "999999-manual-not-in-ledger"


def test_state_hash_reproducible_and_sensitive_to_content():
    mexc_ex = FakeMexcExchange(positions=[mexc_position()], contract_sizes={"BTC_USDT": 0.0001},
                                tickers={"BTC_USDT": {"last": 51000}})
    snap1 = MOD.build_portfolio_snapshot(mexc_exchange=mexc_ex)
    # Same as_of forced for a true content-only comparison.
    snap1b = MOD._build_snapshot(snap1.as_of, list(snap1.positions), snap1.venue_fetch_status)
    assert snap1.state_hash == snap1b.state_hash
    snap2 = MOD._build_snapshot(snap1.as_of, [], {"MEXC": MOD.VenueFetchStatus(
        venue="MEXC", status="SUCCESS", error=None, fetched_at=snap1.as_of, positions_count=0, equity=None)})
    assert snap1.state_hash != snap2.state_hash


# ---------------------------------------------------------------------------
# Equity history -- persisted, atomic read-modify-write.
# ---------------------------------------------------------------------------

def test_equity_history_round_trip_and_skips_none_equity():
    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td)
        assert MOD.load_equity_history(state_dir) == []
        status_ok = MOD.VenueFetchStatus(venue="MEXC", status="SUCCESS", error=None,
                                          fetched_at="2026-09-12T00:00:00+00:00", positions_count=1, equity=10000.0)
        status_no_equity = MOD.VenueFetchStatus(venue="ALPACA", status="SUCCESS", error=None,
                                                 fetched_at="2026-09-12T00:00:00+00:00", positions_count=0, equity=None)
        snap = MOD.PortfolioSnapshot(as_of="2026-09-12T00:00:00+00:00", positions=(),
                                      venue_fetch_status={"MEXC": status_ok, "ALPACA": status_no_equity},
                                      is_complete=True, state_hash="deadbeef")
        events = MOD.append_equity_history(state_dir, snap)
        assert len(events) == 1
        assert events[0]["venue"] == "MEXC"
        assert events[0]["equity"] == 10000.0
        # Persisted to disk, atomically -- read back independently.
        reloaded = MOD.load_equity_history(state_dir)
        assert reloaded == events
        raw = json.loads((state_dir / "equity_history.json").read_text())
        assert "state_hash" in raw


def test_equity_history_accumulates_across_multiple_appends():
    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td)
        for i, (eq, ts) in enumerate([(10000.0, "2026-09-12T00:00:00+00:00"), (10500.0, "2026-09-12T12:00:00+00:00")]):
            status_ok = MOD.VenueFetchStatus(venue="MEXC", status="SUCCESS", error=None,
                                              fetched_at=ts, positions_count=0, equity=eq)
            snap = MOD.PortfolioSnapshot(as_of=ts, positions=(), venue_fetch_status={"MEXC": status_ok},
                                          is_complete=True, state_hash=f"hash{i}")
            MOD.append_equity_history(state_dir, snap)
        events = MOD.load_equity_history(state_dir)
        assert len(events) == 2
        assert [e["equity"] for e in events] == [10000.0, 10500.0]


# ---------------------------------------------------------------------------
# Dimension computations.
# ---------------------------------------------------------------------------

def test_compute_daily_loss_not_computable_no_equity():
    result = MOD.compute_daily_loss([], "MEXC", None, "2026-09-12T12:00:00+00:00")
    assert result["status"] == "NOT_COMPUTABLE"


def test_compute_daily_loss_not_yet_available_no_prior_same_day():
    result = MOD.compute_daily_loss([], "MEXC", 10000.0, "2026-09-12T12:00:00+00:00")
    assert result["status"] == "NOT_YET_AVAILABLE"


def test_compute_daily_loss_computable_with_prior_snapshot():
    history = [{"venue": "MEXC", "equity": 10500.0, "as_of": "2026-09-12T00:00:00+00:00"}]
    result = MOD.compute_daily_loss(history, "MEXC", 10000.0, "2026-09-12T12:00:00+00:00")
    assert result["status"] == "COMPUTABLE"
    assert result["day_start_equity"] == 10500.0
    assert abs(result["daily_loss_pct"] - (500.0 / 10500.0)) < 1e-9


def test_compute_daily_loss_ignores_other_venues_and_future_entries():
    history = [
        {"venue": "ALPACA", "equity": 99999.0, "as_of": "2026-09-12T00:00:00+00:00"},
        {"venue": "MEXC", "equity": 10500.0, "as_of": "2026-09-12T00:00:00+00:00"},
        {"venue": "MEXC", "equity": 10600.0, "as_of": "2026-09-12T23:00:00+00:00"},  # after as_of, must be excluded
    ]
    result = MOD.compute_daily_loss(history, "MEXC", 10000.0, "2026-09-12T12:00:00+00:00")
    assert result["status"] == "COMPUTABLE"
    assert result["day_start_equity"] == 10500.0


def test_compute_max_drawdown_uses_peak_across_history_and_current():
    history = [
        {"venue": "MEXC", "equity": 12000.0, "as_of": "2026-09-10T00:00:00+00:00"},
        {"venue": "MEXC", "equity": 9000.0, "as_of": "2026-09-11T00:00:00+00:00"},
    ]
    result = MOD.compute_max_drawdown(history, "MEXC", 10000.0)
    assert result["status"] == "COMPUTABLE"
    assert result["peak_equity"] == 12000.0
    assert abs(result["drawdown_pct"] - (2000.0 / 12000.0)) < 1e-9


def test_compute_max_drawdown_no_history_uses_current_as_peak():
    result = MOD.compute_max_drawdown([], "MEXC", 10000.0)
    assert result["status"] == "COMPUTABLE"
    assert result["peak_equity"] == 10000.0
    assert result["drawdown_pct"] == 0.0


def _pos(venue="MEXC", symbol="BTC_USDT", direction="LONG", notional=1000.0, leverage=None):
    return MOD.PositionRecord(
        venue=venue, symbol=symbol, direction=direction, quantity=1.0, entry_price=100.0,
        leverage=leverage, mark_price=100.0, notional_usd=notional, notional_basis="MARK_TO_MARKET",
        unrealized_pnl_usd=0.0, liquidation_price=None, raw_source_id=None, as_of="2026-09-12T00:00:00+00:00",
    )


def test_compute_portfolio_heat_computable_with_partial_flag():
    positions = [_pos(notional=1000.0), _pos(symbol="ETH_USDT", notional=None)]
    result = MOD.compute_portfolio_heat(positions, 5000.0)
    assert result["status"] == "COMPUTABLE"
    assert result["total_abs_notional_usd"] == 1000.0
    assert result["heat_ratio"] == 0.2
    assert result["partial"] is True
    assert result["positions_excluded_no_notional"] == 1


def test_compute_portfolio_heat_not_computable_no_equity():
    result = MOD.compute_portfolio_heat([_pos()], None)
    assert result["status"] == "NOT_COMPUTABLE"


def test_compute_asset_concentration_proportions_sum_to_one():
    positions = [_pos(symbol="BTC_USDT", notional=3000.0), _pos(symbol="ETH_USDT", notional=1000.0)]
    result = MOD.compute_asset_concentration(positions)
    assert result["status"] == "COMPUTABLE"
    total_share = sum(result["by_symbol"].values())
    assert abs(total_share - 1.0) < 1e-9
    assert abs(result["by_symbol"]["MEXC:BTC_USDT"] - 0.75) < 1e-9


def test_compute_directional_exposure_net_long_short():
    positions = [_pos(direction="LONG", notional=3000.0), _pos(symbol="ETH_USDT", direction="SHORT", notional=1000.0)]
    result = MOD.compute_directional_exposure(positions, 5000.0)
    assert result["status"] == "COMPUTABLE"
    assert result["net_notional_usd"] == 2000.0
    assert abs(result["net_exposure_ratio"] - 0.4) < 1e-9


def test_compute_leverage_exposure_per_position_and_aggregate():
    positions = [_pos(venue="MEXC", notional=2000.0, leverage=5.0), _pos(venue="ALPACA", symbol="AAPL", notional=1000.0, leverage=None)]
    equity_by_venue = {"MEXC": 4000.0, "ALPACA": 10000.0}
    result = MOD.compute_leverage_exposure(positions, equity_by_venue)
    assert result["per_venue_aggregate"]["MEXC"]["status"] == "COMPUTABLE"
    assert result["per_venue_aggregate"]["MEXC"]["aggregate_leverage_ratio"] == 0.5
    leverages = {p["symbol"]: p["leverage"] for p in result["per_position"]}
    assert leverages["BTC_USDT"] == 5.0
    assert leverages["AAPL"] is None  # never fabricated


def test_check_mexc_leverage_cap_no_cap_configured_never_fabricates_verdict():
    positions = [_pos(venue="MEXC", leverage=12.0)]
    result = MOD.check_mexc_leverage_cap(positions, None)
    assert result["cap_configured"] is False
    assert result["any_position_exceeds_cap"] is None
    assert result["positions"][0]["leverage"] == 12.0
    assert "exceeds_cap" not in result["positions"][0]


def test_check_mexc_leverage_cap_with_cap_detects_breach():
    positions = [_pos(venue="MEXC", leverage=12.0), _pos(venue="MEXC", symbol="ETH_USDT", leverage=3.0)]
    result = MOD.check_mexc_leverage_cap(positions, 10.0)
    assert result["cap_configured"] is True
    assert result["any_position_exceeds_cap"] is True
    by_symbol = {p["symbol"]: p for p in result["positions"]}
    assert by_symbol["BTC_USDT"]["exceeds_cap"] is True
    assert by_symbol["ETH_USDT"]["exceeds_cap"] is False


def test_check_mexc_leverage_cap_ignores_non_mexc_positions():
    positions = [_pos(venue="ALPACA", symbol="AAPL", leverage=None)]
    result = MOD.check_mexc_leverage_cap(positions, 10.0)
    assert result["positions"] == []
    assert result["any_position_exceeds_cap"] is False


# ---------------------------------------------------------------------------
# Full dimension report + post-trade projection.
# ---------------------------------------------------------------------------

def _snapshot_for_dimensions():
    mexc_status = MOD.VenueFetchStatus(venue="MEXC", status="SUCCESS", error=None,
                                        fetched_at="2026-09-12T12:00:00+00:00", positions_count=1, equity=4000.0)
    alpaca_status = MOD.VenueFetchStatus(venue="ALPACA", status="SUCCESS", error=None,
                                          fetched_at="2026-09-12T12:00:00+00:00", positions_count=1, equity=10000.0)
    positions = [_pos(venue="MEXC", notional=2000.0, leverage=5.0), _pos(venue="ALPACA", symbol="AAPL", notional=1000.0)]
    return MOD._build_snapshot("2026-09-12T12:00:00+00:00", positions, {"MEXC": mexc_status, "ALPACA": alpaca_status})


def test_compute_exposure_dimensions_includes_static_not_computable_and_computed():
    snap = _snapshot_for_dimensions()
    report = MOD.compute_exposure_dimensions(snap, [], mexc_leverage_cap=10.0)
    for dim in MOD.NOT_COMPUTABLE_STATIC:
        assert report[dim]["status"] == "NOT_COMPUTABLE"
    assert report["portfolio_heat"]["status"] == "COMPUTABLE"
    assert report["asset_concentration"]["status"] == "COMPUTABLE"
    assert report["directional_exposure"]["status"] == "COMPUTABLE"
    assert report["leverage_exposure"]["status"] == "COMPUTABLE"
    assert report["mexc_leverage_cap_check"]["cap_configured"] is True
    assert report["daily_loss"]["MEXC"]["status"] == "NOT_YET_AVAILABLE"
    assert report["max_drawdown"]["MEXC"]["status"] == "COMPUTABLE"
    assert report["snapshot_is_complete"] is True


def test_project_post_trade_exposure_overlay_changes_heat_and_direction():
    snap = _snapshot_for_dimensions()
    hypothetical = _pos(venue="MEXC", symbol="ETH_USDT", direction="SHORT", notional=1000.0, leverage=4.0)
    result = MOD.project_post_trade_exposure(snap, [], hypothetical, mexc_leverage_cap=10.0)
    assert result["status"] == "COMPUTABLE"
    before_heat = result["before"]["portfolio_heat"]["heat_ratio"]
    after_heat = result["after"]["portfolio_heat"]["heat_ratio"]
    assert after_heat > before_heat
    before_dir = result["before"]["directional_exposure"]["net_notional_usd"]
    after_dir = result["after"]["directional_exposure"]["net_notional_usd"]
    assert after_dir < before_dir  # adding a SHORT pulls net notional down
    # the original snapshot must be unmutated by the projection
    assert len(snap.positions) == 2


def test_project_post_trade_exposure_does_not_mutate_original_snapshot_positions():
    snap = _snapshot_for_dimensions()
    original_len = len(snap.positions)
    hypothetical = _pos(venue="MEXC", symbol="SOL_USDT", notional=500.0)
    MOD.project_post_trade_exposure(snap, [], hypothetical)
    assert len(snap.positions) == original_len


# ---------------------------------------------------------------------------
# Persistence.
# ---------------------------------------------------------------------------

def test_persist_snapshot_writes_atomic_json_file():
    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td)
        snap = _snapshot_for_dimensions()
        report = MOD.compute_exposure_dimensions(snap, [])
        path = MOD.persist_snapshot(state_dir, snap, report)
        assert path.is_file()
        data = json.loads(path.read_text())
        assert data["state_hash"] == snap.state_hash
        assert "exposure_report" in data
        assert data["exposure_report"]["portfolio_heat"]["status"] == "COMPUTABLE"
        # no leftover .tmp file
        assert not any(p.name.endswith(".tmp") for p in path.parent.iterdir())


# ---------------------------------------------------------------------------
# Static guard: this module must remain observability-only.
# ---------------------------------------------------------------------------

def test_module_never_imports_or_calls_execution_or_promotion_modules():
    source = MODULE_PATH.read_text(encoding="utf-8")
    code_only = source.split('"""', 2)[-1]
    forbidden_substrings = [
        "aura_v05323_execution_specification_builder",
        "aura_v05327_mexc_execution_adapter",
        "aura_v05339_strategy_registry",
        "place_order(",
        "submit_order(",
        "cancel_order(",
        "record_evidence(",
    ]
    for token in forbidden_substrings:
        assert token not in code_only, f"observability module must not reference {token!r}"
