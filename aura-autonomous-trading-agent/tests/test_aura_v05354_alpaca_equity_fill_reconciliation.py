#!/usr/bin/env python3
"""Contract + unit tests for AURA v0.5.3.54 Alpaca equity fill reconciliation."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


M = _load("aura_v05354_alpaca_equity_fill_reconciliation", ROOT / "aura_v05354_alpaca_equity_fill_reconciliation.py")


class FakeOrder:
    def __init__(self, *, status, id="broker-1", filled_qty=None, filled_avg_price=None,
                 submitted_at="2026-09-23T10:00:00Z", filled_at=None):
        self.status = status
        self.id = id
        self.filled_qty = filled_qty
        self.filled_avg_price = filled_avg_price
        self.submitted_at = submitted_at
        self.filled_at = filled_at


class _EnumLike:
    """Mirrors alpaca-py's OrderStatus enum having a `.value` attribute."""
    def __init__(self, value):
        self.value = value


class SequencePollClient:
    """Returns each order in `sequence` in turn (repeats the last one
    forever once exhausted), or raises `exceptions[i]` at that poll index
    if supplied."""
    def __init__(self, sequence, exceptions=None):
        self._sequence = sequence
        self._exceptions = exceptions or {}
        self.calls = 0

    def get_order_by_client_id(self, client_order_id):
        idx = self.calls
        self.calls += 1
        if idx in self._exceptions:
            raise self._exceptions[idx]
        seq_idx = min(idx, len(self._sequence) - 1)
        return self._sequence[seq_idx]


def fake_clock(start=0.0):
    state = {"t": start}

    def now_fn():
        return state["t"]

    def sleep_fn(seconds):
        state["t"] += seconds

    return now_fn, sleep_fn


# ============================================================================
# Immediate terminal states
# ============================================================================


def test_immediate_fill():
    client = SequencePollClient([FakeOrder(status="filled", filled_qty="10", filled_avg_price="123.45",
                                            filled_at="2026-09-23T10:00:01Z")])
    now_fn, sleep_fn = fake_clock()
    result = M.poll_order_fill(client, "abc", timeout_seconds=30, poll_interval_seconds=1,
                                sleep_fn=sleep_fn, now_fn=now_fn)
    assert result.status == "FILLED"
    assert result.broker_status == "filled"
    assert result.filled_qty == "10"
    assert result.filled_avg_price == "123.45"
    assert result.polls_performed == 1
    assert result.timed_out is False
    assert result.error is None


def test_enum_like_status_value_extracted():
    client = SequencePollClient([FakeOrder(status=_EnumLike("filled"))])
    now_fn, sleep_fn = fake_clock()
    result = M.poll_order_fill(client, "abc", timeout_seconds=10, poll_interval_seconds=1,
                                sleep_fn=sleep_fn, now_fn=now_fn)
    assert result.status == "FILLED"
    assert result.broker_status == "filled"


@pytest.mark.parametrize("raw,expected", [
    ("canceled", "CANCELED"),
    ("expired", "EXPIRED"),
    ("rejected", "REJECTED"),
])
def test_other_terminal_statuses(raw, expected):
    client = SequencePollClient([FakeOrder(status=raw)])
    now_fn, sleep_fn = fake_clock()
    result = M.poll_order_fill(client, "abc", timeout_seconds=10, poll_interval_seconds=1,
                                sleep_fn=sleep_fn, now_fn=now_fn)
    assert result.status == expected
    assert result.timed_out is False


# ============================================================================
# Polling across multiple calls before terminal state
# ============================================================================


def test_polls_until_filled():
    client = SequencePollClient([
        FakeOrder(status="new"), FakeOrder(status="accepted"), FakeOrder(status="filled", filled_qty="5"),
    ])
    now_fn, sleep_fn = fake_clock()
    result = M.poll_order_fill(client, "abc", timeout_seconds=30, poll_interval_seconds=1,
                                sleep_fn=sleep_fn, now_fn=now_fn)
    assert result.status == "FILLED"
    assert result.polls_performed == 3
    assert client.calls == 3


def test_unrecognized_status_treated_as_open_not_error():
    # A future/unknown Alpaca status string -- never silently treated as
    # filled or as an error; keeps polling until timeout.
    client = SequencePollClient([FakeOrder(status="some_future_status_this_module_does_not_know")])
    now_fn, sleep_fn = fake_clock()
    result = M.poll_order_fill(client, "abc", timeout_seconds=2, poll_interval_seconds=1,
                                sleep_fn=sleep_fn, now_fn=now_fn)
    assert result.status == "TIMEOUT"
    assert result.broker_status == "some_future_status_this_module_does_not_know"
    assert result.timed_out is True


# ============================================================================
# Timeout paths -- must never fabricate a fill
# ============================================================================


def test_timeout_while_still_open():
    client = SequencePollClient([FakeOrder(status="new")])
    now_fn, sleep_fn = fake_clock()
    result = M.poll_order_fill(client, "abc", timeout_seconds=3, poll_interval_seconds=1,
                                sleep_fn=sleep_fn, now_fn=now_fn)
    assert result.status == "TIMEOUT"
    assert result.timed_out is True
    assert result.filled_qty is None


def test_timeout_while_partially_filled_reports_partially_filled_not_timeout():
    client = SequencePollClient([FakeOrder(status="partially_filled", filled_qty="3", filled_avg_price="100.0")])
    now_fn, sleep_fn = fake_clock()
    result = M.poll_order_fill(client, "abc", timeout_seconds=3, poll_interval_seconds=1,
                                sleep_fn=sleep_fn, now_fn=now_fn)
    # Real, partial fill information exists -- must be surfaced distinctly
    # from a plain unfilled timeout, never discarded.
    assert result.status == "PARTIALLY_FILLED"
    assert result.filled_qty == "3"
    assert result.timed_out is True


def test_timeout_zero_still_performs_exactly_one_poll():
    client = SequencePollClient([FakeOrder(status="new")])
    now_fn, sleep_fn = fake_clock()
    result = M.poll_order_fill(client, "abc", timeout_seconds=0, poll_interval_seconds=1,
                                sleep_fn=sleep_fn, now_fn=now_fn)
    assert result.polls_performed == 1
    assert result.status == "TIMEOUT"


# ============================================================================
# Broker/API failure paths
# ============================================================================


def test_every_poll_raises_reports_poll_error_not_timeout():
    client = SequencePollClient([FakeOrder(status="new")], exceptions={0: RuntimeError("network down"), 1: RuntimeError("network down")})
    now_fn, sleep_fn = fake_clock()
    result = M.poll_order_fill(client, "abc", timeout_seconds=1, poll_interval_seconds=1,
                                sleep_fn=sleep_fn, now_fn=now_fn)
    assert result.status == "POLL_ERROR"
    assert "network down" in result.error
    assert result.broker_order_id is None


def test_transient_error_then_success_recovers():
    client = SequencePollClient(
        [FakeOrder(status="new"), FakeOrder(status="filled", filled_qty="1")],
        exceptions={0: RuntimeError("transient")},
    )
    now_fn, sleep_fn = fake_clock()
    result = M.poll_order_fill(client, "abc", timeout_seconds=10, poll_interval_seconds=1,
                                sleep_fn=sleep_fn, now_fn=now_fn)
    assert result.status == "FILLED"
    assert result.error is None


# ============================================================================
# Input validation
# ============================================================================


def test_missing_client_order_id_raises():
    with pytest.raises(M.FillReconciliationError):
        M.poll_order_fill(SequencePollClient([]), "", timeout_seconds=1, poll_interval_seconds=1)


def test_negative_timeout_raises():
    with pytest.raises(M.FillReconciliationError):
        M.poll_order_fill(SequencePollClient([FakeOrder(status="new")]), "abc", timeout_seconds=-1, poll_interval_seconds=1)


def test_nonpositive_poll_interval_raises():
    with pytest.raises(M.FillReconciliationError):
        M.poll_order_fill(SequencePollClient([FakeOrder(status="new")]), "abc", timeout_seconds=1, poll_interval_seconds=0)


def test_to_dict_has_every_required_field():
    client = SequencePollClient([FakeOrder(status="filled", filled_qty="1", filled_avg_price="9.99")])
    now_fn, sleep_fn = fake_clock()
    result = M.poll_order_fill(client, "abc", timeout_seconds=1, poll_interval_seconds=1,
                                sleep_fn=sleep_fn, now_fn=now_fn)
    d = result.to_dict()
    for field in ("client_order_id", "status", "broker_status", "broker_order_id", "filled_qty",
                  "filled_avg_price", "submitted_at", "filled_at", "polls_performed", "timed_out",
                  "error", "observed_at"):
        assert field in d
