#!/usr/bin/env python3
"""
AURA v0.5.4 -- Neutral deterministic LLM stub for .50's decide()

THE FINDING THIS MODULE RESOLVES (surfaced to Martin and approved before
implementation -- see this session's decision record)
-------------------------------------------------------------------------
`aura_v05350_decision_engine.py::decide()` is NOT a pure deterministic
function: when `direction != "NO_DIRECTIONAL_EVIDENCE"` it calls
`proposal_module.generate_proposal_for_candidate(...)` and, if that
proposal's status is `"PROPOSED"`, `proposal_module.challenge_proposal
(...)` -- both of which require a real `llm_client` (`.49`'s AI-
proposal/adversarial-challenge step). This is structurally in tension
with Martin's .54 hard-determinism requirement ("identical inputs must
produce identical outputs... No AI-generated parameter selection... No
hidden randomness").

Martin's approved resolution (Option A, "Neutral stub client"): use a
deterministic, always-no-concerns stub client, modeled directly on
`.50`'s OWN established test pattern
(`tests/test_aura_v05350_decision_engine.py:122`'s `FakeLLMClient`,
already used throughout `.50`'s own test suite to exercise `decide()`
without a real network LLM call). Under this stub:
  - `generate_proposal_for_candidate` always returns a `PROPOSED`
    proposal (a fixed, content-free thesis string -- never a real
    analysis, never model-generated text) -- so `decide()` runs its full
    code path rather than short-circuiting into `REJECTED_NO_MODEL`.
  - `challenge_proposal` always returns zero concerns (`passed=True`).
  - Consequence, load-bearing and made explicit here: `apply_challenge_
    penalty` is a no-op whenever `critique.passed` is `True`
    (`aura_v05349_ai_proposal_pipeline.py:920`: `if critique.passed:
    return base_rank_score`), so `ai_penalty_per_concern` STRUCTURALLY
    NEVER FIRES anywhere in the .54 baseline, regardless of its
    configured value. `run_deterministic_critic` (genuinely code-only,
    confirmed to take no `llm_client` parameter) remains FULLY ACTIVE
    and can still apply `critic_penalty_per_issue` based on real
    evidence-quality issues.
  - This stub never calls a network, never varies its output, and
    contains no model-generated content anywhere -- it satisfies
    determinism by construction, not by chance.

This is an EXPLICITLY_SELECTED interpretation of "use the frozen .50
parameters under a hard-determinism constraint", not evidence, and not
an optimization -- it is documented here, in the .54 implementation
report, and was confirmed with Martin before use.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

VERSION = "AURA v0.5.4"

_NEUTRAL_THESIS = (
    "AURA v0.5.4 baseline: deterministic neutral stub -- no model-generated "
    "analysis. This proposal exists only to exercise .50's decide() code "
    "path deterministically; it is not a real AI thesis."
)
_NEUTRAL_CONFIDENCE = 0.5  # a fixed, content-free midpoint -- never read by any scoring function (.50's own docstring: "informational only")

_CANDIDATE_ID_RE = re.compile(r"candidate_id:\s*(\S+)")


class LLMStubError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class NeutralDeterministicLLMClient:
    """Drop-in `llm_client` for `.50.decide(..., llm_client=...)`. Always
    proposes (fixed, non-substantive thesis), always passes the
    adversarial challenge (zero concerns). Deterministic: identical
    input always produces identical output; no state, no randomness, no
    network call.

    `.complete(system, user, *, json_mode=False)` mirrors the exact
    interface `.49`'s `generate_proposal_for_candidate`/
    `challenge_proposal` call (`llm_client.complete(system=..., user=...,
    json_mode=True)`), distinguishing which of the two calls it is by
    inspecting `system` for `.49`'s own fixed adversarial-challenger
    marker text -- never by call order/count, so it stays correct
    regardless of how many times or in what sequence `decide()` is
    invoked.
    """

    name: str = "aura-v054-neutral-deterministic-stub"
    model: str = "aura-v054-neutral-stub-v1"

    def complete(self, system: str, user: str, *, json_mode: bool = False) -> str:
        if "adversarial challenger" in system:
            # `.49`'s challenge_proposal call -- always zero concerns.
            return '{"concerns": []}'
        # `.49`'s generate_proposal_for_candidate call -- always propose,
        # echoing back the exact candidate_id it was given (required for
        # `.49`'s closed-output-space enforcement to accept the proposal).
        match = _CANDIDATE_ID_RE.search(user)
        if match is None:
            raise LLMStubError("COULD_NOT_EXTRACT_CANDIDATE_ID:unexpected prompt shape from proposal_module")
        candidate_id = match.group(1)
        import json as _json

        return _json.dumps(
            {
                "candidate_id": candidate_id,
                "thesis": _NEUTRAL_THESIS,
                "confidence": _NEUTRAL_CONFIDENCE,
            }
        )
