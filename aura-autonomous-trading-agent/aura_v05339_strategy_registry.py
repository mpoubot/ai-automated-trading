#!/usr/bin/env python3
"""
AURA v0.5.3.39 — Strategy/Hypothesis Registry + Research Promotion Gate

WHY THIS MODULE EXISTS (audited, not assumed)
-----------------------------------------------
Before writing any code here, v0.5.3.23 (aura_v05323_execution_specification_
builder.py) and its full call graph were re-read directly from source this
session, not from an earlier report:

  - v0.5.3.23's VALIDATED_LONG_ENTRY_REGIME_LABELS / _SHORT_ENTRY_REGIME_
    LABELS are empty frozensets. That emptiness is the ACTUAL, LIVE promotion
    gate today: v0.5.3.23 is invoked directly, every 30 minutes, by
    aura_v05321_alpaca_paper_runtime.py, which is scheduled on `main` via
    .github/workflows/paper-cycle-dashboard.yml. The FROZEN_CANDIDATE fix
    (2026-09-11) made the upstream .12/.13 signal path genuinely live; only
    these two empty allowlists stand between that live signal and an
    EXECUTION_SPEC_READY output.
  - v0.5.3.33 (Canonical Execution Specification) and the .34-.38 spine are a
    SEPARATE, newer, venue-agnostic track for MEXC futures and Alpaca
    equities/ETFs. v0.5.3.38's own docstring states plainly: ".23 ... is NOT
    a canonical-spec producer and is NOT used anywhere in this module -- .33
    is the real canonical producer." Confirmed independently here: no
    GitHub Actions workflow references .33-.38 at all; only .21+.23 are
    scheduled. So .33-.38 is a real, tested, but currently DORMANT track,
    while .12-.26 (via .23) is the one actually running live.
  - Conclusion, and the reason this module targets .23 specifically: the
    verified gap is in the .12-.26 chain, at .23's allowlists. This module
    closes THAT gap. It does not touch, wrap, or duplicate .33-.38, and does
    not modify them (per explicit instruction: no changes to .31-.38 unless
    a genuine dependency is found and reported first -- none was found).

WHAT THIS MODULE DOES
-----------------------
Formal boundary: Research/Hypothesis -> Registry -> Validation Evidence ->
Promotion Gate -> Promoted Strategy -> (consumed by) v0.5.3.23.

  1. Strategy/hypothesis identity: a human-assigned `strategy_name` is
     stable across revisions; each revision's frozen spec content produces
     its own immutable `strategy_id` (a content fingerprint), so identity
     never depends on a transient execution/run id.
  2. Immutable, fingerprintable specification: `freeze_spec()` produces a
     spec whose `strategy_id` is sha256 of its own canonical content. Any
     change to the spec produces a different id -- there is no in-place
     mutation of a promoted spec's content, ever.
  3. Evidence requirements: nine named categories, taken directly from the
     v0.5.5 roadmap's Research Promotion Gate description (OOS, walk-
     forward, permutation/matched controls, cost/slippage sensitivity,
     parameter plateau, unseen-symbol validation, regime stability,
     prospective evidence, paper execution evidence). This module does NOT
     compute any of these itself -- it has no backtesting, no statistics,
     and no AI/ML of any kind. It only records, timestamps, and structurally
     verifies evidence an external (human or deterministic) validation
     process supplies, exactly mirroring .23's own stance on its direction
     allowlists: "never inferred by this module." Promotion requires all
     nine categories present, PASS, and unexpired -- nothing else.
  4. Promotion states -- four, not the ten sketched abstractly in the
     roadmap diagram. Justification, argued from what .23 actually needs to
     ask ("is this regime label currently trustworthy, yes or no, and can
     that answer be taken back without losing history"): CANDIDATE (spec
     frozen and registered; evidence collection in progress -- there is no
     meaningful registry state "before" a spec is frozen, since freezing IS
     what creates a registry entry), REJECTED (gate evaluated, at least one
     category missing/failed/expired -- the specific category-level reasons
     live in `rejection_reasons`, so a separate INSUFFICIENT_EVIDENCE state
     would only duplicate that information as a state label), PROMOTED (gate
     evaluated, all nine categories present/PASS/unexpired), REVOKED
     (terminal; only reachable from PROMOTED, only via an explicit
     `revoke_strategy()` call with a reason and actor). CANDIDATE and
     REJECTED are not terminal -- `evaluate_promotion()` is a pure,
     idempotent function of a record's current evidence, so a CANDIDATE
     that gains evidence, or a REJECTED strategy that receives corrected
     evidence, can reach PROMOTED on a later evaluation. REVOKED is
     terminal by design: a spec's content can never change (that would be a
     new strategy_id), so once a specific frozen spec's promotion has been
     explicitly withdrawn, the only way back is a new spec version.
  5. Deterministic promotion criteria: `evaluate_promotion()` is a pure
     boolean-AND over structurally-checked evidence records. No LLM, no
     heuristic, no judgment call lives in this module.
  6. Provenance: every record carries strategy_name, version, strategy_id,
     source_kind, proposed_by, created_at, and an append-only `events` list
     (registration, each evidence recording, each promotion evaluation,
     revocation) -- mirroring v0.5.3.29's own "read-verify-append-atomic-
     rename" discipline and self-hash convention, not inventing a new one.
  7. Revocation: `revoke_strategy()` appends a REVOKED event; it never
     deletes or rewrites prior events. A revoked strategy's full history
     (including the fact it was once promoted, by whom, and why it was
     later revoked) remains permanently readable.
  8. Fail-closed: missing, malformed, stale (expired), or inconsistent
     evidence blocks promotion. A record whose stored strategy_id no longer
     matches a fresh fingerprint of its own spec_body, or whose current_state
     cannot be re-derived from its own events log, is treated as CORRUPTED
     and is excluded from the promotion snapshot -- never silently trusted.
  9. Integration with v0.5.3.23: `build_promotion_snapshot()` materializes a
     small, self-hashed JSON artifact mapping regime_state label -> BUY/SELL,
     built ONLY from currently-PROMOTED, integrity-verified records. v0.5.3.23
     gains an optional `--promotion-input` argument (see that file's own
     change) that loads exactly this artifact. Deliberately NOT wired here:
     this module does not modify aura_v05321_alpaca_paper_runtime.py or
     .github/workflows/paper-cycle-dashboard.yml, so today's live, scheduled
     cycle behaves EXACTLY as before -- both allowlists still resolve empty,
     because no promotion snapshot is passed. Actually promoting a strategy
     into the live schedule remains a separate, deliberate, future action,
     exactly as v0.5.3.23's own docstring already insists it must be.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
--------------------------------------------
  - No AI/ML functionality of any kind (no LLM calls, no proposal generation,
    no evidence scoring).
  - No execution functionality (does not submit, size, or authorize orders;
    does not touch v0.5.3.16/.19/.22/.26).
  - No modification of v0.5.3.31-.38.
  - No redesign of the regime taxonomy -- `regime_state` values are treated
    as opaque strings, exactly as v0.5.3.13/.14/.23 already do.
  - No adversarial-challenge role, no non-expansive AI-output repair, no
    Ghost Trades/counterfactual ledger. Those remain scoped to their
    designated later milestones (.49/.41 in the current baseline) and are
    not prerequisites of this one.
  - Exactly one regime_state label per strategy (deliberately narrower than
    the roadmap's abstract "labels" plural) -- keeps this milestone's
    conflict handling and auditability simple; multi-label strategies are
    future work if ever needed, not assumed here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VERSION = "AURA v0.5.3.39"
ENGINE = "STRATEGY_REGISTRY"
SCHEMA_VERSION = "1.0"

DEFAULT_REGISTRY_DIR = Path("regime_output") / "strategy_registry"
DEFAULT_SNAPSHOT_OUTPUT = Path("regime_output") / "strategy_registry" / "promotion_snapshot.json"

DIRECTIONS: frozenset[str] = frozenset({"LONG", "SHORT"})
SOURCE_KINDS: frozenset[str] = frozenset(
    {"DETERMINISTIC_RESEARCH", "AI_PROPOSED", "HUMAN_PROPOSED"}
)

# Exactly the nine evidentiary categories named in the v0.5.5 roadmap's
# Research Promotion Gate description (§5). Order is preserved for stable
# reporting; the set itself is what promotion logic actually checks against.
EVIDENCE_CATEGORIES: tuple[str, ...] = (
    "OOS_VALIDATION",
    "WALK_FORWARD",
    "PERMUTATION_MATCHED_CONTROLS",
    "COST_SLIPPAGE_SENSITIVITY",
    "PARAMETER_PLATEAU",
    "UNSEEN_SYMBOL_VALIDATION",
    "REGIME_STABILITY",
    "PROSPECTIVE_EVIDENCE",
    "PAPER_EXECUTION_EVIDENCE",
)
EVIDENCE_CATEGORY_SET = frozenset(EVIDENCE_CATEGORIES)

STATES: frozenset[str] = frozenset({"CANDIDATE", "REJECTED", "PROMOTED", "REVOKED"})
TERMINAL_STATES: frozenset[str] = frozenset({"REVOKED"})

_DIRECTION_TO_SIDE = {"LONG": "BUY", "SHORT": "SELL"}


class RegistryError(Exception):
    """Raised for any fail-closed condition. Callers must not catch this
    and substitute a default -- every raise site here is a deliberate
    refusal, not an incidental failure."""


# --------------------------------------------------------------------- #
# canonical hashing -- identical convention to every other v0.5.3 module
# --------------------------------------------------------------------- #


def stable_json(obj: Any) -> str:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------- #
# identity / frozen spec
# --------------------------------------------------------------------- #


def compute_strategy_id(spec_body: dict[str, Any]) -> str:
    digest = sha256_text(stable_json(spec_body))
    return f"STRAT-{digest[:24]}"


def freeze_spec(
    *,
    strategy_name: str,
    version: int,
    regime_state_label: str,
    direction: str,
    source_kind: str,
    proposed_by: str,
    description: str = "",
) -> dict[str, Any]:
    """Produces an immutable spec_body plus its content-fingerprinted
    strategy_id. Deliberately minimal: AURA's current regime-label chain
    (.12/.13/.14/.23) has no numeric strategy parameters anywhere upstream
    of this module, so none are invented here. A future milestone that adds
    real parameterized strategies should extend spec_body then -- not this
    one, and not by guessing a shape now."""
    if not isinstance(strategy_name, str) or not strategy_name.strip():
        raise RegistryError("INVALID_STRATEGY_NAME")
    if not isinstance(version, int) or version < 1:
        raise RegistryError("INVALID_VERSION")
    if not isinstance(regime_state_label, str) or not regime_state_label:
        raise RegistryError("INVALID_REGIME_STATE_LABEL")
    if direction not in DIRECTIONS:
        raise RegistryError(f"INVALID_DIRECTION:{direction}")
    if source_kind not in SOURCE_KINDS:
        raise RegistryError(f"INVALID_SOURCE_KIND:{source_kind}")
    if not isinstance(proposed_by, str) or not proposed_by.strip():
        raise RegistryError("INVALID_PROPOSED_BY")

    spec_body = {
        "schema_version": SCHEMA_VERSION,
        "strategy_name": strategy_name,
        "version": version,
        "regime_state_label": regime_state_label,
        "direction": direction,
        "source_kind": source_kind,
        "proposed_by": proposed_by,
        "description": description,
    }
    strategy_id = compute_strategy_id(spec_body)
    return {
        "strategy_id": strategy_id,
        "spec_body": spec_body,
        "created_at": now_iso(),
    }


# --------------------------------------------------------------------- #
# record I/O -- atomic create, read-verify-append-atomic-rename.
# Mirrors v0.5.3.29's own discipline directly rather than inventing a
# parallel storage pattern.
# --------------------------------------------------------------------- #


def _record_path(strategy_id: str, registry_dir: Path) -> Path:
    return registry_dir / f"{strategy_id}.json"


def _lock_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".lock")


class _FileLock:
    def __init__(self, path: Path, timeout_seconds: float = 5.0):
        self._lock_path = _lock_path(path)
        self._timeout = timeout_seconds
        self._fd: int | None = None

    def __enter__(self) -> "_FileLock":
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self._timeout
        while True:
            try:
                self._fd = os.open(str(self._lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise RegistryError(f"LOCK_TIMEOUT:{self._lock_path}")
                time.sleep(0.02)

    def __exit__(self, *exc_info: Any) -> None:
        if self._fd is not None:
            os.close(self._fd)
        try:
            os.unlink(self._lock_path)
        except FileNotFoundError:
            pass


def _write_record_atomic(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, ensure_ascii=False, allow_nan=False)
        f.write("\n")
    os.replace(tmp_path, path)


def _read_record_raw(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as f:
        record = json.load(f)
    if not isinstance(record, dict):
        raise RegistryError(f"RECORD_NOT_OBJECT:{path.name}")
    return record


def _canonical_record_hash_fields(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "engine": record.get("engine"),
        "schema_version": record.get("schema_version"),
        "strategy_id": record.get("strategy_id"),
        "spec_body": record.get("spec_body"),
        "created_at": record.get("created_at"),
        "events": record.get("events"),
    }


def _canonical_record_hash(record: dict[str, Any]) -> str:
    return sha256_text(stable_json(_canonical_record_hash_fields(record)))


# --------------------------------------------------------------------- #
# state derivation -- current_state is always DERIVED from events, never
# trusted as stored data alone (same discipline as v0.5.3.29's derive_state).
# --------------------------------------------------------------------- #


def derive_state(events: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """Replays the append-only events list into a (state, rejection_reasons)
    pair. The only events this module ever appends are REGISTERED,
    EVIDENCE_RECORDED, PROMOTION_EVALUATED (fields: {"result":
    "PROMOTED"|"REJECTED", "rejection_reasons": [...]}), and REVOKED."""
    if not events:
        raise RegistryError("EMPTY_EVENTS")
    if events[0].get("event") != "REGISTERED":
        raise RegistryError("EVENTS_DO_NOT_START_WITH_REGISTERED")

    state = "CANDIDATE"
    rejection_reasons: list[str] = []
    for event in events[1:]:
        kind = event.get("event")
        if kind == "EVIDENCE_RECORDED":
            if state in TERMINAL_STATES:
                raise RegistryError("EVIDENCE_RECORDED_AFTER_REVOKED")
            continue
        if kind == "PROMOTION_EVALUATED":
            if state in TERMINAL_STATES:
                raise RegistryError("EVALUATION_AFTER_REVOKED")
            fields = event.get("fields") or {}
            result = fields.get("result")
            if result not in ("PROMOTED", "REJECTED"):
                raise RegistryError(f"ILLEGAL_EVALUATION_RESULT:{result}")
            state = result
            rejection_reasons = list(fields.get("rejection_reasons") or [])
            continue
        if kind == "REVOKED":
            if state != "PROMOTED":
                raise RegistryError("REVOKED_FROM_NON_PROMOTED_STATE")
            state = "REVOKED"
            rejection_reasons = []
            continue
        raise RegistryError(f"UNKNOWN_EVENT_KIND:{kind}")

    return state, rejection_reasons


def verify_record(record: dict[str, Any]) -> tuple[bool, list[str]]:
    """Fail-closed structural + tamper verification. Returns (ok, errors).
    A record failing this must never be treated as PROMOTED by any caller,
    regardless of what its stored current_state field claims."""
    errors: list[str] = []

    if record.get("engine") != ENGINE:
        errors.append("WRONG_ENGINE")
    if record.get("schema_version") != SCHEMA_VERSION:
        errors.append("WRONG_SCHEMA_VERSION")

    spec_body = record.get("spec_body")
    stored_strategy_id = record.get("strategy_id")
    if not isinstance(spec_body, dict):
        errors.append("MISSING_SPEC_BODY")
    else:
        try:
            recomputed_id = compute_strategy_id(spec_body)
        except Exception:
            errors.append("SPEC_BODY_NOT_HASHABLE")
        else:
            if recomputed_id != stored_strategy_id:
                errors.append("SPEC_INTEGRITY_MISMATCH")

    supplied_hash = record.get("record_hash")
    if not isinstance(supplied_hash, str) or not supplied_hash:
        errors.append("MISSING_RECORD_HASH")
    else:
        try:
            calculated = _canonical_record_hash(record)
        except Exception:
            errors.append("RECORD_NOT_HASHABLE")
        else:
            if calculated != supplied_hash:
                errors.append("RECORD_HASH_MISMATCH")

    events = record.get("events")
    if not isinstance(events, list) or not events:
        errors.append("MISSING_OR_EMPTY_EVENTS")
        return False, errors

    try:
        derived_state, derived_reasons = derive_state(events)
    except RegistryError as exc:
        errors.append(f"ILLEGAL_EVENT_SEQUENCE:{exc}")
        return False, errors

    if record.get("current_state") != derived_state:
        errors.append(
            f"CURRENT_STATE_DRIFT:stored={record.get('current_state')!r}:derived={derived_state!r}"
        )
    if record.get("rejection_reasons") != derived_reasons:
        errors.append("REJECTION_REASONS_DRIFT")

    return (len(errors) == 0), errors


def load_record(strategy_id: str, registry_dir: Path) -> dict[str, Any]:
    """Fail-closed load: any verification failure raises rather than
    returning a possibly-tampered record to the caller."""
    path = _record_path(strategy_id, registry_dir)
    raw = _read_record_raw(path)
    if raw is None:
        raise RegistryError(f"STRATEGY_NOT_FOUND:{strategy_id}")
    ok, errors = verify_record(raw)
    if not ok:
        raise RegistryError(f"RECORD_VERIFICATION_FAILED:{strategy_id}:{','.join(errors)}")
    return raw


def _finalize_and_write(record: dict[str, Any], path: Path) -> dict[str, Any]:
    events = record["events"]
    state, rejection_reasons = derive_state(events)
    record["current_state"] = state
    record["rejection_reasons"] = rejection_reasons
    record["record_hash"] = _canonical_record_hash(record)
    _write_record_atomic(path, record)
    return record


# --------------------------------------------------------------------- #
# public mutators
# --------------------------------------------------------------------- #


def register_strategy(spec: dict[str, Any], registry_dir: Path) -> dict[str, Any]:
    """Idempotent on content: registering byte-identical spec content a
    second time returns the existing record unchanged rather than creating
    a duplicate or raising -- mirrors v0.5.3.29's "structural mutator ...
    already reached ... silent no-op" convention. Registering a DIFFERENT
    spec_body that happens to collide on strategy_id is cryptographically
    not a real-world concern (sha256) and is treated as RECORD_HASH
    corruption if it ever occurs, never silently overwritten."""
    strategy_id = spec["strategy_id"]
    path = _record_path(strategy_id, registry_dir)

    with _FileLock(path):
        existing = _read_record_raw(path)
        if existing is not None:
            ok, errors = verify_record(existing)
            if not ok:
                raise RegistryError(
                    f"EXISTING_RECORD_CORRUPTED:{strategy_id}:{','.join(errors)}"
                )
            if existing.get("spec_body") != spec["spec_body"]:
                raise RegistryError(f"STRATEGY_ID_COLLISION_WITH_DIFFERENT_SPEC:{strategy_id}")
            return existing

        record: dict[str, Any] = {
            "engine": ENGINE,
            "schema_version": SCHEMA_VERSION,
            "strategy_id": strategy_id,
            "spec_body": spec["spec_body"],
            "created_at": spec["created_at"],
            "events": [
                {
                    "event": "REGISTERED",
                    "at": now_iso(),
                    "fields": {},
                }
            ],
        }
        return _finalize_and_write(record, path)


def record_evidence(
    *,
    strategy_id: str,
    category: str,
    status: str,
    evidence_ref: str,
    recorded_by: str,
    notes: str = "",
    expires_at: str | None = None,
    registry_dir: Path,
) -> dict[str, Any]:
    """Appends one EVIDENCE_RECORDED event. `status` must be PASS or FAIL --
    there is no third option; evidence that was never supplied simply never
    has an event for that category, which evaluate_promotion() treats as
    NOT_PROVIDED. `expires_at`, if given, is supplied by whoever recorded
    the evidence (the validation-evidence owner), never invented by this
    module -- the registry only ENFORCES an expiry that's already been
    stated, exactly like .23 refuses to infer a direction it wasn't told."""
    if category not in EVIDENCE_CATEGORY_SET:
        raise RegistryError(f"UNKNOWN_EVIDENCE_CATEGORY:{category}")
    if status not in ("PASS", "FAIL"):
        raise RegistryError(f"INVALID_EVIDENCE_STATUS:{status}")
    if not isinstance(evidence_ref, str) or not evidence_ref.strip():
        raise RegistryError("MISSING_EVIDENCE_REF")
    if not isinstance(recorded_by, str) or not recorded_by.strip():
        raise RegistryError("MISSING_RECORDED_BY")
    if expires_at is not None:
        try:
            datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            raise RegistryError(f"MALFORMED_EXPIRES_AT:{expires_at}") from None

    path = _record_path(strategy_id, registry_dir)
    with _FileLock(path):
        record = load_record(strategy_id, registry_dir)
        state, _ = derive_state(record["events"])
        if state in TERMINAL_STATES:
            raise RegistryError(f"CANNOT_RECORD_EVIDENCE_ON_TERMINAL_STRATEGY:{strategy_id}")

        record["events"].append(
            {
                "event": "EVIDENCE_RECORDED",
                "at": now_iso(),
                "fields": {
                    "category": category,
                    "status": status,
                    "evidence_ref": evidence_ref,
                    "recorded_by": recorded_by,
                    "notes": notes,
                    "expires_at": expires_at,
                },
            }
        )
        return _finalize_and_write(record, path)


def _current_evidence_by_category(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Latest EVIDENCE_RECORDED event per category wins -- a corrected
    resubmission for the same category supersedes an earlier one, and the
    full history of both remains in `events` regardless."""
    latest: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("event") == "EVIDENCE_RECORDED":
            fields = event["fields"]
            latest[fields["category"]] = fields
    return latest


def evaluate_promotion(
    record: dict[str, Any], *, at: str | None = None
) -> tuple[str, list[str]]:
    """Pure function: given a verified record, returns (result,
    rejection_reasons) without mutating anything or touching disk.
    result is PROMOTED only if every one of the nine evidence categories
    has a PASS status that is not expired as of `at` (default: now)."""
    at_dt = (
        datetime.now(timezone.utc)
        if at is None
        else datetime.fromisoformat(at.replace("Z", "+00:00"))
    )

    # evaluate_promotion() is called only on records already verified by
    # load_record(), so record_hash/state-derivation integrity is already
    # established by the caller. Spec-integrity is re-checked explicitly
    # here anyway, defensively, since it is cheap and this function's
    # result directly gates promotion.
    recomputed_id = compute_strategy_id(record["spec_body"])
    if recomputed_id != record["strategy_id"]:
        return "REJECTED", ["SPEC_INTEGRITY_MISMATCH"]

    evidence = _current_evidence_by_category(record["events"])
    reasons: list[str] = []
    for category in EVIDENCE_CATEGORIES:
        item = evidence.get(category)
        if item is None:
            reasons.append(f"MISSING_EVIDENCE:{category}")
            continue
        if item["status"] != "PASS":
            reasons.append(f"FAILED_EVIDENCE:{category}")
            continue
        expires_at = item.get("expires_at")
        if expires_at is not None:
            expires_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if expires_dt <= at_dt:
                reasons.append(f"EXPIRED_EVIDENCE:{category}")

    if reasons:
        return "REJECTED", sorted(reasons)
    return "PROMOTED", []


def apply_evaluation(strategy_id: str, registry_dir: Path) -> dict[str, Any]:
    """Loads, evaluates, and durably records the evaluation outcome as a
    PROMOTION_EVALUATED event. Safe to call repeatedly (idempotent in
    effect, though it always appends a new event -- the event log is a
    history of evaluation attempts, not just the final answer, by design:
    it must be possible to see that a strategy was re-evaluated and to see
    the outcome at each attempt)."""
    path = _record_path(strategy_id, registry_dir)
    with _FileLock(path):
        record = load_record(strategy_id, registry_dir)
        state, _ = derive_state(record["events"])
        if state in TERMINAL_STATES:
            raise RegistryError(f"CANNOT_EVALUATE_TERMINAL_STRATEGY:{strategy_id}")

        result, reasons = evaluate_promotion(record)
        record["events"].append(
            {
                "event": "PROMOTION_EVALUATED",
                "at": now_iso(),
                "fields": {"result": result, "rejection_reasons": reasons},
            }
        )
        return _finalize_and_write(record, path)


def revoke_strategy(
    strategy_id: str, *, reason: str, revoked_by: str, registry_dir: Path
) -> dict[str, Any]:
    """Only legal from PROMOTED. Terminal: appends REVOKED and nothing may
    ever mutate this record again. History (including the PROMOTED period)
    is preserved in full, never deleted or rewritten."""
    if not isinstance(reason, str) or not reason.strip():
        raise RegistryError("MISSING_REVOCATION_REASON")
    if not isinstance(revoked_by, str) or not revoked_by.strip():
        raise RegistryError("MISSING_REVOKED_BY")

    path = _record_path(strategy_id, registry_dir)
    with _FileLock(path):
        record = load_record(strategy_id, registry_dir)
        state, _ = derive_state(record["events"])
        if state != "PROMOTED":
            raise RegistryError(f"CANNOT_REVOKE_FROM_STATE:{state}")

        record["events"].append(
            {
                "event": "REVOKED",
                "at": now_iso(),
                "fields": {"reason": reason, "revoked_by": revoked_by},
            }
        )
        return _finalize_and_write(record, path)


# --------------------------------------------------------------------- #
# .23-facing snapshot -- the actual promotion-gate artifact
# --------------------------------------------------------------------- #


def list_all_records(registry_dir: Path) -> list[dict[str, Any]]:
    if not registry_dir.exists():
        return []
    records = []
    for path in sorted(registry_dir.glob("STRAT-*.json")):
        raw = _read_record_raw(path)
        if raw is not None:
            records.append(raw)
    return records


def build_promotion_snapshot(registry_dir: Path) -> dict[str, Any]:
    """Builds the artifact v0.5.3.23 optionally consumes. Only records that
    (a) verify cleanly (record_hash, spec integrity, state derivation all
    consistent) and (b) are currently PROMOTED contribute an entry. Two
    verified-PROMOTED strategies claiming the same regime_state_label with
    conflicting directions is a fail-closed condition for that label only
    -- it is excluded from the allowlist (never guessed, never resolved by
    "latest wins") and reported explicitly in `conflicts`."""
    long_labels: dict[str, list[str]] = {}
    short_labels: dict[str, list[str]] = {}
    corrupted: list[str] = []

    for record in list_all_records(registry_dir):
        ok, errors = verify_record(record)
        strategy_id = record.get("strategy_id", "UNKNOWN")
        if not ok:
            corrupted.append(f"{strategy_id}:{','.join(errors)}")
            continue
        state, _ = derive_state(record["events"])
        if state != "PROMOTED":
            continue
        spec = record["spec_body"]
        label = spec["regime_state_label"]
        if spec["direction"] == "LONG":
            long_labels.setdefault(label, []).append(strategy_id)
        else:
            short_labels.setdefault(label, []).append(strategy_id)

    conflicts: list[str] = []
    validated_long: list[str] = []
    validated_short: list[str] = []

    all_labels = set(long_labels) | set(short_labels)
    for label in sorted(all_labels):
        in_long = label in long_labels
        in_short = label in short_labels
        both_directions_conflict = in_long and in_short
        multiple_long = in_long and len(long_labels[label]) > 1
        multiple_short = in_short and len(short_labels[label]) > 1
        if both_directions_conflict or multiple_long or multiple_short:
            conflicts.append(label)
            continue
        if in_long:
            validated_long.append(label)
        else:
            validated_short.append(label)

    result = {
        "agent_version": VERSION,
        "engine": "PROMOTION_SNAPSHOT",
        "generated_at": now_iso(),
        "validated_long_entry_regime_labels": sorted(validated_long),
        "validated_short_entry_regime_labels": sorted(validated_short),
        "conflicts": sorted(conflicts),
        "corrupted_records": sorted(corrupted),
        "guardrails": {
            "ai_ml_involved": False,
            "execution_authority": False,
            "labels_from_promoted_only": True,
            "corrupted_records_excluded": True,
            "conflicting_labels_excluded": True,
        },
    }
    canonical = {
        k: result[k]
        for k in (
            "engine",
            "validated_long_entry_regime_labels",
            "validated_short_entry_regime_labels",
            "conflicts",
            "corrupted_records",
        )
    }
    result["state_hash"] = sha256_text(stable_json(canonical))
    result["state_id"] = f"PG-{result['state_hash'][:24]}"
    return result


def side_for_label(snapshot: dict[str, Any], regime_state: Any) -> str | None:
    """The exact function v0.5.3.23's determine_side() delegates to when a
    --promotion-input snapshot is supplied. Returns None (never a guess)
    for anything not present, exactly like .23's own current behavior."""
    if regime_state in (snapshot.get("validated_long_entry_regime_labels") or []):
        return "BUY"
    if regime_state in (snapshot.get("validated_short_entry_regime_labels") or []):
        return "SELL"
    return None


# --------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------- #


def _print_record(record: dict[str, Any]) -> None:
    state, reasons = derive_state(record["events"])
    print(f"strategy_id       : {record['strategy_id']}")
    print(f"strategy_name     : {record['spec_body']['strategy_name']} (v{record['spec_body']['version']})")
    print(f"regime_state_label: {record['spec_body']['regime_state_label']}")
    print(f"direction         : {record['spec_body']['direction']}")
    print(f"source_kind       : {record['spec_body']['source_kind']}")
    print(f"state             : {state}")
    if reasons:
        print("rejection_reasons :")
        for r in reasons:
            print(f"  - {r}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry-dir", type=Path, default=DEFAULT_REGISTRY_DIR)
    sub = parser.add_subparsers(dest="command", required=True)

    p_register = sub.add_parser("register")
    p_register.add_argument("--strategy-name", required=True)
    p_register.add_argument("--version", type=int, required=True)
    p_register.add_argument("--regime-state-label", required=True)
    p_register.add_argument("--direction", required=True, choices=sorted(DIRECTIONS))
    p_register.add_argument("--source-kind", required=True, choices=sorted(SOURCE_KINDS))
    p_register.add_argument("--proposed-by", required=True)
    p_register.add_argument("--description", default="")

    p_evidence = sub.add_parser("evidence")
    p_evidence.add_argument("--strategy-id", required=True)
    p_evidence.add_argument("--category", required=True, choices=EVIDENCE_CATEGORIES)
    p_evidence.add_argument("--status", required=True, choices=("PASS", "FAIL"))
    p_evidence.add_argument("--evidence-ref", required=True)
    p_evidence.add_argument("--recorded-by", required=True)
    p_evidence.add_argument("--notes", default="")
    p_evidence.add_argument("--expires-at", default=None)

    p_evaluate = sub.add_parser("evaluate")
    p_evaluate.add_argument("--strategy-id", required=True)

    p_revoke = sub.add_parser("revoke")
    p_revoke.add_argument("--strategy-id", required=True)
    p_revoke.add_argument("--reason", required=True)
    p_revoke.add_argument("--revoked-by", required=True)

    p_show = sub.add_parser("show")
    p_show.add_argument("--strategy-id", required=True)

    p_snapshot = sub.add_parser("snapshot")
    p_snapshot.add_argument("--output", type=Path, default=DEFAULT_SNAPSHOT_OUTPUT)

    args = parser.parse_args()

    try:
        if args.command == "register":
            spec = freeze_spec(
                strategy_name=args.strategy_name,
                version=args.version,
                regime_state_label=args.regime_state_label,
                direction=args.direction,
                source_kind=args.source_kind,
                proposed_by=args.proposed_by,
                description=args.description,
            )
            record = register_strategy(spec, args.registry_dir)
            _print_record(record)
            return 0

        if args.command == "evidence":
            record = record_evidence(
                strategy_id=args.strategy_id,
                category=args.category,
                status=args.status,
                evidence_ref=args.evidence_ref,
                recorded_by=args.recorded_by,
                notes=args.notes,
                expires_at=args.expires_at,
                registry_dir=args.registry_dir,
            )
            _print_record(record)
            return 0

        if args.command == "evaluate":
            record = apply_evaluation(args.strategy_id, args.registry_dir)
            _print_record(record)
            return 0

        if args.command == "revoke":
            record = revoke_strategy(
                args.strategy_id,
                reason=args.reason,
                revoked_by=args.revoked_by,
                registry_dir=args.registry_dir,
            )
            _print_record(record)
            return 0

        if args.command == "show":
            record = load_record(args.strategy_id, args.registry_dir)
            _print_record(record)
            return 0

        if args.command == "snapshot":
            snapshot = build_promotion_snapshot(args.registry_dir)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("w", encoding="utf-8") as f:
                json.dump(snapshot, f, indent=2, ensure_ascii=False, allow_nan=False)
                f.write("\n")
            print(f"{VERSION} — PROMOTION SNAPSHOT")
            print(f"  LONG  labels : {snapshot['validated_long_entry_regime_labels']}")
            print(f"  SHORT labels : {snapshot['validated_short_entry_regime_labels']}")
            if snapshot["conflicts"]:
                print(f"  CONFLICTS (excluded): {snapshot['conflicts']}")
            if snapshot["corrupted_records"]:
                print(f"  CORRUPTED (excluded): {snapshot['corrupted_records']}")
            print(f"  OUTPUT: {args.output.resolve()}")
            return 0

        parser.error(f"unknown command {args.command!r}")
        return 2
    except RegistryError as exc:
        print(f"FAIL-CLOSED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
