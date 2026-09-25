#!/usr/bin/env python3
"""
AURA v0.5.3.64 -- Real ALPACA equity-history log.

Why this exists (found while building `.363`, 2026-09-25)
------------------------------------------------------------------------
`.44`'s daily_loss check (`aura_v05344_portfolio_exposure_enforcement.py`,
`_check_daily_loss`) BLOCKs unconditionally, with reason
`INSUFFICIENT_HISTORY`, whenever there is no real same-day-prior equity
snapshot in the `equity_history` list a caller supplies -- this is
correct, deliberate fail-closed behavior ("not proven safe -> BLOCK"),
not a bug. But nothing in this repo persisted real equity observations
anywhere before this module: every existing caller of `run_stage1b_paper_
cycle`/`run_stage1a_dry_run` either supplies a hand-built test fixture
(`.55`'s own tests) or an empty list (`.356`'s dry-run CLI, harmless
there since it never submits). `.363` (the manual-trigger CLI) is the
first caller that can actually reach the broker, so an empty
`equity_history` there would silently BLOCK every real attempt via
daily_loss alone, regardless of the decision or the confirmation flag --
defeating the point of a capped first run. Martin confirmed (2026-09-25,
AskUserQuestion) building this small persistence log rather than either
self-seeding a same-run-only baseline (no cross-run tracking) or shipping
`.363` in a permanently-BLOCKED state.

Design
------------------------------------------------------------------------
Append-only JSON Lines, one real equity observation per line -- same
convention `.361`'s enforcement journal already established
(`aura_v05361_portfolio_enforcement_journal.py`), deliberately mirrored
here rather than inventing a second format. Default path
(`regime_output/equity_history_log/alpaca_equity_history.jsonl`) follows
the same `regime_output/` root `.338` already uses for its own durable
claim directories -- not a new convention.

Every observation this module ever writes is a REAL, freshly-fetched
`alpaca_client.get_account().equity` value (via `.356`'s own
`fetch_real_account_equity_usd`) -- this module never fabricates,
estimates, or backfills a number. A caller appends on every invocation,
unconditionally (ALLOW/BLOCK/ABSTAIN/no-trade alike), exactly mirroring
`.361`'s "log every cycle, not just executed trades" principle -- so the
log accumulates a genuine same-day (and cross-day) equity trail as long
as `.363` (or any future caller) keeps calling `append_observation`.

`read_equity_history()`'s return shape (`{"venue", "equity", "as_of"}`
dicts) is exactly `.343.compute_daily_loss()`'s own expected
`equity_history` element shape -- callers pass the return value straight
through as `run_stage1b_paper_cycle(equity_history=...)`, no translation.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VERSION = "AURA v0.5.3.64"

DEFAULT_EQUITY_HISTORY_LOG_PATH = Path("regime_output/equity_history_log/alpaca_equity_history.jsonl")


class EquityHistoryLogError(Exception):
    """Raised on any failure to durably write or faithfully read the log.
    Fail-closed: callers must not treat a failed write as a recorded
    observation, nor a corrupt read as a complete history -- mirrors
    `.361`'s own `JournalError` discipline exactly."""


@dataclass(frozen=True, slots=True)
class EquityObservation:
    venue: str
    equity: float
    as_of: str  # ISO-8601, timezone-aware -- the moment this equity value was true
    recorded_at: str  # ISO-8601 -- when this observation was appended to the log (may lag as_of slightly)

    def to_dict(self) -> dict[str, Any]:
        return {"venue": self.venue, "equity": self.equity, "as_of": self.as_of, "recorded_at": self.recorded_at}


def append_observation(
    log_path: Path, *, venue: str, equity: float, as_of: datetime | None = None, now: datetime | None = None,
) -> EquityObservation:
    """The only function in this module that touches disk. Append-only:
    opens in 'a' mode, never truncates, never rewrites a prior line.
    `as_of` defaults to `now` (the moment of the call) when the caller
    has no more precise timestamp for when the equity value was read;
    `now` (when omitted) is captured fresh. Raises `EquityHistoryLogError`
    (never a bare OSError) on any failure."""
    now_dt = now or datetime.now(timezone.utc)
    as_of_dt = as_of or now_dt
    observation = EquityObservation(
        venue=venue, equity=float(equity), as_of=as_of_dt.astimezone(timezone.utc).isoformat(),
        recorded_at=now_dt.astimezone(timezone.utc).isoformat(),
    )
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(observation.to_dict(), sort_keys=True, ensure_ascii=False, allow_nan=False)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as exc:
        raise EquityHistoryLogError(f"failed to append equity observation to {log_path}: {exc}") from exc
    return observation


def read_equity_history(log_path: Path) -> list[dict[str, Any]]:
    """Reads observations back in append order (oldest first), as plain
    `{"venue", "equity", "as_of"}` dicts -- exactly `.343.compute_daily_
    loss()`'s expected `equity_history` element shape (the `recorded_at`
    field is dropped here; `.44`'s check never looks at it). Returns `[]`
    if the log does not exist yet -- an empty/unstarted log is not an
    error, mirroring `.361`'s identical choice. Raises
    `EquityHistoryLogError` on a corrupt line rather than silently
    skipping it (fail-closed on corruption, not fail-open)."""
    if not log_path.exists():
        return []
    entries: list[dict[str, Any]] = []
    with log_path.open("r", encoding="utf-8") as f:
        for line_no, raw_line in enumerate(f, start=1):
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                obj = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise EquityHistoryLogError(f"corrupt equity history entry at {log_path}:{line_no}: {exc}") from exc
            entries.append({"venue": obj["venue"], "equity": obj["equity"], "as_of": obj["as_of"]})
    return entries


def record_and_read_equity_history(
    log_path: Path, *, venue: str, equity: float, as_of: datetime | None = None, now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Convenience for the common caller sequence: append THIS real
    observation first, then read the full history back (including the
    just-appended entry) -- so the very same call that supplies today's
    current equity also contributes it as tomorrow's (or a later cycle
    this same day's) same-day-prior baseline. On a brand-new log, this
    means the FIRST-ever observation of a day is its own day-start
    baseline (daily_loss_pct == 0.0 for that first cycle, a REAL number,
    not fabricated) -- every subsequent cycle that day sees a genuine,
    strictly-earlier prior observation."""
    append_observation(log_path, venue=venue, equity=equity, as_of=as_of, now=now)
    return read_equity_history(log_path)
