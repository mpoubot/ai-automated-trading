# AURA Alpaca C0 Backtest Dashboard

Read-only diagnostic dashboard for the deterministic outputs produced by `research/alpaca_c0_backtest.py`.

## Run

From the repository root:

```powershell
python .\research\alpaca_c0_dashboard.py --input ".\AURA_ALPACA_C0" --output ".\AURA_ALPACA_C0_DASHBOARD"
```

Then open:

```text
AURA_ALPACA_C0_DASHBOARD\dashboard.html
```

The dashboard consumes `summary.json` and `trades_c0.csv` only. It does not call Alpaca, place orders, change strategy parameters, or connect to an execution adapter.

## Test

```powershell
python -m pytest .\research\test_alpaca_c0_dashboard.py -q
```

The smoke test verifies dashboard generation, required trade fields, symbol rendering, and the read-only execution guardrail.
