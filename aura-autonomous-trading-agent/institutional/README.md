# institutional/ — new, additive code (2026-10-09, updated same day)

Three new subpackages under this directory: `track_b/`, `options_o10/`,
`portfolio/`. None of this modifies any existing `aura_v053NN...` file.
Each subpackage imports the real classes/functions it needs from the
existing flat modules at this repo's root (e.g. `.368`'s
`combine_enforcement_check_fns`, `.344`'s `DimensionVerdict`, `.361`'s
`record_decision`) — never a re-implementation.

Run its tests from inside each subdirectory, e.g.:
    cd institutional/track_b && pytest -q

Full build report, integration snippets (quoted, not applied to any
existing file), and the open items are in the project doc
`AURA_Institutional_Risk_Execution_Code_Build_Report_2026-10-09` and in
this package's own top-level README.md (delivered to Martin via chat as
AURA_Institutional_Code_Package_2026-10-09.zip — same content now lives
at the root of this `institutional/` folder's sibling copy there).

`portfolio/` needs the `AURA_CORE_DIR` environment variable set to this
repo's root directory (the directory containing this `institutional/`
folder) wherever the real run loop is started — see
`portfolio/portfolio_additional_enforcement.py`'s own docstring. As of
2026-10-09 this is validated at import time: a misconfigured path now
fails closed immediately with a specific error naming the missing file.

## 2026-10-09 update, per Martin's explicit directive

- `portfolio/macro_buckets.py`: the same-direction stacking-ratio
  formula is now implemented and enforced (hard 0.15 threshold,
  unconditional, not config-driven) — both as a portfolio-level
  dimension and as a genuine pre-trade veto. `exposure_ratio` is now
  gross-notional-based, not net.
- `portfolio/greeks_limits.py`: gamma/vega dollarization now use
  Martin's exact specified formulas.
- `options_o10/signal_mapping.py` + `execution_constraints.py`: the
  bull call spread (net-debit) now carries `orders_enabled=False` and
  is unconditionally blocked at the execution-constraints gate — not
  just flagged — until custom debit-spread exit logic exists.

100 tests pass across all four sections (was 90).

## 2026-10-09 production-loop integration and calibration (Task 1/2/3)

- **Task 1 — exact `.355` integration hooks**: see
  `INTEGRATION_HOOKS_2026-10-09.md` at this repo's root (same file also
  committed under `mexc_bot/institutional/`, since Task 1 also covers
  `live_bot.py`/`backtester.py` there). Quoted code, real line numbers,
  not applied. Corrects this package's own prior claim about where the
  `live_bot.py`/`backtester.py` gate belongs (after sizing, not right
  after the signal — see that document's correction note).
- **Task 2 — `greeks_limits.py` mark-price fallback / wide-spread
  handling**: implemented and tested (11 new tests, all opt-in via three
  new optional parameters; omitting them reproduces prior behavior
  exactly). See that module's own docstring for the full mechanism and a
  disclosed correction to a literal reading of Martin's own spec (the
  underlying-price fallback must not be multiplier-scaled before it
  reaches the Spot-Price formula slot, or Dollar Gamma/Vega silently
  inflate 100x for that leg).
- **Task 3 — `calibrate_institutional_limits.py`**: new file at this
  repo's root (beside `aura_v054_backtest.py`, which it imports as a
  sibling). Real pinned universe, real `.054` backtest tool, real
  `macro_buckets`/`greeks_limits` functions. **Important constraint found
  while building this, not previously documented**: `.054`'s backtest has
  zero options exposure, so Greeks limits cannot be swept against real
  backtest data at all — that section of the script's output is formula-
  grounded, not backtest-grounded, and labeled as such. Smoke-tested
  against synthetic data only (code path verification); a real
  calibration run needs real daily bars, which this sandbox has no
  confirmed access to fetch.

111 tests pass across all four sections (was 100).
