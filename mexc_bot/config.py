"""
Central configuration for the strategy, risk management, and bot behavior.
Tune these values here rather than digging through the code.
"""

# ---------------------------------------------------------------------------
# EXCHANGE / API
# ---------------------------------------------------------------------------
EXCHANGE_ID = "mexc"
API_KEY = ""       # fill in via environment variable instead (see README) — never hardcode
API_SECRET = ""    # fill in via environment variable instead (see README) — never hardcode
MARKET_TYPE = "swap"   # "spot" or "swap" (futures). Bot can run both universes separately.

# ---------------------------------------------------------------------------
# UNIVERSE FILTER
# ---------------------------------------------------------------------------
MIN_24H_VOLUME_USDT = 5_000_000     # excludes illiquid pairs / slippage traps
MIN_LISTING_AGE_DAYS = 30           # excludes brand-new erratic listings
ATR_PCT_MIN = 0.015                 # exclude near-dead pairs (ATR < 1.5% of price)
ATR_PCT_MAX = 0.15                  # exclude extreme outliers (ATR > 15% of price)
QUOTE_ASSET = "USDT"
MAX_PAIRS_SCANNED = 60              # cap universe size to keep scan cycle time bounded

# ---------------------------------------------------------------------------
# TIMEFRAME
# ---------------------------------------------------------------------------
TIMEFRAME = "1h"                    # candle timeframe for signals ("1h" or "4h" recommended)
CANDLES_LOOKBACK = 300              # how many candles to pull per pair per scan

# ---------------------------------------------------------------------------
# ENTRY LOGIC
# ---------------------------------------------------------------------------
EMA_FAST = 20
EMA_MID = 50
EMA_SLOW = 200
BREAKOUT_LOOKBACK = 20              # N-candle high/low breakout window
RSI_PERIOD = 14
RSI_LONG_MIN = 50
RSI_LONG_MAX = 70
RSI_SHORT_MIN = 30
RSI_SHORT_MAX = 50
VOLUME_LOOKBACK = 20
VOLUME_CONFIRM_MULT = 1.0           # breakout candle volume must exceed avg * this
ALLOW_SHORTS = True                 # only meaningful on futures/swap market

# ---------------------------------------------------------------------------
# STRATEGY SELECTION
# ---------------------------------------------------------------------------
# "momentum" = original: EMA trend filter + N-candle breakout + volume + RSI
# "macd"     = MACD crossover: long on golden cross, short on death cross
# Both share the SAME ATR-based risk management (sizing, stops, partial TP,
# trailing, circuit breakers) so they can be compared on equal footing —
# only the entry signal differs.
STRATEGY_MODE = "momentum"

MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 8                     # note: 9 is the more common default; 8 per spec tested

# ---------------------------------------------------------------------------
# REGIME FILTER (ADX) — added after walk-forward showed no edge without it
# ---------------------------------------------------------------------------
# ADX measures trend STRENGTH, not direction. Breakout entries fail most often
# in choppy/directionless conditions (false breakout, immediate reversal), so
# requiring a minimum ADX is a direct attempt to sit those periods out.
# Keep this toggleable: comparing filter-on vs filter-off in walk_forward.py
# is the only honest way to know whether it actually helps.
USE_ADX_FILTER = True
ADX_PERIOD = 14
ADX_MIN_THRESHOLD = 25              # typical "trending" threshold is 20-25

# ---------------------------------------------------------------------------
# RISK MANAGEMENT (the core safety layer)
# ---------------------------------------------------------------------------
# Values below were set from the 360-day, 144-combination parameter sweep
# (July 2026) across 11 pairs: the ATR_STOP_MULT=1.0 + TP=3.0R region was
# the only robustly-positive cluster (8 of 9 neighboring variants positive,
# avg ~+2.4%/360d, profit factor 1.01-1.06). Notes that matter:
#   - This edge is THIN. Funding rates aren't modeled in the backtests and
#     could erase it. Treat as "best known settings," not "proven profitable."
#   - Expected win rate at these settings is ~22% — long losing streaks are
#     normal and expected; the math works via avg win >> avg loss (~4-5x).
RISK_PER_TRADE_PCT = 0.02           # 2% of account equity risked per trade
ATR_STOP_MULT = 1.0                 # stop-loss distance = ATR * this (sweep: tight stops best)
MAX_LEVERAGE = 5                    # hard cap regardless of what position sizing implies
MIN_LEVERAGE = 1
MAX_CONCURRENT_POSITIONS = 5
TAKE_PROFIT_R_MULT_PARTIAL = 3.0    # take partial profit at this multiple of risk (R) (sweep: later TP best)
PARTIAL_CLOSE_PCT = 0.5             # close this fraction of position at TP1
TRAIL_ATR_MULT = 1.5                # ATR-based trailing stop for remaining position (sweep: tighter trail best)
TIME_STOP_CANDLES = 24              # close trade if not favorable within N candles

# ---------------------------------------------------------------------------
# CIRCUIT BREAKERS
# ---------------------------------------------------------------------------
DAILY_LOSS_LIMIT_PCT = 0.08         # stop opening new trades after -8% equity in a day
MAX_DRAWDOWN_LIMIT_PCT = 0.20       # halt bot entirely after -20% from equity peak

# ---------------------------------------------------------------------------
# BACKTEST DEFAULTS
# ---------------------------------------------------------------------------
BACKTEST_STARTING_EQUITY = 500.0
BACKTEST_TAKER_FEE_PCT = 0.0005     # MEXC futures taker fee approx — verify current rate
BACKTEST_SLIPPAGE_PCT = 0.0005      # assumed slippage per fill

# --- Funding costs (perpetual futures) ---
# Perps charge/pay funding every ~8h to hold a position. Longs usually pay
# shorts when funding is positive (the common case in crypto). Ignoring this
# flatters every backtest — a strategy with a thin edge can be entirely wiped
# out by funding drag. Enabled by default; set False only to compare against
# older no-funding results.
MODEL_FUNDING_COSTS = True
FUNDING_INTERVAL_HOURS = 8
# Fallback rate used when real historical funding data isn't available for a
# symbol. 0.01% per 8h = ~0.03%/day = ~11%/yr — the common "neutral" default.
FUNDING_RATE_FALLBACK = 0.0001
USE_REAL_FUNDING_HISTORY = True     # try fetching actual rates from the exchange first

# ---------------------------------------------------------------------------
# LIVE BOT
# ---------------------------------------------------------------------------
SCAN_INTERVAL_SECONDS = 60 * 15     # how often to re-scan universe & check signals
DRY_RUN = True                      # True = simulate orders only, no real API calls placed
LOG_DIR = "logs"

# ---------------------------------------------------------------------------
# TELEGRAM NOTIFICATIONS
# ---------------------------------------------------------------------------
# Token/chat ID are read from env vars TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
# (see README for setup steps) — never hardcode them here.
TELEGRAM_NOTIFY_ENTRIES_EXITS = True
TELEGRAM_NOTIFY_DAILY_SUMMARY = True
DAILY_SUMMARY_HOUR_UTC = 8          # hour (0-23, UTC) the daily summary is sent

# ---------------------------------------------------------------------------
# PERFORMANCE TRACKING
# ---------------------------------------------------------------------------
TRADES_CSV_PATH = "logs/trades.csv"
DASHBOARD_PORT = 8787               # dashboard at http://localhost:8787
