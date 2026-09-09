"""
AURA v0.5.1 — Crypto Path & Signal Engine
Experiment C0 instrumentation / H-C1 Path observation layer

RESEARCH ONLY. NO ORDER SUBMISSION.

Purpose
-------
- Preserve the AURA v0.5.0 C0 hypothesis exactly as the control.
- Build an immutable Signal Master from the already-fetched C0 bars.
- Track the early path of each eligible C0 trade at held bars 1, 2 and 3.
- Add passive Open Interest (OI) diagnostics only when genuine historical OI
  observations are supplied. Missing OI is left NaN; it is NEVER fabricated.
- Produce a clean dataset for later out-of-sample testing of H-C1-Path.

Frozen C0 control
-----------------
Signal: EMA3/EMA8 bullish crossover + MACD histogram > 0 + RelVol >= 1
Entry : next 1H bar OPEN after confirmed signal
Exit  : CLOSE of the 10th complete held 1H bar
Long  : 1x notional
Friction: 5 bp entry slippage + 1 bp entry fee + 5 bp exit slippage + 1 bp exit fee

Important
---------
This version does NOT implement an early exit, threshold, time filter, OI filter,
or any other trading decision. Early MFE/MAE and OI are diagnostics only.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Config:
    version: str = "AURA_v0.5.1"
    experiment: str = "C0_PATH_INSTRUMENTATION"
    signal_definition: str = (
        "EMA3/EMA8 bullish crossover + MACD histogram > 0 + RelVol >= 1"
    )
    hold_bars: int = 10
    early_path_bars: tuple[int, ...] = (1, 2, 3)
    fee_bps: float = 1.0
    slippage_bps: float = 5.0
    leverage: float = 1.0
    execution: str = "NONE"


def read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Missing required file: {path}")
    return pd.read_csv(path)


def parse_utc(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, utc=True, errors="coerce")


def safe_float(v) -> float:
    try:
        return float(v)
    except Exception:
        return np.nan


def load_oi(path: Path | None) -> pd.DataFrame:
    """Load genuine historical OI data if supplied; never synthesize it."""
    if path is None:
        return pd.DataFrame(columns=["symbol", "timestamp", "open_interest"])

    oi = read_csv(path)
    required = {"symbol", "timestamp", "open_interest"}
    missing = required - set(oi.columns)
    if missing:
        raise ValueError(
            f"OI file must contain {sorted(required)}; missing {sorted(missing)}"
        )
    oi = oi.copy()
    oi["timestamp"] = parse_utc(oi["timestamp"])
    oi["open_interest"] = pd.to_numeric(oi["open_interest"], errors="coerce")
    oi = oi.dropna(subset=["symbol", "timestamp", "open_interest"])
    oi = oi.drop_duplicates(["symbol", "timestamp"]).sort_values(
        ["symbol", "timestamp"]
    )
    return oi.reset_index(drop=True)


def attach_passive_oi(
    snapshots: pd.DataFrame,
    oi: pd.DataFrame,
) -> pd.DataFrame:
    """Backward as-of join: OI may only come from an observation at/before the bar."""
    if snapshots.empty:
        return snapshots

    out = snapshots.copy()
    out["snapshot_ts"] = parse_utc(out["snapshot_timestamp"])
    out["open_interest"] = np.nan
    out["oi_change_1bar"] = np.nan
    out["oi_change_pct_1bar"] = np.nan
    out["oi_acceleration"] = np.nan

    if oi.empty:
        out = out.drop(columns=["snapshot_ts"])
        return out

    pieces = []
    for symbol, g in out.groupby("symbol", sort=False):
        o = oi[oi["symbol"] == symbol].copy()
        if o.empty:
            pieces.append(g)
            continue
        g = g.sort_values("snapshot_ts").copy()
        o = o.sort_values("timestamp").copy()
        m = pd.merge_asof(
            g,
            o[["timestamp", "open_interest"]],
            left_on="snapshot_ts",
            right_on="timestamp",
            direction="backward",
        )
        m = m.drop(columns=["timestamp"])
        m["oi_change_1bar"] = m["open_interest"].diff()
        m["oi_change_pct_1bar"] = m["open_interest"].pct_change()
        # Acceleration = change in OI change. Purely descriptive.
        m["oi_acceleration"] = m["oi_change_1bar"].diff()
        pieces.append(m)

    out = pd.concat(pieces, ignore_index=True)
    out = out.sort_values(["trade_id", "held_bar"]).drop(columns=["snapshot_ts"])
    return out.reset_index(drop=True)


def funding_for_trade(
    funding: pd.DataFrame,
    symbol: str,
    entry_time: pd.Timestamp,
    exit_time: pd.Timestamp,
) -> float:
    if funding.empty:
        return 0.0

    m = funding[
        (funding["symbol"] == symbol)
        & (funding["settle_time"] > entry_time)
        & (funding["settle_time"] <= exit_time)
    ]

    return float(-m["funding_rate"].sum()) if not m.empty else 0.0


def build_signal_master(
    bars: pd.DataFrame,
    config: Config,
) -> pd.DataFrame:
    rows = []
    for symbol, g in bars.groupby("symbol", sort=False):
        g = g.sort_values("timestamp").reset_index(drop=True)
        for i, r in g.iterrows():
            if not bool(r.get("signal", False)):
                continue
            entry_i = i + 1
            valid_entry = entry_i < len(g)
            rows.append(
                {
                    "signal_id": f"AURA51-{symbol}-{i:06d}",
                    "experiment": config.experiment,
                    "symbol": symbol,
                    "decision_timestamp": pd.Timestamp(r["timestamp"]).isoformat(),
                    "entry_timestamp": (
                        pd.Timestamp(g.iloc[entry_i]["timestamp"]).isoformat()
                        if valid_entry else ""
                    ),
                    "signal_close": safe_float(r["close"]),
                    "ema_3": safe_float(r.get("ema_3")),
                    "ema_8": safe_float(r.get("ema_8")),
                    "macd_hist": safe_float(r.get("macd_hist")),
                    "rel_volume": safe_float(r.get("rel_volume")),
                    "atr_14": safe_float(r.get("atr_14")),
                    "signal_confirmed": True,
                    "entry_data_available": valid_entry,
                    "path_hypothesis_active": False,
                    "notes": "Immutable observation; no trading decision added.",
                }
            )
    return pd.DataFrame(rows)


def simulate_c0_with_path(
    bars: pd.DataFrame,
    funding: pd.DataFrame,
    config: Config,
    signal_master: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    trades = []
    snapshots = []

    for symbol, x in bars.groupby("symbol", sort=False):
        x = x.sort_values("timestamp").reset_index(drop=True)
        signals = x.index[x["signal"].fillna(False)].tolist()
        i_ptr = 0
        trade_no = 0

        while i_ptr < len(signals):
            i = signals[i_ptr]
            entry_i = i + 1
            exit_i = entry_i + config.hold_bars - 1
            if exit_i >= len(x):
                break

            # C0's non-overlapping control behavior is retained exactly.
            if i_ptr > 0:
                prev_signal_i = signals[i_ptr - 1]
                prev_entry = prev_signal_i + 1
                prev_exit = prev_entry + config.hold_bars - 1
                if entry_i <= prev_exit:
                    i_ptr += 1
                    continue

            signal_bar = x.iloc[i]
            entry_bar = x.iloc[entry_i]
            exit_bar = x.iloc[exit_i]

            entry_raw = float(entry_bar["open"])
            entry_exec = entry_raw * (1 + config.slippage_bps / 10000)
            exit_raw = float(exit_bar["close"])
            exit_exec = exit_raw * (1 - config.slippage_bps / 10000)
            price_return = exit_exec / entry_exec - 1
            commission_return = -2 * config.fee_bps / 10000
            funding_return = funding_for_trade(
                funding,
                symbol,
                pd.Timestamp(entry_bar["timestamp"]),
                pd.Timestamp(exit_bar["timestamp"]),
            )
            net_return = price_return + commission_return + funding_return

            path = x.iloc[entry_i : exit_i + 1].copy()
            trade_no += 1
            trade_id = f"C0-{symbol}-{trade_no:05d}"

            row = {
                "trade_id": trade_id,
                "signal_id": f"AURA51-{symbol}-{i:06d}",
                "symbol": symbol,
                "signal_timestamp": pd.Timestamp(signal_bar["timestamp"]).isoformat(),
                "entry_timestamp": pd.Timestamp(entry_bar["timestamp"]).isoformat(),
                "exit_timestamp": pd.Timestamp(exit_bar["timestamp"]).isoformat(),
                "entry_price_raw": entry_raw,
                "entry_price_exec": entry_exec,
                "exit_price_raw": exit_raw,
                "exit_price_exec": exit_exec,
                "price_return": price_return,
                "commission_return": commission_return,
                "funding_return": funding_return,
                "net_return": net_return,
                "mfe": float(path["high"].max() / entry_exec - 1),
                "mae": float(path["low"].min() / entry_exec - 1),
                "hold_bars": config.hold_bars,
                "exit_reason": "TIME_10H_CLOSE",
            }

            # Frozen C0 trade-level outputs plus explicit early-path measurements.
            for n in config.early_path_bars:
                p = path.iloc[:n]
                if len(p) < n:
                    row[f"bar_{n}_mfe"] = np.nan
                    row[f"bar_{n}_mae"] = np.nan
                    row[f"bar_{n}_close_return_before_costs"] = np.nan
                else:
                    row[f"bar_{n}_mfe"] = float(p["high"].max() / entry_exec - 1)
                    row[f"bar_{n}_mae"] = float(p["low"].min() / entry_exec - 1)
                    row[f"bar_{n}_close_return_before_costs"] = float(
                        p.iloc[-1]["close"] / entry_exec - 1
                    )
            trades.append(row)

            for held_bar, (_, bar) in enumerate(path.iterrows(), start=1):
                p = path.iloc[:held_bar]
                snapshots.append(
                    {
                        "trade_id": trade_id,
                        "signal_id": row["signal_id"],
                        "symbol": symbol,
                        "snapshot_timestamp": pd.Timestamp(bar["timestamp"]).isoformat(),
                        "held_bar": held_bar,
                        "mark_price": float(bar["close"]),
                        "path_return_before_costs": float(bar["close"] / entry_exec - 1),
                        "mfe_to_date": float(p["high"].max() / entry_exec - 1),
                        "mae_to_date": float(p["low"].min() / entry_exec - 1),
                        "funding_return_to_date": funding_for_trade(
                            funding,
                            symbol,
                            pd.Timestamp(entry_bar["timestamp"]),
                            pd.Timestamp(bar["timestamp"]),
                        ),
                    }
                )

            # Advance exactly as C0: no overlapping positions on same symbol.
            while i_ptr < len(signals) and signals[i_ptr] <= exit_i - 1:
                i_ptr += 1
            i_ptr += 1

    return pd.DataFrame(trades), pd.DataFrame(snapshots)


def summarize(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame([{"experiment": "C0", "trades": 0}])
    r = pd.to_numeric(trades["net_return"], errors="coerce").dropna()
    gains = r[r > 0].sum()
    losses = -r[r < 0].sum()
    pf = gains / losses if losses else np.nan
    equity = (1 + r).cumprod()
    dd = equity / equity.cummax() - 1
    return pd.DataFrame([{
        "experiment": "C0_PATH_INSTRUMENTATION",
        "trades": len(trades),
        "symbols": trades["symbol"].nunique(),
        "net_mean": r.mean(),
        "net_median": r.median(),
        "win_rate": (r > 0).mean(),
        "profit_factor": pf,
        "max_drawdown": dd.min(),
        "mean_mfe": pd.to_numeric(trades["mfe"], errors="coerce").mean(),
        "mean_mae": pd.to_numeric(trades["mae"], errors="coerce").mean(),
        "mean_bar_1_mfe": pd.to_numeric(trades["bar_1_mfe"], errors="coerce").mean(),
        "mean_bar_1_mae": pd.to_numeric(trades["bar_1_mae"], errors="coerce").mean(),
        "mean_bar_2_mfe": pd.to_numeric(trades["bar_2_mfe"], errors="coerce").mean(),
        "mean_bar_2_mae": pd.to_numeric(trades["bar_2_mae"], errors="coerce").mean(),
        "mean_bar_3_mfe": pd.to_numeric(trades["bar_3_mfe"], errors="coerce").mean(),
        "mean_bar_3_mae": pd.to_numeric(trades["bar_3_mae"], errors="coerce").mean(),
    }])


def main() -> None:
    p = argparse.ArgumentParser(description="AURA v0.5.1 Crypto Path & Signal Engine")
    p.add_argument("--input", default="AURA_CRYPTO")
    p.add_argument("--output", default="AURA_CRYPTO_V051")
    p.add_argument("--oi-file", default=None,
                   help="Optional genuine historical OI CSV: symbol,timestamp,open_interest")
    args = p.parse_args()

    config = Config()
    src = Path(args.input)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)

    bars = read_csv(src / "bars_1h.csv")
    funding = read_csv(src / "funding_history.csv")
    bars["timestamp"] = parse_utc(bars["timestamp"])
    funding["settle_time"] = parse_utc(funding["settle_time"])
    bars = bars.sort_values(["symbol", "timestamp"]).reset_index(drop=True)
    funding = funding.sort_values(["symbol", "settle_time"]).reset_index(drop=True)

    oi = load_oi(Path(args.oi_file) if args.oi_file else None)
    signal_master = build_signal_master(bars, config)
    trades, snapshots = simulate_c0_with_path(bars, funding, config, signal_master)
    snapshots = attach_passive_oi(snapshots, oi)
    summary = summarize(trades)

    # Explicitly document that the early path and OI fields are observational only.
    run = {
        "config": asdict(config),
        "source": str(src),
        "output": str(out),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "research_status": "C0_INSTRUMENTED_C1_PATH_DATASET",
        "execution": "NONE",
        "oi_source": str(args.oi_file) if args.oi_file else None,
        "oi_status": "PASSIVE_ONLY_WITH_GENUINE_INPUT" if args.oi_file else "NOT_SUPPLIED",
        "guardrails": [
            "C0 signal definition unchanged.",
            "Entry and 10H close exit unchanged.",
            "No early-path threshold or exit rule is implemented.",
            "OI is never synthesized or backfilled from current ticker data.",
            "All OI joins are backward/as-of to prevent lookahead.",
        ],
        "research_hypothesis": (
            "H-C1-Path: early MFE/MAE may contain information about final C0 trade quality; "
            "this version records the path but does not act on it."
        ),
    }

    # Make the immutable signal master the primary artifact.
    signal_master.to_csv(out / "signal_master.csv", index=False)
    trades.to_csv(out / "trades_c0.csv", index=False)
    snapshots.to_csv(out / "position_snapshots_c0.csv", index=False)
    summary.to_csv(out / "daily_model_metrics.csv", index=False)

    # A compact analysis-ready table: one row/trade, early path + final outcome.
    early_cols = [
        "trade_id", "signal_id", "symbol", "signal_timestamp", "entry_timestamp",
        "bar_1_mfe", "bar_1_mae", "bar_1_close_return_before_costs",
        "bar_2_mfe", "bar_2_mae", "bar_2_close_return_before_costs",
        "bar_3_mfe", "bar_3_mae", "bar_3_close_return_before_costs",
        "mfe", "mae", "net_return", "funding_return", "exit_reason",
    ]
    if not trades.empty:
        trades[early_cols].to_csv(out / "early_path_diagnostics.csv", index=False)
    else:
        pd.DataFrame(columns=early_cols).to_csv(out / "early_path_diagnostics.csv", index=False)

    with open(out / "research_run.json", "w", encoding="utf-8") as f:
        json.dump(run, f, indent=2)

    print("=" * 78)
    print("AURA v0.5.1 — CRYPTO PATH & SIGNAL ENGINE")
    print("=" * 78)
    print("MODE        : RESEARCH ONLY — NO ORDERS")
    print("CONTROL     : C0 FROZEN")
    print("PATH        : MFE/MAE at held bars 1 / 2 / 3")
    print("OI          : PASSIVE ONLY — NEVER SYNTHESIZED")
    print()
    print(f"Signals     : {len(signal_master)}")
    print(f"C0 trades   : {len(trades)}")
    print(f"OI rows     : {len(oi)}")
    print()
    if not summary.empty:
        print(summary.to_string(index=False))
    print()
    print("Files:")
    for name in [
        "signal_master.csv",
        "trades_c0.csv",
        "position_snapshots_c0.csv",
        "early_path_diagnostics.csv",
        "daily_model_metrics.csv",
        "research_run.json",
    ]:
        print(f"  {out / name}")
    print()
    print("AURA v0.5.1 COMPLETE — NO STRATEGY CHANGE")


if __name__ == "__main__":
    main()
