#!/usr/bin/env python3
"""AURA v0.5.3.75 -- Options Greeks Engine (Phase O3).

An isolated, independently-derived options pricing and Greeks module --
delta, gamma, theta, vega, rho -- computed from a Cox-Ross-Rubinstein (CRR)
binomial tree, American-style (early exercise allowed at every node).

Design choices made explicitly with Martin (2026-10-06, AskUserQuestion):
  1. Pricing model: BINOMIAL TREE (CRR), not Black-Scholes -- these are
     American-style equity/ETF options, and the binomial model prices
     early-exercise value properly (Black-Scholes does not).
  2. Implied volatility INPUT: this module does NOT solve for IV itself.
     It takes `.374`'s (options market data) vendor-reported implied
     volatility as a direct input to the pricing formulas. "Independently
     derived" here refers to the pricing/Greeks MATH, not to re-deriving
     IV from a raw market price.
  3. Output: this module returns ONLY its own computed Greeks. It does
     NOT carry or compare against Alpaca's vendor-reported Greeks --
     that comparison, if ever wanted, is a later consumer's job.

This module is deliberately kept ISOLATED, the same way `aura_v054_atr.py`
is kept isolated from the rest of the pipeline: it has ZERO imports of any
other `aura_v05NNN_*` module, takes only plain scalar inputs, and performs
no I/O, no clock reads, and no network calls. A caller (a future phase)
is responsible for supplying `time_to_expiry_years` (computed from a
contract's expiry date and a wall-clock "as of" time) and for sourcing
`implied_volatility` from `.374`.

Units / sign conventions (documented explicitly since these vary by
convention across the industry):
  - delta: price change per $1 move in the underlying. CALL in [0, 1],
    PUT in [-1, 0].
  - gamma: change in delta per $1 move in the underlying. Always >= 0
    for a vanilla option (long or short is the caller's concern, not
    this module's -- these are per-contract, long-option Greeks).
  - theta: price change per ONE CALENDAR DAY of time passing (not per
    year). Typically negative for a long option position (time decay).
  - vega: price change per ONE PERCENTAGE POINT of implied volatility
    (e.g. IV moving from 30% to 31%), not per unit (100 percentage
    points) of volatility.
  - rho: price change per ONE PERCENTAGE POINT of the risk-free rate
    (e.g. r moving from 4% to 5%), not per unit (100 percentage points)
    of rate.

Validation strategy (see the test file): rather than relying on a
remembered decimal answer from a specific textbook worked example, the
test suite implements the standard Black-Scholes closed-form formula
independently (as a second, completely separate pricing path) and checks
(a) convergence of this module's American binomial price toward the
Black-Scholes European price in the regime where the two are
mathematically guaranteed to be equal (calls on a non-dividend-paying
underlying, where early exercise is never optimal), and (b) a battery of
model-independent, no-arbitrage structural properties (price >= intrinsic
value, American price >= the corresponding European price, delta/gamma/
vega sign and bound checks, numerical-stability-under-refinement checks).
"""
from __future__ import annotations

import math
from typing import Optional

RIGHTS = {"CALL", "PUT"}

# Numerical-differentiation step sizes for vega/rho (bump-and-reprice via
# central difference). These are implementation details of the finite-
# difference method, not business-logic thresholds -- small enough that
# the central-difference approximation of dPrice/dSigma and dPrice/dRate
# is accurate to several significant figures for any realistic input,
# large enough to stay well clear of floating-point noise accumulated
# across a many-step tree.
_EPS_SIGMA = 1.0e-4
_EPS_RATE = 1.0e-4

# Calendar-day convention used to convert the tree's natural "per year"
# theta into the more commonly useful "per day" figure.
_DAYS_PER_YEAR = 365.0


def fail(reason: str, **extra: object) -> dict:
    return {"status": "INVALID_INPUT", "reason": reason, **extra}


def _validate_inputs(
    *,
    spot: float,
    strike: float,
    time_to_expiry_years: float,
    implied_volatility: float,
    option_type: str,
    num_steps: int,
) -> Optional[dict]:
    if not isinstance(option_type, str) or option_type not in RIGHTS:
        return fail("INVALID_OPTION_TYPE", option_type=option_type)
    if not (isinstance(spot, (int, float)) and math.isfinite(spot) and spot > 0):
        return fail("SPOT_MUST_BE_POSITIVE_FINITE", spot=spot)
    if not (isinstance(strike, (int, float)) and math.isfinite(strike) and strike > 0):
        return fail("STRIKE_MUST_BE_POSITIVE_FINITE", strike=strike)
    if not (isinstance(time_to_expiry_years, (int, float)) and math.isfinite(time_to_expiry_years)
            and time_to_expiry_years > 0):
        return fail("TIME_TO_EXPIRY_MUST_BE_POSITIVE_FINITE", time_to_expiry_years=time_to_expiry_years)
    if not (isinstance(implied_volatility, (int, float)) and math.isfinite(implied_volatility)
            and implied_volatility > 0):
        return fail("IMPLIED_VOLATILITY_MUST_BE_POSITIVE_FINITE", implied_volatility=implied_volatility)
    if implied_volatility <= 2 * _EPS_SIGMA:
        return fail(
            "IMPLIED_VOLATILITY_TOO_SMALL_FOR_VEGA_FINITE_DIFFERENCE",
            implied_volatility=implied_volatility,
        )
    if not isinstance(num_steps, int) or num_steps < 2:
        return fail("NUM_STEPS_MUST_BE_INTEGER_AT_LEAST_2", num_steps=num_steps)
    return None


def _tree_price(
    *,
    spot: float,
    strike: float,
    time_to_expiry_years: float,
    risk_free_rate: float,
    dividend_yield: float,
    volatility: float,
    option_type: str,
    num_steps: int,
    want_snapshots: bool = False,
) -> Optional[dict]:
    """Backward-induction CRR binomial tree. Returns None if the supplied
    parameters produce a numerically pathological (out-of-[0,1]) risk-
    neutral probability for this step count -- the caller must fail
    closed on that, never silently proceed."""
    dt = time_to_expiry_years / num_steps
    up = math.exp(volatility * math.sqrt(dt))
    down = 1.0 / up
    discount = math.exp(-risk_free_rate * dt)
    growth = math.exp((risk_free_rate - dividend_yield) * dt)
    prob_up = (growth - down) / (up - down)
    if not (0.0 < prob_up < 1.0) or not math.isfinite(prob_up):
        return None
    prob_down = 1.0 - prob_up

    def intrinsic(price: float) -> float:
        return max(price - strike, 0.0) if option_type == "CALL" else max(strike - price, 0.0)

    n = num_steps
    prices = [spot * (up ** (n - i)) * (down ** i) for i in range(n + 1)]
    values = [intrinsic(p) for p in prices]

    snapshot_step1 = None
    snapshot_step2 = None

    step = n
    while step > 0:
        step -= 1
        new_prices = [spot * (up ** (step - i)) * (down ** i) for i in range(step + 1)]
        new_values = []
        for i in range(step + 1):
            continuation = discount * (prob_up * values[i] + prob_down * values[i + 1])
            new_values.append(max(continuation, intrinsic(new_prices[i])))
        prices, values = new_prices, new_values
        if want_snapshots and step == 2:
            snapshot_step2 = (list(prices), list(values))
        if want_snapshots and step == 1:
            snapshot_step1 = (list(prices), list(values))

    return {
        "price": values[0],
        "dt": dt,
        "step1": snapshot_step1,
        "step2": snapshot_step2,
    }


def price_and_greeks(
    *,
    spot: float,
    strike: float,
    time_to_expiry_years: float,
    risk_free_rate: float,
    dividend_yield: float,
    implied_volatility: float,
    option_type: str,
    num_steps: int,
) -> dict:
    """Price an American-style option and compute its Greeks from a single
    CRR binomial tree build (delta/gamma/theta are read directly off the
    tree's own nodes at zero extra cost; vega/rho need two extra tree
    builds each, via central-difference bump-and-reprice).

    All of risk_free_rate, dividend_yield, and num_steps are REQUIRED,
    caller-supplied parameters -- this module invents no default discount
    rate, no default dividend assumption, and no default tree resolution.
    """
    bad = _validate_inputs(
        spot=spot, strike=strike, time_to_expiry_years=time_to_expiry_years,
        implied_volatility=implied_volatility, option_type=option_type, num_steps=num_steps,
    )
    if bad is not None:
        return bad
    if not (isinstance(risk_free_rate, (int, float)) and math.isfinite(risk_free_rate)):
        return fail("RISK_FREE_RATE_MUST_BE_FINITE", risk_free_rate=risk_free_rate)
    if not (isinstance(dividend_yield, (int, float)) and math.isfinite(dividend_yield)):
        return fail("DIVIDEND_YIELD_MUST_BE_FINITE", dividend_yield=dividend_yield)

    base = _tree_price(
        spot=spot, strike=strike, time_to_expiry_years=time_to_expiry_years,
        risk_free_rate=risk_free_rate, dividend_yield=dividend_yield,
        volatility=implied_volatility, option_type=option_type, num_steps=num_steps,
        want_snapshots=True,
    )
    if base is None:
        return fail("RISK_NEUTRAL_PROBABILITY_OUT_OF_BOUNDS", num_steps=num_steps)

    price = base["price"]
    dt = base["dt"]
    s1_prices, s1_values = base["step1"]
    s2_prices, s2_values = base["step2"]

    # Delta from step 1 (up vs down node).
    delta = (s1_values[0] - s1_values[1]) / (s1_prices[0] - s1_prices[1])

    # Gamma from step 2 (two sub-deltas spanning the up/mid and mid/down pairs).
    delta_upper = (s2_values[0] - s2_values[1]) / (s2_prices[0] - s2_prices[1])
    delta_lower = (s2_values[1] - s2_values[2]) / (s2_prices[1] - s2_prices[2])
    gamma = (delta_upper - delta_lower) / (0.5 * (s2_prices[0] - s2_prices[2]))

    # Theta via the CRR "free theta" identity: the middle step-2 node sits
    # at exactly the original spot price (u*d == 1), two steps (2*dt)
    # earlier in time -- no extra tree build needed.
    theta_per_year = (s2_values[1] - price) / (2.0 * dt)
    theta_per_day = theta_per_year / _DAYS_PER_YEAR

    # Vega: central-difference bump-and-reprice on implied_volatility.
    vega_up = _tree_price(
        spot=spot, strike=strike, time_to_expiry_years=time_to_expiry_years,
        risk_free_rate=risk_free_rate, dividend_yield=dividend_yield,
        volatility=implied_volatility + _EPS_SIGMA, option_type=option_type, num_steps=num_steps,
    )
    vega_down = _tree_price(
        spot=spot, strike=strike, time_to_expiry_years=time_to_expiry_years,
        risk_free_rate=risk_free_rate, dividend_yield=dividend_yield,
        volatility=implied_volatility - _EPS_SIGMA, option_type=option_type, num_steps=num_steps,
    )
    if vega_up is None or vega_down is None:
        return fail("RISK_NEUTRAL_PROBABILITY_OUT_OF_BOUNDS_DURING_VEGA_BUMP", num_steps=num_steps)
    raw_vega = (vega_up["price"] - vega_down["price"]) / (2.0 * _EPS_SIGMA)
    vega_per_vol_point = raw_vega * 0.01

    # Rho: central-difference bump-and-reprice on risk_free_rate.
    rho_up = _tree_price(
        spot=spot, strike=strike, time_to_expiry_years=time_to_expiry_years,
        risk_free_rate=risk_free_rate + _EPS_RATE, dividend_yield=dividend_yield,
        volatility=implied_volatility, option_type=option_type, num_steps=num_steps,
    )
    rho_down = _tree_price(
        spot=spot, strike=strike, time_to_expiry_years=time_to_expiry_years,
        risk_free_rate=risk_free_rate - _EPS_RATE, dividend_yield=dividend_yield,
        volatility=implied_volatility, option_type=option_type, num_steps=num_steps,
    )
    if rho_up is None or rho_down is None:
        return fail("RISK_NEUTRAL_PROBABILITY_OUT_OF_BOUNDS_DURING_RHO_BUMP", num_steps=num_steps)
    raw_rho = (rho_up["price"] - rho_down["price"]) / (2.0 * _EPS_RATE)
    rho_per_rate_point = raw_rho * 0.01

    return {
        "status": "OK",
        "model": "CRR_BINOMIAL_AMERICAN",
        "num_steps": num_steps,
        "theoretical_price": price,
        "delta": delta,
        "gamma": gamma,
        "theta_per_day": theta_per_day,
        "vega_per_vol_point": vega_per_vol_point,
        "rho_per_rate_point": rho_per_rate_point,
        "inputs": {
            "spot": spot,
            "strike": strike,
            "time_to_expiry_years": time_to_expiry_years,
            "risk_free_rate": risk_free_rate,
            "dividend_yield": dividend_yield,
            "implied_volatility": implied_volatility,
            "option_type": option_type,
        },
    }


def greeks_for_contract(
    *,
    strike: float,
    expiry_days_remaining: float,
    right: str,
    spot: float,
    implied_volatility: float,
    risk_free_rate: float,
    dividend_yield: float,
    num_steps: int,
) -> dict:
    """Thin convenience wrapper for callers who have a day-count (e.g. from
    a contract's expiry date minus an "as of" wall-clock date) rather than
    a year-fraction. Deliberately takes plain scalar fields, not an O1
    contract object or an O2 quote object -- this module stays free of any
    aura_v05NNN_* import, per its isolated-module design."""
    if not (isinstance(expiry_days_remaining, (int, float)) and math.isfinite(expiry_days_remaining)
            and expiry_days_remaining > 0):
        return fail("EXPIRY_DAYS_REMAINING_MUST_BE_POSITIVE_FINITE", expiry_days_remaining=expiry_days_remaining)
    time_to_expiry_years = expiry_days_remaining / _DAYS_PER_YEAR
    return price_and_greeks(
        spot=spot, strike=strike, time_to_expiry_years=time_to_expiry_years,
        risk_free_rate=risk_free_rate, dividend_yield=dividend_yield,
        implied_volatility=implied_volatility, option_type=right, num_steps=num_steps,
    )
