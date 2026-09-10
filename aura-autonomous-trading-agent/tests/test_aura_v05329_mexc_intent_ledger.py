#!/usr/bin/env python3
"""Contract tests for AURA v0.5.3.29 MEXC Intent Ledger.

Covers the transition/evidence test matrix's Sec.1 (legal/illegal
transitions) and Sec.2 (structural integrity), plus the crash-retry
idempotency rule locked 2026-09-09 (identical repeated event -> no-op,
conflicting event at the same logical point -> escalation).

No MEXC, no network, no ccxt -- this module never touches any of those.
"""
from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "aura_v05329_mexc_intent_ledger.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("aura_v05329_mexc_intent_ledger", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


MOD = _load_module()


def expect(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"PASS: {name}")


def expect_raises(name: str, fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except RuntimeError:
        print(f"PASS: {name}")
        return
    raise AssertionError(f"{name} (expected RuntimeError, none raised)")


# --------------------------------------------------------------------- #
# fixture helpers
# --------------------------------------------------------------------- #

def new_intent(base_dir: Path, client_order_id: str = "aura-test-29-001", **overrides) -> dict:
    kwargs = dict(
        client_order_id=client_order_id,
        symbol="BTC/USDT:USDT",
        side="buy",
        quantity=1.0,
        direction="OPEN_LONG",
        spec_fingerprint="sha256:test-fingerprint",
        base_dir=base_dir,
    )
    kwargs.update(overrides)
    return MOD.create_intent(**kwargs)


def advance_to(base_dir: Path, state: str, client_order_id: str = "aura-test-29-001", partial_terminal: bool | None = None) -> dict:
    """Build a record up to (and including) `state` via the real
    mutators -- never hand-constructs a record, so every fixture is
    itself proof the legal path actually works."""
    new_intent(base_dir, client_order_id)
    if state == "NEW":
        return MOD.get_intent(client_order_id, base_dir)

    MOD.record_claimed(client_order_id, base_dir=base_dir)
    if state == "CLAIMED":
        return MOD.get_intent(client_order_id, base_dir)

    MOD.record_submission_attempted(client_order_id, base_dir=base_dir)
    if state == "SUBMISSION_ATTEMPTED":
        return MOD.get_intent(client_order_id, base_dir)

    if state == "TERMINAL_SUBMISSION_REJECTED":
        return MOD.record_submission_outcome(client_order_id, "SUBMISSION_REJECTED", base_dir=base_dir)

    MOD.record_submission_outcome(client_order_id, "SUBMISSION_ACKNOWLEDGED", mexc_order_id="mexc-1", base_dir=base_dir)
    if state == "AWAITING_RECONCILIATION":
        return MOD.get_intent(client_order_id, base_dir)

    if state == "RECONCILING":
        return MOD.record_reconciliation_attempt(client_order_id, "snaphash-1", "UNRESOLVED", base_dir=base_dir)

    if state == "RECONCILED_FILLED":
        return MOD.record_reconciliation_attempt(client_order_id, "snaphash-1", "FILLED", base_dir=base_dir)

    if state == "RECONCILED_CANCELED":
        return MOD.record_reconciliation_attempt(client_order_id, "snaphash-1", "CANCELED", base_dir=base_dir)

    if state == "RECONCILED_PARTIALLY_FILLED":
        return MOD.record_reconciliation_attempt(
            client_order_id, "snaphash-1", "PARTIALLY_FILLED",
            order_terminal_on_exchange=partial_terminal, base_dir=base_dir,
        )

    if state == "ESCALATED_HUMAN_REVIEW":
        MOD.record_reconciliation_attempt(client_order_id, "snaphash-1", "UNRESOLVED", base_dir=base_dir)
        return MOD.escalate_to_human(client_order_id, "test escalation", base_dir=base_dir)

    raise ValueError(f"unknown fixture state: {state}")


# --------------------------------------------------------------------- #
# create_intent
# --------------------------------------------------------------------- #

def test_create_intent_basic() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        record = new_intent(base)
        expect("create: engine", record["engine"] == "MEXC_INTENT_LEDGER")
        expect("create: schema_version", record["schema_version"] == "1.0")
        expect("create: current_state NEW", record["current_state"] == "NEW")
        expect("create: partial_fill_terminal None", record["partial_fill_terminal"] is None)
        expect("create: one INTENT_CREATED event", len(record["events"]) == 1)
        expect("create: event type", record["events"][0]["event"] == "INTENT_CREATED")
        expect("create: hash present", isinstance(record["record_hash"], str) and record["record_hash"])
        ok, errors = MOD.verify_intent(record)
        expect(f"create: verifies clean ({errors})", ok)


def test_create_intent_validation() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        expect_raises("create: invalid side", new_intent, base, side="hold")
        expect_raises("create: invalid direction", new_intent, base, direction="SIDEWAYS")
        expect_raises("create: non-positive quantity", new_intent, base, quantity=0)
        expect_raises("create: invalid client_order_id charset", new_intent, base, client_order_id="bad id!")


def test_create_intent_atomic_refusal() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        new_intent(base, client_order_id="aura-test-29-dup")
        expect_raises(
            "create: second create for same client_order_id refused",
            new_intent, base, client_order_id="aura-test-29-dup",
        )
        record = MOD.get_intent("aura-test-29-dup", base)
        expect("create: exactly one record survives, unaffected by refused retry", len(record["events"]) == 1)


def test_direction_all_four_values_and_immutable() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        for i, direction in enumerate(sorted(MOD.DIRECTIONS)):
            coid = f"aura-test-29-dir-{i}"
            record = new_intent(base, client_order_id=coid, direction=direction)
            expect(f"direction accepted: {direction}", record["direction"] == direction)
        # No mutator in this module's interface accepts a `direction`
        # parameter -- confirmed structurally, not just by convention.
        import inspect
        for fn in (MOD.record_claimed, MOD.record_submission_attempted, MOD.record_submission_outcome,
                   MOD.record_reconciliation_attempt, MOD.escalate_to_human):
            params = inspect.signature(fn).parameters
            expect(f"no mutator exposes 'direction': {fn.__name__}", "direction" not in params)


# --------------------------------------------------------------------- #
# legal transitions -- the "happy path" through the whole matrix
# --------------------------------------------------------------------- #

def test_legal_path_clean_fill() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        coid = "aura-test-29-fill"
        new_intent(base, client_order_id=coid)
        MOD.record_claimed(coid, base_dir=base)
        MOD.record_submission_attempted(coid, base_dir=base)
        r = MOD.record_submission_outcome(coid, "SUBMISSION_ACKNOWLEDGED", mexc_order_id="m1", base_dir=base)
        expect("path: AWAITING_RECONCILIATION reached", r["current_state"] == "AWAITING_RECONCILIATION")
        r = MOD.record_reconciliation_attempt(coid, "snap-1", "FILLED", base_dir=base)
        expect("path: RECONCILED_FILLED reached", r["current_state"] == "RECONCILED_FILLED")
        ok, errors = MOD.verify_intent(r)
        expect(f"path: verifies clean ({errors})", ok)


def test_legal_path_execution_uncertain_identical_to_acknowledged() -> None:
    """.29's own locked note: SUBMISSION_ACKNOWLEDGED and
    EXECUTION_UNCERTAIN funnel to the exact same place -- no
    evidentiary head start for the acknowledged case."""
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        for outcome, coid in (("SUBMISSION_ACKNOWLEDGED", "aura-29-a"), ("EXECUTION_UNCERTAIN", "aura-29-b")):
            new_intent(base, client_order_id=coid)
            MOD.record_claimed(coid, base_dir=base)
            MOD.record_submission_attempted(coid, base_dir=base)
            r = MOD.record_submission_outcome(coid, outcome, base_dir=base)
            expect(f"uncertain-vs-ack: {outcome} -> AWAITING_RECONCILIATION", r["current_state"] == "AWAITING_RECONCILIATION")


def test_legal_path_pre_submission_rejected_skips_reconciliation() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        coid = "aura-test-29-rejected"
        new_intent(base, client_order_id=coid)
        MOD.record_claimed(coid, base_dir=base)
        MOD.record_submission_attempted(coid, base_dir=base)
        r = MOD.record_submission_outcome(coid, "SUBMISSION_REJECTED", base_dir=base)
        expect("pre-submission reject: TERMINAL_SUBMISSION_REJECTED reached directly", r["current_state"] == "TERMINAL_SUBMISSION_REJECTED")
        # A reconciliation attempt arriving against an already-rejected
        # (never-submitted) intent is unexpected evidence -- never
        # silently accepted as a fresh RECONCILED_FILLED, and never
        # silently dropped either. It escalates for a human to see.
        r2 = MOD.record_reconciliation_attempt(coid, "snap-x", "FILLED", base_dir=base)
        expect("pre-submission reject: unexpected reconciliation evidence escalates, doesn't silently fill", r2["current_state"] == "ESCALATED_HUMAN_REVIEW")


def test_legal_path_partial_then_completes() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        coid = "aura-test-29-partial-then-fill"
        new_intent(base, client_order_id=coid)
        MOD.record_claimed(coid, base_dir=base)
        MOD.record_submission_attempted(coid, base_dir=base)
        MOD.record_submission_outcome(coid, "SUBMISSION_ACKNOWLEDGED", base_dir=base)
        r = MOD.record_reconciliation_attempt(
            coid, "snap-1", "PARTIALLY_FILLED", order_terminal_on_exchange=False, base_dir=base
        )
        expect("partial-then-fill: non-terminal partial reached", r["current_state"] == "RECONCILED_PARTIALLY_FILLED")
        expect("partial-then-fill: partial_fill_terminal False", r["partial_fill_terminal"] is False)
        r = MOD.record_reconciliation_attempt(coid, "snap-2", "FILLED", base_dir=base)
        expect("partial-then-fill: reaches RECONCILED_FILLED afterward", r["current_state"] == "RECONCILED_FILLED")


def test_partial_fill_terminal_refinement() -> None:
    """The 2026-09-09 refinement: a partial fill on an order MEXC has
    ALREADY terminated (order_terminal_on_exchange=True) is itself
    terminal -- no further reconciliation attempt is accepted."""
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        coid = "aura-test-29-partial-terminal"
        new_intent(base, client_order_id=coid)
        MOD.record_claimed(coid, base_dir=base)
        MOD.record_submission_attempted(coid, base_dir=base)
        MOD.record_submission_outcome(coid, "SUBMISSION_ACKNOWLEDGED", base_dir=base)
        r = MOD.record_reconciliation_attempt(
            coid, "snap-1", "PARTIALLY_FILLED", order_terminal_on_exchange=True, base_dir=base
        )
        expect("partial-terminal: state RECONCILED_PARTIALLY_FILLED", r["current_state"] == "RECONCILED_PARTIALLY_FILLED")
        expect("partial-terminal: partial_fill_terminal True", r["partial_fill_terminal"] is True)
        # A further RECONCILIATION_ATTEMPT with DIFFERENT evidence does
        # not raise -- it escalates (see idempotency tests below). What
        # must be true here is specifically: the record no longer
        # accepts a plain RECORD_ATTEMPT path back into RECONCILING.
        r2 = MOD.record_reconciliation_attempt(coid, "snap-1", "PARTIALLY_FILLED", order_terminal_on_exchange=True, base_dir=base)
        expect("partial-terminal: identical retry is a no-op, stays terminal", r2["current_state"] == "RECONCILED_PARTIALLY_FILLED")
        expect("partial-terminal: no-op did not append a new event", len(r2["events"]) == len(r["events"]))


def test_partial_fill_open_stays_non_terminal_and_reopens() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        coid = "aura-test-29-partial-open"
        r = advance_to(base, "RECONCILED_PARTIALLY_FILLED", coid, partial_terminal=False)
        expect("partial-open: non-terminal", r["partial_fill_terminal"] is False)
        r2 = MOD.record_reconciliation_attempt(coid, "snap-2", "CANCELED", base_dir=base)
        expect("partial-open: can still transition onward to RECONCILED_CANCELED", r2["current_state"] == "RECONCILED_CANCELED")


# --------------------------------------------------------------------- #
# illegal transitions -- generated across every (state, event) pair
# from the matrix's Sec.1 table
# --------------------------------------------------------------------- #

ALL_FIXTURE_STATES = [
    ("NEW", None),
    ("CLAIMED", None),
    ("SUBMISSION_ATTEMPTED", None),
    ("AWAITING_RECONCILIATION", None),
    ("RECONCILING", None),
    ("RECONCILED_FILLED", None),
    ("RECONCILED_PARTIALLY_FILLED", False),
    ("RECONCILED_PARTIALLY_FILLED", True),
    ("RECONCILED_CANCELED", None),
    ("TERMINAL_SUBMISSION_REJECTED", None),
    ("ESCALATED_HUMAN_REVIEW", None),
]


def test_illegal_transitions_matrix() -> None:
    """Each mutator is probed against a FRESH fixture instance per
    check -- reusing one instance across probes within the same state
    would let an earlier, legitimately-legal probe (e.g. record_claimed()
    succeeding from NEW) silently advance the state before a later
    probe runs, corrupting that later probe's premise."""
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        for idx, (state, partial_terminal) in enumerate(ALL_FIXTURE_STATES):
            label = f"{state}" + (f"[terminal={partial_terminal}]" if state == "RECONCILED_PARTIALLY_FILLED" else "")
            current, current_partial_terminal = state, partial_terminal
            open_to_recon = MOD._is_open_to_reconciliation(current, current_partial_terminal)
            terminal_or_escalated = MOD._is_terminal_or_escalated(current, current_partial_terminal)

            # record_claimed(): always a safe no-op once past NEW -- never raises.
            coid = f"aura-test-29-illegal-{idx}-claim"
            advance_to(base, state, coid, partial_terminal=partial_terminal)
            before = MOD.get_intent(coid, base)
            MOD.record_claimed(coid, base_dir=base)
            after = MOD.get_intent(coid, base)
            if state != "NEW":
                expect(f"illegal[{label}]: record_claimed() no-ops (no new event)", len(after["events"]) == len(before["events"]))

            # record_submission_attempted(): legal only from CLAIMED;
            # raises from NEW; no-ops from anything past CLAIMED.
            coid = f"aura-test-29-illegal-{idx}-subatt"
            advance_to(base, state, coid, partial_terminal=partial_terminal)
            if state == "NEW":
                expect_raises(f"illegal[{label}]: submission_attempted raises from NEW", MOD.record_submission_attempted, coid, base_dir=base)
            elif state != "CLAIMED":
                before = MOD.get_intent(coid, base)
                MOD.record_submission_attempted(coid, base_dir=base)
                after = MOD.get_intent(coid, base)
                expect(f"illegal[{label}]: submission_attempted no-ops (not CLAIMED)", len(after["events"]) == len(before["events"]))

            # record_reconciliation_attempt() outside the open window:
            # never raises -- it either applies normally (skipped here,
            # covered by the legal-path tests) or escalates/no-ops,
            # covered by the idempotency test group below. Here we only
            # assert it does NOT silently produce a RECONCILED_* verdict
            # out of thin air when the record is not open to it.
            if not open_to_recon:
                coid = f"aura-test-29-illegal-{idx}-recon"
                advance_to(base, state, coid, partial_terminal=partial_terminal)
                if state in ("NEW", "CLAIMED", "SUBMISSION_ATTEMPTED"):
                    # Genuinely premature -- raises, never silently
                    # accepted and never escalated (nothing to compare
                    # against yet).
                    expect_raises(
                        f"illegal[{label}]: reconciliation_attempt raises (too early, no submission outcome yet)",
                        MOD.record_reconciliation_attempt, coid, "unexpected-snap", "FILLED", base_dir=base,
                    )
                else:
                    # Already resolved -- never silently produces a
                    # fresh RECONCILED_FILLED verdict out of thin air;
                    # either escalates or (if this exact evidence is
                    # already on record) no-ops.
                    before = MOD.get_intent(coid, base)
                    after = MOD.record_reconciliation_attempt(coid, "unexpected-snap", "FILLED", base_dir=base)
                    expect(
                        f"illegal[{label}]: reconciliation_attempt outside the open window never produces a fresh "
                        "RECONCILED_FILLED silently",
                        not (after["current_state"] == "RECONCILED_FILLED" and before["current_state"] != "RECONCILED_FILLED"),
                    )

            # escalate_to_human(): REFUSED (raises) from NEW and from
            # any terminal/escalated state; ALLOWED otherwise.
            if state == "NEW" or terminal_or_escalated:
                coid = f"aura-test-29-illegal-{idx}-escalate"
                advance_to(base, state, coid, partial_terminal=partial_terminal)
                expect_raises(f"illegal[{label}]: escalate_to_human raises", MOD.escalate_to_human, coid, "probe", base_dir=base)


# --------------------------------------------------------------------- #
# the DUPLICATE_CLAIM_REJECTED anomaly path -- legal from any state,
# never a second record, always routes to ESCALATED_HUMAN_REVIEW
# --------------------------------------------------------------------- #

def test_duplicate_claim_anomaly_from_every_state() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        for idx, (state, partial_terminal) in enumerate(ALL_FIXTURE_STATES):
            coid = f"aura-test-29-dup-{idx}"
            advance_to(base, state, coid, partial_terminal=partial_terminal)
            r = MOD.record_submission_outcome(coid, "DUPLICATE_CLAIM_REJECTED", base_dir=base)
            expect(f"dup-claim[{state}]: routes to ESCALATED_HUMAN_REVIEW", r["current_state"] == "ESCALATED_HUMAN_REVIEW")

            # Confirm exactly one record exists for this client_order_id --
            # never a second file.
            path = MOD.record_path(coid, base)
            sibling_files = [p for p in base.glob("*.json") if p.stem == coid]
            expect(f"dup-claim[{state}]: exactly one record file", len(sibling_files) == 1)


def test_duplicate_claim_idempotent_on_already_escalated() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        coid = "aura-test-29-dup-idem"
        advance_to(base, "RECONCILING", coid)
        r1 = MOD.record_submission_outcome(coid, "DUPLICATE_CLAIM_REJECTED", base_dir=base)
        r2 = MOD.record_submission_outcome(coid, "DUPLICATE_CLAIM_REJECTED", base_dir=base)
        expect("dup-claim idempotent: second identical call is a no-op", len(r2["events"]) == len(r1["events"]))
        expect("dup-claim idempotent: still ESCALATED_HUMAN_REVIEW", r2["current_state"] == "ESCALATED_HUMAN_REVIEW")


# --------------------------------------------------------------------- #
# crash-retry idempotency -- the rule Martin explicitly asked to be
# proven by tests, not just documented
# --------------------------------------------------------------------- #

def test_idempotent_retry_of_submission_outcome() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        coid = "aura-test-29-idem-outcome"
        new_intent(base, client_order_id=coid)
        MOD.record_claimed(coid, base_dir=base)
        MOD.record_submission_attempted(coid, base_dir=base)
        r1 = MOD.record_submission_outcome(coid, "SUBMISSION_ACKNOWLEDGED", mexc_order_id="m1", raw_response_ref="ref1", base_dir=base)
        # Simulated crash-retry: identical call repeated.
        r2 = MOD.record_submission_outcome(coid, "SUBMISSION_ACKNOWLEDGED", mexc_order_id="m1", raw_response_ref="ref1", base_dir=base)
        expect("idem outcome: identical retry is a no-op", len(r2["events"]) == len(r1["events"]))
        expect("idem outcome: state unaffected", r2["current_state"] == "AWAITING_RECONCILIATION")
        expect("idem outcome: no ESCALATION event introduced", all(e["event"] != "ESCALATION" for e in r2["events"]))


def test_conflicting_retry_of_submission_outcome_escalates() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        coid = "aura-test-29-conflict-outcome"
        new_intent(base, client_order_id=coid)
        MOD.record_claimed(coid, base_dir=base)
        MOD.record_submission_attempted(coid, base_dir=base)
        MOD.record_submission_outcome(coid, "SUBMISSION_ACKNOWLEDGED", mexc_order_id="m1", base_dir=base)
        # A DIFFERENT outcome arrives for the same logical point --
        # conflicting evidence, must never be silently accepted or
        # silently dropped.
        r = MOD.record_submission_outcome(coid, "EXECUTION_UNCERTAIN", mexc_order_id="m2", base_dir=base)
        expect("conflict outcome: escalates rather than silently overwriting", r["current_state"] == "ESCALATED_HUMAN_REVIEW")
        expect("conflict outcome: an ESCALATION event is present", any(e["event"] == "ESCALATION" for e in r["events"]))
        expect(
            "conflict outcome: original SUBMISSION_ACKNOWLEDGED event untouched",
            any(e["event"] == "SUBMISSION_ACKNOWLEDGED" and e["fields"]["mexc_order_id"] == "m1" for e in r["events"]),
        )


def test_idempotent_retry_of_reconciliation_attempt() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        coid = "aura-test-29-idem-recon"
        new_intent(base, client_order_id=coid)
        MOD.record_claimed(coid, base_dir=base)
        MOD.record_submission_attempted(coid, base_dir=base)
        MOD.record_submission_outcome(coid, "SUBMISSION_ACKNOWLEDGED", base_dir=base)
        r1 = MOD.record_reconciliation_attempt(coid, "snap-final", "FILLED", base_dir=base)
        r2 = MOD.record_reconciliation_attempt(coid, "snap-final", "FILLED", base_dir=base)
        expect("idem recon: identical retry after terminal is a no-op", len(r2["events"]) == len(r1["events"]))
        expect("idem recon: state stays RECONCILED_FILLED", r2["current_state"] == "RECONCILED_FILLED")


def test_conflicting_retry_of_reconciliation_attempt_escalates() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        coid = "aura-test-29-conflict-recon"
        new_intent(base, client_order_id=coid)
        MOD.record_claimed(coid, base_dir=base)
        MOD.record_submission_attempted(coid, base_dir=base)
        MOD.record_submission_outcome(coid, "SUBMISSION_ACKNOWLEDGED", base_dir=base)
        MOD.record_reconciliation_attempt(coid, "snap-final", "FILLED", base_dir=base)
        # Conflicting evidence about the SAME already-terminal intent.
        r = MOD.record_reconciliation_attempt(coid, "snap-different", "CANCELED", base_dir=base)
        expect("conflict recon: escalates rather than silently overwriting FILLED", r["current_state"] == "ESCALATED_HUMAN_REVIEW")
        expect(
            "conflict recon: original FILLED reconciliation event untouched",
            any(e["event"] == "RECONCILIATION_ATTEMPT" and e["fields"]["order_status"] == "FILLED" for e in r["events"]),
        )


def test_idempotent_retry_of_structural_mutators() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        coid = "aura-test-29-idem-structural"
        new_intent(base, client_order_id=coid)
        r1 = MOD.record_claimed(coid, base_dir=base)
        r2 = MOD.record_claimed(coid, base_dir=base)
        expect("idem structural: repeated record_claimed() is a no-op", len(r1["events"]) == len(r2["events"]))
        r3a = MOD.record_submission_attempted(coid, base_dir=base)
        r3b = MOD.record_submission_attempted(coid, base_dir=base)
        expect("idem structural: repeated record_submission_attempted() is a no-op", len(r3a["events"]) == len(r3b["events"]))


# --------------------------------------------------------------------- #
# hash / tamper detection
# --------------------------------------------------------------------- #

def test_hash_tamper_detection() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        coid = "aura-test-29-tamper"
        new_intent(base, client_order_id=coid)
        path = MOD.record_path(coid, base)

        record = json.loads(path.read_text(encoding="utf-8"))
        ok, errors = MOD.verify_intent(record)
        expect(f"tamper: untouched record verifies clean ({errors})", ok)

        tampered = json.loads(path.read_text(encoding="utf-8"))
        tampered["events"][0]["fields"]["quantity"] = 999.0
        ok2, errors2 = MOD.verify_intent(tampered)
        expect("tamper: hash mismatch caught after field edit", not ok2 and "RECORD_HASH_MISMATCH" in errors2)

        # Structurally-invalid-but-hash-consistent: recompute the hash
        # over a tampered, illegal event sequence -- hash alone must not
        # be trusted as a proxy for sequence legality.
        illegal = json.loads(path.read_text(encoding="utf-8"))
        illegal["events"].append({"event": "SUBMISSION_ATTEMPTED", "at": "2026-01-01T00:00:00+00:00", "fields": {}})
        illegal["record_hash"] = MOD.canonical_record_hash(illegal)
        illegal["current_state"] = "SUBMISSION_ATTEMPTED"  # attacker also "fixes up" the summary field
        ok3, errors3 = MOD.verify_intent(illegal)
        expect(
            "tamper: hash-consistent but illegal sequence still caught (CLAIMED skipped)",
            not ok3 and any(e.startswith("ILLEGAL_EVENT_SEQUENCE") for e in errors3),
        )


def test_state_drift_detection() -> None:
    """A record whose stored current_state doesn't match what the
    events themselves derive to -- even if record_hash was
    (incorrectly) left alone -- must be caught."""
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        coid = "aura-test-29-drift"
        new_intent(base, client_order_id=coid)
        path = MOD.record_path(coid, base)
        record = json.loads(path.read_text(encoding="utf-8"))
        record["current_state"] = "AWAITING_RECONCILIATION"  # lie, hash left stale/unchanged
        ok, errors = MOD.verify_intent(record)
        expect("drift: caught even though only the summary field was touched", not ok)


# --------------------------------------------------------------------- #
# crash-mid-write recovery
# --------------------------------------------------------------------- #

def test_crash_before_rename_leaves_prior_record_intact() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        coid = "aura-test-29-crash"
        new_intent(base, client_order_id=coid)
        MOD.record_claimed(coid, base_dir=base)
        path = MOD.record_path(coid, base)
        before = json.loads(path.read_text(encoding="utf-8"))

        # Simulate a crash: a .tmp file is written (as _write_record_atomic
        # would produce mid-way) but os.replace() never runs.
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text("{not even valid json", encoding="utf-8")

        after_crash = json.loads(path.read_text(encoding="utf-8"))
        expect("crash: prior record on disk is untouched by the orphaned .tmp file", after_crash == before)
        ok, errors = MOD.verify_intent(after_crash)
        expect(f"crash: prior record still verifies clean ({errors})", ok)

        tmp_path.unlink()
        # A subsequent legitimate mutation still works normally.
        r = MOD.record_submission_attempted(coid, base_dir=base)
        expect("crash: normal operation resumes after clearing the orphaned tmp file", r["current_state"] == "SUBMISSION_ATTEMPTED")


def test_crash_after_rename_is_equivalent_to_completed_write() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        coid = "aura-test-29-crash-after"
        new_intent(base, client_order_id=coid)
        r = MOD.record_claimed(coid, base_dir=base)
        # os.replace() already ran inside record_claimed(); reading again
        # from a "fresh process" perspective must show the completed state.
        reread = MOD.get_intent(coid, base)
        expect("crash-after-rename: record reflects the completed write", reread["current_state"] == r["current_state"])


# --------------------------------------------------------------------- #
# restart recovery / listing
# --------------------------------------------------------------------- #

def test_list_unresolved_intents() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        for idx, (state, partial_terminal) in enumerate(ALL_FIXTURE_STATES):
            coid = f"aura-test-29-list-{idx}"
            advance_to(base, state, coid, partial_terminal=partial_terminal)

        unresolved = MOD.list_unresolved_intents(base)
        unresolved_ids = {r["client_order_id"] for r in unresolved}

        expected_unresolved_states = {
            "NEW", "CLAIMED", "SUBMISSION_ATTEMPTED", "AWAITING_RECONCILIATION",
            "RECONCILING", "ESCALATED_HUMAN_REVIEW",
        }
        for idx, (state, partial_terminal) in enumerate(ALL_FIXTURE_STATES):
            coid = f"aura-test-29-list-{idx}"
            should_be_unresolved = state in expected_unresolved_states or (
                state == "RECONCILED_PARTIALLY_FILLED" and partial_terminal is False
            )
            expect(
                f"list-unresolved: {coid} ({state}, terminal={partial_terminal}) "
                f"{'included' if should_be_unresolved else 'excluded'} correctly",
                (coid in unresolved_ids) == should_be_unresolved,
            )


def test_list_unresolved_empty_directory() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "does-not-exist-yet"
        expect("list-unresolved: empty/missing directory returns []", MOD.list_unresolved_intents(base) == [])


def test_list_unresolved_isolates_one_corrupt_file() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        advance_to(base, "RECONCILING", "aura-test-29-good")
        corrupt_path = base / "aura-test-29-corrupt.json"
        corrupt_path.write_text("{not valid json", encoding="utf-8")

        unresolved = MOD.list_unresolved_intents(base)
        ids = {r["client_order_id"] for r in unresolved}
        expect("list-unresolved: good record still surfaced despite a corrupt sibling", "aura-test-29-good" in ids)
        corrupt_entries = [r for r in unresolved if r["client_order_id"] == "aura-test-29-corrupt"]
        expect("list-unresolved: corrupt file surfaced as BLOCKED, not silently dropped", len(corrupt_entries) == 1)
        expect("list-unresolved: corrupt entry flagged", corrupt_entries[0]["current_state"] == "BLOCKED_UNREADABLE")


# --------------------------------------------------------------------- #
# get_intent
# --------------------------------------------------------------------- #

def test_get_intent_missing_returns_none() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        expect("get_intent: missing record returns None", MOD.get_intent("does-not-exist", base) is None)


def test_get_intent_roundtrip_full_history() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        coid = "aura-test-29-roundtrip"
        new_intent(base, client_order_id=coid)
        MOD.record_claimed(coid, base_dir=base)
        MOD.record_submission_attempted(coid, base_dir=base)
        MOD.record_submission_outcome(coid, "SUBMISSION_ACKNOWLEDGED", base_dir=base)
        written = MOD.record_reconciliation_attempt(coid, "snap-1", "FILLED", base_dir=base)
        read_back = MOD.get_intent(coid, base)
        expect("roundtrip: full event history matches", read_back["events"] == written["events"])
        expect("roundtrip: current_state matches", read_back["current_state"] == written["current_state"])


def main() -> int:
    test_create_intent_basic()
    test_create_intent_validation()
    test_create_intent_atomic_refusal()
    test_direction_all_four_values_and_immutable()
    test_legal_path_clean_fill()
    test_legal_path_execution_uncertain_identical_to_acknowledged()
    test_legal_path_pre_submission_rejected_skips_reconciliation()
    test_legal_path_partial_then_completes()
    test_partial_fill_terminal_refinement()
    test_partial_fill_open_stays_non_terminal_and_reopens()
    test_illegal_transitions_matrix()
    test_duplicate_claim_anomaly_from_every_state()
    test_duplicate_claim_idempotent_on_already_escalated()
    test_idempotent_retry_of_submission_outcome()
    test_conflicting_retry_of_submission_outcome_escalates()
    test_idempotent_retry_of_reconciliation_attempt()
    test_conflicting_retry_of_reconciliation_attempt_escalates()
    test_idempotent_retry_of_structural_mutators()
    test_hash_tamper_detection()
    test_state_drift_detection()
    test_crash_before_rename_leaves_prior_record_intact()
    test_crash_after_rename_is_equivalent_to_completed_write()
    test_list_unresolved_intents()
    test_list_unresolved_empty_directory()
    test_list_unresolved_isolates_one_corrupt_file()
    test_get_intent_missing_returns_none()
    test_get_intent_roundtrip_full_history()
    print("AURA v0.5.3.29 CONTRACT: ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
