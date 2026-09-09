import json
from pathlib import Path

import pandas as pd

from alpaca_c0_dashboard import build_dashboard


def test_dashboard_smoke(tmp_path: Path):
    inp = tmp_path / "run"
    out = tmp_path / "dashboard"
    inp.mkdir()
    (inp / "summary.json").write_text(
        json.dumps({
            "trades": 2,
            "win_rate": 0.5,
            "net_mean": 0.001,
            "profit_factor": 1.2,
            "max_drawdown": -0.02,
            "mean_mfe": 0.01,
            "mean_mae": -0.005,
            "funding_model": "SPOT_NONE",
        }),
        encoding="utf-8",
    )
    pd.DataFrame([
        {
            "trade_id": "1", "symbol": "BTC/USD",
            "entry_timestamp": "2026-01-01T00:00:00Z",
            "exit_timestamp": "2026-01-01T10:00:00Z",
            "net_return": 0.01, "mfe": 0.02, "mae": -0.01,
            "funding_return": 0.0, "hold_bars": 10,
            "exit_reason": "TIME_10H_CLOSE",
        },
        {
            "trade_id": "2", "symbol": "ETH/USD",
            "entry_timestamp": "2026-01-02T00:00:00Z",
            "exit_timestamp": "2026-01-02T10:00:00Z",
            "net_return": -0.008, "mfe": 0.005, "mae": -0.02,
            "funding_return": 0.0, "hold_bars": 10,
            "exit_reason": "TIME_10H_CLOSE",
        },
    ]).to_csv(inp / "trades_c0.csv", index=False)

    path = build_dashboard(inp, out)
    text = path.read_text(encoding="utf-8")
    assert "AURA Alpaca C0 Backtest Dashboard" in text
    assert "BTC/USD" in text and "ETH/USD" in text
    assert "No orders are placed" in text
