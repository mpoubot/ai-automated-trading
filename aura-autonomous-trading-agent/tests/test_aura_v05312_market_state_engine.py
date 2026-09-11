"""
Regression test for the 2026-09-11 FROZEN_CANDIDATE fix.

Prior to the fix, FROZEN_CANDIDATE in v0.5.3.12 / v0.5.3.13 / v0.5.3.14 was
"BEAR x LOW ATR x POSITIVE bar-2" -- a string the real regime_state
generator (calculate_state()'s f"{trend} x {atr_regime} x {bar2_regime}")
could never produce, since that f-string only ever emits three tokens
("BEAR x LOW x POSITIVE"). frozen_candidate_match was therefore always
False in production, on every scheduled paper-trading cycle, with no test
catching it because every existing v0.5.3.13/.14 test built its upstream
.12-shaped fixture by hand rather than running v0.5.3.12's actual
calculate_state() (see those test files' own docstrings).

This test closes that gap: it feeds a synthetic but internally-consistent
1H OHLCV series through the REAL v0.5.3.12 calculate_state() function --
never a hand-built regime_state string -- engineered to produce a BEAR
trend, a LOW ATR regime, and a POSITIVE bar-2 return, and asserts the real
generator's output now equals FROZEN_CANDIDATE and matches it. It then
feeds that real .12 output into the real v0.5.3.13 process() to confirm
the fix holds end-to-end, not just inside .12.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def _load(module_filename: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / module_filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


engine12 = _load("aura_v05312_market_state_engine.py", "aura_v05312_engine")
engine13 = _load("aura_v05313_signal_decision_engine.py", "aura_v05313_engine")


def _build_bear_low_positive_frame(symbol: str = "BTC/USD", n_hours: int = 220) -> pd.DataFrame:
    """
    A synthetic 1H OHLCV series engineered (not asserted by construction --
    the test still checks the real output) to produce:
      - trend = BEAR: a long, gentle linear decline keeps the current close
        below the EMA50 computed on prior CLOSED 4H candles.
      - atr_regime = LOW: near-zero intrabar range and small bar-to-bar
        steps keep Wilder's ATR14 % well under the 0.596% threshold.
      - bar2_regime = POSITIVE: the final two hours are nudged upward
        relative to two hours prior, producing a positive 2H close-to-close
        return, without altering any already-CLOSED 4H candle (build_4h()
        only uses full 4-bar buckets, so a small tail adjustment on a
        trailing partial bucket cannot move the EMA50 input).
    """
    start = pd.Timestamp("2026-01-01T00:00:00Z")  # 4H-boundary aligned
    timestamps = [start + pd.Timedelta(hours=i) for i in range(n_hours)]

    base = 100.0
    slope = -0.02  # gentle decline, ~4.4 total over 220 hours
    closes = [base + slope * i for i in range(n_hours)]

    # Nudge the final two hours up relative to two hours prior, without
    # touching any bar further back. n_hours (220) is not a multiple of 4
    # from the 00:00-aligned start, so the trailing bucket containing these
    # bars is partial and excluded from build_4h()'s CLOSED 4H candles --
    # this cannot move ema50_4h.
    bump = 0.05
    closes[-2] = closes[-3] + bump
    closes[-1] = closes[-3] + 2 * bump

    rows = []
    for ts, c in zip(timestamps, closes):
        rows.append(
            {
                "timestamp": ts,
                "symbol": symbol,
                "open": c,
                "high": c,
                "low": c,
                "close": c,
                "volume": 1000.0,
            }
        )
    return pd.DataFrame(rows)


def test_real_generator_produces_frozen_candidate_match():
    """
    Exercises v0.5.3.12's real calculate_state() -- not a hand-built
    regime_state string. Before the fix this would have asserted
    regime_state == "BEAR x LOW x POSITIVE" and frozen_candidate_match is
    False, because FROZEN_CANDIDATE was unmatchable; after the fix both
    hold.
    """
    g = _build_bear_low_positive_frame()
    state, errors = engine12.calculate_state(g)

    assert errors == []
    assert state is not None
    assert state["trend_regime"] == "BEAR"
    assert state["atr_regime"] == "LOW"
    assert state["bar_2_regime"] == "POSITIVE"
    assert state["regime_state"] == "BEAR x LOW x POSITIVE"
    assert state["regime_state"] == engine12.FROZEN_CANDIDATE
    assert state["frozen_candidate_match"] is True


def test_real_generator_output_flows_to_signal_candidate_in_v13():
    """
    End-to-end: real .12 output (not a fixture) fed into real .13
    process() must produce SIGNAL_CANDIDATE / FROZEN_CANDIDATE_MATCH for
    BTC/USD. ETH/USD is included as a neutral (non-matching) required
    symbol so the overall snapshot is market_state_valid.
    """
    btc_g = _build_bear_low_positive_frame(symbol="BTC/USD")
    btc_state, btc_errors = engine12.calculate_state(btc_g)
    assert btc_errors == []
    assert btc_state is not None

    # ETH/USD: same shape, but without the final bar-2 bump, so its
    # regime_state stays a non-matching combination and it reports
    # NO_SIGNAL -- confirming the fix is candidate-specific, not a
    # blanket "everything matches now" change.
    eth_g = _build_bear_low_positive_frame(symbol="ETH/USD")
    eth_g = eth_g.copy()
    # Remove the bar-2 bump so bar_2_regime comes back NON_POSITIVE.
    eth_g.loc[eth_g.index[-2:], ["open", "high", "low", "close"]] = eth_g.iloc[-3][
        ["close"]
    ].values[0] - 0.01
    eth_state, eth_errors = engine12.calculate_state(eth_g)
    assert eth_errors == []
    assert eth_state is not None
    assert eth_state["regime_state"] != engine12.FROZEN_CANDIDATE
    assert eth_state["frozen_candidate_match"] is False

    canonical = {
        "symbols": {
            "BTC/USD": {"timestamp": btc_state["timestamp"]},
            "ETH/USD": {"timestamp": eth_state["timestamp"]},
        }
    }
    state_hash = engine13.sha256_text(engine13.stable_json(canonical))
    timestamps = [
        f"BTC/USD:{btc_state['timestamp']}",
        f"ETH/USD:{eth_state['timestamp']}",
    ]
    state_id = f"MS-{engine13.sha256_text('|'.join(sorted(timestamps)) + '|' + state_hash)[:24]}"

    snapshot = {
        "agent_version": "AURA v0.5.3.12",
        "engine": "MARKET_STATE_ENGINE",
        "data_status": "VALID",
        "market_state_valid": True,
        "guardrails": {
            "single_source_of_truth": True,
            "no_recalculation_downstream": True,
            "orders_allowed": False,
            "paper_execution": False,
            "live_execution": False,
            "strategy_changed": False,
            "parameters_changed": False,
        },
        "frozen_configuration": {
            "candidate": engine12.FROZEN_CANDIDATE,
            "atr_threshold_pct": engine12.FROZEN_ATR_THRESHOLD_PCT,
            "ema_period_4h": engine12.FROZEN_EMA_PERIOD_4H,
            "atr_period_1h": engine12.ATR_PERIOD_1H,
            "bar_2_lag_hours": engine12.BAR_2_LAG_HOURS,
        },
        "state_hash": state_hash,
        "state_id": state_id,
        "canonical_state": canonical,
        "symbols": {
            "BTC/USD": {
                "data_status": "VALID",
                "market_state_valid": True,
                "market_state": btc_state,
            },
            "ETH/USD": {
                "data_status": "VALID",
                "market_state_valid": True,
                "market_state": eth_state,
            },
        },
    }

    decision = engine13.process(snapshot, Path("test-fixture-input.json"))

    assert decision["decision_status"] == "DECIDED"
    assert decision["overall_decision"] == "SIGNAL_CANDIDATE"
    btc_decision = decision["decisions"]["BTC/USD"]
    assert btc_decision["decision"] == "SIGNAL_CANDIDATE"
    assert btc_decision["candidate_match"] is True
    assert btc_decision["reason"] == "FROZEN_CANDIDATE_MATCH"

    eth_decision = decision["decisions"]["ETH/USD"]
    assert eth_decision["decision"] == "NO_SIGNAL"
    assert eth_decision["reason"] == "FROZEN_CANDIDATE_NOT_PRESENT"
