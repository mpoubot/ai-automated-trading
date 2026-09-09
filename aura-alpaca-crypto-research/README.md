# AURA Alpaca Crypto Research

Research-only repository for evaluating the AURA crypto strategy on Alpaca-supported crypto markets.

## Scope

This repository deliberately separates three activities:

1. **Historical backtest** — deterministic replay of historical Alpaca crypto bars.
2. **Paper forward test** — real-time Alpaca Paper Trading using paper credentials only.
3. **Comparison / validation** — compare historical expectations with forward-paper behaviour without changing the frozen strategy mid-test.

No live-money trading is enabled by this repository.

## Security

Never commit API keys or secrets. Put Alpaca Paper credentials in a local `.env` file. The repository's `.gitignore` excludes `.env` and common credential files.

Alpaca uses a separate paper endpoint and credentials from live trading. Paper trading is a simulation and can differ from backtests because of fill, liquidity, price and market-data assumptions.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` locally:

```text
ALPACA_API_KEY=your-paper-key
ALPACA_API_SECRET=your-paper-secret
ALPACA_PAPER=true
```

Do not paste the values into GitHub, chat, source code, or screenshots.

## First checks

```powershell
python -m src.alpaca_research.assets --paper
python -m src.alpaca_research.config
```

The assets command queries Alpaca's crypto asset master list and writes a timestamped snapshot under `data/universe/`.

## Research discipline

The strategy must be frozen before the validation run. Record:

- strategy version
- universe snapshot
- timeframe
- historical date range
- transaction-cost assumptions
- slippage assumptions
- position/risk rules
- entry/exit rules
- random seeds where applicable

Do not optimize parameters on the forward-test period.

## Planned layout

```text
src/alpaca_research/
  config.py       # environment/configuration
  assets.py       # Alpaca crypto universe discovery
  data.py         # historical/live market-data adapters
  strategy.py     # frozen strategy interface
  backtest.py     # deterministic historical replay
  paper.py        # paper-forward execution adapter
  ledger.py       # research/decision/execution records

tests/
  test_config.py
  test_strategy_contract.py
```

## Alpaca data notes

Historical crypto data is available through Alpaca's Historical Market Data API. Crypto bars can contain quote midpoint prices, so the backtest must document exactly which fields it uses. The paper environment is real-time simulation, not a guarantee of live-market performance.

## Status

Bootstrap stage. No live trading capability is present or intended here.
