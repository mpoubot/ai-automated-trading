#!/usr/bin/env python3
"""Unit tests for AURA v0.5.3.74 (Options Market Data, Phase O2).

Uses plain top-level imports throughout (`import aura_v05374_options_
market_data as M`), never a second ad hoc importlib loader for a module
also reachable via a normal import -- this is deliberate, see O2's own
module docstring for why (the .338 duplicate-module-object hazard this
session diagnosed and fixed, 2026-10-05).

No real credentials, no network call anywhere in this file -- every
test injects a hand-built fake OptionsDataClient / fake snapshot
object exposing only the attribute names the production code actually
reads (duck-typing against the OptionsDataClient Protocol), never a
real alpaca-py SDK object.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

import aura_v05373_options_instrument_metadata as METADATA
import aura_v05374_options_market_data as M


# ============================================================================
# Fakes -- minimal duck-typed stand-ins for alpaca-py's Quote/Trade/
# OptionsGreeks/OptionsSnapshot, exposing only the attributes the
# production code (_quote_to_dict/_trade_to_dict/_greeks_to_dict/
# _classify_quote_status) actually reads.
# ============================================================================


class FakeQuote:
    def __init__(self, bid_price, ask_price, timestamp, bid_size=1, ask_size=1):
        self.bid_price = bid_price
        self.ask_price = ask_price
        self.bid_size = bid_size
        self.ask_size = ask_size
        self.timestamp = timestamp


class FakeTrade:
    def __init__(self, price, timestamp, size=1):
        self.price = price
        self.size = size
        self.timestamp = timestamp


class FakeGreeks:
    def __init__(self, delta=0.5, gamma=0.1, theta=-0.05, vega=0.2, rho=0.01):
        self.delta = delta
        self.gamma = gamma
        self.theta = theta
        self.vega = vega
        self.rho = rho


class FakeSnapshot:
    def __init__(self, latest_quote=None, latest_trade=None, implied_volatility=None, greeks=None):
        self.latest_quote = latest_quote
        self.latest_trade = latest_trade
        self.implied_volatility = implied_volatility
        self.greeks = greeks


class FakeAPIError(Exception):
    """Stand-in for alpaca.common.exceptions.APIError -- the production
    code only ever reads `.status_code` via getattr(), so a plain
    exception carrying that attribute is sufficient and keeps this test
    file independent of alpaca-py's exception internals."""

    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code


class FakeOptionsDataClient:
    """Implements the OptionsDataClient Protocol. `responses` maps
    underlying_symbol -> either a chain dict (occ_symbol -> FakeSnapshot)
    or an Exception instance to raise."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.requests_seen = []

    def get_option_chain(self, request):
        self.requests_seen.append(request)
        result = self.responses[request.underlying_symbol]
        if isinstance(result, Exception):
            raise result
        return result


NOW = datetime(2026, 10, 6, 15, 0, 0, tzinfo=timezone.utc)
EXP_GTE = date(2026, 10, 10)
EXP_LTE = date(2026, 11, 10)


def fresh_quote(age_seconds=5.0):
    return FakeQuote(bid_price=1.20, ask_price=1.25, timestamp=NOW - timedelta(seconds=age_seconds))


def occ(underlying="AAPL", expiry=date(2026, 10, 16), right="CALL", strike="150.00"):
    return METADATA.build_occ_symbol(underlying, expiry, right, strike)


def expect(name: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}: {name}")
    assert condition, name


def expect_raises(name: str, exc_type, fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except exc_type:
        print(f"PASS: {name}")
        return
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"{name}: expected {exc_type.__name__}, got {type(exc).__name__}: {exc}")
    raise AssertionError(f"{name}: expected {exc_type.__name__}, nothing raised")


# ============================================================================
# _classify_quote_status
# ============================================================================


def test_classify_quote_status_ok():
    quote = fresh_quote(age_seconds=5.0)
    status, age = M._classify_quote_status(quote, now=NOW, max_quote_age_seconds=60.0)
    expect("ok status", status == "OK")
    expect("ok age", age == pytest.approx(5.0))


def test_classify_quote_status_missing_quote():
    status, age = M._classify_quote_status(None, now=NOW, max_quote_age_seconds=60.0)
    expect("missing quote status", status == "MISSING_QUOTE")
    expect("missing quote age is None", age is None)


def test_classify_quote_status_invalid_quote_shapes():
    for bad in [
        FakeQuote(bid_price=None, ask_price=1.0, timestamp=NOW),
        FakeQuote(bid_price=1.0, ask_price=None, timestamp=NOW),
        FakeQuote(bid_price=0, ask_price=1.0, timestamp=NOW),
        FakeQuote(bid_price=1.0, ask_price=0, timestamp=NOW),
        FakeQuote(bid_price=2.0, ask_price=1.0, timestamp=NOW),  # ask < bid
    ]:
        status, _ = M._classify_quote_status(bad, now=NOW, max_quote_age_seconds=60.0)
        expect(f"invalid quote -> INVALID_QUOTE ({bad.bid_price},{bad.ask_price})", status == "INVALID_QUOTE")


def test_classify_quote_status_missing_timestamp():
    quote = FakeQuote(bid_price=1.0, ask_price=1.1, timestamp=None)
    status, age = M._classify_quote_status(quote, now=NOW, max_quote_age_seconds=60.0)
    expect("missing timestamp status", status == "MISSING_QUOTE_TIMESTAMP")
    expect("missing timestamp age is None", age is None)


def test_classify_quote_status_stale():
    quote = fresh_quote(age_seconds=120.0)
    status, age = M._classify_quote_status(quote, now=NOW, max_quote_age_seconds=60.0)
    expect("stale status", status == "STALE_QUOTE")
    expect("stale age", age == pytest.approx(120.0))


def test_classify_quote_status_future_timestamp():
    quote = FakeQuote(bid_price=1.0, ask_price=1.1, timestamp=NOW + timedelta(seconds=10))
    status, age = M._classify_quote_status(quote, now=NOW, max_quote_age_seconds=60.0)
    expect("future timestamp status", status == "FUTURE_TIMESTAMP")
    expect("future timestamp age is negative", age < 0)


def test_classify_quote_status_naive_timestamp_treated_as_utc():
    naive_now = NOW.replace(tzinfo=None)
    quote = FakeQuote(bid_price=1.0, ask_price=1.1, timestamp=naive_now - timedelta(seconds=3))
    status, age = M._classify_quote_status(quote, now=NOW, max_quote_age_seconds=60.0)
    expect("naive timestamp status OK", status == "OK")
    expect("naive timestamp age", age == pytest.approx(3.0))


# ============================================================================
# _quote_to_dict / _trade_to_dict / _greeks_to_dict
# ============================================================================


def test_quote_to_dict_none_and_populated():
    expect("none quote", M._quote_to_dict(None) is None)
    d = M._quote_to_dict(fresh_quote())
    expect("quote dict bid", d["bid_price"] == 1.20)
    expect("quote dict ask", d["ask_price"] == 1.25)
    expect("quote dict timestamp is iso string", isinstance(d["timestamp"], str))


def test_trade_to_dict_none_and_populated():
    expect("none trade", M._trade_to_dict(None) is None)
    d = M._trade_to_dict(FakeTrade(price=1.22, timestamp=NOW))
    expect("trade dict price", d["price"] == 1.22)
    expect("trade dict timestamp is iso string", isinstance(d["timestamp"], str))


def test_greeks_to_dict_none_and_populated():
    expect("none greeks", M._greeks_to_dict(None) is None)
    d = M._greeks_to_dict(FakeGreeks(delta=0.42))
    expect("greeks dict delta", d["delta"] == 0.42)
    expect("greeks dict has all five fields", set(d.keys()) == {"delta", "gamma", "theta", "vega", "rho"})


# ============================================================================
# fetch_option_chain -- validation failures
# ============================================================================


def test_fetch_option_chain_rejects_invalid_underlying():
    client = FakeOptionsDataClient({})
    expect_raises(
        "invalid underlying symbol",
        M.OptionsMarketDataError,
        M.fetch_option_chain, "", client=client,
        expiration_date_gte=EXP_GTE, expiration_date_lte=EXP_LTE, max_quote_age_seconds=60.0,
    )


def test_fetch_option_chain_rejects_inverted_expiration_window():
    client = FakeOptionsDataClient({})
    expect_raises(
        "inverted expiration window",
        M.OptionsMarketDataError,
        M.fetch_option_chain, "AAPL", client=client,
        expiration_date_gte=EXP_LTE, expiration_date_lte=EXP_GTE, max_quote_age_seconds=60.0,
    )


def test_fetch_option_chain_rejects_non_date_expiration():
    client = FakeOptionsDataClient({})
    expect_raises(
        "non-date expiration",
        M.OptionsMarketDataError,
        M.fetch_option_chain, "AAPL", client=client,
        expiration_date_gte="2026-10-10", expiration_date_lte=EXP_LTE, max_quote_age_seconds=60.0,
    )


def test_fetch_option_chain_rejects_invalid_max_quote_age():
    client = FakeOptionsDataClient({})
    for bad_age in [0, -5, "60", True]:
        expect_raises(
            f"invalid max_quote_age_seconds={bad_age!r}",
            M.OptionsMarketDataError,
            M.fetch_option_chain, "AAPL", client=client,
            expiration_date_gte=EXP_GTE, expiration_date_lte=EXP_LTE, max_quote_age_seconds=bad_age,
        )


def test_fetch_option_chain_rejects_inverted_strike_window():
    client = FakeOptionsDataClient({})
    expect_raises(
        "inverted strike window",
        M.OptionsMarketDataError,
        M.fetch_option_chain, "AAPL", client=client,
        expiration_date_gte=EXP_GTE, expiration_date_lte=EXP_LTE, max_quote_age_seconds=60.0,
        strike_price_gte=200.0, strike_price_lte=100.0,
    )


# ============================================================================
# fetch_option_chain -- feed entitlement
# ============================================================================


def test_fetch_option_chain_403_raises_not_entitled():
    client = FakeOptionsDataClient({"AAPL": FakeAPIError("forbidden", status_code=403)})
    expect_raises(
        "403 -> OptionsFeedNotEntitledError",
        M.OptionsFeedNotEntitledError,
        M.fetch_option_chain, "AAPL", client=client,
        expiration_date_gte=EXP_GTE, expiration_date_lte=EXP_LTE, max_quote_age_seconds=60.0, now=NOW,
    )


def test_fetch_option_chain_401_raises_not_entitled():
    client = FakeOptionsDataClient({"AAPL": FakeAPIError("unauthorized", status_code=401)})
    expect_raises(
        "401 -> OptionsFeedNotEntitledError",
        M.OptionsFeedNotEntitledError,
        M.fetch_option_chain, "AAPL", client=client,
        expiration_date_gte=EXP_GTE, expiration_date_lte=EXP_LTE, max_quote_age_seconds=60.0, now=NOW,
    )


def test_fetch_option_chain_500_raises_generic_not_entitlement():
    client = FakeOptionsDataClient({"AAPL": FakeAPIError("server error", status_code=500)})
    try:
        M.fetch_option_chain(
            "AAPL", client=client, expiration_date_gte=EXP_GTE, expiration_date_lte=EXP_LTE,
            max_quote_age_seconds=60.0, now=NOW,
        )
        raise AssertionError("500 should have raised")
    except M.OptionsFeedNotEntitledError:
        raise AssertionError("500 must NOT be classified as a feed-entitlement failure")
    except M.OptionsMarketDataError:
        print("PASS: 500 -> generic OptionsMarketDataError, not OptionsFeedNotEntitledError")


def test_fetch_option_chain_error_with_no_status_code_is_generic():
    client = FakeOptionsDataClient({"AAPL": RuntimeError("connection reset")})
    try:
        M.fetch_option_chain(
            "AAPL", client=client, expiration_date_gte=EXP_GTE, expiration_date_lte=EXP_LTE,
            max_quote_age_seconds=60.0, now=NOW,
        )
        raise AssertionError("should have raised")
    except M.OptionsFeedNotEntitledError:
        raise AssertionError("a plain network error must not be classified as an entitlement failure")
    except M.OptionsMarketDataError:
        print("PASS: no-status-code error -> generic OptionsMarketDataError")


# ============================================================================
# fetch_option_chain -- malformed / empty responses
# ============================================================================


def test_fetch_option_chain_rejects_non_dict_response():
    client = FakeOptionsDataClient({"AAPL": ["not", "a", "dict"]})
    expect_raises(
        "non-dict chain response",
        M.OptionsMarketDataError,
        M.fetch_option_chain, "AAPL", client=client,
        expiration_date_gte=EXP_GTE, expiration_date_lte=EXP_LTE, max_quote_age_seconds=60.0, now=NOW,
    )


def test_fetch_option_chain_rejects_empty_chain():
    client = FakeOptionsDataClient({"AAPL": {}})
    expect_raises(
        "empty chain",
        M.OptionsMarketDataError,
        M.fetch_option_chain, "AAPL", client=client,
        expiration_date_gte=EXP_GTE, expiration_date_lte=EXP_LTE, max_quote_age_seconds=60.0, now=NOW,
    )


# ============================================================================
# fetch_option_chain -- happy path
# ============================================================================


def test_fetch_option_chain_happy_path_builds_contract_and_carries_broker_fields():
    symbol = occ("AAPL", date(2026, 10, 16), "CALL", "150.00")
    snapshot = FakeSnapshot(
        latest_quote=fresh_quote(age_seconds=2.0),
        latest_trade=FakeTrade(price=1.23, timestamp=NOW),
        implied_volatility=0.31,
        greeks=FakeGreeks(delta=0.55),
    )
    client = FakeOptionsDataClient({"AAPL": {symbol: snapshot}})

    result = M.fetch_option_chain(
        "AAPL", client=client, expiration_date_gte=EXP_GTE, expiration_date_lte=EXP_LTE,
        max_quote_age_seconds=60.0, now=NOW,
    )

    expect("schema_version", result["schema_version"] == M.SCHEMA_VERSION)
    expect("underlying_symbol", result["underlying_symbol"] == "AAPL")
    expect("feed", result["feed"] == "OPRA")
    expect("contract_count", result["contract_count"] == 1)
    expect("anomaly_count", result["anomaly_count"] == 0)

    entry = result["contracts"][0]
    expect("contract occ_symbol", entry["contract"]["occ_symbol"] == symbol)
    # O1's OCC round-trip canonicalizes the strike through Decimal
    # (150.00 -> 150) -- already covered by O1's own test suite; compare
    # numerically here rather than assuming a specific string spelling.
    expect("contract strike", Decimal(entry["contract"]["strike"]) == Decimal("150.00"))
    expect("data_status OK", entry["data_status"] == "OK")
    expect("broker iv", entry["broker_implied_volatility"] == 0.31)
    expect("broker greeks delta", entry["broker_greeks"]["delta"] == 0.55)
    expect("broker quote bid", entry["broker_latest_quote"]["bid_price"] == 1.20)

    # Exactly one request was issued, with the OPRA feed and the exact
    # caller-supplied window -- never a silently-defaulted feed.
    expect("one request issued", len(client.requests_seen) == 1)
    from alpaca.data.enums import OptionsFeed
    expect("request used OPRA feed", client.requests_seen[0].feed == OptionsFeed.OPRA)


def test_fetch_option_chain_stale_quote_is_included_not_dropped():
    symbol = occ("AAPL", date(2026, 10, 16), "CALL", "150.00")
    snapshot = FakeSnapshot(latest_quote=fresh_quote(age_seconds=9999.0))
    client = FakeOptionsDataClient({"AAPL": {symbol: snapshot}})

    result = M.fetch_option_chain(
        "AAPL", client=client, expiration_date_gte=EXP_GTE, expiration_date_lte=EXP_LTE,
        max_quote_age_seconds=60.0, now=NOW,
    )
    expect("stale contract still present", result["contract_count"] == 1)
    expect("stale contract flagged", result["contracts"][0]["data_status"] == "STALE_QUOTE")


# ============================================================================
# fetch_option_chain -- anomaly handling (skip, don't fail the whole chain)
# ============================================================================


def test_fetch_option_chain_skips_unparseable_symbol_but_keeps_good_contracts():
    good_symbol = occ("AAPL", date(2026, 10, 16), "CALL", "150.00")
    client = FakeOptionsDataClient({
        "AAPL": {
            "NOT-AN-OCC-SYMBOL": FakeSnapshot(latest_quote=fresh_quote()),
            good_symbol: FakeSnapshot(latest_quote=fresh_quote()),
        }
    })
    result = M.fetch_option_chain(
        "AAPL", client=client, expiration_date_gte=EXP_GTE, expiration_date_lte=EXP_LTE,
        max_quote_age_seconds=60.0, now=NOW,
    )
    expect("one good contract kept", result["contract_count"] == 1)
    expect("one anomaly recorded", result["anomaly_count"] == 1)
    expect("anomaly reason mentions unparseable", "UNPARSEABLE_OCC_SYMBOL" in result["anomalies"][0]["reason"])


def test_fetch_option_chain_skips_underlying_mismatch():
    wrong_underlying_symbol = occ("MSFT", date(2026, 10, 16), "CALL", "150.00")
    good_symbol = occ("AAPL", date(2026, 10, 16), "CALL", "150.00")
    client = FakeOptionsDataClient({
        "AAPL": {
            wrong_underlying_symbol: FakeSnapshot(latest_quote=fresh_quote()),
            good_symbol: FakeSnapshot(latest_quote=fresh_quote()),
        }
    })
    result = M.fetch_option_chain(
        "AAPL", client=client, expiration_date_gte=EXP_GTE, expiration_date_lte=EXP_LTE,
        max_quote_age_seconds=60.0, now=NOW,
    )
    expect("one good contract kept despite mismatch", result["contract_count"] == 1)
    expect("mismatch recorded as anomaly", result["anomalies"][0]["reason"] == "UNDERLYING_MISMATCH")


def test_fetch_option_chain_skips_non_string_symbol_key():
    good_symbol = occ("AAPL", date(2026, 10, 16), "CALL", "150.00")
    client = FakeOptionsDataClient({
        "AAPL": {
            123: FakeSnapshot(latest_quote=fresh_quote()),
            good_symbol: FakeSnapshot(latest_quote=fresh_quote()),
        }
    })
    result = M.fetch_option_chain(
        "AAPL", client=client, expiration_date_gte=EXP_GTE, expiration_date_lte=EXP_LTE,
        max_quote_age_seconds=60.0, now=NOW,
    )
    expect("one good contract kept", result["contract_count"] == 1)
    expect("non-string key recorded as anomaly", result["anomalies"][0]["reason"] == "NON_STRING_SYMBOL_KEY")


def test_fetch_option_chain_all_anomalous_raises_no_usable_contracts():
    client = FakeOptionsDataClient({"AAPL": {"GARBAGE": FakeSnapshot()}})
    expect_raises(
        "all-anomalous chain raises",
        M.OptionsMarketDataError,
        M.fetch_option_chain, "AAPL", client=client,
        expiration_date_gte=EXP_GTE, expiration_date_lte=EXP_LTE, max_quote_age_seconds=60.0, now=NOW,
    )


# ============================================================================
# fetch_universe_option_chains
# ============================================================================


def test_fetch_universe_rejects_empty_symbol_list():
    client = FakeOptionsDataClient({})
    expect_raises(
        "empty symbol list",
        M.OptionsMarketDataError,
        M.fetch_universe_option_chains, client=client, symbols=[],
        expiration_date_gte=EXP_GTE, expiration_date_lte=EXP_LTE, max_quote_age_seconds=60.0,
    )


def test_fetch_universe_partial_failure_keeps_other_symbols():
    good_symbol = occ("AAPL", date(2026, 10, 16), "CALL", "150.00")
    client = FakeOptionsDataClient({
        "AAPL": {good_symbol: FakeSnapshot(latest_quote=fresh_quote())},
        "MSFT": {},  # empty chain -> OptionsMarketDataError for MSFT only
    })
    result = M.fetch_universe_option_chains(
        client=client, symbols=["AAPL", "MSFT"],
        expiration_date_gte=EXP_GTE, expiration_date_lte=EXP_LTE, max_quote_age_seconds=60.0, now=NOW,
    )
    expect("AAPL fetched", result["fetched_count"] == 1)
    expect("MSFT failed", result["failed_count"] == 1)
    expect("AAPL present in chains", "AAPL" in result["chains"])
    expect("MSFT present in fetch_errors", "MSFT" in result["fetch_errors"])


def test_fetch_universe_entitlement_failure_aborts_whole_batch():
    good_symbol = occ("AAPL", date(2026, 10, 16), "CALL", "150.00")
    client = FakeOptionsDataClient({
        "AAPL": FakeAPIError("forbidden", status_code=403),
        "MSFT": {good_symbol: FakeSnapshot(latest_quote=fresh_quote())},
    })
    expect_raises(
        "entitlement failure propagates and aborts the batch",
        M.OptionsFeedNotEntitledError,
        M.fetch_universe_option_chains, client=client, symbols=["AAPL", "MSFT"],
        expiration_date_gte=EXP_GTE, expiration_date_lte=EXP_LTE, max_quote_age_seconds=60.0, now=NOW,
    )


# ============================================================================
# fetch_pinned_universe_option_chains -- exercises the real .351
# cross-module import (staged alongside .373/.374 in this sandbox), not
# a mock, proving the real dependency-resolution path works end to end.
# ============================================================================


def test_fetch_pinned_universe_option_chains_uses_real_351_universe():
    universe_module = M._load_pinned_universe_module()
    pinned = universe_module.load_pinned_universe()
    expect("pinned universe non-empty", len(pinned.symbols) > 0)

    # Build a chain response for every pinned symbol so the batch fetch
    # succeeds end to end without needing the whole real universe to be
    # exercised in detail -- that's already covered by the
    # fetch_universe_option_chains tests above.
    responses = {}
    for sym in pinned.symbols:
        symbol = occ(sym, date(2026, 10, 16), "CALL", "150.00")
        responses[sym] = {symbol: FakeSnapshot(latest_quote=fresh_quote())}
    client = FakeOptionsDataClient(responses)

    result = M.fetch_pinned_universe_option_chains(
        client=client, expiration_date_gte=EXP_GTE, expiration_date_lte=EXP_LTE,
        max_quote_age_seconds=60.0, now=NOW,
    )
    expect("pinned universe fetch_count matches", result["fetched_count"] == len(pinned.symbols))
    expect("pinned universe failed_count is zero", result["failed_count"] == 0)
    expect("universe_size matches", result["universe_size"] == len(pinned.symbols))
