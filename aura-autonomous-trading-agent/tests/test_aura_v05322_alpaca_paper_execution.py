import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "aura_v05322_alpaca_paper_execution.py"


def run_spec(tmp_path, spec, *extra, env=None):
    inp = tmp_path / "spec.json"
    out = tmp_path / "out.json"
    inp.write_text(json.dumps(spec), encoding="utf-8")
    e = os.environ.copy()
    if env:
        e.update(env)
    p = subprocess.run([sys.executable, str(ADAPTER), "--input", str(inp), "--output", str(out), *extra], cwd=ROOT, env=e, capture_output=True, text=True)
    return p, json.loads(out.read_text(encoding="utf-8"))


def valid_spec():
    return {"exchange":"ALPACA","account_mode":"PAPER","live_execution":False,"execution_authorized":True,"paper_execution_authorized":True,"kill_switch_active":False,"symbol":"BTC/USD","side":"BUY","quantity":0.001,"order_type":"MARKET","time_in_force":"GTC","client_order_id":"aura-test-001"}


def test_validation_does_not_submit(tmp_path):
    p, out = run_spec(tmp_path, valid_spec())
    assert p.returncode == 0
    assert out["validation_passed"] is True
    assert out["order_submitted"] is False


def test_live_is_rejected(tmp_path):
    s = valid_spec(); s["live_execution"] = True
    p, out = run_spec(tmp_path, s)
    assert p.returncode != 0
    assert "LIVE_EXECUTION_NOT_FALSE" in out["blocked_reasons"]


def test_kill_switch_rejects(tmp_path):
    s = valid_spec(); s["kill_switch_active"] = True
    p, out = run_spec(tmp_path, s)
    assert p.returncode != 0
    assert "KILL_SWITCH_ACTIVE" in out["blocked_reasons"]


def test_missing_quantity_rejects(tmp_path):
    s = valid_spec(); del s["quantity"]
    p, out = run_spec(tmp_path, s)
    assert p.returncode != 0
    assert "INVALID_QUANTITY" in out["blocked_reasons"]


def test_submission_requires_feature_flag(tmp_path):
    p, out = run_spec(tmp_path, valid_spec(), "--submit-paper")
    assert p.returncode != 0
    assert out["order_submitted"] is False
    assert "PAPER_SUBMISSION_FEATURE_FLAG_NOT_ENABLED" in out["blocked_reasons"]
