#!/usr/bin/env python3
"""
AURA v0.5.3.55 — Track B Stage 1 paper-trading runner (wiring, not new logic).

WHAT THIS MODULE IS, AND IS DELIBERATELY NOT
------------------------------------------------------------------------
This is the Stage 1 entry point Martin asked for: it ties `.53`'s already
built, already tested `run_cycle` orchestration to (a) a REAL `.44`
portfolio-exposure enforcement check (`.53`'s own test suite only ever
exercises `enforcement_check_fn` with hand-rolled allow/block stand-ins,
never the real `.44` engine), (b) real broker asset-metadata lookups for
the shortability audit trail Martin explicitly required, (c) `.54`'s fill
reconciliation for Stage 1B, and (d) one comprehensive, per-order audit
record matching every field in Martin's Stage 1B checklist verbatim.

It adds NO new scoring, execution, or risk logic of its own. Every safety
decision (kill switch, authorization gates, shortability fail-closed
behavior, portfolio-exposure limits) is made by `.38`/`.36`/`.35`/`.34`/
`.44`, exactly as already built and tested — this module only wires their
existing public functions together and logs the result honestly.

Per Martin's three decisions this milestone implements:
  1. LLM client: this module never constructs or references a `.49` real
     LLM client. The caller supplies `decide_kwargs["llm_client"]`
     directly (Stage 1A/1B both use `.54`'s `NeutralDeterministicLLMClient`
     stub, per Martin's explicit "keep the stub through first live paper
     orders" instruction) — `.55` itself is LLM-client-agnostic.
  2. Short-side: Stage 1B exercises both long and short decision paths.
     `.35`/`.34`/`.36`/`.38`'s own fail-closed shortability logic (already
     built, not duplicated here) is what actually blocks a short whose
     borrow status is not positively verified — this module's own
     contribution is `build_shortability_audit()`, which independently
     records the broker's RAW `shortable`/`easy_to_borrow` fields AND the
     adapter's TRANSLATED `direction_capability` decision side-by-side in
     every audit record, per Martin's explicit "do not assume the
     translation is correct" instruction, so a live discrepancy is visible
     without having to reconstruct it from `.38`'s own opaque BLOCKED
     reason string.
  3. `.51`/`.52` signal parameters: this module never imports `.51`/`.52`
     and never constructs a `TechnicalScoringParams`. `SymbolRequest`
     below carries `technical_regime`/`short_technical_regime` fields only
     because `.53`'s own `SymbolCycleInput` does (for future Stage 1C use)
     — Stage 1A/1B callers leave them `None` and drive `sentiment_regime`/
     `wave_result` synthetic/frozen evidence instead, exactly as agreed.

Strategy caveat, carried into every audit record
------------------------------------------------------------------------
`strategy_id` defaults to `STAGE1_SYNTHETIC_SCENARIO` specifically so a
Stage 1B paper fill can never later be mistaken for evidence about a real,
validated `.51`/`.52` strategy (Martin: "If .51/.52 later turns out wrong,
the execution infrastructure should remain usable ... without rebuilding
the bot"). A caller MAY override this, but the default is deliberately
unmistakable.

Fail-closed choices made explicit here, not silently
------------------------------------------------------------------------
  - `.38`'s own kill switch defaults to ENGAGED (`SUPERVISOR_DEFAULT_CONFIG
    = {"kill_switch": True}`) and `.36`'s own `DEFAULT_AUTH_CONFIG` defaults
    both `execution_authorized`/`paper_execution_authorized` to `False`.
    `run_stage1b_paper_cycle` does NOT override either default on the
    caller's behalf — `supervision_kwargs["supervisor_config"]` and
    `supervision_kwargs["auth_config"]` are REQUIRED, no-default
    parameters, so a caller must explicitly, consciously open the kill
    switch and authorization gates for anything to actually reach the
    broker. `run_stage1a_dry_run` (which can NEVER submit — it always
    calls `.53.run_cycle` with `attempt_submission=False`, not caller-
    overridable) opens the kill switch by default purely so the
    construction/preview path is reachable, since a preview blocked at the
    kill switch would prove nothing about the plumbing being wired
    correctly; a caller can still pass `supervision_kwargs` to override
    this and specifically exercise the kill-switch-rejection path.
  - `reference_price_fn`/`max_snapshot_age_seconds`/`fill_poll_timeout_
    seconds`/`fill_poll_interval_seconds` are all REQUIRED, no-default
    parameters on `run_stage1b_paper_cycle` — every one is an operational
    or pricing choice this module refuses to invent, mirroring `.44`'s own
    refusal to default `max_snapshot_age_seconds` and `.54`'s own refusal
    to default poll timing.
  - `limits: PortfolioLimits | None = None` is the one exception, and it
    is NOT an invented default: passing `None` constructs a plain
    `PortfolioLimits()` (every field `None`/empty), which is `.44`'s own
    explicit, disclosed "no limit configured" state, not a number this
    module chose. A caller who wants real portfolio limits enforced
    supplies their own `PortfolioLimits(...)`.

Fees/costs
------------------------------------------------------------------------
No fee or slippage model is wired into Stage 1. `FEES_COST_DISCLOSURE`
below is copied verbatim into every audit record's `fees_cost_assumptions`
field, rather than a fabricated number, per this project's "never invent
numbers" discipline extended to costs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

VERSION = "AURA v0.5.3.55"
ENGINE = "STAGE1_PAPER_TRADING_RUNNER"
SCHEMA_VERSION = "1.0"

ROOT = Path(__file__).resolve().parent

DEFAULT_STRATEGY_ID = "STAGE1_SYNTHETIC_SCENARIO"
DEFAULT_STRATEGY_VERSION = "v1-synthetic-dry-run"

FEES_COST_DISCLOSURE = (
    "Stage 1 assumes zero fees/costs beyond whatever Alpaca's own paper fill "
    "price reflects; no separate fee, commission, or slippage model is wired "
    "into this milestone. Do not treat a Stage 1 paper P&L as cost-inclusive."
)


class Stage1RunnerError(Exception):
    """Programmer-error / invalid-input only, mirroring every other
    milestone's own error-class discipline in this repo — never raised for
    an ordinary handled outcome (a blocked order, a failed asset-metadata
    fetch, an unfilled order at timeout are all recorded on the returned
    report, not raised)."""


# ============================================================================
# Dynamic import of `.34`/`.35`/`.43`/`.44`/`.53`/`.54` -- same `_load_module`
# convention every module in this chain already uses. None of them is
# modified; each is used strictly through its existing public functions.
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


def load_metadata_module():
    return _load_module("aura_v05334_asset_instrument_metadata", "aura_v05334_asset_instrument_metadata.py")


def load_alpaca_adapter_module():
    return _load_module("aura_v05335_alpaca_equity_execution_adapter", "aura_v05335_alpaca_equity_execution_adapter.py")


def load_observability_module():
    return _load_module("aura_v05343_portfolio_exposure_observability", "aura_v05343_portfolio_exposure_observability.py")


def load_enforcement_module():
    return _load_module("aura_v05344_portfolio_exposure_enforcement", "aura_v05344_portfolio_exposure_enforcement.py")


def load_cycle_module():
    return _load_module("aura_v05353_full_paper_orchestration", "aura_v05353_full_paper_orchestration.py")


def load_fill_reconciliation_module():
    return _load_module("aura_v05354_alpaca_equity_fill_reconciliation", "aura_v05354_alpaca_equity_fill_reconciliation.py")


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


# ============================================================================
# Input/output types
# ============================================================================


@dataclass(frozen=True, slots=True)
class SymbolRequest:
    """One symbol's caller-supplied evidence for a Stage 1 cycle -- the
    subset of `.53`'s own `SymbolCycleInput` this runner does not derive
    itself. `already_held`/`alpaca_asset` are deliberately NOT here for
    Stage 1B (this module derives both from a real broker call); Stage 1A
    accepts an optional `alpaca_asset` directly since it never calls a
    broker at all."""

    symbol: str
    asset_class: str  # "STOCK" or "ETF"
    quantity: Any
    sentiment_regime: Any | None = None
    wave_result: Any | None = None
    technical_regime: Any | None = None  # left None in Stage 1A/1B by agreement (see module docstring, point 3)
    short_technical_regime: Any | None = None
    news_item_count: int = 0
    alpaca_asset: dict[str, Any] | None = None  # Stage 1A synthetic only; Stage 1B ignores this and fetches for real


@dataclass(frozen=True, slots=True)
class EnforcementCheckResult:
    allowed: bool
    reasons: tuple[str, ...]
    enforcement_decision: Any  # `.44` EnforcementDecision, kept for audit


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """One row per symbol per cycle, matching every field in Martin's
    Stage 1B logging checklist verbatim, plus the raw-vs-translated
    shortability disclosure his short-side instruction explicitly required."""

    schema_version: str
    engine: str
    agent_version: str
    observed_at: str
    symbol: str
    strategy_id: str
    strategy_version: str
    cycle_stage: str  # `.53` CYCLE_STAGES value
    signal_outcome: str | None  # `.50` TradingDecision.outcome, when reached
    signal_final_rank_score: float | None
    decision_hash: str | None
    risk_decision_status: str  # "NOT_EVALUATED" | "ALLOW" | "BLOCK"
    risk_decision_hash: str | None
    risk_decision_blocked_reasons: tuple[str, ...]
    intended_quantity: Any
    intended_notional_usd: float | None
    broker_order_id: str | None
    client_order_id: str | None
    submission_status: str | None  # `.38`/`.35` status string, when reached
    fill_status: str | None
    fill_price: str | None
    fees_cost_assumptions: str
    resulting_position: dict[str, Any] | None
    kill_switch_engaged: bool
    raw_shortable: Any
    raw_easy_to_borrow: Any
    translated_shortability_status: str | None
    direction_capability: str | None
    short_positively_verified: bool | None
    reasons: tuple[str, ...]  # `.53` SymbolCycleOutcome.reasons

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "engine": self.engine, "agent_version": self.agent_version,
            "observed_at": self.observed_at, "symbol": self.symbol, "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version, "cycle_stage": self.cycle_stage,
            "signal_outcome": self.signal_outcome, "signal_final_rank_score": self.signal_final_rank_score,
            "decision_hash": self.decision_hash, "risk_decision_status": self.risk_decision_status,
            "risk_decision_hash": self.risk_decision_hash,
            "risk_decision_blocked_reasons": list(self.risk_decision_blocked_reasons),
            "intended_quantity": self.intended_quantity, "intended_notional_usd": self.intended_notional_usd,
            "broker_order_id": self.broker_order_id, "client_order_id": self.client_order_id,
            "submission_status": self.submission_status, "fill_status": self.fill_status,
            "fill_price": self.fill_price, "fees_cost_assumptions": self.fees_cost_assumptions,
            "resulting_position": self.resulting_position, "kill_switch_engaged": self.kill_switch_engaged,
            "raw_shortable": self.raw_shortable, "raw_easy_to_borrow": self.raw_easy_to_borrow,
            "translated_shortability_status": self.translated_shortability_status,
            "direction_capability": self.direction_capability,
            "short_positively_verified": self.short_positively_verified,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class Stage1CycleReport:
    stage: str  # "STAGE_1A_DRY_RUN" | "STAGE_1B_PAPER_CYCLE"
    cycle_result: Any  # `.53` CycleResult, or None if every symbol failed asset-metadata fetch before reaching it
    audit_records: tuple[AuditRecord, ...]
    metadata_fetch_failures: tuple[dict[str, Any], ...] = field(default_factory=tuple)


# ============================================================================
# Shortability audit -- point 2 of Martin's instruction: record raw broker
# data and the adapter's translated decision side by side, never assume
# the translation is correct.
# ============================================================================


def build_shortability_audit(
    alpaca_asset: dict[str, Any] | None,
    direction: str | None,
    *,
    asset_class: str,
    adapter_module: Any,
    metadata_module: Any,
) -> dict[str, Any]:
    if direction is None:
        return {
            "raw_shortable": None, "raw_easy_to_borrow": None, "translated_shortability_status": None,
            "direction_capability": None, "short_positively_verified": None,
        }
    asset = alpaca_asset if isinstance(alpaca_asset, dict) else {}
    raw_shortable = asset.get("shortable")
    raw_easy_to_borrow = asset.get("easy_to_borrow")
    translated_status = adapter_module.alpaca_asset_to_shortability_status(raw_shortable, raw_easy_to_borrow)
    capability = metadata_module.classify_direction_capability(
        asset_class, None, direction, shortability_status=translated_status,
    )
    return {
        "raw_shortable": raw_shortable,
        "raw_easy_to_borrow": raw_easy_to_borrow,
        "translated_shortability_status": translated_status,
        "direction_capability": capability,
        "short_positively_verified": (capability == metadata_module.BORROW_CONFIRMED) if direction == "OPEN_SHORT" else None,
    }


# ============================================================================
# Enforcement adapter -- bridges `.44`'s EnforcementDecision to `.53`'s
# expected `.allowed`/`.reasons` shape. This is the genuinely new
# integration this milestone adds: `.53`'s own test suite only ever
# exercises enforcement_check_fn with hand-rolled stand-ins, never the real
# `.44` engine.
# ============================================================================


def build_enforcement_check_fn(
    *,
    snapshot: Any,
    equity_history: list[dict[str, Any]],
    limits: Any,
    max_snapshot_age_seconds: float,
    reference_price_fn: Callable[[str], float],
    now: datetime | None,
    decisions_by_symbol: dict[str, Any],
    enforcement_module: Any,
    observability_module: Any,
) -> Callable[[str, str, Any, Any], EnforcementCheckResult]:
    """Returns a closure matching `.53.run_cycle`'s `enforcement_check_fn`
    contract exactly: `(symbol, direction, quantity, decision) ->
    EnforcementCheckResult`. Builds a hypothetical `.43.PositionRecord` for
    the candidate order (never yet in `snapshot`) using
    `notional_basis="ENTRY_PRICE_ESTIMATE"` and the caller-required
    `reference_price_fn`, exactly matching `.44`'s own test-fixture
    convention for a not-yet-priced hypothetical trade, then calls `.44`'s
    OWN `evaluate_hypothetical_trade` -- this function does not re-derive
    any exposure math. Every `EnforcementDecision` produced is recorded
    into `decisions_by_symbol` (keyed by symbol) so a caller can build a
    full audit record afterward even for a candidate that was ALLOWED
    (`.53`'s own `CycleResult` does not retain an allowed verdict, only a
    blocked one's reasons)."""

    def check(symbol: str, direction: str, quantity: Any, decision: Any) -> EnforcementCheckResult:
        try:
            qty = float(quantity)
        except (TypeError, ValueError) as exc:
            raise Stage1RunnerError(f"INVALID_QUANTITY_FOR_ENFORCEMENT_CHECK:{quantity!r}") from exc
        price = reference_price_fn(symbol)
        hypothetical = observability_module.PositionRecord(
            venue="ALPACA", symbol=symbol, direction="LONG" if direction == "OPEN_LONG" else "SHORT",
            quantity=qty, entry_price=price, leverage=None, mark_price=None,
            notional_usd=abs(qty * price), notional_basis="ENTRY_PRICE_ESTIMATE",
            unrealized_pnl_usd=None, liquidation_price=None, raw_source_id=None,
            as_of=_now_iso(now),
        )
        enforcement_decision = enforcement_module.evaluate_hypothetical_trade(
            snapshot, equity_history, hypothetical, limits,
            max_snapshot_age_seconds=max_snapshot_age_seconds, now=now,
        )
        decisions_by_symbol[symbol] = enforcement_decision
        allowed = enforcement_decision.overall_verdict == "ALLOW"
        reasons = tuple(
            f"{v.dimension}[{v.venue or 'PORTFOLIO'}]:{v.reason}"
            for v in enforcement_decision.dimension_verdicts
            if v.verdict == "BLOCK"
        )
        return EnforcementCheckResult(allowed=allowed, reasons=reasons, enforcement_decision=enforcement_decision)

    return check


# ============================================================================
# Real broker asset-metadata fetch -- thin, fail-closed wrapper on `.35`'s
# own already-tested `fetch_asset_metadata`.
# ============================================================================


def fetch_real_alpaca_asset(client: Any, symbol: str, *, adapter_module: Any) -> dict[str, Any]:
    """Never raises. A failed fetch is reported, not silently retried or
    defaulted to a permissive empty dict -- a caller must treat
    status != "OK" as "do not build an order for this symbol this cycle"."""
    try:
        asset = adapter_module.fetch_asset_metadata(client, symbol)
        return {"status": "OK", "alpaca_asset": asset, "error": None}
    except Exception as exc:  # noqa: BLE001 -- broker call, classified and reported, never swallowed
        return {"status": "FETCH_FAILED", "alpaca_asset": None, "error": f"{type(exc).__name__}: {exc}"}


# ============================================================================
# Audit record construction.
# ============================================================================


def build_audit_record(
    *,
    symbol: str,
    strategy_id: str,
    strategy_version: str,
    outcome: Any,  # `.53` SymbolCycleOutcome, or None if never reached (asset-metadata fetch failure)
    intended_quantity: Any,
    reference_price_fn: Callable[[str], float] | None,
    shortability: dict[str, Any],
    enforcement_decision: Any | None,
    fill_result: Any | None,  # `.54` FillReconciliationResult, or None
    resulting_position: dict[str, Any] | None,
    kill_switch_engaged: bool,
    now: datetime | None,
    cycle_stage_override: str | None = None,
) -> AuditRecord:
    observed_at = _now_iso(now)
    decision = getattr(outcome, "decision", None) if outcome is not None else None
    supervision_result = getattr(outcome, "supervision_result", None) if outcome is not None else None

    intended_notional_usd: float | None = None
    if reference_price_fn is not None:
        try:
            intended_notional_usd = abs(float(intended_quantity) * reference_price_fn(symbol))
        except (TypeError, ValueError):
            intended_notional_usd = None

    if enforcement_decision is None:
        risk_decision_status = "NOT_EVALUATED"
        risk_decision_hash = None
        risk_blocked_reasons: tuple[str, ...] = ()
    else:
        risk_decision_status = enforcement_decision.overall_verdict
        risk_decision_hash = enforcement_decision.decision_hash
        risk_blocked_reasons = tuple(
            f"{v.dimension}[{v.venue or 'PORTFOLIO'}]:{v.reason}"
            for v in enforcement_decision.dimension_verdicts
            if v.verdict == "BLOCK"
        )

    submission_result = (supervision_result or {}).get("submission_result") if supervision_result else None
    broker_order_id = (submission_result or {}).get("broker_order_id")
    client_order_id = (submission_result or {}).get("client_order_id") or (supervision_result or {}).get("client_order_id")
    submission_status = (submission_result or {}).get("status") or (supervision_result or {}).get("status")

    return AuditRecord(
        schema_version=SCHEMA_VERSION, engine=ENGINE, agent_version=VERSION, observed_at=observed_at,
        symbol=symbol, strategy_id=strategy_id, strategy_version=strategy_version,
        cycle_stage=cycle_stage_override or getattr(outcome, "stage", "ASSET_METADATA_FETCH_FAILED"),
        signal_outcome=getattr(decision, "outcome", None), signal_final_rank_score=getattr(decision, "final_rank_score", None),
        decision_hash=getattr(decision, "decision_hash", None),
        risk_decision_status=risk_decision_status, risk_decision_hash=risk_decision_hash,
        risk_decision_blocked_reasons=risk_blocked_reasons,
        intended_quantity=intended_quantity, intended_notional_usd=intended_notional_usd,
        broker_order_id=broker_order_id, client_order_id=client_order_id, submission_status=submission_status,
        fill_status=getattr(fill_result, "status", None), fill_price=getattr(fill_result, "filled_avg_price", None),
        fees_cost_assumptions=FEES_COST_DISCLOSURE, resulting_position=resulting_position,
        kill_switch_engaged=kill_switch_engaged,
        raw_shortable=shortability["raw_shortable"], raw_easy_to_borrow=shortability["raw_easy_to_borrow"],
        translated_shortability_status=shortability["translated_shortability_status"],
        direction_capability=shortability["direction_capability"],
        short_positively_verified=shortability["short_positively_verified"],
        reasons=tuple(getattr(outcome, "reasons", ()) or ()),
    )


def _find_position(snapshot: Any, symbol: str) -> dict[str, Any] | None:
    for p in snapshot.positions:
        if p.venue == "ALPACA" and p.symbol == symbol:
            return p.to_dict()
    return None


# ============================================================================
# Stage 1A -- dry run. attempt_submission is hardcoded False; no broker
# call of any kind is ever made by this function.
# ============================================================================


def run_stage1a_dry_run(
    symbol_requests: tuple[SymbolRequest, ...],
    *,
    decide_kwargs: dict[str, Any],
    max_new_orders_per_cycle: int,
    strategy_id: str = DEFAULT_STRATEGY_ID,
    strategy_version: str = DEFAULT_STRATEGY_VERSION,
    reference_price_fn: Callable[[str], float] | None = None,
    supervision_kwargs: dict[str, Any] | None = None,
    limits: Any | None = None,
    equity_history: list[dict[str, Any]] | None = None,
    max_snapshot_age_seconds: float | None = None,
    now: datetime | None = None,
    synthetic_account_equity_usd: float | None = None,
) -> Stage1CycleReport:
    """Exercises the full lifecycle from evidence through `.38` construction
    preview (`READY_FOR_SUBMISSION`/`CONSTRUCTION_ONLY`), never a real
    submission -- `.53.run_cycle` is always called with
    `attempt_submission=False` here, not overridable by `supervision_kwargs`.
    `symbol_requests[i].alpaca_asset`, when supplied, is a caller-provided
    SYNTHETIC asset dict (no broker is ever called in Stage 1A) -- used only
    to exercise the shortability audit trail end to end before Stage 1B
    talks to a real account.

    `limits` is OPTIONAL here, unlike Stage 1B: `.53.run_cycle` calls
    `enforcement_check_fn` whenever it is supplied, regardless of
    `attempt_submission` -- passing `limits` (a `.44.PortfolioLimits`)
    wires the SAME real `build_enforcement_check_fn` Stage 1B uses, so the
    dry run can genuinely exercise `.44` risk-blocking end to end, with
    ZERO broker contact.

    IMPORTANT, discovered while building Stage 2 (corrects an earlier,
    incomplete Stage 1 docstring claim): `.43.build_portfolio_snapshot()`
    called with NO client reports both venues `NOT_CONFIGURED`, and
    `NOT_CONFIGURED` is indeed not a *data-quality failure* -- but `.44`'s
    portfolio_heat/directional_exposure dimensions ALSO require a real
    total-equity figure to compute a ratio at all (`asset_concentration`
    is the only aggregate dimension with a flat-book carve-out). With no
    venue ever SUCCESS, `total_equity` is unknown, so both dimensions
    report `NOT_COMPUTABLE` and `.44` BLOCKs with `EXPOSURE_NOT_COMPUTABLE`
    -- unconditionally, even when `limits` leaves that dimension's numeric
    threshold unconfigured (`LIMIT_NOT_CONFIGURED` is only reached AFTER
    the computability check). This is correct, fail-closed `.44` behavior,
    not a bug: it cannot express "no limit configured, so allow" about a
    ratio it cannot compute the numerator/denominator of.

    Passing `synthetic_account_equity_usd` (a caller-disclosed, clearly
    SYNTHETIC number -- never fabricated silently) builds the snapshot by
    hand instead, as one ALPACA `VenueFetchStatus(status="SUCCESS")`
    carrying that equity and zero live positions -- still ZERO broker
    contact (no client object is ever touched), but with a real, known
    total equity so portfolio_heat/directional_exposure become genuinely
    COMPUTABLE and can be driven to either ALLOW or a real LIMIT_BREACHED
    BLOCK by the caller's chosen `limits`. Leaving it `None` reproduces
    the original no-equity-anywhere snapshot, under which those two
    dimensions always BLOCK on `EXPOSURE_NOT_COMPUTABLE` regardless of
    `limits` -- a legitimate scenario in its own right (fail-closed when
    no account-equity source exists at all), just not a way to observe a
    real numeric-limit ALLOW/BLOCK contrast.

    Leaving `limits=None` (the default) skips enforcement entirely, exactly
    matching this function's original behavior."""
    now_dt = now or datetime.now(timezone.utc)
    cycle_module = load_cycle_module()
    metadata_module = load_metadata_module()
    adapter_module = load_alpaca_adapter_module()

    enforcement_check_fn = None
    decisions_by_symbol: dict[str, Any] = {}
    if limits is not None:
        if reference_price_fn is None or max_snapshot_age_seconds is None:
            raise Stage1RunnerError(
                "ENFORCEMENT_REQUIRES_REFERENCE_PRICE_FN_AND_MAX_SNAPSHOT_AGE_SECONDS:"
                "limits was supplied but reference_price_fn/max_snapshot_age_seconds was not"
            )
        observability_module = load_observability_module()
        enforcement_module = load_enforcement_module()
        if synthetic_account_equity_usd is not None:
            # Hand-built snapshot -- no client object constructed or
            # touched anywhere in this branch, so this remains zero
            # broker contact. Only the equity figure is synthetic; it is
            # carried through to the Stage 2 report's disclosure, never
            # silently blended with anything real.
            synthetic_as_of = _now_iso(now_dt)
            synthetic_status = {
                "MEXC": observability_module.VenueFetchStatus(
                    venue="MEXC", status="NOT_CONFIGURED", error=None,
                    fetched_at=synthetic_as_of, positions_count=0, equity=None,
                ),
                "ALPACA": observability_module.VenueFetchStatus(
                    venue="ALPACA", status="SUCCESS", error=None,
                    fetched_at=synthetic_as_of, positions_count=0, equity=synthetic_account_equity_usd,
                ),
            }
            no_broker_snapshot = observability_module._build_snapshot(synthetic_as_of, [], synthetic_status)
        else:
            no_broker_snapshot = observability_module.build_portfolio_snapshot()  # no client -- both venues NOT_CONFIGURED
        # `build_portfolio_snapshot()` always stamps its OWN real wall-clock
        # `as_of`, independent of any caller-supplied `now` -- capturing the
        # enforcement freshness check's `now` fresh, strictly AFTER building
        # the snapshot, guarantees `now >= snapshot.as_of` so `.44`'s
        # snapshot-freshness check (which treats ANY negative age as
        # SNAPSHOT_TIMESTAMP_IN_FUTURE, unconditionally, not gated by
        # `max_snapshot_age_seconds`) is never falsely tripped by ordinary
        # call-to-call latency. Deliberately decoupled from `now_dt` (which
        # may be a caller-supplied synthetic/historical timestamp used for
        # deterministic decision-engine testing elsewhere in this cycle).
        # The hand-built synthetic-equity branch stamps its own `as_of`
        # from `now_dt` directly, so it is already <= `enforcement_now`.
        enforcement_now = datetime.now(timezone.utc)
        enforcement_check_fn = build_enforcement_check_fn(
            snapshot=no_broker_snapshot, equity_history=equity_history or [], limits=limits,
            max_snapshot_age_seconds=max_snapshot_age_seconds, reference_price_fn=reference_price_fn,
            now=enforcement_now, decisions_by_symbol=decisions_by_symbol,
            enforcement_module=enforcement_module, observability_module=observability_module,
        )

    # Two independent kill switches gate this path: `.38`'s own supervisor
    # kill switch (`supervisor_config`) AND `.36`'s own, separate
    # `auth_config["kill_switch"]` (plus its execution_authorized/
    # paper_execution_authorized gates, both False by default). Opening
    # all of them here is safe ONLY because Stage 1A always calls
    # `.53.run_cycle` with `attempt_submission=False`, hardcoded above --
    # see module docstring. A caller can still override any of this via
    # `supervision_kwargs` to specifically exercise a rejection path.
    default_supervision = {
        "supervisor_config": {"kill_switch": False},
        "auth_config": {
            "kill_switch": False, "execution_authorized": True, "paper_execution_authorized": True,
            "authorization_ttl_seconds": 60, "safety_state_ttl_seconds": 60,
        },
    }
    merged_supervision = dict(default_supervision)
    if supervision_kwargs:
        merged_supervision.update(supervision_kwargs)

    inputs = tuple(
        cycle_module.SymbolCycleInput(
            symbol=r.symbol, asset_class=r.asset_class, quantity=r.quantity, already_held=False,
            sentiment_regime=r.sentiment_regime, wave_result=r.wave_result,
            technical_regime=r.technical_regime, short_technical_regime=r.short_technical_regime,
            news_item_count=r.news_item_count, alpaca_asset=r.alpaca_asset,
        )
        for r in symbol_requests
    )
    cycle_result = cycle_module.run_cycle(
        inputs, max_new_orders_per_cycle=max_new_orders_per_cycle, decide_kwargs=decide_kwargs,
        strategy_id=strategy_id, strategy_version=strategy_version, attempt_submission=False,
        enforcement_check_fn=enforcement_check_fn, now=now_dt, supervision_kwargs=merged_supervision,
    )

    kill_switch_engaged = bool(merged_supervision.get("supervisor_config", {}).get("kill_switch", True))

    records = []
    for req, outcome in zip(symbol_requests, cycle_result.outcomes):
        direction = cycle_module.OPEN_DIRECTIONS.get(getattr(outcome.decision, "outcome", None))
        shortability = build_shortability_audit(
            req.alpaca_asset, direction, asset_class=req.asset_class,
            adapter_module=adapter_module, metadata_module=metadata_module,
        )
        records.append(build_audit_record(
            symbol=req.symbol, strategy_id=strategy_id, strategy_version=strategy_version, outcome=outcome,
            intended_quantity=req.quantity, reference_price_fn=reference_price_fn, shortability=shortability,
            enforcement_decision=decisions_by_symbol.get(req.symbol), fill_result=None, resulting_position=None,
            kill_switch_engaged=kill_switch_engaged, now=now_dt,
        ))

    return Stage1CycleReport(stage="STAGE_1A_DRY_RUN", cycle_result=cycle_result, audit_records=tuple(records))


# ============================================================================
# Stage 1B -- real Alpaca paper account, real .44 enforcement, real .54
# fill reconciliation. attempt_submission is hardcoded True.
# ============================================================================


def run_stage1b_paper_cycle(
    symbol_requests: tuple[SymbolRequest, ...],
    *,
    alpaca_client: Any,
    decide_kwargs: dict[str, Any],
    max_new_orders_per_cycle: int,
    equity_history: list[dict[str, Any]],
    max_snapshot_age_seconds: float,
    reference_price_fn: Callable[[str], float],
    supervision_kwargs: dict[str, Any],
    fill_poll_timeout_seconds: float,
    fill_poll_interval_seconds: float,
    limits: Any | None = None,
    strategy_id: str = DEFAULT_STRATEGY_ID,
    strategy_version: str = DEFAULT_STRATEGY_VERSION,
    now: datetime | None = None,
) -> Stage1CycleReport:
    """`supervision_kwargs` MUST include an explicit `auth_config` (with
    `execution_authorized`/`paper_execution_authorized` set True -- `.36`'s
    own defaults are False) and `supervisor_config` (with `kill_switch`
    set False -- `.38`'s own default is True) for anything to actually
    reach the broker; this function does not choose either for the
    caller. `alpaca_client` must already expose `.get_account()`,
    `.get_all_positions()`, `.get_asset(symbol)`, `.get_order_by_client_id()`
    and `.submit_order()` -- a real alpaca-py `TradingClient(paper=True)`
    in production, matching every other Alpaca call site in this repo."""
    now_dt = now or datetime.now(timezone.utc)
    cycle_module = load_cycle_module()
    metadata_module = load_metadata_module()
    adapter_module = load_alpaca_adapter_module()
    observability_module = load_observability_module()
    enforcement_module = load_enforcement_module()
    fill_module = load_fill_reconciliation_module()

    if limits is None:
        limits = enforcement_module.PortfolioLimits()

    snapshot = observability_module.build_portfolio_snapshot(alpaca_client=alpaca_client)
    # Same ordering hazard fixed in run_stage1a_dry_run: build_portfolio_snapshot()
    # always stamps its own fresh, real wall-clock `as_of`, so a `now` captured
    # BEFORE this call can end up strictly earlier than the snapshot's own
    # timestamp, tripping .44's unconditional (not max_snapshot_age_seconds-
    # gated) SNAPSHOT_TIMESTAMP_IN_FUTURE check. Capturing the enforcement
    # freshness `now` fresh, strictly AFTER building the snapshot, guarantees
    # `enforcement_now >= snapshot.as_of` regardless of ordinary call latency.
    # `now_dt` (possibly caller-supplied/synthetic) is still used everywhere
    # else in this cycle -- only the enforcement freshness check gets its own,
    # deliberately later, real timestamp.
    enforcement_now = datetime.now(timezone.utc)
    held_symbols = {p.symbol for p in snapshot.positions if p.venue == "ALPACA"}

    metadata_fetch_failures: list[dict[str, Any]] = []
    usable_requests: list[SymbolRequest] = []
    asset_by_symbol: dict[str, dict[str, Any]] = {}
    for r in symbol_requests:
        fetched = fetch_real_alpaca_asset(alpaca_client, r.symbol, adapter_module=adapter_module)
        if fetched["status"] != "OK":
            metadata_fetch_failures.append({"symbol": r.symbol, "error": fetched["error"]})
            continue
        asset_by_symbol[r.symbol] = fetched["alpaca_asset"]
        usable_requests.append(r)

    kill_switch_engaged = bool(supervision_kwargs.get("supervisor_config", {}).get("kill_switch", True))

    decisions_by_symbol: dict[str, Any] = {}
    enforcement_check_fn = build_enforcement_check_fn(
        snapshot=snapshot, equity_history=equity_history, limits=limits,
        max_snapshot_age_seconds=max_snapshot_age_seconds, reference_price_fn=reference_price_fn,
        now=enforcement_now, decisions_by_symbol=decisions_by_symbol,
        enforcement_module=enforcement_module, observability_module=observability_module,
    )

    metadata_records = [
        build_audit_record(
            symbol=f["symbol"], strategy_id=strategy_id, strategy_version=strategy_version, outcome=None,
            intended_quantity=None, reference_price_fn=None,
            shortability=build_shortability_audit(None, None, asset_class="STOCK", adapter_module=adapter_module, metadata_module=metadata_module),
            enforcement_decision=None, fill_result=None, resulting_position=None,
            kill_switch_engaged=kill_switch_engaged, now=now_dt, cycle_stage_override="ASSET_METADATA_FETCH_FAILED",
        )
        for f in metadata_fetch_failures
    ]

    if not usable_requests:
        return Stage1CycleReport(
            stage="STAGE_1B_PAPER_CYCLE", cycle_result=None, audit_records=tuple(metadata_records),
            metadata_fetch_failures=tuple(metadata_fetch_failures),
        )

    merged_supervision = dict(supervision_kwargs)
    merged_supervision["alpaca_client"] = alpaca_client

    inputs = tuple(
        cycle_module.SymbolCycleInput(
            symbol=r.symbol, asset_class=r.asset_class, quantity=r.quantity,
            already_held=r.symbol in held_symbols,
            sentiment_regime=r.sentiment_regime, wave_result=r.wave_result,
            technical_regime=r.technical_regime, short_technical_regime=r.short_technical_regime,
            news_item_count=r.news_item_count, alpaca_asset=asset_by_symbol[r.symbol],
        )
        for r in usable_requests
    )
    cycle_result = cycle_module.run_cycle(
        inputs, max_new_orders_per_cycle=max_new_orders_per_cycle, decide_kwargs=decide_kwargs,
        strategy_id=strategy_id, strategy_version=strategy_version, attempt_submission=True,
        enforcement_check_fn=enforcement_check_fn, now=now_dt, supervision_kwargs=merged_supervision,
    )

    fills_by_symbol: dict[str, Any] = {}
    for outcome in cycle_result.outcomes:
        submission_result = (outcome.supervision_result or {}).get("submission_result") if outcome.supervision_result else None
        client_order_id = (submission_result or {}).get("client_order_id")
        if client_order_id:
            fills_by_symbol[outcome.symbol] = fill_module.poll_order_fill(
                alpaca_client, client_order_id,
                timeout_seconds=fill_poll_timeout_seconds, poll_interval_seconds=fill_poll_interval_seconds,
            )

    post_snapshot = observability_module.build_portfolio_snapshot(alpaca_client=alpaca_client)

    records = list(metadata_records)
    for req, outcome in zip(usable_requests, cycle_result.outcomes):
        direction = cycle_module.OPEN_DIRECTIONS.get(getattr(outcome.decision, "outcome", None))
        shortability = build_shortability_audit(
            asset_by_symbol[req.symbol], direction, asset_class=req.asset_class,
            adapter_module=adapter_module, metadata_module=metadata_module,
        )
        records.append(build_audit_record(
            symbol=req.symbol, strategy_id=strategy_id, strategy_version=strategy_version, outcome=outcome,
            intended_quantity=req.quantity, reference_price_fn=reference_price_fn, shortability=shortability,
            enforcement_decision=decisions_by_symbol.get(req.symbol), fill_result=fills_by_symbol.get(req.symbol),
            resulting_position=_find_position(post_snapshot, req.symbol),
            kill_switch_engaged=kill_switch_engaged, now=now_dt,
        ))

    return Stage1CycleReport(
        stage="STAGE_1B_PAPER_CYCLE", cycle_result=cycle_result, audit_records=tuple(records),
        metadata_fetch_failures=tuple(metadata_fetch_failures),
    )
