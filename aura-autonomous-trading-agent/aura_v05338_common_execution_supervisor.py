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
  - Order-payload digest check (added 2026-10): `order_request["order_spec"]`
    is a plain dict handed back from `.336.authorized_order_request()` with
    nothing in `.33`/`.36`/`.37` verifying it is still the same dict by the
    time `.35.submit()` is actually called -- the gap was between
    construction and submission being protected only by this function's
    own "no intervening logic" discipline, never a runtime-checked
    invariant. A SHA-256 digest of the order_spec's canonical JSON is now
    taken immediately when `.336` returns it and recomputed immediately
    before `.35.submit()`, failing closed (stage ORDER_PAYLOAD_INTEGRITY)
    on any mismatch. Scoped to Alpaca only: the MEXC path has no
    equivalent surface, since `.31.authorized_submit()` fuses claim,
    revalidation, adapter validation, and submission into one atomic call
    internal to `.31`/`.27`, so this module never holds an intermediate
    MEXC order payload to begin with.

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

Static kill switch -- Extension, 2026-10-01 (Martin, AskUserQuestion,
referencing the Lablab/Alpaca Hackathon Official Winners Audit)
------------------------------------------------------------------------
The kill switch described above (SUPERVISOR_DEFAULT_CONFIG/
supervisor_config) is a single layer: any caller can clear it simply by
passing `supervisor_config={"kill_switch": False}` as a plain function
argument -- exactly what `.363`/`.365` already do to reach real
submission. That audit found PRISM (1st place) implements a SECOND,
static layer that the dynamic layer structurally cannot clear --
`app/autonomous/control.py::set_kill_switch` refuses to deactivate the
dynamic switch while the static, config-level one is still engaged, so
clearing it requires an actual deployment/configuration change, never an
application-layer call. `.338` had no equivalent until now.

`static_kill_switch_engaged()` reads the `AURA_338_STATIC_KILL_SWITCH`
environment variable at call time (not cached, not threaded through any
config dict) and is checked FIRST in both `supervise_alpaca_equity_
execution()` and `supervise_mexc_futures_execution()` -- before `cfg =
load_supervisor_config(...)` is even evaluated, so no value any caller
supplies (via `supervisor_config` or anything else) can influence it.
Unset, or set to one of `""`/`"0"`/`"false"`/`"no"`/`"off"`
(case-insensitive) = not engaged, reproducing every existing behavior
and every existing test byte-for-byte. Set to anything else = engaged,
both venues blocked at stage SUPERVISOR_KILL_SWITCH, reason
STATIC_KILL_SWITCH_ENGAGED -- before the dynamic kill switch is even
read. Clearing it means unsetting (or explicitly zeroing) the
environment variable in the process's own environment -- not something
reachable from inside this module's own call graph, matching PRISM's
guarantee. Same scope as the dynamic kill switch: blocks only NEW
execution attempts on either venue (construction-only preview calls
included, exactly like the dynamic switch already does -- see above),
never `supervise_mexc_reconciliation_pass()` or
`observe_and_reconcile_alpaca_equity_execution()`, for the same reason
given above.

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

============================================================================
7. O9 -- Options supervisor integration (added 2026-10-07, extending this
   file in place -- no new module number, same convention `.343`/`.344`
   used for their own O8 extension)
============================================================================

Options orders had NO supervisor path at all before this addition --
every equity/crypto order already flows through the fail-closed,
PAPER-only, kill-switch-gated discipline above; options did not. This
section wires the five already-built options modules (O4 `.376`, O4B
`.377`, O5 `.378`/`.379`, O6 `.380`, O7 `.381`) and O8's enforcement hard
gate (`.344.evaluate_hypothetical_trade()`) into that SAME discipline,
never a separate, weaker path. `.376`-`.381`/`.343`/`.344` are read-only
dependencies here, exactly like every other module this file imports --
none of them is modified.

Three new entry points, mirroring the existing MEXC/Alpaca shape:
  - `supervise_options_execution()`      -- OPEN decisions (new risk).
  - `supervise_options_exit_execution()` -- O6 CLOSE decisions. A
    DEDICATED entry point, not a branch inside the open-path function,
    because its kill-switch treatment is the deliberate OPPOSITE of
    every other entry point in this file (see below).
  - `supervise_options_reconciliation_pass()` -- mirrors
    `supervise_mexc_reconciliation_pass()`'s shape, calling O4B's
    `reconcile()` instead of MEXC's `.30`.

Crash-recovery / idempotency design decision (the question explicitly
asked: does `.379`'s own claim layer already give this module enough
state on its own, or is a new ledger needed?) -- NEITHER extreme is
right, and the answer is already proven elsewhere in THIS file:
  - `.379.claim()` keys its durable claim on `authorization_id` -- but
    `.378.authorize()` mints a FRESH, random `authorization_id` on every
    call (confirmed by direct reading, exactly like `.31.authorize()`/
    `.36.authorize()` do for their own venues). So `.379` alone does
    GUARANTEE no authorization_id is ever double-consumed, but it does
    NOT by itself make a RETRY with the same `client_order_id` idempotent
    -- a second `authorize()` call for the identical spec gets a brand
    new authorization_id, which `.379` will happily claim a second time,
    which would durably construct (and, if `attempt_submission=True`,
    submit) the SAME order twice. This is the EXACT gap the Alpaca-equity
    path above already hit and already fixed with its own NEW, Supervisor-
    owned, client_order_id-keyed claim layer (`claim_alpaca_client_order_id()`
    / `ALPACA_CLIENT_ORDER_ID_RE`, section 5 above) -- NOT by inventing a
    full MEXC-style intent ledger (`.29`), which would be building far more
    machinery (lifecycle STATE: CLAIMED/SUBMISSION_ATTEMPTED/AWAITING_
    RECONCILIATION/etc., none of which the options spine has or needs
    today) than the actual gap requires.
  - A repo-wide grep for "intent_ledger"/"ledger" across every
    `aura_v0537*.py`/`aura_v0538*.py` file (done before writing any code
    here) confirms no options-specific intent ledger exists anywhere.
  - The SIMPLEST correct fix, already proven in this exact file for the
    structurally identical Alpaca-equity problem, is reused again here:
    `claim_options_client_order_id()` / `OPTIONS_CLIENT_ORDER_ID_RE`
    below -- a second, Supervisor-owned, atomic O_CREAT|O_EXCL claim
    keyed by `client_order_id` (never released, same discipline as every
    other claim store in this codebase). This is sufficient: calling
    `supervise_options_execution()` twice with the same `client_order_id`
    claims a NEW `.379` authorization_id each time (harmless -- `.379`
    just has two valid-but-unused claims on file) but the SECOND call's
    own `claim_options_client_order_id()` call is refused, so `.376.submit()`
    is reached at most once per `client_order_id`, exactly the guarantee
    actually needed. No new intent ledger is built; none is needed.
  - Residual, DISCLOSED limitation (identical in kind to the one already
    accepted for the Alpaca-equity path above, not a new problem
    introduced here): if a crash happens in the narrow window AFTER
    `claim_options_client_order_id()` grants but BEFORE `.376.submit()`
    actually runs, that `client_order_id` is permanently claimed with no
    real order ever having been submitted. This is an accepted
    consequence of the "never release a claim" design used by every
    claim store in this codebase (`.27`, `.29`, `.32`, `.37`, `.379`,
    and this module's own two local claim layers) -- trading a
    vanishingly narrow crash window for a strong, simple anti-double-
    submission guarantee. Not fixed here, same as it is not fixed for
    Alpaca equities today.

Construction-only preview (`attempt_submission=False`) -- the MEXC
lesson, not the Alpaca one. `.378.authorized_order_request()` FUSES
`.379`'s claim with order construction into ONE atomic call, exactly
like `.31.authorized_submit()` does for MEXC (NOT like `.36.
authorized_order_request()`/`.37.claim()` for Alpaca equities, which are
claimed unconditionally even during a preview). So
`supervise_options_execution()`/`supervise_options_exit_execution()`
follow the MEXC precedent here, not the Alpaca one: a preview calls
`.378.authorize()` ONLY and stops -- it never calls `authorized_order_
request()`, so it never touches `.379`'s claim store or this module's
own `claim_options_client_order_id()` claim store either. Calling the
fused claim+construct call during a preview would durably burn a `.379`
claim (and this module's own client_order_id claim) for a submission
that never really happened -- the identical "earlier draft bug" already
found and fixed for MEXC above, deliberately not repeated here.

O4B (`.377`) as a HARD GATE (Martin's explicit decision) -- `.377` itself
is DETECTION ONLY (its own docstring: "enforcement of a freeze decision
... is O9's job ... not yet built"). `supervise_options_execution()` is
that enforcement: it calls `.377.reconcile()` fresh on every call (no
caching, matching `.377`'s own "never fabricate or reuse stale evidence"
principle) and FAILS CLOSED, before ever calling `.378.authorize()`, if
the proposed trade's underlying is in `.377`'s own `frozen_underlyings`
list. The caller must supply a real (in production) or faked (every test
here) read-only Alpaca client plus AURA's own expected-positions list and
a `lookback_days` -- all REQUIRED (no default; `.377`'s own `lookback_days`
has none either, and this module does not invent one). Missing any of
them fails closed rather than silently skipping the freeze check.
`supervise_options_exit_execution()` deliberately does NOT re-check this
gate -- Martin's decision scopes the freeze hard-gate to NEW (open)
decisions only; closing an existing position is never blocked by it.

O8 (`.344.evaluate_hypothetical_trade()`) as a second HARD GATE -- called
after the freeze gate and the dynamic kill-switch check, before
`.378.authorize()`, exactly matching `.344`'s own description of itself
as an "authorization-time gate." Every input it needs
(`portfolio_snapshot`/`portfolio_hypothetical`/`portfolio_limits`/
`portfolio_max_snapshot_age_seconds`) is REQUIRED and caller-supplied --
this module never builds a `.343` PortfolioSnapshot or a Greeks-bearing
PositionRecord itself, and never computes a Greek anywhere (per Martin's
explicit instruction: O8 is a pure function of caller-supplied exposure
data, never a Greeks source). An `overall_verdict == "BLOCK"` fails
closed before authorization, exactly like the static/dynamic kill
switch; the full `EnforcementDecision` is attached to the result either
way (ALLOW or BLOCK) for audit. `supervise_options_exit_execution()`
deliberately does NOT call this gate either -- reducing/closing existing
risk is never blocked by a portfolio-exposure limit designed to stop NEW
risk from being taken on.

O7 (`.381.estimate_round_trip_cost()`) as INFORMATIONAL ONLY -- computed
(when `cost_model_config`/`cost_model_leg_quotes` are supplied) and
attached to the result as `cost_estimate` for audit/visibility, but its
outcome can NEVER produce a BLOCKED result on its own -- `.381` has no
configured limit to breach, it is pure arithmetic (its own docstring:
"estimation function"). Any exception raised while computing it is
caught and recorded in `cost_estimate` itself, never propagated and never
treated as a block reason.

O6 (`.380`) kill-switch INVERSION, extended here for the first time to an
actual execution path -- `supervise_options_exit_execution()` DOES read
the Supervisor's own kill switch (both static and dynamic), the one
place in this entire function it is read, but it is never used to
produce a BLOCKED result. It is used ONLY to compute the
`kill_switch_engaged` argument handed to `.380.evaluate_exit()` --
`forced_close = bool(cfg["kill_switch"]) or static_kill_switch_engaged()`
-- whose OWN inversion (True FORCES an emergency close, per its own
docstring and Martin's explicit confirmation: "a kill switch must never
accidentally block the exits it triggers") then does the actual work:
with `kill_switch_engaged=True`, KILL_SWITCH is `.380`'s own highest-
priority trigger, evaluated unconditionally, so `evaluate_exit()` always
returns `action="CLOSE"`. From that point on, this function applies NO
further kill-switch check of any kind before authorizing/claiming/
submitting the close. This is a DELIBERATE, EXPLICITLY-CONFIRMED
departure from this module's own existing language in section 5 above
("a CLOSE_LONG/CLOSE_SHORT decision is itself a new execution attempt
and is blocked by the kill switch exactly like an OPEN, consistent with
every other module in this chain") -- that sentence described every
module BEFORE `.380` existed. When the Supervisor's kill switch is OFF,
`.380` evaluates normally and may just as well return HOLD (handled by
stopping immediately, before any authorization/claim/construction is
even attempted) -- the inversion only ever matters when the switch is
actually engaged. This is flagged here explicitly, not silently done,
because it changes this file's own stated kill-switch philosophy for
exactly one, options-specific, closing-only path.

No audit-trail (`.340`) integration for any options entry point --
`.378`'s own docstring explicitly defers this "most naturally O9,"
confirming it is a known, disclosed gap rather than an oversight; wiring
options into the SAME shared `.340` trail used by equity/MEXC without
first confirming that trail is genuinely venue-agnostic is a real risk of
silently corrupting or conflating it, not something to guess at here. No
live chain/quote recheck at the options supervisor layer either -- `.378`
itself already made this same call for its own layer (module docstring
item 2) and this module does not second-guess it. No modification to
`.343`/`.344`/`.373`-`.381` -- all read-only, dynamically imported,
called only through their existing public functions.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

VERSION = "AURA v0.5.3.38"
ENGINE = "COMMON_EXECUTION_SUPERVISOR"
SCHEMA_VERSION = "1.0"

ROOT = Path(__file__).resolve().parent

# LIVE is deliberately NOT a member -- see module docstring §5.
ALLOWED_ENVIRONMENTS = frozenset({"PAPER"})

SUPERVISOR_DEFAULT_CONFIG: dict[str, Any] = {"kill_switch": True}

# Static, deployment-level kill switch -- see module docstring, "Static
# kill switch" section. Independent of SUPERVISOR_DEFAULT_CONFIG/
# supervisor_config: no caller-supplied dict can clear this, only a
# change to the process's own environment can.
STATIC_KILL_SWITCH_ENV_VAR = "AURA_338_STATIC_KILL_SWITCH"
_STATIC_KILL_SWITCH_OFF_VALUES = frozenset({"", "0", "false", "no", "off"})

DEFAULT_MEXC_AUTH_CLAIMS_DIR = Path("regime_output/common_execution_supervisor/mexc_authorization_claims")
DEFAULT_MEXC_ADAPTER_CLAIMS_DIR = Path("regime_output/common_execution_supervisor/mexc_adapter_claims")
DEFAULT_MEXC_LEDGER_DIR = Path("regime_output/common_execution_supervisor/mexc_intent_ledger")
DEFAULT_ALPACA_AUTH_CLAIMS_DIR = Path("regime_output/common_execution_supervisor/alpaca_authorization_claims")
DEFAULT_ALPACA_CLIENT_ORDER_ID_CLAIMS_DIR = Path("regime_output/common_execution_supervisor/alpaca_client_order_id_claims")

# O9 options dependencies -- see module docstring §7.
DEFAULT_OPTIONS_AUTH_CLAIMS_DIR = Path("regime_output/common_execution_supervisor/options_authorization_claims")
DEFAULT_OPTIONS_CLIENT_ORDER_ID_CLAIMS_DIR = Path("regime_output/common_execution_supervisor/options_client_order_id_claims")


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


def static_kill_switch_engaged() -> bool:
    """The static, deployment-level kill switch -- see module docstring,
    "Static kill switch". Read from the environment at call time (never
    cached, never threaded through `supervisor_config` or any other
    caller-supplied argument). Unset, or set to one of the explicit "off"
    spellings in `_STATIC_KILL_SWITCH_OFF_VALUES` (case-insensitive,
    surrounding whitespace ignored), returns False -- every existing
    behavior and every existing test is byte-for-byte unchanged. Set to
    anything else returns True, and no value any caller passes anywhere
    in this module's public functions can turn that back to False."""
    value = os.environ.get(STATIC_KILL_SWITCH_ENV_VAR)
    if value is None:
        return False
    return value.strip().lower() not in _STATIC_KILL_SWITCH_OFF_VALUES


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


def _load_revalidation_module():
    return _load_module("aura_v05340_pre_submission_revalidation", "aura_v05340_pre_submission_revalidation.py")


# --- O9 options dependencies (added 2026-10-07 -- see module docstring §7) ---

def _load_options_adapter_module():
    return _load_module("aura_v05376_options_execution_adapter", "aura_v05376_options_execution_adapter.py")


def _load_options_reconciliation_module():
    return _load_module("aura_v05377_options_assignment_reconciliation", "aura_v05377_options_assignment_reconciliation.py")


def _load_options_authorization_module():
    return _load_module("aura_v05378_options_execution_authorization", "aura_v05378_options_execution_authorization.py")


def _load_options_exit_engine_module():
    return _load_module("aura_v05380_options_exit_engine", "aura_v05380_options_exit_engine.py")


def _load_options_cost_model_module():
    return _load_module("aura_v05381_options_cost_model", "aura_v05381_options_cost_model.py")


def _load_portfolio_enforcement_module():
    return _load_module("aura_v05344_portfolio_exposure_enforcement", "aura_v05344_portfolio_exposure_enforcement.py")


def _audit_write_best_effort(revalidation40: Any, fn_name: str, *args: Any, **kwargs: Any) -> None:
    """v0.5.3.40 audit-trail writes are best-effort, never safety-
    relevant -- same helper, same rationale, as .31's/.36's own
    _audit_write_best_effort() (Martin's explicit .40 completion-report
    decision: "best-effort audit writes"). This module's own two direct
    .40 write call sites (Alpaca SUBMISSION_ATTEMPTED/OUTCOME -- the only
    events .36 itself never writes, since .36 never calls .35.submit())
    are structurally protected from the concurrent-race case by the
    never-released Supervisor-level client_order_id claim above them, but
    are wrapped here anyway for defense-in-depth and consistency with
    every other .40 write call site in this codebase. Swallows ONLY
    IllegalTransitionError, never AuditTrailError."""
    fn = getattr(revalidation40, fn_name)
    try:
        fn(*args, **kwargs)
    except revalidation40.IllegalTransitionError:
        pass


def _record_decision_idempotent(revalidation40: Any, client_order_id: str, *, venue: str, asset_class: str,
                                 symbol: str, spec_fingerprint: str, base_dir: Path) -> dict[str, Any]:
    """Wraps `.40.record_decision()` with the SAME crash-retry discipline
    already established for `.29.create_intent()` (module docstring §4):
    attempt the create optimistically, and on AUDIT_RECORD_ALREADY_EXISTS
    (this Supervisor re-entering for a client_order_id whose DECISION
    event was already durably recorded on an earlier attempt -- there is
    no `.29`-equivalent ledger on the Alpaca side to short-circuit on
    first, so this module itself is the only thing that can tell a crash
    retry apart from a genuine new decision), fall back to reading the
    existing record instead of treating it as an error. A caller passing
    a spec_fingerprint that disagrees with an existing record's own
    DECISION event is not specially detected here -- that mismatch would
    mean two DIFFERENT canonical specs collided on the same
    client_order_id, which `.33`'s own deterministic-fingerprint
    construction (decision_id/symbol/direction/signal_timestamp) is
    designed to make structurally unreachable in the first place."""
    try:
        return revalidation40.record_decision(
            client_order_id, venue=venue, asset_class=asset_class, symbol=symbol,
            spec_fingerprint=spec_fingerprint, base_dir=base_dir,
        )
    except revalidation40.AuditTrailError as exc:
        if "AUDIT_RECORD_ALREADY_EXISTS" not in str(exc):
            raise
        existing = revalidation40.get_record(client_order_id, base_dir=base_dir)
        if existing is None:
            raise
        return existing


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
        "order_spec_digest": None,
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


# O9 options local claim layer -- see module docstring §7's crash-
# recovery/idempotency discussion for why this exists, and why it is a
# second, DISTINCT claim store from the one just above (a separate
# claims directory, keyed the same way, for a different venue/asset
# class) rather than the Alpaca-equity one being reused directly.
OPTIONS_CLIENT_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def claim_options_client_order_id(claims_dir: Path, client_order_id: str) -> bool:
    """Atomic, single-use claim on an options MLEG order's client_order_id,
    owned by the Supervisor, separate from `.379`'s authorization_id
    store. Returns True if THIS call granted the claim, False if already
    claimed. Never released, under any outcome -- there is deliberately
    no release/delete function anywhere in this module (checked by a
    dedicated structural test, mirroring `claim_alpaca_client_order_id()`'s
    own identical property)."""
    if not isinstance(client_order_id, str) or not OPTIONS_CLIENT_ORDER_ID_RE.match(client_order_id):
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
    price_fetch_fn: Any = None,
    max_quote_age_seconds: float | None = None,
    max_price_drift_bps: float | None = None,
    max_revalidation_to_submission_seconds: float | None = None,
    audit_dir: Path | None = None,
) -> dict[str, Any]:
    """The single entry point for a NEW Alpaca STOCK/ETF execution
    decision. Sequence: canonical spec self-consistency -> venue/asset-
    class gate -> (opt-in v0.5.3.40) unified audit-trail DECISION event,
    written only once the spec is confirmed to genuinely be routed as an
    Alpaca equity spec -> `.34` instrument-metadata resolution ->
    Supervisor kill switch ->
    `.36.authorize()` -> `.36.authorized_order_request()` (which itself
    revalidates -- including, when `price_fetch_fn` is supplied, a live
    v0.5.3.40 price check -- then claims via `.37` and constructs, never
    submits) -> this module's OWN local client_order_id claim -> (opt-in
    v0.5.3.40) the revalidation-to-submission ceiling check -> (only
    then, with NO intervening logic) `.35.submit()`, wrapped to classify
    any exception as EXECUTION_UNCERTAIN (module docstring §2). Never
    bypasses `.33`/authorization/replay protection/adapter validation --
    there is no earlier return path that reaches `order_spec`/submission
    without every one of those succeeding first (module docstring §6, "no
    bypass path").

    price_fetch_fn/max_quote_age_seconds/max_price_drift_bps/
    max_revalidation_to_submission_seconds/audit_dir are ALL additive and
    opt-in (v0.5.3.40): every one defaults to None, and when every one is
    None this function's behavior is byte-for-byte unchanged from before
    v0.5.3.40. `.36` itself never performs the ceiling check and never
    writes SUBMISSION_ATTEMPTED/OUTCOME (its own docstring says so
    explicitly) because `.36` never calls `.35.submit()` -- THIS function
    does, so THIS function is where those two responsibilities live for
    the Alpaca path, mirroring `.31.authorized_submit()`'s identical
    responsibilities on the MEXC path one layer down."""
    if static_kill_switch_engaged():
        return _blocked(
            canonical_spec if isinstance(canonical_spec, dict) else None,
            "SUPERVISOR_KILL_SWITCH", "STATIC_KILL_SWITCH_ENGAGED",
            f"{STATIC_KILL_SWITCH_ENV_VAR} is set in the process environment -- this overrides any "
            f"supervisor_config value and cannot be cleared by any caller; unset it in the deployment "
            f"environment to resume.",
        )
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

    # v0.5.3.40: the DECISION audit event is written only once the spec is
    # confirmed to genuinely be an ALPACA/STOCK|ETF spec -- recording it
    # any earlier (e.g. before the venue/asset-class gate above) would
    # risk writing a DECISION event with a hardcoded venue="ALPACA" for a
    # spec that was never actually routed as one.
    revalidation40 = _load_revalidation_module() if audit_dir is not None else None
    if revalidation40 is not None:
        _record_decision_idempotent(
            revalidation40, canonical_spec["client_order_id"],
            venue="ALPACA", asset_class=canonical_spec["asset_class"], symbol=canonical_spec["symbol"],
            spec_fingerprint=canonical_spec["spec_fingerprint"], base_dir=audit_dir,
        )

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
        claims_dir=auth_claims_dir, now=now_dt, audit_dir=audit_dir,
    )
    if record.get("status") != "AUTHORIZED":
        return _blocked(canonical_spec, "AUTHORIZATION", record.get("reason"), record.get("detail"))

    order_request = auth36.authorized_order_request(
        record, canonical_spec, alpaca_asset, auth_claims_dir, environment=environment,
        config=auth_config, now=now_dt,
        price_fetch_fn=price_fetch_fn, max_quote_age_seconds=max_quote_age_seconds,
        max_price_drift_bps=max_price_drift_bps, audit_dir=audit_dir,
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

    # Order-payload digest (v0.5.3.?) -- a SHA-256 of the canonical JSON of
    # the exact broker-bound order_spec `.336` just constructed, taken at
    # the first possible moment (`.336` never computes one itself: it
    # returns a plain dict). This is NOT a replacement for `.33`'s
    # spec_fingerprint (which binds the DECISION) or `.37`'s
    # authorization-id claim (which binds the AUTHORIZATION) -- neither of
    # those covers the one thing nothing else in this chain checks:
    # whether the literal dict handed to `.35.submit()` is still
    # byte-identical to the one `.336` returned. Today that's true only by
    # the "no intervening logic" discipline documented further down; this
    # digest makes it a runtime-checked invariant instead of a
    # code-review-only one, so a future edit that accidentally mutates
    # `order_spec` between construction and submission fails closed
    # instead of silently submitting a drifted order. See the matching
    # recheck immediately before the `.35.submit()` call below.
    order_spec_digest = sha256_text(stable_json(order_request["order_spec"]))

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
    result["order_spec_digest"] = order_spec_digest

    if not attempt_submission:
        result["status"] = "READY_FOR_SUBMISSION"
        result["stage"] = "CONSTRUCTION_ONLY"
        return result

    if alpaca_client is None:
        return _blocked(canonical_spec, "SUBMISSION", "MISSING_ALPACA_CLIENT",
                         "attempt_submission=True requires an already-constructed alpaca_client; "
                         "this module never constructs one itself",
                         authorization_id=record.get("authorization_id"), order_spec=order_request["order_spec"])

    # v0.5.3.40 ceiling check -- fail-closed backstop for the residual
    # claim-to-submit race, only meaningful when a price check actually
    # happened above (order_request["price_check"] is only present when
    # `price_fetch_fn` was supplied). BOTH claims already granted above
    # (`.37`'s authorization_id claim inside .36, and this module's own
    # local client_order_id claim just above) stay consumed regardless of
    # this check's outcome -- never released, exactly mirroring `.31.
    # authorized_submit()`'s identical MEXC-side backstop.
    price_check = order_request.get("price_check")
    if price_check is not None and revalidation40 is not None:
        kwargs: dict[str, Any] = {}
        if max_revalidation_to_submission_seconds is not None:
            kwargs["max_seconds"] = max_revalidation_to_submission_seconds
        try:
            revalidation40.check_revalidation_to_submission_ceiling(
                price_checked_at=price_check["price_checked_at"], now_dt=now_dt, **kwargs
            )
        except revalidation40.RevalidationRejected as exc:
            return _blocked(canonical_spec, "SUBMISSION_CEILING", exc.reason, exc.detail,
                             authorization_id=record.get("authorization_id"), order_spec=order_request["order_spec"])

    # NO intervening logic between this point and the actual .35.submit()
    # call below, beyond the digest recheck immediately following -- the
    # ceiling check above and this digest check are themselves part of
    # what "tightening the claim-to-submit gap" means (fail-closed checks,
    # not additional work that could itself go stale), not a violation of
    # it.
    #
    # Order-payload digest recheck -- the other half of the invariant
    # introduced above the local claim layer: recompute the same digest
    # from the exact object about to be passed to `.35.submit()` and
    # require an exact match. Always on, unlike the price/ceiling checks
    # -- this has no external dependency (no network, no clock), it is a
    # hash of a dict already in memory, so there is no cost or reason to
    # make it opt-in. A mismatch means `order_spec` was mutated somewhere
    # between construction and this point; there is no way to tell from
    # here whether that was a bug or something adversarial, so it is
    # treated exactly like any other fail-closed rejection: BLOCKED, no
    # broker call, and both claims above stay consumed (never released),
    # exactly like every other post-claim rejection in this function.
    resubmission_digest = sha256_text(stable_json(order_request["order_spec"]))
    if resubmission_digest != order_spec_digest:
        return _blocked(
            canonical_spec, "ORDER_PAYLOAD_INTEGRITY", "ORDER_PAYLOAD_DIGEST_MISMATCH",
            "order_spec changed between authorization and submission -- refusing to submit",
            authorization_id=record.get("authorization_id"), order_spec=order_request["order_spec"],
            order_spec_digest=order_spec_digest, resubmission_digest=resubmission_digest,
        )

    if revalidation40 is not None:
        _audit_write_best_effort(revalidation40, "record_submission_attempted",
                                  canonical_spec["client_order_id"], base_dir=audit_dir)

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

    if revalidation40 is not None:
        _audit_write_best_effort(
            revalidation40, "record_outcome", canonical_spec["client_order_id"],
            outcome=submission_result.get("status", "UNKNOWN"),
            detail=submission_result.get("error"), base_dir=audit_dir,
        )

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
    price_fetch_fn: Any = None,
    max_quote_age_seconds: float | None = None,
    max_price_drift_bps: float | None = None,
    max_revalidation_to_submission_seconds: float | None = None,
    audit_dir: Path | None = None,
) -> dict[str, Any]:
    """The single entry point for a NEW MEXC CRYPTO_FUTURES execution
    decision. Sequence: canonical spec self-consistency -> venue/asset-
    class gate -> (opt-in v0.5.3.40) unified audit-trail DECISION event ->
    `.34` instrument-metadata resolution -> Supervisor kill switch ->
    crash-recovery check via `.29.get_intent()` -> (new decision
    only) `.29.create_intent()` -> `.33.to_mexc_execution_spec()` ->
    `.31.authorize()` -> IF `attempt_submission` (else stop here, nothing
    claimed anywhere -- see the inline comment above this function's
    `.31.authorized_submit()` call) -> `.31.authorized_submit()` (revalidates
    -- including, when `price_fetch_fn` is supplied, a live v0.5.3.40 price
    check and its own ceiling-check backstop -- claims via `.32`,
    adapter-validates, calls `.27.submit()`, and writes its OWN
    SUBMISSION_ATTEMPTED/OUTCOME audit events since `.31`, unlike `.36`, is
    the thing that actually calls the venue adapter's submit function) ->
    outcome mapped onto `.29`'s ledger per module docstring §3.

    `exchange` must already be constructed by the caller (real in a
    hypothetical production deployment, a fake test double in every test
    here) and is REQUIRED when `attempt_submission=True` -- this module
    never builds one itself and never reads MEXC credentials (module
    docstring §5). `.31.authorized_submit()` (and therefore `.27.submit()`,
    which claims client_order_id unconditionally before touching any
    exchange) is called ONLY when `attempt_submission=True` -- never as a
    "dry run" with a stand-in exchange, because that would durably consume
    both replay-protection claims for a submission that never really
    happened.

    price_fetch_fn/max_quote_age_seconds/max_price_drift_bps/
    max_revalidation_to_submission_seconds/audit_dir are ALL additive and
    opt-in (v0.5.3.40), passed straight through to `.31.authorize()`/
    `.31.authorized_submit()`, which already implement the entire
    revalidate -> claim -> submit reordering and ceiling-check backstop
    themselves (unlike the Alpaca path, where this module owns those
    responsibilities because `.36` never submits -- see
    `supervise_alpaca_equity_execution()`'s docstring). Every one defaults
    to None, and when every one is None this function's behavior is
    byte-for-byte unchanged from before v0.5.3.40."""
    if static_kill_switch_engaged():
        return _blocked(
            canonical_spec if isinstance(canonical_spec, dict) else None,
            "SUPERVISOR_KILL_SWITCH", "STATIC_KILL_SWITCH_ENGAGED",
            f"{STATIC_KILL_SWITCH_ENV_VAR} is set in the process environment -- this overrides any "
            f"supervisor_config value and cannot be cleared by any caller; unset it in the deployment "
            f"environment to resume.",
        )
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

    revalidation40 = _load_revalidation_module() if audit_dir is not None else None
    if revalidation40 is not None:
        _record_decision_idempotent(
            revalidation40, canonical_spec["client_order_id"],
            venue="MEXC", asset_class=canonical_spec["asset_class"], symbol=canonical_spec["symbol"],
            spec_fingerprint=canonical_spec["spec_fingerprint"], base_dir=audit_dir,
        )

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
                               claims_dir=auth_claims_dir, now=now_dt, audit_dir=audit_dir)
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
        price_fetch_fn=price_fetch_fn, max_quote_age_seconds=max_quote_age_seconds,
        max_price_drift_bps=max_price_drift_bps,
        max_revalidation_to_submission_seconds=max_revalidation_to_submission_seconds,
        audit_dir=audit_dir,
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

    # v0.5.3.40 FIX (found during this module's own re-verification, not
    # present in the original .38 milestone): since .31's v0.5.3.40
    # revalidate -> claim -> submit reordering, a "REVALIDATION_FAILED"
    # status is ambiguous on its own -- it now covers BOTH a pre-claim
    # rejection (nothing was ever claimed) AND a post-claim ceiling-check
    # rejection (a claim WAS granted moments earlier). The OLD code here
    # unconditionally assumed "anything other than
    # AUTHORIZATION_ALREADY_CONSUMED means the claim was granted" --
    # true under the pre-v0.5.3.40 claim -> revalidate -> submit order,
    # but no longer true. `.31.authorized_submit()` now returns an
    # explicit `claim_granted` boolean (added alongside this fix) that
    # this module consults directly instead of inferring it from
    # `status`, so a pre-claim REVALIDATION_FAILED correctly leaves the
    # `.29` intent untouched (still NEW, still retryable) instead of
    # incorrectly recording a claim -> escalating an intent that was
    # never actually claimed. See `.31.authorized_submit()`'s own
    # docstring for the full explanation of why this field exists.
    if not submission.get("claim_granted"):
        result["status"] = "BLOCKED"
        result["stage"] = "REVALIDATION"
        result["reason"] = submission.get("reason")
        result["detail"] = submission.get("detail")
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


# ======================================================================= #
# O9 -- Options (Alpaca 2-leg vertical spread) supervision. Added
# 2026-10-07, extending this file in place. See module docstring §7 for
# the full design rationale (crash-recovery decision, kill-switch
# inversion for the exit path, O4B/O8 hard gates, O7 informational-only
# cost estimate).
# ======================================================================= #

def _options_pseudo_spec(underlying_symbol: str | None, client_order_id: str | None,
                          direction: str | None = None) -> dict[str, Any]:
    """Options has no `.33`-equivalent canonical-spec layer (`.378`'s own
    module docstring item 0), so there is no real canonical_spec dict to
    hand `_base_result()`/`_blocked()` the way the MEXC/Alpaca-equity
    paths do. This builds the minimal dict those two SHARED helpers
    actually read (`venue`/`asset_class`/`symbol`/`direction`/
    `client_order_id`/`spec_fingerprint`) so this module's options paths
    can reuse them unchanged, exactly like every other result shape in
    this file."""
    return {
        "venue": "ALPACA", "asset_class": "OPTION", "symbol": underlying_symbol,
        "direction": direction, "client_order_id": client_order_id, "spec_fingerprint": None,
    }


def _options_authorize(spec: dict[str, Any], *, environment: str, auth_config: dict[str, Any] | None,
                        auth_claims_dir: Path, now_dt: datetime | None) -> tuple[tuple[str, str, Any] | None, dict[str, Any]]:
    """Shared `.378.authorize()` call, used by BOTH
    `supervise_options_execution()` (OPEN) and
    `supervise_options_exit_execution()` (CLOSE) -- identical sequence in
    both. Returns `(blocked, record)`: `blocked` is `None` on success, or
    `(stage, reason, detail)` when `record["status"] != "AUTHORIZED"`.
    Deliberately stops here and does NOT call `.378.authorized_order_
    request()` -- that is the caller's job, and only when
    `attempt_submission=True` (module docstring §7's construction-only-
    preview discussion: this function alone touches nothing durable)."""
    auth78 = _load_options_authorization_module()
    record = auth78.authorize(spec, environment=environment, config=auth_config,
                               claims_dir=auth_claims_dir, now=now_dt)
    if record.get("status") != "AUTHORIZED":
        return ("AUTHORIZATION", record.get("reason"), record.get("detail")), record
    return None, record


def _options_claim_and_submit(record: dict[str, Any], spec: dict[str, Any], *, environment: str,
                               auth_config: dict[str, Any] | None, auth_claims_dir: Path,
                               supervisor_claims_dir: Path, alpaca_client: Any,
                               now_dt: datetime | None) -> dict[str, Any]:
    """Shared tail, used by BOTH options entry points, called ONLY when
    `attempt_submission=True` (never during a construction-only preview
    -- see module docstring §7). Sequence: `.378.authorized_order_
    request()` (revalidate -> `.379` claim -> `.376` pure construction)
    -> this module's OWN local client_order_id claim (second layer,
    mirrors `claim_alpaca_client_order_id()`'s role for Alpaca equities,
    module docstring §7's crash-recovery discussion) -> order-payload
    digest check (same invariant `supervise_alpaca_equity_execution()`
    already enforces, reused here for the identical reason) -> `.376.
    submit()`, wrapped exactly like `.35.submit()` is wrapped above
    (module docstring §2's EXECUTION_UNCERTAIN discipline -- `.376.
    submit()` has the identical gap: no exception classification of its
    own around `client.submit_order()`).

    Returns a dict with `"blocked": True` plus `stage`/`reason`/`detail`
    (and any extra fields such as `order_spec`/`existing_claim`) on any
    rejection, or `"blocked": False` plus `order_spec`/`order_spec_digest`/
    `submission_result` on completion. Extracted as a SHARED helper
    (unlike the MEXC/Alpaca-equity split elsewhere in this file, which is
    deliberately NOT shared because those two venues' plumbing genuinely
    differs) because the open and exit options paths call this exact
    `.378`/`.379`/`.376` sequence byte-for-byte -- sharing it removes the
    risk of a future safety fix landing in only one copy."""
    auth78 = _load_options_authorization_module()
    order_request = auth78.authorized_order_request(
        record, spec, auth_claims_dir, environment=environment, config=auth_config, now=now_dt,
    )
    if order_request["status"] == "AUTHORIZATION_ALREADY_CONSUMED":
        return {
            "blocked": True, "stage": "REPLAY_PROTECTION", "reason": order_request.get("reason"),
            "detail": order_request.get("detail"),
            "existing_client_order_id": order_request.get("existing_client_order_id"),
            "existing_claim": order_request.get("existing_claim"),
        }
    if order_request["status"] == "CLAIM_STORE_UNREACHABLE":
        return {"blocked": True, "stage": "REPLAY_PROTECTION", "reason": order_request.get("reason"),
                "detail": order_request.get("detail")}
    if order_request["status"] != "AUTHORIZED_ORDER_REQUEST_READY":
        return {"blocked": True, "stage": "REVALIDATION", "reason": order_request.get("reason"),
                "detail": order_request.get("detail")}

    # Order-payload digest -- identical rationale to the one documented at
    # length above `supervise_alpaca_equity_execution()`'s own call site:
    # a SHA-256 of the canonical JSON of the exact dict `.378` just
    # handed back, taken now and rechecked immediately before `.376.
    # submit()`, so a future edit that mutates `order_spec` in between
    # fails closed instead of silently submitting a drifted order.
    order_spec = order_request["order_spec"]
    order_spec_digest = sha256_text(stable_json(order_spec))

    # Second, local claim layer -- see claim_options_client_order_id()'s
    # own docstring and module docstring §7. `.379`'s authorization_id
    # claim is already permanently consumed at this point and stays that
    # way regardless of what happens below (never released).
    granted = claim_options_client_order_id(supervisor_claims_dir, order_spec["client_order_id"])
    if not granted:
        return {
            "blocked": True, "stage": "SUPERVISOR_CLIENT_ORDER_ID_CLAIM",
            "reason": "CLIENT_ORDER_ID_ALREADY_CLAIMED_AT_SUPERVISOR_LAYER",
            "detail": ("authorization_id claim succeeded but this client_order_id was already claimed by a "
                        "different authorization attempt -- a genuine anomaly, not an ordinary replay"),
            "status_override": "AUTHORIZED_BUT_SUPERVISOR_CLAIM_REJECTED",
            "order_spec": order_spec, "order_spec_digest": order_spec_digest,
        }

    if alpaca_client is None:
        return {
            "blocked": True, "stage": "SUBMISSION", "reason": "MISSING_ALPACA_CLIENT",
            "detail": ("attempt_submission=True requires an already-constructed alpaca_client; "
                        "this module never constructs one itself"),
            "order_spec": order_spec, "order_spec_digest": order_spec_digest,
        }

    # NO intervening logic between this point and the actual .376.submit()
    # call below, beyond the digest recheck immediately following -- same
    # discipline as supervise_alpaca_equity_execution()'s identical
    # comment above its own submit() call.
    resubmission_digest = sha256_text(stable_json(order_spec))
    if resubmission_digest != order_spec_digest:
        return {
            "blocked": True, "stage": "ORDER_PAYLOAD_INTEGRITY", "reason": "ORDER_PAYLOAD_DIGEST_MISMATCH",
            "detail": "order_spec changed between authorization and submission -- refusing to submit",
            "order_spec": order_spec, "order_spec_digest": order_spec_digest,
            "resubmission_digest": resubmission_digest,
        }

    adapter76 = _load_options_adapter_module()
    try:
        submission_result = adapter76.submit(order_spec, alpaca_client)
    except Exception as exc:  # noqa: BLE001 -- see module docstring §2: `.376.submit()` has no
        # exception classification of its own, the identical gap `.35.submit()` has.
        submission_result = {
            "adapter_version": adapter76.VERSION,
            "status": "EXECUTION_UNCERTAIN",
            "client_order_id": order_spec["client_order_id"],
            "observed_at": now(),
            "paper": True,
            "live": False,
            "error": f"{type(exc).__name__}: {exc}",
        }

    return {
        "blocked": False, "order_spec": order_spec, "order_spec_digest": order_spec_digest,
        "submission_result": submission_result,
    }


def supervise_options_execution(
    execution_spec: dict[str, Any],
    *,
    environment: str = "PAPER",
    auth_config: dict[str, Any] | None = None,
    auth_claims_dir: Path | None = None,
    supervisor_claims_dir: Path | None = None,
    expected_options_positions: list[dict[str, Any]] | None = None,
    reconciliation_client: Any = None,
    reconciliation_lookback_days: int | None = None,
    portfolio_snapshot: Any = None,
    portfolio_hypothetical: Any = None,
    portfolio_limits: Any = None,
    portfolio_max_snapshot_age_seconds: float | None = None,
    portfolio_equity_history: list[dict[str, Any]] | None = None,
    cost_model_config: dict[str, Any] | None = None,
    cost_model_leg_quotes: dict[str, Any] | None = None,
    attempt_submission: bool = False,
    alpaca_client: Any = None,
    supervisor_config: dict[str, Any] | None = None,
    now_dt: datetime | None = None,
) -> dict[str, Any]:
    """The single entry point for a NEW options (2-leg Alpaca vertical
    spread) execution decision -- see module docstring §7. Sequence:
    static kill switch -> `execution_spec` type check -> environment gate
    -> `.376.validate_vertical_spec()` (fresh, independent structural
    check, giving this function `underlying_symbol`/`client_order_id`
    before anything else can run) -> O4B (`.377`) assignment/exercise
    freeze HARD GATE -> Supervisor's own dynamic kill switch -> O8
    (`.344.evaluate_hypothetical_trade()`) portfolio-exposure HARD GATE ->
    O7 (`.381.estimate_round_trip_cost()`) informational-only cost
    estimate -> `.378.authorize()` -> (only if `attempt_submission=True`)
    `.378.authorized_order_request()` (claim + construct) -> this
    module's own client_order_id claim -> `.376.submit()`.

    `expected_options_positions`/`reconciliation_client`/
    `reconciliation_lookback_days` and `portfolio_snapshot`/
    `portfolio_hypothetical`/`portfolio_limits`/
    `portfolio_max_snapshot_age_seconds` are ALL required (default `None`
    only so a missing one produces a clearly-named BLOCKED reason instead
    of a bare TypeError, matching this file's existing `exchange is None`/
    `alpaca_client is None` convention) -- both hard gates run on EVERY
    call, preview or real submission alike (module docstring §7: a
    preview must still prove the spec passes every gate, not just that
    it is structurally well-formed). `cost_model_config`/
    `cost_model_leg_quotes` are genuinely optional -- omitting them simply
    means `cost_estimate` comes back `None` (O7 is informational only;
    see module docstring §7)."""
    if static_kill_switch_engaged():
        return _blocked(
            None, "SUPERVISOR_KILL_SWITCH", "STATIC_KILL_SWITCH_ENGAGED",
            f"{STATIC_KILL_SWITCH_ENV_VAR} is set in the process environment -- this overrides any "
            f"supervisor_config value and cannot be cleared by any caller; unset it in the deployment "
            f"environment to resume.",
        )
    cfg = load_supervisor_config(supervisor_config)
    auth_claims_dir = auth_claims_dir or DEFAULT_OPTIONS_AUTH_CLAIMS_DIR
    supervisor_claims_dir = supervisor_claims_dir or DEFAULT_OPTIONS_CLIENT_ORDER_ID_CLAIMS_DIR

    if not isinstance(execution_spec, dict):
        return _blocked(None, "EXECUTION_SPEC", "INVALID_EXECUTION_SPEC", "not a dict")

    if environment not in ALLOWED_ENVIRONMENTS:
        reason = "LIVE_EXECUTION_NOT_SUPPORTED_FOR_OPTIONS" if environment == "LIVE" else "UNSUPPORTED_ENVIRONMENT"
        return _blocked(None, "ENVIRONMENT", reason, environment)

    # Fresh, independent structural check -- never cached, never trusted
    # from a later `.378.authorize()` call (which re-validates again
    # internally; same "always fresh" discipline `.378`'s own docstring
    # item 1 requires of itself). This is the ONLY way this function
    # learns `underlying_symbol` before the O4B freeze gate can run.
    adapter76 = _load_options_adapter_module()
    try:
        validated = adapter76.validate_vertical_spec(execution_spec)
    except RuntimeError as exc:
        return _blocked(None, "STRUCTURE_VALIDATION", "STRUCTURE_VALIDATION_FAILED", str(exc))

    underlying_symbol = validated["structure"]["underlying_symbol"]
    client_order_id = validated["client_order_id"]
    pseudo_spec = _options_pseudo_spec(underlying_symbol, client_order_id)

    # --- O4B (.377) assignment/exercise freeze -- HARD GATE (Martin's
    # explicit decision; .377 itself is detection-only, see module
    # docstring §7). Required inputs fail closed rather than silently
    # skipping the check.
    if expected_options_positions is None:
        return _blocked(pseudo_spec, "ASSIGNMENT_FREEZE_CHECK", "MISSING_EXPECTED_OPTIONS_POSITIONS",
                         "supervise_options_execution() requires AURA's own expected options positions to "
                         "reconcile against -- this module never fabricates that list itself")
    if reconciliation_client is None:
        return _blocked(pseudo_spec, "ASSIGNMENT_FREEZE_CHECK", "MISSING_RECONCILIATION_CLIENT",
                         "attempt_submission=False still requires a real (or faked, in tests) read-only "
                         "Alpaca client to check for an open assignment/exercise freeze -- this module never "
                         "constructs one itself")
    if reconciliation_lookback_days is None:
        return _blocked(pseudo_spec, "ASSIGNMENT_FREEZE_CHECK", "MISSING_RECONCILIATION_LOOKBACK_DAYS",
                         "`.377.reconcile()` has no built-in default for lookback_days; this module does not "
                         "invent one either")

    recon77 = _load_options_reconciliation_module()
    try:
        reconciliation = recon77.reconcile(
            expected_options_positions, reconciliation_client, lookback_days=reconciliation_lookback_days,
        )
    except RuntimeError as exc:
        return _blocked(pseudo_spec, "ASSIGNMENT_FREEZE_CHECK", "RECONCILIATION_FAILED", str(exc))

    if underlying_symbol in reconciliation.get("frozen_underlyings", []):
        return _blocked(
            pseudo_spec, "ASSIGNMENT_FREEZE_CHECK", "UNDERLYING_FROZEN_PENDING_ASSIGNMENT_EXERCISE_RESOLUTION",
            reconciliation["underlyings"].get(underlying_symbol), reconciliation=reconciliation,
        )

    if cfg["kill_switch"]:
        return _blocked(pseudo_spec, "SUPERVISOR_KILL_SWITCH", "SUPERVISOR_KILL_SWITCH_ENGAGED",
                         reconciliation=reconciliation)

    # --- O8 (.344) portfolio-exposure enforcement -- second HARD GATE.
    # Every input is caller-supplied; this module never computes a Greek
    # or builds a .343 snapshot itself (module docstring §7).
    if portfolio_snapshot is None:
        return _blocked(pseudo_spec, "PORTFOLIO_EXPOSURE_ENFORCEMENT", "MISSING_PORTFOLIO_SNAPSHOT", None,
                         reconciliation=reconciliation)
    if portfolio_hypothetical is None:
        return _blocked(pseudo_spec, "PORTFOLIO_EXPOSURE_ENFORCEMENT", "MISSING_PORTFOLIO_HYPOTHETICAL", None,
                         reconciliation=reconciliation)
    if portfolio_limits is None:
        return _blocked(pseudo_spec, "PORTFOLIO_EXPOSURE_ENFORCEMENT", "MISSING_PORTFOLIO_LIMITS", None,
                         reconciliation=reconciliation)
    if portfolio_max_snapshot_age_seconds is None:
        return _blocked(pseudo_spec, "PORTFOLIO_EXPOSURE_ENFORCEMENT", "MISSING_PORTFOLIO_MAX_SNAPSHOT_AGE_SECONDS",
                         None, reconciliation=reconciliation)

    enf344 = _load_portfolio_enforcement_module()
    try:
        enforcement_decision = enf344.evaluate_hypothetical_trade(
            portfolio_snapshot, portfolio_equity_history or [], portfolio_hypothetical, portfolio_limits,
            max_snapshot_age_seconds=portfolio_max_snapshot_age_seconds, now=now_dt,
        )
    except Exception as exc:  # noqa: BLE001 -- classified, never silently swallowed
        return _blocked(pseudo_spec, "PORTFOLIO_EXPOSURE_ENFORCEMENT", "ENFORCEMENT_EVALUATION_FAILED",
                         f"{type(exc).__name__}: {exc}", reconciliation=reconciliation)

    enforcement_dict = enforcement_decision.to_dict()
    if enforcement_decision.overall_verdict == "BLOCK":
        return _blocked(pseudo_spec, "PORTFOLIO_EXPOSURE_ENFORCEMENT", "PORTFOLIO_EXPOSURE_LIMIT_BREACHED",
                         enforcement_dict, reconciliation=reconciliation, portfolio_enforcement=enforcement_dict)

    # --- O7 (.381) cost estimate -- INFORMATIONAL ONLY, never blocks.
    cost_estimate: dict[str, Any] | None = None
    if cost_model_config is not None and cost_model_leg_quotes is not None:
        cost81 = _load_options_cost_model_module()
        legs_for_cost = [
            {"occ_symbol": leg["contract"]["occ_symbol"], "side": leg["side"]}
            for leg in validated["structure"]["legs"]
        ]
        try:
            cost_estimate = cost81.estimate_round_trip_cost(
                legs_for_cost, cost_model_leg_quotes, qty=validated["qty"], config=cost_model_config,
            )
        except Exception as exc:  # noqa: BLE001 -- informational only; see module docstring §7
            cost_estimate = {"status": "COST_ESTIMATE_UNAVAILABLE", "error": f"{type(exc).__name__}: {exc}"}

    audit_extra = {"reconciliation": reconciliation, "portfolio_enforcement": enforcement_dict,
                    "cost_estimate": cost_estimate, "underlying_symbol": underlying_symbol}

    blocked, record = _options_authorize(
        execution_spec, environment=environment, auth_config=auth_config, auth_claims_dir=auth_claims_dir,
        now_dt=now_dt,
    )
    if blocked is not None:
        stage, reason, detail = blocked
        return _blocked(pseudo_spec, stage, reason, detail, **audit_extra)

    if not attempt_submission:
        # Construction-only preview -- stops HERE, before `.378.
        # authorized_order_request()` (hence `.379`'s claim store and this
        # module's own client_order_id claim store) is ever touched. See
        # module docstring §7 -- the MEXC lesson, not the Alpaca one.
        result = _base_result(pseudo_spec)
        result["authorization_id"] = record.get("authorization_id")
        result["status"] = "READY_FOR_SUBMISSION"
        result["stage"] = "CONSTRUCTION_ONLY"
        result.update(audit_extra)
        return result

    tail = _options_claim_and_submit(
        record, execution_spec, environment=environment, auth_config=auth_config, auth_claims_dir=auth_claims_dir,
        supervisor_claims_dir=supervisor_claims_dir, alpaca_client=alpaca_client, now_dt=now_dt,
    )
    if tail["blocked"]:
        extra = {k: v for k, v in tail.items() if k not in ("blocked", "stage", "reason", "detail")}
        return _blocked(pseudo_spec, tail["stage"], tail["reason"], tail["detail"],
                         authorization_id=record.get("authorization_id"), **extra, **audit_extra)

    result = _base_result(pseudo_spec)
    result["authorization_id"] = record.get("authorization_id")
    result["order_spec"] = tail["order_spec"]
    result["order_spec_digest"] = tail["order_spec_digest"]
    result.update(audit_extra)
    result["submission_result"] = tail["submission_result"]
    result["status"] = tail["submission_result"]["status"]
    result["stage"] = "SUBMITTED"
    return result


def supervise_options_exit_execution(
    position: dict[str, Any],
    *,
    entry_net_price: Any,
    leg_quotes: dict[str, Any],
    as_of_date: date,
    closing_execution_spec: dict[str, Any],
    earnings_calendar_state: Any = None,
    exit_config: dict[str, Any] | None = None,
    environment: str = "PAPER",
    auth_config: dict[str, Any] | None = None,
    auth_claims_dir: Path | None = None,
    supervisor_claims_dir: Path | None = None,
    attempt_submission: bool = False,
    alpaca_client: Any = None,
    supervisor_config: dict[str, Any] | None = None,
    now_dt: datetime | None = None,
) -> dict[str, Any]:
    """The single entry point for evaluating AND, when warranted, executing
    an O6 (`.380.evaluate_exit()`) CLOSE decision on an options vertical
    spread -- see module docstring §7. UNLIKE every other entry point in
    this file, this function reads the Supervisor's OWN kill switch
    (static AND dynamic) but NEVER uses it to produce a BLOCKED result --
    it reads it ONLY to compute the `kill_switch_engaged` argument it
    hands to `.380.evaluate_exit()`, whose inversion (True FORCES an
    emergency close) then does the actual work. This is the literal
    mechanism behind "the exit path must still succeed when the
    Supervisor's own kill switch is engaged": when `cfg["kill_switch"]`
    or the static switch is on, O6 is told to force a close and (being
    priority-ordered with KILL_SWITCH first, evaluated unconditionally)
    always returns `action="CLOSE"`, which this function then executes
    with NO further kill-switch check anywhere below this point --
    closing is always allowed once O6 has decided CLOSE, full stop.

    `position`/`entry_net_price`/`leg_quotes`/`as_of_date`/
    `earnings_calendar_state`/`exit_config` are `.380.evaluate_exit()`'s
    own parameters, passed straight through (this function never second-
    guesses O6's own decision logic -- it only supplies the one input
    that is genuinely THIS module's to supply: the Supervisor's own
    kill-switch state). `closing_execution_spec` is the actual O4-shaped
    vertical-spread spec for the CLOSING order (both legs' `direction`
    must be `CLOSE_LONG`/`CLOSE_SHORT`) -- a separately caller-supplied
    spec, never derived from O6's decision (O6 has no limit_price/qty/
    time_in_force/client_order_id fields to derive one from; that remains
    the caller's own pricing/sizing decision, exactly like O4's own
    adapter trusts `limit_price` structurally, never economically). When
    O6 decides HOLD, this function stops there and NEVER even looks at
    `closing_execution_spec` -- no authorization, no claim, nothing.

    Sequence: compute `kill_switch_engaged` from the Supervisor's own
    static+dynamic kill switch -> `.380.evaluate_exit()` -> if HOLD, stop
    -> environment gate -> `.376.validate_vertical_spec()` (fresh) ->
    closing-direction check -> `.378.authorize()` -> (only if
    `attempt_submission=True`) `.378.authorized_order_request()` (claim +
    construct) -> this module's own client_order_id claim -> `.376.
    submit()`. No O4B freeze gate and no O8 portfolio-exposure gate
    either -- both are scoped by Martin's decision to NEW (open)
    decisions only; reducing/closing existing risk is never blocked by
    either."""
    auth_claims_dir = auth_claims_dir or DEFAULT_OPTIONS_AUTH_CLAIMS_DIR
    supervisor_claims_dir = supervisor_claims_dir or DEFAULT_OPTIONS_CLIENT_ORDER_ID_CLAIMS_DIR

    cfg = load_supervisor_config(supervisor_config)
    forced_close = bool(cfg["kill_switch"]) or static_kill_switch_engaged()

    exit80 = _load_options_exit_engine_module()
    try:
        exit_decision = exit80.evaluate_exit(
            position, entry_net_price=entry_net_price, leg_quotes=leg_quotes, as_of_date=as_of_date,
            kill_switch_engaged=forced_close, earnings_calendar_state=earnings_calendar_state,
            config=exit_config,
        )
    except RuntimeError as exc:
        return _blocked(None, "EXIT_DECISION", "EXIT_EVALUATION_FAILED", str(exc))

    if exit_decision["action"] != "CLOSE":
        # HOLD -- nothing to execute. Deliberately never even inspects
        # `closing_execution_spec` in this branch: no authorization, no
        # claim, nothing durable touched for a position that isn't
        # closing.
        result = _base_result(None)
        result["status"] = "HOLD"
        result["stage"] = "EXIT_DECISION"
        result["exit_decision"] = exit_decision
        return result

    audit_extra = {"exit_decision": exit_decision}

    if environment not in ALLOWED_ENVIRONMENTS:
        reason = "LIVE_EXECUTION_NOT_SUPPORTED_FOR_OPTIONS" if environment == "LIVE" else "UNSUPPORTED_ENVIRONMENT"
        return _blocked(None, "ENVIRONMENT", reason, environment, **audit_extra)

    adapter76 = _load_options_adapter_module()
    try:
        validated = adapter76.validate_vertical_spec(closing_execution_spec)
    except RuntimeError as exc:
        return _blocked(None, "STRUCTURE_VALIDATION", "STRUCTURE_VALIDATION_FAILED", str(exc), **audit_extra)

    if not all(d in adapter76.CLOSING_DIRECTIONS for d in validated["directions"]):
        return _blocked(None, "EXIT_DECISION", "CLOSING_SPEC_MUST_BE_ALL_CLOSE_DIRECTIONS",
                         validated["directions"], **audit_extra)

    underlying_symbol = validated["structure"]["underlying_symbol"]
    client_order_id = validated["client_order_id"]
    pseudo_spec = _options_pseudo_spec(underlying_symbol, client_order_id, direction="CLOSE")
    audit_extra["underlying_symbol"] = underlying_symbol

    blocked, record = _options_authorize(
        closing_execution_spec, environment=environment, auth_config=auth_config, auth_claims_dir=auth_claims_dir,
        now_dt=now_dt,
    )
    if blocked is not None:
        stage, reason, detail = blocked
        return _blocked(pseudo_spec, stage, reason, detail, **audit_extra)

    if not attempt_submission:
        # Construction-only preview -- same MEXC-precedent discipline as
        # supervise_options_execution(): stops before .379/this module's
        # own client_order_id claim store is ever touched.
        result = _base_result(pseudo_spec)
        result["authorization_id"] = record.get("authorization_id")
        result["status"] = "READY_FOR_SUBMISSION"
        result["stage"] = "CONSTRUCTION_ONLY"
        result.update(audit_extra)
        return result

    tail = _options_claim_and_submit(
        record, closing_execution_spec, environment=environment, auth_config=auth_config,
        auth_claims_dir=auth_claims_dir, supervisor_claims_dir=supervisor_claims_dir,
        alpaca_client=alpaca_client, now_dt=now_dt,
    )
    if tail["blocked"]:
        extra = {k: v for k, v in tail.items() if k not in ("blocked", "stage", "reason", "detail")}
        return _blocked(pseudo_spec, tail["stage"], tail["reason"], tail["detail"],
                         authorization_id=record.get("authorization_id"), **extra, **audit_extra)

    result = _base_result(pseudo_spec)
    result["authorization_id"] = record.get("authorization_id")
    result["order_spec"] = tail["order_spec"]
    result["order_spec_digest"] = tail["order_spec_digest"]
    result.update(audit_extra)
    result["submission_result"] = tail["submission_result"]
    result["status"] = tail["submission_result"]["status"]
    result["stage"] = "SUBMITTED"
    return result


def supervise_options_reconciliation_pass(
    expected_options_positions: list[dict[str, Any]],
    *,
    reconciliation_client: Any = None,
    lookback_days: int | None = None,
) -> dict[str, Any]:
    """Crash-recovery / ongoing-reconciliation entry point for options
    positions -- mirrors `supervise_mexc_reconciliation_pass()`'s shape,
    calling O4B's `.377.reconcile()` instead of MEXC's `.30`. Deliberately
    NOT gated by either kill switch (module docstring §5/§7) -- pure
    observation/recording of what already happened, never a new order.

    Unlike the MEXC version, there is no options-specific intent ledger
    to recover an individual intent's lifecycle state from (module
    docstring §7's crash-recovery design decision: none was built, none
    is needed) -- this function's only job is to surface `.377`'s own
    freeze/mismatch findings directly, for a caller (ops, or the next
    `supervise_options_execution()` call, which already re-runs this same
    check as its own hard gate) to act on. Safe to call any number of
    times: `.377` is stateless and makes no state-mutating call."""
    if reconciliation_client is None:
        return {"status": "BLOCKED", "reason": "MISSING_RECONCILIATION_CLIENT"}
    if lookback_days is None:
        return {"status": "BLOCKED", "reason": "MISSING_LOOKBACK_DAYS"}

    recon77 = _load_options_reconciliation_module()
    try:
        reconciliation = recon77.reconcile(
            expected_options_positions, reconciliation_client, lookback_days=lookback_days,
        )
    except RuntimeError as exc:
        return {"status": "BLOCKED", "reason": "RECONCILIATION_FAILED", "detail": str(exc)}

    return {
        "status": "ESCALATED" if reconciliation["requires_human_attention"] else "RECONCILED",
        "reconciliation": reconciliation,
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
    "supervise_options_execution",
    "supervise_options_exit_execution",
    "supervise_options_reconciliation_pass",
})
