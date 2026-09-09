# Experiment B — Quality Universe V1

This is a separate research experiment for the controlled **Top 200 by market-cap** universe.

- DRY_RUN=True
- No orders are placed.
- Output is separate from the existing Asset Selection experiment.
- Run it in a separate PowerShell window.

## Run
`python experiment_b_quality_universe_v1.py`

Stop with Ctrl+C. The CSV remains on disk.

## Localhost
The existing experiment can keep its localhost dashboard (for example 8787).
Experiment B reserves 8788 for a future dashboard, but this recorder does **not**
start a web server, so there is currently no localhost conflict.

## Output
`research\quality_universe_events.csv`
