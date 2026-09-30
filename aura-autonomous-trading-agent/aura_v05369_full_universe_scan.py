#!/usr/bin/env python3
"""
AURA v0.5.3.69 — Full tradable-universe scan (Alpaca asset list).

WHAT THIS MODULE IS, AND IS DELIBERATELY NOT
------------------------------------------------------------------------
INGESTION ONLY, mirroring `.67`'s own "ingestion, never decides" split
and `.51`'s own pinned-universe precedent. This module's entire job is to
fetch Alpaca's own tradable US-equity asset list, filter it down to
`tradable=True` (Martin's explicit, final confirmed scope -- see "Filter
scope" below), and persist a cache to disk. It does not fetch bars, does
not compute any signal, and does not decide which symbols actually
receive an order that cycle -- exactly as `.51`'s existing pinned-universe
scan does not either; `.53`'s already-built ranking/cap logic is what
narrows candidates to actual decisions, unchanged by this module.

Why this exists (Martin, chat, 2026-09-30 -- "full universe expansion")
------------------------------------------------------------------------
`.51`'s pinned universe (39 symbols as of the 2026-09-29 ETF curation) is
a curated, hand-maintained list, not the "whole market" Martin's own
2026-09-30 status note called out as a gap ("if you want 'full universe'
to mean something closer to the whole S&P 500 or similar, that would be
new work"). Martin's explicit choices (AskUserQuestion, 2026-09-30):
scope = the whole tradable US equity market (not S&P 500/Russell 1000);
source = Alpaca's own tradable-assets list (no new API key/dependency);
filter = `tradable=True` ONLY -- Martin explicitly reversed his own
earlier "yes, filter by min price/volume" answer when asked to confirm,
so this module does NOT apply any price, dollar-volume, or exchange
filter. That means this scan's output legitimately includes thin,
low-priced, and OTC-listed names; nothing in this module or downstream
softens that. `.55`'s existing `sizing_failures`/`fetch_failures`
handling (a symbol that cannot be sized or fetched is skipped for that
cycle and reported, never fabricated) is the only safety net a symbol
this permissive gets, and it already existed before this module.

Filter scope, made explicit rather than silently assumed
------------------------------------------------------------------------
  - `asset_class=AssetClass.US_EQUITY`, `status=AssetStatus.ACTIVE` are
    requested server-side (Alpaca's own `GetAssetsRequest` filters) --
    inactive/delisted assets and non-equity asset classes are never
    fetched at all, not merely filtered out client-side.
  - `tradable=True` is filtered client-side after the fetch (Alpaca's
    `GetAssetsRequest` has no server-side tradable filter) -- this is the
    ONLY filter this module applies beyond the two above. No price, no
    volume, no exchange restriction.
  - ETF vs STOCK classification: Alpaca's `Asset` model has no ETF/stock
    field at all (confirmed by direct field introspection -- see
    `alpaca.trading.models.Asset`'s field list). At full-market scale,
    `.56`'s existing `KNOWN_ETF_SYMBOLS` (11 sector SPDRs + 6 broad-market
    ETFs) cannot cover the thousands of real ETFs in the full universe.
    This module classifies every symbol in `KNOWN_ETF_SYMBOLS` as "ETF"
    and defaults every other symbol to "STOCK" -- an explicit, disclosed
    approximation, not a claim of accurate ETF detection at this scale.
    `asset_class` is consumed downstream only for shortability-audit
    routing (see `.55`), not for signal computation, so a stock
    misclassified as... a stock (the actual failure mode here: a real ETF
    outside `KNOWN_ETF_SYMBOLS` gets labeled "STOCK") is a labeling
    inaccuracy, not a trading-logic error.

Design choices, mirroring `.67`'s own established pattern
------------------------------------------------------------------------
  - ONE whole-market asset-list API call covers the entire universe (not
    per-symbol) -- same "whole-market call, cache the result" shape as
    `.67`'s earnings calendar.
  - Cache max-age default is 24 hours: the tradable-asset universe does
    not meaningfully change within a trading day, and Martin's own
    operational routine already restarts `.365` close to daily (same
    rationale `.67` documents for its own cache lifetime).
  - Fail-closed on a stale-or-missing cache whose refetch fails: never
    raises for an ordinary fetch failure -- returns a
    `FullUniverseScanState` with `status="UNAVAILABLE"`. Nothing in the
    existing `.363`/`.365` wiring treats an empty/unavailable symbol
    source as anything other than "no symbols usable this cycle" (the
    same path a `.363` requests-config with zero usable symbols already
    takes), so this module does not need its own bespoke blocking logic.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

VERSION = "AURA v0.5.3.69"
ENGINE = "FULL_UNIVERSE_SCAN"

DEFAULT_CACHE_MAX_AGE_HOURS = 24.0
DEFAULT_STATE_FILENAME = "full_universe_scan_cache.json"

ROOT = Path(__file__).resolve().parent


class FullUniverseScanError(Exception):
    """Programmer-error / invalid-input only -- never raised for an
    ordinary handled outcome like a failed fetch; see module docstring,
    'Fail-closed on a stale-or-missing cache'."""


# ============================================================================
# stable_json / _atomic_write_json / now_iso -- copied, not imported,
# matching `.67`'s own copied-not-imported convention (see `.67`'s
# identical copy, itself copied from `.46`/`.43`/`.29`).
# ============================================================================


def now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def _atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False, allow_nan=False, sort_keys=True)
        f.write("\n")
    os.replace(tmp_path, path)


def _load_module(module_name: str, filename: str):
    try:
        return __import__(module_name)
    except ImportError:
        import importlib.util

        spec = importlib.util.spec_from_file_location(module_name, ROOT / filename)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod


def load_equity_cli_module():
    """`.56` -- source of `KNOWN_ETF_SYMBOLS` (see module docstring,
    'ETF vs STOCK classification'). Not modified anywhere in this file."""
    return _load_module("aura_v05356_stage3_live_equity_cli", "aura_v05356_stage3_live_equity_cli.py")


# ============================================================================
# Types
# ============================================================================


@dataclass(frozen=True, slots=True)
class UniverseAsset:
    """One row from Alpaca's asset list. `tradable` is the ONLY field this
    module filters on (see module docstring) -- the others are carried
    through purely for audit/disclosure."""

    symbol: str
    exchange: str | None
    tradable: bool

    def to_dict(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "exchange": self.exchange, "tradable": self.tradable}


@dataclass(frozen=True, slots=True)
class FullUniverseScanState:
    """The one object callers consume. `status="OK"` is the only status
    under which `symbols` may be trusted as a real fetch/cache result;
    `status="UNAVAILABLE"` means an empty tuple, and callers must treat
    that as "no symbols usable this cycle" (see module docstring),
    exactly as `.55` already does for a requests-config with zero usable
    symbols."""

    status: str  # "OK" | "UNAVAILABLE"
    source: str  # "FETCHED_FRESH" | "CACHE_REUSED" | "FETCH_FAILED_NO_USABLE_CACHE"
    symbols: tuple[str, ...] = ()
    filter_applied: str = "tradable=True (Martin, 2026-09-30, AskUserQuestion -- no price/volume/exchange filter)"
    generated_at: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "source": self.source,
            "symbol_count": len(self.symbols),
            "symbols": list(self.symbols),
            "filter_applied": self.filter_applied,
            "generated_at": self.generated_at,
            "error": self.error,
        }


# ============================================================================
# Fetch -- real Alpaca API call isolated to this one function,
# `assets_fetch_fn` dependency-injected so every other function/test never
# touches the network, matching this repo's universal fake-client testing
# convention (see `.67`'s identical `http_get` injection).
# ============================================================================


def fetch_tradable_us_equity_assets(
    alpaca_client: Any,
    *,
    assets_fetch_fn: Callable[[Any], Sequence[Any]] | None = None,
) -> tuple[UniverseAsset, ...]:
    """One real (or injected-fake) `TradingClient.get_all_assets()` call,
    filtered server-side to `asset_class=US_EQUITY, status=ACTIVE` and
    client-side to `tradable=True` (see module docstring, 'Filter
    scope'). Raises on any failure -- never returns a partial or
    empty-on-error result silently; the caller
    (`refresh_full_universe_if_stale`) is what turns that into a
    fail-closed `FullUniverseScanState`, not this function pretending
    nothing went wrong."""
    if assets_fetch_fn is None:
        from alpaca.trading.enums import AssetClass, AssetStatus
        from alpaca.trading.requests import GetAssetsRequest

        def assets_fetch_fn(client: Any) -> Sequence[Any]:
            request = GetAssetsRequest(asset_class=AssetClass.US_EQUITY, status=AssetStatus.ACTIVE)
            return client.get_all_assets(request)

    raw_assets = assets_fetch_fn(alpaca_client)
    assets: list[UniverseAsset] = []
    for a in raw_assets:
        symbol = getattr(a, "symbol", None)
        tradable = bool(getattr(a, "tradable", False))
        if not symbol or not tradable:
            continue
        exchange = getattr(a, "exchange", None)
        exchange_value = getattr(exchange, "value", exchange)
        assets.append(UniverseAsset(symbol=str(symbol).upper(), exchange=exchange_value, tradable=True))
    return tuple(assets)


# ============================================================================
# Cache -- atomic-write JSON, same shape/convention as `.67`.
# ============================================================================


def _cache_path(state_dir: Path) -> Path:
    return state_dir / DEFAULT_STATE_FILENAME


def save_universe_cache(state_dir: Path, *, assets: Sequence[UniverseAsset], generated_at: str) -> None:
    _atomic_write_json(
        _cache_path(state_dir),
        {
            "schema_version": "1.0",
            "engine": ENGINE,
            "agent_version": VERSION,
            "generated_at": generated_at,
            "assets": [a.to_dict() for a in assets],
        },
    )


def load_universe_cache(state_dir: Path) -> dict[str, Any] | None:
    """Never raises. A missing or corrupt cache file is treated identically
    -- both mean "no usable cache", fail-closed handled by the caller."""
    path = _cache_path(state_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


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
# Classification -- ETF vs STOCK (see module docstring, 'ETF vs STOCK
# classification' for the explicit, disclosed approximation this makes).
# ============================================================================


def classify_asset_class(symbol: str, *, known_etf_symbols: frozenset[str]) -> str:
    return "ETF" if symbol in known_etf_symbols else "STOCK"


# ============================================================================
# The one orchestration function callers actually use.
# ============================================================================


def refresh_full_universe_if_stale(
    *,
    alpaca_client: Any,
    state_dir: Path,
    now: datetime,
    max_age_hours: float = DEFAULT_CACHE_MAX_AGE_HOURS,
    assets_fetch_fn: Callable[[Any], Sequence[Any]] | None = None,
) -> FullUniverseScanState:
    """Reuses a fresh cache with zero network calls; fetches and re-caches
    when the cache is stale or missing. NEVER raises for a fetch failure
    (fail-closed is expressed as `status="UNAVAILABLE"` data, per module
    docstring)."""
    cache = load_universe_cache(state_dir)
    if cache is not None and is_cache_fresh(cache.get("generated_at", ""), now=now, max_age_hours=max_age_hours):
        symbols = tuple(row["symbol"] for row in cache.get("assets", []) if row.get("tradable"))
        return FullUniverseScanState(
            status="OK", source="CACHE_REUSED", symbols=symbols, generated_at=cache.get("generated_at"),
        )

    try:
        assets = fetch_tradable_us_equity_assets(alpaca_client, assets_fetch_fn=assets_fetch_fn)
    except Exception as exc:  # noqa: BLE001 -- any fetch failure is fail-closed data, never propagated
        return FullUniverseScanState(
            status="UNAVAILABLE", source="FETCH_FAILED_NO_USABLE_CACHE", symbols=(),
            error=f"{type(exc).__name__}: {exc}",
        )

    generated_at = now_iso(now)
    save_universe_cache(state_dir, assets=assets, generated_at=generated_at)
    return FullUniverseScanState(
        status="OK", source="FETCHED_FRESH", symbols=tuple(a.symbol for a in assets), generated_at=generated_at,
    )


def build_symbol_requests_from_full_universe(state: FullUniverseScanState, *, equity_cli_module: Any) -> tuple[Any, ...]:
    """Turns a `FullUniverseScanState` into `.56.LiveSymbolRequest` tuples
    -- the same shape `.51`'s pinned-universe scan produces (see `.56`'s
    `build_symbol_requests_from_pinned_universe`), so every downstream
    caller (`.363`, `.365`) handles this symbol source identically to the
    existing pinned-universe one. `quantity` is always `None` (auto-sized
    via ATR risk sizing that cycle), matching the pinned-universe
    precedent exactly."""
    known_etf_symbols = equity_cli_module.KNOWN_ETF_SYMBOLS
    return tuple(
        equity_cli_module.LiveSymbolRequest(
            symbol=symbol,
            asset_class=classify_asset_class(symbol, known_etf_symbols=known_etf_symbols),
            quantity=None,
        )
        for symbol in state.symbols
    )
