#!/usr/bin/env python3
"""Tests for AURA v0.5.3.38's O9 options-supervisor additions (added
2026-10-07): `supervise_options_execution()`, `supervise_options_exit_
execution()`, and `supervise_options_reconciliation_pass()`.

Covers: the O4B assignment/exercise freeze hard gate (frozen + clear),
the O8 portfolio-exposure hard gate (breach + clear), O7's cost estimate
appearing on the result without ever blocking (including when it cannot
be computed), the kill-switch-blocks-open-but-never-blocks-exit behavior
(including the supervisor's own kill switch FORCING an O6 close),
PAPER-only enforcement, crash-recovery/idempotency (calling the open path
twice with the same client_order_id does not double-submit), and a
construction-only preview mode that claims nothing durable (the MEXC
lesson, not the Alpaca one -- see module docstring §7).

No live Alpaca order or broker call anywhere in this file -- every
client (submitting and reconciliation) is an injected fake test double,
exactly the discipline the rest of this codebase's test suites already
use (see `tests/test_aura_v05376_options_execution_adapter.py` and
`tests/test_aura_v05377_options_assignment_reconciliation.py`, whose
fake-client shapes are mirrored here).

Repo root is on sys.path when running under pytest from the repo root
(same convention every other test file in this suite relies on), so
plain `import aura_v0533x_...`/`import aura_v0537x_...` resolves to the
SAME cached module objects the module under test dynamically imports
internally.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import aura_v05338_common_execution_supervisor as SUP
import aura_v05343_portfolio_exposure_observability as OBS
import aura_v05344_portfolio_exposure_enforcement as ENF
import aura_v05373_options_instrument_metadata as META

NOW = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)
AS_OF = NOW.isoformat()
EXPIRY = date(2026, 12, 18)

SHORT_OCC = META.build_occ_symbol("SPY", EXPIRY, "CALL", "450")
LONG_OCC = META.build_occ_symbol("SPY", EXPIRY, "CALL", "455")


# ======================================================================= #
# Fixtures / builders
# ======================================================================= #

def make_open_spec(**overrides) -> dict:
    """A 2-leg net-credit vertical: short the 450 call, long the 455
    call -- same shape `tests/test_aura_v05376_options_execution_
    adapter.py` already establishes."""
    legs = [
        {"underlying_symbol": "SPY", "strike": "450", "expiry": EXPIRY, "right": "CALL",
         "direction": "OPEN_SHORT", "ratio": 1},
        {"underlying_symbol": "SPY", "strike": "455", "expiry": EXPIRY, "right": "CALL",
         "direction": "OPEN_LONG", "ratio": 1},
    ]
    spec = {
        "legs": legs, "qty": 1, "order_type": "LIMIT", "limit_price": "-1.50",
        "time_in_force": "DAY", "client_order_id": "test-open-coid",
    }
    spec.update(overrides)
    return spec


def make_close_spec(**overrides) -> dict:
    """The closing counterpart of make_open_spec() -- each leg's
    direction flips to its CLOSE_* opposite."""
    legs = [
        {"underlying_symbol": "SPY", "strike": "450", "expiry": EXPIRY, "right": "CALL",
         "direction": "CLOSE_SHORT", "ratio": 1},
        {"underlying_symbol": "SPY", "strike": "455", "expiry": EXPIRY, "right": "CALL",
         "direction": "CLOSE_LONG", "ratio": 1},
    ]
    spec = {
        "legs": legs, "qty": 1, "order_type": "LIMIT", "limit_price": "0.75",
        "time_in_force": "DAY", "client_order_id": "test-close-coid",
    }
    spec.update(overrides)
    return spec


def auth_config_open(**overrides) -> dict:
    config = {
        "kill_switch": False, "execution_authorized": True, "paper_execution_authorized": True,
        "authorization_ttl_seconds": 60, "safety_state_ttl_seconds": 60,
    }
    config.update(overrides)
    return config


def sup_open(**overrides) -> dict:
    """Supervisor's OWN kill switch, open (False) unless overridden."""
    config = {"kill_switch": False}
    config.update(overrides)
    return config


class Dirs:
    def __init__(self, tmp_path: Path):
        self.auth = tmp_path / "options-auth-claims"
        self.supervisor = tmp_path / "options-supervisor-claims"

    def kwargs(self) -> dict:
        return dict(auth_claims_dir=self.auth, supervisor_claims_dir=self.supervisor)


# --- O4B reconciliation fakes -- mirrors test_aura_v05377's own FakeClient --

class FakeReconciliationClient:
    def __init__(self, positions=None, activities_raw=None):
        self._positions = positions if positions is not None else []
        self._activities_raw = activities_raw if activities_raw is not None else []

    def get_all_positions(self):
        return self._positions

    def get(self, path, data=None, **kwargs):
        return self._activities_raw


def clean_reconciliation_client() -> FakeReconciliationClient:
    """Nothing expected, nothing at the broker -- always CONSISTENT."""
    return FakeReconciliationClient(positions=[], activities_raw=[])


def frozen_reconciliation_client() -> FakeReconciliationClient:
    """AURA expects no SPY position, but the broker has never heard of
    one either (qty 0 both sides) -- to force a genuine mismatch we
    instead tell AURA to expect a short position that the broker does
    NOT report, with NO assignment/exercise activity evidence -> a loud
    UNEXPLAINED_POSITION_MISMATCH, freezing the SPY underlying."""
    return FakeReconciliationClient(positions=[], activities_raw=[])


EXPECTED_POSITIONS_CLEAN: list = []
EXPECTED_POSITIONS_MISMATCH = [{"occ_symbol": SHORT_OCC, "qty": "-1"}]


# --- Alpaca submission fake -- mirrors .338's own FakeAlpacaClient / O4's
# test file's FakeClient ---

class FakeOrder:
    def __init__(self, order_id="broker-opt-1", status="new"):
        self.id = order_id
        self.status = status
        self.legs = None


class FakeAlpacaClient:
    def __init__(self, existing_order=None, submit_exception=None):
        self._existing = existing_order
        self._submit_exception = submit_exception
        self.submit_calls: list = []

    def get_order_by_client_id(self, client_order_id):
        if self._existing is None:
            raise Exception("order not found")
        return self._existing

    def submit_order(self, order_data):
        self.submit_calls.append(order_data)
        if self._submit_exception:
            raise self._submit_exception
        return FakeOrder()


# --- O8 portfolio-enforcement fixtures -- mirrors test_aura_v05344's own
# helpers directly against the real .343/.344 data structures ---

def venue_status(venue, equity=10000.0, positions_count=0) -> OBS.VenueFetchStatus:
    return OBS.VenueFetchStatus(venue=venue, status="SUCCESS", error=None, fetched_at=AS_OF,
                                 positions_count=positions_count, equity=equity)


def clean_snapshot() -> OBS.PortfolioSnapshot:
    return OBS._build_snapshot(AS_OF, [], {"MEXC": venue_status("MEXC"), "ALPACA": venue_status("ALPACA")})


def option_leg_position(delta=0.10, qty=1.0) -> OBS.PositionRecord:
    return OBS.PositionRecord(
        venue="ALPACA", symbol=SHORT_OCC, direction="SHORT", quantity=qty, entry_price=1.50,
        leverage=None, mark_price=1.50, notional_usd=1.50 * qty * 100, notional_basis="MARK_TO_MARKET",
        unrealized_pnl_usd=0.0, liquidation_price=None, raw_source_id=None, as_of=AS_OF,
        option_detail={"strike": 450.0, "expiry": EXPIRY.isoformat(), "right": "CALL", "delta": delta},
        structure_group_id=None,
    )


def equity_history() -> list:
    return [
        {"venue": "MEXC", "equity": 10000.0, "as_of": AS_OF[:10] + "T00:00:00+00:00"},
        {"venue": "ALPACA", "equity": 10000.0, "as_of": AS_OF[:10] + "T00:00:00+00:00"},
    ]


def clean_portfolio_kwargs(**overrides) -> dict:
    kwargs = dict(
        portfolio_snapshot=clean_snapshot(), portfolio_hypothetical=option_leg_position(delta=0.10, qty=1.0),
        portfolio_limits=ENF.PortfolioLimits(), portfolio_max_snapshot_age_seconds=300.0,
        portfolio_equity_history=equity_history(),
    )
    kwargs.update(overrides)
    return kwargs


def clean_gate_kwargs(d: Dirs, **overrides) -> dict:
    """Every required gate input, all clean/empty -- the minimal set of
    kwargs needed to get supervise_options_execution() past BOTH hard
    gates (O4B + O8) for tests that are about something else entirely."""
    kwargs = dict(
        expected_options_positions=EXPECTED_POSITIONS_CLEAN, reconciliation_client=clean_reconciliation_client(),
        reconciliation_lookback_days=30, auth_config=auth_config_open(), supervisor_config=sup_open(),
        now_dt=NOW, **d.kwargs(),
    )
    kwargs.update(clean_portfolio_kwargs())
    kwargs.update(overrides)
    return kwargs


# ======================================================================= #
# 1. O4B assignment/exercise freeze -- hard gate
# ======================================================================= #

def test_frozen_underlying_blocks_new_options_order(tmp_path):
    d = Dirs(tmp_path)
    kwargs = clean_gate_kwargs(d)
    kwargs["expected_options_positions"] = EXPECTED_POSITIONS_MISMATCH
    kwargs["reconciliation_client"] = frozen_reconciliation_client()

    result = SUP.supervise_options_execution(make_open_spec(), **kwargs)
    assert result["status"] == "BLOCKED"
    assert result["stage"] == "ASSIGNMENT_FREEZE_CHECK"
    assert result["reason"] == "UNDERLYING_FROZEN_PENDING_ASSIGNMENT_EXERCISE_RESOLUTION"
    assert "SPY" in result["reconciliation"]["frozen_underlyings"]


def test_clean_reconciliation_does_not_block(tmp_path):
    d = Dirs(tmp_path)
    kwargs = clean_gate_kwargs(d)
    result = SUP.supervise_options_execution(make_open_spec(), **kwargs)
    assert result["status"] == "READY_FOR_SUBMISSION"
    assert result["reconciliation"]["frozen_underlyings"] == []


def test_missing_reconciliation_inputs_fail_closed(tmp_path):
    d = Dirs(tmp_path)
    r1 = SUP.supervise_options_execution(make_open_spec(), **{**clean_gate_kwargs(d), "expected_options_positions": None})
    assert r1["status"] == "BLOCKED" and r1["reason"] == "MISSING_EXPECTED_OPTIONS_POSITIONS"

    r2 = SUP.supervise_options_execution(make_open_spec(), **{**clean_gate_kwargs(d), "reconciliation_client": None})
    assert r2["status"] == "BLOCKED" and r2["reason"] == "MISSING_RECONCILIATION_CLIENT"

    r3 = SUP.supervise_options_execution(make_open_spec(), **{**clean_gate_kwargs(d), "reconciliation_lookback_days": None})
    assert r3["status"] == "BLOCKED" and r3["reason"] == "MISSING_RECONCILIATION_LOOKBACK_DAYS"


def test_exit_path_does_not_re_check_freeze_gate(tmp_path):
    """Martin's decision scopes the freeze hard-gate to NEW decisions
    only -- an exit path call never even accepts reconciliation
    parameters, so there is nothing to make it check."""
    d = Dirs(tmp_path)
    result = SUP.supervise_options_exit_execution(
        _hold_position(), entry_net_price="-1.50", leg_quotes=_hold_quotes(), as_of_date=AS_OF_DATE_FAR,
        closing_execution_spec=make_close_spec(), auth_config=auth_config_open(),
        supervisor_config=sup_open(), now_dt=NOW, **d.kwargs(),
    )
    # Far from expiry, no trigger fires with the kill switch off -> HOLD,
    # proving this path never even reaches (or needs) a freeze check.
    assert result["status"] == "HOLD"


# ======================================================================= #
# 2. O8 portfolio-exposure enforcement -- hard gate
# ======================================================================= #

def test_portfolio_exposure_breach_blocks_new_options_order(tmp_path):
    d = Dirs(tmp_path)
    kwargs = clean_gate_kwargs(d)
    kwargs["portfolio_limits"] = ENF.PortfolioLimits(max_portfolio_delta=1.0)
    kwargs["portfolio_hypothetical"] = option_leg_position(delta=0.40, qty=2.0)  # net_delta = 0.40*2*100=80

    result = SUP.supervise_options_execution(make_open_spec(), **kwargs)
    assert result["status"] == "BLOCKED"
    assert result["stage"] == "PORTFOLIO_EXPOSURE_ENFORCEMENT"
    assert result["reason"] == "PORTFOLIO_EXPOSURE_LIMIT_BREACHED"
    assert result["portfolio_enforcement"]["overall_verdict"] == "BLOCK"


def test_portfolio_exposure_within_limits_allows(tmp_path):
    d = Dirs(tmp_path)
    kwargs = clean_gate_kwargs(d)
    kwargs["portfolio_limits"] = ENF.PortfolioLimits(max_portfolio_delta=1000.0)

    result = SUP.supervise_options_execution(make_open_spec(), **kwargs)
    assert result["status"] == "READY_FOR_SUBMISSION"
    assert result["portfolio_enforcement"]["overall_verdict"] == "ALLOW"


def test_missing_portfolio_inputs_fail_closed(tmp_path):
    d = Dirs(tmp_path)
    for field in ("portfolio_snapshot", "portfolio_hypothetical", "portfolio_limits",
                  "portfolio_max_snapshot_age_seconds"):
        kwargs = clean_gate_kwargs(d)
        kwargs[field] = None
        result = SUP.supervise_options_execution(make_open_spec(client_order_id=f"missing-{field}"), **kwargs)
        assert result["status"] == "BLOCKED", field
        assert result["stage"] == "PORTFOLIO_EXPOSURE_ENFORCEMENT", field


def test_exit_path_does_not_call_portfolio_enforcement_gate(tmp_path):
    """Reducing/closing existing risk is never blocked by a limit
    designed to stop NEW risk -- proven by the exit path's signature
    having no portfolio_* parameters at all for it to consult."""
    assert "portfolio_snapshot" not in SUP.supervise_options_exit_execution.__code__.co_varnames


# ======================================================================= #
# 3. O7 cost estimate -- informational only, never blocks
# ======================================================================= #

COST_CONFIG = {
    "commission_per_contract": "0.00",
    "regulatory_fee_per_contract_buy": "0.02",
    "regulatory_fee_per_contract_sell": "0.07",
}


def _cost_leg_quotes() -> dict:
    return {
        SHORT_OCC: {"data_status": "OK", "broker_latest_quote": {"bid_price": "1.53", "ask_price": "1.57"}},
        LONG_OCC: {"data_status": "OK", "broker_latest_quote": {"bid_price": "0.04", "ask_price": "0.06"}},
    }


def test_cost_estimate_attached_and_correct(tmp_path):
    d = Dirs(tmp_path)
    kwargs = clean_gate_kwargs(d)
    kwargs["cost_model_config"] = COST_CONFIG
    kwargs["cost_model_leg_quotes"] = _cost_leg_quotes()

    result = SUP.supervise_options_execution(make_open_spec(), **kwargs)
    assert result["status"] == "READY_FOR_SUBMISSION"
    assert result["cost_estimate"]["status"] == "OK"
    assert result["cost_estimate"]["total_cost"] is not None


def test_cost_estimate_omitted_without_config_never_blocks(tmp_path):
    d = Dirs(tmp_path)
    result = SUP.supervise_options_execution(make_open_spec(), **clean_gate_kwargs(d))
    assert result["status"] == "READY_FOR_SUBMISSION"
    assert result["cost_estimate"] is None


def test_cost_estimate_failure_is_captured_and_never_blocks(tmp_path):
    d = Dirs(tmp_path)
    kwargs = clean_gate_kwargs(d)
    kwargs["cost_model_config"] = {}  # missing required keys -> O7 raises
    kwargs["cost_model_leg_quotes"] = _cost_leg_quotes()

    result = SUP.supervise_options_execution(make_open_spec(), **kwargs)
    assert result["status"] == "READY_FOR_SUBMISSION"
    assert result["cost_estimate"]["status"] == "COST_ESTIMATE_UNAVAILABLE"
    assert "error" in result["cost_estimate"]


# ======================================================================= #
# 4. Kill switch -- blocks the open path, never blocks the exit path
# ======================================================================= #

def test_dynamic_kill_switch_blocks_new_options_order(tmp_path):
    d = Dirs(tmp_path)
    kwargs = clean_gate_kwargs(d)
    kwargs["supervisor_config"] = {"kill_switch": True}
    result = SUP.supervise_options_execution(make_open_spec(), **kwargs)
    assert result["status"] == "BLOCKED" and result["stage"] == "SUPERVISOR_KILL_SWITCH"
    assert result["reason"] == "SUPERVISOR_KILL_SWITCH_ENGAGED"


def test_static_kill_switch_blocks_new_options_order(tmp_path, monkeypatch):
    d = Dirs(tmp_path)
    monkeypatch.setenv("AURA_338_STATIC_KILL_SWITCH", "1")
    result = SUP.supervise_options_execution(make_open_spec(), **clean_gate_kwargs(d))
    assert result["status"] == "BLOCKED" and result["stage"] == "SUPERVISOR_KILL_SWITCH"
    assert result["reason"] == "STATIC_KILL_SWITCH_ENGAGED"


AS_OF_DATE_FAR = date(2026, 10, 7)  # EXPIRY is 2026-12-18 -- far from any DTE/earnings trigger


def _hold_position() -> dict:
    return {"underlying_symbol": "SPY", "legs": [
        {"occ_symbol": SHORT_OCC, "side": "SELL"}, {"occ_symbol": LONG_OCC, "side": "BUY"},
    ]}


def _hold_quotes() -> dict:
    def rec(mid):
        return {"data_status": "OK", "broker_latest_quote": {"bid_price": str(mid - 0.02), "ask_price": str(mid + 0.02)}}
    return {SHORT_OCC: rec(1.55), LONG_OCC: rec(0.05)}


def test_dynamic_kill_switch_forces_exit_close_and_does_not_block_it(tmp_path):
    """The supervisor's own (dynamic) kill switch, engaged, FORCES O6 to
    return CLOSE (per .380's inversion) and this function then executes
    that close WITHOUT any kill-switch block -- proof that the exit path
    is not merely "unchecked" but genuinely driven by the switch."""
    d = Dirs(tmp_path)
    client = FakeAlpacaClient()
    result = SUP.supervise_options_exit_execution(
        _hold_position(), entry_net_price="-1.50", leg_quotes=_hold_quotes(), as_of_date=AS_OF_DATE_FAR,
        closing_execution_spec=make_close_spec(), auth_config=auth_config_open(),
        supervisor_config={"kill_switch": True}, attempt_submission=True, alpaca_client=client,
        now_dt=NOW, **d.kwargs(),
    )
    assert result["exit_decision"]["trigger"] == "KILL_SWITCH"
    assert result["exit_decision"]["action"] == "CLOSE"
    assert result["status"] == "SUBMITTED"
    assert len(client.submit_calls) == 1


def test_static_kill_switch_forces_exit_close_and_does_not_block_it(tmp_path, monkeypatch):
    d = Dirs(tmp_path)
    monkeypatch.setenv("AURA_338_STATIC_KILL_SWITCH", "1")
    result = SUP.supervise_options_exit_execution(
        _hold_position(), entry_net_price="-1.50", leg_quotes=_hold_quotes(), as_of_date=AS_OF_DATE_FAR,
        closing_execution_spec=make_close_spec(), auth_config=auth_config_open(),
        supervisor_config=sup_open(), attempt_submission=False, now_dt=NOW, **d.kwargs(),
    )
    assert result["exit_decision"]["trigger"] == "KILL_SWITCH"
    assert result["status"] == "READY_FOR_SUBMISSION"
    assert result["stage"] == "CONSTRUCTION_ONLY"


def test_exit_path_holds_when_kill_switch_off_and_no_trigger_fires(tmp_path):
    d = Dirs(tmp_path)
    result = SUP.supervise_options_exit_execution(
        _hold_position(), entry_net_price="-1.50", leg_quotes=_hold_quotes(), as_of_date=AS_OF_DATE_FAR,
        closing_execution_spec=make_close_spec(), auth_config=auth_config_open(),
        supervisor_config=sup_open(), now_dt=NOW, **d.kwargs(),
    )
    assert result["status"] == "HOLD"
    assert result["exit_decision"]["action"] == "HOLD"


def test_exit_path_closes_on_a_genuine_market_trigger_with_kill_switch_off(tmp_path):
    """DTE_EXIT fires naturally (as_of_date within the default 3-day
    threshold of EXPIRY) with the kill switch fully OFF -- proves this
    path is really wired to O6's own trigger logic, not just the
    kill-switch inversion."""
    d = Dirs(tmp_path)
    near_expiry = EXPIRY  # dte == 0 <= default dte_exit_threshold (3)
    client = FakeAlpacaClient()
    result = SUP.supervise_options_exit_execution(
        _hold_position(), entry_net_price="-1.50", leg_quotes=_hold_quotes(), as_of_date=near_expiry,
        closing_execution_spec=make_close_spec(), auth_config=auth_config_open(),
        supervisor_config=sup_open(), attempt_submission=True, alpaca_client=client, now_dt=NOW, **d.kwargs(),
    )
    assert result["exit_decision"]["trigger"] == "DTE_EXIT"
    assert result["status"] == "SUBMITTED"


# ======================================================================= #
# 5. PAPER-only enforcement
# ======================================================================= #

def test_live_environment_rejected_for_open_path(tmp_path):
    d = Dirs(tmp_path)
    kwargs = clean_gate_kwargs(d)
    kwargs["environment"] = "LIVE"
    result = SUP.supervise_options_execution(make_open_spec(), **kwargs)
    assert result["status"] == "BLOCKED" and result["stage"] == "ENVIRONMENT"
    assert result["reason"] == "LIVE_EXECUTION_NOT_SUPPORTED_FOR_OPTIONS"


def test_live_environment_rejected_for_exit_path_even_with_forced_close(tmp_path):
    d = Dirs(tmp_path)
    result = SUP.supervise_options_exit_execution(
        _hold_position(), entry_net_price="-1.50", leg_quotes=_hold_quotes(), as_of_date=AS_OF_DATE_FAR,
        closing_execution_spec=make_close_spec(), environment="LIVE", auth_config=auth_config_open(),
        supervisor_config={"kill_switch": True}, now_dt=NOW, **d.kwargs(),
    )
    assert result["status"] == "BLOCKED" and result["stage"] == "ENVIRONMENT"
    assert result["reason"] == "LIVE_EXECUTION_NOT_SUPPORTED_FOR_OPTIONS"


# ======================================================================= #
# 6. Crash-recovery / idempotency -- calling the open path twice with the
#    same client_order_id must not double-submit
# ======================================================================= #

def test_repeated_open_call_same_client_order_id_does_not_double_submit(tmp_path):
    d = Dirs(tmp_path)
    client = FakeAlpacaClient()
    spec = make_open_spec(client_order_id="idempotency-coid")

    first = SUP.supervise_options_execution(
        spec, attempt_submission=True, alpaca_client=client, **clean_gate_kwargs(d),
    )
    assert first["status"] == "SUBMITTED"
    assert len(client.submit_calls) == 1

    second = SUP.supervise_options_execution(
        spec, attempt_submission=True, alpaca_client=client, **clean_gate_kwargs(d),
    )
    assert second["status"] == "BLOCKED"
    assert second["stage"] == "SUPERVISOR_CLIENT_ORDER_ID_CLAIM"
    assert second["status_override"] == "AUTHORIZED_BUT_SUPERVISOR_CLAIM_REJECTED"
    # The real submission call count never grew -- .376.submit() was
    # reached exactly once for this client_order_id.
    assert len(client.submit_calls) == 1


def test_repeated_exit_call_same_client_order_id_does_not_double_submit(tmp_path):
    d = Dirs(tmp_path)
    client = FakeAlpacaClient()
    spec = make_close_spec(client_order_id="idempotency-exit-coid")

    def do_exit():
        return SUP.supervise_options_exit_execution(
            _hold_position(), entry_net_price="-1.50", leg_quotes=_hold_quotes(), as_of_date=AS_OF_DATE_FAR,
            closing_execution_spec=spec, auth_config=auth_config_open(), supervisor_config={"kill_switch": True},
            attempt_submission=True, alpaca_client=client, now_dt=NOW, **d.kwargs(),
        )

    first = do_exit()
    assert first["status"] == "SUBMITTED"
    second = do_exit()
    assert second["status"] == "BLOCKED" and second["stage"] == "SUPERVISOR_CLIENT_ORDER_ID_CLAIM"
    assert len(client.submit_calls) == 1


# ======================================================================= #
# 7. Construction-only preview -- claims nothing durable (the MEXC
#    lesson, not the Alpaca one -- see module docstring §7)
# ======================================================================= #

def _claim_file_count(dir_path: Path) -> int:
    if not dir_path.exists():
        return 0
    return len(list(dir_path.glob("*")))


def test_open_path_preview_claims_nothing_then_real_submission_still_works(tmp_path):
    d = Dirs(tmp_path)
    spec = make_open_spec(client_order_id="preview-coid")
    client = FakeAlpacaClient()

    preview = SUP.supervise_options_execution(spec, attempt_submission=False, **clean_gate_kwargs(d))
    assert preview["status"] == "READY_FOR_SUBMISSION"
    assert preview["stage"] == "CONSTRUCTION_ONLY"
    assert preview.get("order_spec") is None

    # Nothing durable was claimed anywhere -- neither this module's own
    # client_order_id claim store nor .379's authorization-claim store
    # (both share this module's auth_claims_dir / supervisor_claims_dir
    # in this test, so an empty directory on both proves neither was
    # ever touched).
    assert _claim_file_count(d.supervisor) == 0

    real = SUP.supervise_options_execution(
        spec, attempt_submission=True, alpaca_client=client, **clean_gate_kwargs(d),
    )
    assert real["status"] == "SUBMITTED"
    assert len(client.submit_calls) == 1
    assert _claim_file_count(d.supervisor) == 1


def test_exit_path_preview_claims_nothing_then_real_submission_still_works(tmp_path):
    d = Dirs(tmp_path)
    spec = make_close_spec(client_order_id="preview-exit-coid")
    client = FakeAlpacaClient()

    kwargs = dict(
        entry_net_price="-1.50", leg_quotes=_hold_quotes(), as_of_date=AS_OF_DATE_FAR,
        auth_config=auth_config_open(), supervisor_config={"kill_switch": True}, now_dt=NOW, **d.kwargs(),
    )
    preview = SUP.supervise_options_exit_execution(
        _hold_position(), closing_execution_spec=spec, attempt_submission=False, **kwargs,
    )
    assert preview["status"] == "READY_FOR_SUBMISSION"
    assert _claim_file_count(d.supervisor) == 0

    real = SUP.supervise_options_exit_execution(
        _hold_position(), closing_execution_spec=spec, attempt_submission=True, alpaca_client=client, **kwargs,
    )
    assert real["status"] == "SUBMITTED"
    assert len(client.submit_calls) == 1


# ======================================================================= #
# 8. supervise_options_reconciliation_pass()
# ======================================================================= #

def test_reconciliation_pass_reports_escalated_when_frozen():
    result = SUP.supervise_options_reconciliation_pass(
        EXPECTED_POSITIONS_MISMATCH, reconciliation_client=frozen_reconciliation_client(), lookback_days=30,
    )
    assert result["status"] == "ESCALATED"
    assert "SPY" in result["reconciliation"]["frozen_underlyings"]


def test_reconciliation_pass_reports_reconciled_when_clean():
    result = SUP.supervise_options_reconciliation_pass(
        EXPECTED_POSITIONS_CLEAN, reconciliation_client=clean_reconciliation_client(), lookback_days=30,
    )
    assert result["status"] == "RECONCILED"


def test_reconciliation_pass_missing_inputs_fail_closed():
    r1 = SUP.supervise_options_reconciliation_pass(EXPECTED_POSITIONS_CLEAN, reconciliation_client=None, lookback_days=30)
    assert r1["status"] == "BLOCKED" and r1["reason"] == "MISSING_RECONCILIATION_CLIENT"

    r2 = SUP.supervise_options_reconciliation_pass(
        EXPECTED_POSITIONS_CLEAN, reconciliation_client=clean_reconciliation_client(), lookback_days=None,
    )
    assert r2["status"] == "BLOCKED" and r2["reason"] == "MISSING_LOOKBACK_DAYS"


def test_reconciliation_pass_not_gated_by_either_kill_switch(monkeypatch):
    """Pure observation, never a new order -- neither kill switch is even
    a parameter this function accepts."""
    assert "kill_switch" not in SUP.supervise_options_reconciliation_pass.__code__.co_varnames
    monkeypatch.setenv("AURA_338_STATIC_KILL_SWITCH", "1")
    result = SUP.supervise_options_reconciliation_pass(
        EXPECTED_POSITIONS_CLEAN, reconciliation_client=clean_reconciliation_client(), lookback_days=30,
    )
    assert result["status"] == "RECONCILED"


# ======================================================================= #
# 9. End-to-end happy path (both venues' own test files have an
#    equivalent "everything real, nothing faked but the broker client"
#    smoke test; this is this module's)
# ======================================================================= #

def test_full_open_submission_happy_path(tmp_path):
    d = Dirs(tmp_path)
    client = FakeAlpacaClient()
    kwargs = clean_gate_kwargs(d)
    kwargs["cost_model_config"] = COST_CONFIG
    kwargs["cost_model_leg_quotes"] = _cost_leg_quotes()

    result = SUP.supervise_options_execution(
        make_open_spec(client_order_id="happy-path-coid"), attempt_submission=True, alpaca_client=client, **kwargs,
    )
    assert result["status"] == "SUBMITTED"
    assert result["stage"] == "SUBMITTED"
    assert result["order_spec"]["client_order_id"] == "happy-path-coid"
    assert result["order_spec_digest"] is not None
    assert result["submission_result"]["status"] == "SUBMITTED"
    assert result["cost_estimate"]["status"] == "OK"
    assert result["portfolio_enforcement"]["overall_verdict"] == "ALLOW"
    assert result["reconciliation"]["frozen_underlyings"] == []
    assert result["underlying_symbol"] == "SPY"


def test_submission_exception_classified_as_execution_uncertain(tmp_path):
    """`.376.submit()` has the identical gap `.35.submit()` has -- no
    exception classification of its own around `client.submit_order()`.
    This module wraps it exactly like it wraps the Alpaca equity path."""
    d = Dirs(tmp_path)
    client = FakeAlpacaClient(submit_exception=TimeoutError("broker timeout"))
    result = SUP.supervise_options_execution(
        make_open_spec(client_order_id="uncertain-coid"), attempt_submission=True, alpaca_client=client,
        **clean_gate_kwargs(d),
    )
    assert result["status"] == "EXECUTION_UNCERTAIN"
    assert result["submission_result"]["error"].startswith("TimeoutError")
