# MEXC Momentum Bot

A rule-based, volatility-adjusted momentum strategy for MEXC spot and futures,
with automated position sizing, leverage capping, and circuit breakers.

**Read the whole "Before you go live" section before touching real money.**

---

## What this is

- `config.py` — every tunable parameter (strategy, risk, universe filter, circuit breakers)
- `core/indicators.py` — EMA, RSI, ATR calculations
- `core/strategy.py` — entry/exit signal logic
- `core/risk_manager.py` — position sizing, leverage derivation, daily-loss/drawdown halts
- `core/data_fetcher.py` — MEXC market data via [ccxt](https://github.com/ccxt/ccxt)
- `backtester.py` — replays historical data through the exact same signal/risk logic
- `live_bot.py` — the 24/7 scanning + execution loop

## Strategy summary

Long: uptrend (EMA20>EMA50>EMA200) + breakout above 20-candle high + volume
confirmation + RSI 50-70. Shorts mirror this for downtrends (futures only).
Position size and leverage are *derived* from each pair's own ATR (volatility)
and a fixed 2% risk-per-trade — leverage is capped at 5x regardless of what
the math implies for very volatile pairs. Stops are ATR-based; partial profit
taken at 1.5R, remainder trailed; daily loss limit and max-drawdown halt are
enforced automatically. Full parameter list and rationale in `config.py` comments.

## Setup

```bash
pip install -r requirements.txt --break-system-packages   # or use a venv
```

Set your API keys as environment variables — **never put them in config.py
or any file you might commit/share**:

```bash
export MEXC_API_KEY="your_key_here"
export MEXC_API_SECRET="your_secret_here"
```

Your MEXC API key should have **trade permissions only, withdrawal disabled**
— you mentioned you've already set this up, which is the right call.

## Telegram notifications (optional but recommended)

You'll get a message on: trade entries, trade exits (stop/partial TP/time-stop),
and a once-daily performance summary (sent at `DAILY_SUMMARY_HOUR_UTC` in
`config.py`, default 08:00 UTC).

**Setup:**
1. Open Telegram, search for **@BotFather**, start a chat, send `/newbot`
2. Follow the prompts (give it a name and a username) — BotFather replies
   with a token that looks like `123456789:AAExampleTokenTextHere`
3. Search for the bot you just created (by the username you gave it) and
   send it any message, e.g. "hi" — this lets it message you back
4. In your browser, visit (replacing `<TOKEN>` with your real token):
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
5. In the JSON response, find `"chat":{"id": 123456789, ...}` — that number
   is your chat ID
6. Set both as environment variables before running the bot:
   ```bash
   export TELEGRAM_BOT_TOKEN="123456789:AAExampleTokenTextHere"
   export TELEGRAM_CHAT_ID="123456789"
   ```

If these aren't set, the bot runs completely normally — it just skips
sending Telegram messages (logged as a warning on startup, not an error).

## Performance dashboard

Every trade event (entry, partial TP, stop, time-stop) is written to
`logs/trades.csv` as it happens — open this directly in Excel/Sheets any time.

For a live visual view, `live_bot.py` also starts a small local web server
automatically. While the bot is running, open in your browser:

```
http://localhost:8787
```

This shows your equity curve, win rate, drawdown from peak, open positions,
and recent trade events — it re-fetches data every 10 seconds on its own, so
just leave the tab open. It only works while `live_bot.py` is actively
running (it's served from the bot process itself, not a separate always-on
service). Change the port via `DASHBOARD_PORT` in `config.py` if 8787 is
already in use on your machine.

## Step 1 — Backtest before anything else

For a quick sanity check:
```bash
python backtester.py --symbols "BTC/USDT:USDT" "ETH/USDT:USDT" --days 90
```

**For a real read on the strategy, use the broader tools below instead** — 2
symbols over 90 days is too small a sample to draw conclusions from.

### Broad multi-pair backtest

```bash
python run_broad_backtest.py --days 180
```

Runs the same strategy/risk logic across a curated set of ~18 pairs (majors
+ higher-volatility alts, closer to what the live scanner actually catches)
over a longer window. **Each pair is tested with its own independent
starting capital and its own circuit breaker** — a bad early result on one
pair can't silently zero out the test for every pair after it. If a pair's
own circuit breaker halts (its own 20% max-drawdown limit), that's reported
clearly in the output and only affects that one pair's results. Reports
**true per-trade metrics** — win rate, expectancy, profit factor — not the
inflated per-event count you'd get from naively counting partial-take-profits
as separate wins. Also prints a per-symbol breakdown so you can see which
pairs the strategy actually works on. Override the pair list with
`--symbols "SOL/USDT:USDT" "DOGE/USDT:USDT" ...` if you want to test something specific.

### Parameter sweep

```bash
python param_sweep.py --days 180            # full grid (slower, more thorough)
python param_sweep.py --days 180 --quick     # smaller grid (faster)
```

Fetches historical data **once**, then tests many combinations of the
parameters that shape risk:reward (stop distance, partial take-profit
level, trailing stop tightness, RSI entry band) against that same cached
data — no repeated network calls, so this stays fast even with a large grid.
**Each symbol gets its own independent starting capital and circuit breaker
within every combo tested** (same fix as the broad backtest — a bad symbol
can't silently zero out the rest of a combo's results). Ranks results by
expectancy-per-trade (only combos with 15+ trades are ranked, to avoid a
lucky small sample looking artificially good), and saves every combination
tested to `backtest_results/param_sweep_<timestamp>.csv` so you can inspect
the full picture, not just the top row.

### On the current default pair list

`run_broad_backtest.py`'s `DEFAULT_SYMBOLS` no longer includes SOL, NEAR,
LINK, INJ, APT, ARB, or OP — three independent real backtests (21-day,
~82-day, ~365-day windows) showed all seven losing money in *every* run,
not just an unlucky window. They're kept in `EXCLUDED_SYMBOLS_CONSISTENTLY_NEGATIVE`
in that same file if you want to re-test them later as the strategy evolves.

Worth being honest about the flip side too: **no pair was consistently
profitable across all three of those runs either** — even the better-looking
pairs (COTI, BTC, AVAX) flipped negative in at least one window. That's a
sign the entry logic itself may not have a robust edge yet, not just that
the pair list needed trimming — the parameter sweep may help, but there's a
real chance no combination of these particular parameters fixes that, and
the core strategy logic (e.g. adding a trend/chop regime filter) may need
rethinking at some point.

**Important:** the sweep changes `config.py` values only in-memory, for that
one script run — it does not edit your actual `config.py` file. If a
combination looks good, manually update the matching values in `config.py`
yourself.

### Walk-forward validation (the honest overfitting test)

```bash
python walk_forward.py --days 720 --train-days 180 --test-days 60          # full grid
python walk_forward.py --days 720 --train-days 180 --test-days 60 --quick  # faster
```

This is the strongest validation tool in the project. It splits history into
rolling train→test folds: parameters are tuned on each train window, then
applied to the *following* test window — data the tuning never saw. The
test-window ("out-of-sample") results are the honest measure of whether the
strategy has a real edge or has just memorized the past.

What to look for in the output:
- **Aggregate out-of-sample expectancy > 0 across most folds** → genuinely promising
- **Great train numbers but flat/negative test numbers** → overfitting exposed
- **Same parameters chosen fold after fold** → robust region (trustworthy);
  **different parameters every fold** → the "edge" is probably noise
- The train columns will *always* look good — that's the tuning working, not evidence

Run this after any significant strategy change, and before ever considering
going live with new settings. It's slower than a plain sweep (it runs the
full grid once per fold), so use `--quick` for iteration and the full grid
for final validation.

### Regime filter (ADX) + funding costs — what changed and why

The first full walk-forward run produced a clear negative verdict: aggregate
out-of-sample **profit factor 0.99, expectancy −$0.07/trade over 1,752 trades**,
with a train→test expectancy correlation of **0.081** — meaning parameter
tuning on past data had essentially *zero* predictive power for the next
period. That's the textbook signature of overfitting, not of a working edge.

Two changes were made in response:

1. **ADX regime filter** (`USE_ADX_FILTER`, `ADX_MIN_THRESHOLD` in config).
   ADX measures trend *strength*. Breakout entries fail most often in choppy,
   directionless markets — false breakout, immediate reversal. Requiring a
   minimum ADX is a direct attempt to sit those periods out. `ADX_MIN = 0`
   is included in the sweep grids so filter-off is always compared head-to-head
   against filter-on; if the filter genuinely helps, it has to prove it.
2. **Funding cost modeling** (`MODEL_FUNDING_COSTS`). Perpetual futures charge
   funding roughly every 8h to hold a position. Real historical rates are
   fetched per symbol where available, with a configurable fallback otherwise.
   Ignoring this flatters every backtest — a thin edge can be entirely
   consumed by funding drag. Results with this enabled will look *worse* than
   older runs, and that's the point: they're more honest.

Both are exercised by `param_sweep.py` and `walk_forward.py`, and both apply
identically in `live_bot.py`, so validated logic and live logic never diverge.

### Timeframe comparison

Both tools now accept `--timeframe`:

```bash
python walk_forward.py --days 720 --train-days 180 --test-days 60 --quick --timeframe 1h
python walk_forward.py --days 720 --train-days 180 --test-days 60 --quick --timeframe 4h
```

Chop is often as much a timeframe problem as a filter problem — a 1h breakout
that's noise may be a real move on 4h. Run both and compare.

### The pre-committed decision bar

`walk_forward.py` now prints an explicit PASS/FAIL against a bar set *before*
the results were seen:

> **Out-of-sample profit factor > 1.10 AND more than half of folds positive.**

This exists to prevent the most common failure mode in strategy development:
looking at a marginal result (profit factor 1.03, say) and rationalizing "one
more filter and it'll get there." A FAIL means this configuration has no
demonstrated edge. It does not mean it needs one more adjustment.

If both timeframes FAIL, the honest conclusion is that this strategy family
doesn't have a tradeable edge — and the correct response is to stop, not to
keep adding parameters until something fits the past.

### Testing a different entry signal (MACD crossover)

`config.py` has `STRATEGY_MODE`, either `"momentum"` (the original: EMA trend
+ breakout + volume + RSI) or `"macd"` (long on MACD golden cross, short on
death cross, MACD(12,26,8)). **Both share the exact same ATR-based risk
management** — position sizing, stops, partial take-profit, trailing, circuit
breakers — so any difference in results comes from the entry signal alone,
not from different risk handling.

Both `param_sweep.py` and `walk_forward.py` accept `--strategy`, and
`walk_forward.py` accepts `--wide` to test against the full 18-pair universe
rather than the 11-pair list that was filtered by the *momentum* strategy's
failures (that exclusion is strategy-specific and shouldn't bias a new signal):

```bash
python walk_forward.py --days 720 --train-days 180 --test-days 60 --quick \
    --strategy macd --wide --timeframe 1h
```

The same pre-committed bar applies: **out-of-sample profit factor > 1.10 and
more than half of folds positive.** A widely-known signal like MACD crossover
on liquid pairs has been tested by an enormous number of people; if it cleared
that bar easily it would be surprising. Run it and find out rather than
assuming either way — that's what the rig is for.

### A word on interpreting any of these results

- **"Win rate" alone is misleading** for this strategy — because of the
  partial-take-profit design, an event-level count (as opposed to per-trade)
  inflates it. Always look at expectancy-per-trade and profit factor instead.
- **Small samples lie.** 60 trades on 2 pairs over 90 days can look like a
  real edge or a real failure and be neither — it's just noise. Don't
  change your risk settings based on a small run; use the broad backtest
  and sweep tools above before drawing conclusions.
- **Past performance on historical data doesn't guarantee future results** —
  standard caveat, genuinely true here. Market regime shifts (trending vs.
  choppy) will change how this strategy performs regardless of parameters.


## Step 2 — Dry run the live bot

`config.py` has `DRY_RUN = True` by default. In this mode the bot scans
real MEXC market data, generates real signals, and logs exactly what it
*would* trade — but places no real orders. Run it like this for at least
a few days and read `logs/bot.log` before considering live trading:

```bash
python live_bot.py
```

## Step 3 — Go live (only when you're ready)

1. In `config.py`, set `DRY_RUN = False`
2. Double check `MAX_LEVERAGE`, `RISK_PER_TRADE_PCT`, and `MAX_CONCURRENT_POSITIONS`
   reflect what you actually want — these are the guardrails
3. Run `python live_bot.py` and keep it running (this needs your PC on and awake —
   disable sleep/hibernate, and consider a small uninterruptible power supply
   if outages are common where you are)
4. Check `logs/bot.log` regularly — this does not text/email you, it only logs

## Safety notes — please actually read this

- **This strategy is not a guaranteed edge.** It's a standard, transparent
  momentum framework. It will have losing streaks, and backtest results on
  90 days of data are not a promise of future results — market regime changes
  (e.g., trending vs. choppy) will affect it significantly.
- **5x+ leverage with a few hundred dollars of capital is genuinely high risk.**
  The risk manager caps leverage per-trade based on volatility, but it cannot
  protect against gaps, exchange outages, or your PC losing power/internet
  mid-trade with an open position.
- **Ctrl+C does not close open positions.** If you stop the bot manually,
  check MEXC directly for anything still open.
- **This code has not been live-tested with real capital.** Treat it as a
  starting point you refine, not a finished product — the honest next step
  after backtesting is running it in `DRY_RUN` for a while, then starting
  live with capital you're fully prepared to lose entirely.
- I'm not a financial advisor and this isn't financial advice — it's a
  tool implementing the rules you asked for. The risk is yours to own.

## Tuning ideas for later

- Add a webhook listener so TradingView Pine Script alerts can trigger
  entries too (useful if you want to combine your own TradingView analysis
  with this bot's execution)
- Add Telegram/Discord notifications so you don't have to tail logs manually
- Vectorized backtest for faster parameter sweeps across many symbols
