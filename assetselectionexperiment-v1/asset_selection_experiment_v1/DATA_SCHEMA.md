# Data schema

## Event types

### candidate

A pair evaluated by the strategy. This is the broad universe observation.

### signal

The strategy actually generated an entry signal.

### enrichment

Research-only market metadata attached to a signal.

### exit

Optional research exit record linked to `position_id`.

## Key identifiers

- `signal_id`: unique signal decision
- `position_id`: unique position lifecycle
- `event_id`: unique event row

The purpose of `position_id` is to eliminate the FIFO ambiguity seen in earlier logs where the same symbol was entered multiple times before an exit.

## Executability

The experiment estimates:

```text
estimated_slippage_bps
+
entry fee
+
exit fee
+
spread impact
=
estimated_total_execution_cost
```

This is a research estimate, not a claim of actual fill quality.

Actual fills should eventually be compared with:

```text
expected execution price
vs
actual execution price
```

## False-signal indicators

The V1 framework records/derives:

- repeated signal
- direction reversal
- wide spread
- thin order book
- abnormal volume
- price jump
- spread expansion
- rapid reversal

These are diagnostic flags first, not automatic rejection rules.
