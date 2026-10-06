#!/usr/bin/env python3
"""Unit tests for AURA v0.5.3.75 (Options Greeks Engine, Phase O3).

Validation strategy: rather than trusting a remembered decimal answer
from a specific textbook worked example, this file implements the
standard Black-Scholes closed-form formula INDEPENDENTLY (a second,
completely separate pricing path from `.375`'s binomial tree) and uses
it as a validation oracle for the one regime where the two models are
mathematically guaranteed to agree (a call on a non-dividend-paying
underlying, where early exercise is never optimal, so American ==
European). For puts and dividend-paying calls, where no closed form for
the American price exists, the Black-Scholes European price is instead
used as a proven LOWER BOUND (the right to exercise early can only add
value, never subtract it) -- this is a model-independent, no-arbitrage
fact, not a specific numeric claim.

Plain top-level import, matching `.374`'s test file convention (`.375`
has zero aura_v05NNN_* imports of its own, so there is no duplicate-
module-object hazard here to design around).
"""
from __future__ import annotations

import math

import aura_v05375_options_greeks as G


def expect(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"PASS: {name}")


# ======================================================================= #
# Independent Black-Scholes reference oracle (test-file-only, never
# imported by or shared with production code).
# ======================================================================= #

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(S: float, K: float, T: float, r: float, q: float, sigma: float, option_type: str) -> float:
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if option_type == "CALL":
        return S * math.exp(-q * T) * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * math.exp(-q * T) * _norm_cdf(-d1)


# ======================================================================= #
# 1. Fail-closed input validation
# ======================================================================= #

def test_invalid_option_type() -> None:
    r = G.price_and_greeks(spot=100, strike=100, time_to_expiry_years=0.5, risk_free_rate=0.04,
                            dividend_yield=0.0, implied_volatility=0.2, option_type="SWAP", num_steps=100)
    expect("1: invalid option_type fails closed", r["status"] == "INVALID_INPUT")
    expect("1: reason is INVALID_OPTION_TYPE", r["reason"] == "INVALID_OPTION_TYPE")


def test_non_positive_spot_strike_time_vol_fail_closed() -> None:
    base = dict(strike=100, time_to_expiry_years=0.5, risk_free_rate=0.04, dividend_yield=0.0,
                implied_volatility=0.2, option_type="CALL", num_steps=100)
    r = G.price_and_greeks(spot=0, **base)
    expect("2a: spot=0 fails closed", r["status"] == "INVALID_INPUT" and r["reason"] == "SPOT_MUST_BE_POSITIVE_FINITE")
    r = G.price_and_greeks(spot=-10, **base)
    expect("2b: negative spot fails closed", r["status"] == "INVALID_INPUT")

    base2 = dict(spot=100, time_to_expiry_years=0.5, risk_free_rate=0.04, dividend_yield=0.0,
                 implied_volatility=0.2, option_type="CALL", num_steps=100)
    r = G.price_and_greeks(strike=0, **base2)
    expect("2c: strike=0 fails closed", r["status"] == "INVALID_INPUT" and r["reason"] == "STRIKE_MUST_BE_POSITIVE_FINITE")

    base3 = dict(spot=100, strike=100, risk_free_rate=0.04, dividend_yield=0.0,
                 implied_volatility=0.2, option_type="CALL", num_steps=100)
    r = G.price_and_greeks(time_to_expiry_years=0.0, **base3)
    expect("2d: zero time-to-expiry fails closed",
           r["status"] == "INVALID_INPUT" and r["reason"] == "TIME_TO_EXPIRY_MUST_BE_POSITIVE_FINITE")
    r = G.price_and_greeks(time_to_expiry_years=-0.1, **base3)
    expect("2e: negative time-to-expiry fails closed", r["status"] == "INVALID_INPUT")

    base4 = dict(spot=100, strike=100, time_to_expiry_years=0.5, risk_free_rate=0.04, dividend_yield=0.0,
                 option_type="CALL", num_steps=100)
    r = G.price_and_greeks(implied_volatility=0.0, **base4)
    expect("2f: zero IV fails closed",
           r["status"] == "INVALID_INPUT" and r["reason"] == "IMPLIED_VOLATILITY_MUST_BE_POSITIVE_FINITE")
    r = G.price_and_greeks(implied_volatility=-0.2, **base4)
    expect("2g: negative IV fails closed", r["status"] == "INVALID_INPUT")


def test_num_steps_too_small_fails_closed() -> None:
    base = dict(spot=100, strike=100, time_to_expiry_years=0.5, risk_free_rate=0.04, dividend_yield=0.0,
                implied_volatility=0.2, option_type="CALL")
    r = G.price_and_greeks(num_steps=1, **base)
    expect("3a: num_steps=1 fails closed", r["status"] == "INVALID_INPUT" and r["reason"] == "NUM_STEPS_MUST_BE_INTEGER_AT_LEAST_2")
    r = G.price_and_greeks(num_steps=0, **base)
    expect("3b: num_steps=0 fails closed", r["status"] == "INVALID_INPUT")
    r = G.price_and_greeks(num_steps=100.5, **base)  # type: ignore[arg-type]
    expect("3c: non-integer num_steps fails closed", r["status"] == "INVALID_INPUT")


def test_iv_too_small_for_vega_fails_closed() -> None:
    r = G.price_and_greeks(spot=100, strike=100, time_to_expiry_years=0.5, risk_free_rate=0.04,
                            dividend_yield=0.0, implied_volatility=1e-6, option_type="CALL", num_steps=100)
    expect("4: pathologically tiny IV fails closed rather than silently producing a garbage vega",
           r["status"] == "INVALID_INPUT" and r["reason"] == "IMPLIED_VOLATILITY_TOO_SMALL_FOR_VEGA_FINITE_DIFFERENCE")


def test_greeks_for_contract_validates_expiry_days() -> None:
    r = G.greeks_for_contract(strike=100, expiry_days_remaining=0, right="CALL", spot=100,
                               implied_volatility=0.2, risk_free_rate=0.04, dividend_yield=0.0, num_steps=100)
    expect("5: zero expiry_days_remaining fails closed",
           r["status"] == "INVALID_INPUT" and r["reason"] == "EXPIRY_DAYS_REMAINING_MUST_BE_POSITIVE_FINITE")


# ======================================================================= #
# 2. Determinism
# ======================================================================= #

def test_pricing_is_deterministic() -> None:
    kwargs = dict(spot=100, strike=105, time_to_expiry_years=0.5, risk_free_rate=0.04, dividend_yield=0.01,
                  implied_volatility=0.25, option_type="PUT", num_steps=200)
    r1 = G.price_and_greeks(**kwargs)
    r2 = G.price_and_greeks(**kwargs)
    expect("6: identical inputs produce identical theoretical_price",
           r1["theoretical_price"] == r2["theoretical_price"])
    expect("6: identical inputs produce identical delta", r1["delta"] == r2["delta"])
    expect("6: identical inputs produce identical vega", r1["vega_per_vol_point"] == r2["vega_per_vol_point"])


# ======================================================================= #
# 3. No-arbitrage bound: price >= intrinsic value
# ======================================================================= #

def test_price_at_least_intrinsic_value_itm_call() -> None:
    r = G.price_and_greeks(spot=120, strike=100, time_to_expiry_years=0.25, risk_free_rate=0.04,
                            dividend_yield=0.0, implied_volatility=0.2, option_type="CALL", num_steps=300)
    expect("7a: ITM call status OK", r["status"] == "OK")
    expect("7a: ITM call price >= intrinsic value (20.0)", r["theoretical_price"] >= 20.0 - 1e-9)


def test_price_at_least_intrinsic_value_itm_put() -> None:
    r = G.price_and_greeks(spot=80, strike=100, time_to_expiry_years=0.25, risk_free_rate=0.04,
                            dividend_yield=0.0, implied_volatility=0.2, option_type="PUT", num_steps=300)
    expect("7b: ITM put status OK", r["status"] == "OK")
    expect("7b: ITM put price >= intrinsic value (20.0)", r["theoretical_price"] >= 20.0 - 1e-9)


# ======================================================================= #
# 4. Convergence to the independent Black-Scholes oracle (the one regime
#    where American == European: a call, non-dividend-paying underlying)
# ======================================================================= #

def test_american_call_no_dividend_converges_to_black_scholes() -> None:
    S, K, T, r, q, sigma = 100.0, 100.0, 1.0, 0.04, 0.0, 0.25
    reference = bs_price(S, K, T, r, q, sigma, "CALL")

    errors = []
    for steps in (50, 200, 800):
        res = G.price_and_greeks(spot=S, strike=K, time_to_expiry_years=T, risk_free_rate=r,
                                  dividend_yield=q, implied_volatility=sigma, option_type="CALL",
                                  num_steps=steps)
        expect(f"8: steps={steps} status OK", res["status"] == "OK")
        errors.append(abs(res["theoretical_price"] - reference))

    expect("8: 800-step binomial price within 2 cents of the independent Black-Scholes price",
           errors[-1] < 0.02)
    expect("8: error shrinks as step count increases (50-step error > 800-step error)",
           errors[0] > errors[-1])


def test_american_call_no_dividend_delta_converges_to_black_scholes_delta() -> None:
    S, K, T, r, q, sigma = 100.0, 100.0, 1.0, 0.04, 0.0, 0.25
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    reference_delta = math.exp(-q * T) * _norm_cdf(d1)

    res = G.price_and_greeks(spot=S, strike=K, time_to_expiry_years=T, risk_free_rate=r,
                              dividend_yield=q, implied_volatility=sigma, option_type="CALL", num_steps=800)
    expect("9: 800-step binomial delta within 0.01 of the independent Black-Scholes delta",
           abs(res["delta"] - reference_delta) < 0.01)


# ======================================================================= #
# 5. American price >= European (Black-Scholes) price -- a proven,
#    model-independent no-arbitrage lower bound, not a specific numeric
#    claim. Exercised on an ITM put (where early-exercise value is real)
#    and on a dividend-paying ITM call (where early exercise can also be
#    optimal just before an ex-dividend date, modeled here via a
#    continuous dividend yield).
# ======================================================================= #

def test_american_put_at_least_european_black_scholes_price() -> None:
    S, K, T, r, q, sigma = 90.0, 100.0, 0.5, 0.05, 0.0, 0.3
    european_reference = bs_price(S, K, T, r, q, sigma, "PUT")
    res = G.price_and_greeks(spot=S, strike=K, time_to_expiry_years=T, risk_free_rate=r,
                              dividend_yield=q, implied_volatility=sigma, option_type="PUT", num_steps=500)
    expect("10: American put price >= European (Black-Scholes) put price",
           res["theoretical_price"] >= european_reference - 1e-6)
    expect("10: early-exercise value is actually nonzero here (strictly greater, not just equal)",
           res["theoretical_price"] > european_reference + 1e-4)


def test_american_dividend_call_at_least_european_black_scholes_price() -> None:
    S, K, T, r, q, sigma = 110.0, 100.0, 0.5, 0.02, 0.06, 0.25
    european_reference = bs_price(S, K, T, r, q, sigma, "CALL")
    res = G.price_and_greeks(spot=S, strike=K, time_to_expiry_years=T, risk_free_rate=r,
                              dividend_yield=q, implied_volatility=sigma, option_type="CALL", num_steps=500)
    expect("11: American dividend-paying call price >= European (Black-Scholes) call price",
           res["theoretical_price"] >= european_reference - 1e-6)


# ======================================================================= #
# 6. Delta bounds and deep ITM/OTM behavior
# ======================================================================= #

def test_delta_bounds_call_and_put() -> None:
    call = G.price_and_greeks(spot=100, strike=100, time_to_expiry_years=0.5, risk_free_rate=0.04,
                               dividend_yield=0.0, implied_volatility=0.2, option_type="CALL", num_steps=300)
    put = G.price_and_greeks(spot=100, strike=100, time_to_expiry_years=0.5, risk_free_rate=0.04,
                              dividend_yield=0.0, implied_volatility=0.2, option_type="PUT", num_steps=300)
    expect("12a: ATM call delta in (0, 1)", 0.0 < call["delta"] < 1.0)
    expect("12b: ATM put delta in (-1, 0)", -1.0 < put["delta"] < 0.0)


def test_deep_itm_otm_delta_behavior() -> None:
    deep_itm_call = G.price_and_greeks(spot=200, strike=100, time_to_expiry_years=0.1, risk_free_rate=0.04,
                                        dividend_yield=0.0, implied_volatility=0.2, option_type="CALL", num_steps=300)
    deep_otm_call = G.price_and_greeks(spot=50, strike=100, time_to_expiry_years=0.1, risk_free_rate=0.04,
                                        dividend_yield=0.0, implied_volatility=0.2, option_type="CALL", num_steps=300)
    expect("13a: deep ITM call delta close to 1", deep_itm_call["delta"] > 0.95)
    expect("13b: deep OTM call delta close to 0", deep_otm_call["delta"] < 0.05)

    deep_itm_put = G.price_and_greeks(spot=50, strike=100, time_to_expiry_years=0.1, risk_free_rate=0.04,
                                       dividend_yield=0.0, implied_volatility=0.2, option_type="PUT", num_steps=300)
    deep_otm_put = G.price_and_greeks(spot=200, strike=100, time_to_expiry_years=0.1, risk_free_rate=0.04,
                                       dividend_yield=0.0, implied_volatility=0.2, option_type="PUT", num_steps=300)
    expect("13c: deep ITM put delta close to -1", deep_itm_put["delta"] < -0.95)
    expect("13d: deep OTM put delta close to 0", deep_otm_put["delta"] > -0.05)


# ======================================================================= #
# 7. Gamma non-negative (convexity holds for both calls and puts)
# ======================================================================= #

def test_gamma_nonnegative_call_and_put() -> None:
    for option_type in ("CALL", "PUT"):
        for spot in (80, 100, 120):
            r = G.price_and_greeks(spot=spot, strike=100, time_to_expiry_years=0.5, risk_free_rate=0.04,
                                    dividend_yield=0.0, implied_volatility=0.25, option_type=option_type,
                                    num_steps=300)
            expect(f"14: gamma >= 0 for {option_type} at spot={spot}", r["gamma"] >= -1e-8)


# ======================================================================= #
# 8. Vega positive (more volatility -> more option value, for both calls
#    and puts, long positions)
# ======================================================================= #

def test_vega_positive_call_and_put() -> None:
    for option_type in ("CALL", "PUT"):
        r = G.price_and_greeks(spot=100, strike=100, time_to_expiry_years=0.5, risk_free_rate=0.04,
                                dividend_yield=0.0, implied_volatility=0.25, option_type=option_type,
                                num_steps=300)
        expect(f"15: vega_per_vol_point > 0 for {option_type}", r["vega_per_vol_point"] > 0.0)

    # Cross-check directly: pricing at two different IVs, confirming the
    # higher-IV price is actually higher (the structural fact vega claims).
    low = G.price_and_greeks(spot=100, strike=100, time_to_expiry_years=0.5, risk_free_rate=0.04,
                              dividend_yield=0.0, implied_volatility=0.15, option_type="CALL", num_steps=300)
    high = G.price_and_greeks(spot=100, strike=100, time_to_expiry_years=0.5, risk_free_rate=0.04,
                               dividend_yield=0.0, implied_volatility=0.35, option_type="CALL", num_steps=300)
    expect("15: directly pricing at higher IV actually gives a higher price",
           high["theoretical_price"] > low["theoretical_price"])


# ======================================================================= #
# 9. Rho sign convention (q=0, to keep the sign unambiguous): higher rates
#    help calls, hurt puts.
# ======================================================================= #

def test_rho_sign_convention() -> None:
    call = G.price_and_greeks(spot=100, strike=100, time_to_expiry_years=1.0, risk_free_rate=0.04,
                               dividend_yield=0.0, implied_volatility=0.2, option_type="CALL", num_steps=300)
    put = G.price_and_greeks(spot=100, strike=100, time_to_expiry_years=1.0, risk_free_rate=0.04,
                              dividend_yield=0.0, implied_volatility=0.2, option_type="PUT", num_steps=300)
    expect("16a: call rho positive (higher rates help calls)", call["rho_per_rate_point"] > 0.0)
    expect("16b: put rho negative (higher rates hurt puts)", put["rho_per_rate_point"] < 0.0)


# ======================================================================= #
# 10. Numerical stability under refinement
# ======================================================================= #

def test_price_stable_across_step_counts() -> None:
    kwargs = dict(spot=100, strike=95, time_to_expiry_years=0.75, risk_free_rate=0.03, dividend_yield=0.015,
                  implied_volatility=0.3, option_type="PUT")
    r_300 = G.price_and_greeks(num_steps=300, **kwargs)
    r_600 = G.price_and_greeks(num_steps=600, **kwargs)
    expect("17: price at 300 vs 600 steps agrees within 5 cents (converged, not blowing up)",
           abs(r_300["theoretical_price"] - r_600["theoretical_price"]) < 0.05)
    expect("17: delta at 300 vs 600 steps agrees within 0.01",
           abs(r_300["delta"] - r_600["delta"]) < 0.01)


# ======================================================================= #
# 11. greeks_for_contract wrapper actually matches price_and_greeks given
#     the equivalent year-fraction
# ======================================================================= #

def test_greeks_for_contract_matches_direct_call() -> None:
    wrapper = G.greeks_for_contract(strike=100, expiry_days_remaining=182.5, right="CALL", spot=100,
                                     implied_volatility=0.2, risk_free_rate=0.04, dividend_yield=0.0,
                                     num_steps=300)
    direct = G.price_and_greeks(spot=100, strike=100, time_to_expiry_years=182.5 / 365.0,
                                 risk_free_rate=0.04, dividend_yield=0.0, implied_volatility=0.2,
                                 option_type="CALL", num_steps=300)
    expect("18: greeks_for_contract matches an equivalent direct price_and_greeks call",
           wrapper["theoretical_price"] == direct["theoretical_price"])


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_")]
    for t in tests:
        t()
    print(f"AURA v0.5.3.75 UNIT TEST CONTRACT: ALL {len(tests)} PASS")
