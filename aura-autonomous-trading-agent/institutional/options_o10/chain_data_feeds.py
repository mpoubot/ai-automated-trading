#!/usr/bin/env python3
"""AURA O10 -- Options Chain Data Feed Protocol + Synthetic Test Fixture.

WHAT THIS MODULE IS
------------------------------------------------------------------------
O10 (strategy signal mapping) needs options-chain data -- available
expiries/strikes for an underlying, each with a bid/ask/open_interest/
volume and (ideally) a delta -- to pick a defined-risk spread's strikes.
No such module exists upstream yet: O2 ("real market-data fetch", per
the Options build's own phase numbering referenced in `.373`'s and
`.376`'s docstrings) has not been built, and O3 (a Greeks engine) has
not been built either. Rather than block O10's entire design on two
unbuilt phases, this module defines the SHAPE O10 needs as a
`typing.Protocol` -- `OptionsChainProvider` -- so:

  1. O10's own logic (`signal_mapping.py`, `execution_constraints.py`)
     is written and tested against that shape today, independent of
     which concrete provider eventually implements it;
  2. when O2 (a real broker/vendor chain fetch) is built, it only needs
     to satisfy this Protocol's method signature to slot in beneath
     O10 unchanged -- no O10 code needs to change;
  3. in the meantime, `SyntheticFixtureOptionsChain` below gives O10's
     own test suite (`test_o10.py`) a deterministic, controllable,
     clearly-labeled-as-fake chain to test against, so the test suite
     documents and freezes what O10's logic does, not what a particular
     mocked return value happened to contain.

Every provider -- real or synthetic -- is REQUIRED to carry two class
attributes, checked on every provider this module or O10 constructs:
`DATA_SOURCE_LABEL` (a short human string identifying where data came
from, e.g. `"ALPACA_OPTIONS_CHAIN"` or `"SYNTHETIC_FIXTURE"`) and
`IS_REAL_MARKET_DATA` (bool). Any caller receiving a provider instance
can inspect these two attributes directly to know, without guessing,
whether a given run used real or synthetic data -- the same disclosure
discipline the rest of the AURA options build uses for CLAIMED vs
DERIVED vs OBSERVED data (see `signal_mapping.py`'s module docstring).

DELTA IS OPTIONAL -- NO GREEKS ENGINE (O3) EXISTS YET
------------------------------------------------------------------------
A real chain-data vendor response (Alpaca's options chain snapshot
endpoint, at the time this module was written) may or may not carry a
usable per-contract delta -- and even when a vendor DOES supply one,
O3 (AURA's own Greeks engine, used to independently recompute/validate
a vendor-supplied Greek) has not been built, so there is no
independent cross-check available for a delta this module receives.
`ContractQuote.delta` is therefore typed `float | None` everywhere in
this module, and every provider (real or synthetic) is allowed to
return `None` for it on any contract.

**Documented choice**: O10 (`signal_mapping.py`) FAILS CLOSED when
delta is unavailable for the contracts it needs, rather than falling
back to a price-only strike-selection heuristic (e.g. "N% OTM by
price"). A delta-targeted strike (e.g. "sell the ~0.20-delta put") is
the entire strategic basis for this structure menu's risk/reward
profile; silently substituting a price-distance heuristic would change
the strategy's real risk without any caller asking for that change.
See `signal_mapping.py`'s module docstring for exactly where and how
this fail-closed behavior fires.

What this module deliberately does NOT do
------------------------------------------------------------------------
No network call anywhere in this file (the synthetic fixture is pure
arithmetic; a real provider implementing this Protocol would live in
its own module, not here). No pricing model of record -- the synthetic
fixture's premium/delta approximation is explicitly NOT Black-Scholes,
just an internally-consistent, deterministic, monotonic-by-strike
stand-in good enough to exercise O10's selection logic in tests. No
modification to any existing AURA file.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, ClassVar, Protocol, runtime_checkable

VERSION = "AURA_O10 v1"
ENGINE = "OPTIONS_CHAIN_DATA_FEEDS"


# ============================================================================
# The shape O10 depends on. A plain dict (not a frozen dataclass) is used
# for each contract record so a real future provider can return exactly
# whatever extra vendor fields it wants alongside these required keys
# without needing to subclass anything here.
# ============================================================================

REQUIRED_CONTRACT_KEYS: frozenset[str] = frozenset({
    "underlying_symbol", "expiry", "strike", "right",
    "bid", "ask", "open_interest", "volume", "delta",
})


@runtime_checkable
class OptionsChainProvider(Protocol):
    """Structural type every chain-data source (real or synthetic) must
    satisfy for O10 to consume it. `isinstance(x, OptionsChainProvider)`
    works at runtime (via `@runtime_checkable`) and checks for the
    presence of `fetch_chain` plus the two disclosure attributes -- it
    does NOT check `fetch_chain`'s return shape; callers that need that
    checked should call `validate_chain_contracts()` below on the
    result.
    """

    DATA_SOURCE_LABEL: ClassVar[str]
    IS_REAL_MARKET_DATA: ClassVar[bool]

    def fetch_chain(
        self,
        underlying_symbol: str,
        *,
        dte_min: int,
        dte_max: int,
        now: datetime,
    ) -> list[dict[str, Any]]:
        """Return every known contract (both CALL and PUT, every
        expiry whose DTE as of `now` falls in `[dte_min, dte_max]`
        inclusive) for `underlying_symbol`. Each returned dict MUST
        carry every key in `REQUIRED_CONTRACT_KEYS`:

          underlying_symbol: str
          expiry:            date
          strike:            float  (dollars)
          right:              "CALL" | "PUT"
          bid:                float  (dollars, > 0)
          ask:                float  (dollars, >= bid)
          open_interest:      int    (>= 0)
          volume:             int    (>= 0, PRIOR trading day's volume)
          delta:              float | None  (signed: CALL in [0, 1],
                               PUT in [-1, 0]; None when unavailable --
                               see module docstring)

        May return an empty list (no contracts found / underlying not
        optionable / no expiry in range) -- callers must treat an empty
        list as "no data", never raise on it themselves.
        """
        ...


def validate_chain_contracts(contracts: Any) -> tuple[bool, list[str]]:
    """Defensive shape check any O10 caller MAY run over a provider's
    `fetch_chain()` result before trusting it (not required by the
    Protocol itself, which only checks method presence). Returns
    `(True, [])` on a well-formed list (possibly empty); otherwise
    `(False, [problem, ...])`."""
    if not isinstance(contracts, list):
        return False, ["CONTRACTS_NOT_A_LIST"]
    problems: list[str] = []
    for index, record in enumerate(contracts):
        if not isinstance(record, dict):
            problems.append(f"CONTRACT_{index}_NOT_A_DICT")
            continue
        missing = REQUIRED_CONTRACT_KEYS - set(record.keys())
        if missing:
            problems.append(f"CONTRACT_{index}_MISSING_KEYS:{sorted(missing)!r}")
            continue
        if record["right"] not in ("CALL", "PUT"):
            problems.append(f"CONTRACT_{index}_INVALID_RIGHT:{record['right']!r}")
        if not isinstance(record["expiry"], date):
            problems.append(f"CONTRACT_{index}_INVALID_EXPIRY:{record['expiry']!r}")
        delta = record["delta"]
        if delta is not None and not isinstance(delta, (int, float)):
            problems.append(f"CONTRACT_{index}_INVALID_DELTA:{delta!r}")
    return (len(problems) == 0), problems


# ============================================================================
# Synthetic fixture -- IS_REAL_MARKET_DATA = False, always. Pure
# arithmetic, deterministic for a given seed of constructor parameters
# (no randomness anywhere), internally consistent (monotonic delta by
# strike, bid <= mid <= ask, wider spreads / thinner OI farther OTM).
# ============================================================================


@dataclass(frozen=True, slots=True)
class SyntheticFixtureOptionsChain:
    """A deterministic, parameterizable fake options chain for tests.

    NOT a pricing model of record -- see module docstring. `delta` is
    approximated via a logistic function of normalized moneyness
    (mirrors the qualitative shape of a Black-Scholes N(d1) term: 0.5
    at-the-money, -> 1.0 deep ITM call / -> 0.0 deep OTM call, monotonic
    in strike), and put delta is derived from the same call-delta
    figure via put-call delta parity (`delta_put = delta_call - 1`) so
    both rights stay mutually consistent. Premiums are a simple
    intrinsic-plus-time-value approximation using that same delta-shape
    term, never claimed to match a real quoted price.

    Set `include_delta=False` to produce a chain with every contract's
    `delta` forced to `None`, exercising O10's documented fail-closed
    path when delta data is unavailable.
    """

    DATA_SOURCE_LABEL: ClassVar[str] = "SYNTHETIC_FIXTURE"
    IS_REAL_MARKET_DATA: ClassVar[bool] = False

    spot_price: float
    iv_guess: float = 0.30  # flat IV assumption across strikes/expiries -- a simplification, not a smile model
    dte_offsets: tuple[int, ...] = (28, 37, 45, 52, 60)  # candidate expiry DTEs generated relative to `now`
    strike_increment: float = 5.0
    strike_range_pct: float = 0.35  # generate strikes from spot*(1-range) to spot*(1+range)
    include_delta: bool = True
    base_open_interest: int = 800
    base_volume: int = 150
    base_spread_pct_of_mid: float = 0.06  # ATM spread as a fraction of mid; widens farther OTM

    def _strikes(self) -> list[float]:
        low = self.spot_price * (1 - self.strike_range_pct)
        high = self.spot_price * (1 + self.strike_range_pct)
        start = math.floor(low / self.strike_increment) * self.strike_increment
        strikes: list[float] = []
        strike = start
        while strike <= high:
            if strike > 0:
                strikes.append(round(strike, 2))
            strike += self.strike_increment
        return strikes

    def _call_delta(self, strike: float, dte_days: int) -> float:
        """Logistic approximation of a call's delta -- see class
        docstring. Monotonically decreasing in strike (confirmed by
        `test_o10.py`)."""
        years = max(dte_days, 1) / 365.0
        scale = max(self.spot_price * self.iv_guess * math.sqrt(years), 0.01)
        moneyness = (strike - self.spot_price) / scale
        return 1.0 / (1.0 + math.exp(moneyness))

    def _mid_price(self, strike: float, right: str, dte_days: int, call_delta: float) -> float:
        years = max(dte_days, 1) / 365.0
        if right == "CALL":
            intrinsic = max(self.spot_price - strike, 0.0)
            shape = call_delta
        else:
            intrinsic = max(strike - self.spot_price, 0.0)
            shape = call_delta  # put-call delta parity: (1+delta_put) == delta_call
        time_value = self.spot_price * self.iv_guess * math.sqrt(years) * shape * (1 - shape) * 4.0
        mid = intrinsic + time_value
        return max(round(mid, 2), 0.05)

    def fetch_chain(
        self,
        underlying_symbol: str,
        *,
        dte_min: int,
        dte_max: int,
        now: datetime,
    ) -> list[dict[str, Any]]:
        as_of_date = now.date()
        strikes = self._strikes()
        contracts: list[dict[str, Any]] = []

        for offset in self.dte_offsets:
            expiry = as_of_date + timedelta(days=offset)
            dte_days = (expiry - as_of_date).days
            if not (dte_min <= dte_days <= dte_max):
                continue

            for strike in strikes:
                call_delta = self._call_delta(strike, dte_days)
                moneyness_abs = abs(strike - self.spot_price) / max(self.spot_price, 0.01)
                decay = math.exp(-3.0 * moneyness_abs)  # ATM-centered liquidity decay
                open_interest = max(int(self.base_open_interest * decay), 0)
                volume = max(int(self.base_volume * decay), 0)
                spread_pct = self.base_spread_pct_of_mid * (1.0 + 2.0 * moneyness_abs)

                for right, call_delta_term in (("CALL", call_delta), ("PUT", call_delta)):
                    mid = self._mid_price(strike, right, dte_days, call_delta_term)
                    half_spread = max(mid * spread_pct / 2.0, 0.01)
                    bid = round(max(mid - half_spread, 0.01), 2)
                    ask = round(mid + half_spread, 2)
                    if right == "CALL":
                        delta = round(call_delta, 4)
                    else:
                        delta = round(call_delta - 1.0, 4)  # put-call parity

                    contracts.append({
                        "underlying_symbol": underlying_symbol,
                        "expiry": expiry,
                        "strike": strike,
                        "right": right,
                        "bid": bid,
                        "ask": ask,
                        "open_interest": open_interest,
                        "volume": volume,
                        "delta": delta if self.include_delta else None,
                    })

        return contracts
