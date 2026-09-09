#!/usr/bin/env python3
"""Static contract checks for the v0.5.3.21 Alpaca paper runtime."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = (ROOT / "aura_v05321_alpaca_paper_runtime.py").read_text(encoding="utf-8")
DATA = (ROOT / "aura_v05321_alpaca_market_data.py").read_text(encoding="utf-8")

checks = [
    ("runtime calls Alpaca data adapter", "aura_v05321_alpaca_market_data.py" in RUNTIME),
    ("runtime includes .12-.17", all(f"aura_v053{n}" in RUNTIME for n in ("12", "13", "14", "15", "16", "17"))),
    ("runtime includes .20 simulator", "aura_v05320_paper_fill_simulator.py" in RUNTIME),
    ("runtime includes .18 reconciliation", "aura_v05318_reconciliation.py" in RUNTIME),
    ("runtime includes .19 safety", "aura_v05319_execution_safety.py" in RUNTIME),
    ("runtime hard-blocks live execution", '"live_execution": False' in RUNTIME),
    ("data adapter uses paper credential names", "ALPACA_PAPER_API_KEY" in DATA and "ALPACA_PAPER_SECRET_KEY" in DATA),
    ("data adapter requires frozen warmup", "args.bars < 204" in DATA and "BARS = 240" in DATA),
    ("data adapter follows Alpaca pagination", "next_page_token" in DATA and "page_token" in DATA),
    ("data adapter excludes current hour", 'df["timestamp"] < end' in DATA),
    ("data adapter enforces exact per-symbol bar count", "final bar count mismatch" in DATA),
    ("data adapter never submits orders", "never submits an order" in DATA),
    ("runtime includes .25 position sizing", "aura_v05325_position_sizing.py" in RUNTIME),
    ("runtime includes .23 execution specification", "aura_v05323_execution_specification_builder.py" in RUNTIME),
    ("runtime includes .26 paper order submission glue", "aura_v05326_paper_order_submission.py" in RUNTIME),
    ("runtime exposes --submit-paper, off by default (store_true)", 'p.add_argument("--submit-paper", action="store_true"' in RUNTIME),
    ("runtime forwards --submit-paper only when requested", "if submit_paper:" in RUNTIME),
    ("runtime still hard-blocks live execution after Phase 5 tail", '"live_execution": False' in RUNTIME),
]

failed = 0
for name, ok in checks:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    failed += not ok

print(f"PASSED : {len(checks)-failed}")
print(f"FAILED : {failed}")
print(f"RESULT : {'PASS' if failed == 0 else 'FAIL'}")
raise SystemExit(1 if failed else 0)
