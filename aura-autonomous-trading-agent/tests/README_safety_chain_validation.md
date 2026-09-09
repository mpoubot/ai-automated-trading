# AURA v0.5.3 Safety Chain Validation

Run from repository root:

```powershell
python .\tests\aura_v053_safety_chain_validation.py
```

The harness is offline-only and uses temporary fixtures. It does not call MEXC,
place orders, enable live execution, or mutate production `regime_output` state.

It validates the `.16 -> .17 -> .18 -> .19` execution-safety chain, including:

- clean no-order lineage;
- requirement for an explicit observed-execution source;
- valid observed-fill reconciliation;
- tampered ledger rejection;
- tampered observed-snapshot rejection;
- fail-closed execution-safety behavior;
- presence of all four contracts;
- exchange/live-execution guardrails remaining disabled.

A PASS is a prerequisite for proceeding to v0.5.3.20.
