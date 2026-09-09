# AURA v0.4.9 build

## What this adds

### v0.4.8.2 — Paper Lab Integrity Check
`aura_v0482_integrity_check.py`

Read-only hardening layer. It checks the frozen Experiment B data contract without modifying it.

Run:

```powershell
python aura_v0482_integrity_check.py
```

### v0.4.9 — Mission Control
`aura_v049_mission_control.py`

Read-only HTML dashboard over the existing `AURA_LIVE` CSV files.

Run:

```powershell
python aura_v049_mission_control.py
```

Then open:

```text
AURA_LIVE\mission_control.html
```

It also creates:

```text
AURA_LIVE\mission_control_summary.csv
```

## Frozen v0.4.8 contract

- Signal Master: EMA3/EMA8 bullish crossover + MACD histogram > 0 + RelVol >= 1
- Model A: 10D close
- Model B: pure ATR 2x stop / 4x target, 60-session research horizon
- Model C: ATR 2x/4x + 10D close
- Friction: 5 bp entry slippage + 1 bp commission + 5 bp exit slippage + 1 bp commission = 12 bp round trip
- S&P 100 universe is immutable once `universe.csv` exists
- Paper only — no order submission

## Important

Do not edit historical `AURA_LIVE` files manually. The dashboard is an analytics layer, not a trading engine.

Next phase after sufficient live observations:
- v0.5.0 Crypto Research Engine
- v0.6.0 Options Overlay Research
