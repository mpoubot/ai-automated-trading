"""
Telegram notifier — sends trade entry/exit and daily summary messages.

Setup (also documented in README):
  1. Message @BotFather on Telegram, send /newbot, follow prompts -> get a token
  2. Message your new bot at least once (so it can message you back)
  3. Get your chat ID: visit https://api.telegram.org/bot<TOKEN>/getUpdates
     after messaging the bot, and read "chat":{"id": ...} from the response
  4. export TELEGRAM_BOT_TOKEN="..." and export TELEGRAM_CHAT_ID="..."

If these env vars aren't set, the notifier silently no-ops (bot still runs,
just without Telegram messages) rather than crashing the whole bot.
"""
import os
import logging
import requests

log = logging.getLogger("notifier")


class TelegramNotifier:
    def __init__(self):
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        self.enabled = bool(self.token and self.chat_id)
        if not self.enabled:
            log.warning("Telegram not configured (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID "
                        "env vars missing) — notifications disabled.")

    def send(self, text: str):
        if not self.enabled:
            return
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            resp = requests.post(url, data={"chat_id": self.chat_id, "text": text,
                                              "parse_mode": "HTML"}, timeout=10)
            if resp.status_code != 200:
                log.error(f"Telegram send failed: {resp.status_code} {resp.text}")
        except Exception as e:
            log.error(f"Telegram send error: {e}")

    def notify_entry(self, symbol, side, price, leverage, risk_amount):
        self.send(f"🟢 <b>ENTRY</b> {symbol}\nSide: {side.upper()}\nPrice: {price:.6f}\n"
                   f"Leverage: {leverage}x\nRisk: ${risk_amount:.2f}")

    def notify_exit(self, symbol, event_type, price, pnl, equity):
        emoji = "✅" if pnl and pnl > 0 else "🔴"
        label = {"exit_stop": "STOP", "exit_time_stop": "TIME STOP",
                  "partial_tp": "PARTIAL TP"}.get(event_type, event_type)
        self.send(f"{emoji} <b>{label}</b> {symbol}\nPrice: {price:.6f}\n"
                   f"PnL: ${pnl:.2f}\nEquity: ${equity:.2f}")

    def notify_daily_summary(self, stats: dict, open_positions_count: int):
        self.send(
            "📊 <b>Daily Summary</b>\n"
            f"Equity: ${stats['current_equity']:.2f} "
            f"({stats['total_return_pct']:+.2f}% all-time)\n"
            f"Drawdown from peak: {stats['drawdown_pct']:.2f}%\n"
            f"Closed trades: {stats['total_closed_trades']} "
            f"(win rate {stats['win_rate_pct']:.1f}%)\n"
            f"Open positions: {open_positions_count}"
        )

    def notify_halt(self, reason: str):
        self.send(f"🛑 <b>BOT HALTED</b>\n{reason}")

    def notify_error(self, message: str):
        self.send(f"⚠️ <b>Error</b>\n{message}")
