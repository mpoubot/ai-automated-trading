#!/usr/bin/env python3
"""Contract + unit tests for AURA v0.5.3.64 Equity History Log."""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name, path):
    """Idempotent: reuses an already-registered `sys.modules[name]` when
    present (see `.363`'s test file for the full rationale)."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


M = _load("aura_v05364_equity_history_log", ROOT / "aura_v05364_equity_history_log.py")

NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)


# ============================================================================
# append_observation / read_equity_history
# ============================================================================


def test_read_equity_history_missing_file_returns_empty(tmp_path):
    assert M.read_equity_history(tmp_path / "does_not_exist.jsonl") == []


def test_append_then_read_round_trips(tmp_path):
    log_path = tmp_path / "hist.jsonl"
    M.append_observation(log_path, venue="ALPACA", equity=100_000.0, now=NOW)
    history = M.read_equity_history(log_path)
    assert history == [{"venue": "ALPACA", "equity": 100_000.0, "as_of": NOW.isoformat()}]


def test_append_is_append_only_never_truncates(tmp_path):
    log_path = tmp_path / "hist.jsonl"
    M.append_observation(log_path, venue="ALPACA", equity=100_000.0, now=NOW)
    later = datetime(2026, 9, 25, 14, 0, 0, tzinfo=timezone.utc)
    M.append_observation(log_path, venue="ALPACA", equity=99_500.0, now=later)
    history = M.read_equity_history(log_path)
    assert len(history) == 2
    assert history[0]["equity"] == 100_000.0
    assert history[1]["equity"] == 99_500.0


def test_append_as_of_defaults_to_now_when_omitted(tmp_path):
    log_path = tmp_path / "hist.jsonl"
    obs = M.append_observation(log_path, venue="ALPACA", equity=100_000.0, now=NOW)
    assert obs.as_of == NOW.isoformat()
    assert obs.recorded_at == NOW.isoformat()


def test_append_as_of_can_differ_from_now(tmp_path):
    log_path = tmp_path / "hist.jsonl"
    earlier = datetime(2026, 9, 25, 6, 0, 0, tzinfo=timezone.utc)
    obs = M.append_observation(log_path, venue="ALPACA", equity=100_000.0, as_of=earlier, now=NOW)
    assert obs.as_of == earlier.isoformat()
    assert obs.recorded_at == NOW.isoformat()


def test_read_equity_history_creates_no_side_effect_on_missing_file(tmp_path):
    log_path = tmp_path / "sub" / "hist.jsonl"
    M.read_equity_history(log_path)
    assert not log_path.parent.exists()


def test_append_observation_creates_parent_dirs(tmp_path):
    log_path = tmp_path / "a" / "b" / "hist.jsonl"
    M.append_observation(log_path, venue="ALPACA", equity=100_000.0, now=NOW)
    assert log_path.exists()


def test_read_equity_history_raises_on_corrupt_line(tmp_path):
    log_path = tmp_path / "hist.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("not valid json\n", encoding="utf-8")
    with pytest.raises(M.EquityHistoryLogError):
        M.read_equity_history(log_path)


def test_read_equity_history_skips_blank_lines(tmp_path):
    log_path = tmp_path / "hist.jsonl"
    M.append_observation(log_path, venue="ALPACA", equity=100_000.0, now=NOW)
    with log_path.open("a", encoding="utf-8") as f:
        f.write("\n")
    history = M.read_equity_history(log_path)
    assert len(history) == 1


# ============================================================================
# record_and_read_equity_history
# ============================================================================


def test_record_and_read_first_ever_observation_is_its_own_baseline(tmp_path):
    log_path = tmp_path / "hist.jsonl"
    history = M.record_and_read_equity_history(log_path, venue="ALPACA", equity=100_000.0, now=NOW)
    assert len(history) == 1
    assert history[0]["equity"] == 100_000.0
    assert history[0]["as_of"] == NOW.isoformat()


def test_record_and_read_accumulates_across_calls(tmp_path):
    log_path = tmp_path / "hist.jsonl"
    M.record_and_read_equity_history(log_path, venue="ALPACA", equity=100_000.0, now=NOW)
    later = datetime(2026, 9, 25, 15, 0, 0, tzinfo=timezone.utc)
    history = M.record_and_read_equity_history(log_path, venue="ALPACA", equity=98_000.0, now=later)
    assert len(history) == 2
    assert [h["equity"] for h in history] == [100_000.0, 98_000.0]


def test_default_log_path_is_under_regime_output():
    assert str(M.DEFAULT_EQUITY_HISTORY_LOG_PATH) == "regime_output/equity_history_log/alpaca_equity_history.jsonl"
