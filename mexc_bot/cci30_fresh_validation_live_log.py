"""
CCI30 FRESH / FORWARD VALIDATION
================================

Purpose
-------
Validate the PRE-DECLARED CCI30 rule on genuinely new trade data:

    KEEP a CURRENT trade only when CCI30 trailing 30-day return < 0.

This script does NOT:
- tune the CCI30 threshold
- test alternative thresholds
- add other filters
- change entries/exits
- select a direction
- optimize anything from the validation data

The earlier 1,458-trade dataset must NOT be used as the validation dataset.
By default, this script requires validation trades to start AFTER 2026-08-09,
the end of the previously inspected dataset.

Expected inputs
---------------
1) A NEW trade CSV produced by the normal backtester after the validation
   period has elapsed.
2) A CCI30 daily OHLCV CSV covering the same dates.

The trade CSV must contain:
    entry_time
    realized_r (or realized_pnl_r / pnl_r / r_multiple)
    side

The CCI30 CSV should contain:
    Date (or Timestamp/date)
    Close

Recommended protocol
--------------------
Run the normal backtester on a NEW period only. Do not mix the old 1,458
trades into the validation file.

Then run:

python cci30_fresh_validation.py --trades-csv "backtest_results/fresh_trades.csv" --cci30-csv ".\\cci30_OHLCV_fresh.csv"

For a different validation cutoff:

python cci30_fresh_validation.py --trades-csv "..." --cci30-csv "..." --min-entry-date "2026-08-10"

Interpretation
--------------
PRIMARY:
    CCI30_NEGATIVE_30D versus CURRENT on the new data.

The rule is considered promising only if:
    - enough trades exist (default >= 50)
    - filtered PF > CURRENT PF
    - filtered expectancy > CURRENT expectancy
    - the filtered sample is not trivially small

This is a validation test, not a license to tune the rule.

IMPORTANT:
The CCI30 observation was discovered using the old dataset, so the first
fresh run is a confirmation test. If it succeeds, the next step should be
a longer forward/paper validation, not threshold optimization.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import numpy as np
import pandas as pd


OLD_DATA_END = pd.Timestamp("2026-08-09", tz="UTC")
DEFAULT_MIN_TRADES = 50


def find_col(df: pd.DataFrame, candidates: list[str], label: str) -> str:
    lower = {str(c).strip().lower(): c for c in df.columns}
    for candidate in candidates:
        if candidate.lower() in lower:
            return lower[candidate.lower()]
    raise ValueError(
        f"Could not find {label} column. Expected one of {candidates}. "
        f"Available columns: {list(df.columns)}"
    )


def load_trades(
    path: Path,
    min_entry_date: pd.Timestamp,
    risk_per_trade: float = 10.0,
) -> pd.DataFrame:
    """
    Load either:
      1) normal backtester output containing realized-R, OR
      2) the live/paper bot event log (timestamp,symbol,event_type,side,
         price,quantity,leverage,pnl,equity).

    For the live bot log we reconstruct completed trades from ENTRY ->
    optional PARTIAL_TP events -> EXIT events, then calculate:

        realized_R = total realized pnl / fixed risk_per_trade

    The event log does not contain a per-trade R field, so the risk amount
    must come from the bot's configured risk-per-trade. Default is $10,
    matching the current bot logs shown in the research run.
    """
    if not path.exists():
        raise FileNotFoundError(f"Trade CSV not found: {path}")

    if risk_per_trade <= 0:
        raise ValueError("--risk-per-trade must be > 0.")

    df = pd.read_csv(path)
    normalized = {str(c).strip().lower() for c in df.columns}

    # ------------------------------------------------------------
    # FORMAT A: normal backtester output already has realized-R
    # ------------------------------------------------------------
    has_r = any(
        c in normalized
        for c in ["realized_r", "realized_pnl_r", "pnl_r", "r_multiple"]
    )

    if has_r:
        entry_col = find_col(
            df,
            ["entry_time", "entry_timestamp", "timestamp", "entry_date"],
            "entry time",
        )
        r_col = find_col(
            df,
            ["realized_r", "realized_pnl_r", "pnl_r", "r_multiple"],
            "realized-R",
        )
        side_col = find_col(df, ["side", "direction"], "direction")

        out = df.copy()
        out["_entry_time"] = pd.to_datetime(
            out[entry_col], utc=True, errors="coerce"
        )
        out["_realized_r"] = pd.to_numeric(out[r_col], errors="coerce")
        out["_side"] = out[side_col].astype(str).str.upper().str.strip()

        bad = out["_entry_time"].isna() | out["_realized_r"].isna()
        if bad.any():
            raise ValueError(
                f"{int(bad.sum())} trade rows have invalid entry time or realized-R."
            )

        out = out[out["_entry_time"] > min_entry_date].copy()

        if out.empty:
            raise ValueError(
                f"No trades remain after the freshness cutoff {min_entry_date}."
            )

        return out.sort_values("_entry_time").reset_index(drop=True)

    # ------------------------------------------------------------
    # FORMAT B: live/paper bot event log
    # ------------------------------------------------------------
    required = {
        "timestamp",
        "symbol",
        "event_type",
        "side",
        "price",
        "quantity",
        "pnl",
    }
    missing = sorted(required - normalized)
    if missing:
        raise ValueError(
            "Trade CSV is neither backtester format nor live-bot event-log format. "
            f"Missing columns: {missing}. Available columns: {list(df.columns)}"
        )

    ts_col = find_col(df, ["timestamp"], "timestamp")
    symbol_col = find_col(df, ["symbol"], "symbol")
    event_col = find_col(df, ["event_type"], "event type")
    side_col = find_col(df, ["side"], "direction")
    price_col = find_col(df, ["price"], "price")
    qty_col = find_col(df, ["quantity"], "quantity")
    pnl_col = find_col(df, ["pnl"], "pnl")

    events = df.copy()
    events["_time"] = pd.to_datetime(events[ts_col], utc=True, errors="coerce")
    events["_symbol"] = events[symbol_col].astype(str).str.strip()
    events["_event"] = events[event_col].astype(str).str.lower().str.strip()
    events["_side"] = events[side_col].astype(str).str.upper().str.strip()
    events["_price"] = pd.to_numeric(events[price_col], errors="coerce")
    events["_quantity"] = pd.to_numeric(events[qty_col], errors="coerce")
    events["_pnl"] = pd.to_numeric(events[pnl_col], errors="coerce").fillna(0.0)

    bad = (
        events["_time"].isna()
        | events["_symbol"].eq("")
        | events["_side"].eq("")
        | events["_price"].isna()
        | events["_quantity"].isna()
    )
    if bad.any():
        raise ValueError(
            f"{int(bad.sum())} event rows have invalid timestamp/symbol/side/"
            "price/quantity."
        )

    events = events.sort_values("_time").reset_index(drop=True)

    # One open position per symbol+side is the model used by the current
    # bot. If another entry arrives before an exit, fail loudly rather than
    # silently assigning PnL to the wrong trade.
    open_positions = {}
    completed = []

    for _, row in events.iterrows():
        key = (row["_symbol"], row["_side"])
        event = row["_event"]

        is_entry = event == "entry" or event.startswith("entry_")
        is_partial = event.startswith("partial_tp") or event in {
            "partial_tp",
            "partial-tp",
        }
        is_exit = (
            event.startswith("exit")
            or event in {"close", "closed", "liquidation", "liquidated"}
        )

        if is_entry:
            if key in open_positions:
                raise ValueError(
                    "Encountered a second entry before the previous position "
                    f"closed for {key}. The event log cannot be safely reconstructed "
                    "without a trade/position ID."
                )

            # Ignore old entries only after reconstructing the position state;
            # this lets a position opened before the cutoff and closed after it
            # be treated correctly as a fresh realized trade only if its entry
            # itself is after the cutoff.
            open_positions[key] = {
                "entry_time": row["_time"],
                "symbol": row["_symbol"],
                "side": row["_side"],
                "entry_price": float(row["_price"]),
                "entry_quantity": float(row["_quantity"]),
                "realized_pnl": 0.0,
                "events": 0,
            }
            continue

        if key not in open_positions:
            # An exit without a matching entry in the supplied file cannot be
            # assigned reliably. Ignore it rather than inventing a trade.
            continue

        pos = open_positions[key]

        if is_partial:
            pos["realized_pnl"] += float(row["_pnl"])
            pos["events"] += 1
            continue

        if is_exit:
            pos["realized_pnl"] += float(row["_pnl"])
            pos["events"] += 1

            # Only completed trades whose ENTRY is after the freshness cutoff
            # belong in the validation sample.
            if pos["entry_time"] > min_entry_date:
                completed.append(
                    {
                        "entry_time": pos["entry_time"],
                        "symbol": pos["symbol"],
                        "side": pos["side"],
                        "entry_price": pos["entry_price"],
                        "entry_quantity": pos["entry_quantity"],
                        "exit_time": row["_time"],
                        "exit_price": float(row["_price"]),
                        "realized_pnl": pos["realized_pnl"],
                        "_realized_r": pos["realized_pnl"] / risk_per_trade,
                    }
                )

            del open_positions[key]

    out = pd.DataFrame(completed)

    if out.empty:
        raise ValueError(
            "No completed live-bot trades remain after the freshness cutoff "
            f"{min_entry_date}. Open positions are not included."
        )

    out["_entry_time"] = pd.to_datetime(out["entry_time"], utc=True)
    out["_side"] = out["side"].astype(str).str.upper().str.strip()

    return out.sort_values("_entry_time").reset_index(drop=True)


def load_cci30(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"CCI30 CSV not found: {path}")

    df = pd.read_csv(path)

    date_col = find_col(
        df,
        ["date", "timestamp", "datetime", "time"],
        "CCI30 date",
    )
    close_col = find_col(df, ["close"], "CCI30 close")

    out = pd.DataFrame({
        "_cci_time": pd.to_datetime(df[date_col], utc=True, errors="coerce"),
        "_cci_close": pd.to_numeric(df[close_col], errors="coerce"),
    }).dropna()

    out = (
        out.sort_values("_cci_time")
        .drop_duplicates("_cci_time")
        .reset_index(drop=True)
    )

    if len(out) < 35:
        raise ValueError("CCI30 CSV contains too few usable daily rows.")

    # Work from daily closes.
    daily = (
        out.set_index("_cci_time")["_cci_close"]
        .resample("1D")
        .last()
        .dropna()
        .sort_index()
    )

    target = pd.DataFrame({"_cci_time": daily.index})
    target["_target_30d"] = target["_cci_time"] - pd.Timedelta(days=30)

    prior = pd.DataFrame({
        "_prior_time": daily.index,
        "_prior_close": daily.values,
    })

    target = pd.merge_asof(
        target.sort_values("_target_30d"),
        prior.sort_values("_prior_time"),
        left_on="_target_30d",
        right_on="_prior_time",
        direction="backward",
        tolerance=pd.Timedelta(days=5),
    )

    target["_cci30_30d_return"] = (
        daily.reindex(target["_cci_time"]).to_numpy()
        / target["_prior_close"]
        - 1.0
    )

    return target[
        ["_cci_time", "_cci30_30d_return"]
    ].dropna().sort_values("_cci_time").reset_index(drop=True)


def attach_regime(trades: pd.DataFrame, cci: pd.DataFrame) -> pd.DataFrame:
    left = trades.copy()
    left["_entry_day"] = left["_entry_time"].dt.normalize()

    right = cci.copy()
    right["_cci_day"] = right["_cci_time"].dt.normalize()

    enriched = pd.merge_asof(
        left.sort_values("_entry_day"),
        right.sort_values("_cci_day"),
        left_on="_entry_day",
        right_on="_cci_day",
        direction="backward",
        tolerance=pd.Timedelta(days=3),
    )

    enriched["cci30_regime"] = np.where(
        enriched["_cci30_30d_return"] < 0,
        "NEGATIVE_30D",
        np.where(
            enriched["_cci30_30d_return"] >= 0,
            "POSITIVE_30D",
            "UNMATCHED",
        ),
    )

    return enriched


def max_losing_streak(r: pd.Series) -> int:
    streak = best = 0
    for x in (r > 0).astype(int).to_numpy():
        if x:
            streak = 0
        else:
            streak += 1
            best = max(best, streak)
    return best


def metrics(df: pd.DataFrame) -> dict:
    r = pd.to_numeric(df["_realized_r"], errors="coerce").dropna()

    if r.empty:
        return {
            "trades": 0,
            "win_rate_pct": np.nan,
            "profit_factor": np.nan,
            "expectancy_R": np.nan,
            "total_R": 0.0,
            "median_R": np.nan,
            "max_losing_streak": 0,
        }

    wins = r[r > 0]
    losses = r[r < 0]

    gross_profit = float(wins.sum())
    gross_loss = float(-losses.sum())

    if gross_loss > 0:
        pf = gross_profit / gross_loss
    else:
        pf = math.inf if gross_profit > 0 else np.nan

    return {
        "trades": int(len(r)),
        "win_rate_pct": float((r > 0).mean() * 100),
        "profit_factor": float(pf),
        "expectancy_R": float(r.mean()),
        "total_R": float(r.sum()),
        "median_R": float(r.median()),
        "max_losing_streak": max_losing_streak(r),
    }


def fmt_pf(x):
    if pd.isna(x):
        return "nan"
    if math.isinf(x):
        return "inf"
    return f"{x:.3f}"


def print_metrics(label: str, df: pd.DataFrame) -> dict:
    m = metrics(df)
    print(
        f"{label:<24} "
        f"trades {m['trades']:>5} | "
        f"win {m['win_rate_pct']:>6.2f}% | "
        f"PF {fmt_pf(m['profit_factor']):>7} | "
        f"expectancy {m['expectancy_R']:+.4f}R | "
        f"total R {m['total_R']:+.2f} | "
        f"max loss streak {m['max_losing_streak']:>3}"
    )
    return m


def main():
    parser = argparse.ArgumentParser(
        description="Validate the pre-declared CCI30 negative-30D filter on fresh data."
    )
    parser.add_argument("--trades-csv", required=True)
    parser.add_argument("--cci30-csv", required=True)
    parser.add_argument(
        "--min-entry-date",
        default="2026-08-09",
        help="Validation trades must be AFTER this UTC date.",
    )
    parser.add_argument(
        "--min-trades",
        type=int,
        default=DEFAULT_MIN_TRADES,
    )
    parser.add_argument(
        "--risk-per-trade",
        type=float,
        default=10.0,
        help=(
            "Fixed dollar risk used to convert live-bot realized PnL to R. "
            "Used only when --trades-csv is the live event log."
        ),
    )
    args = parser.parse_args()

    cutoff = pd.Timestamp(args.min_entry_date, tz="UTC")

    print("=" * 82)
    print("CCI30 FRESH / FORWARD VALIDATION")
    print("=" * 82)
    print("PRE-DECLARED RULE:")
    print("Keep CURRENT trades only when CCI30 trailing 30-day return < 0.")
    print()
    print("NO TUNING:")
    print("- no threshold sweep")
    print("- no additional filters")
    print("- no direction changes")
    print("- no exit changes")
    print()
    print(f"Fresh-data cutoff: trades AFTER {cutoff}")
    print(f"Live-log R conversion risk: ${args.risk_per_trade:.2f} per trade")
    print()

    trades = load_trades(Path(args.trades_csv), cutoff, args.risk_per_trade)
    cci = load_cci30(Path(args.cci30_csv))
    enriched = attach_regime(trades, cci)

    matched = enriched["cci30_regime"].ne("UNMATCHED")
    valid = enriched.loc[matched].copy()

    print("DATA VALIDATION")
    print("-" * 82)
    print(f"Trades supplied:       {len(trades)}")
    print(f"Completed trades after cutoff: {len(trades)}")
    print(f"CCI30 rows:             {len(cci)}")
    print(f"Trades matched:         {int(matched.sum())}")
    print(f"Trades unmatched:       {int((~matched).sum())}")

    if valid.empty:
        raise RuntimeError("No trades could be matched to CCI30.")

    print()
    print(
        f"Validation period: {valid['_entry_time'].min()} -> "
        f"{valid['_entry_time'].max()}"
    )

    current = print_metrics("CURRENT", valid)
    negative = print_metrics(
        "CCI30_NEGATIVE_30D",
        valid[valid["cci30_regime"] == "NEGATIVE_30D"],
    )
    positive = print_metrics(
        "CCI30_POSITIVE_30D",
        valid[valid["cci30_regime"] == "POSITIVE_30D"],
    )

    pf_delta = negative["profit_factor"] - current["profit_factor"]
    exp_delta = negative["expectancy_R"] - current["expectancy_R"]
    wr_delta = negative["win_rate_pct"] - current["win_rate_pct"]

    print()
    print("=" * 82)
    print("DECISIVE FRESH-DATA COMPARISON")
    print("=" * 82)
    print(f"CURRENT trades:             {current['trades']}")
    print(f"Negative-30D trades:        {negative['trades']}")
    print(f"Positive-30D trades:        {positive['trades']}")
    print(f"PF delta:                   {pf_delta:+.3f}")
    print(f"Expectancy delta:           {exp_delta:+.4f}R")
    print(f"Win-rate delta:             {wr_delta:+.2f} pp")
    print(f"Trades removed:             {current['trades'] - negative['trades']}")

    print()
    print("VALIDATION DECISION")
    print("-" * 82)

    enough = negative["trades"] >= args.min_trades
    improves_pf = pf_delta > 0
    improves_exp = exp_delta > 0

    if enough and improves_pf and improves_exp:
        print("PASS: CCI30_NEGATIVE_30D improves BOTH PF and expectancy")
        print("      on fresh data.")
        print()
        print("NEXT STEP:")
        print("Do NOT tune the threshold yet.")
        print("Run a longer forward/paper validation with the same fixed rule.")
    elif enough and (improves_pf or improves_exp):
        print("MIXED: the rule improves only one primary metric.")
        print("Do NOT change the rule from this result.")
        print("Continue validation or stop the branch.")
    else:
        print("FAIL: the fixed CCI30 rule does not improve both PF and")
        print("      expectancy on fresh data.")
        print("STOP this branch; do not optimize the threshold.")

    print()
    print("IMPORTANT:")
    print("- This test is confirmation of a pre-declared rule, not a new search.")
    print("- The fresh dataset must remain untouched until this run.")
    print("- Do not select another filter because it looks better here.")
    print("- Results are not proof of causality.")
    print("- Do not put the rule into live trading based on this run alone.")

    out_dir = Path("backtest_results")
    out_dir.mkdir(exist_ok=True)

    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S")

    enriched_path = out_dir / f"cci30_fresh_enriched_{stamp}.csv"
    summary_path = out_dir / f"cci30_fresh_summary_{stamp}.csv"

    enriched.to_csv(enriched_path, index=False)

    summary = pd.DataFrame([
        {"model": "CURRENT", **current},
        {"model": "CCI30_NEGATIVE_30D", **negative},
        {"model": "CCI30_POSITIVE_30D", **positive},
    ])
    summary["validation_start"] = valid["_entry_time"].min()
    summary["validation_end"] = valid["_entry_time"].max()
    summary["fresh_cutoff"] = cutoff
    summary.to_csv(summary_path, index=False)

    print()
    print(f"Saved enriched trades: {enriched_path}")
    print(f"Saved summary:         {summary_path}")
    print("=" * 82)


if __name__ == "__main__":
    main()
