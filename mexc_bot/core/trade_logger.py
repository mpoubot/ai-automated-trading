"""
Trade logging: appends every trade event to a CSV (Excel-friendly) and keeps
an in-memory record the dashboard server reads from directly (no disk re-read
needed on every request, though the CSV remains the durable source of truth).
"""
import os
import csv
import threading
from datetime import datetime, timezone

import config as cfg

CSV_FIELDS = ["timestamp", "symbol", "event_type", "side", "price",
              "quantity", "leverage", "pnl", "equity"]


class TradeLogger:
    def __init__(self, csv_path: str = None, starting_equity: float = None):
        self.csv_path = csv_path or cfg.TRADES_CSV_PATH
        self.lock = threading.Lock()
        self._ensure_csv()
        self.events = self._load_existing()
        self.starting_equity = starting_equity or cfg.BACKTEST_STARTING_EQUITY
        self.peak_equity = self.starting_equity
        self.current_equity = self.events[-1]["equity"] if self.events else self.starting_equity
        if self.events:
            self.peak_equity = max(e["equity"] for e in self.events + [{"equity": self.starting_equity}])

    def _ensure_csv(self):
        os.makedirs(os.path.dirname(self.csv_path) or ".", exist_ok=True)
        if not os.path.exists(self.csv_path):
            with open(self.csv_path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
                writer.writeheader()

    def _load_existing(self):
        events = []
        if os.path.exists(self.csv_path):
            with open(self.csv_path, "r", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    row["price"] = float(row["price"]) if row["price"] else None
                    row["quantity"] = float(row["quantity"]) if row["quantity"] else None
                    row["leverage"] = float(row["leverage"]) if row["leverage"] else None
                    row["pnl"] = float(row["pnl"]) if row["pnl"] else None
                    row["equity"] = float(row["equity"]) if row["equity"] else None
                    events.append(row)
        return events

    def log_event(self, symbol, event_type, equity, side=None, price=None,
                  quantity=None, leverage=None, pnl=None):
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "event_type": event_type,
            "side": side,
            "price": price,
            "quantity": quantity,
            "leverage": leverage,
            "pnl": pnl,
            "equity": equity,
        }
        with self.lock:
            with open(self.csv_path, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
                writer.writerow(row)
            self.events.append(row)
            self.current_equity = equity
            if equity > self.peak_equity:
                self.peak_equity = equity

    def get_stats(self):
        """
        Per-TRADE stats (entry through final exit), NOT per-event.

        Counting each partial-take-profit as its own "win" inflates the win
        rate badly — a trade that takes partial profit then stops out at
        breakeven is NOT a win, but naive event counting scores it as one.
        This delegates to the same trade_metrics module the backtester and
        walk-forward use, so the dashboard and the validation tools always
        report the same number for the same data.
        """
        import pandas as pd
        from core import trade_metrics

        drawdown_pct = ((self.peak_equity - self.current_equity) / self.peak_equity * 100) \
            if self.peak_equity else 0
        total_return_pct = ((self.current_equity - self.starting_equity) / self.starting_equity * 100) \
            if self.starting_equity else 0

        base = {
            "current_equity": round(self.current_equity, 2),
            "starting_equity": round(self.starting_equity, 2),
            "peak_equity": round(self.peak_equity, 2),
            "total_return_pct": round(total_return_pct, 2),
            "drawdown_pct": round(drawdown_pct, 2),
        }

        if not self.events:
            base.update({"total_closed_trades": 0, "win_rate_pct": 0,
                         "avg_win": 0, "avg_loss": 0, "profit_factor": 0})
            return base

        ev = pd.DataFrame(self.events).rename(columns={"event_type": "type"})
        m = trade_metrics.compute_metrics(ev, self.starting_equity)
        base.update({
            "total_closed_trades": m["num_trades"],
            "win_rate_pct": m["win_rate_pct"],
            "avg_win": m["avg_win"],
            "avg_loss": m["avg_loss"],
            "profit_factor": m["profit_factor"],
        })
        return base

    def get_recent_events(self, limit=50):
        return list(reversed(self.events[-limit:]))

    def get_equity_curve(self):
        return [{"timestamp": e["timestamp"], "equity": e["equity"]} for e in self.events]
