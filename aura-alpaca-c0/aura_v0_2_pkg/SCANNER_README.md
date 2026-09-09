# AURA v0.2 — Historical Signal Scanner

Research-only backtest layer for the AURA autonomous trading agent.

## What it does

- Pulls daily OHLCV data from Alpaca using the IEX feed.
- Builds the existing AURA technical features.
- Finds every bullish EMA 3/8 crossover after 55 bars of history.
- Evaluates the signal using the current AURA validator at the exact historical bar.
- Uses the **next trading day's open** as the entry proxy to avoid look-ahead from the signal bar's close.
- Measures forward close returns at 1, 3, 5 and 10 trading days.
- Measures 5-day maximum favorable excursion (MFE) and maximum adverse excursion (MAE).
- Labels outcomes for research only: STRONG_POSITIVE, POSITIVE, FLAT, NEGATIVE, WHIPSAW, INCOMPLETE.
- Writes a CSV for later statistical analysis.

## Run

```powershell
python historical_signal_scanner.py --symbol AAPL --start 2026-01-01 --end 2026-08-25
```

Output:

```text
research\aapl_historical_signals.csv
```

## Important

This is **not an execution strategy** and places no orders. Outcome labels are research classifications, not trading rules. Thresholds should be validated across many symbols and market regimes before being used for live decisions.
