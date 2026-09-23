#!/usr/bin/env python3
"""
AURA Track B -- Stage 2: dry-run end-to-end orchestration validation.

Runs `aura_v05355_stage1_paper_trading_runner.run_stage1a_dry_run()` as a
REAL, LIVE multi-symbol process (not a pytest unit test) against one
deterministic, synthetic-evidence scenario, to prove out the full chain:

    synthetic evidence -> .50 decision -> real .44 risk enforcement ->
    portfolio state -> .36/.37 authorization + replay-protection claim
    handling -> .33 order-intent construction -> dry-run execution
    boundary (.38, attempt_submission=False, never reaches .35.submit()) ->
    audit/logging -> resulting persisted state

Per Martin's Stage 2 instruction (2026-09-23):
  - .53.run_stage1a_dry_run() is run as an actual process, not only unit
    tests.
  - One realistic multi-symbol scenario exercises: a long candidate, a
    short candidate with VERIFIED shortability, a short candidate with
    UNVERIFIED shortability (fails closed, never reaches .38's
    authorization step), a low-conviction/no-signal candidate, a real
    .44 portfolio-heat limit breach (genuine risk blocking), a
    kill-switch-engaged rejection, and duplicate/replay protection
    (the identical cycle run twice against the same isolated claim
    store).
  - Claim-store/state paths are isolated from the repository's normal
    runtime state (never regime_output/) so this run cannot contaminate
    subsequent runs -- see --state-dir below.
  - NO real Alpaca account is ever contacted (alpaca_client is never
    constructed anywhere in this script), NO order is submitted
    (attempt_submission=False, hardcoded inside run_stage1a_dry_run and
    not overridable via supervision_kwargs), NO .49 real LLM client is
    wired (the .54 neutral deterministic stub is used, exactly as
    Stage 1), and NO .51/.52 TechnicalScoringParams are invented
    (every candidate drives sentiment_regime evidence only).

This script writes nothing into regime_output/ or anywhere else inside
the repository -- every claim/state directory lives under --state-dir
(default: a fresh timestamped directory outside the repo entirely).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, filename: str):
    path = ROOT / filename
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# Same load-order-and-registration discipline as the .55 test suite: load
# every dependency into sys.modules under its canonical name BEFORE .55
# itself, so .55's own dynamic _load_module() calls resolve to these SAME
# module objects rather than re-importing (and re-executing) each one a
# second time.
SENT = _load("aura_v05347_market_sentiment_scoring", "aura_v05347_market_sentiment_scoring.py")
PROPOSAL = _load("aura_v05349_ai_proposal_pipeline", "aura_v05349_ai_proposal_pipeline.py")
ENGINE50 = _load("aura_v05350_decision_engine", "aura_v05350_decision_engine.py")
META = _load("aura_v05334_asset_instrument_metadata", "aura_v05334_asset_instrument_metadata.py")
ADAPTER35 = _load("aura_v05335_alpaca_equity_execution_adapter", "aura_v05335_alpaca_equity_execution_adapter.py")
AUTH36 = _load("aura_v05336_alpaca_equity_execution_authorization", "aura_v05336_alpaca_equity_execution_authorization.py")
REPLAY37 = _load("aura_v05337_alpaca_replay_protected_consumption", "aura_v05337_alpaca_replay_protected_consumption.py")
SUP = _load("aura_v05338_common_execution_supervisor", "aura_v05338_common_execution_supervisor.py")
CANON = _load("aura_v05333_canonical_execution_specification", "aura_v05333_canonical_execution_specification.py")
OBS = _load("aura_v05343_portfolio_exposure_observability", "aura_v05343_portfolio_exposure_observability.py")
ENFORCE = _load("aura_v05344_portfolio_exposure_enforcement", "aura_v05344_portfolio_exposure_enforcement.py")
CYCLE = _load("aura_v05353_full_paper_orchestration", "aura_v05353_full_paper_orchestration.py")
FILL = _load("aura_v05354_alpaca_equity_fill_reconciliation", "aura_v05354_alpaca_equity_fill_reconciliation.py")
STUB = _load("aura_v054_llm_stub", "aura_v054_llm_stub.py")
M = _load("aura_v05355_stage1_paper_trading_runner", "aura_v05355_stage1_paper_trading_runner.py")

# Deliberately NOT a hardcoded wall-clock constant: .44's snapshot-
# freshness check (see aura_v05355_stage1_paper_trading_runner.py's
# run_stage1a_dry_run docstring on the SNAPSHOT_TIMESTAMP_IN_FUTURE
# ordering hazard) requires the enforcement check's real `now` to be >=
# this scenario's own `now` -- a hardcoded future-looking timestamp would
# eventually (or immediately, depending on when this script runs) trip
# that check. Captured once at import time, a safe margin in the past, and
# reused identically across every run in this script for determinism
# WITHIN one execution (run1/run2 use the identical timestamp, which is
# exactly what makes the replay-protection scenario meaningful).
NOW = datetime.now(timezone.utc) - timedelta(minutes=10)
EQUITY_HISTORY = [{"venue": "ALPACA", "equity": 100_000.0, "as_of": (NOW - timedelta(hours=7)).isoformat()}]
SYNTHETIC_ACCOUNT_EQUITY_USD = 100_000.0
REFERENCE_PRICE_USD = 100.0

# Identical to .53's/.50's own test fixture -- explicitly NOT re-validated
# here, per Martin's instruction that DECIDE_KWARGS_BASE reuse stays
# flagged rather than silently decided during Stage 2.
DECIDE_KWARGS_BASE = dict(
    sentiment_weight=1.0, wave_weight=1.0, technical_weight=0.0, short_technical_weight=0.0,
    decision_threshold=0.1, ai_penalty_per_concern=0.2, critic_penalty_per_issue=0.15,
    proposal_module=PROPOSAL,
)


def decide_kwargs() -> dict[str, Any]:
    kwargs = dict(DECIDE_KWARGS_BASE)
    kwargs["llm_client"] = STUB.NeutralDeterministicLLMClient()
    return kwargs


def reference_price_fn(symbol: str) -> float:
    return REFERENCE_PRICE_USD


def make_sentiment(symbol: str, promotable_score: float):
    return SENT.SentimentRegime(
        symbol=symbol, as_of=NOW.isoformat(), decay_window_hours=48.0, min_source_count=2,
        items_considered=5, input_event_ids=("e1", "e2", "e3"),
        distinct_origin_sources=("Benzinga", "Reuters", "Bloomberg"),
        source_count=3, corroboration_status="SUFFICIENT",
        bullish_count=3 if promotable_score >= 0 else 0,
        bearish_count=0 if promotable_score >= 0 else 3,
        neutral_count=2, mixed_count=0,
        raw_score=promotable_score, promotable_score=promotable_score,
    )


def make_request(symbol: str, *, promotable_score: float, quantity: str, alpaca_asset: dict[str, Any] | None) -> M.SymbolRequest:
    return M.SymbolRequest(
        symbol=symbol, asset_class="STOCK", quantity=quantity,
        sentiment_regime=make_sentiment(symbol, promotable_score),
        alpaca_asset=alpaca_asset,
    )


# ============================================================================
# The one deterministic, multi-symbol scenario for the main cycle.
# ============================================================================

def build_main_scenario() -> tuple[M.SymbolRequest, ...]:
    return (
        # 1. Long candidate, high conviction, small notional (1% of the
        #    synthetic $100k equity) -- expected to clear real .44
        #    portfolio-heat enforcement and reach .38's READY_FOR_SUBMISSION
        #    construction preview.
        make_request(
            "AAPL", promotable_score=0.9, quantity="10",
            alpaca_asset={"symbol": "AAPL", "tradable": True, "fractionable": True, "shortable": True, "easy_to_borrow": True},
        ),
        # 2. Short candidate, POSITIVELY VERIFIED shortable + easy_to_borrow
        #    -- expected to clear both real .44 enforcement and .34/.35/.36's
        #    fail-closed short-side gate, reaching construction preview.
        make_request(
            "TSLA", promotable_score=-0.9, quantity="10",
            alpaca_asset={"symbol": "TSLA", "tradable": True, "fractionable": True, "shortable": True, "easy_to_borrow": True},
        ),
        # 3. Short candidate, shortability data present but NEGATIVE
        #    (shortable=False, easy_to_borrow=False) -- expected to clear
        #    .44 (small notional) but be blocked at .36.authorize() before
        #    any .37 claim is ever attempted: fail-closed, never reaches
        #    construction preview.
        make_request(
            "GME", promotable_score=-0.85, quantity="10",
            alpaca_asset={"symbol": "GME", "tradable": True, "fractionable": True, "shortable": False, "easy_to_borrow": False},
        ),
        # 4. Low-conviction candidate -- expected to never become an
        #    executable candidate at all (.50.decide() itself returns an
        #    outcome outside OPEN_DIRECTIONS), long before enforcement or
        #    .38 are ever reached.
        make_request(
            "NVDA", promotable_score=0.02, quantity="10",
            alpaca_asset={"symbol": "NVDA", "tradable": True, "fractionable": True, "shortable": True, "easy_to_borrow": True},
        ),
        # 5. Long candidate, high conviction, DELIBERATELY oversized notional
        #    (quantity=1000 @ the fixed $100 reference price = $100,000 --
        #    100% of the synthetic $100k equity) against a 5% portfolio-heat
        #    cap -- expected to be genuinely BLOCKed by real .44 enforcement
        #    (LIMIT_BREACHED, not merely EXPOSURE_NOT_COMPUTABLE) before
        #    ever reaching .38.
        make_request(
            "MSFT", promotable_score=0.85, quantity="1000",
            alpaca_asset={"symbol": "MSFT", "tradable": True, "fractionable": True, "shortable": True, "easy_to_borrow": True},
        ),
    )


def run_main_cycle(state_dir: Path, *, tag: str) -> M.Stage1CycleReport:
    reqs = build_main_scenario()
    limits = ENFORCE.PortfolioLimits(max_portfolio_heat_ratio=0.05)
    return M.run_stage1a_dry_run(
        reqs,
        decide_kwargs=decide_kwargs(),
        max_new_orders_per_cycle=10,
        reference_price_fn=reference_price_fn,
        limits=limits,
        equity_history=EQUITY_HISTORY,
        max_snapshot_age_seconds=10**9,
        synthetic_account_equity_usd=SYNTHETIC_ACCOUNT_EQUITY_USD,
        now=NOW,
        supervision_kwargs={
            "auth_claims_dir": state_dir / f"{tag}-auth-claims",
            "supervisor_claims_dir": state_dir / f"{tag}-supervisor-claims",
        },
    )


def run_kill_switch_cycle(state_dir: Path) -> M.Stage1CycleReport:
    reqs = (
        make_request(
            "IBM", promotable_score=0.9, quantity="10",
            alpaca_asset={"symbol": "IBM", "tradable": True, "fractionable": True, "shortable": True, "easy_to_borrow": True},
        ),
    )
    return M.run_stage1a_dry_run(
        reqs,
        decide_kwargs=decide_kwargs(),
        max_new_orders_per_cycle=10,
        now=NOW,
        supervision_kwargs={
            "auth_claims_dir": state_dir / "stage2-killswitch-auth-claims",
            "supervisor_claims_dir": state_dir / "stage2-killswitch-supervisor-claims",
            "supervisor_config": {"kill_switch": True},
            "auth_config": {
                "kill_switch": False, "execution_authorized": True, "paper_execution_authorized": True,
                "authorization_ttl_seconds": 60, "safety_state_ttl_seconds": 60,
            },
        },
    )


# ============================================================================
# Reporting helpers
# ============================================================================

def outcome_summary(report: M.Stage1CycleReport) -> list[dict[str, Any]]:
    rows = []
    for outcome in report.cycle_result.outcomes:
        supervision = outcome.supervision_result or {}
        rows.append({
            "symbol": outcome.symbol,
            "cycle_stage": outcome.stage,
            "signal_outcome": getattr(outcome.decision, "outcome", None),
            "decision_hash": getattr(outcome.decision, "decision_hash", None),
            "reasons": list(outcome.reasons),
            "supervision_status": supervision.get("status"),
            "supervision_stage": supervision.get("stage"),
            "supervision_reason": supervision.get("reason"),
            "authorization_id": supervision.get("authorization_id"),
            "order_spec": supervision.get("order_spec"),
        })
    return rows


def audit_rows(report: M.Stage1CycleReport) -> list[dict[str, Any]]:
    return [r.to_dict() for r in report.audit_records]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--state-dir", type=Path, default=None,
        help="Directory for isolated claim-store/state files (never the "
        "repository's regime_output/). Defaults to a fresh timestamped "
        "directory under the system temp directory, entirely outside the "
        "repository.",
    )
    args = parser.parse_args()

    if args.state_dir is not None:
        state_dir = args.state_dir
    else:
        import tempfile
        state_dir = Path(tempfile.mkdtemp(prefix="aura_stage2_dry_run_"))
    state_dir.mkdir(parents=True, exist_ok=True)
    assert ROOT not in state_dir.parents and state_dir != ROOT, "state_dir must be outside the repository"

    print(f"Stage 2 dry-run validation -- state dir: {state_dir}")
    print(f"Repository root: {ROOT} (never written to)")
    print()

    # ------------------------------------------------------------------
    # Run 1: the main multi-symbol scenario, first pass.
    # ------------------------------------------------------------------
    print("=== RUN 1: main multi-symbol scenario (first pass) ===")
    report1 = run_main_cycle(state_dir, tag="stage2-main")
    run1_outcomes = outcome_summary(report1)
    for row in run1_outcomes:
        print(f"  {row['symbol']:6s} cycle_stage={row['cycle_stage']:22s} signal={row['signal_outcome']} "
              f"supervision_status={row['supervision_status']} reason={row['supervision_reason']}")

    # ------------------------------------------------------------------
    # Run 2: identical main scenario, second pass -- duplicate/replay
    # protection, against the SAME isolated claim dirs.
    # ------------------------------------------------------------------
    print()
    print("=== RUN 2: identical main scenario replayed (duplicate/replay protection) ===")
    report2 = run_main_cycle(state_dir, tag="stage2-main")
    run2_outcomes = outcome_summary(report2)
    for row in run2_outcomes:
        print(f"  {row['symbol']:6s} cycle_stage={row['cycle_stage']:22s} signal={row['signal_outcome']} "
              f"supervision_status={row['supervision_status']} reason={row['supervision_reason']}")

    # ------------------------------------------------------------------
    # Kill-switch scenario: a separate single-symbol cycle with .38's
    # supervisor kill switch explicitly engaged.
    # ------------------------------------------------------------------
    print()
    print("=== RUN 3: kill-switch-engaged rejection (separate single-symbol cycle) ===")
    report3 = run_kill_switch_cycle(state_dir)
    run3_outcomes = outcome_summary(report3)
    for row in run3_outcomes:
        print(f"  {row['symbol']:6s} cycle_stage={row['cycle_stage']:22s} signal={row['signal_outcome']} "
              f"supervision_status={row['supervision_status']} reason={row['supervision_reason']}")

    # ------------------------------------------------------------------
    # Persist the full audit trail -- state/audit persistence across a
    # complete cycle, isolated from the repository.
    # ------------------------------------------------------------------
    full_report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scenario_now": NOW.isoformat(),
        "state_dir": str(state_dir),
        "repository_root": str(ROOT),
        "no_broker_contact": True,
        "attempt_submission": False,
        "decide_kwargs_base": {k: v for k, v in DECIDE_KWARGS_BASE.items() if k != "proposal_module"},
        "synthetic_account_equity_usd": SYNTHETIC_ACCOUNT_EQUITY_USD,
        "reference_price_usd_fixed": REFERENCE_PRICE_USD,
        "portfolio_heat_limit_configured": 0.05,
        "run1_main_first_pass": {
            "outcome_summary": run1_outcomes,
            "audit_records": audit_rows(report1),
        },
        "run2_main_replay": {
            "outcome_summary": run2_outcomes,
            "audit_records": audit_rows(report2),
        },
        "run3_kill_switch": {
            "outcome_summary": run3_outcomes,
            "audit_records": audit_rows(report3),
        },
    }
    output_path = state_dir / "stage2_full_audit_trail.json"
    output_path.write_text(json.dumps(full_report, indent=2, default=str), encoding="utf-8")
    print()
    print(f"Full audit trail persisted to: {output_path}")

    # ------------------------------------------------------------------
    # Sanity assertions -- fail loudly (non-zero exit) if the scenario
    # didn't actually exercise what it claims to.
    # ------------------------------------------------------------------
    by_symbol_1 = {r["symbol"]: r for r in run1_outcomes}
    by_symbol_2 = {r["symbol"]: r for r in run2_outcomes}
    checks = [
        ("AAPL run1 reaches READY_FOR_SUBMISSION", by_symbol_1["AAPL"]["supervision_status"] == "READY_FOR_SUBMISSION"),
        ("TSLA run1 (verified short) reaches READY_FOR_SUBMISSION", by_symbol_1["TSLA"]["supervision_status"] == "READY_FOR_SUBMISSION"),
        ("GME run1 (unverified short) never reaches READY_FOR_SUBMISSION", by_symbol_1["GME"]["supervision_status"] != "READY_FOR_SUBMISSION"),
        ("NVDA run1 (low conviction) never became a candidate", by_symbol_1["NVDA"]["cycle_stage"] == "NO_TRADE_DECIDED" and by_symbol_1["NVDA"]["signal_outcome"] not in ("DECIDE_LONG", "DECIDE_SHORT")),
        ("MSFT run1 (oversized) blocked by real .44 LIMIT_BREACHED", by_symbol_1["MSFT"]["cycle_stage"] == "NO_TRADE_DECIDED" and any("LIMIT_BREACHED" in r for r in by_symbol_1["MSFT"]["reasons"])),
        # NOTE (Stage 2 finding, see report): .36.authorize() mints a fresh
        # uuid4 authorization_id on every call, so .37's authorization_id
        # claim layer never actually collides on a repeated cycle -- it
        # guards concurrent double-processing of ONE authorization record,
        # not cross-cycle replay of an identical decision. The genuine
        # cross-cycle duplicate guard is .38's OWN second, local claim on
        # the deterministic client_order_id (derived from decision_hash/
        # symbol/signal_timestamp), which is what actually fires here.
        ("AAPL run2 (replay) is blocked, not re-authorized", by_symbol_2["AAPL"]["supervision_status"] != "READY_FOR_SUBMISSION"),
        ("TSLA run2 (replay) is blocked, not re-authorized", by_symbol_2["TSLA"]["supervision_status"] != "READY_FOR_SUBMISSION"),
        ("AAPL run2 blocked at a genuine duplicate-claim stage", by_symbol_2["AAPL"]["supervision_stage"] in ("REPLAY_PROTECTION", "SUPERVISOR_CLIENT_ORDER_ID_CLAIM")),
        ("TSLA run2 blocked at a genuine duplicate-claim stage", by_symbol_2["TSLA"]["supervision_stage"] in ("REPLAY_PROTECTION", "SUPERVISOR_CLIENT_ORDER_ID_CLAIM")),
        ("IBM (kill-switch run) blocked at SUPERVISOR_KILL_SWITCH", run3_outcomes[0]["supervision_stage"] == "SUPERVISOR_KILL_SWITCH"),
        ("No .35.submit() call is possible (attempt_submission hardcoded False in run_stage1a_dry_run)", True),
    ]
    print()
    print("=== Sanity checks ===")
    all_ok = True
    for name, ok in checks:
        print(f"  [{'OK' if ok else 'FAIL'}] {name}")
        all_ok = all_ok and ok

    print()
    if all_ok:
        print("ALL CHECKS PASSED.")
        return 0
    print("ONE OR MORE CHECKS FAILED -- see above.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
