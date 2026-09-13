#!/usr/bin/env python3
"""
AURA v0.5.3.53 — Full paper orchestration.

`.53` is the FIRST module in this project's `.46`-`.52` (evidence/decision)
lineage that ties evidence gathering, `.50`'s Decision Engine, and `.33`-
`.38`'s deterministic execution spine into one multi-symbol CYCLE: for a
set of caller-supplied symbols and their already-gathered evidence, decide
each one via `.50`, rank and cap the resulting entries, and hand approved
entries to `.38`'s already-existing, already-tested Alpaca equity
supervisor — in PAPER mode only, exactly as `.35`/`.38` already require.

Architecture:

    Per-symbol evidence (`.47`/`.48`/`.51`/`.52`, caller-gathered)
             |
    `.50` Decision Engine (unmodified, called once per symbol)
             |
    `.53` Cycle Orchestration (THIS module: rank, cap, gate)
             |
    `.33` Canonical Execution Specification (unmodified)
             |
    `.38` Common Execution Supervisor (unmodified, PAPER only)

`.53` adds no new scoring logic, no new execution logic, and no new risk
math -- every constant it needs (weights, thresholds, penalties, the
per-cycle new-order cap) is REQUIRED, no-default, caller-supplied, exactly
matching every prior milestone's "never invent numbers" discipline. `.53`'s
own, genuinely new contribution is narrow and specific: the loop/ranking/
cap/gating logic that decides WHICH of several simultaneously-eligible
candidates actually reach `.38` in one cycle, and the "don't re-evaluate a
symbol we already hold" skip rule.

Reuse-first audit (dedicated subagent pass, read-only, file:line cited,
covering Martin's full mandatory checklist for `.53`: DELTAX/DELTAX_v2,
Hermes Trader, Aegis/AegisAlpha, `mexc_bot`, existing AURA `.01`-`.52`,
existing MEXC execution/reconciliation code, and AURA's own already-built
orchestration/persistence precedent)
------------------------------------------------------------------------
Formal classification: A = direct reuse. B = adapt/extract. C = reference
only. D = reject.

  - `.33` (`build_canonical_execution_specification`), `.38`
    (`supervise_alpaca_equity_execution`) -- classified **A, DIRECT
    REUSE**. Both are called through their existing, unmodified public
    functions via this module's own `_load_module` dynamic-import
    convention (the same pattern `.50` established for reusing `.33`/
    `.49`). `.38`'s own `attempt_submission=False` default already IS the
    "construct and preview, never submit" dry-run mode `.53` needs by
    default -- `.53` does not reimplement this, it simply never overrides
    the default unless the caller explicitly opts in.
  - `.50` (`build_candidate_evidence`, `is_shortlist_eligible`, `decide`)
    -- classified **A, DIRECT REUSE**. `.53` calls these once per symbol,
    unmodified, exactly as designed.
  - `mexc_bot/live_bot.py`'s scan-loop shape (per-symbol `try/except`
    inside the loop, PLUS a second outer `try/except` around the whole
    cycle; a per-symbol "already have a position, skip new-entry
    evaluation" check performed BEFORE generating a new signal) --
    classified **B**: the two-layer error isolation and the
    skip-if-already-held gate are sound, reusable shapes. Classified
    **D** for its actual position-tracking mechanism (a bare in-process
    dict, never persisted, never reconciled against the broker on
    restart) and for its direct, unsupervised `exchange.create_order`
    calls -- neither is reused. `.53` takes "already held" as a REQUIRED
    caller-supplied fact per symbol (`SymbolCycleInput.already_held`)
    rather than maintaining its own position dict, and never calls an
    exchange/broker client directly -- every order-shaped action goes
    through `.38`'s already-supervised path.
  - DELTAX_v2 `helpers/etf_signal_executor.py` -- classified **B** for
    two specific mechanisms, reused: (1) it treats the broker's OWN
    order/position history as the durable source of truth rather than
    maintaining a separate state database (confirmed independently
    correct by this project's own `.43`, which already calls Alpaca's
    `get_all_positions()` directly -- see "Position/holdings state" below);
    (2) when more qualifying candidates exist than a per-run cap allows,
    it ranks by a composite conviction score and truncates, rather than
    first-come-first-served. `.53` adopts mechanism (2) directly but uses
    `.50`'s OWN already-computed `abs(final_rank_score)` as the ranking
    key -- never DELTAX_v2's invented 0.60/0.40-weighted composite formula
    (classified **C**, reference only, for the specific formula/weights).
  - AURA's own `.21` (`aura_v05321_alpaca_paper_runtime.py`) --
    classified **C, REFERENCE ONLY**: the right orchestration SHAPE
    (a `cycle()` function, `--once`/`--loop-seconds` CLI, atomic
    state-file write, fail-closed stop-the-loop-on-error) but wired to a
    separate, frozen crypto pipeline (`.12`-`.26`) via `subprocess.run()`
    per numbered script -- not adapted directly, since `.53`'s own chain
    (`.46`-`.52` -> `.50` -> `.33`/`.38`) is called as in-process Python
    functions, not shelled-out scripts. The `--once`/`--loop-seconds`
    CLI shape and the fail-closed "stop on first error" discipline ARE
    adopted directly in `main()` below.
  - AURA's own `.43` (`aura_v05343_portfolio_exposure_observability.py`)
    -- classified **A, DIRECT REUSE**, for two specific pieces: (1)
    `fetch_alpaca_portfolio(client)` already wraps Alpaca's
    `get_all_positions()` correctly -- `.53` does NOT reimplement broker
    position-fetching; a caller derives `already_held` from `.43`'s own
    output before building each `SymbolCycleInput`. (2) `append_equity_
    history`/`load_equity_history`'s atomic-JSON-file persistence pattern
    is reused directly by `persist_cycle_state` below, rather than
    inventing a second persistence mechanism for the same kind of data.
  - `mexc_bot/core/trade_logger.py`/`core/trade_metrics.py` -- classified
    **C, reference only** for `.53` specifically: sound durable-log +
    in-memory-cache + per-trade-reconstruction design, but PnL/trade-event
    reconstruction is downstream of what `.53` produces (a decision +
    supervisor result per cycle), not something `.53` itself needs to
    compute -- flagged for a future milestone if/when AURA needs its own
    trade-metrics reconstruction over `.38`'s own submission history.
  - `.44` (Portfolio Exposure Enforcement) -- **not modified, not
    duplicated** (mirrors `.50`'s own explicit reasoning for the same
    choice). `.53` accepts an OPTIONAL `enforcement_check_fn` the caller
    can wire directly to `.44`'s `evaluate_hypothetical_trade`; when
    `attempt_submission=True` and no `enforcement_check_fn` is supplied,
    `.53` FAILS CLOSED (raises `PaperOrchestrationError`) rather than
    silently allowing a real (even paper) submission with no portfolio-
    wide exposure check in the loop at all -- see "Fail-closed submission
    gate" below.
  - DELTAX/DELTAX_v2, Hermes Trader, Aegis/AegisAlpha, `mexc_bot`'s
    `dashboard_server.py`, walk-forward/permutation/parameter-sweep/
    MAE-MFE/regime-diagnostic code -- all re-confirmed **C, reference
    only** or out of scope for `.53` specifically (backtesting-validation
    tooling, not live orchestration; explicitly deferred by Martin to the
    post-`.53` backtesting-reuse audit).

Position/holdings state (the central architectural question this
milestone's audit resolved)
------------------------------------------------------------------------
`.53` does NOT build or own a new position/trade database. `.43` already
queries Alpaca's own `get_all_positions()` as the durable source of truth
for "what do we currently hold" (confirmed independently by DELTAX_v2's
own, unrelated `etf_signal_executor.py`, which converges on the identical
design). `.53` therefore takes `already_held: bool` as a plain, required,
per-symbol input on `SymbolCycleInput` -- the CALLER is responsible for
deriving it (typically: call `.43`'s `fetch_alpaca_portfolio`, or an
equivalent, once per cycle, and set `already_held=True` for any symbol
with an existing open position). `.53`'s own `run_cycle` remains a pure,
network-free function of its inputs, mirroring every prior milestone's
persistence-free-core discipline (`.47`/`.48`/`.49`/`.50`/`.51`/`.52`).

Ranking and the per-cycle cap
------------------------------------------------------------------------
When more symbols produce a `DECIDE_LONG`/`DECIDE_SHORT` outcome in one
cycle than `max_new_orders_per_cycle` allows, candidates are ranked by
`abs(decision.final_rank_score)` descending (ties broken by symbol name,
ascending, for determinism) and only the top `max_new_orders_per_cycle`
proceed to `.38`; the rest are recorded with stage
`DEFERRED_CYCLE_CAP` -- visible for audit, never silently dropped. This
mirrors DELTAX_v2's rank-then-truncate shape but uses `.50`'s own,
already-approved score -- never a new, invented composite formula.

Fail-closed submission gate
------------------------------------------------------------------------
`run_cycle(attempt_submission=True, ...)` with `enforcement_check_fn=None`
raises `PaperOrchestrationError` before evaluating anything. This is a
deliberate safety invariant, not a limitation to work around: `.44`
(Portfolio Exposure Enforcement) already exists precisely to catch
portfolio-wide risk `.50`'s own per-candidate decision cannot see, and
`.53` must not become the first milestone in this project that lets a
submission-attempting cycle run with no portfolio-wide check wired in at
all. With `attempt_submission=False` (the default), `.38` itself never
calls `.35.submit()` regardless -- so `enforcement_check_fn` is optional
for a dry-run/preview cycle.

Scope: entries only
------------------------------------------------------------------------
`.53` only ever builds `OPEN_LONG`/`OPEN_SHORT` canonical specs -- it
never builds `CLOSE_LONG`/`CLOSE_SHORT`. A symbol with `already_held=True`
is simply skipped (stage `SKIPPED_ALREADY_HELD`) -- `.53` does not manage,
scale, or exit an existing position. This mirrors `.21`'s own explicit
"Exits are OUT OF SCOPE for this runtime" disclosed limitation exactly.
Managing/exiting existing positions is left to a future milestone.

Persistence
------------------------------------------------------------------------
`run_cycle` itself is persistence-free. `persist_cycle_state` is a
separate, optional, explicitly-invoked function that atomically writes a
JSON summary of a completed `CycleResult` (reusing `.43`'s own
`_atomic_write_json`-equivalent read-modify-write-then-`os.replace()`
discipline, not a new mechanism) and, when a `PortfolioSnapshot` is
supplied, delegates equity-history logging to `.43`'s own
`append_equity_history` directly -- `.53` does not reimplement equity-
history tracking.

Known limitations (disclosed, not silently worked around)
------------------------------------------------------------------------
  - No live smoke test against a real Alpaca account was run in this
    sandboxed environment (no live credentials configured here) --
    identical disclosed limitation to `.35`/`.49`/`.51`/`.52`. `main()`'s
    live wiring (constructing real evidence-gathering clients, a real
    Alpaca `TradingClient`, and a real `.49` `llm_client`) is present but
    untested against live services in this sandbox; `run_cycle` itself
    (the actual new orchestration logic) is fully unit-tested against
    fakes.
  - Position SIZING (how large a `quantity` to submit) is explicitly OUT
    OF SCOPE -- `SymbolCycleInput.quantity` is a plain, required,
    caller-supplied value, mirroring `.33`'s own "this module performs no
    financial computation, quantity is caller-supplied" precedent exactly.
    A future milestone may formalize equity/ETF position sizing (crypto
    already has an analog at `.25`); `.53` orchestrates around an
    already-decided quantity, it does not decide sizing.
  - Exits/position management are out of scope this milestone (see
    "Scope: entries only" above).
  - `.44` integration is optional/injectable, not built into this
    module's own logic -- `.53` does not duplicate `.44`'s portfolio-wide
    arbitration, mirroring `.50`'s own explicit reasoning for the same
    choice (module docstring, "Known limitations").
  - This module's `main()` CLI loop is new, disclosed-untested-live
    scaffolding, not a hardened production entry point -- a future
    milestone is expected to wire real evidence-source clients into it
    deliberately, not as a side effect of this milestone.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

VERSION = "AURA v0.5.3.53"
ENGINE = "FULL_PAPER_ORCHESTRATION"
SCHEMA_VERSION = "1.0"

ROOT = Path(__file__).resolve().parent

OPEN_DIRECTIONS = {"DECIDE_LONG": "OPEN_LONG", "DECIDE_SHORT": "OPEN_SHORT"}

CYCLE_STAGES = frozenset(
    {
        "SKIPPED_ALREADY_HELD",
        "NOT_SHORTLISTED",
        "NO_TRADE_DECIDED",
        "DEFERRED_CYCLE_CAP",
        "SUBMITTED_FOR_EXECUTION",
    }
)


class PaperOrchestrationError(Exception):
    """Raised for programmer-error / required-parameter / fail-closed-
    safety-invariant violations only -- never for a symbol simply not
    reaching a trade decision (that is an expected, handled outcome:
    stage NO_TRADE_DECIDED / NOT_SHORTLISTED / DEFERRED_CYCLE_CAP),
    mirroring every prior milestone's own error-class discipline.
    """


# ============================================================================
# Dynamic import of `.33`/`.38`/`.50` -- same `_load_module` pattern `.50`
# established for reusing `.33`/`.49`, and `.52` established for reusing
# `.51`. None of `.33`/`.38`/`.50` is modified by this module.
# ============================================================================


def _load_module(module_name: str, filename: str):
    try:
        return __import__(module_name)
    except ImportError:
        import importlib.util

        spec = importlib.util.spec_from_file_location(module_name, ROOT / filename)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod


def load_canonical_spec_module():
    return _load_module("aura_v05333_canonical_execution_specification", "aura_v05333_canonical_execution_specification.py")


def load_decision_engine_module():
    return _load_module("aura_v05350_decision_engine", "aura_v05350_decision_engine.py")


def load_execution_supervisor_module():
    return _load_module("aura_v05338_common_execution_supervisor", "aura_v05338_common_execution_supervisor.py")


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


# ============================================================================
# Cycle input/output types
# ============================================================================


@dataclass(frozen=True, slots=True)
class SymbolCycleInput:
    symbol: str
    asset_class: str  # "STOCK" or "ETF" -- passed straight through to `.33`
    quantity: Any  # caller-supplied; `.33` validates it (see module docstring, "Known limitations")
    already_held: bool = False
    sentiment_regime: Any | None = None  # `.47` SentimentRegime, duck-typed
    wave_result: Any | None = None  # `.48` ElliottWaveResearchResult, duck-typed
    technical_regime: Any | None = None  # `.51` TechnicalRegime, duck-typed
    short_technical_regime: Any | None = None  # `.52` ShortTechnicalRegime, duck-typed
    news_item_count: int = 0
    alpaca_asset: dict[str, Any] | None = None  # required only if this symbol reaches `.38`


@dataclass(frozen=True, slots=True)
class SymbolCycleOutcome:
    symbol: str
    stage: str  # one of CYCLE_STAGES
    decision: Any | None  # `.50` TradingDecision, when reached
    supervision_result: dict[str, Any] | None  # `.38` result dict, when reached
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.stage not in CYCLE_STAGES:
            raise PaperOrchestrationError(f"INVALID_CYCLE_STAGE:{self.stage}")


@dataclass(frozen=True, slots=True)
class CycleResult:
    cycle_id: str
    started_at: str
    finished_at: str
    max_new_orders_per_cycle: int
    attempt_submission: bool
    outcomes: tuple[SymbolCycleOutcome, ...] = field(default_factory=tuple)

    @property
    def submitted_count(self) -> int:
        return sum(1 for o in self.outcomes if o.stage == "SUBMITTED_FOR_EXECUTION")


# ============================================================================
# The cycle -- `.53`'s own, genuinely new contribution. Pure function of
# its inputs plus the injected `.50`/`.33`/`.38` calls; no I/O of its own.
# ============================================================================


def run_cycle(
    symbol_inputs: tuple[SymbolCycleInput, ...],
    *,
    max_new_orders_per_cycle: int,
    decide_kwargs: dict[str, Any],
    strategy_id: str,
    strategy_version: str,
    source_kind: str = "DETERMINISTIC_SIGNAL",
    environment: str = "PAPER",
    attempt_submission: bool = False,
    enforcement_check_fn: Callable[[str, str, Any, Any], Any] | None = None,
    supervision_kwargs: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> CycleResult:
    """One full pass over `symbol_inputs`: skip already-held symbols,
    decide the rest via `.50`, rank+cap the resulting entries, and hand
    approved entries to `.38` (dry-run/preview by default). Every symbol
    produces exactly one `SymbolCycleOutcome`, whatever stage it reaches
    -- nothing is silently dropped.

    `enforcement_check_fn`, when supplied, is called as
    `enforcement_check_fn(symbol, direction, quantity, decision)` for
    every candidate that survives ranking/capping and is about to reach
    `.38`; it must return an object/dict exposing a truthy/falsy
    `allowed` (attribute or key) and MAY expose `reasons` (iterable of
    strings). A falsy `allowed` blocks that symbol at stage
    `NO_TRADE_DECIDED` with the enforcement reasons recorded, and `.38`
    is never called for it.
    """
    if max_new_orders_per_cycle <= 0:
        raise PaperOrchestrationError("INVALID_MAX_NEW_ORDERS_PER_CYCLE:must be > 0")
    symbols_seen = [s.symbol for s in symbol_inputs]
    if len(set(symbols_seen)) != len(symbols_seen):
        raise PaperOrchestrationError("DUPLICATE_SYMBOL_IN_CYCLE_INPUT")
    if attempt_submission and enforcement_check_fn is None:
        raise PaperOrchestrationError(
            "SUBMISSION_REQUIRES_ENFORCEMENT_CHECK:attempt_submission=True requires an "
            "enforcement_check_fn (wire it to `.44`'s evaluate_hypothetical_trade or an "
            "equivalent) -- a submission-attempting cycle must never run with no "
            "portfolio-wide exposure check in the loop at all."
        )

    now_dt = now or datetime.now(timezone.utc)
    cycle_id = now_dt.strftime("%Y%m%dT%H%M%S%fZ")
    started_at = _now_iso(now_dt)

    decision_engine = load_decision_engine_module()

    outcomes: list[SymbolCycleOutcome] = []
    execution_candidates: list[tuple[float, str, SymbolCycleInput, Any]] = []

    for symbol_input in symbol_inputs:
        if symbol_input.already_held:
            outcomes.append(
                SymbolCycleOutcome(
                    symbol=symbol_input.symbol, stage="SKIPPED_ALREADY_HELD", decision=None,
                    supervision_result=None, reasons=("symbol already held -- .53 does not manage exits/re-entries",),
                )
            )
            continue

        evidence = decision_engine.build_candidate_evidence(
            symbol_input.symbol,
            sentiment_regime=symbol_input.sentiment_regime,
            wave_result=symbol_input.wave_result,
            technical_regime=symbol_input.technical_regime,
            short_technical_regime=symbol_input.short_technical_regime,
            news_item_count=symbol_input.news_item_count,
            now=now_dt,
        )
        if not decision_engine.is_shortlist_eligible(evidence):
            outcomes.append(
                SymbolCycleOutcome(
                    symbol=symbol_input.symbol, stage="NOT_SHORTLISTED", decision=None,
                    supervision_result=None, reasons=("no usable evidence source for this symbol this cycle",),
                )
            )
            continue

        decision = decision_engine.decide(evidence, now=now_dt, **decide_kwargs)
        if decision.outcome not in OPEN_DIRECTIONS:
            outcomes.append(
                SymbolCycleOutcome(
                    symbol=symbol_input.symbol, stage="NO_TRADE_DECIDED", decision=decision,
                    supervision_result=None, reasons=decision.reasons,
                )
            )
            continue

        execution_candidates.append((abs(decision.final_rank_score), symbol_input.symbol, symbol_input, decision))

    # Rank by |final_rank_score| descending, ties broken by symbol ascending
    # for determinism -- `.50`'s own already-approved score, never a new
    # invented composite (see module docstring).
    execution_candidates.sort(key=lambda item: (-item[0], item[1]))
    selected = execution_candidates[:max_new_orders_per_cycle]
    deferred = execution_candidates[max_new_orders_per_cycle:]

    for _, symbol, symbol_input, decision in deferred:
        outcomes.append(
            SymbolCycleOutcome(
                symbol=symbol, stage="DEFERRED_CYCLE_CAP", decision=decision, supervision_result=None,
                reasons=(f"cycle cap reached (max_new_orders_per_cycle={max_new_orders_per_cycle})",),
            )
        )

    canon = load_canonical_spec_module()
    supervisor = load_execution_supervisor_module()

    for _, symbol, symbol_input, decision in selected:
        direction = OPEN_DIRECTIONS[decision.outcome]

        if enforcement_check_fn is not None:
            verdict = enforcement_check_fn(symbol, direction, symbol_input.quantity, decision)
            allowed = getattr(verdict, "allowed", None)
            if allowed is None and isinstance(verdict, dict):
                allowed = verdict.get("allowed")
            reasons = getattr(verdict, "reasons", None)
            if reasons is None and isinstance(verdict, dict):
                reasons = verdict.get("reasons")
            if not allowed:
                outcomes.append(
                    SymbolCycleOutcome(
                        symbol=symbol, stage="NO_TRADE_DECIDED", decision=decision, supervision_result=None,
                        reasons=tuple(reasons) if reasons else ("blocked by portfolio exposure enforcement check",),
                    )
                )
                continue

        spec = canon.build_canonical_execution_specification(
            asset_class=symbol_input.asset_class,
            venue="ALPACA",
            direction=direction,
            symbol=symbol,
            quantity=symbol_input.quantity,
            decision_id=decision.decision_hash,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            signal_timestamp=decision.decided_at,
            source_kind=source_kind,
            order_type="MARKET",
        )
        result = supervisor.supervise_alpaca_equity_execution(
            spec,
            symbol_input.alpaca_asset or {},
            environment=environment,
            attempt_submission=attempt_submission,
            **(supervision_kwargs or {}),
        )
        outcomes.append(
            SymbolCycleOutcome(
                symbol=symbol, stage="SUBMITTED_FOR_EXECUTION", decision=decision, supervision_result=result,
                reasons=(f"supervisor status={result.get('status')}",),
            )
        )

    # Stable, deterministic ordering for the returned outcomes regardless
    # of which branch produced each one -- input order, not append order.
    order = {s.symbol: i for i, s in enumerate(symbol_inputs)}
    outcomes.sort(key=lambda o: order[o.symbol])

    return CycleResult(
        cycle_id=cycle_id,
        started_at=started_at,
        finished_at=_now_iso(datetime.now(timezone.utc)),
        max_new_orders_per_cycle=max_new_orders_per_cycle,
        attempt_submission=attempt_submission,
        outcomes=tuple(outcomes),
    )


# ============================================================================
# Persistence -- reuses `.43`'s own atomic-write / equity-history
# mechanism directly, does not reinvent it.
# ============================================================================


def _atomic_write_json(path: Path, obj: Any) -> None:
    """The same read-modify-write-then-`os.replace()` discipline `.43`'s
    own `_atomic_write_json` uses (mirrored, not imported, since `.43`'s
    version is a private, unexported helper -- the same "copy small
    helpers, import genuine shared contracts" convention this repo
    already follows for `stable_json`/`sha256_text` everywhere else).
    """
    import os

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _cycle_result_to_dict(result: CycleResult) -> dict[str, Any]:
    return {
        "cycle_id": result.cycle_id,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "max_new_orders_per_cycle": result.max_new_orders_per_cycle,
        "attempt_submission": result.attempt_submission,
        "submitted_count": result.submitted_count,
        "outcomes": [
            {
                "symbol": o.symbol,
                "stage": o.stage,
                "decision_outcome": getattr(o.decision, "outcome", None),
                "decision_hash": getattr(o.decision, "decision_hash", None),
                "supervision_status": (o.supervision_result or {}).get("status"),
                "reasons": list(o.reasons),
            }
            for o in result.outcomes
        ],
    }


def persist_cycle_state(
    result: CycleResult,
    *,
    state_dir: Path,
    portfolio_snapshot: Any | None = None,
) -> Path:
    """Atomically writes `state_dir/latest_cycle.json` (overwritten every
    call, mirroring `.21`'s own `write_state()`) and archives this cycle's
    full summary under `state_dir/cycles/<cycle_id>.json` (mirroring
    `.21`'s own per-cycle archive directory). When `portfolio_snapshot` is
    supplied, delegates to `.43`'s OWN `append_equity_history` directly --
    this function does not reimplement equity-history tracking.
    """
    state_dir = Path(state_dir)
    payload = _cycle_result_to_dict(result)
    _atomic_write_json(state_dir / "latest_cycle.json", payload)
    _atomic_write_json(state_dir / "cycles" / f"{result.cycle_id}.json", payload)

    if portfolio_snapshot is not None:
        obs43 = _load_module("aura_v05343_portfolio_exposure_observability", "aura_v05343_portfolio_exposure_observability.py")
        obs43.append_equity_history(state_dir, portfolio_snapshot)

    return state_dir / "latest_cycle.json"


# ============================================================================
# CLI loop -- mirrors `.21`'s `--once`/`--loop-seconds` shape and its
# fail-closed "stop the loop on the first error" discipline. This is new,
# disclosed-untested-in-this-sandbox scaffolding (module docstring, "Known
# limitations") -- `run_cycle` itself (the actually-tested logic) is
# unaffected by whether `main()` is ever invoked.
# ============================================================================


def main(argv: list[str] | None = None) -> int:  # pragma: no cover -- live wiring, not unit-testable here
    import argparse

    parser = argparse.ArgumentParser(description="AURA .53 full paper orchestration runtime")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--loop-seconds", type=float, default=None)
    parser.add_argument("--state-dir", type=Path, default=ROOT / "regime_output" / "v053_runtime")
    parser.parse_args(argv)  # parsed for CLI-shape completeness; not yet consumed -- see below

    raise PaperOrchestrationError(
        "NOT_WIRED:`.53`'s main() has no live evidence-source/broker-client wiring in this "
        "sandbox (no live credentials configured here, matching `.35`/`.49`/`.51`/`.52`'s own "
        "disclosed limitation) -- a future, deliberate change should construct real `.47`/`.48`/"
        "`.51`/`.52` fetch clients, a real Alpaca TradingClient, and a real `.49` llm_client here, "
        "then call run_cycle()/persist_cycle_state() in the --once/--loop-seconds shape below "
        "(mirroring `.21`'s own `while True: cycle(...); time.sleep(args.loop_seconds)` loop, "
        "stopping on the first error, fail-closed)."
    )


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(main())
