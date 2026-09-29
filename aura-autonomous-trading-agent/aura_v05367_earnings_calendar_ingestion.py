#!/usr/bin/env python3
"""
AURA v0.5.3.67 — Earnings calendar ingestion (Financial Modeling Prep).

WHAT THIS MODULE IS, AND IS DELIBERATELY NOT
------------------------------------------------------------------------
INGESTION ONLY, mirroring `.46`'s own "ingestion, never decides" split.
This module's entire job is to fetch a date-range earnings calendar from
Financial Modeling Prep's free-tier `/stable/earnings-calendar` endpoint,
parse it into a minimal, universe-agnostic cache, and persist it to disk
(atomic write, same convention as `.46`/`.43`). It never decides whether a
symbol is blacked out -- that is `.68`'s job, consuming this module's
`EarningsCalendarState` output. It never blocks or authorizes a trade.

Why this exists (Martin, chat, 2026-09-29 -- "Lets go for Earnings
blackout")
------------------------------------------------------------------------
Confirmed by direct code introspection (not assumed): Alpaca's
`alpaca-py` TradingClient exposes `get_corporate_announcements` and
`CorporateActionType` (DIVIDEND/MERGER/SPINOFF/SPLIT only) -- no earnings
date anywhere in its API. `.46`'s own docstring explicitly reserves
earnings-blackout / event-risk windows as "OUT of scope ... deferred to
its own future milestone". Provider research (Martin, AskUserQuestion,
2026-09-29): Koyfin has no API at all (confirmed via its own FAQ);
Massive.com's earnings data is a $99/mo Benzinga add-on, not free; the
`anthropic-financial-services` GitHub repo is agent/skill scaffolding, not
a data source. Financial Modeling Prep's free "Basic" plan (250 calls/day,
no card, no documented reliability complaints on this endpoint, vs.
Finnhub's free earnings-calendar endpoint which has multiple open GitHub
issues reporting wrong years / empty results) was Martin's confirmed
choice.

Design choices, made explicit rather than silently assumed
------------------------------------------------------------------------
  - ONE whole-market date-range API call covers the entire universe (the
    endpoint is not per-symbol) -- filtering to this repo's pinned
    universe happens client-side, after the fetch. This is why the free
    tier's 250-calls/day limit is not a practical constraint here: even a
    fresh fetch every single cycle would be nowhere near that limit, and
    the cache (below) makes it unnecessary anyway.
  - The on-disk cache stores the UNFILTERED parsed entries for the fetched
    date window, not a universe-filtered subset. `calendar_by_symbol` is
    always re-derived from those cached entries against whichever
    `universe_symbols` the caller passes THIS run -- so a cache built
    before a universe change (e.g. the 2026-09-29 ETF curation pass) is
    still correct after one, with no cache-invalidation logic needed.
  - Fail-closed on a stale-or-missing cache whose refetch fails (Martin,
    AskUserQuestion, 2026-09-29: "Fail closed -- block all new entries"):
    `refresh_earnings_calendar_if_stale` NEVER raises for an ordinary
    fetch failure -- it returns an `EarningsCalendarState` with
    `status="UNAVAILABLE"`, mirroring this repo's "an ordinary handled
    outcome is recorded, not raised" discipline (see `.55`'s own
    `Stage1RunnerError` docstring). `.68` is the module that turns
    `status != "OK"` into an actual blocked verdict for every symbol --
    this module only ever reports data availability honestly.
  - Cache max-age default is 24 hours: earnings dates do not move within a
    trading day, and Martin's own operational routine already restarts
    `.365` close to daily, so this keeps the common case to one real
    network call per day while never serving data older than that without
    at least attempting a refresh.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

VERSION = "AURA v0.5.3.67"
ENGINE = "EARNINGS_CALENDAR_INGESTION"

FMP_API_KEY_ENV = "FMP_API_KEY"
FMP_EARNINGS_CALENDAR_URL = "https://financialmodelingprep.com/stable/earnings-calendar"

DEFAULT_CACHE_MAX_AGE_HOURS = 24.0
DEFAULT_LOOKBACK_DAYS = 3
DEFAULT_LOOKAHEAD_DAYS = 30
DEFAULT_STATE_FILENAME = "earnings_calendar_cache.json"

# HTTP request timeout -- this repo has no existing outbound-HTTP module to
# copy a convention from (`.46` uses alpaca-py's own NewsClient, not raw
# `requests`), so a conservative, explicit timeout is chosen here rather
# than left to `requests`' own no-timeout default (which would hang
# forever on a stalled connection -- never acceptable in a live-trading
# startup path).
DEFAULT_REQUEST_TIMEOUT_SECONDS = 15.0


class EarningsCalendarIngestionError(Exception):
    """Programmer-error / invalid-input only (e.g. a missing API key at
    startup) -- never raised for an ordinary handled outcome like a failed
    fetch; see module docstring, 'Fail-closed on a stale-or-missing cache'."""


# ============================================================================
# stable_json / _atomic_write_json -- copied, not imported, matching this
# repo's own established convention (see `.46`'s identical copy).
# ============================================================================


def stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def _atomic_write_json(path: Path, obj: Any) -> None:
    """Same tmp-then-os.replace() pattern as `.29`/`.43`/`.46`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False, allow_nan=False, sort_keys=True)
        f.write("\n")
    os.replace(tmp_path, path)


# ============================================================================
# Credential loading -- self-contained (FMP is not an Alpaca credential,
# does not belong in `.356`'s Alpaca-specific loader).
# ============================================================================


def load_fmp_api_key() -> str:
    """Reads ONLY `FMP_API_KEY` from the environment. Raises
    `EarningsCalendarIngestionError` (fail-closed, no silent empty-string
    fallback) if unset -- mirrors `.356.load_equity_paper_credentials()`'s
    own no-fallback discipline for its own required credential."""
    api_key = os.getenv(FMP_API_KEY_ENV)
    if not api_key:
        raise EarningsCalendarIngestionError(
            f"MISSING_FMP_API_KEY:set {FMP_API_KEY_ENV} in your environment (see .env.example). "
            "Get a free key at https://site.financialmodelingprep.com/ (Basic plan, no card required)."
        )
    return api_key


# ============================================================================
# Types
# ============================================================================


@dataclass(frozen=True, slots=True)
class EarningsCalendarEntry:
    """One row from FMP's earnings-calendar response. Only `symbol` and
    `report_date` are load-bearing for `.68`'s blackout decision -- every
    other field is carried through purely for audit/disclosure, never
    consulted by the gate itself (Martin's confirmed scope is "day-of
    only", not a BMO/AMC-aware window)."""

    symbol: str
    report_date: date
    time_hint: str | None  # FMP's raw "time" field (e.g. "bmo"/"amc"), informational only
    fiscal_date_ending: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "report_date": self.report_date.isoformat(),
            "time_hint": self.time_hint,
            "fiscal_date_ending": self.fiscal_date_ending,
        }


@dataclass(frozen=True, slots=True)
class EarningsCalendarState:
    """The one object `.68` consumes. `status="OK"` is the only status
    under which `calendar_by_symbol` may be trusted; any other status
    means `.68` must fail closed for every symbol (see module docstring)."""

    status: str  # "OK" | "UNAVAILABLE"
    source: str  # "FETCHED_FRESH" | "CACHE_REUSED" | "FETCH_FAILED_NO_USABLE_CACHE"
    calendar_by_symbol: dict[str, tuple[date, ...]] = field(default_factory=dict)
    generated_at: str | None = None
    from_date: str | None = None
    to_date: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "source": self.source,
            "calendar_by_symbol": {
                symbol: [d.isoformat() for d in dates] for symbol, dates in self.calendar_by_symbol.items()
            },
            "generated_at": self.generated_at,
            "from_date": self.from_date,
            "to_date": self.to_date,
            "error": self.error,
        }


# ============================================================================
# Fetch -- network I/O isolated to this one function, `http_get`
# dependency-injected so every other function/test never touches the
# network, matching this repo's universal fake-client testing convention.
# ============================================================================


def fetch_earnings_calendar_raw(
    api_key: str,
    *,
    from_date: date,
    to_date: date,
    http_get: Callable[..., Any] | None = None,
    timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    """One HTTP GET, whole-market, date-range -- see module docstring for
    why this is never called per-symbol. Raises on any failure (bad
    status, network error, malformed JSON) -- never returns a partial or
    empty-on-error result silently; the caller (
    `refresh_earnings_calendar_if_stale`) is what turns that into a
    fail-closed `EarningsCalendarState`, not this function pretending
    nothing went wrong."""
    if http_get is None:
        import requests  # local import: keeps this module importable/testable with zero
        # network dependency present when a fake http_get is always supplied, matching
        # this repo's "network library imported only where actually used" convention.

        http_get = requests.get

    response = http_get(
        FMP_EARNINGS_CALENDAR_URL,
        params={"from": from_date.isoformat(), "to": to_date.isoformat(), "apikey": api_key},
        timeout=timeout_seconds,
    )
    status_code = getattr(response, "status_code", None)
    if status_code != 200:
        raise EarningsCalendarIngestionError(
            f"FMP_EARNINGS_CALENDAR_HTTP_{status_code}:{getattr(response, 'text', '')[:500]}"
        )
    payload = response.json()
    if not isinstance(payload, list):
        raise EarningsCalendarIngestionError(
            f"FMP_EARNINGS_CALENDAR_UNEXPECTED_RESPONSE_SHAPE:expected a JSON list, got {type(payload).__name__}"
        )
    return payload


# ============================================================================
# Parse -- pure. Tolerant of missing/renamed optional fields (FMP's own
# field naming has drifted between its legacy v3 and current stable API
# per public third-party write-ups); a row missing the two load-bearing
# fields (symbol, date) is skipped, never crashes the whole batch --
# matching `.53`'s own "a bad row is recorded/skipped, never blocks the
# rest" discipline.
# ============================================================================


def parse_earnings_calendar_entries(raw_rows: list[dict[str, Any]]) -> tuple[EarningsCalendarEntry, ...]:
    entries: list[EarningsCalendarEntry] = []
    for row in raw_rows:
        if not isinstance(row, dict):
            continue
        symbol = row.get("symbol")
        raw_date = row.get("date")
        if not symbol or not raw_date:
            continue
        try:
            report_date = date.fromisoformat(str(raw_date)[:10])
        except ValueError:
            continue
        time_hint = row.get("time")
        fiscal_date_ending = row.get("fiscalDateEnding")
        entries.append(
            EarningsCalendarEntry(
                symbol=str(symbol).upper(),
                report_date=report_date,
                time_hint=str(time_hint) if time_hint else None,
                fiscal_date_ending=str(fiscal_date_ending) if fiscal_date_ending else None,
            )
        )
    return tuple(entries)


def build_calendar_by_symbol(
    entries: Sequence[EarningsCalendarEntry], universe_symbols: Sequence[str],
) -> dict[str, tuple[date, ...]]:
    """Filters `entries` (universe-agnostic, as cached) down to
    `universe_symbols` and groups report dates per symbol, sorted,
    deduplicated. A symbol genuinely absent from FMP's data (e.g. every
    non-reporting ETF in this repo's pinned universe -- DIA, the SPDR
    sector ETFs) simply never appears as a key here; `.68` treats that as
    ordinary "not in blackout today", never as a data-unavailability
    signal (see `.68`'s own module docstring for that distinction)."""
    wanted = {s.upper() for s in universe_symbols}
    by_symbol: dict[str, set[date]] = {}
    for entry in entries:
        if entry.symbol not in wanted:
            continue
        by_symbol.setdefault(entry.symbol, set()).add(entry.report_date)
    return {symbol: tuple(sorted(dates)) for symbol, dates in by_symbol.items()}


# ============================================================================
# Cache -- atomic-write JSON, universe-agnostic (stores parsed entries, not
# a pre-filtered calendar_by_symbol; see module docstring).
# ============================================================================


def _cache_path(state_dir: Path) -> Path:
    return state_dir / DEFAULT_STATE_FILENAME


def save_calendar_cache(
    state_dir: Path, *, entries: Sequence[EarningsCalendarEntry], generated_at: str, from_date: date, to_date: date,
) -> None:
    _atomic_write_json(
        _cache_path(state_dir),
        {
            "schema_version": "1.0",
            "engine": ENGINE,
            "agent_version": VERSION,
            "generated_at": generated_at,
            "from_date": from_date.isoformat(),
            "to_date": to_date.isoformat(),
            "entries": [e.to_dict() for e in entries],
        },
    )


def load_calendar_cache(state_dir: Path) -> dict[str, Any] | None:
    """Never raises. A missing or corrupt cache file is treated identically
    -- both mean "no usable cache", fail-closed handled by the caller."""
    path = _cache_path(state_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _parse_cached_entries(cache: dict[str, Any]) -> tuple[EarningsCalendarEntry, ...]:
    entries = []
    for row in cache.get("entries", []):
        try:
            entries.append(
                EarningsCalendarEntry(
                    symbol=row["symbol"],
                    report_date=date.fromisoformat(row["report_date"]),
                    time_hint=row.get("time_hint"),
                    fiscal_date_ending=row.get("fiscal_date_ending"),
                )
            )
        except (KeyError, ValueError):
            continue
    return tuple(entries)


def is_cache_fresh(generated_at: str, *, now: datetime, max_age_hours: float) -> bool:
    try:
        generated_dt = datetime.fromisoformat(generated_at)
    except ValueError:
        return False
    if generated_dt.tzinfo is None:
        generated_dt = generated_dt.replace(tzinfo=timezone.utc)
    age_seconds = (now.astimezone(timezone.utc) - generated_dt.astimezone(timezone.utc)).total_seconds()
    return 0 <= age_seconds <= max_age_hours * 3600.0


# ============================================================================
# The one orchestration function callers actually use.
# ============================================================================


def refresh_earnings_calendar_if_stale(
    *,
    api_key: str,
    state_dir: Path,
    universe_symbols: Sequence[str],
    now: datetime,
    max_age_hours: float = DEFAULT_CACHE_MAX_AGE_HOURS,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    lookahead_days: int = DEFAULT_LOOKAHEAD_DAYS,
    http_get: Callable[..., Any] | None = None,
) -> EarningsCalendarState:
    """Reuses a fresh cache with zero network calls; fetches and re-caches
    when the cache is stale or missing. NEVER raises for a fetch failure
    (fail-closed is expressed as `status="UNAVAILABLE"` data, per module
    docstring) -- an `EarningsCalendarIngestionError` can still propagate
    from `load_fmp_api_key()` if the caller passes a bad/empty `api_key`,
    since that IS a programmer error, not an ordinary runtime outcome."""
    today = now.astimezone(timezone.utc).date()
    from_date = today - timedelta(days=lookback_days)
    to_date = today + timedelta(days=lookahead_days)

    cache = load_calendar_cache(state_dir)
    if cache is not None and is_cache_fresh(cache.get("generated_at", ""), now=now, max_age_hours=max_age_hours):
        entries = _parse_cached_entries(cache)
        return EarningsCalendarState(
            status="OK",
            source="CACHE_REUSED",
            calendar_by_symbol=build_calendar_by_symbol(entries, universe_symbols),
            generated_at=cache.get("generated_at"),
            from_date=cache.get("from_date"),
            to_date=cache.get("to_date"),
        )

    try:
        raw_rows = fetch_earnings_calendar_raw(api_key, from_date=from_date, to_date=to_date, http_get=http_get)
        entries = parse_earnings_calendar_entries(raw_rows)
    except Exception as exc:  # noqa: BLE001 -- any fetch/parse failure is fail-closed data, never propagated
        return EarningsCalendarState(
            status="UNAVAILABLE",
            source="FETCH_FAILED_NO_USABLE_CACHE",
            calendar_by_symbol={},
            error=f"{type(exc).__name__}: {exc}",
        )

    generated_at = now_iso(now)
    save_calendar_cache(state_dir, entries=entries, generated_at=generated_at, from_date=from_date, to_date=to_date)
    return EarningsCalendarState(
        status="OK",
        source="FETCHED_FRESH",
        calendar_by_symbol=build_calendar_by_symbol(entries, universe_symbols),
        generated_at=generated_at,
        from_date=from_date.isoformat(),
        to_date=to_date.isoformat(),
    )


# ============================================================================
# Standalone CLI -- manual cache refresh/inspection, mirroring
# `verify_new_etf_shortability.py`'s own standalone-script pattern.
# ============================================================================


def main(argv: list[str] | None = None) -> int:  # pragma: no cover -- live network wiring
    import argparse
    import sys

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--symbols", type=str, required=True, help="Comma-separated symbol list to filter to.")
    parser.add_argument("--force", action="store_true", help="Ignore any fresh cache and fetch now.")
    args = parser.parse_args(argv)

    try:
        api_key = load_fmp_api_key()
    except EarningsCalendarIngestionError as exc:
        print(f"FAIL-CLOSED: {exc}", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    if args.force:
        cache_path = _cache_path(args.state_dir)
        if cache_path.exists():
            cache_path.unlink()

    state = refresh_earnings_calendar_if_stale(
        api_key=api_key, state_dir=args.state_dir,
        universe_symbols=tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip()),
        now=now,
    )
    print(stable_json(state.to_dict()))
    return 0 if state.status == "OK" else 1


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(main())
