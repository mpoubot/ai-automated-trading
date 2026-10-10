"""AURA multi-bot weekly telemetry compiler.

Reads the already-logged, append-only files each of the three live paper
trading instances (Bot 1 / Core Pinned, Bot 2 / S&P 500, Bot 3 / Russell
2000 High-Risk) writes on every cycle, and prints one consolidated matrix
instead of three separate Streamlit tabs.

SCHEMA NOTE (read this before trusting the numbers): an earlier draft of
this script assumed a top-level "status"=="EXECUTED" + "realized_pnl_usd"
field on each decision-journal line, and an "equity_usd" field on each
equity-history line. Neither exists anywhere in the actual AURA codebase --
confirmed by reading the real writers:
  - `aura_v05361_portfolio_enforcement_journal.py` (`JournalEntry.to_dict()`)
    writes {"appended_at", "decision_hash", "overall_verdict",
    "snapshot_as_of", "decision", "context"} -- there is no per-trade
    realized P&L anywhere in that record. `overall_verdict` is a risk-gate
    verdict ("ALLOW"/"BLOCK"-style), not a fill outcome.
  - `aura_v05364_equity_history_log.py` (`EquityObservation.to_dict()`)
    writes the equity value under the key "equity", not "equity_usd".
  - A repo-wide search for "realized_pnl_usd" found it nowhere as a
    persisted field -- only "unrealized_pnl_usd" exists, and only in live
    open-position snapshots that are never written to any .jsonl log.

So: win rate and profit factor genuinely cannot be computed from what's
logged today. This script reports only what's real -- the equity curve
(for return % and max drawdown %) and a verdict breakdown from the
decision journal as a rough activity proxy.

NOTE on the activity proxy: an earlier draft of this fix assumed
`overall_verdict` takes values like "ALLOW"/"BLOCK". Checked against
Martin's real, live `stage1b_decisions.jsonl` (293KB / 174 entries as of
this build) and that assumption was also wrong -- the actual values
observed are "SUBMITTED_FOR_EXECUTION" and "NOT_SHORTLISTED". Rather than
hardcode a second guess, this script counts every distinct verdict string
it actually finds and reports "SUBMITTED_FOR_EXECUTION" (if present) as
the headline activity number, with the full breakdown available via
--verbose. This is still a decision-engine-shortlist count, NOT a
confirmed fill and NOT P&L -- per-trade win rate / profit factor will
need a real fill/exit logger added to the live-trading pipeline before
they can be reported honestly -- that is a separate, bigger change to
`aura_v05365`/`aura_v05361` that was deliberately NOT made here so the
live bots stay untouched ahead of Monday.
"""

import os
import sys
import json
from collections import Counter
from datetime import datetime

# ============================================================================
# CONFIGURATION -- VERIFIED AGAINST run_aura_live.bat AND THE ACTUAL WRITER
# MODULES ON 2026-10-10. Bot 1 passes no --decision-journal-path /
# --equity-history-log-path flags, so it uses the scripts' own defaults
# (DEFAULT_JOURNAL_PATH / DEFAULT_EQUITY_HISTORY_LOG_PATH). Bot 2 / Bot 3
# paths are read directly out of their Window 3 / Window 4 start commands.
# ============================================================================
BOT_CONFIGS = {
    "Bot 1 (Core Pinned - 39)": {
        "journal": "regime_output/decision_journal/stage1b_decisions.jsonl",
        "equity": "regime_output/equity_history_log/alpaca_equity_history.jsonl",
    },
    "Bot 2 (S&P 500 Index)": {
        "journal": "regime_output/decision_journal/stage1b_decisions_bot2_sp500.jsonl",
        "equity": "regime_output/equity_history_log/alpaca_equity_history_bot2_sp500.jsonl",
    },
    "Bot 3 (Russell 2000 Small-Cap)": {
        "journal": "regime_output/decision_journal/stage1b_decisions_bot3_highrisk.jsonl",
        "equity": "regime_output/equity_history_log/alpaca_equity_history_bot3_highrisk.jsonl",
    },
}


def load_jsonl(path):
    if not os.path.exists(path):
        return []
    data = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                try:
                    data.append(json.loads(line.strip()))
                except Exception:
                    continue
    return data


def calculate_metrics(journal_data, equity_data):
    # Activity proxy only -- top-level "overall_verdict" is the real field
    # name (JournalEntry.to_dict()). Values observed in Martin's real log:
    # "SUBMITTED_FOR_EXECUTION" and "NOT_SHORTLISTED" -- counted dynamically
    # rather than hardcoded, since a third value (e.g. a risk-gate block)
    # may show up once real risk events happen and hasn't been observed yet.
    verdict_counts = Counter(e.get("overall_verdict") for e in journal_data)
    submitted_count = verdict_counts.get("SUBMITTED_FOR_EXECUTION", 0)
    total_entries = len(journal_data)

    # Real field is "equity" (EquityObservation.to_dict()), not "equity_usd".
    equity_curve = [float(e["equity"]) for e in equity_data if "equity" in e]

    initial_equity = equity_curve[0] if equity_curve else None
    current_equity = equity_curve[-1] if equity_curve else None

    if initial_equity is not None and initial_equity != 0:
        total_return_pct = (current_equity - initial_equity) / initial_equity * 100
    else:
        total_return_pct = None

    max_dd_pct = 0.0
    peak = None
    for val in equity_curve:
        if peak is None or val > peak:
            peak = val
        if peak and peak > 0:
            dd = (peak - val) / peak
            if dd > max_dd_pct:
                max_dd_pct = dd

    return {
        "submitted_count": submitted_count,
        "verdict_counts": verdict_counts,
        "journal_entries": total_entries,
        "snapshots": len(equity_curve),
        "initial_equity": initial_equity,
        "current_equity": current_equity,
        "total_return_pct": total_return_pct,
        "max_drawdown_pct": max_dd_pct * 100,
    }


def fmt_money(v):
    return f"${v:,.2f}" if v is not None else "N/A"


def fmt_pct(v):
    return f"{v:+.2f}%" if v is not None else "N/A"


def print_performance_matrix(verbose=False):
    print("=" * 112)
    print(f" AURA MULTI-BOT WEEKLY TELEMETRY -- EXTRACTION TIME: {datetime.now()}")
    print(" (equity-curve metrics only -- see module docstring for why trade-level")
    print("  win rate / profit factor are not included)")
    print("=" * 112)
    header = (
        f"{'Execution Instance':<32} | {'Submitted':<9} | {'Journal':<7} | {'Snaps':<6} | "
        f"{'Initial Equity':<16} | {'Current Equity':<16} | {'Total Return %':<15} | {'Max DD %':<8}"
    )
    print(header)
    print("-" * 112)

    all_metrics = {}
    for bot_name, paths in BOT_CONFIGS.items():
        journal = load_jsonl(paths["journal"])
        equity = load_jsonl(paths["equity"])
        m = calculate_metrics(journal, equity)
        all_metrics[bot_name] = m

        print(
            f"{bot_name:<32} | {m['submitted_count']:<9} | {m['journal_entries']:<7} | {m['snapshots']:<6} | "
            f"{fmt_money(m['initial_equity']):<16} | {fmt_money(m['current_equity']):<16} | "
            f"{fmt_pct(m['total_return_pct']):<15} | {m['max_drawdown_pct']:<7.2f}%"
        )

    print("=" * 112)
    print("NOTES:")
    print("- 'Submitted' = journal entries with overall_verdict == 'SUBMITTED_FOR_EXECUTION'.")
    print("  'Journal' = total decision-journal entries (submitted + not-shortlisted +")
    print("  any other verdict). This is a decision-engine shortlist count, NOT a")
    print("  confirmed fill and NOT P&L -- it is an activity proxy only.")
    print("- Total Return % / Max DD % are cumulative since each bot's own first")
    print("  logged equity snapshot, NOT a week-over-week delta. Re-running this")
    print("  next Friday will show the new cumulative numbers, not 'this week only'.")
    print("- 'Snaps' with 0 or 1 usually means the bot hasn't been live long enough")
    print("  yet to have a meaningful equity curve -- treat early N/A rows as that,")
    print("  not as a bug.")
    print("- Per-trade win rate / profit factor require a real fill/exit logger that")
    print("  does not exist yet in the live-trading pipeline (see module docstring).")
    print("=" * 112)

    if verbose:
        print()
        print("VERDICT BREAKDOWN (--verbose):")
        for bot_name, m in all_metrics.items():
            print(f"  {bot_name}: {dict(m['verdict_counts'])}")


if __name__ == "__main__":
    verbose_flag = "--verbose" in sys.argv
    print_performance_matrix(verbose=verbose_flag)
