# AURA v0.1 — Signal Research Core

Research-only first build for the Alpaca AI Trading Agents Hackathon.

Pipeline:
Alpaca Market Data -> Technical Agent -> Signal Validation -> Structured Opportunity

Indicators:
- EMA 3/8 early momentum
- EMA 8/21 confirmation
- EMA 21/50 broader trend
- RSI 14
- MACD 12/26/9
- Bollinger Bands 20/2
- ATR 14
- Relative volume
- Price acceleration

False-signal checks:
- crossover persistence
- acceleration confirmation
- volume confirmation
- trend alignment
- RSI extension
- immediate reversal / whipsaw
- disagreement between signal speeds

This version NEVER places orders.

Run:
python aura_research.py --symbol AAPL --start 2026-07-01 --end 2026-08-25
