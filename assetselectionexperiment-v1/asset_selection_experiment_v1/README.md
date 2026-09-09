# Asset Selection & Executability Experiment V1

## Purpose

This is a **separate research experiment** for the MEXC Futures/Swap bot.

It does **not** change the strategy's entry rules, exits, CCI/RSI logic, stops, or universe.

The research question is:

> Does the current strategy perform differently when its signals are grouped by asset quality and real-world executability?

The experiment measures:

1. Market-cap rank / market-cap bucket
2. 24h liquidity and volume/market-cap
3. Bid/ask spread in basis points
4. Order-book depth at 5/10/25/50 bps
5. 5m / 1h / 24h volatility
6. Asset category: utility/fundamental vs speculative
7. Listing age where available
8. MEXC Futures API eligibility / Innovation Zone status where available
9. Repeated signals on the same symbol
10. Signal reversals
11. Thin-book / spread-expansion / price-spike warning flags
12. Estimated slippage for the intended position size
13. Estimated execution cost including fees
14. Theoretical vs estimated executable P/L

## Important research rule

V1 is observational first.

Do NOT add a Top-100/Top-200 filter to the live bot yet.

We first collect the evidence and then compare:

- current baseline
- market-cap buckets
- liquidity buckets
- spread buckets
- volatility buckets
- asset categories
- executability buckets

Only after sufficient observations should a filter become a separate experiment.

## Architecture

```text
CURRENT DRY-RUN BOT
       |
       | optional research hook
       v
asset_events.csv
       |
       +--> candidate snapshots
       +--> signal snapshots
       +--> detailed order book
       |
       v
asset_selection_analyzer.py
       |
       +--> asset_selection_summary.csv
       +--> asset_selection_bucket_results.csv
       +--> asset_selection_symbol_results.csv
       +--> asset_selection_trade_enrichment.csv
```

The current strategy remains untouched except for optional logging calls.

## Files

- `asset_selection_recorder.py`
  - thread-safe CSV recorder
  - creates `signal_id` and `position_id`
  - records candidate and signal observations

- `asset_selection_capture.py`
  - queries public MEXC Futures market data
  - optionally enriches with CoinGecko market-cap/category data
  - can snapshot one or more symbols

- `asset_selection_analyzer.py`
  - analyses captured research events
  - calculates market-cap/liquidity/spread/volatility buckets
  - detects repeated signals and reversals
  - estimates execution cost

- `config.json`
  - thresholds and API settings

- `live_bot_hook_example.py`
  - exact example of how to add the research logger without changing trading logic

## MEXC Futures note

This project is intentionally written for the bot's current `MARKET_TYPE=swap` / Futures environment.

MEXC's API Futures fee schedule has changed during 2026. The configuration therefore keeps maker/taker fees configurable rather than hard-coding them into the strategy. Verify the fee shown for your own MEXC API account before using executable-P/L estimates.

## Installation

From the `mexc_bot` folder:

```powershell
python -m pip install -r requirements.txt
```

## Step 1 — Create the research directory

```powershell
mkdir research\asset_selection
```

Copy the files from this package into that directory.

## Step 2 — Capture a manual snapshot

Example:

```powershell
python research\asset_selection\asset_selection_capture.py --symbols KAITO_USDT,CRV_USDT,PUMPFUN_USDT,BTC_USDT,ETH_USDT --config research\asset_selection\config.json
```

This is useful for checking that MEXC and CoinGecko enrichment works.

## Step 3 — Integrate the recorder into the existing bot

Open `live_bot.py`.

Do not rewrite the signal logic.

At startup:

```python
from research.asset_selection.asset_selection_recorder import ResearchRecorder

research = ResearchRecorder(
    "research/asset_selection/asset_events.csv"
)
```

At the point where the bot has evaluated a candidate pair:

```python
research.record_candidate(
    symbol=symbol,
    timestamp=datetime.now(timezone.utc),
    side=signal_side_or_none,
    price=current_price,
    signal_strength=signal_strength_or_none,
    reason=signal_reason_or_none,
)
```

When the strategy actually produces a signal:

```python
signal_id = research.record_signal(
    symbol=symbol,
    timestamp=datetime.now(timezone.utc),
    side=side,
    price=price,
    quantity=quantity,
    risk_usd=risk_usd,
    leverage=leverage,
    reason=reason,
)
```

Use the returned `signal_id` in the bot's normal trade log if possible.

### Do not do this

Do not replace:

```python
if signal:
    place_trade(...)
```

with a new filter.

The research hook should only observe and log.

## Step 4 — Detailed enrichment

For actual signals, run:

```powershell
python research\asset_selection\asset_selection_capture.py --signals research\asset_selection\asset_events.csv --config research\asset_selection\config.json
```

The capture script is designed to avoid unnecessarily querying deep order books for every scanned market. Ticker-level information can be collected broadly; detailed depth is most valuable for actual signals.

## Step 5 — Analyse

```powershell
python research\asset_selection\asset_selection_analyzer.py --events research\asset_selection\asset_events.csv --config research\asset_selection\config.json
```

Results are written to:

```text
research\asset_selection\results\
```

## What we want to see

The eventual output should answer:

- What market-cap ranks does the strategy select?
- How liquid are the assets?
- What spreads do we encounter?
- Are signals concentrated in small/speculative coins?
- Which market regimes generate signals?
- How long do positions remain open?
- How often do signals reverse?
- How much slippage is likely?
- Are theoretical signals executable?
- Does asset quality explain part of the current drawdown?

## Decision gates

Do not change the live strategy because of a small sample.

Suggested research gates:

### Gate 1 — Data quality

At least 95% of signal observations have usable:
- timestamp
- symbol
- side
- price
- spread

### Gate 2 — Sample size

Prefer at least 100 completed signal/trade observations before making a meaningful asset-universe decision.

### Gate 3 — Executability

Compare theoretical and estimated executable expectancy.

### Gate 4 — Out-of-sample

Any proposed filter must be tested on a fresh, untouched period.

### Gate 5 — Only then

If the evidence supports it, create:

```text
ASSET_FILTER_EXPERIMENT_V1
```

as a separate backtest/live-DRY-RUN branch.

## Research philosophy

The experiment is designed to test the hypothesis:

> The current strategy may be losing partly because its signal-selection mechanism disproportionately selects low-cap, volatile, thinly traded or speculative assets.

We do not assume the hypothesis is true.

If smaller assets produce superior **net executable** results, the data wins.

If larger, more liquid assets produce better PF, expectancy and drawdown, that becomes evidence for an asset-universe change.

