#!/usr/bin/env python3
"""Contract + unit tests for AURA v0.5.3.61 Portfolio Enforcement Decision
Journal.

Covers: pure entry-building, append-only writes, fail-closed behavior on
write/read failure, an empty/unstarted journal being valid (not an
error), `since`/`limit` filtering, and that this module never imports or
depends on `.44` (duck-typed, per its own module docstring).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
JOURNAL_PATH = ROOT / "aura_v05361_portfolio_enforcement_journal.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


J = _load("aura_v05361_portfolio_enforcement_journal", JOURNAL_PATH)

NOW = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class _FakeDecision:
    """Duck-typed stand-in for .44's EnforcementDecision -- this module
    must work against the shape alone, never a real .44 import."""
    decision_hash: str
    overall_verdict: str
    snapshot_as_of: str
    body: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.body)


def _decision(verdict="ALLOW", h="hash1", snapshot_as_of="2026-09-24T12:00:00+00:00"):
    return _FakeDecision(decision_hash=h, overall_verdict=verdict, snapshot_as_of=snapshot_as_of,
                          body={"overall_verdict": verdict, "decision_hash": h, "dimension_verdicts": []})


# ---------------------------------------------------------------------------
# build_entry -- pure, no I/O.
# ---------------------------------------------------------------------------

def test_build_entry_is_pure_and_captures_decision_fields():
    entry = J.build_entry(_decision(verdict="BLOCK", h="abc123"), now=NOW)
    assert entry.decision_hash == "abc123"
    assert entry.overall_verdict == "BLOCK"
    assert entry.appended_at == NOW.isoformat()
    assert entry.context == {}


def test_build_entry_captures_optional_context():
    entry = J.build_entry(_decision(), context={"trigger": "scheduled_cycle", "symbol": "AAPL"}, now=NOW)
    assert entry.context == {"trigger": "scheduled_cycle", "symbol": "AAPL"}


def test_build_entry_defaults_now_to_real_utc_when_omitted():
    before = datetime.now(timezone.utc)
    entry = J.build_entry(_decision())
    after = datetime.now(timezone.utc)
    appended = datetime.fromisoformat(entry.appended_at)
    assert before <= appended <= after


# ---------------------------------------------------------------------------
# append_entry / record_decision -- append-only writes.
# ---------------------------------------------------------------------------

def test_record_decision_appends_one_json_line(tmp_path):
    journal_path = tmp_path / "journal.jsonl"
    entry = J.record_decision(journal_path, _decision(verdict="ALLOW", h="h1"), now=NOW)
    lines = journal_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["decision_hash"] == "h1"
    assert parsed["overall_verdict"] == "ALLOW"
    assert entry.decision_hash == "h1"


def test_record_decision_records_allow_and_block_and_no_trade_cycles_identically():
    """Per EdgeStack's pattern (audit item 12): every cycle is a
    first-class record, not just BLOCKs or executed trades."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        journal_path = Path(d) / "journal.jsonl"
        J.record_decision(journal_path, _decision(verdict="ALLOW", h="h1"), now=NOW)
        J.record_decision(journal_path, _decision(verdict="BLOCK", h="h2"), now=NOW)
        J.record_decision(journal_path, _decision(verdict="ALLOW", h="h3"), now=NOW)  # e.g. NO_SIGNAL cycle
        entries = J.read_journal(journal_path)
        assert [e["overall_verdict"] for e in entries] == ["ALLOW", "BLOCK", "ALLOW"]


def test_append_is_never_destructive_to_prior_entries(tmp_path):
    journal_path = tmp_path / "journal.jsonl"
    J.record_decision(journal_path, _decision(h="h1"), now=NOW)
    J.record_decision(journal_path, _decision(h="h2"), now=NOW)
    entries = J.read_journal(journal_path)
    assert len(entries) == 2
    assert entries[0]["decision_hash"] == "h1"
    assert entries[1]["decision_hash"] == "h2"


def test_append_entry_fails_closed_when_parent_cannot_be_created(tmp_path):
    # A file (not a directory) in the path where a directory needs to be
    # created forces mkdir() to fail -- this must surface as JournalError,
    # never be silently swallowed.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    journal_path = blocker / "nested" / "journal.jsonl"
    with pytest.raises(J.JournalError):
        J.record_decision(journal_path, _decision(), now=NOW)


# ---------------------------------------------------------------------------
# read_journal -- empty/unstarted journal, since/limit filtering, corrupt data.
# ---------------------------------------------------------------------------

def test_read_journal_on_nonexistent_file_returns_empty_list_not_error(tmp_path):
    journal_path = tmp_path / "does_not_exist.jsonl"
    assert J.read_journal(journal_path) == []


def test_read_journal_since_filters_by_appended_at(tmp_path):
    journal_path = tmp_path / "journal.jsonl"
    early = datetime(2026, 9, 24, 10, 0, 0, tzinfo=timezone.utc)
    late = datetime(2026, 9, 24, 14, 0, 0, tzinfo=timezone.utc)
    J.record_decision(journal_path, _decision(h="early"), now=early)
    J.record_decision(journal_path, _decision(h="late"), now=late)
    cutoff = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc).isoformat()
    entries = J.read_journal(journal_path, since=cutoff)
    assert [e["decision_hash"] for e in entries] == ["late"]


def test_read_journal_limit_keeps_most_recent_not_earliest(tmp_path):
    journal_path = tmp_path / "journal.jsonl"
    for i in range(5):
        J.record_decision(journal_path, _decision(h=f"h{i}"), now=NOW)
    entries = J.read_journal(journal_path, limit=2)
    assert [e["decision_hash"] for e in entries] == ["h3", "h4"]


def test_read_journal_raises_on_corrupt_line_rather_than_skipping(tmp_path):
    journal_path = tmp_path / "journal.jsonl"
    J.record_decision(journal_path, _decision(h="good"), now=NOW)
    with journal_path.open("a", encoding="utf-8") as f:
        f.write("{not valid json\n")
    with pytest.raises(J.JournalError):
        J.read_journal(journal_path)


def test_read_journal_skips_blank_lines():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        journal_path = Path(d) / "journal.jsonl"
        J.record_decision(journal_path, _decision(h="h1"), now=NOW)
        with journal_path.open("a", encoding="utf-8") as f:
            f.write("\n")
        J.record_decision(journal_path, _decision(h="h2"), now=NOW)
        entries = J.read_journal(journal_path)
        assert len(entries) == 2


# ---------------------------------------------------------------------------
# Independence: this module never imports or hard-depends on .44.
# ---------------------------------------------------------------------------

def test_module_does_not_import_portfolio_exposure_enforcement():
    """This module documents its relationship to .44 in prose (the module
    docstring names it for context), but must not actually import or
    dynamically load it -- the duck-typing in build_entry() is deliberate
    so either module can be tested/loaded independently."""
    source = JOURNAL_PATH.read_text(encoding="utf-8")
    code_only = source.split('"""', 2)[-1]
    assert "aura_v05344" not in code_only
    assert "spec_from_file_location" not in code_only
    assert not any(line.strip().startswith(("import ", "from ")) and "aura_v053" in line for line in code_only.splitlines())


def test_module_never_places_or_modifies_orders_or_positions():
    source = JOURNAL_PATH.read_text(encoding="utf-8")
    forbidden = ["place_order(", "submit_order(", "cancel_order(", "authorized_submit(", "authorized_order_request("]
    for token in forbidden:
        assert token not in source, f"journal module must not reference {token!r}"
