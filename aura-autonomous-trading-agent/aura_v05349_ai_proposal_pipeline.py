#!/usr/bin/env python3
"""
AURA v0.5.3.49 — AI Proposal Generation Pipeline + AI Capability Contract
(amended scope: adversarial-challenge sub-role + non-expansive-repair
sanitizer, per the v0.5.5 Final Implementation Baseline §6).

This is the FIRST module in AURA to call an LLM. Everything through `.48`
is deterministic. `.49` introduces AI strictly as a Level 1/2 research
capability — proposing and challenging candidate ideas — never as
something that can author, size, or authorize a trade. Per this project's
own canonical architecture/governance doc (`AURA_DELTAX_MEXC_canonical_
architecture_and_governance_2026-09-08.md` §4.2-4.4): Level 1 = raw
observations, Level 2 = derived Market State, Level 3 = Trading Decision
(only Level 3 may produce an ExecutionIntent). `.49` is Level 1/2 only.

Why this exists (v0.5.5 Final Implementation Baseline, §6, `.49` amended
acceptance criteria — quoted, not paraphrased, so a future reader does not
have to reconstruct the exact bar from chat history)
------------------------------------------------------------------------
  1. "An adversarial-challenge role exists as a second, independent AI
     pass whose sole permitted output is a critique/penalty applied to a
     first-pass proposal's ranking score — it may never itself author,
     size, or approve an order (ORION-derived)."
  2. "Any AI output field that fails schema/type validation is handled by
     a non-expansive repair: the deterministic sanitizer may only remove
     or null the invalid field, never invent, substitute, or expand a
     value — and every repair is logged to the audit trail (Catalyst
     Surface Agent-derived)."
  3. "The deterministic, LLM-free critic planned for `.50` and the AI-vs-
     AI adversarial-challenge role in `.49` are both present and distinct
     — a regression test should confirm neither can be silently
     substituted for the other."

The AI Capability Contract (v0.5.5 Master Roadmap §5) governing everything
in this module: ALLOW hypothesis generation, market analysis, strategy
proposal, candidate ranking, experiment design, abstain, reduce-never-
author size. DENY increase risk, override Risk Gate/Position State,
disable kill switch, call broker directly, create final order parameters.
This is enforced STRUCTURALLY here, not just by convention: `AIProposal`
and `Critique` below have no field capable of expressing quantity, price,
side, order type, broker call, or execution intent — there is no field to
repurpose into one, and a dedicated governance test locks this in.

Reuse scan (Martin's explicit A/B/C/D framework, performed BEFORE writing
any new code, via a dedicated audit)
------------------------------------------------------------------------
  - Nothing in AURA's own `.31`-`.48` chain implements any of this: (D),
    greenfield within AURA. `.33`'s `SOURCE_KINDS` already includes
    `"AI_PROPOSAL"` as a valid tag on a `CanonicalExecutionIntent`, and
    `.38`'s Common Execution Supervisor already has a passing test proving
    an `AI_PROPOSAL`-tagged intent is supervised identically to a
    `DETERMINISTIC_SIGNAL` one (no special-casing, no bypass) — but that
    tag is applied by a future promotion step (`.50`'s Decision Engine, at
    the earliest), never by this module. `.49` never constructs, imports,
    or references `CanonicalExecutionIntent` at all.
  - `.49`'s two named source patterns (ORION for the adversarial-challenge
    role, Catalyst Surface Agent for non-expansive repair) were both
    marked "Not located" in this project's own prior competitor audit
    (`AURA_Competition_GitHub_Deep_Dive_2026-09-11.md`). The acceptance
    criteria above are Martin's description of the pattern, not verified
    source to copy from.
  - `/home/claude/work/competitors/198ea29e-aegismain/` ("Aegis", a
    from-scratch find this audit, not one of the 9 previously-named/
    verified submissions — flagged as unverified, not run through this
    project's Implemented/Demonstrated/Validated/Claimed process):
      - `aegis/agents/llm.py`: (B, adapted not copied) the provider-
        agnostic `LLMClient` protocol, and the degrade-to-`NullClient`-on-
        missing-key convention, are reused in shape below
        (`LLMClient`/`NullClient`/`AnthropicClient`). Per Martin's
        explicit choice, ONLY the Anthropic backend is adapted here —
        Aegis's Gemini backend is deliberately NOT ported.
      - `aegis/agents/critic.py`: (B, adapted not copied) the core
        design — the model's response schema has no field for approval,
        so `passed` is *derived* ("raised no concerns"), never asserted by
        the AI, and any client/API/parse failure fails closed — is the
        closest match found anywhere in the searched corpus to acceptance
        criterion 1, and is reused in shape for `challenge_proposal`/
        `Critique` below. Its options-specific prompt content (mandate
        clauses, option legs) is NOT reused; AURA's critic prompt and
        `Critique` fields were written independently for this module's
        candidate/thesis shape.
      - `aegis/core/proposal.py`: (B, adapted not copied) the content-
        hash-bound-proposal idea (a proposal's approval/critique is bound
        to a hash of its exact content, so it cannot be silently edited
        after the fact) is reused conceptually in `_proposal_content_
        hash` below — using this project's own `stable_json`/`sha256_
        text` convention (first established in `.33`), not Aegis's
        Decimal-normalizing canonicalizer, since AURA's proposals carry
        no monetary fields to normalize.
      - No sanitizer implementing true subtract-only repair exists
        anywhere in the searched corpus — Aegis's critic fails closed on
        any parse/validation failure rather than repairing a partial
        result. See "Martin's scoping decision on non-expansive repair"
        below for how this gap was resolved.
  - Already-validated patterns (per this project's own prior audits, not
    re-verified here): PrintRunner's closed-output-space LLM constraint
    and Hermes Trader's rolling confidence calibration, both cited in the
    Final Baseline as feeding the AI Capability Contract. `.49` implements
    its own, more conservative version of the closed-output-space idea —
    see "Closed-output-space enforcement" below — rather than importing
    PrintRunner code, which was not located as directly portable in this
    audit.

Scoping decisions (Martin, 2026-09-13, via `AskUserQuestion` — reproduced
here so a future reader does not have to reconstruct them from chat
history)
------------------------------------------------------------------------
  1. LLM provider: Anthropic only. No Gemini/other backend in this
     module. (A second provider can be added later without reworking the
     `LLMClient` protocol, per Aegis's own design rationale for keeping it
     narrow.)
  2. Typed AI-output contract shape: frozen `@dataclass`, matching every
     other `.31`-`.48` module. No Pydantic dependency introduced.
  3. Non-expansive repair, Martin's explicit choice: **FAIL CLOSED on any
     invalid field**, not literal subtract-only repair. This is a
     DELIBERATE, DOCUMENTED DEVIATION from acceptance criterion 2's exact
     text ("may only remove or null the invalid field... never invent,
     substitute, or expand a value" implies partial recovery of the valid
     remainder). Martin was shown this exact tension before choosing:
     no reference implementation of true subtract-only repair exists
     anywhere in the audited corpus (Aegis, the closest analog, also
     fails closed rather than repairing), and fail-closed is simpler,
     stricter, and consistent with AURA's own standing philosophy already
     enforced everywhere else in this codebase — unknown/invalid critical
     data is rejected outright, never partially trusted (`.15`, `.40`,
     `.44`). Concretely: if ANY field of a parsed AI response fails
     validation (missing, wrong type, out of range, or a `candidate_id`
     outside the closed shortlist), the ENTIRE proposal is rejected
     (`status="REJECTED_INVALID_OUTPUT"`) — there is no code path that
     returns a proposal with some fields nulled out and others kept. Every
     rejection records its exact reason (`rejection_reason`) as the
     module's implementation of "every repair is logged to the audit
     trail" — the log entry is the rejection itself, not a silent drop.
  4. Adversarial-challenge scope: narrow — the challenge role's only
     permitted output is a list of concerns feeding a ranking-score
     penalty (`apply_challenge_penalty` below), matching acceptance
     criterion 1's literal text. It never reviews or restates the full
     proposal as an alternative recommendation (Aegis's broader pattern
     was NOT adopted, by explicit choice).

Closed-output-space enforcement (this module's concrete implementation of
the pattern named, not copied, from PrintRunner)
------------------------------------------------------------------------
The caller supplies a fixed, deterministically-built `shortlist` of
`Candidate` records (their `candidate_id`s form the only universe the AI
may ever reference — `.49` does not compute this shortlist itself; a
future integration point, most likely `.50`, is responsible for building
it from `.46`/`.47`/`.48`'s deterministic evidence). `.49` goes further
than "the AI must pick from N options": each proposal-generation call
presents the AI with exactly ONE candidate at a time and the AI is only
ever asked to confirm the SAME `candidate_id` it was given, not choose
among several. The returned `candidate_id` is validated to match exactly;
any mismatch is a hard rejection. This eliminates the possibility of the
AI ever substituting a different, unrequested identifier — a stronger
guarantee than "chooses from a list," and a deliberately conservative
design choice given `.49` is AURA's first AI-touching module.

Governance constraints this module is built to respect (from the
canonical architecture/governance doc, same as `.46`-`.48`)
------------------------------------------------------------------------
  "AI may propose, analyse, challenge, or abstain... never bypass
  deterministic execution authority." -- `AIProposal`/`Critique` cannot
      structurally express an order; this module has no path to an
      order, position, sizing, or authorization decision anywhere in its
      call graph (locked in by a dedicated governance test).
  "No alternative-intelligence signal may determine trade direction or
  size on its own." -- a `Critique`'s `passed` field is DERIVED from
      `concerns` being empty; the AI has no field to assert an approval
      into, mirroring `.47`'s "no fabricated corroboration" discipline
      one layer up (no fabricated approval, here).
  "Level 1/2 only; only Level 3 may produce an ExecutionIntent." -- this
      module never imports, constructs, or references `.33`'s
      `CanonicalExecutionIntent`.

Persistence
------------------------------------------------------------------------
`.49` introduces NO new persisted state, mirroring `.47`/`.48`'s own
precedent: every function here is a pure function of caller-supplied
inputs (a shortlist, an injected `LLMClient`, optional `now`) plus
required research parameters, and every audit-relevant fact (rejection
reasons, raw model responses, content hashes, timestamps) is carried on
the returned dataclasses rather than written to disk. A future
integration milestone (most likely `.50`, when a real Decision Engine
exists to journal decisions) is expected to persist these for a durable
audit trail — `.49` provides the complete in-memory record; it does not
yet write it anywhere.

Reproducibility / auditability
------------------------------------------------------------------------
Every `AIProposal` carries: the exact `candidate_id`/`symbol` requested,
`shortlist_hash` (binds the proposal to the exact shortlist it was
generated against), `model_name`, `generated_at`, `raw_response` (the
verbatim model output, for audit even when rejected), `status`, and
`rejection_reason` when rejected. Every `Critique` carries a
`proposal_content_hash` binding it to the exact proposal content
reviewed (so a proposal edited after being challenged would produce a
different hash, mirroring Aegis's content-addressable-approval idea), plus
`model_name`, `raw_response`, and `reviewed_at`. Test fixtures use an
injected fake `LLMClient` throughout — this module makes zero live network
calls in its own test suite, matching `.46`'s convention of never calling
a live external API from tests.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from time import sleep as _sleep
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

log = logging.getLogger(__name__)

VERSION = "AURA v0.5.3.49"
ENGINE = "AI_PROPOSAL_PIPELINE"
SCHEMA_VERSION = "1.0"

VALID_PROPOSAL_STATUSES = frozenset(
    {"PROPOSED", "REJECTED_INVALID_OUTPUT", "REJECTED_NO_MODEL"}
)


class AIProposalPipelineError(Exception):
    """Raised for programmer-error / required-parameter violations only.

    Never raised for AI output being wrong, malformed, or untrustworthy —
    that is what `status="REJECTED_INVALID_OUTPUT"` on `AIProposal`, and
    `passed=False` on `Critique`, are for. This mirrors `.47`'s
    `SentimentScoringError` discipline: a required-parameter violation is
    a caller bug; untrustworthy AI output is an expected, handled case.
    """


# ============================================================================
# Stable JSON / hashing helpers (mirrors `.33`'s stable_json/sha256_text
# convention verbatim — copied, not imported, per this repo's established
# convention of not creating cross-module import coupling for small
# helpers; see `.40`/`.44`/`.45` for the same pattern).
# ============================================================================


def stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def _parse_json_object(raw: str | None) -> dict[str, Any] | None:
    """Pull the first JSON object out of a raw model response.

    Tolerates prose/code-fences around the JSON (some backends/prompts
    produce them despite being asked for a bare object) but fails closed
    (`None`) when nothing parseable is found. Adapted from Aegis's
    `critic.py::_parse_json_object` — same shape, reused because the
    problem ("a model asked for bare JSON sometimes wraps it anyway") is
    generic, not proposal-specific.
    """
    if not raw:
        return None
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


# ============================================================================
# LLM client — provider-agnostic protocol, Anthropic-only backend per
# Martin's explicit scoping decision. Adapted (not copied) from Aegis's
# `aegis/agents/llm.py`.
# ============================================================================

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BACKOFF_SECONDS = 4.0

_no_provider_logged = False


@runtime_checkable
class LLMClient(Protocol):
    """Anything that can turn a system instruction plus a message into text."""

    def complete(self, system: str, user: str, *, json_mode: bool = False) -> str | None:
        ...

    @property
    def model(self) -> str:
        ...

    @property
    def name(self) -> str:
        ...


def _status_of(exc: BaseException) -> int | None:
    for attribute in ("code", "status_code"):
        value = getattr(exc, attribute, None)
        if isinstance(value, int):
            return value
    return None


def _is_transient_status(status: int | None) -> bool:
    return status is not None and (status == 429 or 500 <= status < 600)


def _anthropic_retryable(exc: BaseException) -> bool:
    if _is_transient_status(_status_of(exc)):
        return True
    return type(exc).__name__ in {"RateLimitError", "InternalServerError", "APIConnectionError"}


def _retry_after(exc: BaseException) -> float | None:
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if not headers:
        return None
    try:
        return float(headers.get("retry-after"))
    except (TypeError, ValueError):
        return None


def _with_retries(
    call: Callable[[], Any],
    *,
    is_retryable: Callable[[BaseException], bool],
    provider: str,
    max_attempts: int,
    backoff_seconds: float,
    sleeper: Callable[[float], None],
) -> Any:
    """Call `call`, retrying only transient (429/5xx) failures. Everything
    else propagates immediately -- a 4xx that is not 429 is a decision,
    not a blip.
    """
    delay = backoff_seconds
    for attempt in range(1, max_attempts + 1):
        try:
            return call()
        except Exception as exc:
            if attempt == max_attempts or not is_retryable(exc):
                raise
            wait = _retry_after(exc) or delay
            log.warning(
                "%s call failed with %s (attempt %d/%d); retrying in %.0fs",
                provider, _status_of(exc) or type(exc).__name__, attempt, max_attempts, wait,
            )
            sleeper(wait)
            delay *= 2
    raise AssertionError("unreachable")  # pragma: no cover


class AnthropicClient:
    """The Anthropic SDK behind the shared `LLMClient` protocol."""

    MODEL_ENV = "ANTHROPIC_MODEL"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str | None = None,
        max_tokens: int = 4096,
        sdk: Any | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self._model = model or os.environ.get(self.MODEL_ENV) or DEFAULT_ANTHROPIC_MODEL
        self._max_tokens = max_tokens
        self._max_attempts = max(1, int(max_attempts))
        self._backoff = float(backoff_seconds)
        self._sleeper = sleeper or _sleep
        if sdk is not None:
            self._sdk = sdk
        else:
            import anthropic  # local import: keep this an optional dependency

            self._sdk = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    def complete(self, system: str, user: str, *, json_mode: bool = False) -> str | None:
        # No native JSON mode on this SDK path: the caller's prompt does
        # the shaping (see _PROPOSAL_SYSTEM / _CRITIC_SYSTEM below), and
        # _parse_json_object parses defensively.
        request: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        response = _with_retries(
            lambda: self._sdk.messages.create(**request),
            is_retryable=_anthropic_retryable,
            provider="anthropic",
            max_attempts=self._max_attempts,
            backoff_seconds=self._backoff,
            sleeper=self._sleeper,
        )
        blocks = getattr(response, "content", None) or []
        text = "".join(
            getattr(block, "text", "") for block in blocks if getattr(block, "type", None) == "text"
        )
        return text or None

    @property
    def model(self) -> str:
        return self._model

    @property
    def name(self) -> str:
        return f"Anthropic {self._model}"


class NullClient:
    """No model configured. Returns None so callers take their fail-closed
    path -- a missing key costs a rejected proposal, never a trade.
    """

    def complete(self, system: str, user: str, *, json_mode: bool = False) -> str | None:
        return None

    @property
    def model(self) -> str:
        return "none"

    @property
    def name(self) -> str:
        return "no model"


def build_llm_client(env: Mapping[str, str] | None = None, **kwargs: Any) -> LLMClient:
    """Pick a backend from the environment. `ANTHROPIC_API_KEY` unset ->
    `NullClient` (degrade, never fail the caller). Per Martin's explicit
    scoping decision, Anthropic is the only backend -- no other provider
    is consulted.
    """
    global _no_provider_logged
    env = os.environ if env is None else env
    if env.get("ANTHROPIC_API_KEY"):
        # Thread the resolved model from the SAME env mapping used for
        # provider selection -- not raw os.environ -- so an injected env
        # dict (tests, or a future non-process config source) is honored
        # end-to-end rather than only for picking the backend.
        kwargs.setdefault("model", env.get(AnthropicClient.MODEL_ENV))
        return AnthropicClient(env["ANTHROPIC_API_KEY"], **kwargs)
    if not _no_provider_logged:
        log.info(
            "no ANTHROPIC_API_KEY configured; .49 will produce only "
            "REJECTED_NO_MODEL proposals and unreviewed (fail-closed) critiques"
        )
        _no_provider_logged = True
    return NullClient()


# ============================================================================
# Closed-output-space input contract
# ============================================================================


@dataclass(frozen=True, slots=True)
class Candidate:
    """One item in the deterministically-built shortlist the AI may
    discuss. `.49` does not build this shortlist itself -- a future
    integration point (most likely `.50`) is responsible for constructing
    it from `.46`/`.47`/`.48`'s deterministic evidence.
    """

    candidate_id: str
    symbol: str
    evidence_summary: str


def build_shortlist_hash(shortlist: tuple[Candidate, ...]) -> str:
    """Binds a proposal run to the exact shortlist it was generated
    against -- if the shortlist changes, every proposal generated from it
    is auditable as having seen a different universe.
    """
    payload = [
        {"candidate_id": c.candidate_id, "symbol": c.symbol, "evidence_summary": c.evidence_summary}
        for c in shortlist
    ]
    return sha256_text(stable_json(payload))


# ============================================================================
# AI Proposal (Level 1/2 output contract). Structurally incapable of
# carrying an execution instruction: no qty/price/side/order_type/broker/
# execution_intent field exists on this type, and never will without
# editing this file directly (locked in by a governance test).
# ============================================================================


@dataclass(frozen=True, slots=True)
class AIProposal:
    candidate_id: str
    symbol: str
    thesis: str | None
    stated_confidence: float | None
    model_name: str
    shortlist_hash: str
    generated_at: str
    raw_response: str | None
    status: str  # one of VALID_PROPOSAL_STATUSES
    rejection_reason: str | None = None

    def __post_init__(self) -> None:
        if self.status not in VALID_PROPOSAL_STATUSES:
            raise AIProposalPipelineError(f"INVALID_PROPOSAL_STATUS:{self.status}")
        if self.status == "PROPOSED" and (self.thesis is None or self.stated_confidence is None):
            raise AIProposalPipelineError("PROPOSED_PROPOSAL_MISSING_REQUIRED_FIELDS")
        if self.status != "PROPOSED" and self.rejection_reason is None:
            raise AIProposalPipelineError("REJECTED_PROPOSAL_MISSING_REASON")


_PROPOSAL_SYSTEM = """You are a research analyst for AURA, an autonomous trading \
research system. You are being asked about exactly ONE candidate from a \
pre-approved shortlist. You may discuss ONLY that candidate, referenced by \
its exact candidate_id given below -- you have no ability to select, name, \
or discuss any other candidate, symbol, or instrument.

You cannot author, size, price, or place a trade. You have no mechanism to \
do so, and nothing you write is an order or an instruction to trade -- it is \
research evidence for a separate, deterministic decision process that you \
do not control and cannot see the output of.

Respond with a single JSON object and nothing else:

{"candidate_id": "<must exactly equal the candidate_id given below>", \
"thesis": "<your written analysis>", "confidence": <a number from 0.0 to 1.0>}

Do not wrap the JSON in code fences or add commentary. Do not invent a \
candidate_id different from the one given."""


def _proposal_prompt(candidate: Candidate) -> str:
    return (
        f"CANDIDATE\n"
        f"  candidate_id: {candidate.candidate_id}\n"
        f"  symbol: {candidate.symbol}\n"
        f"  evidence: {candidate.evidence_summary}\n"
    )


def generate_proposal_for_candidate(
    candidate: Candidate,
    *,
    shortlist_hash: str,
    llm_client: LLMClient,
    now: datetime | None = None,
) -> AIProposal:
    """Generate (or fail-closed reject) one AI proposal for `candidate`.

    Fail-closed validation (Martin's explicit `.49` scoping decision — see
    module docstring for the documented deviation from the acceptance
    criterion's literal subtract-only-repair text): ANY validation
    failure — no model configured, a call error, an unparseable response,
    a `candidate_id` mismatch, a missing/non-string thesis, or a
    confidence value outside [0.0, 1.0] — rejects the ENTIRE proposal.
    There is no code path that returns a proposal with some fields kept
    and others nulled out.
    """
    generated_at = _now_iso(now)

    if llm_client is None or isinstance(llm_client, NullClient):
        return AIProposal(
            candidate_id=candidate.candidate_id,
            symbol=candidate.symbol,
            thesis=None,
            stated_confidence=None,
            model_name="none",
            shortlist_hash=shortlist_hash,
            generated_at=generated_at,
            raw_response=None,
            status="REJECTED_NO_MODEL",
            rejection_reason="no LLM client configured",
        )

    try:
        raw = llm_client.complete(system=_PROPOSAL_SYSTEM, user=_proposal_prompt(candidate), json_mode=True)
    except Exception as exc:  # noqa: BLE001 -- any provider failure is a rejection, not a crash
        return AIProposal(
            candidate_id=candidate.candidate_id,
            symbol=candidate.symbol,
            thesis=None,
            stated_confidence=None,
            model_name=llm_client.name,
            shortlist_hash=shortlist_hash,
            generated_at=generated_at,
            raw_response=None,
            status="REJECTED_INVALID_OUTPUT",
            rejection_reason=f"llm call failed: {type(exc).__name__}: {exc}",
        )

    if not raw:
        return AIProposal(
            candidate_id=candidate.candidate_id,
            symbol=candidate.symbol,
            thesis=None,
            stated_confidence=None,
            model_name=llm_client.name,
            shortlist_hash=shortlist_hash,
            generated_at=generated_at,
            raw_response=raw,
            status="REJECTED_INVALID_OUTPUT",
            rejection_reason="llm returned no content",
        )

    parsed = _parse_json_object(raw)
    if parsed is None:
        return AIProposal(
            candidate_id=candidate.candidate_id,
            symbol=candidate.symbol,
            thesis=None,
            stated_confidence=None,
            model_name=llm_client.name,
            shortlist_hash=shortlist_hash,
            generated_at=generated_at,
            raw_response=raw,
            status="REJECTED_INVALID_OUTPUT",
            rejection_reason="response could not be parsed as a JSON object",
        )

    returned_id = parsed.get("candidate_id")
    if returned_id != candidate.candidate_id:
        # Closed-output-space enforcement: the AI substituted a different
        # (possibly hallucinated) identifier. Hard rejection, no repair.
        return AIProposal(
            candidate_id=candidate.candidate_id,
            symbol=candidate.symbol,
            thesis=None,
            stated_confidence=None,
            model_name=llm_client.name,
            shortlist_hash=shortlist_hash,
            generated_at=generated_at,
            raw_response=raw,
            status="REJECTED_INVALID_OUTPUT",
            rejection_reason=f"candidate_id mismatch: requested {candidate.candidate_id!r}, got {returned_id!r}",
        )

    thesis = parsed.get("thesis")
    if not isinstance(thesis, str) or not thesis.strip():
        return AIProposal(
            candidate_id=candidate.candidate_id,
            symbol=candidate.symbol,
            thesis=None,
            stated_confidence=None,
            model_name=llm_client.name,
            shortlist_hash=shortlist_hash,
            generated_at=generated_at,
            raw_response=raw,
            status="REJECTED_INVALID_OUTPUT",
            rejection_reason="thesis missing or not a non-empty string",
        )

    confidence = parsed.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not (0.0 <= float(confidence) <= 1.0):
        return AIProposal(
            candidate_id=candidate.candidate_id,
            symbol=candidate.symbol,
            thesis=None,
            stated_confidence=None,
            model_name=llm_client.name,
            shortlist_hash=shortlist_hash,
            generated_at=generated_at,
            raw_response=raw,
            status="REJECTED_INVALID_OUTPUT",
            rejection_reason=f"confidence missing, non-numeric, or outside [0.0, 1.0]: {confidence!r}",
        )

    return AIProposal(
        candidate_id=candidate.candidate_id,
        symbol=candidate.symbol,
        thesis=thesis.strip(),
        stated_confidence=float(confidence),
        model_name=llm_client.name,
        shortlist_hash=shortlist_hash,
        generated_at=generated_at,
        raw_response=raw,
        status="PROPOSED",
        rejection_reason=None,
    )


def generate_proposals(
    shortlist: tuple[Candidate, ...],
    *,
    llm_client: LLMClient,
    now: datetime | None = None,
) -> tuple[AIProposal, ...]:
    """Generate one proposal attempt per candidate in `shortlist`. Every
    candidate gets exactly one `AIProposal` back (`PROPOSED` or a
    `REJECTED_*` status) -- never fewer, never a silently dropped
    candidate, so a caller can always account for the full shortlist.
    """
    shortlist_hash = build_shortlist_hash(shortlist)
    return tuple(
        generate_proposal_for_candidate(c, shortlist_hash=shortlist_hash, llm_client=llm_client, now=now)
        for c in shortlist
    )


def _proposal_content_hash(proposal: AIProposal) -> str:
    """Content-addressable hash of a proposal's evidentiary fields.

    Adapted conceptually from Aegis's `TradeProposal.content_hash()` (bind
    approval/critique to exact content so an edit after the fact voids
    it) -- reimplemented with this project's own `stable_json`/
    `sha256_text` convention rather than Aegis's Decimal-normalizing
    canonicalizer, since a proposal here carries no monetary fields.
    """
    payload = {
        "candidate_id": proposal.candidate_id,
        "symbol": proposal.symbol,
        "thesis": proposal.thesis,
        "stated_confidence": proposal.stated_confidence,
        "shortlist_hash": proposal.shortlist_hash,
        "status": proposal.status,
    }
    return sha256_text(stable_json(payload))


# ============================================================================
# Adversarial-challenge role (acceptance criterion 1). The model's
# response schema has no field for approval -- `passed` is DERIVED
# ("raised no concerns"), never asserted by the AI. Adapted in shape from
# Aegis's `critic.py`; AURA's own prompt/field content.
# ============================================================================


@dataclass(frozen=True, slots=True)
class Critique:
    """The adversarial-challenge role's advisory reading of a proposal.

    `passed` means "the challenger found nothing to say", not "this
    proposal is approved" -- there is no field the AI can write an
    approval into. Binds nothing: no code anywhere in this repo consults
    `Critique.passed` to authorize, size, or gate an order; it is
    consumed only by `apply_challenge_penalty` below, which adjusts a
    caller-supplied ranking score, never an authorization decision.
    """

    passed: bool
    concerns: tuple[str, ...]
    proposal_content_hash: str
    model_name: str
    raw_response: str | None
    reviewed_at: str


_CRITIC_SYSTEM = """You are the adversarial challenger for AURA, an autonomous \
trading research system. You are reviewing a proposal another AI wrote about \
one candidate, before a separate, deterministic ranking process considers it \
alongside other candidates.

Your job is to challenge it. Look for reasoning that does not follow from the \
evidence given, overconfidence relative to the evidence's strength, and \
claims the candidate's evidence does not actually support.

You cannot approve this proposal. You have no mechanism to do so, and raising \
no concerns is not an endorsement -- a separate deterministic process decides \
what happens next, and it does not consult your verdict, only your concerns.

Report only concerns you can support from the evidence and thesis given. Do \
not invent facts, and do not manufacture an objection when you have none.

Respond with a single JSON object and nothing else:

{"concerns": ["..."]}

The list may be empty. Do not wrap the JSON in code fences or add commentary."""


def _critic_prompt(proposal: AIProposal, candidate: Candidate) -> str:
    return (
        f"CANDIDATE\n"
        f"  candidate_id: {candidate.candidate_id}\n"
        f"  symbol: {candidate.symbol}\n"
        f"  evidence: {candidate.evidence_summary}\n\n"
        f"PROPOSAL UNDER REVIEW\n"
        f"  stated confidence: {proposal.stated_confidence}\n"
        f"  thesis: {proposal.thesis}\n"
    )


def challenge_proposal(
    proposal: AIProposal,
    candidate: Candidate,
    *,
    llm_client: LLMClient,
    now: datetime | None = None,
) -> Critique:
    """Challenge a `PROPOSED` proposal. Never raises for AI-side failure —
    fails closed (passed=False) on no client, an API error, or an
    unparseable response, exactly mirroring `generate_proposal_for_
    candidate`'s fail-closed discipline.
    """
    if proposal.status != "PROPOSED":
        raise AIProposalPipelineError(
            f"CANNOT_CHALLENGE_NON_PROPOSED_PROPOSAL:{proposal.status}"
        )
    if candidate.candidate_id != proposal.candidate_id:
        raise AIProposalPipelineError("CANDIDATE_PROPOSAL_MISMATCH")

    reviewed_at = _now_iso(now)
    content_hash = _proposal_content_hash(proposal)

    if llm_client is None or isinstance(llm_client, NullClient):
        return Critique(
            passed=False,
            concerns=("no LLM client configured; the proposal has not been challenged",),
            proposal_content_hash=content_hash,
            model_name="none",
            raw_response=None,
            reviewed_at=reviewed_at,
        )

    try:
        raw = llm_client.complete(system=_CRITIC_SYSTEM, user=_critic_prompt(proposal, candidate), json_mode=True)
    except Exception as exc:  # noqa: BLE001 -- fail closed, never propagate
        return Critique(
            passed=False,
            concerns=(f"challenger could not run: {type(exc).__name__}: {exc}",),
            proposal_content_hash=content_hash,
            model_name=llm_client.name,
            raw_response=None,
            reviewed_at=reviewed_at,
        )

    if not raw:
        return Critique(
            passed=False,
            concerns=("challenger returned nothing; treating as unreviewed",),
            proposal_content_hash=content_hash,
            model_name=llm_client.name,
            raw_response=raw,
            reviewed_at=reviewed_at,
        )

    parsed = _parse_json_object(raw)
    if parsed is None:
        return Critique(
            passed=False,
            concerns=("challenger response could not be parsed; treating as unreviewed",),
            proposal_content_hash=content_hash,
            model_name=llm_client.name,
            raw_response=raw,
            reviewed_at=reviewed_at,
        )

    concerns = tuple(str(c) for c in (parsed.get("concerns") or []) if str(c).strip())

    return Critique(
        passed=not concerns,  # derived, never asserted by the model
        concerns=concerns,
        proposal_content_hash=content_hash,
        model_name=llm_client.name,
        raw_response=raw,
        reviewed_at=reviewed_at,
    )


# ============================================================================
# Ranking-score penalty (acceptance criterion 1's "critique/penalty
# applied to a first-pass proposal's ranking score"). `penalty_per_
# concern` is a REQUIRED, no-default research parameter -- mirroring
# `.47`'s `decay_window_hours`/`min_source_count` convention: the
# magnitude of a penalty is a research decision, never an invented AURA
# constant.
# ============================================================================


def apply_challenge_penalty(
    base_rank_score: float,
    critique: Critique,
    *,
    penalty_per_concern: float,
) -> float:
    """Adjust `base_rank_score` by the challenger's concerns. This is the
    ONLY thing a `Critique` is permitted to influence anywhere in this
    repo -- a ranking score, never an authorization/order decision.
    `base_rank_score` itself is not computed by this module (out of
    scope for `.49`; expected to come from a future ranking capability,
    most likely `.50`).
    """
    if penalty_per_concern < 0:
        raise AIProposalPipelineError("INVALID_PENALTY_PER_CONCERN:must be >= 0")
    if critique.passed:
        return base_rank_score
    return base_rank_score - (penalty_per_concern * len(critique.concerns))
