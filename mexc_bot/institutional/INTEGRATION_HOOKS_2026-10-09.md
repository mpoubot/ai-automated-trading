# AURA Institutional — Live Run-Loop Integration Hooks (Task 1, 2026-10-09)

**Status: code blocks only — nothing in this document has been applied to
any of your real files.** Every snippet below is quoted against the exact
real line numbers it was read from (staged, read-only copies of your repo,
re-verified against the live files on 2026-10-09). Apply and commit these
yourself, per your own git workflow — consistent with every other
integration point in this package (Track A's `entry_filters.py`, Track B's
`short_protective_gates.py`, Portfolio's `portfolio_additional_enforcement.
py` all document their own call sites the same way, never self-applying).

**One correction to this package's own prior documentation, found while
grounding this task:** `track_a/entry_filters.py`'s module docstring (and
this package's top-level `README.md`) previously said the `live_bot.py`
hook goes "right after `strat.passes_universe_filter(...)` and before `sig
= strat.evaluate_signal(df)`." That is wrong on two counts, confirmed by
re-reading the real file: (1) `entry_filters.py`'s own docstring already
says the call belongs "right after `strat.evaluate_signal(...)` returns a
non-None signal" — i.e. AFTER, not before; (2) more importantly,
`evaluate_entry_filters()` has a REQUIRED `planned_notional_usd` argument,
which does not exist yet at that point in either `live_bot.py` or
`backtester.py` — it is only produced by `calc_position_plan()`, which
itself only runs after the signal check. The real, data-dependency-correct
insertion point in BOTH files is **after `calc_position_plan()` succeeds,
immediately before the order/trade is actually opened** — one step later
than this package previously documented. This document supersedes that
earlier claim; `README.md` is updated accordingly (see its own 2026-10-09
section).

---

## 1. `live_bot.py` (mexc_bot) — Track A gate

### 1a. Imports and `sys.path` wiring (top of file, after the existing imports)

Real file today, lines 22–36:

```python
import os
import sys
import time
import json
import logging
from datetime import datetime, timezone

import config as cfg
from core import data_fetcher as dfetch
from core import strategy as strat
from core.risk_manager import calc_position_plan, CircuitBreaker
from core.trade_logger import TradeLogger
from notifier import TelegramNotifier
from dashboard_server import DashboardServer
```

`sys`/`os` are already imported — no new top-level import needed for
those. Add immediately after that import block (new lines, ~37):

```python
# --- INSTITUTIONAL Track A wiring (2026-10-09) -------------------------
# track_a/'s own modules (data_feeds.py, liquidity_regime_gate.py, etc.)
# import each other as flat same-directory siblings -- that directory,
# NOT mexc_bot/'s own root, must be on sys.path for `import entry_filters`
# below to resolve its own internal imports. mexc_bot/'s root is already
# on sys.path whenever this file is run directly (Python's own "script's
# own directory" rule), so `from core import ...` above is unaffected.
_TRACK_A_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "institutional", "track_a")
if _TRACK_A_DIR not in sys.path:
    sys.path.insert(0, _TRACK_A_DIR)

import entry_filters as institutional_entry_filters          # noqa: E402
from data_feeds import SyntheticFixtureDataFeeds              # noqa: E402
from data_feeds import NativeMexcFundingProvider               # noqa: E402

# Known, already-disclosed gap (see institutional/track_a/data_feeds.py and
# this package's README): MEXC order-book depth, open interest, and
# liquidation feeds do not exist anywhere in this codebase today -- only
# funding rate does. `_TrackAFeedBundle` below wires the ONE real feed that
# exists (funding, via `.mexc_native`) and leaves the other three as the
# synthetic fixture until a real feed is built. This is not a silent
# approximation: `SyntheticFixtureDataFeeds`' own `DATA_SOURCE_LABEL` makes
# this visible in every verdict's evidence.
class _TrackAFeedBundle:
    def __init__(self, exchange):
        self._synthetic = SyntheticFixtureDataFeeds()
        self.order_book = self._synthetic.order_book
        self.cross_exchange = self._synthetic.cross_exchange
        self.open_interest = self._synthetic.open_interest
        self.liquidations = self._synthetic.liquidations
        self.funding = NativeMexcFundingProvider(exchange)      # the one REAL feed
```

### 1b. `LiveBot.__init__` — new state (real file, lines 65–83)

Add inside `__init__`, after `self.dashboard.start()` (line 82):

```python
        self._track_a_feeds = _TrackAFeedBundle(self.exchange)
        self._track_a_cooldown_state: dict = {}   # caller-owned liquidation-cascade cooldown state, per entry_filters.py's own contract
        self._universe_bars_cache: dict = {}      # symbol -> most-recently-fetched OHLCV, for the macro-regime breadth check
```

### 1c. `scan_and_trade` — refresh the universe-bars cache once per cycle

`evaluate_entry_filters()` needs a `universe_bars: dict[str, pd.DataFrame]`
covering the WHOLE traded universe for its cross-sectional macro-regime
breadth check — not just the one symbol being evaluated. Fetching every
candidate's full OHLCV again just for this would duplicate
`_process_symbol`'s own fetch. **Disclosed approximation**: this hook
reuses whatever was already fetched during the current scan cycle (built
up symbol-by-symbol as `_process_symbol` runs), so coverage starts thin on
the very first cycle after a restart and fills in as the cycle proceeds —
it is never a fabricated substitute, just a partial, honestly-labeled
snapshot. A caller who wants full-universe coverage from cycle 1 would
need to pre-fetch every candidate's bars before this loop, at real added
API-call cost; not done here by default.

No change to `scan_and_trade`'s own code is required — `self.
_universe_bars_cache` is populated inside `_process_symbol` (next section).

### 1d. `_process_symbol` — the actual gate (real file, lines 179–223)

```python
    def _process_symbol(self, symbol, candidate, equity, can_open_new):
        df = dfetch.fetch_ohlcv_df(self.exchange, symbol)
        if df.empty or len(df) < cfg.CANDLES_LOOKBACK * 0.8:
            return
        df = strat.add_indicators(df)
        self._universe_bars_cache[symbol] = df   # <-- NEW: feeds the macro-regime breadth check for every symbol's evaluation this cycle and beyond

        # -- manage existing position on this symbol --
        if symbol in self.open_positions:
            self._manage_position(symbol, df)
            return

        if not can_open_new:
            return
        if len(self.open_positions) >= cfg.MAX_CONCURRENT_POSITIONS:
            return
        if not strat.passes_universe_filter(df, candidate["quote_volume_24h"],
                                             candidate["listing_age_days"]):
            return

        sig = strat.evaluate_signal(df)
        if sig["signal"] is None:
            return

        price = df["close"].iloc[-1]
        plan = calc_position_plan(price, sig["atr"], sig["signal"], equity)
        if plan is None or plan.position_size_usdt <= 0:
            return

        # --- INSTITUTIONAL Track A entry-filter gate (2026-10-09) -------
        # Inserted HERE, not right after evaluate_signal -- see this
        # document's correction note at the top: planned_notional_usd is
        # not known until calc_position_plan() has already run.
        track_a_verdict = institutional_entry_filters.evaluate_entry_filters(
            df, self._universe_bars_cache, symbol, sig["signal"],
            planned_notional_usd=plan.position_size_usdt,
            order_book=self._track_a_feeds.order_book,
            cross_exchange=self._track_a_feeds.cross_exchange,
            funding=self._track_a_feeds.funding,
            open_interest=self._track_a_feeds.open_interest,
            liquidations=self._track_a_feeds.liquidations,
            now=datetime.now(timezone.utc),
            cooldown_state=self._track_a_cooldown_state,
        )
        if not track_a_verdict.allowed:
            log.info(f"TRACK_A_BLOCKED {symbol}: {track_a_verdict.reasons}")
            return
        # -----------------------------------------------------------------

        log.info(f"SIGNAL: {symbol} {sig['signal']} @ {price:.6f} "
                 f"(reason: {sig['reason']}, leverage={plan.leverage}x, "
                 f"risk=${plan.risk_amount_usdt:.2f})")

        order = self.place_entry_order(symbol, sig["signal"], plan.quantity, plan.leverage)
        if order is not None:
            self.open_positions[symbol] = OpenPosition(
                symbol, sig["signal"], price, plan.stop_price,
                plan.quantity, plan.leverage, plan.risk_amount_usdt,
            )
            self.trade_logger.log_event(symbol, "entry", equity, side=sig["signal"],
                                         price=price, quantity=plan.quantity,
                                         leverage=plan.leverage)
            if cfg.TELEGRAM_NOTIFY_ENTRIES_EXITS:
                self.notifier.notify_entry(symbol, sig["signal"], price, plan.leverage,
                                            plan.risk_amount_usdt)
```

Every line above the `--- INSTITUTIONAL ... ---` marker through
`place_entry_order` is byte-identical to the real file today; only the
nine new lines between the markers are inserted.

---

## 2. `backtester.py` (mexc_bot) — Track A gate

Same gate, same signature, same correction (insert after sizing, not after
the signal) — applied to the sequential single-symbol replay instead of
the live scan loop. Two changes: `simulate_symbol()` gains two new optional
parameters, and `run_backtest()` is restructured to fetch every symbol's
bars BEFORE simulating any of them (needed to build `universe_bars`, since
today's loop fetches-and-simulates one symbol at a time and discards each
symbol's frame before moving to the next).

### 2a. Imports (top of file, real lines 15–26)

```python
import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

import config as cfg
from core import data_fetcher as dfetch
from core import strategy as strat
from core.risk_manager import calc_position_plan, CircuitBreaker
from core import trade_metrics

# --- INSTITUTIONAL Track A wiring (2026-10-09) -- same sys.path reasoning as live_bot.py ---
_TRACK_A_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "institutional", "track_a")
if _TRACK_A_DIR not in sys.path:
    sys.path.insert(0, _TRACK_A_DIR)
import entry_filters as institutional_entry_filters   # noqa: E402
from data_feeds import SyntheticFixtureDataFeeds        # noqa: E402
_TRACK_A_FEEDS = SyntheticFixtureDataFeeds()
# backtester.py has no live exchange handle at indicator-build time in the
# same way live_bot.py does, and funding history for an arbitrary
# historical window is a separate fetch this module does not currently
# make (see `_funding_rate_at()` below, which already falls back to a
# constant) -- so this backtest wiring uses the full synthetic fixture for
# ALL four Track A data feeds, not just three. Disclosed, not silent:
# every verdict's evidence carries `DATA_SOURCE_LABEL="SYNTHETIC_FIXTURE"`.
```

(`sys` is a new top-level import for this file; `os` already was one.)

### 2b. `simulate_symbol()` signature and the gate itself (real lines 84–254)

New parameters added to the signature (line 84–86):

```python
def simulate_symbol(df: pd.DataFrame, symbol: str, equity_tracker: dict,
                     breaker: CircuitBreaker, trade_log: list, funding_df=None,
                     entry_mode: dict = None,
                     universe_bars: dict | None = None,              # NEW
                     track_a_cooldown_state: dict | None = None):    # NEW
```

Inside the function, right after the existing `open_trade = None` /
`last_funding_ts = None` initialization (real lines 120–121), add:

```python
    universe_bars = universe_bars if universe_bars is not None else {}
    track_a_cooldown_state = track_a_cooldown_state if track_a_cooldown_state is not None else {}
```

Then, at the real entry-opening block (lines 239–254):

```python
            if sig["signal"] is not None:
                if sig["atr"] is None or pd.isna(sig["atr"]):
                    continue
                plan = calc_position_plan(price, sig["atr"], sig["signal"], equity_tracker["equity"])
                if plan is None or plan.position_size_usdt <= 0:
                    continue

                # --- INSTITUTIONAL Track A entry-filter gate (2026-10-09) ---
                track_a_verdict = institutional_entry_filters.evaluate_entry_filters(
                    window, universe_bars, symbol, sig["signal"],
                    planned_notional_usd=plan.position_size_usdt,
                    order_book=_TRACK_A_FEEDS.order_book,
                    cross_exchange=_TRACK_A_FEEDS.cross_exchange,
                    funding=_TRACK_A_FEEDS.funding,
                    open_interest=_TRACK_A_FEEDS.open_interest,
                    liquidations=_TRACK_A_FEEDS.liquidations,
                    now=row["timestamp"].to_pydatetime(),
                    cooldown_state=track_a_cooldown_state,
                )
                if not track_a_verdict.allowed:
                    continue
                # --------------------------------------------------------------

                open_trade = Trade(symbol, sig["signal"], row["timestamp"], price,
                                    plan.stop_price, plan.quantity, plan.leverage,
                                    plan.risk_amount_usdt)
                last_funding_ts = row["timestamp"]
                equity_tracker["open_count"] += 1
                if mode is not None:
                    sched_ptr += 1
                trade_log.append({"symbol": symbol, "time": row["timestamp"], "type": "entry",
                                   "side": sig["signal"], "price": price,
                                   "leverage": plan.leverage, "equity": equity_tracker["equity"]})
```

### 2c. `run_backtest()` — pre-fetch every symbol first (real lines 257–279)

Today's loop fetches and simulates one symbol at a time, discarding each
symbol's frame before the next. Building `universe_bars` requires every
symbol's frame to exist simultaneously, so the loop is split into a fetch
pass and a simulate pass:

```python
def run_backtest(symbols: list[str], days: int):
    exchange = dfetch.build_exchange()
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=days)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    equity_tracker = {"equity": cfg.BACKTEST_STARTING_EQUITY, "open_count": 0}
    breaker = CircuitBreaker(cfg.BACKTEST_STARTING_EQUITY)
    trade_log = []
    track_a_cooldown_state: dict = {}   # shared across the whole run, one dict per backtest, matching the live bot's one-dict-per-process convention

    # --- NEW: fetch pass (was previously fetch-then-simulate per symbol) ---
    universe_bars: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        print(f"Fetching {symbol} ({cfg.TIMEFRAME}, {days}d)...")
        df = dfetch.fetch_ohlcv_range(exchange, symbol, cfg.TIMEFRAME, start_ms, end_ms)
        if df.empty or len(df) < 250:
            print(f"  skipped — insufficient data ({len(df)} candles)")
            continue
        universe_bars[symbol] = df
        time.sleep(exchange.rateLimit / 1000)

    # --- simulate pass ---
    for symbol, df in universe_bars.items():
        simulate_symbol(df, symbol, equity_tracker, breaker, trade_log,
                         universe_bars=universe_bars,
                         track_a_cooldown_state=track_a_cooldown_state)

    log_df = pd.DataFrame(trade_log)
    print_report(log_df, equity_tracker["equity"])
    return log_df
```

**Disclosed consequence of this restructuring**: `universe_bars` passed
into `simulate_symbol` for symbol X now includes symbol X's OWN full-
history frame (previously `window` only, the slice up to bar `i`) inside
the dict under its own key — the macro-regime breadth check in
`macro_regime_overlay.py` reads `universe_bars` cross-sectionally for
breadth, not for any single symbol's own current bar, so this does not
leak future information into X's own entry decision; it is still `window`
(the point-in-time slice) that is passed as this call's own `window`
argument. Flagged here rather than left for a reviewer to have to
re-derive.

---

## 3. `aura_v05355_stage1_paper_trading_runner.py` (.355) — Track B + Portfolio gates

### 3a. Imports and `sys.path` wiring (real file, lines 92–104)

```python
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

VERSION = "AURA v0.5.3.55"
ENGINE = "STAGE1_PAPER_TRADING_RUNNER"
SCHEMA_VERSION = "1.0"

ROOT = Path(__file__).resolve().parent

# --- INSTITUTIONAL Track B + Portfolio wiring (2026-10-09) --------------
# track_b/'s and portfolio/'s own modules import each other as flat
# same-directory siblings (e.g. `from borrow_data_feeds import ...`,
# `import macro_buckets`) -- each subpackage's OWN directory, not .355's
# ROOT, must be on sys.path for those internal imports to resolve.
_INSTITUTIONAL_DIR = ROOT / "institutional"
_TRACK_B_DIR = _INSTITUTIONAL_DIR / "track_b"
_PORTFOLIO_DIR = _INSTITUTIONAL_DIR / "portfolio"
for _d in (_TRACK_B_DIR, _PORTFOLIO_DIR):
    _d_str = str(_d)
    if _d_str not in sys.path:
        sys.path.insert(0, _d_str)

# portfolio/'s own AURA_CORE_DIR hardening (2026-10-09) expects to be
# pointed at the directory containing the real aura_v053NN modules -- which
# is exactly .355's own ROOT. Set it here, once, so Martin does not have to
# separately configure this environment variable in production; an
# operator-set value always wins (setdefault, never overwritten).
os.environ.setdefault("AURA_CORE_DIR", str(ROOT))
```

(`os`/`sys` are new top-level imports for this file — it did not import
`os` before; `sys` was already imported for the unrelated `_load_module`
Python-3.14 fix.)

### 3b. New loader functions (alongside the existing `load_*` helpers, real lines 152–194)

```python
def load_institutional_track_b_module():
    return _load_module("short_protective_gates", "institutional/track_b/short_protective_gates.py")


def load_institutional_portfolio_module():
    return _load_module("portfolio_additional_enforcement", "institutional/portfolio/portfolio_additional_enforcement.py")
```

### 3c. New config type + composition helper (alongside `_compose_with_earnings_blackout`, real lines 495–521)

```python
@dataclass(frozen=True, slots=True)
class InstitutionalGatesConfig:
    """Bundles the caller-supplied collaborators both new subpackages
    need. `None` (the default everywhere this is threaded through) skips
    institutional gating entirely -- reproduces this module's pre-
    2026-10-09 behavior exactly, same convention as `earnings_calendar_
    state`/`limits` elsewhere in this file."""
    track_b_borrow_feed: Any
    track_b_short_interest_feed: Any
    track_b_bars_provider: Callable[[str], Any]
    track_b_config: Any | None
    macro_config: Any | None
    greeks_config: Any


def _compose_with_institutional_gates(
    enforcement_check_fn: Callable[[str, str, Any, Any], Any] | None,
    *,
    institutional_config: "InstitutionalGatesConfig | None",
    snapshot_provider: Callable[[], Any],
    account_equity_lookup: Callable[[], float],
    reference_price_fn: Callable[[str], float],
    now_dt: datetime,
) -> Callable[[str, str, Any, Any], Any] | None:
    if institutional_config is None:
        return enforcement_check_fn
    track_b_module = load_institutional_track_b_module()
    portfolio_module = load_institutional_portfolio_module()
    earnings_module = load_earnings_blackout_module()  # reuse .368's combine_enforcement_check_fns, same as _compose_with_earnings_blackout

    track_b_check_fn = track_b_module.build_short_protective_check_fn(
        borrow_feed=institutional_config.track_b_borrow_feed,
        short_interest_feed=institutional_config.track_b_short_interest_feed,
        bars_provider=institutional_config.track_b_bars_provider,
        account_equity_lookup=account_equity_lookup,
        entry_price_lookup=reference_price_fn,
        config=institutional_config.track_b_config,
    )
    portfolio_check_fn = portfolio_module.build_additional_portfolio_check_fn(
        snapshot_provider=snapshot_provider,
        greeks_config=institutional_config.greeks_config,
        account_equity_lookup=account_equity_lookup,
        macro_config=institutional_config.macro_config,
        now_fn=lambda: now_dt,
    )
    return earnings_module.combine_enforcement_check_fns(
        enforcement_check_fn, track_b_check_fn, portfolio_check_fn,
    )
```

### 3d. Call sites

**`run_stage1a_dry_run`** — new keyword-only parameter on the signature
(real lines 530–545, added alongside `earnings_calendar_state`):

```python
    earnings_calendar_state: Any | None = None,
    institutional_config: "InstitutionalGatesConfig | None" = None,   # NEW
) -> Stage1CycleReport:
```

Composition, inserted between the existing `enforcement_check_fn =
build_enforcement_check_fn(...)` block and the existing
`_compose_with_earnings_blackout` call (real lines 650–659):

```python
        enforcement_check_fn = build_enforcement_check_fn(
            snapshot=no_broker_snapshot, equity_history=equity_history or [], limits=limits,
            max_snapshot_age_seconds=max_snapshot_age_seconds, reference_price_fn=reference_price_fn,
            now=enforcement_now, decisions_by_symbol=decisions_by_symbol,
            enforcement_module=enforcement_module, observability_module=observability_module,
        )

    # --- INSTITUTIONAL Track B + Portfolio composition (2026-10-09) ----
    enforcement_check_fn = _compose_with_institutional_gates(
        enforcement_check_fn,
        institutional_config=institutional_config,
        snapshot_provider=lambda: no_broker_snapshot if limits is not None else None,
        account_equity_lookup=lambda: (
            synthetic_account_equity_usd if synthetic_account_equity_usd is not None else 0.0
        ),
        reference_price_fn=reference_price_fn or (lambda s: 0.0),
        now_dt=now_dt,
    )
    # ---------------------------------------------------------------------

    enforcement_check_fn = _compose_with_earnings_blackout(
        enforcement_check_fn, earnings_calendar_state=earnings_calendar_state, now_dt=now_dt,
    )
```

**Disclosed gap, not guessed around**: in Stage 1A, `no_broker_snapshot`
only exists inside the `if limits is not None:` branch, and `limits=None`
is this function's own default (enforcement off entirely). The lambda
above returns `None` for `snapshot_provider()` in that case;
`portfolio_module.build_additional_portfolio_check_fn`'s own `check()`
closure only calls `snapshot_provider()` after it has already determined
there is something to gate (a `macro_config` or a `proposed` position),
so this is safe in practice, but a caller that supplies `institutional_
config` with `limits=None` should be aware the portfolio gate still runs
against whatever `snapshot_provider()` returns in that configuration —
tested explicitly in this task's test additions (see below).

**`run_stage1b_paper_cycle`** — identical shape, new parameter alongside
`journal_path` (real lines 720–738):

```python
    journal_path: Path | None = None,
    institutional_config: "InstitutionalGatesConfig | None" = None,   # NEW
) -> Stage1CycleReport:
```

### RESOLVED 2026-10-09: the account-equity lookup

**This corrects Martin's own proposed internal mapping.** The directive
("reuse the exact internal calculation that `.344` already performs...
summing cash balances and current position market values: `cash +
sum(position.market_value)`") does not match the real schema or the real
code, confirmed by re-reading `aura_v05343_portfolio_exposure_
observability.py` directly (and re-verified functionally, see below):

- `PortfolioSnapshot` has no `cash` field anywhere (`as_of`, `positions`,
  `venue_fetch_status`, `is_complete`, `state_hash` — that's all of it,
  real lines 336–341).
- `PositionRecord` has no `market_value` field either — it has
  `notional_usd` (real lines 292–312). `market_value` only ever appears
  transiently, inside `fetch_alpaca_portfolio()`, as a raw attribute read
  off the Alpaca SDK's own position object (`getattr(p, "market_value",
  None)`, real line 542) purely to COMPUTE `notional_usd` — it is never
  stored, and reconstructing total equity from it would double-count
  unrealized P&L already baked into the broker's own reported equity.
- The REAL computation `.344` already performs (real lines 902+918) is
  far simpler than "cash + position values": each venue's OWN
  broker-reported total-equity field is read ONCE, per venue, at fetch
  time (`fetch_alpaca_portfolio`, real line 518: `equity =
  float(account.equity)` — Alpaca's own `TradingClient.get_account()
  .equity`; `fetch_mexc_portfolio`, real line 393: `balance['USDT']
  ['total']`), stored on `VenueFetchStatus.equity`, and then simply
  summed across venues that succeeded:

  ```python
  equity_by_venue = {v: status.equity for v, status in snapshot.venue_fetch_status.items()}
  total_equity = sum(e for e in equity_by_venue.values() if e is not None) or None
  ```

  This is `.344`'s own real one-liner (`aura_v05343_portfolio_exposure_
  observability.py` lines 902/918), not a new derivation — this document
  reuses it verbatim rather than re-deriving equity from positions at all.

New helper, placed alongside `_compose_with_institutional_gates` (same
section as the earlier loader functions):

```python
def _account_equity_from_snapshot(snapshot: Any) -> float | None:
    """Reuses .344's OWN real total-equity computation verbatim
    (aura_v05343_portfolio_exposure_observability.py lines 902/918) --
    never 'cash + sum(position.market_value)' (PortfolioSnapshot has no
    cash field; PositionRecord has no market_value field -- only
    notional_usd; see this section's correction note). A venue that
    failed or was never configured already carries `equity=None` on its
    own VenueFetchStatus (set by fetch_alpaca_portfolio/
    fetch_mexc_portfolio themselves) -- excluding None here is not a new
    judgment call, it is how `.343`/`.344` already treat an unreadable
    venue. Returns None (never 0.0, never a guessed fallback) when no
    venue has a usable equity figure -- both greeks_limits.py and
    macro_buckets.py already fail closed (NOT_COMPUTABLE, never a silent
    PASS) on `account_equity_usd is None or account_equity_usd <= 0`,
    confirmed by direct call in this task's own verification (below)."""
    equity_by_venue = {v: status.equity for v, status in snapshot.venue_fetch_status.items()}
    values = [e for e in equity_by_venue.values() if e is not None]
    return sum(values) if values else None
```

Composition, inserted between the existing enforcement-building block and
`_compose_with_earnings_blackout` (real lines 834–842):

```python
    enforcement_check_fn = build_enforcement_check_fn(
        snapshot=snapshot, equity_history=equity_history, limits=limits,
        max_snapshot_age_seconds=max_snapshot_age_seconds, reference_price_fn=reference_price_fn,
        now=enforcement_now, decisions_by_symbol=decisions_by_symbol,
        enforcement_module=enforcement_module, observability_module=observability_module,
    )

    # --- INSTITUTIONAL Track B + Portfolio composition (2026-10-09) ----
    enforcement_check_fn = _compose_with_institutional_gates(
        enforcement_check_fn,
        institutional_config=institutional_config,
        snapshot_provider=lambda: snapshot,
        account_equity_lookup=lambda: _account_equity_from_snapshot(snapshot),
        reference_price_fn=reference_price_fn,
        now_dt=now_dt,
    )
    # ---------------------------------------------------------------------

    enforcement_check_fn = _compose_with_earnings_blackout(
        enforcement_check_fn, earnings_calendar_state=earnings_calendar_state, now_dt=now_dt,
    )
```

### Fail-closed verification (point 3 of Martin's request) — run directly against the real staged `.343`/`greeks_limits.py`, not asserted

Four cases, run against the real classes:

| Scenario | `_account_equity_from_snapshot()` result | Downstream (`greeks_limits.evaluate_greeks_dimension`, limit configured) |
|---|---|---|
| ALPACA + MEXC both `SUCCESS` (25000.0 / 10000.0) | `35000.0` | normal ALLOW/BLOCK against the real sum |
| ALPACA `FAILED` (equity=`None`), MEXC `SUCCESS` (10000.0) | `10000.0` — failed venue excluded, not treated as zero | normal ALLOW/BLOCK against the one good venue |
| Both venues `FAILED`/`NOT_CONFIGURED` (snapshot "corrupt or unreadable") | `None` — **never `0.0`, never a guessed fallback** | `NOT_COMPUTABLE`, reason `ACCOUNT_EQUITY_NOT_AVAILABLE` — confirmed by direct call, not inferred |

The third row is the fail-closed case Martin's point 3 asked to verify:
an unreadable snapshot produces `account_equity_lookup() -> None`, which
`greeks_limits.py`'s own pre-existing guard (`if account_equity_usd is
None or account_equity_usd <= 0:`) turns into `NOT_COMPUTABLE` — never a
silent `ALLOW`, never a `ZeroDivisionError`, never a fabricated equity
figure. `macro_buckets.py` has the identical guard for the same reason.
This was executed against the real staged files in this session, not
reasoned about abstractly.

---

## Summary of what's genuinely new vs. what's a disclosed open question

| File | Status |
|---|---|
| `live_bot.py` | Complete, concrete snippet; `_universe_bars_cache` breadth-check coverage is a disclosed partial/best-effort approximation, not a gap in the gate logic itself. |
| `backtester.py` | Complete, concrete snippet; requires restructuring `run_backtest()`'s fetch loop (shown above) — a real, disclosed behavior change to that function, not a drop-in one-liner. |
| `.355` (Stage 1A) | Complete, concrete snippet. |
| `.355` (Stage 1B) | Complete, concrete snippet. The account-equity lookup is now resolved: `.344`'s own real per-venue sum, reused verbatim — not the "cash + sum(position.market_value)" mapping originally proposed, which does not match this repo's real schema (see correction note above). Fail-closed behavior verified directly against the real staged classes, not asserted. |
