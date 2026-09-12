"""
Contract tests for AURA v0.5.3.39 Strategy/Hypothesis Registry + Research
Promotion Gate.

Each test uses a fresh tmp_path-backed registry directory (real file I/O,
not mocked) so passing tests are meaningful evidence about the real
read-verify-append-atomic-rename storage path, not just in-memory logic.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(module_filename: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / module_filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


registry = _load("aura_v05339_strategy_registry.py", "aura_v05339_registry")

ALL_CATEGORIES = registry.EVIDENCE_CATEGORIES


def expect(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"PASS: {name}")


def _register(reg_dir, *, label="TEST_LONG_LABEL", direction="LONG", name="test-strategy", version=1):
    spec = registry.freeze_spec(
        strategy_name=name,
        version=version,
        regime_state_label=label,
        direction=direction,
        source_kind="DETERMINISTIC_RESEARCH",
        proposed_by="test-suite",
        description="unit test fixture",
    )
    return registry.register_strategy(spec, reg_dir)


def _pass_all_evidence(reg_dir, strategy_id, *, except_category=None, expires_at=None):
    for category in ALL_CATEGORIES:
        if category == except_category:
            continue
        registry.record_evidence(
            strategy_id=strategy_id,
            category=category,
            status="PASS",
            evidence_ref=f"test-artifact://{category.lower()}",
            recorded_by="test-suite",
            expires_at=expires_at,
            registry_dir=reg_dir,
        )


# --------------------------------------------------------------------- #
# identity / registration
# --------------------------------------------------------------------- #


def test_register_creates_candidate_with_content_fingerprint_id(tmp_path):
    record = _register(tmp_path)
    expect("id has STRAT- prefix", record["strategy_id"].startswith("STRAT-"))
    state, reasons = registry.derive_state(record["events"])
    expect("initial state is CANDIDATE", state == "CANDIDATE")
    expect("no rejection reasons yet", reasons == [])
    # Identity is a pure function of content -- recomputing from the same
    # spec_body must reproduce the exact same id (this is the "immutable,
    # fingerprintable specification" requirement, proven, not asserted).
    recomputed = registry.compute_strategy_id(record["spec_body"])
    expect("id is a pure content fingerprint", recomputed == record["strategy_id"])


def test_register_is_idempotent_on_identical_content():
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        reg_dir = Path(d)
        first = _register(reg_dir)
        second = _register(reg_dir)
        expect("re-registering identical content returns same record", first == second)
        files = list(reg_dir.glob("STRAT-*.json"))
        expect("only one record file exists", len(files) == 1)


def test_different_spec_content_produces_different_identity(tmp_path):
    a = _register(tmp_path, label="LABEL_A")
    b = _register(tmp_path, label="LABEL_B")
    expect("different regime_state_label -> different strategy_id", a["strategy_id"] != b["strategy_id"])


# --------------------------------------------------------------------- #
# evidence / deterministic promotion
# --------------------------------------------------------------------- #


def test_all_nine_categories_pass_promotes(tmp_path):
    record = _register(tmp_path)
    sid = record["strategy_id"]
    _pass_all_evidence(tmp_path, sid)
    result = registry.apply_evaluation(sid, tmp_path)
    state, reasons = registry.derive_state(result["events"])
    expect("promoted once all nine categories pass", state == "PROMOTED")
    expect("no rejection reasons when promoted", reasons == [])


def test_missing_one_category_rejects_with_specific_reason(tmp_path):
    record = _register(tmp_path)
    sid = record["strategy_id"]
    _pass_all_evidence(tmp_path, sid, except_category="REGIME_STABILITY")
    result = registry.apply_evaluation(sid, tmp_path)
    state, reasons = registry.derive_state(result["events"])
    expect("rejected when one category is missing", state == "REJECTED")
    expect(
        "reason names the missing category",
        "MISSING_EVIDENCE:REGIME_STABILITY" in reasons,
    )


def test_failed_evidence_status_rejects(tmp_path):
    record = _register(tmp_path)
    sid = record["strategy_id"]
    for category in ALL_CATEGORIES:
        status = "FAIL" if category == "COST_SLIPPAGE_SENSITIVITY" else "PASS"
        registry.record_evidence(
            strategy_id=sid,
            category=category,
            status=status,
            evidence_ref="ref",
            recorded_by="test-suite",
            registry_dir=tmp_path,
        )
    result = registry.apply_evaluation(sid, tmp_path)
    state, reasons = registry.derive_state(result["events"])
    expect("FAIL status rejects", state == "REJECTED")
    expect("reason names the failed category", "FAILED_EVIDENCE:COST_SLIPPAGE_SENSITIVITY" in reasons)


def test_expired_evidence_rejects_stale_promotion(tmp_path):
    record = _register(tmp_path)
    sid = record["strategy_id"]
    _pass_all_evidence(tmp_path, sid)
    # Overwrite one category with evidence that already expired.
    registry.record_evidence(
        strategy_id=sid,
        category="PROSPECTIVE_EVIDENCE",
        status="PASS",
        evidence_ref="ref",
        recorded_by="test-suite",
        expires_at="2020-01-01T00:00:00Z",
        registry_dir=tmp_path,
    )
    result = registry.apply_evaluation(sid, tmp_path)
    state, reasons = registry.derive_state(result["events"])
    expect("expired evidence rejects", state == "REJECTED")
    expect("reason names the expired category", "EXPIRED_EVIDENCE:PROSPECTIVE_EVIDENCE" in reasons)


def test_latest_evidence_submission_for_a_category_supersedes_earlier_one(tmp_path):
    record = _register(tmp_path)
    sid = record["strategy_id"]
    registry.record_evidence(
        strategy_id=sid, category="WALK_FORWARD", status="FAIL",
        evidence_ref="ref-v1", recorded_by="test-suite", registry_dir=tmp_path,
    )
    _pass_all_evidence(tmp_path, sid, except_category="WALK_FORWARD")
    result = registry.apply_evaluation(sid, tmp_path)
    state, reasons = registry.derive_state(result["events"])
    expect("still rejected on the FAIL", state == "REJECTED")
    # Corrected resubmission for the same category.
    registry.record_evidence(
        strategy_id=sid, category="WALK_FORWARD", status="PASS",
        evidence_ref="ref-v2-corrected", recorded_by="test-suite", registry_dir=tmp_path,
    )
    result = registry.apply_evaluation(sid, tmp_path)
    state, reasons = registry.derive_state(result["events"])
    expect("resubmission with PASS promotes on re-evaluation", state == "PROMOTED")
    # History of the original FAIL must still be visible.
    fail_events = [
        e for e in result["events"]
        if e["event"] == "EVIDENCE_RECORDED" and e["fields"]["evidence_ref"] == "ref-v1"
    ]
    expect("original FAIL evidence event preserved in history", len(fail_events) == 1)


# --------------------------------------------------------------------- #
# tamper / integrity
# --------------------------------------------------------------------- #


def test_tampered_spec_body_is_rejected_on_load(tmp_path):
    record = _register(tmp_path)
    sid = record["strategy_id"]
    path = tmp_path / f"{sid}.json"

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    on_disk["spec_body"]["direction"] = "SHORT"  # tamper: id no longer matches content
    path.write_text(json.dumps(on_disk), encoding="utf-8")

    raised = False
    try:
        registry.load_record(sid, tmp_path)
    except registry.RegistryError as exc:
        raised = True
        expect("error names the integrity mismatch", "SPEC_INTEGRITY_MISMATCH" in str(exc))
    expect("tampered spec_body is rejected, not silently loaded", raised)


def test_tampered_record_hash_is_rejected_on_load(tmp_path):
    record = _register(tmp_path)
    sid = record["strategy_id"]
    path = tmp_path / f"{sid}.json"

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    on_disk["events"].append({"event": "EVIDENCE_RECORDED", "at": "x", "fields": {
        "category": "WALK_FORWARD", "status": "PASS", "evidence_ref": "r",
        "recorded_by": "attacker", "notes": "", "expires_at": None,
    }})
    # Note: record_hash on disk is deliberately NOT recomputed after this edit.
    path.write_text(json.dumps(on_disk), encoding="utf-8")

    raised = False
    try:
        registry.load_record(sid, tmp_path)
    except registry.RegistryError as exc:
        raised = True
        expect("error names the hash mismatch", "RECORD_HASH_MISMATCH" in str(exc))
    expect("record with stale hash after an unaudited edit is rejected", raised)


def test_re_registering_with_tampered_existing_record_fails_closed(tmp_path):
    record = _register(tmp_path)
    sid = record["strategy_id"]
    path = tmp_path / f"{sid}.json"
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    on_disk["spec_body"]["proposed_by"] = "someone-else"  # tamper, id now stale
    path.write_text(json.dumps(on_disk), encoding="utf-8")

    raised = False
    try:
        _register(tmp_path)  # same original content -> same strategy_id
    except registry.RegistryError as exc:
        raised = True
        expect("duplicate registration over a corrupted record fails closed", "CORRUPTED" in str(exc))
    expect("register_strategy never silently overwrites a corrupted existing record", raised)


# --------------------------------------------------------------------- #
# revocation
# --------------------------------------------------------------------- #


def test_revoke_only_legal_from_promoted(tmp_path):
    record = _register(tmp_path)
    sid = record["strategy_id"]
    raised = False
    try:
        registry.revoke_strategy(sid, reason="test", revoked_by="ops", registry_dir=tmp_path)
    except registry.RegistryError as exc:
        raised = True
        expect("error names the illegal state", "CANNOT_REVOKE_FROM_STATE:CANDIDATE" in str(exc))
    expect("cannot revoke a CANDIDATE", raised)


def test_revoke_is_terminal_and_preserves_history(tmp_path):
    record = _register(tmp_path)
    sid = record["strategy_id"]
    _pass_all_evidence(tmp_path, sid)
    registry.apply_evaluation(sid, tmp_path)

    revoked = registry.revoke_strategy(sid, reason="research invalidated", revoked_by="martin", registry_dir=tmp_path)
    state, _ = registry.derive_state(revoked["events"])
    expect("state is REVOKED", state == "REVOKED")

    promoted_events = [e for e in revoked["events"] if e["event"] == "PROMOTION_EVALUATED" and e["fields"]["result"] == "PROMOTED"]
    expect("history still shows it was once PROMOTED", len(promoted_events) == 1)

    raised = False
    try:
        registry.apply_evaluation(sid, tmp_path)
    except registry.RegistryError:
        raised = True
    expect("cannot re-evaluate a REVOKED strategy", raised)

    raised = False
    try:
        registry.record_evidence(
            strategy_id=sid, category="WALK_FORWARD", status="PASS",
            evidence_ref="r", recorded_by="x", registry_dir=tmp_path,
        )
    except registry.RegistryError:
        raised = True
    expect("cannot record new evidence on a REVOKED strategy", raised)


def test_revoked_strategy_excluded_from_promotion_snapshot(tmp_path):
    record = _register(tmp_path, label="WILL_BE_REVOKED")
    sid = record["strategy_id"]
    _pass_all_evidence(tmp_path, sid)
    registry.apply_evaluation(sid, tmp_path)
    snapshot_before = registry.build_promotion_snapshot(tmp_path)
    expect("promoted label present before revocation", "WILL_BE_REVOKED" in snapshot_before["validated_long_entry_regime_labels"])

    registry.revoke_strategy(sid, reason="x", revoked_by="ops", registry_dir=tmp_path)
    snapshot_after = registry.build_promotion_snapshot(tmp_path)
    expect("revoked label absent after revocation", "WILL_BE_REVOKED" not in snapshot_after["validated_long_entry_regime_labels"])


# --------------------------------------------------------------------- #
# promotion snapshot / conflicts / corruption handling
# --------------------------------------------------------------------- #


def test_snapshot_maps_promoted_long_and_short_labels(tmp_path):
    long_rec = _register(tmp_path, label="LONG_LABEL", direction="LONG", name="s-long")
    short_rec = _register(tmp_path, label="SHORT_LABEL", direction="SHORT", name="s-short")
    for rec in (long_rec, short_rec):
        _pass_all_evidence(tmp_path, rec["strategy_id"])
        registry.apply_evaluation(rec["strategy_id"], tmp_path)

    snapshot = registry.build_promotion_snapshot(tmp_path)
    expect("LONG_LABEL in long allowlist", "LONG_LABEL" in snapshot["validated_long_entry_regime_labels"])
    expect("SHORT_LABEL in short allowlist", "SHORT_LABEL" in snapshot["validated_short_entry_regime_labels"])
    expect("no conflicts", snapshot["conflicts"] == [])
    expect("no corrupted records", snapshot["corrupted_records"] == [])

    expect("side_for_label resolves BUY", registry.side_for_label(snapshot, "LONG_LABEL") == "BUY")
    expect("side_for_label resolves SELL", registry.side_for_label(snapshot, "SHORT_LABEL") == "SELL")
    expect("side_for_label is None for an unknown label", registry.side_for_label(snapshot, "SOMETHING_ELSE") is None)


def test_candidate_and_rejected_strategies_never_appear_in_snapshot(tmp_path):
    _register(tmp_path, label="STILL_CANDIDATE", name="s1")
    rejected = _register(tmp_path, label="WAS_REJECTED", name="s2")
    _pass_all_evidence(tmp_path, rejected["strategy_id"], except_category="OOS_VALIDATION")
    registry.apply_evaluation(rejected["strategy_id"], tmp_path)

    snapshot = registry.build_promotion_snapshot(tmp_path)
    all_labels = set(snapshot["validated_long_entry_regime_labels"]) | set(snapshot["validated_short_entry_regime_labels"])
    expect("CANDIDATE-only strategy not in snapshot", "STILL_CANDIDATE" not in all_labels)
    expect("REJECTED strategy not in snapshot", "WAS_REJECTED" not in all_labels)


def test_conflicting_promoted_directions_for_same_label_excluded_not_guessed(tmp_path):
    long_rec = _register(tmp_path, label="CONTESTED_LABEL", direction="LONG", name="s-long")
    short_rec = _register(tmp_path, label="CONTESTED_LABEL", direction="SHORT", name="s-short")
    for rec in (long_rec, short_rec):
        _pass_all_evidence(tmp_path, rec["strategy_id"])
        registry.apply_evaluation(rec["strategy_id"], tmp_path)

    snapshot = registry.build_promotion_snapshot(tmp_path)
    all_labels = set(snapshot["validated_long_entry_regime_labels"]) | set(snapshot["validated_short_entry_regime_labels"])
    expect("contested label excluded from both allowlists", "CONTESTED_LABEL" not in all_labels)
    expect("contested label reported as a conflict", "CONTESTED_LABEL" in snapshot["conflicts"])


def test_corrupted_record_excluded_from_snapshot_and_reported(tmp_path):
    record = _register(tmp_path, label="WOULD_BE_PROMOTED")
    sid = record["strategy_id"]
    _pass_all_evidence(tmp_path, sid)
    registry.apply_evaluation(sid, tmp_path)

    path = tmp_path / f"{sid}.json"
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    on_disk["spec_body"]["regime_state_label"] = "TAMPERED_LABEL"  # id no longer matches
    path.write_text(json.dumps(on_disk), encoding="utf-8")

    snapshot = registry.build_promotion_snapshot(tmp_path)
    all_labels = set(snapshot["validated_long_entry_regime_labels"]) | set(snapshot["validated_short_entry_regime_labels"])
    expect("tampered record's label never trusted", "TAMPERED_LABEL" not in all_labels)
    expect("original label also absent (record excluded wholesale)", "WOULD_BE_PROMOTED" not in all_labels)
    expect("corruption reported", any(sid in c for c in snapshot["corrupted_records"]))


def main() -> int:
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    import inspect
    import tempfile

    passed = 0
    for test in tests:
        params = inspect.signature(test).parameters
        if "tmp_path" in params:
            with tempfile.TemporaryDirectory() as d:
                test(Path(d))
        else:
            test()
        passed += 1
    print(f"AURA v0.5.3.39 CONTRACT: {passed}/{len(tests)} PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
