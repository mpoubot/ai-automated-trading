#!/usr/bin/env python3
"""
AURA v0.5.3.38 -- Common Execution Supervisor

The major architectural integration milestone (per Martin's 2026-09-11 GO
message): brings the two proven execution spines -- MEXC crypto futures
(.27/.28/.29/.30/.31/.32) and Alpaca equities/ETFs (.34/.35/.36/.37) --
under one common orchestration boundary, without redesigning or duplicating
either spine's own safety mechanisms.

    Decision
        |
        v
    .33 Canonical Execution Specification
        |
        v
    .34 Instrument Metadata
        |
        v
    venue-specific Authorization      (.31 MEXC  | .36 Alpaca)
        |
        v
    venue-specific Replay Protection  (.32 MEXC  | .37 Alpaca)
        |
        v
    venue-specific Adapter            (.27 MEXC  | .35 Alpaca)
        |
        v
    SUPERVISOR FINAL EXECUTION CONTROL       <- THIS MODULE
        |
        v
    submission
        |
        v
    observation                        (.28 MEXC | NOT YET BUILT for Alpaca)
        |
        v
    reconciliation / recovery          (.29/.30 MEXC | NOT YET BUILT for Alpaca)

============================================================================
0. Inspection done before writing any code below (per explicit instruction:
   "do not assume that a previous report is sufficient")
============================================================================

Every module named above was read IN FULL this session, directly from
source, not from an earlier report:

  .19 (aura_v05319_execution_safety.py) -- a BATCH CLI TOOL over the OLD
    .12-.18 BTC/ETH pipeline's reconciliation.json file. It hardcodes
    execution_authorized=False in every branch and its REQUIRED_SYMBOLS are
    the Alpaca crypto pair (BTC/USD, ETH/USD), not MEXC's or the equity
    track's symbols. Neither .31 nor .36 import or call it -- each built
    its OWN safety-state assembler instead (documented in their own
    docstrings). This module does the same: it does NOT import or route
    data through .19, and does NOT pretend .19 is an authorization source
    for either venue (Martin's explicit instruction, section 7).
  .23 (aura_v05323_execution_specification_builder.py) -- also legacy:
    targets .22's Alpaca-CRYPTO wire shape (exchange="ALPACA",
    account_mode="PAPER", BTC/USD-style symbols), with both its direction
    allowlists empty by default. It is NOT a canonical-spec producer and is
    NOT used anywhere in this module -- .33 is the real canonical producer.
  .27/.28/.29/.30/.31/.32 (MEXC spine) -- read in full. Key findings that
    shaped this module's design, below (section 1).
  .33/.34/.35/.36/.37 (Alpaca-equity spine) -- .33/.34 read in full this
    session; .35/.36/.37 were built earlier in this same engagement and are
    already fully known. Key contracts reused directly, below (section 2).

============================================================================
1. MEXC spine -- what was actually found, and what it means for this module
============================================================================

  - `.31.authorize()` does NOT take .33's canonical spec. It takes MEXC's
    own WIRE-format spec (exchange="MEXC", account_mode, market_type,
    side/reduce_only/quantity/leverage/client_order_id) -- exactly what
    `.27.validate_spec()` expects. `.33.to_mexc_execution_spec()` ALREADY
    exists and is exactly this translator (canonical -> MEXC wire),
    including the direction -> (side, reduce_only) mapping. This module
    therefore does NOT build a new canonical->wire translator for MEXC --
    it calls `.33.to_mexc_execution_spec()`, proven-compatible-by-test
    already at `.33`'s own milestone.
  - `.31.authorized_submit()` is ALREADY a mini-orchestrator: one call does
    (a) claim via `.32` (durable, ledger-linked), (b) revalidate via the
    same guardrail policy `.31.authorize()` used, (c) `.27.validate_spec()`
    + `.27.submit()` (which makes its OWN separate client_order_id claim).
    This module does not re-implement any of that -- it calls
    `.31.authorized_submit()` as one atomic step and reacts to its result.
  - CRITICAL STRUCTURAL FACT, disclosed prominently (see section 5 below
    for the full consequence): `.33.to_mexc_execution_spec()` ALWAYS sets
    `account_mode: "LIVE"` in the wire spec it produces -- there is no
    PAPER value, because `.27.validate_spec()` itself hard-requires
    account_mode=="LIVE" (MEXC has no broker-side sandbox at all; see
    `.27`'s own docstring). This means `.31.authorize()` on any MEXC wire
    spec built by `.33` ALWAYS evaluates `live_execution_authorized` in the
    safety config, NEVER `paper_execution_authorized` -- there is no
    reachable "PAPER account_mode" code path for MEXC anywhere in this
    repository. "PAPER-only" for the MEXC spine in THIS milestone is
    therefore enforced structurally, a different way than for Alpaca (see
    section 5) -- never by an account_mode flag, because MEXC's wire
    format has no such option.
  - `.29` (Intent Ledger) is NOT touched by `.31.authorized_submit()` at
    all -- nothing in the MEXC spine automatically records intents. This
    module is the first thing in the repository that actually drives
    `.29`'s mutators (create_intent/record_claimed/record_submission_
    attempted/record_submission_outcome/escalate_to_human) around
    `.31.authorized_submit()`'s single atomic call, mapping its five
    possible outcomes onto the correct ledger transitions (section 3).
  - `.29`'s `client_order_id` is deterministic (built by `.33` from
    decision_id/symbol/direction/signal_timestamp, excluding wall-clock
    fields) -- the SAME decision always produces the SAME client_order_id.
    This module uses that fact as the crash-recovery mechanism: before
    ever authorizing/claiming/submitting, it always checks `.29.get_intent()`
    first. A NEW client_order_id gets a fresh `.29.create_intent()`
    (attempted optimistically, its own O_CREAT|O_EXCL atomicity is what
    actually decides a concurrent race -- see section 4); an EXISTING
    intent is routed to recovery, never re-authorized or re-submitted.

============================================================================
2. Alpaca-equity spine -- what is reused directly, unmodified
============================================================================

  - `.36.authorize()` takes .33's canonical spec DIRECTLY (no wire
    translator exists or is needed for Alpaca equities -- `.35` has none,
    by its own design).
  - `.36.authorized_order_request()` is ALREADY a mini-orchestrator, exactly
    like `.31.authorized_submit()`: it claims through `.37.claim()`
    (wired in the .37 milestone, this same engagement), revalidates, and
    constructs (never submits) the real Alpaca SDK order request via
    `.35.build_order_request_spec()` + `.35.to_alpaca_order_request()`.
    This module calls it as one atomic step, exactly as it calls
    `.31.authorized_submit()` for MEXC.
  - `.35.submit(order_spec, client)` is the ONLY Alpaca submission call.
    Read directly this session: UNLIKE `.27.submit()`, it has NO exception
    classification around `client.submit_order()` -- a broker error or
    network fault simply propagates as an unhandled exception. This is a
    genuine, disclosed gap in `.35` (not something this module invents),
    and `.35` is a protected module this milestone must not modify without
    stopping to report a genuine defect first. Modifying `.35` is NOT
    necessary to fix this: this module wraps its OWN call to `.35.submit()`
    in a try/except and conservatively classifies ANY exception as
    EXECUTION_UNCERTAIN (never REJECTED -- there is no basis in `.35`'s own
    code to distinguish an explicit broker refusal from a timeout), exactly
    matching `.27`'s own "never guess REJECTED" discipline, just enforced
    one layer up instead of inside `.35` itself. Flagged here, and in the
    completion report, as found-but-not-silently-patched.
  - No Alpaca-equity equivalent of `.28`/`.29`/`.30` exists anywhere in the
    repository (confirmed again this session; matches `.36`'s own
    docstring item 0's identical finding for SafetyState.reconciliation_
    health). This module does NOT fabricate one. It defines
    `observe_and_reconcile_alpaca_equity_execution()` as an explicit,
    documented interface a future module can fill in, and that function
    always returns NOT_YET_IMPLEMENTED -- never a fabricated RECONCILED
    verdict (section 6).

============================================================================
3. MEXC outcome -> `.29` ledger mapping (the core new orchestration logic)
============================================================================

`.31.authorized_submit()` returns exactly one of five `status` values.
Each is mapped onto `.29`'s mutators as follows -- chosen so that `.29`'s
own event log always reflects EXACTLY what really happened, never more,
never less:

  AUTHORIZATION_ALREADY_CONSUMED
    -- `.32`'s claim was NOT granted by this call (already consumed by an
       earlier attempt). Nothing new happened at the claim layer, so `.29`
       is NOT touched by this call at all. The caller is given
       `existing_intent_id` (from `.32`'s own claim result, threaded
       through by `.31`) to look up via `.29.get_intent()`.
  REVALIDATION_FAILED
    -- The `.32` claim WAS granted (consumed) by this call, but
       revalidation blocked further progress (e.g. kill switch engaged
       between authorize() and this call). `.29.record_claimed()` is
       called (a real claim did happen), then `.29.escalate_to_human()`
       (a claimed-but-permanently-stuck intent needs a human, per Martin's
       "never silently strand a claim" instruction, section 6).
  ADAPTER_SPEC_REJECTED
    -- Claim granted, revalidated, but `.27.validate_spec()` itself raised
       (a genuine wire-spec defect). Same treatment as REVALIDATION_FAILED:
       record_claimed() then escalate_to_human().
  AUTHORIZED_BUT_ADAPTER_CLAIM_REJECTED
    -- Claim granted, revalidated, adapter-validated, but `.27`'s OWN,
       separate client_order_id claim (inside `.27.submit()`) was already
       taken by an earlier attempt -- a genuine anomaly (two different
       authorization_ids converging on the same client_order_id).
       `.29.record_claimed()` then `.29.record_submission_outcome(
       outcome="DUPLICATE_CLAIM_REJECTED")` -- legal from any state per
       `.29`'s own design, routes automatically to ESCALATED_HUMAN_REVIEW.
  SUBMISSION_ATTEMPTED
    -- The full path: claimed, revalidated, adapter-validated, and
       `.27.submit()` was actually called. Its own submission_result.status
       is one of SUBMITTED / REJECTED / EXECUTION_UNCERTAIN (never
       DUPLICATE_CLAIM_REJECTED here -- that is the branch above).
       `.29.record_claimed()` -> `.29.record_submission_attempted()` ->
       `.29.record_submission_outcome(outcome=<mapped>)`, where SUBMITTED
       maps to `.29`'s own "SUBMISSION_ACKNOWLEDGED", REJECTED to
       "SUBMISSION_REJECTED", and EXECUTION_UNCERTAIN passes through
       unchanged.

============================================================================
4. Crash recovery (Martin's seven boundaries, section 6 of the GO message)
============================================================================

A. Before authorization: nothing has been written anywhere yet except,
   for MEXC, possibly a `.29` NEW-state intent (see D). Restart simply
   re-derives the same deterministic client_order_id/canonical spec and
   re-enters this module's entry point -- see D for how that is made safe.
B. After authorization but before replay claim: for MEXC, `.31.authorize()`
   itself writes nothing durable (no claim yet) -- a crash here just means
   the AuthorizationRecord is lost; a fresh authorize() call is safe (the
   record itself is never persisted by `.31`). For Alpaca, same: `.36.
   authorize()` writes nothing durable either.
C. After replay claim but before order construction: cannot actually
   happen as a separate step in either spine -- `.31.authorized_submit()`
   and `.36.authorized_order_request()` both perform claim AND
   construction inside one Python call, so a process crash either happens
   before that call returns (in which case, from this module's
   perspective, the call never completed -- see D/E for how the NEXT
   attempt discovers the claim already exists) or after (handled below).
D. After claim but before/instead of submission (MEXC): THIS is exactly
   why `.29.get_intent()` is checked FIRST, before ever attempting a new
   `create_intent()`/`authorize()`/`authorized_submit()` call, for every
   MEXC entry. An intent already at CLAIMED, SUBMISSION_ATTEMPTED,
   AWAITING_RECONCILIATION, or RECONCILING means a REAL claim already
   happened -- this module refuses to re-run authorization/submission for
   that client_order_id and returns EXISTING_INTENT_IN_PROGRESS instead,
   directing the caller to `supervise_mexc_reconciliation_pass()`. This is
   exactly why `.37.get_claim()` exists on the Alpaca side too (Martin's
   own words) -- the Supervisor-level second claim layer added in section
   5 below is likewise checked, never re-attempted blindly.
E. After submission but before response / F. after response but before
   observation: for MEXC, the intent is now AWAITING_RECONCILIATION --
   `supervise_mexc_reconciliation_pass()` is the recovery entry point,
   safe to call any number of times (idempotent: `.30.reconcile_intent()`
   is a pure function, and `.29`'s own mutators are retry-safe/idempotent
   for identical evidence, escalating only on genuinely CONFLICTING
   evidence).
G. After observation but before reconciliation: cannot happen as a
   separate persisted step either -- `.30.apply_verdict()` is the single
   call that both interprets `.28`'s snapshot AND writes the verdict to
   `.29`; a crash before it returns just means the observation was
   wasted work, safely re-run on the next reconciliation pass (`.28`
   itself makes no state-mutating call, so re-observing is always safe).

For MEXC specifically: the EXECUTION_UNCERTAIN outcome never becomes
permission to retry the SAME client_order_id/authorization -- the intent
simply sits at AWAITING_RECONCILIATION until `.30`'s evidence-driven
verdict resolves it one way or another (RECONCILED_FILLED/CANCELED, or
ESCALATED), exactly matching `.29`'s locked EVIDENCE-AUTHORITY RULE.

CONCURRENCY FINDING, found and fixed during this module's own
implementation (before any concurrency test existed against it): `.29`'s
own `create_intent()` atomicity gates only the FIRST client_order_id
collision -- but the "state == NEW, safe to proceed" fallthrough (this
module, right after the create/get_intent step) has a genuine window
where MULTIPLE threads can all observe NEW before the eventual winner
records a claim. Each of those threads then independently calls
`.31.authorize()`, which -- exactly like `.36.authorize()` on the Alpaca
side -- mints a FRESH, random authorization_id every call, so `.32`'s own
authorization_id-keyed claim does NOT stop more than one thread from
proceeding: the actual single winner is decided only by `.27`'s own
client_order_id claim, deep inside `.27.submit()`. Every losing thread
gets AUTHORIZED_BUT_ADAPTER_CLAIM_REJECTED (or, for a rarer guardrail-
timing race, REVALIDATION_FAILED) and this module escalates the intent to
ESCALATED_HUMAN_REVIEW via `.29.escalate_to_human()` -- which by `.29`'s
own deliberate, UNMODIFIED design refuses a second escalation of an
already-escalated intent (raising, to protect against a redundant
HUMAN-initiated re-escalation). Under concurrency, the first losing
thread's escalation legitimately succeeds; every subsequent losing
thread's identical call would otherwise crash with an unhandled
RuntimeError -- found via this module's own concurrency test, not
guessed. Fixed with `_escalate_or_reread()` below: on exactly this
expected race (and no other RuntimeError), the current, already-correct
intent is re-read and returned instead of letting the exception escape --
`.29` itself is not modified or weakened in any way.

============================================================================
5. PAPER / LIVE boundary -- enforced differently per venue, disclosed
   explicitly rather than smoothed into one story
============================================================================

ALPACA: `ALLOWED_ENVIRONMENTS = frozenset({"PAPER"})` in this module,
checked BEFORE `.36.authorize()` is even called -- `.36` independently
enforces the identical restriction itself (defense in depth; `.36`'s own
ALLOWED_ENVIRONMENTS also excludes LIVE structurally). This module never
constructs an Alpaca TradingClient and never reads Alpaca credentials --
`supervise_alpaca_equity_execution(..., attempt_submission=True)` REQUIRES
an already-constructed `alpaca_client` argument; there is no code path
anywhere in this file that could build one itself, so no configuration
flag of any kind can activate live Alpaca trading from here.

MEXC: has no PAPER account_mode to gate on at all (section 1). This
module's MEXC-path safety boundary is instead: it NEVER constructs a real
ccxt exchange and NEVER reads MEXC credentials -- `supervise_mexc_futures_
execution(..., attempt_submission=True, exchange=...)` REQUIRES an
already-constructed exchange object (real in a hypothetical future
production deployment, a fake test double in every test in this
milestone), exactly mirroring the Alpaca-side discipline. When
`attempt_submission=False`, `exchange` is never even inspected: the
function stops after `.31.authorize()` succeeds and never reaches
`.31.authorized_submit()`/`.27.submit()` at all (an earlier draft of this
function instead always called `authorized_submit()`, passing a
request-refusing stand-in exchange for the preview case -- discovered,
during this module's own implementation, to durably burn both the `.32`
authorization claim and `.27`'s own client_order_id claim for a submission
that never really happened, since `.27.submit()` claims client_order_id
unconditionally before ever touching `exchange`. Fixed before any test was
written against it; see the completion report). `auth_config["live_execution_authorized"]` must be explicitly
set True by a caller for `.31.authorize()` to ever succeed on a MEXC wire
spec (an unavoidable consequence of section 1's finding, not a design
choice made here) -- this is disclosed plainly: that flag's name mentions
"live" because `.31`/`.27`'s own vocabulary does, NOT because setting it
enables real trading. Real trading is prevented by the complete absence of
any credential-reading or exchange-construction code in this file, which
`live_execution_authorized` cannot override or bypass.

Both entry points additionally check this module's OWN kill switch
(SUPERVISOR_DEFAULT_CONFIG["kill_switch"], safe-by-default True) before
authorization is even attempted -- an absolute veto over any NEW execution
attempt on either venue, matching the fail-closed precedent set by `.19`/
`.31`/`.36`. The kill switch deliberately does NOT gate
`supervise_mexc_reconciliation_pass()` or
`observe_and_reconcile_alpaca_equity_execution()` -- reconciliation is
pure observation/recording of what ALREADY happened, never a new order,
so "prevent new execution" does not, and structurally cannot, mean
"prevent finding out what happened to an order already placed" (Martin's
section 7: a kill switch must not make it impossible to safely reconcile
existing positions). No existing module (`.19`, `.31`, `.36`) has a
close-order kill-switch bypass, and none is invented here either -- a
CLOSE_LONG/CLOSE_SHORT decision is itself a new execution attempt and is
blocked by the kill switch exactly like an OPEN, consistent with every
other module in this chain.

============================================================================
6. What this module deliberately does NOT do
============================================================================

No strategy/AI/news/sentiment/Elliott-Wave/stock-selection/portfolio
engine of any kind -- this module orchestrates already-validated
decisions, it does not produce them. No modification to `.17`-`.37` (all
read-only, dynamically imported, called only through existing public
functions) -- the one exception considered, adding a local Alpaca
client_order_id claim layer, is implemented as NEW code in THIS file
(section 5's local claim store below), never by editing `.35`. No live
execution on either venue (section 5). No fabricated Alpaca observation/
reconciliation (section 2/6). `source_kind` is read from the canonical
spec only for pass-through/audit fields -- never consulted by any
authorization/guardrail decision here, exactly matching `.36`'s own
discipline (a dedicated test proves an AI_PROPOSAL and a
DETERMINISTIC_SIGNAL decision are supervised identically).

No real credentials, no network call, anywhere in this file.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VERSION = "AURA v0.5.3.38"
ENGINE = "COMMON_EXECUTION_SUPERVISOR"
SCHEMA_VERSION = "1.0"

ROOT = Path(__file__).resolve().parent

# LIVE is deliberately NOT a member -- see module docstring §5.
ALLOWED_ENVIRONMENTS = frozenset({"PAPER"})

SUPERVISOR_DEFAULT_CONFIG: dict[str, Any] = {"kill_switch": True}

DEFAULT_MEXC_AUTH_CLAIMS_DIR = Path("regime_output/common_execution_supervisor/mexc_authorization_claims")
DEFAULT_MEXC_ADAPTER_CLAIMS_DIR = Path("regime_output/common_execution_supervisor/mexc_adapter_claims")
DEFAULT_MEXC_LEDGER_DIR = Path("regime_output/common_execution_supervisor/mexc_intent_ledger")
DEFAULT_ALPACA_AUTH_CLAIMS_DIR = Path("regime_output/common_execution_supervisor/alpaca_authorization_claims")
DEFAULT_ALPACA_CLIENT_ORDER_ID_CLAIMS_DIR = Path("regime_output/common_execution_supervisor/alpaca_client_order_id_claims")


def fail(message: str) -> None:
    raise RuntimeError(message)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_supervisor_config(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    config = dict(SUPERVISOR_DEFAULT_CONFIG)
    if overrides:
        for key in SUPERVISOR_DEFAULT_CONFIG:
            if key in overrides:
                config[key] = overrides[key]
    return config


# --------------------------------------------------------------------- #
# Dynamic imports -- same _load_*_module() pattern established by every
# module in this chain (.30/.31/.35/.36/.37). None of these modules is
# modified; each is used strictly through its existing public functions.
# --------------------------------------------------------------------- #

def _load_module(module_name: str, filename: str):
    try:
        return __import__(module_name)
    except ImportError:
        import importlib.util
        spec = importlib.util.spec_from_file_location(module_name, ROOT / filename)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod


def _load_canonical_spec_module():
    return _load_module("aura_v05333_canonical_execution_specification", "aura_v05333_canonical_execution_specification.py")


def _load_metadata_module():
    return _load_module("aura_v05334_asset_instrument_metadata", "aura_v05334_asset_instrument_metadata.py")


def _load_alpaca_adapter_module():
    return _load_module("aura_v05335_alpaca_equity_execution_adapter", "aura_v05335_alpaca_equity_execution_adapter.py")


def _load_alpaca_authorization_module():
    return _load_module("aura_v05336_alpaca_equity_execution_authorization", "aura_v05336_alpaca_equity_execution_authorization.py")


def _load_alpaca_replay_module():
    return _load_module("aura_v05337_alpaca_replay_protected_consumption", "aura_v05337_alpaca_replay_protected_consumption.py")


def _load_mexc_adapter_module():
    return _load_module("aura_v05327_mexc_execution_adapter", "aura_v05327_mexc_execution_adapter.py")


def _load_mexc_observed_execution_module():
    return _load_module("aura_v05328_mexc_observed_execution", "aura_v05328_mexc_observed_execution.py")


def _load_mexc_intent_ledger_module():
    return _load_module("aura_v05329_mexc_intent_ledger", "aura_v05329_mexc_intent_ledger.py")


def _load_mexc_reconciliation_module():
    return _load_module("aura_v05330_mexc_intent_reconciliation", "aura_v05330_mexc_intent_reconciliation.py")


def _load_mexc_authorization_module():
    return _load_module("aura_v05331_mexc_execution_authorization", "aura_v05331_mexc_execution_authorization.py")


# --------------------------------------------------------------------- #
# Result helpers -- one common shape for every SupervisionResult, so
# determinism/audit fields (§12 of the GO message) are always present,
# on every path, success or failure.
# --------------------------------------------------------------------- #

def _base_result(canonical_spec: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "agent_version": VERSION,
        "engine": ENGINE,
        "schema_version": SCHEMA_VERSION,
        "status": "BLOCKED",
        "stage": None,
        "reason": None,
        "detail": None,
        "venue": canonical_spec.get("venue") if isinstance(canonical_spec, dict) else None,
        "asset_class": canonical_spec.get("asset_class") if isinstance(canonical_spec, dict) else None,
        "symbol": canonical_spec.get("symbol") if isinstance(canonical_spec, dict) else None,
        "direction": canonical_spec.get("direction") if isinstance(canonical_spec, dict) else None,
        "client_order_id": canonical_spec.get("client_order_id") if isinstance(canonical_spec, dict) else None,
        "spec_fingerprint": canonical_spec.get("spec_fingerprint") if isinstance(canonical_spec, dict) else None,
        "authorization_id": None,
        "supervised_at": now(),
        "order_spec": None,
        "submission_result": None,
        "intent_record": None,
    }


def _blocked(canonical_spec: dict[str, Any] | None, stage: str, reason: str, detail: Any = None,
             **extra: Any) -> dict[str, Any]:
    result = _base_result(canonical_spec)
    result["status"] = "BLOCKED"
    result["stage"] = stage
    result["reason"] = reason
    result["detail"] = detail
    result.update(extra)
    return result


# ======================================================================= #
# Alpaca equity/ETF supervision
# ======================================================================= #

# NEW local claim layer -- the "second, offline layer" the .37 report
# flagged as absent (module docstring §5's referenced instruction, §4 of
# Martin's GO message). Deliberately implemented HERE, as new Supervisor-
# owned code, rather than by modifying `.35` (protected) -- mirrors what
# `.27.claim_client_order_id()` already does for MEXC, one layer up
# instead of inside the adapter itself. Same atomic O_CREAT|O_EXCL
# primitive, same claims-directory pattern, same never-released discipline
# as every other claim store in this codebase (`.27`, `.29`'s intent
# creation, `.32`, `.37`) -- no new mechanism invented, the proven pattern
# reused at a new layer.
ALPACA_CLIENT_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def claim_alpaca_client_order_id(claims_dir: Path, client_order_id: str) -> bool:
    """Atomic, single-use claim on an Alpaca client_order_id, owned by the
    Supervisor, separate from `.37`'s authorization_id store. Returns True
    if THIS call granted the claim, False if already claimed. Never
    released, under any outcome -- there is deliberately no release/delete
    function anywhere in this module (checked by a dedicated structural
    test, mirroring `.32`/`.37`'s own)."""
    if not isinstance(client_order_id, str) or not ALPACA_CLIENT_ORDER_ID_RE.match(client_order_id):
        fail(f"INVALID_CLIENT_ORDER_ID:{client_order_id!r}")
    claims_dir.mkdir(parents=True, exist_ok=True)
    claim_path = claims_dir / f"{client_order_id}.claimed"
    try:
        fd = os.open(str(claim_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as f:
        f.write(now())
    return True


def supervise_alpaca_equity_execution(
    canonical_spec: dict[str, Any],
    alpaca_asset: dict[str, Any],
    *,
    environment: str = "PAPER",
    auth_config: dict[str, Any] | None = None,
    auth_claims_dir: Path | None = None,
    supervisor_claims_dir: Path | None = None,
    attempt_submission: bool = False,
    alpaca_client: Any = None,
    supervisor_config: dict[str, Any] | None = None,
    now_dt: datetime | None = None,
) -> dict[str, Any]:
    """The single entry point for a NEW Alpaca STOCK/ETF execution
    decision. Sequence: canonical spec self-consistency -> venue/asset-
    class gate -> `.34` instrument-metadata resolution -> Supervisor kill
    switch -> `.36.authorize()` -> `.36.authorized_order_request()` (which
    itself claims via `.37` and constructs, never submits) -> this
    module's OWN local client_order_id claim -> (optionally)
    `.35.submit()`, wrapped to classify any exception as EXECUTION_UNCERTAIN
    (module docstring §2). Never bypasses `.33`/authorization/replay
    protection/adapter validation -- there is no earlier return path that
    reaches `order_spec`/submission without every one of those succeeding
    first (module docstring §6, "no bypass path")."""
    cfg = load_supervisor_config(supervisor_config)
    auth_claims_dir = auth_claims_dir or DEFAULT_ALPACA_AUTH_CLAIMS_DIR
    supervisor_claims_dir = supervisor_claims_dir or DEFAULT_ALPACA_CLIENT_ORDER_ID_CLAIMS_DIR

    if not isinstance(canonical_spec, dict):
        return _blocked(None, "CANONICAL_SPEC", "INVALID_CANONICAL_SPEC", "not a dict")

    canon = _load_canonical_spec_module()
    ok, errors = canon.verify_canonical_specification(canonical_spec)
    if not ok:
        return _blocked(canonical_spec, "CANONICAL_SPEC", "MALFORMED_CANONICAL_SPEC", ",".join(errors))

    if environment not in ALLOWED_ENVIRONMENTS:
        reason = "LIVE_EXECUTION_NOT_SUPPORTED" if environment == "LIVE" else "UNSUPPORTED_ENVIRONMENT"
        return _blocked(canonical_spec, "ENVIRONMENT", reason, environment)

    if canonical_spec.get("venue") != "ALPACA" or canonical_spec.get("asset_class") not in ("STOCK", "ETF"):
        return _blocked(canonical_spec, "ASSET_CLASS_VENUE", "NOT_AN_ALPACA_EQUITY_SPEC",
                         f"{canonical_spec.get('venue')}/{canonical_spec.get('asset_class')}")

    meta = _load_metadata_module()
    adapter35 = _load_alpaca_adapter_module()
    try:
        shortability_status = adapter35.alpaca_asset_to_shortability_status(
            alpaca_asset.get("shortable") if isinstance(alpaca_asset, dict) else None,
            alpaca_asset.get("easy_to_borrow") if isinstance(alpaca_asset, dict) else None,
        )
        instrument_metadata = meta.build_instrument_metadata(
            instrument_type=canonical_spec["asset_class"],
            contract_class=None,
            symbol=canonical_spec["symbol"],
            venue="ALPACA",
            shortability_status=shortability_status,
        )
    except Exception as exc:  # noqa: BLE001 -- classified, never silently swallowed
        return _blocked(canonical_spec, "INSTRUMENT_METADATA", "INSTRUMENT_METADATA_RESOLUTION_FAILED",
                         f"{type(exc).__name__}: {exc}")

    if instrument_metadata["canonical_asset_class"] != canonical_spec["asset_class"]:
        return _blocked(canonical_spec, "INSTRUMENT_METADATA", "CANONICAL_ASSET_CLASS_MISMATCH",
                         f"{instrument_metadata['canonical_asset_class']}!={canonical_spec['asset_class']}")

    if cfg["kill_switch"]:
        return _blocked(canonical_spec, "SUPERVISOR_KILL_SWITCH", "SUPERVISOR_KILL_SWITCH_ENGAGED")

    auth36 = _load_alpaca_authorization_module()
    record = auth36.authorize(
        canonical_spec, alpaca_asset, environment=environment, config=auth_config,
        claims_dir=auth_claims_dir, now=now_dt,
    )
    if record.get("status") != "AUTHORIZED":
        return _blocked(canonical_spec, "AUTHORIZATION", record.get("reason"), record.get("detail"))

    order_request = auth36.authorized_order_request(
        record, canonical_spec, alpaca_asset, auth_claims_dir, environment=environment,
        config=auth_config, now=now_dt,
    )
    if order_request["status"] == "AUTHORIZATION_ALREADY_CONSUMED":
        return _blocked(canonical_spec, "REPLAY_PROTECTION", order_request.get("reason"), order_request.get("detail"),
                         authorization_id=record.get("authorization_id"),
                         existing_client_order_id=order_request.get("existing_client_order_id"),
                         existing_claim=order_request.get("existing_claim"))
    if order_request["status"] == "CLAIM_STORE_UNREACHABLE":
        return _blocked(canonical_spec, "REPLAY_PROTECTION", order_request.get("reason"), order_request.get("detail"),
                         authorization_id=record.get("authorization_id"))
    if order_request["status"] != "AUTHORIZED_ORDER_REQUEST_READY":
        return _blocked(canonical_spec, "REVALIDATION", order_request.get("reason"), order_request.get("detail"),
                         authorization_id=record.get("authorization_id"))

    # Second, local claim layer -- see module-level docstring immediately
    # above claim_alpaca_client_order_id(). `.37`'s authorization_id claim
    # is already permanently consumed at this point and stays that way
    # regardless of what happens below (never released).
    granted = claim_alpaca_client_order_id(supervisor_claims_dir, canonical_spec["client_order_id"])
    if not granted:
        return _blocked(
            canonical_spec, "SUPERVISOR_CLIENT_ORDER_ID_CLAIM",
            "CLIENT_ORDER_ID_ALREADY_CLAIMED_AT_SUPERVISOR_LAYER",
            "authorization_id claim succeeded but this client_order_id was already claimed by a "
            "different authorization attempt -- a genuine anomaly, not an ordinary replay",
            status_override="AUTHORIZED_BUT_SUPERVISOR_CLAIM_REJECTED",
            authorization_id=record.get("authorization_id"),
            order_spec=order_request["order_spec"],
        )

    result = _base_result(canonical_spec)
    result["authorization_id"] = record.get("authorization_id")
    result["order_spec"] = order_request["order_spec"]

    if not attempt_submission:
        result["status"] = "READY_FOR_SUBMISSION"
        result["stage"] = "CONSTRUCTION_ONLY"
        return result

    if alpaca_client is None:
        return _blocked(canonical_spec, "SUBMISSION", "MISSING_ALPACA_CLIENT",
                         "attempt_submission=True requires an already-constructed alpaca_client; "
                         "this module never constructs one itself",
                         authorization_id=record.get("authorization_id"), order_spec=order_request["order_spec"])

    try:
        submission_result = adapter35.submit(order_request["order_spec"], alpaca_client)
    except Exception as exc:  # noqa: BLE001 -- see module docstring §2: `.35.submit()` has no
        # exception classification of its own; conservatively EXECUTION_UNCERTAIN, never REJECTED.
        submission_result = {
            "adapter_version": adapter35.VERSION,
            "status": "EXECUTION_UNCERTAIN",
            "client_order_id": order_request["order_spec"]["client_order_id"],
            "observed_at": now(),
            "paper": True,
            "live": False,
            "error": f"{type(exc).__name__}: {exc}",
        }

    result["submission_result"] = submission_result
    result["status"] = submission_result["status"]
    result["stage"] = "SUBMITTED"
    return result


def observe_and_reconcile_alpaca_equity_execution(client_order_id: str, **_: Any) -> dict[str, Any]:
    """Defined interface for future Alpaca-equity observation/
    reconciliation (module docstring §2/§6). No genuine Alpaca-equity
    intent ledger or reconciliation engine (a `.28`/`.29`/`.30` equivalent)
    exists anywhere in this repository today -- confirmed again this
    session, matching `.36`'s own identical finding for SafetyState.
    reconciliation_health. This function intentionally fabricates nothing:
    it always reports NOT_YET_IMPLEMENTED, never a RECONCILED verdict.
    Accepts **kwargs so a future implementation's real parameters
    (lookback window, an injected Alpaca client, etc.) can be added without
    breaking this placeholder's call shape for existing callers."""
    return {
        "agent_version": VERSION,
        "engine": ENGINE,
        "status": "NOT_YET_IMPLEMENTED",
        "reason": "NO_ALPACA_EQUITY_OBSERVATION_RECONCILIATION_SPINE_EXISTS",
        "client_order_id": client_order_id,
        "detail": (
            "A genuine Alpaca-equity intent ledger and reconciliation engine (the .28/.29/.30 "
            "equivalent for the Alpaca-equity spine) does not exist in this repository. This "
            "function defines the call shape a future implementation would fill, without "
            "fabricating a verdict now."
        ),
        "queried_at": now(),
    }


# ======================================================================= #
# MEXC futures supervision
# ======================================================================= #

_MEXC_STATUS_TO_LEDGER_OUTCOME = {
    "SUBMITTED": "SUBMISSION_ACKNOWLEDGED",
    "REJECTED": "SUBMISSION_REJECTED",
    "EXECUTION_UNCERTAIN": "EXECUTION_UNCERTAIN",
}

_MEXC_LEDGER_OPEN_STATES = frozenset({
    "NEW",
})

_MEXC_LEDGER_IN_PROGRESS_STATES = frozenset({
    "CLAIMED", "SUBMISSION_ATTEMPTED", "AWAITING_RECONCILIATION", "RECONCILING",
})


def _escalate_or_reread(ledger29: Any, client_order_id: str, reason: str, ledger_base_dir: Any) -> dict[str, Any]:
    """`.29.escalate_to_human()` deliberately REFUSES to double-escalate an
    already-terminal-or-escalated intent (fails closed by raising, by
    `.29`'s own explicit design -- protecting against a redundant
    HUMAN-initiated re-escalation). But `.31.authorize()`/`.32.claim()`
    mint a FRESH authorization_id on every call (confirmed by direct
    reading -- the exact same property that motivated the new Alpaca
    Supervisor-level client_order_id claim in this module, section 5 of
    the module docstring): so under genuine concurrency, MULTIPLE threads
    can each independently win their OWN authorization_id/`.32` claim for
    the SAME client_order_id (only `.27`'s own client_order_id claim,
    shared across all of them, actually decides the one real winner), and
    every LOSING thread reaches this module's own escalation call site
    for the identical intent. The first loser's call legitimately
    escalates it; every subsequent loser's call would otherwise crash on
    `.29`'s own guard. This is a genuine race in THIS module's own new
    orchestration code (not a `.29` defect -- `.29`'s refusal is correct
    and is left completely unmodified), fixed here: on that specific,
    expected race, the CURRENT authoritative intent (whatever the winning
    thread actually wrote) is re-read and returned instead of letting the
    exception propagate -- never fabricated, always ground truth, and the
    caller's own result.status/stage still correctly reports BLOCKED for
    ITS OWN attempt."""
    try:
        return ledger29.escalate_to_human(client_order_id, reason=reason, base_dir=ledger_base_dir)
    except RuntimeError as exc:
        if "ALREADY_TERMINAL_OR_ESCALATED" not in str(exc):
            # NOTHING_TO_ESCALATE_YET (state still NEW) would mean THIS
            # module called escalate before ever recording a claim -- a
            # genuine ordering bug, never swallowed. Only the
            # already-escalated-by-a-racing-thread case is expected.
            raise
        current = ledger29.get_intent(client_order_id, base_dir=ledger_base_dir)
        if current is None:
            raise
        return current


def _mexc_side_for_ledger(direction: str) -> str:
    # `.29`'s SIDES = {"buy", "sell"} (lowercase) -- derived from the same
    # direction -> side mapping `.33.to_mexc_execution_spec()` uses
    # internally, so the ledger's recorded side always agrees with what
    # was actually sent to `.27`.
    canon = _load_canonical_spec_module()
    side, _reduce_only = canon._MEXC_DIRECTION_TO_SIDE_REDUCE[direction]
    return side.lower()


def _raw_response_ref(submission_result: dict[str, Any]) -> str | None:
    raw = submission_result.get("raw_response")
    if raw is None:
        return None
    return sha256_text(stable_json(raw)) if _is_jsonable(raw) else None


def _is_jsonable(value: Any) -> bool:
    try:
        json.dumps(value, default=str)
        return True
    except (TypeError, ValueError):
        return False


def supervise_mexc_futures_execution(
    canonical_spec: dict[str, Any],
    *,
    auth_config: dict[str, Any] | None = None,
    auth_claims_dir: Path | None = None,
    adapter_claims_dir: Path | None = None,
    ledger_base_dir: Path | None = None,
    exchange: Any = None,
    attempt_submission: bool = False,
    supervisor_config: dict[str, Any] | None = None,
    now_dt: datetime | None = None,
) -> dict[str, Any]:
    """The single entry point for a NEW MEXC CRYPTO_FUTURES execution
    decision. Sequence: canonical spec self-consistency -> venue/asset-
    class gate -> `.34` instrument-metadata resolution -> Supervisor kill
    switch -> crash-recovery check via `.29.get_intent()` -> (new decision
    only) `.29.create_intent()` -> `.33.to_mexc_execution_spec()` ->
    `.31.authorize()` -> IF `attempt_submission` (else stop here, nothing
    claimed anywhere -- see the inline comment above this function's
    `.31.authorized_submit()` call) -> `.31.authorized_submit()` (claims via
    `.32`, revalidates, adapter-validates, calls `.27.submit()`) -> outcome
    mapped onto `.29`'s ledger per module docstring §3.

    `exchange` must already be constructed by the caller (real in a
    hypothetical production deployment, a fake test double in every test
    here) and is REQUIRED when `attempt_submission=True` -- this module
    never builds one itself and never reads MEXC credentials (module
    docstring §5). `.31.authorized_submit()` (and therefore `.27.submit()`,
    which claims client_order_id unconditionally before touching any
    exchange) is called ONLY when `attempt_submission=True` -- never as a
    "dry run" with a stand-in exchange, because that would durably consume
    both replay-protection claims for a submission that never really
    happened."""
    cfg = load_supervisor_config(supervisor_config)
    auth_claims_dir = auth_claims_dir or DEFAULT_MEXC_AUTH_CLAIMS_DIR
    adapter_claims_dir = adapter_claims_dir or DEFAULT_MEXC_ADAPTER_CLAIMS_DIR
    ledger_base_dir = ledger_base_dir or DEFAULT_MEXC_LEDGER_DIR

    if not isinstance(canonical_spec, dict):
        return _blocked(None, "CANONICAL_SPEC", "INVALID_CANONICAL_SPEC", "not a dict")

    canon = _load_canonical_spec_module()
    ok, errors = canon.verify_canonical_specification(canonical_spec)
    if not ok:
        return _blocked(canonical_spec, "CANONICAL_SPEC", "MALFORMED_CANONICAL_SPEC", ",".join(errors))

    if canonical_spec.get("venue") != "MEXC" or canonical_spec.get("asset_class") != "CRYPTO_FUTURES":
        return _blocked(canonical_spec, "ASSET_CLASS_VENUE", "NOT_A_MEXC_FUTURES_SPEC",
                         f"{canonical_spec.get('venue')}/{canonical_spec.get('asset_class')}")

    meta = _load_metadata_module()
    try:
        instrument_metadata = meta.build_instrument_metadata(
            instrument_type="CRYPTO",
            contract_class="PERPETUAL_SWAP",
            symbol=canonical_spec["symbol"],
            venue="MEXC",
        )
    except Exception as exc:  # noqa: BLE001 -- classified, never silently swallowed
        return _blocked(canonical_spec, "INSTRUMENT_METADATA", "INSTRUMENT_METADATA_RESOLUTION_FAILED",
                         f"{type(exc).__name__}: {exc}")

    if instrument_metadata["canonical_asset_class"] != canonical_spec["asset_class"]:
        return _blocked(canonical_spec, "INSTRUMENT_METADATA", "CANONICAL_ASSET_CLASS_MISMATCH",
                         f"{instrument_metadata['canonical_asset_class']}!={canonical_spec['asset_class']}")

    ledger29 = _load_mexc_intent_ledger_module()
    client_order_id = canonical_spec["client_order_id"]

    # Crash-recovery-first: attempt create_intent() optimistically (its own
    # O_CREAT|O_EXCL atomicity is what actually decides a concurrent race --
    # see module docstring §4). A pre-check-then-create would have a
    # TOCTOU race; catching the failure instead does not.
    try:
        intent = ledger29.create_intent(
            client_order_id=client_order_id,
            symbol=canonical_spec["symbol"],
            side=_mexc_side_for_ledger(canonical_spec["direction"]),
            quantity=float(canonical_spec["quantity"]),
            direction=canonical_spec["direction"],
            spec_fingerprint=canonical_spec["spec_fingerprint"],
            base_dir=ledger_base_dir,
        )
        is_new_intent = True
    except RuntimeError as exc:
        if "INTENT_ALREADY_EXISTS" not in str(exc):
            return _blocked(canonical_spec, "INTENT_LEDGER", "INTENT_CREATION_FAILED", str(exc))
        intent = ledger29.get_intent(client_order_id, base_dir=ledger_base_dir)
        is_new_intent = False

    if not is_new_intent:
        state = intent.get("current_state") if intent else None
        if state in _MEXC_LEDGER_IN_PROGRESS_STATES or (
            state == "RECONCILED_PARTIALLY_FILLED" and intent.get("partial_fill_terminal") is False
        ):
            return _blocked(canonical_spec, "EXISTING_INTENT", "EXISTING_INTENT_IN_PROGRESS", state,
                             status_override="EXISTING_INTENT_IN_PROGRESS", intent_record=intent)
        if state not in _MEXC_LEDGER_OPEN_STATES:
            # Terminal or escalated -- informational only, never re-attempted.
            return _blocked(canonical_spec, "EXISTING_INTENT", "EXISTING_INTENT_TERMINAL_OR_ESCALATED", state,
                             status_override="EXISTING_INTENT_TERMINAL_OR_ESCALATED", intent_record=intent)
        # state == "NEW": nothing has ever been claimed for this intent --
        # safe to proceed through authorization/claim/submission below,
        # exactly as if it were newly created.

    if cfg["kill_switch"]:
        return _blocked(canonical_spec, "SUPERVISOR_KILL_SWITCH", "SUPERVISOR_KILL_SWITCH_ENGAGED",
                         intent_record=intent)

    wire_spec = canon.to_mexc_execution_spec(
        canonical_spec, kill_switch=False, execution_authorized=True, live_execution_authorized=True,
    )

    auth31 = _load_mexc_authorization_module()
    record = auth31.authorize(wire_spec, config=auth_config, ledger_base_dir=ledger_base_dir,
                               claims_dir=auth_claims_dir, now=now_dt)
    if record.get("status") != "AUTHORIZED":
        # Nothing claimed yet -- the intent stays at NEW, safe to retry later.
        return _blocked(canonical_spec, "AUTHORIZATION", record.get("reason"), record.get("detail"),
                         intent_record=intent)

    if not attempt_submission:
        # `.31.authorize()` writes NOTHING durable (confirmed by direct
        # reading: no claim file, no ledger write anywhere in its body) --
        # a construction-only preview stops HERE, before any claim is made
        # anywhere (`.32`'s authorization_id claim, `.27`'s own
        # client_order_id claim). This is deliberately NOT the same shape as
        # `.36.authorized_order_request()` for Alpaca: `.31.authorized_
        # submit()` fuses claim + revalidate + adapter-validate + submit
        # into ONE atomic call, by `.31`'s own explicit design ("there is no
        # retry path for the same authorization_id under any outcome"),
        # specifically so a claim is never left stranded without a real
        # submission attempt. Calling `authorized_submit()` here anyway --
        # even with a refusing/fake exchange -- would therefore permanently
        # burn BOTH claims and leave the `.29` intent stuck at
        # SUBMISSION_ATTEMPTED forever with no way to ever complete it. This
        # was found, during this module's own implementation, to be exactly
        # what an earlier draft of this function did (a `_RefusingExchange`
        # stand-in passed into `authorized_submit()` unconditionally) -- a
        # genuine defect in THIS module's own new code, not in `.31`/`.27`,
        # fixed before any test was written against it (see completion
        # report). A construction-only preview for MEXC is therefore
        # `authorize()` succeeding, nothing more -- proving the spec IS
        # authorizable without ever consuming anything.
        result = _base_result(canonical_spec)
        result["authorization_id"] = record.get("authorization_id")
        result["status"] = "READY_FOR_SUBMISSION"
        result["stage"] = "CONSTRUCTION_ONLY"
        result["intent_record"] = intent
        return result

    if exchange is None:
        return _blocked(canonical_spec, "SUBMISSION", "MISSING_EXCHANGE_FOR_SUBMISSION",
                         "attempt_submission=True requires an already-constructed exchange; "
                         "this module never constructs one itself and never reads MEXC credentials",
                         authorization_id=record.get("authorization_id"), intent_record=intent)

    submission = auth31.authorized_submit(
        record, wire_spec, auth_claims_dir, adapter_claims_dir, config=auth_config,
        ledger_base_dir=ledger_base_dir, exchange=exchange, now=now_dt,
    )

    result = _base_result(canonical_spec)
    result["authorization_id"] = record.get("authorization_id")

    if submission["status"] == "AUTHORIZATION_ALREADY_CONSUMED":
        # `.32`'s claim was not granted by THIS call -- nothing new
        # happened, `.29` is not touched (module docstring §3).
        result["status"] = "BLOCKED"
        result["stage"] = "REPLAY_PROTECTION"
        result["reason"] = submission.get("reason")
        result["existing_intent_id"] = submission.get("existing_intent_id")
        result["intent_record"] = intent
        return result

    # From here on, the `.32` claim WAS granted by this call -- record it.
    intent = ledger29.record_claimed(client_order_id, base_dir=ledger_base_dir)

    if submission["status"] in ("REVALIDATION_FAILED", "ADAPTER_SPEC_REJECTED"):
        intent = _escalate_or_reread(
            ledger29, client_order_id, f"{submission['status']}:{submission.get('reason')}", ledger_base_dir,
        )
        result["status"] = "BLOCKED"
        result["stage"] = "REVALIDATION" if submission["status"] == "REVALIDATION_FAILED" else "ADAPTER_VALIDATION"
        result["reason"] = submission.get("reason")
        result["detail"] = submission.get("detail")
        result["intent_record"] = intent
        return result

    if submission["status"] == "AUTHORIZED_BUT_ADAPTER_CLAIM_REJECTED":
        intent = ledger29.record_submission_outcome(
            client_order_id, outcome="DUPLICATE_CLAIM_REJECTED", base_dir=ledger_base_dir,
        )
        result["status"] = "BLOCKED"
        result["stage"] = "ADAPTER_CLIENT_ORDER_ID_CLAIM"
        result["reason"] = submission.get("reason")
        result["detail"] = submission.get("detail")
        result["intent_record"] = intent
        return result

    # submission["status"] == "SUBMISSION_ATTEMPTED" -- reached only when
    # attempt_submission=True and a real, caller-supplied `exchange` was
    # present (guarded above), so `.27.submit()` was called against a real
    # exchange object and `mexc_status` below is always one of its three
    # genuine outcome values, never a stand-in.
    intent = ledger29.record_submission_attempted(client_order_id, base_dir=ledger_base_dir)
    submission_result = submission.get("submission_result") or {}
    mexc_status = submission_result.get("status")

    outcome = _MEXC_STATUS_TO_LEDGER_OUTCOME.get(mexc_status)
    if outcome is None:
        # Should not happen given `.27`'s own closed outcome set -- fail
        # closed via escalation rather than silently dropping evidence.
        intent = _escalate_or_reread(
            ledger29, client_order_id, f"UNRECOGNIZED_ADAPTER_STATUS:{mexc_status}", ledger_base_dir,
        )
        result["status"] = "BLOCKED"
        result["stage"] = "UNRECOGNIZED_ADAPTER_STATUS"
        result["reason"] = mexc_status
        result["intent_record"] = intent
        return result

    intent = ledger29.record_submission_outcome(
        client_order_id, outcome=outcome,
        mexc_order_id=submission_result.get("mexc_order_id"),
        raw_response_ref=_raw_response_ref(submission_result),
        base_dir=ledger_base_dir,
    )
    result["status"] = mexc_status
    result["stage"] = "SUBMITTED"
    result["submission_result"] = submission_result
    result["intent_record"] = intent
    return result


def supervise_mexc_reconciliation_pass(
    client_order_id: str,
    *,
    ledger_base_dir: Path | None = None,
    lookback_minutes: int = 60,
    exchange: Any = None,
    staleness_policy: dict[str, Any] | None = None,
    now_dt: datetime | None = None,
) -> dict[str, Any]:
    """Crash-recovery / ongoing-reconciliation entry point for an EXISTING
    MEXC intent (module docstring §4, boundaries E/F/G). Deliberately NOT
    gated by the Supervisor kill switch (module docstring §5) -- this is
    pure observation/recording of what already happened, never a new
    order. Safe to call any number of times: `.28` makes no state-mutating
    call, and `.29`'s mutators are retry-safe for identical evidence."""
    ledger_base_dir = ledger_base_dir or DEFAULT_MEXC_LEDGER_DIR
    ledger29 = _load_mexc_intent_ledger_module()

    intent = ledger29.get_intent(client_order_id, base_dir=ledger_base_dir)
    if intent is None:
        return {"status": "BLOCKED", "reason": "INTENT_NOT_FOUND", "client_order_id": client_order_id}

    state = intent.get("current_state")
    partial_terminal = intent.get("partial_fill_terminal")
    open_to_reconciliation = state in ("AWAITING_RECONCILIATION", "RECONCILING") or (
        state == "RECONCILED_PARTIALLY_FILLED" and partial_terminal is False
    )
    if not open_to_reconciliation:
        return {"status": "NOT_OPEN_TO_RECONCILIATION", "current_state": state, "intent_record": intent}

    if exchange is None:
        return {"status": "BLOCKED", "reason": "MISSING_EXCHANGE_FOR_OBSERVATION", "intent_record": intent}

    submitted_event = next(
        (e for e in intent["events"] if e.get("event") == "SUBMISSION_ATTEMPTED"), None,
    )
    ack_event = next(
        (e for e in intent["events"] if e.get("event") in ("SUBMISSION_ACKNOWLEDGED", "EXECUTION_UNCERTAIN")), None,
    )
    submitted_at = (ack_event or submitted_event or intent["events"][0]).get("at")
    mexc_order_id = ack_event.get("fields", {}).get("mexc_order_id") if ack_event else None

    obs28 = _load_mexc_observed_execution_module()
    from datetime import datetime as _dt
    submitted_dt = _dt.fromisoformat(submitted_at.replace("Z", "+00:00"))
    if submitted_dt.tzinfo is None:
        submitted_dt = submitted_dt.replace(tzinfo=timezone.utc)

    submission_view = {
        "client_order_id": client_order_id,
        "symbol": intent["symbol"],
        "submitted_at": submitted_at,
        "submitted_at_dt": submitted_dt,
        "submission_status": ack_event.get("event") if ack_event else "UNKNOWN",
        "submission_mexc_order_id": mexc_order_id,
    }
    snapshot = obs28.read_observed_execution(submission_view, lookback_minutes=lookback_minutes, exchange=exchange)

    recon30 = _load_mexc_reconciliation_module()
    verdict = recon30.reconcile_intent(intent, snapshot, staleness_policy=staleness_policy, now=now_dt)
    if verdict["action"] == "BLOCKED":
        return {"status": "BLOCKED", "reason": verdict["reason"], "intent_record": intent, "snapshot": snapshot}

    updated_intent = recon30.apply_verdict(client_order_id, verdict, base_dir=ledger_base_dir)
    return {
        "status": "RECONCILED" if verdict["action"] == "RECORD_ATTEMPT" else "ESCALATED",
        "verdict": verdict,
        "snapshot": snapshot,
        "intent_record": updated_intent,
    }


# --------------------------------------------------------------------- #
# Disclosure list -- every function in this module capable of reaching a
# real network call (indirectly, through an injected client/exchange
# object it was handed -- this module constructs neither itself). Checked
# directly by a dedicated test, mirroring `.35`'s own NETWORK_CAPABLE_
# FUNCTIONS convention.
# --------------------------------------------------------------------- #
NETWORK_CAPABLE_FUNCTIONS = frozenset({
    "supervise_alpaca_equity_execution",
    "supervise_mexc_futures_execution",
    "supervise_mexc_reconciliation_pass",
})
