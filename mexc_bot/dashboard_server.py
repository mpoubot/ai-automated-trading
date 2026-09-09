"""
Local-only dashboard server. Serves dashboard.html and a JSON data endpoint
at http://localhost:<port>/api/data so the dashboard can auto-refresh live
in your browser without hitting file:// CORS restrictions.

Runs in a background thread inside live_bot.py — no separate process needed.
"""
import json
import threading
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config as cfg

log = logging.getLogger("dashboard")


class DashboardHandler(BaseHTTPRequestHandler):
    trade_logger = None
    bot_ref = None  # set by DashboardServer to access live open_positions / breaker state

    def log_message(self, format, *args):
        pass  # silence default request logging (keeps bot.log readable)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._serve_file("dashboard.html", "text/html")
        elif self.path == "/api/data":
            self._serve_json()
        else:
            self.send_response(404)
            self.end_headers()

    def _serve_file(self, filename, content_type):
        try:
            with open(filename, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()

    def _serve_json(self):
        stats = self.trade_logger.get_stats() if self.trade_logger else {}
        recent = self.trade_logger.get_recent_events(50) if self.trade_logger else []
        curve = self.trade_logger.get_equity_curve() if self.trade_logger else []

        open_positions = []
        halted = False
        halt_reason = ""
        if self.bot_ref is not None:
            open_positions = [
                {"symbol": p.symbol, "side": p.side, "entry_price": p.entry_price,
                 "stop_price": p.stop_price, "leverage": p.leverage}
                for p in self.bot_ref.open_positions.values()
            ]
            if self.bot_ref.breaker is not None:
                halted = self.bot_ref.breaker.halted
                halt_reason = self.bot_ref.breaker.halt_reason

        payload = {
            "stats": stats,
            "recent_events": recent,
            "equity_curve": curve,
            "open_positions": open_positions,
            "halted": halted,
            "halt_reason": halt_reason,
            "dry_run": cfg.DRY_RUN,
        }
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)


class DashboardServer:
    def __init__(self, trade_logger, bot_ref=None, port: int = None):
        self.port = port or cfg.DASHBOARD_PORT
        DashboardHandler.trade_logger = trade_logger
        DashboardHandler.bot_ref = bot_ref
        self.httpd = ThreadingHTTPServer(("localhost", self.port), DashboardHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        log.info(f"Dashboard running at http://localhost:{self.port}")

    def stop(self):
        self.httpd.shutdown()
