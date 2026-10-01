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

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Extension -- 2026-10-01 ("lets go for #4"): a default path, mirroring
# `.364`'s own `DEFAULT_EQUITY_HISTORY_LOG_PATH` convention exactly (same
# `regime_output/` root `.338` already uses for its own durable claim
# directories), for the real caller that opts into journaling
# (`.363`/`.365`) to fall back to when it doesn't need a different path.
# This module's own functions (`record_decision`/`record_raw_decision`)
# never read this constant themselves -- `journal_path` stays a required,
# explicit argument everywhere in this file, exactly as before; only a
# CALLER one layer up (`.363`) chooses to default to it.
DEFAULT_JOURNAL_PATH = Path("regime_output/decision_journal/stage1b_decisions.jsonl")


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


# ----------------------------------------------------------------------- #
# Extension -- 2026-10-01 (Martin, "lets go for #4", AURA_Lablab_Hackathon_
# Official_Winners_Audit_2026-10-01.md ranked finding #4: three independent
# hackathon teams -- EdgeStack, Killswitch Capital, and this journal's own
# original design above -- converge on "log every symbol every cycle, not
# just the ones that traded". That principle was already implemented here
# for `.44`'s own EnforcementDecision; it was NOT usable by any OTHER
# decision-producer in this repo (`.338`'s own execution-level outcomes --
# kill switch / auth failure / replay rejection / EXECUTION_UNCERTAIN /
# submitted -- and `.355`'s own per-symbol AuditRecord, which already
# folds `.350`'s signal decision, `.44`'s enforcement verdict, and `.338`'s
# supervision result into ONE row per symbol per cycle) because
# `build_entry()` above hard-requires an object shaped exactly like `.44`'s
# `EnforcementDecision` (`.decision_hash`/`.overall_verdict`/
# `.snapshot_as_of`/`.to_dict()`).
#
# `build_raw_entry()`/`record_raw_decision()` are a second, parallel entry
# point into the SAME journal file and the SAME `JournalEntry` shape --
# for a caller whose decision is a plain dict, not a duck-typed object.
# This is pure addition: `JournalEntry`, `build_entry()`, `append_entry()`,
# `record_decision()`, and `read_journal()` above are byte-for-byte
# unchanged, so every existing `.44`-journaling caller (and this module's
# own pre-existing tests) is unaffected. Entries from both entry points
# interleave in the same file in append order, in the same shape
# (`read_journal()` reads either kind identically) -- distinguishable by
# `context`/`decision` contents, not by a different file or format.
# ----------------------------------------------------------------------- #


def build_raw_entry(
    *,
    overall_verdict: str,
    snapshot_as_of: str,
    decision: dict[str, Any],
    decision_hash: str | None = None,
    context: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> JournalEntry:
    """Pure (no I/O) -- the `build_entry()` analogue for a plain-dict
    decision instead of a duck-typed `.44`-shaped object. `decision` is
    stored as-is (already a `dict`, e.g. `.355.AuditRecord.to_dict()` or a
    `.338` result dict) -- this function does not interpret or validate
    its contents beyond requiring it round-trip through `json.dumps`.

    `decision_hash`, when the caller has no better one of its own (e.g.
    `.355`'s `AuditRecord.decision_hash`, itself `.350`'s own signal-
    decision hash when reached, else `None`), defaults to a SHA-256 of
    `decision`'s own canonical JSON -- same `stable_json`/`sha256_text`
    convention `.338` itself already uses, so every entry in this journal
    always has SOME content-derived hash, never a bare `None` unless the
    caller explicitly passes one."""
    now = now or datetime.now(timezone.utc)
    if decision_hash is None:
        canonical = json.dumps(decision, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        decision_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return JournalEntry(
        appended_at=now.isoformat(),
        decision_hash=decision_hash,
        overall_verdict=overall_verdict,
        snapshot_as_of=snapshot_as_of,
        decision=dict(decision),
        context=dict(context or {}),
    )


def record_raw_decision(
    journal_path: Path,
    *,
    overall_verdict: str,
    snapshot_as_of: str,
    decision: dict[str, Any],
    decision_hash: str | None = None,
    context: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> JournalEntry:
    """`build_raw_entry()` + `append_entry()` in one call -- the
    `record_decision()` analogue for a caller whose decision is a plain
    dict. Raises `JournalError` on write failure (via `append_entry()`,
    unchanged); on success returns the `JournalEntry` that was written."""
    entry = build_raw_entry(
        overall_verdict=overall_verdict, snapshot_as_of=snapshot_as_of, decision=decision,
        decision_hash=decision_hash, context=context, now=now,
    )
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
