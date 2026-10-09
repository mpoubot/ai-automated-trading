#!/usr/bin/env python3
"""
Anchor-verified patch script — applies the Task 1 integration hooks from
`institutional/INTEGRATION_HOOKS_2026-10-09.md` to the three real run-loop
files (`.355`, `mexc_bot/live_bot.py`, `mexc_bot/backtester.py`).

WHY ANCHOR-VERIFIED, NOT LINE-NUMBER-BASED: line numbers drift the moment
any earlier line in a file changes. Every patch below instead searches for
an exact, multi-line, byte-for-byte snippet of the file's OWN real current
content (verified against a freshly staged copy of your three files on
2026-10-09, immediately before this script was written) and inserts next
to it. If a single character of any anchor does not match EXACTLY ONCE in
the live file when you run this, that file's patches are skipped entirely
and the script exits non-zero — it never guesses, never partial-applies a
file, and never touches a file it isn't 100% sure about.

USAGE
    python apply_institutional_hooks_2026-10-09.py --dry-run
        Checks every anchor against your real files and reports
        MATCH / NOT FOUND / MATCHED N TIMES (anything other than exactly
        one match blocks that file). Makes NO changes.

    python apply_institutional_hooks_2026-10-09.py
        Same checks, and if (and only if) every anchor for a given file
        matches exactly once, writes a timestamped backup of that file
        (<file>.bak-YYYYMMDD-HHMMSS) next to it, then applies all of that
        file's patches in order and writes the result. Each file is
        all-or-nothing: if any one of its anchors fails, NOTHING in that
        file is touched, and the other files are still attempted
        independently.

Run this from the repo root (the directory containing this script,
`aura_v05355_stage1_paper_trading_runner.py`, and the `mexc_bot/`
subdirectory).

After running for real: `python -m py_compile` the three patched files
(sanity check #1 in the deployment checklist), confirm each new
`sys.path.insert(...)` line precedes its corresponding import (#2),
deliberately unset AURA_CORE_DIR once to confirm the fail-closed error
fires (#3), re-run the full test suite (#4), and do one
institutional_config=None dry run to confirm byte-identical behavior (#5)
— per the operational deployment checklist.
"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# ============================================================================
# live_bot.py
# ============================================================================

LIVE_BOT_PATCHES = [
    (
        "imports + sys.path wiring",
        'from dashboard_server import DashboardServer\n',
        'from dashboard_server import DashboardServer\n'
        '\n'
        '# --- INSTITUTIONAL Track A wiring (2026-10-09) -------------------------\n'
        '# track_a/\'s own modules (data_feeds.py, liquidity_regime_gate.py, etc.)\n'
        '# import each other as flat same-directory siblings -- that directory,\n'
        '# NOT mexc_bot/\'s own root, must be on sys.path for `import entry_filters`\n'
        '# below to resolve its own internal imports. mexc_bot/\'s root is already\n'
        '# on sys.path whenever this file is run directly (Python\'s own "script\'s\n'
        '# own directory" rule), so `from core import ...` above is unaffected.\n'
        '_TRACK_A_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "institutional", "track_a")\n'
        'if _TRACK_A_DIR not in sys.path:\n'
        '    sys.path.insert(0, _TRACK_A_DIR)\n'
        '\n'
        'import entry_filters as institutional_entry_filters          # noqa: E402\n'
        'from data_feeds import SyntheticFixtureDataFeeds              # noqa: E402\n'
        'from data_feeds import NativeMexcFundingProvider               # noqa: E402\n'
        '\n'
        '# Known, already-disclosed gap (see institutional/track_a/data_feeds.py and\n'
        '# this package\'s README): MEXC order-book depth, open interest, and\n'
        '# liquidation feeds do not exist anywhere in this codebase today -- only\n'
        '# funding rate does. `_TrackAFeedBundle` below wires the ONE real feed that\n'
        '# exists (funding, via `.mexc_native`) and leaves the other three as the\n'
        '# synthetic fixture until a real feed is built. This is not a silent\n'
        '# approximation: `SyntheticFixtureDataFeeds\'` own `DATA_SOURCE_LABEL` makes\n'
        '# this visible in every verdict\'s evidence.\n'
        'class _TrackAFeedBundle:\n'
        '    def __init__(self, exchange):\n'
        '        self._synthetic = SyntheticFixtureDataFeeds()\n'
        '        self.order_book = self._synthetic.order_book\n'
        '        self.cross_exchange = self._synthetic.cross_exchange\n'
        '        self.open_interest = self._synthetic.open_interest\n'
        '        self.liquidations = self._synthetic.liquidations\n'
        '        self.funding = NativeMexcFundingProvider(exchange)      # the one REAL feed\n',
    ),
    (
        "__init__ new state",
        '        self.dashboard.start()\n'
        '        self._last_summary_date = None\n',
        '        self.dashboard.start()\n'
        '        self._last_summary_date = None\n'
        '        self._track_a_feeds = _TrackAFeedBundle(self.exchange)\n'
        '        self._track_a_cooldown_state: dict = {}   # caller-owned liquidation-cascade cooldown state, per entry_filters.py\'s own contract\n'
        '        self._universe_bars_cache: dict = {}      # symbol -> most-recently-fetched OHLCV, for the macro-regime breadth check\n',
    ),
    (
        "_process_symbol gate + universe_bars cache feed",
        '        df = strat.add_indicators(df)\n'
        '\n'
        '        # -- manage existing position on this symbol --',
        '        df = strat.add_indicators(df)\n'
        '        self._universe_bars_cache[symbol] = df   # <-- NEW: feeds the macro-regime breadth check for every symbol\'s evaluation this cycle and beyond\n'
        '\n'
        '        # -- manage existing position on this symbol --',
    ),
    (
        "_process_symbol institutional gate call",
        '        price = df["close"].iloc[-1]\n'
        '        plan = calc_position_plan(price, sig["atr"], sig["signal"], equity)\n'
        '        if plan is None or plan.position_size_usdt <= 0:\n'
        '            return\n'
        '\n'
        '        log.info(f"SIGNAL: {symbol} {sig[\'signal\']} @ {price:.6f} "\n',
        '        price = df["close"].iloc[-1]\n'
        '        plan = calc_position_plan(price, sig["atr"], sig["signal"], equity)\n'
        '        if plan is None or plan.position_size_usdt <= 0:\n'
        '            return\n'
        '\n'
        '        # --- INSTITUTIONAL Track A entry-filter gate (2026-10-09) -------\n'
        '        # Inserted HERE, not right after evaluate_signal -- see this\n'
        '        # document\'s correction note at the top: planned_notional_usd is\n'
        '        # not known until calc_position_plan() has already run.\n'
        '        track_a_verdict = institutional_entry_filters.evaluate_entry_filters(\n'
        '            df, self._universe_bars_cache, symbol, sig["signal"],\n'
        '            planned_notional_usd=plan.position_size_usdt,\n'
        '            order_book=self._track_a_feeds.order_book,\n'
        '            cross_exchange=self._track_a_feeds.cross_exchange,\n'
        '            funding=self._track_a_feeds.funding,\n'
        '            open_interest=self._track_a_feeds.open_interest,\n'
        '            liquidations=self._track_a_feeds.liquidations,\n'
        '            now=datetime.now(timezone.utc),\n'
        '            cooldown_state=self._track_a_cooldown_state,\n'
        '        )\n'
        '        if not track_a_verdict.allowed:\n'
        '            log.info(f"TRACK_A_BLOCKED {symbol}: {track_a_verdict.reasons}")\n'
        '            return\n'
        '        # -----------------------------------------------------------------\n'
        '\n'
        '        log.info(f"SIGNAL: {symbol} {sig[\'signal\']} @ {price:.6f} "\n',
    ),
]

# ============================================================================
# backtester.py
# ============================================================================

BACKTESTER_PATCHES = [
    (
        "imports + sys.path wiring",
        'from core import trade_metrics\n',
        'from core import trade_metrics\n'
        '\n'
        '# --- INSTITUTIONAL Track A wiring (2026-10-09) -- same sys.path reasoning as live_bot.py ---\n'
        'import sys\n'
        '_TRACK_A_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "institutional", "track_a")\n'
        'if _TRACK_A_DIR not in sys.path:\n'
        '    sys.path.insert(0, _TRACK_A_DIR)\n'
        'import entry_filters as institutional_entry_filters   # noqa: E402\n'
        'from data_feeds import SyntheticFixtureDataFeeds        # noqa: E402\n'
        '_TRACK_A_FEEDS = SyntheticFixtureDataFeeds()\n'
        '# backtester.py has no live exchange handle at indicator-build time in the\n'
        '# same way live_bot.py does, and funding history for an arbitrary\n'
        '# historical window is a separate fetch this module does not currently\n'
        '# make (see `_funding_rate_at()` below, which already falls back to a\n'
        '# constant) -- so this backtest wiring uses the full synthetic fixture for\n'
        '# ALL four Track A data feeds, not just three. Disclosed, not silent:\n'
        '# every verdict\'s evidence carries `DATA_SOURCE_LABEL="SYNTHETIC_FIXTURE"`.\n',
    ),
    (
        "simulate_symbol signature — new params",
        '                     entry_mode: dict = None):\n',
        '                     entry_mode: dict = None,\n'
        '                     universe_bars: dict | None = None,              # NEW\n'
        '                     track_a_cooldown_state: dict | None = None):    # NEW\n',
    ),
    (
        "simulate_symbol local defaults",
        '    open_trade = None\n'
        '    last_funding_ts = None\n',
        '    open_trade = None\n'
        '    last_funding_ts = None\n'
        '    universe_bars = universe_bars if universe_bars is not None else {}\n'
        '    track_a_cooldown_state = track_a_cooldown_state if track_a_cooldown_state is not None else {}\n',
    ),
    (
        "simulate_symbol institutional gate call",
        '            if sig["signal"] is not None:\n'
        '                if sig["atr"] is None or pd.isna(sig["atr"]):\n'
        '                    continue\n'
        '                plan = calc_position_plan(price, sig["atr"], sig["signal"], equity_tracker["equity"])\n'
        '                if plan is None or plan.position_size_usdt <= 0:\n'
        '                    continue\n'
        '                open_trade = Trade(symbol, sig["signal"], row["timestamp"], price,\n'
        '                                    plan.stop_price, plan.quantity, plan.leverage,\n'
        '                                    plan.risk_amount_usdt)\n'
        '                last_funding_ts = row["timestamp"]\n'
        '                equity_tracker["open_count"] += 1\n',
        '            if sig["signal"] is not None:\n'
        '                if sig["atr"] is None or pd.isna(sig["atr"]):\n'
        '                    continue\n'
        '                plan = calc_position_plan(price, sig["atr"], sig["signal"], equity_tracker["equity"])\n'
        '                if plan is None or plan.position_size_usdt <= 0:\n'
        '                    continue\n'
        '\n'
        '                # --- INSTITUTIONAL Track A entry-filter gate (2026-10-09) ---\n'
        '                track_a_verdict = institutional_entry_filters.evaluate_entry_filters(\n'
        '                    window, universe_bars, symbol, sig["signal"],\n'
        '                    planned_notional_usd=plan.position_size_usdt,\n'
        '                    order_book=_TRACK_A_FEEDS.order_book,\n'
        '                    cross_exchange=_TRACK_A_FEEDS.cross_exchange,\n'
        '                    funding=_TRACK_A_FEEDS.funding,\n'
        '                    open_interest=_TRACK_A_FEEDS.open_interest,\n'
        '                    liquidations=_TRACK_A_FEEDS.liquidations,\n'
        '                    now=row["timestamp"].to_pydatetime(),\n'
        '                    cooldown_state=track_a_cooldown_state,\n'
        '                )\n'
        '                if not track_a_verdict.allowed:\n'
        '                    continue\n'
        '                # --------------------------------------------------------------\n'
        '\n'
        '                open_trade = Trade(symbol, sig["signal"], row["timestamp"], price,\n'
        '                                    plan.stop_price, plan.quantity, plan.leverage,\n'
        '                                    plan.risk_amount_usdt)\n'
        '                last_funding_ts = row["timestamp"]\n'
        '                equity_tracker["open_count"] += 1\n',
    ),
    (
        "run_backtest() — fetch-then-simulate restructuring",
        'def run_backtest(symbols: list[str], days: int):\n'
        '    exchange = dfetch.build_exchange()\n'
        '    end_dt = datetime.now(timezone.utc)\n'
        '    start_dt = end_dt - timedelta(days=days)\n'
        '    start_ms = int(start_dt.timestamp() * 1000)\n'
        '    end_ms = int(end_dt.timestamp() * 1000)\n'
        '\n'
        '    equity_tracker = {"equity": cfg.BACKTEST_STARTING_EQUITY, "open_count": 0}\n'
        '    breaker = CircuitBreaker(cfg.BACKTEST_STARTING_EQUITY)\n'
        '    trade_log = []\n'
        '\n'
        '    for symbol in symbols:\n'
        '        print(f"Fetching {symbol} ({cfg.TIMEFRAME}, {days}d)...")\n'
        '        df = dfetch.fetch_ohlcv_range(exchange, symbol, cfg.TIMEFRAME, start_ms, end_ms)\n'
        '        if df.empty or len(df) < 250:\n'
        '            print(f"  skipped — insufficient data ({len(df)} candles)")\n'
        '            continue\n'
        '        simulate_symbol(df, symbol, equity_tracker, breaker, trade_log)\n'
        '        time.sleep(exchange.rateLimit / 1000)\n'
        '\n'
        '    log_df = pd.DataFrame(trade_log)\n'
        '    print_report(log_df, equity_tracker["equity"])\n'
        '    return log_df\n',
        'def run_backtest(symbols: list[str], days: int):\n'
        '    exchange = dfetch.build_exchange()\n'
        '    end_dt = datetime.now(timezone.utc)\n'
        '    start_dt = end_dt - timedelta(days=days)\n'
        '    start_ms = int(start_dt.timestamp() * 1000)\n'
        '    end_ms = int(end_dt.timestamp() * 1000)\n'
        '\n'
        '    equity_tracker = {"equity": cfg.BACKTEST_STARTING_EQUITY, "open_count": 0}\n'
        '    breaker = CircuitBreaker(cfg.BACKTEST_STARTING_EQUITY)\n'
        '    trade_log = []\n'
        '    track_a_cooldown_state: dict = {}   # shared across the whole run, one dict per backtest, matching the live bot\'s one-dict-per-process convention\n'
        '\n'
        '    # --- NEW: fetch pass (was previously fetch-then-simulate per symbol) ---\n'
        '    universe_bars: dict[str, pd.DataFrame] = {}\n'
        '    for symbol in symbols:\n'
        '        print(f"Fetching {symbol} ({cfg.TIMEFRAME}, {days}d)...")\n'
        '        df = dfetch.fetch_ohlcv_range(exchange, symbol, cfg.TIMEFRAME, start_ms, end_ms)\n'
        '        if df.empty or len(df) < 250:\n'
        '            print(f"  skipped — insufficient data ({len(df)} candles)")\n'
        '            continue\n'
        '        universe_bars[symbol] = df\n'
        '        time.sleep(exchange.rateLimit / 1000)\n'
        '\n'
        '    # --- simulate pass ---\n'
        '    for symbol, df in universe_bars.items():\n'
        '        simulate_symbol(df, symbol, equity_tracker, breaker, trade_log,\n'
        '                         universe_bars=universe_bars,\n'
        '                         track_a_cooldown_state=track_a_cooldown_state)\n'
        '\n'
        '    log_df = pd.DataFrame(trade_log)\n'
        '    print_report(log_df, equity_tracker["equity"])\n'
        '    return log_df\n',
    ),
]

# ============================================================================
# aura_v05355_stage1_paper_trading_runner.py (.355)
# ============================================================================

_355_INSTITUTIONAL_HELPERS = (
    '\n'
    '# --- INSTITUTIONAL Track B + Portfolio composition helpers (2026-10-09) ---\n'
    '@dataclass(frozen=True, slots=True)\n'
    'class InstitutionalGatesConfig:\n'
    '    """Bundles the caller-supplied collaborators both new subpackages\n'
    '    need. `None` (the default everywhere this is threaded through) skips\n'
    '    institutional gating entirely -- reproduces this module\'s pre-\n'
    '    2026-10-09 behavior exactly, same convention as `earnings_calendar_\n'
    '    state`/`limits` elsewhere in this file."""\n'
    '    track_b_borrow_feed: Any\n'
    '    track_b_short_interest_feed: Any\n'
    '    track_b_bars_provider: Callable[[str], Any]\n'
    '    track_b_config: Any | None\n'
    '    macro_config: Any | None\n'
    '    greeks_config: Any\n'
    '\n'
    '\n'
    'def _compose_with_institutional_gates(\n'
    '    enforcement_check_fn: Callable[[str, str, Any, Any], Any] | None,\n'
    '    *,\n'
    '    institutional_config: "InstitutionalGatesConfig | None",\n'
    '    snapshot_provider: Callable[[], Any],\n'
    '    account_equity_lookup: Callable[[], float],\n'
    '    reference_price_fn: Callable[[str], float],\n'
    '    now_dt: datetime,\n'
    ') -> Callable[[str, str, Any, Any], Any] | None:\n'
    '    if institutional_config is None:\n'
    '        return enforcement_check_fn\n'
    '    track_b_module = load_institutional_track_b_module()\n'
    '    portfolio_module = load_institutional_portfolio_module()\n'
    '    earnings_module = load_earnings_blackout_module()  # reuse .368\'s combine_enforcement_check_fns, same as _compose_with_earnings_blackout\n'
    '\n'
    '    track_b_check_fn = track_b_module.build_short_protective_check_fn(\n'
    '        borrow_feed=institutional_config.track_b_borrow_feed,\n'
    '        short_interest_feed=institutional_config.track_b_short_interest_feed,\n'
    '        bars_provider=institutional_config.track_b_bars_provider,\n'
    '        account_equity_lookup=account_equity_lookup,\n'
    '        entry_price_lookup=reference_price_fn,\n'
    '        config=institutional_config.track_b_config,\n'
    '    )\n'
    '    portfolio_check_fn = portfolio_module.build_additional_portfolio_check_fn(\n'
    '        snapshot_provider=snapshot_provider,\n'
    '        greeks_config=institutional_config.greeks_config,\n'
    '        account_equity_lookup=account_equity_lookup,\n'
    '        macro_config=institutional_config.macro_config,\n'
    '        now_fn=lambda: now_dt,\n'
    '    )\n'
    '    return earnings_module.combine_enforcement_check_fns(\n'
    '        enforcement_check_fn, track_b_check_fn, portfolio_check_fn,\n'
    '    )\n'
    '\n'
    '\n'
    'def _account_equity_from_snapshot(snapshot: Any) -> float | None:\n'
    '    """Reuses .344\'s OWN real total-equity computation verbatim\n'
    '    (aura_v05343_portfolio_exposure_observability.py lines 902/918) --\n'
    '    never \'cash + sum(position.market_value)\' (PortfolioSnapshot has no\n'
    '    cash field; PositionRecord has no market_value field -- only\n'
    '    notional_usd; see institutional/INTEGRATION_HOOKS_2026-10-09.md\'s\n'
    '    RESOLVED 2026-10-09 section for the correction note). A venue that\n'
    '    failed or was never configured already carries equity=None on its\n'
    '    own VenueFetchStatus (set by fetch_alpaca_portfolio/\n'
    '    fetch_mexc_portfolio themselves) -- excluding None here is not a new\n'
    '    judgment call, it is how `.343`/`.344` already treat an unreadable\n'
    '    venue. Returns None (never 0.0, never a guessed fallback) when no\n'
    '    venue has a usable equity figure -- both greeks_limits.py and\n'
    '    macro_buckets.py already fail closed (NOT_COMPUTABLE, never a silent\n'
    '    PASS) on `account_equity_usd is None or account_equity_usd <= 0`."""\n'
    '    equity_by_venue = {v: status.equity for v, status in snapshot.venue_fetch_status.items()}\n'
    '    values = [e for e in equity_by_venue.values() if e is not None]\n'
    '    return sum(values) if values else None\n'
)

_355_PATCHES = [
    (
        "import os (sys already imported)",
        'import sys\n'
        'from dataclasses import dataclass, field\n',
        'import os\n'
        'import sys\n'
        'from dataclasses import dataclass, field\n',
    ),
    (
        "sys.path wiring + AURA_CORE_DIR default",
        'ROOT = Path(__file__).resolve().parent\n',
        'ROOT = Path(__file__).resolve().parent\n'
        '\n'
        '# --- INSTITUTIONAL Track B + Portfolio wiring (2026-10-09) --------------\n'
        '# track_b/\'s and portfolio/\'s own modules import each other as flat\n'
        '# same-directory siblings (e.g. `from borrow_data_feeds import ...`,\n'
        '# `import macro_buckets`) -- each subpackage\'s OWN directory, not .355\'s\n'
        '# ROOT, must be on sys.path for those internal imports to resolve.\n'
        '_INSTITUTIONAL_DIR = ROOT / "institutional"\n'
        '_TRACK_B_DIR = _INSTITUTIONAL_DIR / "track_b"\n'
        '_PORTFOLIO_DIR = _INSTITUTIONAL_DIR / "portfolio"\n'
        'for _d in (_TRACK_B_DIR, _PORTFOLIO_DIR):\n'
        '    _d_str = str(_d)\n'
        '    if _d_str not in sys.path:\n'
        '        sys.path.insert(0, _d_str)\n'
        '\n'
        '# portfolio/\'s own AURA_CORE_DIR hardening (2026-10-09) expects to be\n'
        '# pointed at the directory containing the real aura_v053NN modules -- which\n'
        '# is exactly .355\'s own ROOT. Set it here, once, so Martin does not have to\n'
        '# separately configure this environment variable in production; an\n'
        '# operator-set value always wins (setdefault, never overwritten).\n'
        'os.environ.setdefault("AURA_CORE_DIR", str(ROOT))\n',
    ),
    (
        "new loader functions",
        'def load_earnings_blackout_module():\n'
        '    return _load_module("aura_v05368_earnings_blackout_gate", "aura_v05368_earnings_blackout_gate.py")\n',
        'def load_earnings_blackout_module():\n'
        '    return _load_module("aura_v05368_earnings_blackout_gate", "aura_v05368_earnings_blackout_gate.py")\n'
        '\n'
        '\n'
        'def load_institutional_track_b_module():\n'
        '    return _load_module("short_protective_gates", "institutional/track_b/short_protective_gates.py")\n'
        '\n'
        '\n'
        'def load_institutional_portfolio_module():\n'
        '    return _load_module("portfolio_additional_enforcement", "institutional/portfolio/portfolio_additional_enforcement.py")\n',
    ),
    (
        "InstitutionalGatesConfig + composition helpers (incl. resolved account-equity lookup)",
        'def _compose_with_earnings_blackout(\n',
        _355_INSTITUTIONAL_HELPERS + '\n\ndef _compose_with_earnings_blackout(\n',
    ),
    (
        "Stage 1A signature — new institutional_config param",
        '    earnings_calendar_state: Any | None = None,\n'
        ') -> Stage1CycleReport:',
        '    earnings_calendar_state: Any | None = None,\n'
        '    institutional_config: "InstitutionalGatesConfig | None" = None,   # NEW\n'
        ') -> Stage1CycleReport:',
    ),
    (
        "Stage 1B signature — new institutional_config param",
        '    earnings_calendar_state: Any | None = None,\n'
        '    journal_path: Path | None = None,\n'
        ') -> Stage1CycleReport:',
        '    earnings_calendar_state: Any | None = None,\n'
        '    journal_path: Path | None = None,\n'
        '    institutional_config: "InstitutionalGatesConfig | None" = None,   # NEW\n'
        ') -> Stage1CycleReport:',
    ),
    (
        "Stage 1A composition call site",
        '        enforcement_check_fn = build_enforcement_check_fn(\n'
        '            snapshot=no_broker_snapshot, equity_history=equity_history or [], limits=limits,\n'
        '            max_snapshot_age_seconds=max_snapshot_age_seconds, reference_price_fn=reference_price_fn,\n'
        '            now=enforcement_now, decisions_by_symbol=decisions_by_symbol,\n'
        '            enforcement_module=enforcement_module, observability_module=observability_module,\n'
        '        )\n'
        '\n'
        '    enforcement_check_fn = _compose_with_earnings_blackout(\n'
        '        enforcement_check_fn, earnings_calendar_state=earnings_calendar_state, now_dt=now_dt,\n'
        '    )\n',
        '        enforcement_check_fn = build_enforcement_check_fn(\n'
        '            snapshot=no_broker_snapshot, equity_history=equity_history or [], limits=limits,\n'
        '            max_snapshot_age_seconds=max_snapshot_age_seconds, reference_price_fn=reference_price_fn,\n'
        '            now=enforcement_now, decisions_by_symbol=decisions_by_symbol,\n'
        '            enforcement_module=enforcement_module, observability_module=observability_module,\n'
        '        )\n'
        '\n'
        '    # --- INSTITUTIONAL Track B + Portfolio composition (2026-10-09) ----\n'
        '    enforcement_check_fn = _compose_with_institutional_gates(\n'
        '        enforcement_check_fn,\n'
        '        institutional_config=institutional_config,\n'
        '        snapshot_provider=lambda: no_broker_snapshot if limits is not None else None,\n'
        '        account_equity_lookup=lambda: (\n'
        '            synthetic_account_equity_usd if synthetic_account_equity_usd is not None else 0.0\n'
        '        ),\n'
        '        reference_price_fn=reference_price_fn or (lambda s: 0.0),\n'
        '        now_dt=now_dt,\n'
        '    )\n'
        '    # ---------------------------------------------------------------------\n'
        '\n'
        '    enforcement_check_fn = _compose_with_earnings_blackout(\n'
        '        enforcement_check_fn, earnings_calendar_state=earnings_calendar_state, now_dt=now_dt,\n'
        '    )\n',
    ),
    (
        "Stage 1B composition call site (resolved account-equity lookup)",
        '    enforcement_check_fn = build_enforcement_check_fn(\n'
        '        snapshot=snapshot, equity_history=equity_history, limits=limits,\n'
        '        max_snapshot_age_seconds=max_snapshot_age_seconds, reference_price_fn=reference_price_fn,\n'
        '        now=enforcement_now, decisions_by_symbol=decisions_by_symbol,\n'
        '        enforcement_module=enforcement_module, observability_module=observability_module,\n'
        '    )\n'
        '    enforcement_check_fn = _compose_with_earnings_blackout(\n'
        '        enforcement_check_fn, earnings_calendar_state=earnings_calendar_state, now_dt=now_dt,\n'
        '    )\n',
        '    enforcement_check_fn = build_enforcement_check_fn(\n'
        '        snapshot=snapshot, equity_history=equity_history, limits=limits,\n'
        '        max_snapshot_age_seconds=max_snapshot_age_seconds, reference_price_fn=reference_price_fn,\n'
        '        now=enforcement_now, decisions_by_symbol=decisions_by_symbol,\n'
        '        enforcement_module=enforcement_module, observability_module=observability_module,\n'
        '    )\n'
        '\n'
        '    # --- INSTITUTIONAL Track B + Portfolio composition (2026-10-09) ----\n'
        '    enforcement_check_fn = _compose_with_institutional_gates(\n'
        '        enforcement_check_fn,\n'
        '        institutional_config=institutional_config,\n'
        '        snapshot_provider=lambda: snapshot,\n'
        '        account_equity_lookup=lambda: _account_equity_from_snapshot(snapshot),\n'
        '        reference_price_fn=reference_price_fn,\n'
        '        now_dt=now_dt,\n'
        '    )\n'
        '    # ---------------------------------------------------------------------\n'
        '\n'
        '    enforcement_check_fn = _compose_with_earnings_blackout(\n'
        '        enforcement_check_fn, earnings_calendar_state=earnings_calendar_state, now_dt=now_dt,\n'
        '    )\n',
    ),
]

FILES = [
    ("aura_v05355_stage1_paper_trading_runner.py", _355_PATCHES),
    ("../mexc_bot/live_bot.py", LIVE_BOT_PATCHES),
    ("../mexc_bot/backtester.py", BACKTESTER_PATCHES),
]


def check_and_apply(relpath: str, patches: list[tuple[str, str, str]], dry_run: bool) -> bool:
    path = ROOT / relpath
    print(f"\n=== {relpath} ===")
    if not path.exists():
        print(f"  FILE NOT FOUND at {path} -- skipped.")
        return False

    original = path.read_text(encoding="utf-8")
    text = original
    all_ok = True
    for label, anchor, _replacement in patches:
        n = original.count(anchor)
        if n == 1:
            print(f"  [match]      {label}")
        elif n == 0:
            print(f"  [NOT FOUND]  {label} -- this file has diverged from what this script expects.")
            all_ok = False
        else:
            print(f"  [AMBIGUOUS]  {label} -- anchor appears {n} times, expected exactly 1.")
            all_ok = False

    if not all_ok:
        print(f"  => {relpath}: one or more anchors failed. NOTHING in this file will be changed.")
        return False

    if dry_run:
        print(f"  => {relpath}: all {len(patches)} anchors matched exactly once. (--dry-run: no changes written)")
        return True

    for _label, anchor, replacement in patches:
        assert text.count(anchor) == 1, "anchor count changed mid-patch -- aborting this file"
        text = text.replace(anchor, replacement, 1)

    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = path.with_suffix(path.suffix + f".bak-{timestamp}")
    backup_path.write_text(original, encoding="utf-8")
    path.write_text(text, encoding="utf-8")
    print(f"  => {relpath}: patched. Backup written to {backup_path.name}")
    return True


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    print(f"Mode: {'DRY RUN (no changes will be written)' if dry_run else 'APPLY (files will be modified, backups written)'}")
    print(f"Repo root: {ROOT}")

    results = {relpath: check_and_apply(relpath, patches, dry_run) for relpath, patches in FILES}

    print("\n=== Summary ===")
    for relpath, ok in results.items():
        print(f"  {'OK  ' if ok else 'FAIL'}  {relpath}")

    if not all(results.values()):
        print("\nAt least one file did not patch cleanly. See [NOT FOUND]/[AMBIGUOUS] lines above.")
        print("No partially-patched file was written for any failing file.")
        return 1

    if dry_run:
        print("\nAll anchors matched. Re-run without --dry-run to apply.")
    else:
        print("\nAll three files patched. Next: run_backtest's own docstring/line numbers will")
        print("have shifted -- that's expected and harmless (this script never relied on them).")
        print("Proceed to checklist step 2: python -m py_compile on all three files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
