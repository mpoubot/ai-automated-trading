# institutional/ — new, additive code (2026-10-09)

One new subpackage: `track_a/`. Does not modify `backtester.py`,
`live_bot.py`, or anything in `core/`.

Run its tests from inside the subdirectory:
    cd institutional/track_a && pytest -q

INTEGRATION NOTE (not yet applied to backtester.py/live_bot.py — quoted
here and in track_a/entry_filters.py's own docstring):

Both call sites need `institutional/track_a` added to sys.path before
`import entry_filters`, since this subpackage's own files import each
other as flat siblings (matching this repo's own existing style inside
track_a itself), e.g.:

    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "institutional", "track_a"))
    import entry_filters

`core/` already resolves correctly with no extra path change, because
backtester.py/live_bot.py themselves already run with this repo's root
on sys.path.

Exact one-line call-site integration for both files is quoted in
`track_a/entry_filters.py`'s own docstring.

## 2026-10-09 update — exact insertion points, corrected

`INTEGRATION_HOOKS_2026-10-09.md` (this directory and the
`aura-autonomous-trading-agent/institutional/` copy) now gives the full,
real-line-number code for both `backtester.py` and `live_bot.py`,
including the `sys.path` wiring sketched above. It also **corrects** this
package's own prior documentation: the gate belongs AFTER
`calc_position_plan()` succeeds, not right after `strat.evaluate_signal
(...)` — `evaluate_entry_filters()`'s required `planned_notional_usd`
argument does not exist until sizing has already run. Still quoted, not
applied, to either real file.
