#!/usr/bin/env python3
"""
AURA v0.5.3.43 — Portfolio Exposure Observability (cross-venue).

OBSERVABILITY ONLY. NEVER PLACES, CANCELS, OR MODIFIES ANY ORDER. NEVER
BLOCKS A TRADE. Enforcement (blocking a submission on an exposure breach)
is explicitly `.44`'s job, not this module's — this module only computes
and persists what the portfolio's exposure actually looks like right now,
so `.44` has real, trustworthy numbers to enforce against later.

Why this exists (v0.5.5 Final Implementation Baseline, milestone `.43`)
------------------------------------------------------------------------
An audit (2026-09-12) found essentially none of the roadmap's 12-dimension
exposure checklist was computed anywhere live: no common cross-venue
"what's open right now" schema exists (MEXC only had a single-symbol
position query and an order-lifecycle ledger that drops filled positions
once they go terminal; Alpaca's own sizing module (`.25`) queries
positions but discards qty/price/side); the "`.15` 12-state Position State
machine" referenced in other project docs does not exist anywhere in this
repo (`.15` is FLAT/ENTRY_CANDIDATE/BLOCKED only, and says so explicitly in
its own contract) — that finding is preserved as-is, not "fixed" by
inventing a 12-state machine here, per Martin's explicit instruction.

Reuse scan (Martin's explicit A/B/C/D framework, performed BEFORE writing
any new code, per his instruction not to reinvent what already exists)
------------------------------------------------------------------------
Surveyed: this repo's own CAURA lineage, the BABIL hackathon submission
(competitor, reference-only), DELTAX (three repos: pautax007_DELTAX,
mpoubot_DELTAX, mpoubot_DELTAX_v2), and this project's own sibling
`mexc_bot/` research track.
  - CAURA `max_drawdown_from_returns` / `drawdown_from_running_max`: (C)
    architecturally useful (correct drawdown math) but incompatible --
    single-series backtest helpers, not live multi-venue position state.
  - BABIL `risk_evaluator.py` (G3 exposure/sizing gate), `babil_authorization.py`,
    `babil_pre_execution.py`: (D) not applicable -- confirmed by direct
    read to be per-order execution-safety/sizing only, no portfolio
    aggregation concept anywhere in any of the three files.
  - DELTAX `gates.py::gate_portfolio_risk` (single aggregate committed-
    max-loss vs equity threshold): (C) architecturally useful pattern
    (aggregate risk vs equity) but incompatible -- single scalar, single
    venue, no multi-dimension model.
  - DELTAX `reconcile.py` (reads broker truth + pending orders, fails
    closed on any unparseable holding, pairs option legs into spreads):
    (C) the FAIL-CLOSED-ON-UNPARSEABLE discipline is adopted here
    (see `VenueFetchStatus`) -- the option-leg-pairing logic itself does
    not apply to AURA's stock/crypto-perpetual positions.
  - DELTAX v2 `portfolio_risk_monitor.py` (780 lines, Postgres-backed,
    computes exactly 2 of the 12 dimensions -- daily PnL two-tier
    threshold, crude per-asset-class market-value split): (C) the
    two-tier daily-loss-threshold SHAPE is a reasonable idea, but the
    implementation (Postgres, single-venue, in-memory) does not fit
    AURA's atomic-file/hash-chain convention -- reimplemented here on
    that convention instead of adopted directly.
  - `mexc_bot/core/risk_manager.py::CircuitBreaker` (verified by direct
    read; no test suite exists for it): (B) the daily-loss-pct-of-
    day-start-equity and drawdown-pct-from-peak FORMULAS are correct,
    standard, and adapted here almost verbatim -- but the STORAGE (a
    mutable in-process instance that does not survive a restart) is not
    reusable; this module persists the same math atomically instead.
    `calc_position_plan`'s `MAX_LEVERAGE` clamp idea directly informs
    `check_mexc_leverage_cap` below, parameterized rather than hardcoded
    (see that function's docstring for why no cap value is invented here).
  - No sibling project anywhere has a venue-agnostic position snapshot,
    correlation computation, liquidity-depth data, or a sector map. Those
    remain genuinely unbuilt (see the NOT_COMPUTABLE dimensions below) --
    confirmed absent, not merely unwired, by direct code read.

Canonical position snapshot (the missing piece every dimension below is
computed from)
------------------------------------------------------------------------
`PositionRecord` / `VenueFetchStatus` / `PortfolioSnapshot` are the common,
venue-neutral schema this repo has never had. Built from VERIFIED field
mappings, not guessed:
  - MEXC: `exchange.fetch_positions()` (widened from `.28`'s single-symbol
    `resolve_position()` -- no symbol filter) is parsed against ccxt
    4.5.78's actual installed `MexcClass.parse_position()` source (read
    directly from the installed package, not assumed): raw `positionType`
    ('1'=long, else short), `holdVol` (quantity), `openAvgPrice` (entry),
    `leverage`, `liquidatePrice`. ccxt's own unified `notional`/
    `markPrice`/`unrealizedPnl` fields are confirmed, by reading the same
    source, to be hardcoded `None` for MEXC -- this module does NOT read
    them. Mark-to-market notional instead requires a supplementary
    `fetch_ticker(symbol)` call plus the market's own `contractSize`
    (confirmed real and populated for MEXC swaps by reading
    `fetch_swap_markets()`'s source -- e.g. BTC_USDT's contractSize is
    0.0001 BTC/contract, NOT 1). If either the ticker call fails or
    `contractSize` is unavailable, notional is left `None`
    (`notional_basis=None`) rather than guessed at contractSize=1.
  - Alpaca: `alpaca.trading.models.Position` (verified by reading the
    installed package) already carries `market_value`, `current_price`,
    and `unrealized_pl` directly -- no supplementary call needed.
  - IMPORTANT DISCLOSED LIMITATION: this session's sandboxed network
    egress policy blocks both mexc.com and alpaca.markets (confirmed via
    the outbound proxy's own status endpoint -- a 403 policy denial, not
    a transient failure), matching the same limitation disclosed in
    `.42`'s completion report. Every fetcher below is therefore verified
    against the actual installed ccxt/alpaca-py library source and
    exercised in tests against injected fakes (matching this repo's own
    established `.28` test convention), but could not be exercised
    against a real, live MEXC or Alpaca account in this session. Field
    names/shapes are library-verified, not merely assumed, but a live
    smoke test from an environment with broker network access is still
    the right next check before `.44` (enforcement) depends on this data.

The 12 exposure dimensions, and why each is or is not computable today
------------------------------------------------------------------------
Per Martin's explicit instruction: "If a dimension genuinely cannot be
computed from available authoritative data, explicitly mark it
NOT_COMPUTABLE rather than producing a misleading value." Three statuses:
  COMPUTABLE       -- a real number is produced from live/fetched data.
  NOT_YET_AVAILABLE -- the data SOURCE exists (this module's own persisted
                       equity history) but has not accumulated enough
                       history yet (e.g. no prior same-day snapshot).
                       Resolves itself as this module keeps running.
  NOT_COMPUTABLE   -- no data source exists anywhere in AURA today. A
                      structural gap, not a timing gap.

 1. per_trade_risk            NOT_COMPUTABLE -- AURA does not persist a
                               stop-loss/planned-risk distance for any
                               already-open position anywhere; `.25` only
                               computes a target risk fraction at NEW-
                               entry sizing time.
 2. daily_loss                COMPUTABLE / NOT_YET_AVAILABLE -- needs a
                               same-day prior equity snapshot from this
                               module's own persisted equity history.
 3. max_drawdown               COMPUTABLE -- from this module's own
                               persisted equity history (peak-so-far is
                               well-defined even from a single point).
 4. portfolio_heat             COMPUTABLE (per venue with computable
                               notional; partial coverage is flagged, not
                               silently dropped) -- sum(|notional|)/equity.
 5. asset_concentration        COMPUTABLE (same notional-coverage caveat).
 6. correlation                NOT_COMPUTABLE -- no continuously-updated
                               cross-symbol return history exists live.
 7. directional_exposure       COMPUTABLE -- net(LONG notional - SHORT
                               notional)/equity.
 8. leverage_exposure          COMPUTABLE for MEXC (leverage read directly
                               per position); Alpaca positions are reported
                               unlevered (this account structure models no
                               margin), not silently computed as if levered.
 9. liquidity_concentration    NOT_COMPUTABLE -- no order-book depth/
                               volume data available outside backtest CSVs.
10. crypto_funding_basis_exposure  PARTIAL/COMPUTABLE -- current funding
                               rate * notional per open MEXC position IS
                               computed (via `fetch_funding_rate`); the
                               "basis" half (perp vs spot price) is
                               explicitly NOT computed -- no live spot
                               reference feed exists -- and is reported
                               as its own NOT_COMPUTABLE sub-field so the
                               computed funding piece is never conflated
                               with an uncomputed basis number.
11. equity_sector_concentration NOT_COMPUTABLE -- no sector mapping data
                               anywhere in this repo.
12. projected_post_trade_risk  COMPUTABLE -- `project_post_trade_exposure()`
                               is a pure function overlaying one
                               hypothetical position onto the current
                               snapshot and recomputing dimensions
                               4/5/7/8; deterministic, no live call.

A 13th, separately-tracked item Martin's roadmap note calls out by name:
`check_mexc_leverage_cap(positions, cap)` reports each position's actual
leverage (COMPUTABLE) but takes the CAP THRESHOLD as an explicit,
required parameter with no default -- no cap value exists anywhere in
`.34` or any config file today (confirmed absent by the pre-implementation
audit), and this module does not invent one. Called with `cap=None`, it
reports current leverage only and a `cap_configured=False` flag, never a
fabricated PASS/FAIL.

Output
------
This module has no CLI/scheduled entry point of its own in this milestone
-- it is a library, called by whatever orchestrates a snapshot run (a
future scheduled job, or ad hoc). `persist_snapshot()` writes:
  state/portfolio_exposure/snapshots/<as_of>.json  -- one immutable,
      atomically-written snapshot + exposure report per run.
  state/portfolio_exposure/equity_history.json -- append-only-by-content
      (atomic read-modify-write) equity-by-venue history, hash-stamped,
      the same file `daily_loss`/`max_drawdown` read from on every run.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DEFAULT_STATE_DIR = ROOT / "state" / "portfolio_exposure"

VENUES = ("MEXC", "ALPACA")


# ------------------------------------------------------------------------
# stable_json / sha256_text -- copied, not imported, matching this repo's
# own established convention (every aura_v053NN module is independently
# file-loadable and defines these itself; see e.g. aura_v05312's own copy).
# ------------------------------------------------------------------------

def stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, obj: Any) -> None:
    """Same tmp-then-os.replace() pattern as .29's _write_record_atomic."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False, allow_nan=False, sort_keys=True)
        f.write("\n")
    os.replace(tmp_path, path)


# ------------------------------------------------------------------------
# Canonical, venue-neutral schema.
# ------------------------------------------------------------------------

@dataclass(frozen=True)
class PositionRecord:
    venue: str                      # "MEXC" | "ALPACA"
    symbol: str                     # venue-native symbol, e.g. "BTC_USDT" or "AAPL"
    direction: str                  # "LONG" | "SHORT"
    quantity: float
    entry_price: float
    leverage: float | None          # MEXC: real, read per position. Alpaca: None (unlevered account model).
    mark_price: float | None        # None if no live price could be obtained for this position.
    notional_usd: float | None      # None if it cannot be honestly computed (see notional_basis).
    notional_basis: str | None      # "MARK_TO_MARKET" | "ENTRY_PRICE_ESTIMATE" | None
    unrealized_pnl_usd: float | None
    liquidation_price: float | None  # MEXC only; None for Alpaca.
    raw_source_id: str | None       # positionId (MEXC) / asset_id (Alpaca), for traceability only.
    as_of: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class VenueFetchStatus:
    venue: str
    status: str                     # "SUCCESS" | "FAILED" | "NOT_CONFIGURED"
    error: str | None
    fetched_at: str
    positions_count: int
    equity: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PortfolioSnapshot:
    as_of: str
    positions: tuple[PositionRecord, ...]
    venue_fetch_status: dict[str, VenueFetchStatus]
    is_complete: bool                # True only if every CONFIGURED venue's status == SUCCESS
    state_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of,
            "positions": [p.to_dict() for p in self.positions],
            "venue_fetch_status": {k: v.to_dict() for k, v in self.venue_fetch_status.items()},
            "is_complete": self.is_complete,
            "state_hash": self.state_hash,
        }


def _build_snapshot(as_of: str, positions: list[PositionRecord], venue_fetch_status: dict[str, VenueFetchStatus]) -> PortfolioSnapshot:
    configured = [v for v in venue_fetch_status.values() if v.status != "NOT_CONFIGURED"]
    is_complete = bool(configured) and all(v.status == "SUCCESS" for v in configured)
    body = {
        "as_of": as_of,
        "positions": [p.to_dict() for p in positions],
        "venue_fetch_status": {k: v.to_dict() for k, v in venue_fetch_status.items()},
        "is_complete": is_complete,
    }
    state_hash = sha256_text(stable_json(body))
    return PortfolioSnapshot(
        as_of=as_of, positions=tuple(positions), venue_fetch_status=venue_fetch_status,
        is_complete=is_complete, state_hash=state_hash,
    )


# ------------------------------------------------------------------------
# MEXC fetcher.
# ------------------------------------------------------------------------

def fetch_mexc_portfolio(exchange: Any) -> tuple[list[PositionRecord], VenueFetchStatus]:
    """exchange: a ccxt.mexc instance (e.g. from .28's build_exchange()),
    already load_markets()'d. Widened from .28's resolve_position() (which
    is deliberately single-symbol, for order reconciliation) to enumerate
    ALL open positions. Fails closed to VenueFetchStatus(status="FAILED")
    on any exception -- never returns a partial position list silently."""
    fetched_at = now_iso()
    try:
        raw_positions = exchange.fetch_positions()
    except Exception as exc:  # noqa: BLE001 - broker call, report and fail closed
        return [], VenueFetchStatus(
            venue="MEXC", status="FAILED", error=f"{type(exc).__name__}: {exc}",
            fetched_at=fetched_at, positions_count=0, equity=None,
        )

    equity: float | None = None
    try:
        balance = exchange.fetch_balance()
        usdt = balance.get("USDT") if isinstance(balance, dict) else None
        if isinstance(usdt, dict) and usdt.get("total") is not None:
            equity = float(usdt["total"])
    except Exception:  # noqa: BLE001 - equity is best-effort; position data above still stands
        equity = None

    records: list[PositionRecord] = []
    for pos in raw_positions:
        info = pos.get("info") if isinstance(pos, dict) else None
        if not isinstance(info, dict):
            continue
        hold_vol = _to_float(info.get("holdVol"))
        if hold_vol is None or hold_vol <= 0:
            continue  # absent/zero holdVol == not actually open, matches .28's own convention

        symbol = info.get("symbol")
        raw_side = info.get("positionType")
        direction = "LONG" if str(raw_side) == "1" else "SHORT"  # verified against installed ccxt 4.5.78 parse_position()
        entry_price = _to_float(info.get("openAvgPrice"))
        leverage = _to_float(info.get("leverage"))
        liquidation_price = _to_float(info.get("liquidatePrice"))
        position_id = info.get("positionId")

        mark_price, notional_usd, notional_basis = _mexc_mark_and_notional(exchange, symbol, hold_vol, entry_price)

        unrealized_pnl_usd = None
        if mark_price is not None and entry_price is not None:
            sign = 1.0 if direction == "LONG" else -1.0
            contract_size = _mexc_contract_size(exchange, symbol)
            if contract_size is not None:
                unrealized_pnl_usd = sign * (mark_price - entry_price) * hold_vol * contract_size

        records.append(PositionRecord(
            venue="MEXC", symbol=str(symbol), direction=direction, quantity=hold_vol,
            entry_price=entry_price if entry_price is not None else 0.0,
            leverage=leverage, mark_price=mark_price, notional_usd=notional_usd,
            notional_basis=notional_basis, unrealized_pnl_usd=unrealized_pnl_usd,
            liquidation_price=liquidation_price,
            raw_source_id=str(position_id) if position_id is not None else None,
            as_of=fetched_at,
        ))

    return records, VenueFetchStatus(
        venue="MEXC", status="SUCCESS", error=None, fetched_at=fetched_at,
        positions_count=len(records), equity=equity,
    )


def _mexc_contract_size(exchange: Any, symbol: str) -> float | None:
    try:
        market = exchange.market(symbol)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(market, dict):
        return None
    size = market.get("contractSize")
    return _to_float(size)


def _mexc_mark_and_notional(exchange: Any, symbol: str, hold_vol: float, entry_price: float | None) -> tuple[float | None, float | None, str | None]:
    """Returns (mark_price, notional_usd, notional_basis). Tries a live
    ticker for mark-to-market; falls back to an entry-price ESTIMATE,
    explicitly labeled as such; leaves notional None (never guessed) if
    contractSize cannot be confirmed."""
    contract_size = _mexc_contract_size(exchange, symbol)

    mark_price: float | None = None
    try:
        ticker = exchange.fetch_ticker(symbol)
        if isinstance(ticker, dict):
            mark_price = _to_float(ticker.get("last")) or _to_float(ticker.get("close"))
    except Exception:  # noqa: BLE001 - ticker fetch is best-effort
        mark_price = None

    if contract_size is None:
        return mark_price, None, None

    if mark_price is not None:
        return mark_price, mark_price * hold_vol * contract_size, "MARK_TO_MARKET"
    if entry_price is not None:
        return None, entry_price * hold_vol * contract_size, "ENTRY_PRICE_ESTIMATE"
    return None, None, None


def fetch_mexc_funding_exposure(exchange: Any, positions: list[PositionRecord]) -> dict[str, dict[str, Any]]:
    """Per-symbol current funding rate * notional -- the COMPUTABLE half
    of dimension 10 (crypto funding/basis exposure). Basis itself
    (perp vs spot) is intentionally not attempted here -- see module
    docstring dimension 10. One entry per MEXC position; a failed fetch
    for one symbol never blocks the others."""
    out: dict[str, dict[str, Any]] = {}
    for p in positions:
        if p.venue != "MEXC":
            continue
        try:
            fr = exchange.fetch_funding_rate(p.symbol)
            rate = _to_float(fr.get("fundingRate")) if isinstance(fr, dict) else None
        except Exception as exc:  # noqa: BLE001
            out[p.symbol] = {"status": "NOT_COMPUTABLE", "reason": f"{type(exc).__name__}: {exc}"}
            continue
        if rate is None or p.notional_usd is None:
            out[p.symbol] = {"status": "NOT_COMPUTABLE", "reason": "MISSING_RATE_OR_NOTIONAL"}
            continue
        sign = 1.0 if p.direction == "LONG" else -1.0
        out[p.symbol] = {
            "status": "COMPUTABLE",
            "funding_rate": rate,
            "projected_funding_cost_usd_next_settlement": sign * rate * p.notional_usd,
            "basis_status": "NOT_COMPUTABLE",
            "basis_reason": "no live spot reference price feed exists in AURA",
        }
    return out


# ------------------------------------------------------------------------
# Alpaca fetcher.
# ------------------------------------------------------------------------

def fetch_alpaca_portfolio(client: Any) -> tuple[list[PositionRecord], VenueFetchStatus]:
    """client: an alpaca-py TradingClient (paper=True, matching every
    other Alpaca call site in this repo). Widened from .25's
    fetch_account_state(), which deliberately discards qty/price/side
    since .25 only needs a symbol set and a position count."""
    fetched_at = now_iso()
    equity: float | None = None
    try:
        account = client.get_account()
        equity = float(account.equity)
    except Exception as exc:  # noqa: BLE001
        return [], VenueFetchStatus(
            venue="ALPACA", status="FAILED", error=f"{type(exc).__name__}: {exc}",
            fetched_at=fetched_at, positions_count=0, equity=None,
        )

    try:
        positions = client.get_all_positions()
    except Exception as exc:  # noqa: BLE001
        return [], VenueFetchStatus(
            venue="ALPACA", status="FAILED", error=f"{type(exc).__name__}: {exc}",
            fetched_at=fetched_at, positions_count=0, equity=equity,
        )

    records: list[PositionRecord] = []
    for p in positions:
        qty = _to_float(getattr(p, "qty", None))
        if qty is None or qty == 0:
            continue
        side = str(getattr(p, "side", "")).lower()
        direction = "LONG" if "long" in side else "SHORT"
        entry_price = _to_float(getattr(p, "avg_entry_price", None))
        mark_price = _to_float(getattr(p, "current_price", None))
        market_value = _to_float(getattr(p, "market_value", None))
        unrealized_pl = _to_float(getattr(p, "unrealized_pl", None))
        notional_usd = abs(market_value) if market_value is not None else None
        notional_basis = "MARK_TO_MARKET" if market_value is not None else None

        records.append(PositionRecord(
            venue="ALPACA", symbol=str(getattr(p, "symbol", "")), direction=direction,
            quantity=abs(qty), entry_price=entry_price if entry_price is not None else 0.0,
            leverage=None,  # this account model carries no margin/leverage data -- never fabricated as 1.0
            mark_price=mark_price, notional_usd=notional_usd, notional_basis=notional_basis,
            unrealized_pnl_usd=unrealized_pl, liquidation_price=None,
            raw_source_id=str(getattr(p, "asset_id", "")) or None, as_of=fetched_at,
        ))

    return records, VenueFetchStatus(
        venue="ALPACA", status="SUCCESS", error=None, fetched_at=fetched_at,
        positions_count=len(records), equity=equity,
    )


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------------
# Orchestration: build one cross-venue snapshot. Each venue's failure is
# independent -- MEXC failing never blocks Alpaca data and vice versa,
# and the result says exactly which venues succeeded.
# ------------------------------------------------------------------------

def build_portfolio_snapshot(*, mexc_exchange: Any = None, alpaca_client: Any = None) -> PortfolioSnapshot:
    as_of = now_iso()
    positions: list[PositionRecord] = []
    venue_status: dict[str, VenueFetchStatus] = {}

    if mexc_exchange is not None:
        mexc_positions, mexc_status = fetch_mexc_portfolio(mexc_exchange)
        positions.extend(mexc_positions)
        venue_status["MEXC"] = mexc_status
    else:
        venue_status["MEXC"] = VenueFetchStatus(
            venue="MEXC", status="NOT_CONFIGURED", error=None, fetched_at=as_of, positions_count=0, equity=None
        )

    if alpaca_client is not None:
        alpaca_positions, alpaca_status = fetch_alpaca_portfolio(alpaca_client)
        positions.extend(alpaca_positions)
        venue_status["ALPACA"] = alpaca_status
    else:
        venue_status["ALPACA"] = VenueFetchStatus(
            venue="ALPACA", status="NOT_CONFIGURED", error=None, fetched_at=as_of, positions_count=0, equity=None
        )

    return _build_snapshot(as_of, positions, venue_status)


# ------------------------------------------------------------------------
# Equity history -- persisted, atomic read-modify-write. This is the data
# source daily_loss/max_drawdown read from; without it those two
# dimensions cannot be computed (NOT_YET_AVAILABLE on a fresh state dir).
# ------------------------------------------------------------------------

def _equity_history_path(state_dir: Path) -> Path:
    return state_dir / "equity_history.json"


def load_equity_history(state_dir: Path) -> list[dict[str, Any]]:
    path = _equity_history_path(state_dir)
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    events = data.get("events", []) if isinstance(data, dict) else []
    return events if isinstance(events, list) else []


def append_equity_history(state_dir: Path, snapshot: PortfolioSnapshot) -> list[dict[str, Any]]:
    """Appends one entry per venue whose equity was actually observed
    this run (None-equity venues are not recorded as a fabricated zero).
    Whole-file atomic rewrite -- matches this repo's established
    read-modify-write-then-os.replace() convention, not a raw append,
    so a crash mid-write can never corrupt the file."""
    events = load_equity_history(state_dir)
    for venue, status in snapshot.venue_fetch_status.items():
        if status.status == "SUCCESS" and status.equity is not None:
            events.append({"venue": venue, "equity": status.equity, "as_of": snapshot.as_of})
    events.sort(key=lambda e: e["as_of"])
    body = {"events": events}
    body["state_hash"] = sha256_text(stable_json({"events": events}))
    _atomic_write_json(_equity_history_path(state_dir), body)
    return events


def persist_snapshot(state_dir: Path, snapshot: PortfolioSnapshot, exposure_report: dict[str, Any] | None = None) -> Path:
    out = snapshot.to_dict()
    if exposure_report is not None:
        out["exposure_report"] = exposure_report
    safe_ts = snapshot.as_of.replace(":", "").replace("+00:00", "Z")
    path = state_dir / "snapshots" / f"{safe_ts}.json"
    _atomic_write_json(path, out)
    return path


# ------------------------------------------------------------------------
# 12-dimension exposure computation.
# ------------------------------------------------------------------------

def _venue_day_key(iso_ts: str) -> str:
    return iso_ts[:10]  # UTC calendar date -- consistent with every timestamp in this module being UTC


def compute_daily_loss(equity_history: list[dict[str, Any]], venue: str, current_equity: float | None, as_of: str) -> dict[str, Any]:
    if current_equity is None:
        return {"status": "NOT_COMPUTABLE", "reason": "NO_CURRENT_EQUITY"}
    today = _venue_day_key(as_of)
    prior_today = [e for e in equity_history if e["venue"] == venue and _venue_day_key(e["as_of"]) == today and e["as_of"] < as_of]
    if not prior_today:
        return {"status": "NOT_YET_AVAILABLE", "reason": "NO_PRIOR_SNAPSHOT_TODAY"}
    day_start_equity = prior_today[0]["equity"]  # first observation of the day, sorted ascending
    if day_start_equity <= 0:
        return {"status": "NOT_COMPUTABLE", "reason": "INVALID_DAY_START_EQUITY"}
    daily_loss_pct = (day_start_equity - current_equity) / day_start_equity
    return {"status": "COMPUTABLE", "day_start_equity": day_start_equity, "current_equity": current_equity, "daily_loss_pct": daily_loss_pct}


def compute_max_drawdown(equity_history: list[dict[str, Any]], venue: str, current_equity: float | None) -> dict[str, Any]:
    if current_equity is None:
        return {"status": "NOT_COMPUTABLE", "reason": "NO_CURRENT_EQUITY"}
    venue_equities = [e["equity"] for e in equity_history if e["venue"] == venue]
    peak = max(venue_equities + [current_equity]) if venue_equities else current_equity
    if peak <= 0:
        return {"status": "NOT_COMPUTABLE", "reason": "INVALID_PEAK_EQUITY"}
    drawdown_pct = (peak - current_equity) / peak
    return {"status": "COMPUTABLE", "peak_equity": peak, "current_equity": current_equity, "drawdown_pct": drawdown_pct}


def compute_portfolio_heat(positions: list[PositionRecord], equity: float | None) -> dict[str, Any]:
    if equity is None or equity <= 0:
        return {"status": "NOT_COMPUTABLE", "reason": "NO_VALID_EQUITY"}
    priced = [p for p in positions if p.notional_usd is not None]
    excluded = len(positions) - len(priced)
    total_notional = sum(abs(p.notional_usd) for p in priced)
    result = {"status": "COMPUTABLE", "total_abs_notional_usd": total_notional, "equity": equity, "heat_ratio": total_notional / equity}
    if excluded:
        result["partial"] = True
        result["positions_excluded_no_notional"] = excluded
    return result


def compute_asset_concentration(positions: list[PositionRecord]) -> dict[str, Any]:
    priced = [p for p in positions if p.notional_usd is not None]
    excluded = len(positions) - len(priced)
    total = sum(abs(p.notional_usd) for p in priced)
    if total <= 0:
        return {"status": "NOT_COMPUTABLE" if not priced else "COMPUTABLE", "by_symbol": {}, "positions_excluded_no_notional": excluded}
    by_symbol: dict[str, float] = {}
    for p in priced:
        key = f"{p.venue}:{p.symbol}"
        by_symbol[key] = by_symbol.get(key, 0.0) + abs(p.notional_usd) / total
    result = {"status": "COMPUTABLE", "by_symbol": by_symbol}
    if excluded:
        result["partial"] = True
        result["positions_excluded_no_notional"] = excluded
    return result


def compute_directional_exposure(positions: list[PositionRecord], equity: float | None) -> dict[str, Any]:
    if equity is None or equity <= 0:
        return {"status": "NOT_COMPUTABLE", "reason": "NO_VALID_EQUITY"}
    priced = [p for p in positions if p.notional_usd is not None]
    net = sum((1.0 if p.direction == "LONG" else -1.0) * abs(p.notional_usd) for p in priced)
    result = {"status": "COMPUTABLE", "net_notional_usd": net, "net_exposure_ratio": net / equity}
    excluded = len(positions) - len(priced)
    if excluded:
        result["partial"] = True
        result["positions_excluded_no_notional"] = excluded
    return result


def compute_leverage_exposure(positions: list[PositionRecord], equity_by_venue: dict[str, float | None]) -> dict[str, Any]:
    per_position = [
        {"venue": p.venue, "symbol": p.symbol, "leverage": p.leverage}
        for p in positions
    ]
    per_venue_aggregate: dict[str, Any] = {}
    for venue in VENUES:
        equity = equity_by_venue.get(venue)
        venue_positions = [p for p in positions if p.venue == venue and p.notional_usd is not None]
        if equity is None or equity <= 0 or not venue_positions:
            per_venue_aggregate[venue] = {"status": "NOT_COMPUTABLE"}
            continue
        total_notional = sum(abs(p.notional_usd) for p in venue_positions)
        per_venue_aggregate[venue] = {"status": "COMPUTABLE", "aggregate_leverage_ratio": total_notional / equity}
    return {"status": "COMPUTABLE", "per_position": per_position, "per_venue_aggregate": per_venue_aggregate}


def check_mexc_leverage_cap(positions: list[PositionRecord], cap: float | None) -> dict[str, Any]:
    """Reports actual per-position MEXC leverage (a real, computable
    number) against an EXPLICIT cap parameter. Per the module docstring:
    no cap value exists anywhere in AURA's config today, and this
    function never invents one -- cap=None reports leverage only, with
    cap_configured=False, never a fabricated PASS/FAIL."""
    mexc_positions = [p for p in positions if p.venue == "MEXC"]
    entries = []
    breach = False
    for p in mexc_positions:
        entry: dict[str, Any] = {"symbol": p.symbol, "leverage": p.leverage}
        if cap is not None and p.leverage is not None:
            entry["exceeds_cap"] = p.leverage > cap
            breach = breach or entry["exceeds_cap"]
        entries.append(entry)
    return {
        "cap_configured": cap is not None,
        "cap": cap,
        "positions": entries,
        "any_position_exceeds_cap": breach if cap is not None else None,
    }


NOT_COMPUTABLE_STATIC = {
    "correlation": "no continuously-updated cross-symbol return history exists live in AURA",
    "liquidity_concentration": "no order-book depth/volume data available outside backtest CSVs",
    "equity_sector_concentration": "no sector mapping data exists anywhere in this repo",
    "per_trade_risk": "AURA does not persist a stop-loss/planned-risk distance for any already-open position",
}


def compute_exposure_dimensions(
    snapshot: PortfolioSnapshot, equity_history: list[dict[str, Any]], *, mexc_leverage_cap: float | None = None,
) -> dict[str, Any]:
    """The full 12-dimension report (plus the separately-tracked MEXC
    leverage-cap check) for one PortfolioSnapshot. Pure function of its
    inputs -- makes no network call itself."""
    equity_by_venue = {v: status.equity for v, status in snapshot.venue_fetch_status.items()}
    positions = list(snapshot.positions)

    report: dict[str, Any] = {"as_of": snapshot.as_of, "snapshot_state_hash": snapshot.state_hash, "snapshot_is_complete": snapshot.is_complete}

    for dim, reason in NOT_COMPUTABLE_STATIC.items():
        report[dim] = {"status": "NOT_COMPUTABLE", "reason": reason}

    report["daily_loss"] = {
        venue: compute_daily_loss(equity_history, venue, equity_by_venue.get(venue), snapshot.as_of)
        for venue in VENUES
    }
    report["max_drawdown"] = {
        venue: compute_max_drawdown(equity_history, venue, equity_by_venue.get(venue))
        for venue in VENUES
    }
    total_equity = sum(e for e in equity_by_venue.values() if e is not None) or None
    report["portfolio_heat"] = compute_portfolio_heat(positions, total_equity)
    report["asset_concentration"] = compute_asset_concentration(positions)
    report["directional_exposure"] = compute_directional_exposure(positions, total_equity)
    report["leverage_exposure"] = compute_leverage_exposure(positions, equity_by_venue)
    report["crypto_funding_basis_exposure"] = {"status": "SEE_fetch_mexc_funding_exposure", "reason": "requires a live per-symbol funding-rate call, not a pure function of the snapshot alone"}
    report["mexc_leverage_cap_check"] = check_mexc_leverage_cap(positions, mexc_leverage_cap)

    return report


def project_post_trade_exposure(
    snapshot: PortfolioSnapshot, equity_history: list[dict[str, Any]], hypothetical: PositionRecord, *, mexc_leverage_cap: float | None = None,
) -> dict[str, Any]:
    """Dimension 12: projected post-trade risk. Overlays one hypothetical
    position onto the current snapshot (never mutates it) and recomputes
    the notional-dependent dimensions. Deterministic, no network call."""
    projected_positions = list(snapshot.positions) + [hypothetical]
    projected_snapshot = _build_snapshot(snapshot.as_of, projected_positions, snapshot.venue_fetch_status)
    before = compute_exposure_dimensions(snapshot, equity_history, mexc_leverage_cap=mexc_leverage_cap)
    after = compute_exposure_dimensions(projected_snapshot, equity_history, mexc_leverage_cap=mexc_leverage_cap)
    return {
        "status": "COMPUTABLE",
        "before": {
            "portfolio_heat": before["portfolio_heat"], "directional_exposure": before["directional_exposure"],
            "leverage_exposure": before["leverage_exposure"], "mexc_leverage_cap_check": before["mexc_leverage_cap_check"],
        },
        "after": {
            "portfolio_heat": after["portfolio_heat"], "directional_exposure": after["directional_exposure"],
            "leverage_exposure": after["leverage_exposure"], "mexc_leverage_cap_check": after["mexc_leverage_cap_check"],
        },
    }
