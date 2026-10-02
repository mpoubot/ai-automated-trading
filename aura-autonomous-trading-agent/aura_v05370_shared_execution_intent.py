#!/usr/bin/env python3
"""Shared execution-intent log -- closes the race-condition window between
AURA and DELTAX when both trade the same Alpaca account.

WHY THIS EXISTS
------------------------------------------------------------------------
AURA and DELTAX each reconcile the broker's own position list before
sizing a new trade, so each one is already aware of the OTHER's FILLED
positions (both read the same account). What neither can see is an order
the other has just SUBMITTED but that has not filled yet -- a window of
seconds to a couple of minutes. If both systems happen to submit new risk
inside that window, each one's pre-trade check was computed from a
snapshot that doesn't yet reflect the other's pending order, and the two
can stack risk past either one's intended cap. (DELTAX's own code
comments flag exactly this failure mode between two of its own
concurrent cron cycles -- this module closes the same gap between AURA
and DELTAX.)

This file is a small, append-only, shared log of "I am about to commit
this much new risk" notes, written by whichever system is about to
submit, and read by both before they size. It is NOT a replacement for
reading the broker -- the broker's own position list is still the
source of truth for anything that has actually filled. This only covers
the short gap before a fill is visible there.

ONE COPY OF THIS FILE goes into both the AURA repo and the DELTAX repo
(identical content -- it has no project-specific imports). Both sides
point at the SAME path on disk (set via whatever argument/env var the
caller wires it through) so they're reading and writing one shared log.

FORMAT
------------------------------------------------------------------------
Append-only JSONL. One line per intent:
    {"system": "AURA" | "DELTAX", "venue": "ALPACA", "symbol": "...",
     "side": "...", "estimated_risk_usd": <float>, "ts": "<ISO8601 UTC>"}

`estimated_risk_usd` is intentionally a rough, conservative-direction
proxy (premium notional for DELTAX, raw position-value delta for AURA),
not a precise max-loss figure -- it only has to be in the right ballpark
for a few minutes, not exact.

CONCURRENCY
------------------------------------------------------------------------
Both systems are separate OS processes, possibly on Windows, possibly
writing at the same moment. `mkdir` is atomic on every filesystem this
runs on, so a tiny mkdir-based lock (mirroring the lock DELTAX's own
bin/deltax-cron.sh already uses, with the same stale-lock reclaim logic)
guards every read-modify-write. A lock older than LOCK_STALE_SECONDS is
assumed abandoned (the holder died without cleaning up) and reclaimed --
never blocks forever.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOCK_STALE_SECONDS = 30
LOCK_WAIT_RETRY_SECONDS = 0.05
LOCK_WAIT_TIMEOUT_SECONDS = 5.0

# Entries older than this are dropped from the file on every write, so it
# never grows without bound. Must be comfortably larger than any
# max_age_seconds a caller will ever query with.
DEFAULT_RETENTION_SECONDS = 2 * 60 * 60  # 2 hours


class _FileLock:
    """mkdir-based lock on `<path>.lock`. Atomic on Windows and POSIX
    alike. Reclaims a lock older than LOCK_STALE_SECONDS, on the
    assumption its holder crashed without cleaning up -- the same
    reasoning DELTAX's own bin/deltax-cron.sh uses for its /tmp lock."""

    def __init__(self, target_path: Path):
        self.lock_dir = target_path.with_suffix(target_path.suffix + ".lock")

    def __enter__(self) -> "_FileLock":
        deadline = time.monotonic() + LOCK_WAIT_TIMEOUT_SECONDS
        while True:
            try:
                self.lock_dir.mkdir(parents=True, exist_ok=False)
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.lock_dir.stat().st_mtime
                except OSError:
                    age = 0.0
                if age > LOCK_STALE_SECONDS:
                    try:
                        self.lock_dir.rmdir()
                    except OSError:
                        pass
                    continue
                if time.monotonic() >= deadline:
                    # Never block forever: proceed WITHOUT the lock rather
                    # than hang a trading cycle. A lost update here means at
                    # worst one intent entry is dropped -- the broker
                    # reconciliation both systems already do on every cycle
                    # is still the real safety net.
                    return self
                time.sleep(LOCK_WAIT_RETRY_SECONDS)

    def __exit__(self, *exc_info: Any) -> None:
        try:
            self.lock_dir.rmdir()
        except OSError:
            pass


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def _parse_ts(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _read_all(path: Path) -> list[dict]:
    if not path.exists():
        return []
    entries: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a corrupt line is dropped, never lets a reader crash
    return entries


def _write_all(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, sort_keys=True) + "\n")
    os.replace(tmp, path)  # atomic on Windows and POSIX


def append_intent(
    path: Path, *, system: str, venue: str, symbol: str,
    estimated_risk_usd: float, side: str = "", now: datetime | None = None,
    retention_seconds: float = DEFAULT_RETENTION_SECONDS,
) -> dict:
    """Append one intent record and prune anything older than
    `retention_seconds` in the same pass, so the file never grows
    unbounded. Returns the record written."""
    record = {
        "system": system, "venue": venue, "symbol": symbol, "side": side,
        "estimated_risk_usd": float(estimated_risk_usd), "ts": _now_iso(now),
    }
    cutoff = (now or datetime.now(timezone.utc))
    with _FileLock(path):
        entries = _read_all(path)
        kept = []
        for e in entries:
            ts = _parse_ts(e.get("ts", ""))
            if ts is not None and (cutoff - ts).total_seconds() <= retention_seconds:
                kept.append(e)
        kept.append(record)
        _write_all(path, kept)
    return record


def read_recent_intent(
    path: Path, *, max_age_seconds: float, exclude_system: str | None = None,
    venue: str | None = None, now: datetime | None = None,
) -> list[dict]:
    """Entries newer than `max_age_seconds`, optionally excluding one
    system's own entries and/or filtering to one venue. Read-only --
    does not prune (append_intent does that); safe to call as often as
    needed."""
    cutoff = (now or datetime.now(timezone.utc))
    with _FileLock(path):
        entries = _read_all(path)
    out = []
    for e in entries:
        if exclude_system is not None and e.get("system") == exclude_system:
            continue
        if venue is not None and e.get("venue") != venue:
            continue
        ts = _parse_ts(e.get("ts", ""))
        if ts is None or (cutoff - ts).total_seconds() > max_age_seconds:
            continue
        out.append(e)
    return out


def sum_recent_foreign_risk(
    path: Path, *, own_system: str, max_age_seconds: float,
    venue: str | None = None, now: datetime | None = None,
) -> float:
    """Convenience: total estimated_risk_usd from every OTHER system's
    recent entries. This is what a caller adds to its own
    about-to-submit risk figure before checking a shared cap."""
    entries = read_recent_intent(
        path, max_age_seconds=max_age_seconds, exclude_system=own_system,
        venue=venue, now=now,
    )
    return sum(abs(float(e.get("estimated_risk_usd", 0.0))) for e in entries)
