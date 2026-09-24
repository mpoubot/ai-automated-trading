#!/usr/bin/env python3
"""
AURA v0.5.3.61 — Portfolio Enforcement Decision Journal

Why this exists (Martin's scoping, 2026-09-24)
------------------------------------------------------------------------
`.44` (`aura_v05344_portfolio_exposure_enforcement.py`) computes a full,
deterministic `EnforcementDecision` on every call — every dimension,
every verdict, a `decision_hash` — but as a pure function it does nothing
with that result once it returns it. Nothing anywhere in AURA today keeps
a record of what the risk gate actually saw and decided across cycles.

This module closes that gap, adapted (concept only, not code) from
EdgeStack's `agent/journal.py`, found in the 2026-09-24 26-repo Lablab
hackathon competitor audit (item 12, VALIDATED): "every session,
including flat/no-trade days, records the full gate trail and near-
misses, not just executed trades." The same principle applies here — an
ALLOW cycle and a no-signal cycle are exactly as informative, as
evidence, as a BLOCK, and are recorded identically.

Deliberately kept as its own file, not folded into `.44` itself
------------------------------------------------------------------------
`.44`'s module docstring states its own scope explicitly: "ENFORCEMENT
ONLY... never places, cancels, or modifies an order, never mutates a
position." Every one of its check functions is a pure function of its
inputs — no I/O, no side effects, no clock reads except the explicit
`now` parameter. Adding file writes into that module would break that
property for every existing caller, including the 40+ tests in
`test_aura_v05344_portfolio_exposure_enforcement.py` that rely on it
staying pure. Persistence is instead a separate, explicit step a caller
opts into — the same pattern `.44` itself already uses for wiring its
decision into `.31`/`.36`'s authorization chain ("explicitly left to a
later step"). Wiring `record_decision()` into an actual scheduled/live
runner (e.g. `.357`) is likewise left to a later, separate step — this
module's job is the durable record itself, not the calling convention
around it.

Design, and the judgment calls behind it
------------------------------------------------------------------------
  - Append-only JSON Lines (one `EnforcementDecision` per line). Never
    rewrites or truncates an existing file — a journal that can lose or
    silently overwrite a past entry defeats its own purpose.
  - `build_entry()` (pure, no I/O) and `append_entry()` (the one function
    that touches disk) are kept separate so the entry shape can be tested
    without a filesystem, matching this repo's established test style
    (see `.343`'s own `_atomic_write_json` vs. its pure compute
    functions).
  - Fail-closed on write: a failed append raises `JournalError` rather
    than silently swallowing the failure. An enforcement decision that
    was supposed to be recorded and wasn't is exactly the kind of gap
    this module exists to prevent — surfacing the failure loudly is safer
    than a caller believing a cycle was journaled when it wasn't.
  - Fail-closed on read: a corrupt line raises `JournalError` rather than
    being silently skipped. A partial read that looks complete is worse
    than a read that visibly fails — the same "don't understate what's
    wrong" principle `.44` itself applies to failed-venue data.
  - `read_journal()` on a journal file that does not exist yet returns
    `[]`, not an error — no entries recorded yet is a normal, expected
    state before the first `record_decision()` call, not a failure.
  - `context` is a small, optional, caller-supplied free-form dict (e.g.
    which symbol/hypothetical was under evaluation for an authorization-
    time check). It is never required and never interpreted by this
    module — purely a convenience for a future reader.
  - This module invents no retention policy, no rotation, no size cap,
    and no query language beyond a `since` timestamp filter and a
    `limit` (most-recent-N) cut. Anything beyond that (e.g. a real
    database, log rotation) is a later, separately-scoped decision, not
    assumed here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class JournalError(Exception):
    """Raised on any failure to durably write or faithfully read the
    journal. Fail-closed: callers must not treat a failed write as a
    recorded entry, nor a corrupt read as a complete one."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class JournalEntry:
    appended_at: str
    decision_hash: str
    overall_verdict: str
    snapshot_as_of: str
    decision: dict[str, Any]
    context: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "appended_at": self.appended_at,
            "decision_hash": self.decision_hash,
            "overall_verdict": self.overall_verdict,
            "snapshot_as_of": self.snapshot_as_of,
            "decision": self.decision,
            "context": self.context,
        }


def build_entry(decision: Any, *, context: dict[str, Any] | None = None, now: datetime | None = None) -> JournalEntry:
    """`decision` is a `.44` `EnforcementDecision` (duck-typed: needs
    `.to_dict()` / `.decision_hash` / `.overall_verdict` / `.snapshot_as_of`
    — this module does not import `.44` to avoid a hard dependency either
    direction; both modules already share this same duck-typed-attribute
    convention with `.43`, e.g. `.44`'s own `snapshot.as_of`/`.state_hash`
    usage). Pure — does no I/O, so entry shape is testable without disk."""
    now = now or datetime.now(timezone.utc)
    return JournalEntry(
        appended_at=now.isoformat(),
        decision_hash=decision.decision_hash,
        overall_verdict=decision.overall_verdict,
        snapshot_as_of=decision.snapshot_as_of,
        decision=decision.to_dict(),
        context=dict(context or {}),
    )


def append_entry(journal_path: Path, entry: JournalEntry) -> None:
    """The only function in this module that touches disk. Append-only:
    opens in 'a' mode, never truncates. Raises JournalError (never a bare
    OSError) on any failure, per module docstring."""
    try:
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry.to_dict(), sort_keys=True, ensure_ascii=False, allow_nan=False)
        with journal_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as exc:
        raise JournalError(f"failed to append journal entry to {journal_path}: {exc}") from exc


def record_decision(
    journal_path: Path, decision: Any, *, context: dict[str, Any] | None = None, now: datetime | None = None,
) -> JournalEntry:
    """Convenience: build_entry() + append_entry() in one call. This is
    the function a scheduled/live caller wires in immediately after
    `evaluate_portfolio_enforcement()` / `evaluate_hypothetical_trade()`
    — unconditionally, regardless of `overall_verdict`, so ALLOW and
    no-trade cycles are journaled exactly like BLOCKs (see module
    docstring). Raises JournalError on write failure; on success returns
    the JournalEntry that was written."""
    entry = build_entry(decision, context=context, now=now)
    append_entry(journal_path, entry)
    return entry


def read_journal(journal_path: Path, *, since: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    """Reads entries back in append order (oldest first). `since` filters
    on `appended_at >= since` (safe as an ISO-8601 string compare because
    every entry's timestamp comes from `now_iso()`-style timezone-aware
    UTC isoformat). `limit`, if given, keeps only the LAST `limit`
    matching entries — the common "show me the most recent N cycles" case
    — not the first `limit`. Returns `[]` if the file does not exist yet
    (see module docstring: an empty/unstarted journal is not an error).
    Raises JournalError on a corrupt line rather than silently skipping
    it (see module docstring on fail-closed reads)."""
    if not journal_path.exists():
        return []

    entries: list[dict[str, Any]] = []
    with journal_path.open("r", encoding="utf-8") as f:
        for line_no, raw_line in enumerate(f, start=1):
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                obj = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise JournalError(f"corrupt journal entry at {journal_path}:{line_no}: {exc}") from exc
            if since is not None and str(obj.get("appended_at", "")) < since:
                continue
            entries.append(obj)

    if limit is not None and limit >= 0:
        entries = entries[-limit:]
    return entries
