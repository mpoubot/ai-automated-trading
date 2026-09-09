"""
Live trading bot. Runs continuously on your machine, scanning the MEXC pair
universe on a fixed interval and executing trades per the strategy + risk
rules in config.py.

SAFETY:
  - Starts in DRY_RUN mode by default (config.py). In dry run, it logs what
    it WOULD do but places no real orders. Flip DRY_RUN = False only after
    you've reviewed backtest results and are comfortable.
  - API keys are read from environment variables, never hardcoded:
        MEXC_API_KEY, MEXC_API_SECRET
  - A daily loss limit and max-drawdown halt are enforced automatically
    (see config.py: DAILY_LOSS_LIMIT_PCT, MAX_DRAWDOWN_LIMIT_PCT).
  - Ctrl+C stops the bot cleanly; it does NOT auto-close open positions —
    check MEXC directly after a manual stop.

Usage:
    export MEXC_API_KEY="..."
    export MEXC_API_SECRET="..."
    python live_bot.py
"""
import os
import sys
import time
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
import uuid

# --- Observation-only Asset Selection research layer ---
RESEARCH_ROOT = Path(os.environ.get(
    "ASSET_SELECTION_RESEARCH_ROOT",
    str(Path(__file__).resolve().parent.parent / "assetselectionexperiment-v1" / "asset_selection_experiment_v1")
))
if str(RESEARCH_ROOT) not in sys.path:
    sys.path.insert(0, str(RESEARCH_ROOT))
try:
    from asset_selection_recorder import ResearchRecorder
except Exception as _research_import_error:
    ResearchRecorder = None
    _research_import_error = str(_research_import_error)

import config as cfg
from core import data_fetcher as dfetch
from core import strategy as strat
from core.risk_manager import calc_position_plan, CircuitBreaker
from core.trade_logger import TradeLogger
from notifier import TelegramNotifier
from dashboard_server import DashboardServer

os.makedirs(cfg.LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(cfg.LOG_DIR, "bot.log")),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("live_bot")


class OpenPosition:
    def __init__(self, symbol, side, entry_price, stop_price, quantity, leverage, risk_amount):
        self.symbol = symbol
        self.side = side
        self.entry_price = entry_price
        self.stop_price = stop_price
        self.initial_stop_price = stop_price
        self.quantity = quantity
        self.remaining_qty = quantity
        self.leverage = leverage
        self.risk_amount = risk_amount
        self.partial_taken = False
        self.opened_at = datetime.now(timezone.utc)
        self.candles_open = 0


class LiveBot:
    def __init__(self):
        api_key = os.environ.get("MEXC_API_KEY", "")
        api_secret = os.environ.get("MEXC_API_SECRET", "")
        if not cfg.DRY_RUN and (not api_key or not api_secret):
            raise RuntimeError(
                "DRY_RUN is False but MEXC_API_KEY/MEXC_API_SECRET env vars are not set. "
                "Refusing to start live trading without credentials."
            )
        self.exchange = dfetch.build_exchange(api_key, api_secret)
        self.open_positions: dict[str, OpenPosition] = {}
        self.equity = None
        self.breaker = None

        self.notifier = TelegramNotifier()
        self.trade_logger = TradeLogger()
        self.dashboard = DashboardServer(self.trade_logger, bot_ref=self)
        self.dashboard.start()
        self._last_summary_date = None

        # Research is observation-only and can be disabled with ASSET_RESEARCH_ENABLED=0.
        self.research_enabled = os.environ.get("ASSET_RESEARCH_ENABLED", "1") != "0"
        self.research = None
        if self.research_enabled:
            if ResearchRecorder is None:
                log.warning(f"Asset Selection research unavailable: {_research_import_error}")
                self.research_enabled = False
            else:
                research_out = os.environ.get(
                    "ASSET_SELECTION_EVENTS",
                    str(RESEARCH_ROOT / "research" / "asset_selection" / "asset_events.csv")
                )
                self.research = ResearchRecorder(research_out)
                log.info(f"Asset Selection research enabled -> {research_out}")

    # -- account state -------------------------------------------------
    def _settle_pnl(self, pnl: float) -> float:
        """Apply realized PnL to equity. In DRY_RUN this updates the simulated
        equity directly (since there's no real account to re-query); live mode
        re-fetches the real balance instead."""
        if cfg.DRY_RUN:
            self.equity = (self.equity if self.equity is not None else cfg.BACKTEST_STARTING_EQUITY) + pnl
            return self.equity
        return self.fetch_equity()

    def fetch_equity(self) -> float:
        if cfg.DRY_RUN:
            # In dry run there's no real account to query — track a simulated
            # equity value instead, seeded from config for realistic sizing math.
            if self.equity is None:
                self.equity = cfg.BACKTEST_STARTING_EQUITY
            return self.equity
        balance = self.exchange.fetch_balance()
        usdt = balance.get("USDT", {})
        return float(usdt.get("total", 0))

    # -- order placement -------------------------------------------------
    def place_entry_order(self, symbol, side, quantity, leverage):
        if cfg.DRY_RUN:
            log.info(f"[DRY RUN] Would place {side.upper()} entry: {symbol} "
                      f"qty={quantity:.6f} leverage={leverage}x")
            return {"id": "dry-run", "status": "simulated"}

        order_side = "buy" if side == "long" else "sell"
        try:
            if cfg.MARKET_TYPE == "swap":
                self.exchange.set_leverage(leverage, symbol)
            order = self.exchange.create_order(symbol, "market", order_side, quantity)
            log.info(f"LIVE order placed: {order}")
            return order
        except Exception as e:
            log.error(f"Order placement FAILED for {symbol}: {e}")
            return None

    def place_exit_order(self, symbol, side, quantity):
        if cfg.DRY_RUN:
            log.info(f"[DRY RUN] Would place EXIT for {symbol}: qty={quantity:.6f}")
            return {"id": "dry-run", "status": "simulated"}

        order_side = "sell" if side == "long" else "buy"
        try:
            order = self.exchange.create_order(symbol, "market", order_side, quantity, params={"reduceOnly": True})
            log.info(f"LIVE exit order placed: {order}")
            return order
        except Exception as e:
            log.error(f"Exit order FAILED for {symbol}: {e}")
            return None

    # -- core scan loop -------------------------------------------------
    def scan_and_trade(self):
        equity = self.fetch_equity()
        today_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if self.breaker is None:
            self.breaker = CircuitBreaker(equity)
        self.breaker.update(equity, today_key)

        can_trade, reason = self.breaker.can_open_new_trades(equity)
        if not can_trade:
            log.warning(f"New trades blocked: {reason}")
            if self.breaker.halted:
                log.error("Bot HALTED due to max drawdown. Exiting scan loop.")
                self.notifier.notify_halt(reason)
                return False

        self._maybe_send_daily_summary(today_key)

        universe = dfetch.get_candidate_universe(self.exchange)
        log.info(f"Scanning {len(universe)} candidate pairs (equity=${equity:.2f})...")

        for candidate in universe:
            symbol = candidate["symbol"]
            try:
                self._process_symbol(symbol, candidate, equity, can_trade)
            except Exception as e:
                log.error(f"Error processing {symbol}: {e}")
            time.sleep(self.exchange.rateLimit / 1000)

        return True

    def _maybe_send_daily_summary(self, today_key: str):
        if not cfg.TELEGRAM_NOTIFY_DAILY_SUMMARY:
            return
        now_hour = datetime.now(timezone.utc).hour
        if self._last_summary_date != today_key and now_hour >= cfg.DAILY_SUMMARY_HOUR_UTC:
            stats = self.trade_logger.get_stats()
            self.notifier.notify_daily_summary(stats, len(self.open_positions))
            self._last_summary_date = today_key

    def _process_symbol(self, symbol, candidate, equity, can_open_new):
        df = dfetch.fetch_ohlcv_df(self.exchange, symbol)
        if df.empty or len(df) < cfg.CANDLES_LOOKBACK * 0.8:
            return
        df = strat.add_indicators(df)

        # -- manage existing position on this symbol --
        if symbol in self.open_positions:
            self._manage_position(symbol, df)
            return

        if not can_open_new:
            return
        if len(self.open_positions) >= cfg.MAX_CONCURRENT_POSITIONS:
            return
        # Record every candidate before the existing universe filter. This is research only.
        if self.research_enabled and self.research:
            try:
                self.research.record_candidate(
                    symbol=symbol,
                    timestamp=datetime.now(timezone.utc),
                    price=float(df["close"].iloc[-1]),
                    metadata={
                        "quote_volume_24h": candidate.get("quote_volume_24h", ""),
                        "listing_age_days": candidate.get("listing_age_days", ""),
                        "passed_universe_filter": bool(strat.passes_universe_filter(
                            df, candidate["quote_volume_24h"], candidate["listing_age_days"]
                        )),
                    }
                )
            except Exception as e:
                log.warning(f"Asset research candidate record failed for {symbol}: {e}")

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

        research_signal_id = None
        research_position_id = None
        if self.research_enabled and self.research:
            try:
                research_signal_id = f"signal_{uuid.uuid4().hex[:16]}"
                research_position_id = f"position_{uuid.uuid4().hex[:16]}"
                self.research.record_signal(
                    symbol=symbol,
                    timestamp=datetime.now(timezone.utc),
                    side=sig["signal"],
                    price=float(price),
                    quantity=float(plan.quantity),
                    risk_usd=float(plan.risk_amount_usdt),
                    leverage=float(plan.leverage),
                    reason=sig["reason"],
                    signal_id=research_signal_id,
                    position_id=research_position_id,
                    metadata={
                        "timeframe": cfg.TIMEFRAME,
                        "market_type": cfg.MARKET_TYPE,
                        "quote_volume_24h": candidate.get("quote_volume_24h", ""),
                        "listing_age_days": candidate.get("listing_age_days", ""),
                        "atr": float(sig.get("atr", 0) or 0),
                    }
                )
            except Exception as e:
                log.warning(f"Asset research signal record failed for {symbol}: {e}")

        log.info(f"SIGNAL: {symbol} {sig['signal']} @ {price:.6f} "
                 f"(reason: {sig['reason']}, leverage={plan.leverage}x, "
                 f"risk=${plan.risk_amount_usdt:.2f})")

        order = self.place_entry_order(symbol, sig["signal"], plan.quantity, plan.leverage)
        if order is not None:
            self.open_positions[symbol] = OpenPosition(
                symbol, sig["signal"], price, plan.stop_price,
                plan.quantity, plan.leverage, plan.risk_amount_usdt,
            )
            if research_position_id:
                self.open_positions[symbol].research_position_id = research_position_id
                self.open_positions[symbol].research_signal_id = research_signal_id
            self.trade_logger.log_event(symbol, "entry", equity, side=sig["signal"],
                                         price=price, quantity=plan.quantity,
                                         leverage=plan.leverage)
            if cfg.TELEGRAM_NOTIFY_ENTRIES_EXITS:
                self.notifier.notify_entry(symbol, sig["signal"], price, plan.leverage,
                                            plan.risk_amount_usdt)

    def _manage_position(self, symbol, df):
        pos = self.open_positions[symbol]
        pos.candles_open += 1
        row = df.iloc[-1]
        price = row["close"]

        hit_stop = (row["low"] <= pos.stop_price) if pos.side == "long" \
            else (row["high"] >= pos.stop_price)

        if hit_stop:
            log.info(f"STOP hit for {symbol} @ {pos.stop_price:.6f}")
            self.place_exit_order(symbol, pos.side, pos.remaining_qty)
            pnl = (pos.stop_price - pos.entry_price) * pos.remaining_qty if pos.side == "long" \
                else (pos.entry_price - pos.stop_price) * pos.remaining_qty
            new_equity = self._settle_pnl(pnl)
            self.trade_logger.log_event(symbol, "exit_stop", new_equity, side=pos.side,
                                         price=pos.stop_price, quantity=pos.remaining_qty,
                                         leverage=pos.leverage, pnl=pnl)
            if self.research_enabled and self.research and getattr(pos, "research_position_id", None):
                try:
                    self.research.record_exit(pos.research_position_id, symbol, pos.side, float(pos.stop_price), float(pnl), metadata={"exit_type":"exit_stop", "equity":new_equity})
                except Exception as e:
                    log.warning(f"Asset research exit record failed for {symbol}: {e}")
            if cfg.TELEGRAM_NOTIFY_ENTRIES_EXITS:
                self.notifier.notify_exit(symbol, "exit_stop", pos.stop_price, pnl, new_equity)
            del self.open_positions[symbol]
            return

        risk_per_unit = abs(pos.entry_price - pos.initial_stop_price)
        move = (price - pos.entry_price) if pos.side == "long" else (pos.entry_price - price)
        r_mult = move / risk_per_unit if risk_per_unit else 0

        if not pos.partial_taken and r_mult >= cfg.TAKE_PROFIT_R_MULT_PARTIAL:
            partial_qty = pos.quantity * cfg.PARTIAL_CLOSE_PCT
            log.info(f"PARTIAL TP for {symbol} @ {price:.6f} (R={r_mult:.2f})")
            self.place_exit_order(symbol, pos.side, partial_qty)
            pnl = (price - pos.entry_price) * partial_qty if pos.side == "long" \
                else (pos.entry_price - price) * partial_qty
            new_equity = self._settle_pnl(pnl)
            self.trade_logger.log_event(symbol, "partial_tp", new_equity, side=pos.side,
                                         price=price, quantity=partial_qty,
                                         leverage=pos.leverage, pnl=pnl)
            if self.research_enabled and self.research and getattr(pos, "research_position_id", None):
                try:
                    self.research.record_exit(pos.research_position_id, symbol, pos.side, float(price), float(pnl), metadata={"exit_type":"partial_tp", "equity":new_equity, "quantity":partial_qty})
                except Exception as e:
                    log.warning(f"Asset research partial exit record failed for {symbol}: {e}")
            if cfg.TELEGRAM_NOTIFY_ENTRIES_EXITS:
                self.notifier.notify_exit(symbol, "partial_tp", price, pnl, new_equity)
            pos.remaining_qty -= partial_qty
            pos.partial_taken = True
            pos.stop_price = pos.entry_price  # move to breakeven

        if pos.partial_taken:
            trail_dist = row["atr"] * cfg.TRAIL_ATR_MULT
            if pos.side == "long":
                pos.stop_price = max(pos.stop_price, price - trail_dist)
            else:
                pos.stop_price = min(pos.stop_price, price + trail_dist)

        if pos.candles_open >= cfg.TIME_STOP_CANDLES and r_mult <= 0:
            log.info(f"TIME STOP for {symbol} @ {price:.6f}")
            self.place_exit_order(symbol, pos.side, pos.remaining_qty)
            pnl = (price - pos.entry_price) * pos.remaining_qty if pos.side == "long" \
                else (pos.entry_price - price) * pos.remaining_qty
            new_equity = self._settle_pnl(pnl)
            self.trade_logger.log_event(symbol, "exit_time_stop", new_equity, side=pos.side,
                                         price=price, quantity=pos.remaining_qty,
                                         leverage=pos.leverage, pnl=pnl)
            if self.research_enabled and self.research and getattr(pos, "research_position_id", None):
                try:
                    self.research.record_exit(pos.research_position_id, symbol, pos.side, float(price), float(pnl), metadata={"exit_type":"exit_time_stop", "equity":new_equity})
                except Exception as e:
                    log.warning(f"Asset research exit record failed for {symbol}: {e}")
            if cfg.TELEGRAM_NOTIFY_ENTRIES_EXITS:
                self.notifier.notify_exit(symbol, "exit_time_stop", price, pnl, new_equity)
            del self.open_positions[symbol]

    def run_forever(self):
        log.info(f"Bot starting. DRY_RUN={cfg.DRY_RUN}, MARKET_TYPE={cfg.MARKET_TYPE}, "
                 f"TIMEFRAME={cfg.TIMEFRAME}, SCAN_INTERVAL={cfg.SCAN_INTERVAL_SECONDS}s")
        if cfg.DRY_RUN:
            log.warning("Running in DRY_RUN mode — no real orders will be placed.")
        while True:
            try:
                keep_going = self.scan_and_trade()
                if not keep_going:
                    break
            except KeyboardInterrupt:
                log.info("Shutdown requested by user. Exiting.")
                break
            except Exception as e:
                log.error(f"Unhandled error in scan loop: {e}")
            time.sleep(cfg.SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    bot = LiveBot()
    bot.run_forever()
