# Alpaca C0 Control Experiment

This directory contains the Alpaca venue/data replication of AURA's frozen C0 control.

## Control

- EMA3/EMA8 bullish crossover
- MACD histogram > 0
- Relative volume >= 1
- Entry: next 1H bar OPEN
- Exit: CLOSE of the 10th complete held 1H bar
- Long-only, 1x notional
- 5 bp entry slippage + 1 bp entry fee
- 5 bp exit slippage + 1 bp exit fee
- No optimization
- No threshold selection
- No stop/target
- No time filter
- No OI filter
- No orders

## Important comparison rule

The original C0 control uses MEXC USDT perpetual data. Alpaca crypto is spot data. Therefore this experiment is a **venue/data replication**, not an economically identical perpetual-vs-perpetual comparison.

The default universe is BTC/USD and ETH/USD because these are the closest Alpaca spot symbols to the frozen BTC_USDT/ETH_USDT control.

`--all-usd` is intentionally a separate universe experiment and must not be mixed into the control result.

## Run

```powershell
python -m research.alpaca_c0_backtest --days 30
```

Optional broad universe:

```powershell
python -m research.alpaca_c0_backtest --days 30 --all-usd
```

Outputs are written under `AURA_ALPACA_C0/` and include:

- `bars_1h.csv`
- `trades_c0.csv`
- `summary.json`
- `research_run.json`
