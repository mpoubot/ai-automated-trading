#!/usr/bin/env python3
"""
AURA v0.5.3.54 — Alpaca equity/ETF order fill reconciliation.

WHY THIS MODULE EXISTS
------------------------------------------------------------------------
`.38`'s own `observe_and_reconcile_alpaca_equity_execution()` is an
explicit, disclosed placeholder: it always returns `NOT_YET_IMPLEMENTED`
and its own docstring says "a genuine Alpaca-equity intent ledger and
reconciliation engine ... does not exist in this repository. This
function defines the call shape a future implementation would fill,
without fabricating a verdict now."

Martin's Track B Stage 1B instruction requires every attempted order to
be logged with, among other fields, "fill status" and "fill price" — `.35`
`submit()` only returns the broker's status AT SUBMISSION TIME
(`broker_status`, typically `"new"`/`"accepted"` for a market order, not
yet `"filled"`), because Alpaca fills asynchronously even for paper market
orders. Something has to ask the broker again, after submission, whether
the order actually filled — that "something" does not exist anywhere in
this codebase today. This module is that "future implementation",
written now because Stage 1B genuinely needs it, not filled in as an
afterthought.

Scope, deliberately narrow
------------------------------------------------------------------------
This module does ONE thing: given a `client_order_id` this repo's own
adapter already submitted, poll the broker (via the SAME, already-tested
`client.get_order_by_client_id()` call shape `.35.submit()` already uses
for its idempotency preflight — no new SDK call shape is introduced) until
the order reaches a terminal state or a caller-supplied timeout elapses.
It does not build a persistent intent ledger, does not attempt
crash-recovery/restart reconciliation (a `.53`-family "genuine .28/.29/.30
equivalent for Alpaca equities" remains a disclosed, separate future
milestone — see `.38`'s own docstring, "Known limitations"), and does not
retry a failed poll beyond what `timeout_seconds` allows.

UNVERIFIED against live Alpaca (disclosed, per this project's own
CLAIMED/PROVEN/UNVERIFIED convention, matching `.35`'s own disclosure for
its shortability translation)
------------------------------------------------------------------------
The exact lowercase Alpaca `OrderStatus` string values this module
classifies as terminal (`"filled"`, `"canceled"`, `"expired"`,
`"rejected"`) are taken from alpaca-py's public `OrderStatus` enum and
Alpaca's own API documentation. No live account has ever been reached
from this sandbox (identical disclosed limitation to `.35`/`.49`/`.51`/
`.52`/`.53`), so this exact set of terminal-status strings has never been
observed against a real fill. `TERMINAL_STATUSES`/`OPEN_STATUSES` are
named module-level constants specifically so a Stage 1B discrepancy
(a real Alpaca status string this module does not recognize) is easy to
find and fix in one place, per Martin's "any discrepancy ... becomes a
Stage 1B defect to investigate" instruction — an unrecognized status is
never silently treated as terminal or as filled; see `_classify_status()`.

Never fabricates a fill. A timeout returns status `"TIMEOUT"` (the order
may still be open, or may have filled a moment after the last poll) —
never `"FILLED"`. A poll that raises returns status `"POLL_ERROR"` with
the exception recorded — never treated as any other status.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

VERSION = "AURA v0.5.3.54"
ENGINE = "ALPACA_EQUITY_FILL_RECONCILIATION"
SCHEMA_VERSION = "1.0"


class FillReconciliationError(Exception):
    """Programmer-error / invalid-input only — never raised for an order
    that is simply still open or a broker call that simply fails; those
    are ordinary, handled outcomes reported on `FillReconciliationResult`,
    mirroring every other milestone's error-class discipline in this
    repo."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Per alpaca-py's OrderStatus enum / Alpaca's own API docs — UNVERIFIED
# against a live account from this sandbox, see module docstring.
TERMINAL_STATUSES = frozenset({"filled", "canceled", "expired", "rejected"})
OPEN_STATUSES = frozenset({
    "new", "accepted", "pending_new", "accepted_for_bidding", "partially_filled",
    "pending_cancel", "pending_replace", "calculated", "stopped", "suspended",
})
_STATUS_TO_RESULT = {
    "filled": "FILLED",
    "canceled": "CANCELED",
    "expired": "EXPIRED",
    "rejected": "REJECTED",
    "partially_filled": "PARTIALLY_FILLED",
}


@dataclass(frozen=True, slots=True)
class FillReconciliationResult:
    schema_version: str
    engine: str
    agent_version: str
    client_order_id: str
    status: str  # FILLED | PARTIALLY_FILLED | OPEN | CANCELED | EXPIRED | REJECTED | TIMEOUT | POLL_ERROR | ORDER_NOT_FOUND
    broker_status: str | None  # raw, un-translated status string the broker returned on the LAST successful poll
    broker_order_id: str | None
    filled_qty: str | None
    filled_avg_price: str | None
    submitted_at: str | None
    filled_at: str | None
    polls_performed: int
    timed_out: bool
    error: str | None
    observed_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "engine": self.engine, "agent_version": self.agent_version,
            "client_order_id": self.client_order_id, "status": self.status, "broker_status": self.broker_status,
            "broker_order_id": self.broker_order_id, "filled_qty": self.filled_qty,
            "filled_avg_price": self.filled_avg_price, "submitted_at": self.submitted_at,
            "filled_at": self.filled_at, "polls_performed": self.polls_performed,
            "timed_out": self.timed_out, "error": self.error, "observed_at": self.observed_at,
        }


def _classify_status(raw_status: str | None) -> tuple[str, bool]:
    """Returns (result_status, is_terminal). An unrecognized status string
    is treated as OPEN (never as filled, never as an error) -- fail-closed
    toward "keep polling / do not claim a fill", exactly mirroring `.35`'s
    own UNKNOWN-is-not-SHORTABLE convention for shortability."""
    if raw_status is None:
        return "OPEN", False
    normalized = raw_status.lower()
    if normalized in TERMINAL_STATUSES:
        return _STATUS_TO_RESULT.get(normalized, normalized.upper()), True
    if normalized == "partially_filled":
        return "PARTIALLY_FILLED", False
    return "OPEN", False


def poll_order_fill(
    client: Any,
    client_order_id: str,
    *,
    timeout_seconds: float,
    poll_interval_seconds: float,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], float] = time.monotonic,
) -> FillReconciliationResult:
    """Polls `client.get_order_by_client_id(client_order_id)` -- the SAME
    call `.35.submit()` already uses -- until the order reaches a terminal
    state (see `TERMINAL_STATUSES`) or `timeout_seconds` elapses. `client`
    is any object exposing `.get_order_by_client_id(client_order_id)`: a
    real alpaca-py TradingClient in production, a fake test double in
    every test in this milestone (mirroring `.35`'s own testing
    convention -- no live Alpaca order required to prove this module
    works).

    Never sleeps after the LAST poll (whether that poll was terminal or
    the timeout was reached), and always performs at least one poll
    before ever checking the timeout, so `timeout_seconds=0` still
    performs exactly one poll rather than reporting TIMEOUT with zero
    information."""
    if not isinstance(client_order_id, str) or not client_order_id.strip():
        raise FillReconciliationError("MISSING_CLIENT_ORDER_ID")
    if timeout_seconds < 0:
        raise FillReconciliationError("INVALID_TIMEOUT_SECONDS:must be >= 0")
    if poll_interval_seconds <= 0:
        raise FillReconciliationError("INVALID_POLL_INTERVAL_SECONDS:must be > 0")

    deadline = now_fn() + timeout_seconds
    polls_performed = 0
    last_error: str | None = None
    last_order: Any = None

    while True:
        polls_performed += 1
        try:
            order = client.get_order_by_client_id(client_order_id)
            last_order = order
            last_error = None
        except Exception as exc:  # noqa: BLE001 -- classified below, never silently swallowed
            last_error = f"{type(exc).__name__}: {exc}"
            order = None

        if order is None:
            if last_error is not None and now_fn() >= deadline:
                return FillReconciliationResult(
                    schema_version=SCHEMA_VERSION, engine=ENGINE, agent_version=VERSION,
                    client_order_id=client_order_id, status="POLL_ERROR", broker_status=None,
                    broker_order_id=None, filled_qty=None, filled_avg_price=None,
                    submitted_at=None, filled_at=None, polls_performed=polls_performed,
                    timed_out=True, error=last_error, observed_at=now(),
                )
        else:
            raw_status = getattr(order, "status", None)
            raw_status = getattr(raw_status, "value", raw_status)  # alpaca-py enum -> str, mirrors .35's own `g()` helper
            result_status, is_terminal = _classify_status(str(raw_status) if raw_status is not None else None)
            if is_terminal:
                return FillReconciliationResult(
                    schema_version=SCHEMA_VERSION, engine=ENGINE, agent_version=VERSION,
                    client_order_id=client_order_id, status=result_status,
                    broker_status=str(raw_status) if raw_status is not None else None,
                    broker_order_id=str(getattr(order, "id", None)),
                    filled_qty=_str_or_none(getattr(order, "filled_qty", None)),
                    filled_avg_price=_str_or_none(getattr(order, "filled_avg_price", None)),
                    submitted_at=_str_or_none(getattr(order, "submitted_at", None)),
                    filled_at=_str_or_none(getattr(order, "filled_at", None)),
                    polls_performed=polls_performed, timed_out=False, error=None, observed_at=now(),
                )

        if now_fn() >= deadline:
            if last_order is not None:
                raw_status = getattr(last_order, "status", None)
                raw_status = getattr(raw_status, "value", raw_status)
                result_status, _ = _classify_status(str(raw_status) if raw_status is not None else None)
                return FillReconciliationResult(
                    schema_version=SCHEMA_VERSION, engine=ENGINE, agent_version=VERSION,
                    client_order_id=client_order_id,
                    status="PARTIALLY_FILLED" if result_status == "PARTIALLY_FILLED" else "TIMEOUT",
                    broker_status=str(raw_status) if raw_status is not None else None,
                    broker_order_id=str(getattr(last_order, "id", None)),
                    filled_qty=_str_or_none(getattr(last_order, "filled_qty", None)),
                    filled_avg_price=_str_or_none(getattr(last_order, "filled_avg_price", None)),
                    submitted_at=_str_or_none(getattr(last_order, "submitted_at", None)),
                    filled_at=_str_or_none(getattr(last_order, "filled_at", None)),
                    polls_performed=polls_performed, timed_out=True, error=None, observed_at=now(),
                )
            return FillReconciliationResult(
                schema_version=SCHEMA_VERSION, engine=ENGINE, agent_version=VERSION,
                client_order_id=client_order_id, status="POLL_ERROR", broker_status=None,
                broker_order_id=None, filled_qty=None, filled_avg_price=None,
                submitted_at=None, filled_at=None, polls_performed=polls_performed,
                timed_out=True, error=last_error, observed_at=now(),
            )

        sleep_fn(poll_interval_seconds)


def _str_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


# Disclosure list, mirroring `.35`'s own convention: exactly which
# functions in this module are network-capable.
NETWORK_CAPABLE_FUNCTIONS = frozenset({"poll_order_fill"})
