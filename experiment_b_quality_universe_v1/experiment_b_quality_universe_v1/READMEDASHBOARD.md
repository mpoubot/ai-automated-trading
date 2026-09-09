# Experiment B Dashboard v1

Standalone read-only dashboard for Experiment B v2.

It reads:

`research\quality_universe_events_v2.csv`

and serves the dashboard on:

`http://localhost:8788`

It does **not** place orders and does **not** modify the Experiment B CSV.

## Run

Keep the existing Experiment B v2 PowerShell window running.

Open a **new PowerShell window** and run:

```powershell
cd "C:\Users\Martin\Desktop\AI agents\AI automated trading\experiment_b_quality_universe_v1\experiment_b_quality_universe_v1"
python .\experiment_b_dashboard_v1.py
```

Then open:

http://localhost:8788

The dashboard refreshes every 10 seconds.
