# MEXC Automated Trading Research Platform
## Signal Validation & False-Signal Defense Specification

Version 1.0 — 2026-08-12

### Objective
Evolve the bot into an **AI-assisted quantitative research platform that discovers, tests, validates and monitors trading opportunities — with automated execution available only after an idea has survived the research process.**

The system must distinguish:
1. **Interesting signal** — technical setup worth investigating.
2. **Validated trade candidate** — survives market-quality, liquidity, persistence, regime and manipulation checks.
3. **Executable trade** — realistic entry/exit with acceptable spread, depth, slippage and market impact.

New filters must initially run in **research/shadow mode**, not directly change live execution.

---

## 1. Current observations to test
The current bot has shown:
- frequent selection of small-cap / highly volatile assets;
- repeated selection of the same asset;
- repeated PUMPFUN entries and stop-outs;
- technical signals that may be valid on candles but poor in practical execution;
- potential sensitivity to short-term price moves that can be created or amplified by relatively small trading activity;
- a recent dashboard state around 31 closed trades with negative return and profit factor below 1.

These are **hypotheses, not conclusions**. The new research layer must measure them.

---

## 2. Validation pipeline

TECHNICAL SIGNAL
→ Asset quality / market cap
→ Liquidity / depth
→ Spread
→ Estimated market impact
→ Signal persistence
→ Trade-flow confirmation
→ Order-book persistence / instability
→ Broader market regime
→ Manipulation / anomaly checks
→ Repeated-signal / cooldown check
→ SIGNAL INTEGRITY SCORE
→ Execution feasibility
→ Trade candidate
→ Telegram / human review
→ eventual automated execution only after validation

---

## 3. Asset quality and market-cap research

Record:
- market_cap_rank
- market_cap_usd
- FDV where available
- circulating supply
- listing age
- asset category
- utility/fundamental vs speculative classification

At minimum segment:
- Top 10
- Top 25
- Top 50
- Top 100
- Top 200
- >200
- unranked

Research question:
**What market-cap ranks does the strategy actually select?**

Do not assume large-cap automatically means profitable; measure the difference.

---

## 4. Utility vs speculative classification

Classify assets into research categories such as:
- Layer 1
- Layer 2 / scaling
- store of value / digital commodity
- DeFi / governance
- infrastructure / RWA / oracle / storage / compute
- stablecoin
- meme / social / speculative
- unknown

The purpose is to determine whether the technical strategy behaves differently on utility-oriented versus attention-driven assets.

---

## 5. MEXC listing/trading quality

Where reliable metadata exists, record:
- trading status
- API trading permission
- market type
- market-order availability
- listing date / listing age

Research listing-age buckets:
- <14 days
- 14–30 days
- 30–90 days
- >90 days

Initially **flag rather than hard-reject** new listings so the data can tell us whether a hard rule is justified.

---

## 6. Liquidity and order-book depth

24h volume alone is not sufficient.

Record:
- 24h quote volume
- bid depth
- ask depth
- depth within ±0.10%
- ±0.25%
- ±0.50%
- ±1.00%

Calculate:
`depth_to_position_ratio = available_depth / intended_position_value`

Research question:
**How liquid are the assets we select relative to our actual position size?**

---

## 7. Spread

Record:
- best bid
- best ask
- mid price
- absolute spread
- percentage spread

Formula:
`mid = (bid + ask) / 2`
`spread_pct = ((ask - bid) / mid) * 100`

Research question:
**What spreads are we actually encountering?**

---

## 8. Estimated market impact

Estimate expected impact for the intended order size, for example:
- impact($10)
- impact($50)
- impact($100)
- impact($500)

Record:
- entry impact
- exit impact
- round-trip impact
- order-book levels consumed
- nearby depth consumed

Research question:
**Would our own order materially move the market?**

Do not hard-code a final threshold before collecting the distribution.

---

## 9. Signal persistence

A technical signal must not automatically become an order.

At signal time capture a snapshot, then use a configurable confirmation window appropriate to the timeframe.

Record:
- signal price/time
- price after confirmation intervals
- high/low during confirmation
- whether original conditions remain valid
- failed-breakout status

Concept:
`SIGNAL → WAIT → SURVIVED? → YES: continue / NO: reject`

---

## 10. Trade-flow confirmation

Where recent trade data is available, record:
- trade count
- aggressive buy volume
- aggressive sell volume
- buy/sell volume ratio
- average and median trade size
- largest trade
- trade concentration

Goal:
Determine whether a move is supported by broad trading activity or only a small number of transactions.

---

## 11. Order-book persistence / manipulation risk

Repeatedly sample the order book where practical.

Example:
`T0 → T+10s → T+30s → T+60s`

Measure:
- persistence of large orders
- cancellation rate
- replenishment rate
- bid/ask depth changes
- imbalance stability

Classify behaviour as:
- stable
- unstable
- suspicious/anomalous

Do not claim to identify a specific malicious actor. Measure market behaviour.

---

## 12. Broader market regime

Record:
- BTC trend
- ETH trend
- CCI30 regime
- 7-day market return
- 30-day market return
- market breadth where available
- asset relative performance vs BTC
- asset relative performance vs CCI30

The previous CCI30 experiment showed promising evidence for a negative 30-day regime, but that is **research evidence only**, not a permanent rule. CCI30 should initially remain a contextual research feature.

---

## 13. Isolated price-move detection

Compare individual-asset moves with the broader market.

Example:
`Asset +5%, BTC flat, ETH flat, CCI30 flat/down` → higher scrutiny.

Versus:
`Asset +5%, BTC +1%, ETH +1.5%, CCI30 +2%` → broader confirmation.

This is a risk feature, not an automatic rejection.

---

## 14. Repeated signals and cooldowns

Record:
- signals per asset
- signals per hour/day
- time between signals
- successful vs failed repeated signals
- direction changes
- stop-outs before success

Example:
`PUMPFUN signal → STOP → signal → STOP → signal`

Research whether repeated signals represent genuine opportunity or repeated reaction to volatility.

Initially calculate a cooldown candidate in shadow mode rather than imposing it live.

---

## 15. Signal reversal

Measure:
- LONG → SHORT transitions
- SHORT → LONG transitions
- time between opposite signals
- price movement between reversals
- outcome after reversal

Research question:
**How often do signals reverse?**

A high reversal rate may indicate reaction to noise rather than persistent trends.

---

## 16. Position duration and trade quality

For every closed position record:
- entry time
- exit time
- duration
- exit reason
- MFE
- MAE
- time to +0.5R
- time to +1R
- time to stop
- time to exit

Research question:
**How long do positions actually remain open?**

---

## 17. Executability and slippage

For each theoretical signal calculate:
- theoretical entry
- estimated executable entry
- estimated exit
- estimated round-trip cost
- theoretical P&L
- estimated executable P&L

Research question:
**Are the theoretical signals actually executable?**

A candle-based positive backtest is not sufficient if execution costs destroy the edge.

---

## 18. Signal Integrity Score

Create a normalized 0–100 research score from:
- asset quality
- liquidity
- spread
- market impact
- signal persistence
- trade-flow confirmation
- order-book stability
- market regime
- repeated-signal risk
- executability

Initially use the score for observation only. Do not optimize its weighting against the final holdout dataset.

---

## 19. Three-state decision model

### INTERESTING
Technical conditions satisfied.

Action: log and enrich.

### VALIDATED
Technical signal plus market-quality checks pass.

Action: Telegram candidate / human review.

### EXECUTABLE
Validated signal plus realistic execution conditions pass.

Action: eventually eligible for automated execution.

A highly volatile coin that repeatedly triggers the strategy but cannot be traded reliably should be classified as:

**INTERESTING BUT NOT TRADEABLE.**

---

## 20. Telegram notification concept

Instead of simply:

`BUY PUMP`

use:

`TRADE CANDIDATE`

with:
- symbol/direction
- technical conditions
- market-cap rank
- 24h volume
- spread
- depth
- estimated market impact
- signal persistence
- trade-flow status
- order-book stability
- CCI30/BTC/ETH regime
- Signal Integrity Score
- execution status
- rejection/warning reason

This preserves the user's role as final decision-maker while making the signal much more useful.

---

## 21. Required research fields

At minimum log:

`timestamp`
`symbol`
`direction`
`technical_signal`
`market_cap_rank`
`market_cap_usd`
`asset_category`
`listing_age_days`
`volume_24h`
`best_bid`
`best_ask`
`mid_price`
`spread_pct`
`depth_010`
`depth_025`
`depth_050`
`depth_100`
`estimated_market_impact`
`buy_volume`
`sell_volume`
`trade_count`
`trade_size_statistics`
`orderbook_stability`
`cci30_regime`
`btc_regime`
`eth_regime`
`signal_persistence`
`signal_reversal`
`previous_signal_count`
`cooldown_status`
`signal_integrity_score`
`validation_state`
`rejection_reason`
`theoretical_entry`
`estimated_executable_entry`
`estimated_slippage`
`eventual_outcome`

Every candidate should be logged, including rejected candidates.

---

## 22. Experiments before changing live rules

Run controlled research comparisons:

A. Current strategy  
B. + market-cap segmentation  
C. + liquidity/spread controls  
D. + signal persistence  
E. + market-regime context  
F. + complete validation layer

Compare:
- expectancy
- profit factor
- average R
- win rate
- drawdown
- max losing streak
- MFE
- MAE
- average duration
- estimated slippage
- rejected-signal percentage
- executable-signal percentage

Use fresh/untouched data for final validation. Do not tune thresholds on the holdout.

---

## 23. Key research questions

1. What market-cap ranks does it select?
2. How liquid are the assets?
3. What spreads are encountered?
4. Are signals concentrated in small coins?
5. Which market regimes generate signals?
6. How long do positions remain open?
7. How often do signals reverse?
8. How much slippage would likely occur?
9. Are theoretical signals actually executable?
10. Are repeated signals caused by genuine persistent opportunities or simply volatility?
11. Do rejected low-quality signals perform worse than accepted signals?
12. Does filtering small/illiquid assets improve out-of-sample expectancy?
13. Does signal persistence reduce false breakouts?
14. Does trade-flow/order-book confirmation reduce stop-outs?
15. Does CCI30 add independent predictive value?
16. Does the combined validation layer improve results without simply reducing trade count?

---

## 24. Anti-overfitting rules

The objective is **not** to find filters that make historical results look best.

The objective is to find market-quality characteristics that continue to matter on unseen data.

Therefore:
- keep a frozen baseline;
- keep an untouched validation period;
- do not optimize on the final holdout;
- do not select rules merely because one historical bucket performed best;
- avoid adding indicators simply because they improve one backtest;
- test promising hypotheses on fresh data.

---

## 25. Implementation phases

### Phase 1 — OBSERVE
Collect the new data without changing trading behaviour.

### Phase 2 — CLASSIFY
Calculate validation state, anomaly indicators and Signal Integrity Score.

### Phase 3 — VALIDATE
Compare accepted and rejected signals on fresh data.

### Phase 4 — SHADOW
Run proposed rules alongside the existing strategy without changing execution.

### Phase 5 — LIMITED EXECUTION
Only after out-of-sample evidence supports the rules should selected safeguards affect automated execution.

---

## 26. Working hypothesis

The current bot's repeated selection of small, volatile assets may be contributing materially to poor practical performance.

This is **not proven yet**.

The platform must test whether small/illiquid/high-volatility signals have:
- worse execution;
- higher slippage;
- higher MAE;
- more stop-outs;
- more signal reversals;
- shorter-lived momentum;
- lower realized expectancy

than larger/more liquid/better-depth assets.

The final decision must come from fresh out-of-sample evidence.

---

## Final design principle

The platform should seek opportunities where:

**TECHNICAL EDGE  
+ MARKET QUALITY  
+ LIQUIDITY  
+ PERSISTENCE  
+ MARKET CONFIRMATION  
+ EXECUTABILITY**

are aligned.

The bot should not simply ask:

> “Did the price move?”

It should ask:

> **“Is this a genuine, persistent and sufficiently liquid opportunity that can realistically be traded?”**
