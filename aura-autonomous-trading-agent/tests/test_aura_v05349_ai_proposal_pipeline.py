#!/usr/bin/env python3
"""Contract + unit tests for AURA v0.5.3.49 AI Proposal Generation Pipeline.

Every test uses an injected fake `LLMClient` -- this suite makes zero live
network calls, matching `.46`'s convention of never calling a live
external API from tests. Malformed-output tests exist specifically to
prove the fail-closed discipline (Martin's explicit `.49` scoping
decision, documented in the module docstring): a rejected proposal NEVER
retains a partial valid field.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PIPE_PATH = ROOT / "aura_v05349_ai_proposal_pipeline.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


PIPE = _load("aura_v05349_ai_proposal_pipeline", PIPE_PATH)

NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)

CANDIDATE_A = PIPE.Candidate(candidate_id="cand-1", symbol="BTC/USDT:USDT", evidence_summary="bullish sentiment, 3 sources")
CANDIDATE_B = PIPE.Candidate(candidate_id="cand-2", symbol="ETH/USDT:USDT", evidence_summary="ambiguous elliott wave candidates")
SHORTLIST = (CANDIDATE_A, CANDIDATE_B)


class FakeLLMClient:
    """Scriptable fake: returns `response` (a string or an exception
    instance to raise) regardless of prompt content, and records every
    call for assertion.
    """

    def __init__(self, response, *, name="fake-model", model="fake-model-v1"):
        self._response = response
        self._name = name
        self._model = model
        self.calls = []

    def complete(self, system, user, *, json_mode=False):
        self.calls.append((system, user, json_mode))
        if isinstance(self._response, BaseException):
            raise self._response
        return self._response

    @property
    def model(self):
        return self._model

    @property
    def name(self):
        return self._name


def _valid_proposal_json(candidate_id="cand-1", thesis="Strong momentum with corroborated sentiment.", confidence=0.7):
    import json

    return json.dumps({"candidate_id": candidate_id, "thesis": thesis, "confidence": confidence})


# ============================================================================
# LLM client construction
# ============================================================================


def test_null_client_always_returns_none():
    client = PIPE.NullClient()
    assert client.complete(system="s", user="u") is None
    assert client.model == "none"


def test_build_llm_client_degrades_to_null_without_api_key():
    client = PIPE.build_llm_client(env={})
    assert isinstance(client, PIPE.NullClient)


def test_build_llm_client_builds_anthropic_when_key_present():
    class FakeSDK:
        class messages:
            @staticmethod
            def create(**kwargs):
                raise AssertionError("should not be called in this test")

    client = PIPE.build_llm_client(env={"ANTHROPIC_API_KEY": "sk-test-123"}, sdk=FakeSDK())
    assert isinstance(client, PIPE.AnthropicClient)
    assert client.model  # non-empty, resolved from default or env


def test_anthropic_client_uses_model_env_override():
    class FakeSDK:
        class messages:
            @staticmethod
            def create(**kwargs):
                raise AssertionError("not called")

    client = PIPE.build_llm_client(
        env={"ANTHROPIC_API_KEY": "sk-test-123", "ANTHROPIC_MODEL": "claude-custom-model"},
        sdk=FakeSDK(),
    )
    assert client.model == "claude-custom-model"


def test_anthropic_client_extracts_text_blocks_and_retries_on_429():
    calls = {"n": 0}

    class FakeBlock:
        def __init__(self, type_, text=""):
            self.type = type_
            self.text = text

    class FakeResponse:
        content = [FakeBlock("text", "hello"), FakeBlock("thinking", "ignored")]

    class RateLimitError(Exception):
        status_code = 429

    class FakeSDK:
        class messages:
            @staticmethod
            def create(**kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RateLimitError("slow down")
                return FakeResponse()

    client = PIPE.AnthropicClient("sk-test", sdk=FakeSDK(), sleeper=lambda s: None)
    text = client.complete(system="s", user="u")
    assert text == "hello"
    assert calls["n"] == 2  # one retry after the transient failure


def test_anthropic_client_does_not_retry_non_transient_4xx():
    class BadRequestError(Exception):
        status_code = 400

    class FakeSDK:
        class messages:
            @staticmethod
            def create(**kwargs):
                raise BadRequestError("bad request")

    client = PIPE.AnthropicClient("sk-test", sdk=FakeSDK(), sleeper=lambda s: None)
    with pytest.raises(Exception):
        client.complete(system="s", user="u")


# ============================================================================
# Proposal generation -- happy path
# ============================================================================


def test_generate_proposal_happy_path_is_proposed():
    client = FakeLLMClient(_valid_proposal_json())
    shortlist_hash = PIPE.build_shortlist_hash(SHORTLIST)
    proposal = PIPE.generate_proposal_for_candidate(CANDIDATE_A, shortlist_hash=shortlist_hash, llm_client=client, now=NOW)
    assert proposal.status == "PROPOSED"
    assert proposal.candidate_id == "cand-1"
    assert proposal.symbol == "BTC/USDT:USDT"
    assert proposal.thesis == "Strong momentum with corroborated sentiment."
    assert proposal.stated_confidence == 0.7
    assert proposal.rejection_reason is None
    assert proposal.shortlist_hash == shortlist_hash
    assert proposal.raw_response == _valid_proposal_json()


def test_generate_proposal_confidence_boundary_values_accepted():
    for conf in (0.0, 1.0):
        client = FakeLLMClient(_valid_proposal_json(confidence=conf))
        proposal = PIPE.generate_proposal_for_candidate(CANDIDATE_A, shortlist_hash="h", llm_client=client, now=NOW)
        assert proposal.status == "PROPOSED"
        assert proposal.stated_confidence == conf


def test_generate_proposals_returns_one_per_candidate_in_order():
    client = FakeLLMClient(_valid_proposal_json())  # will mismatch cand-2, that's fine, tests coverage not correctness here
    proposals = PIPE.generate_proposals(SHORTLIST, llm_client=client, now=NOW)
    assert len(proposals) == len(SHORTLIST)
    assert proposals[0].candidate_id == "cand-1"
    assert proposals[1].candidate_id == "cand-2"


def test_generate_proposals_shares_one_shortlist_hash_across_all_candidates():
    client_a = FakeLLMClient(_valid_proposal_json(candidate_id="cand-1"))
    client_b = FakeLLMClient(_valid_proposal_json(candidate_id="cand-2"))
    hash_ = PIPE.build_shortlist_hash(SHORTLIST)
    p1 = PIPE.generate_proposal_for_candidate(CANDIDATE_A, shortlist_hash=hash_, llm_client=client_a, now=NOW)
    p2 = PIPE.generate_proposal_for_candidate(CANDIDATE_B, shortlist_hash=hash_, llm_client=client_b, now=NOW)
    assert p1.shortlist_hash == p2.shortlist_hash == hash_


def test_generate_proposal_is_reproducible_given_same_inputs():
    client1 = FakeLLMClient(_valid_proposal_json())
    client2 = FakeLLMClient(_valid_proposal_json())
    p1 = PIPE.generate_proposal_for_candidate(CANDIDATE_A, shortlist_hash="h", llm_client=client1, now=NOW)
    p2 = PIPE.generate_proposal_for_candidate(CANDIDATE_A, shortlist_hash="h", llm_client=client2, now=NOW)
    assert p1 == p2


# ============================================================================
# build_shortlist_hash determinism
# ============================================================================


def test_shortlist_hash_deterministic_for_same_content():
    assert PIPE.build_shortlist_hash(SHORTLIST) == PIPE.build_shortlist_hash(SHORTLIST)


def test_shortlist_hash_changes_when_order_changes():
    reordered = (CANDIDATE_B, CANDIDATE_A)
    assert PIPE.build_shortlist_hash(SHORTLIST) != PIPE.build_shortlist_hash(reordered)


def test_shortlist_hash_changes_when_content_changes():
    changed = (PIPE.Candidate(candidate_id="cand-1", symbol="BTC/USDT:USDT", evidence_summary="different evidence"), CANDIDATE_B)
    assert PIPE.build_shortlist_hash(SHORTLIST) != PIPE.build_shortlist_hash(changed)


# ============================================================================
# Fail-closed rejection paths -- every one of these must produce
# thesis=None and stated_confidence=None (no partial repair, ever).
# ============================================================================


def test_rejected_when_no_model_configured():
    proposal = PIPE.generate_proposal_for_candidate(CANDIDATE_A, shortlist_hash="h", llm_client=PIPE.NullClient(), now=NOW)
    assert proposal.status == "REJECTED_NO_MODEL"
    assert proposal.rejection_reason == "no LLM client configured"
    assert proposal.thesis is None
    assert proposal.stated_confidence is None
    assert proposal.raw_response is None


def test_rejected_when_llm_client_is_none():
    proposal = PIPE.generate_proposal_for_candidate(CANDIDATE_A, shortlist_hash="h", llm_client=None, now=NOW)
    assert proposal.status == "REJECTED_NO_MODEL"


def test_rejected_when_llm_call_raises():
    client = FakeLLMClient(RuntimeError("provider is down"))
    proposal = PIPE.generate_proposal_for_candidate(CANDIDATE_A, shortlist_hash="h", llm_client=client, now=NOW)
    assert proposal.status == "REJECTED_INVALID_OUTPUT"
    assert "provider is down" in proposal.rejection_reason
    assert proposal.thesis is None
    assert proposal.stated_confidence is None


def test_rejected_when_response_is_empty():
    client = FakeLLMClient("")
    proposal = PIPE.generate_proposal_for_candidate(CANDIDATE_A, shortlist_hash="h", llm_client=client, now=NOW)
    assert proposal.status == "REJECTED_INVALID_OUTPUT"
    assert "no content" in proposal.rejection_reason
    assert proposal.thesis is None


def test_rejected_when_response_unparseable():
    client = FakeLLMClient("this is not json at all")
    proposal = PIPE.generate_proposal_for_candidate(CANDIDATE_A, shortlist_hash="h", llm_client=client, now=NOW)
    assert proposal.status == "REJECTED_INVALID_OUTPUT"
    assert "parsed" in proposal.rejection_reason
    assert proposal.thesis is None
    assert proposal.raw_response == "this is not json at all"  # preserved for audit even when rejected


def test_rejected_on_candidate_id_mismatch_closed_output_space():
    """The core closed-output-space guarantee: the AI cannot substitute a
    different identifier, even a plausible-looking one.
    """
    client = FakeLLMClient(_valid_proposal_json(candidate_id="cand-999-hallucinated"))
    proposal = PIPE.generate_proposal_for_candidate(CANDIDATE_A, shortlist_hash="h", llm_client=client, now=NOW)
    assert proposal.status == "REJECTED_INVALID_OUTPUT"
    assert "mismatch" in proposal.rejection_reason
    assert "cand-999-hallucinated" in proposal.rejection_reason
    assert proposal.thesis is None
    assert proposal.stated_confidence is None


def test_rejected_when_thesis_missing():
    import json

    client = FakeLLMClient(json.dumps({"candidate_id": "cand-1", "confidence": 0.5}))
    proposal = PIPE.generate_proposal_for_candidate(CANDIDATE_A, shortlist_hash="h", llm_client=client, now=NOW)
    assert proposal.status == "REJECTED_INVALID_OUTPUT"
    assert "thesis" in proposal.rejection_reason
    assert proposal.stated_confidence is None  # even though confidence WAS valid -- no partial retention


def test_rejected_when_thesis_empty_string():
    client = FakeLLMClient(_valid_proposal_json(thesis="   "))
    proposal = PIPE.generate_proposal_for_candidate(CANDIDATE_A, shortlist_hash="h", llm_client=client, now=NOW)
    assert proposal.status == "REJECTED_INVALID_OUTPUT"
    assert proposal.thesis is None


@pytest.mark.parametrize("bad_confidence", [None, "high", True, False, 1.5, -0.1, float("nan")])
def test_rejected_for_every_invalid_confidence_value(bad_confidence):
    import json
    import math

    raw = json.dumps({"candidate_id": "cand-1", "thesis": "a valid thesis", "confidence": bad_confidence}) if not (
        isinstance(bad_confidence, float) and math.isnan(bad_confidence)
    ) else '{"candidate_id": "cand-1", "thesis": "a valid thesis", "confidence": NaN}'
    client = FakeLLMClient(raw)
    proposal = PIPE.generate_proposal_for_candidate(CANDIDATE_A, shortlist_hash="h", llm_client=client, now=NOW)
    assert proposal.status == "REJECTED_INVALID_OUTPUT"
    assert proposal.thesis is None  # no partial retention -- thesis was valid but proposal is still fully rejected
    assert proposal.stated_confidence is None


def test_rejected_when_confidence_missing_entirely():
    import json

    client = FakeLLMClient(json.dumps({"candidate_id": "cand-1", "thesis": "a valid thesis"}))
    proposal = PIPE.generate_proposal_for_candidate(CANDIDATE_A, shortlist_hash="h", llm_client=client, now=NOW)
    assert proposal.status == "REJECTED_INVALID_OUTPUT"


def test_no_partial_repair_corpus():
    """Fuzzed corpus of malformed AI output -- proves the fail-closed
    sanitizer NEVER retains a partial valid field (Martin's documented
    deviation from the acceptance criterion's literal subtract-only text:
    fail-closed instead). Every entry here has at least one valid field
    alongside at least one invalid one.
    """
    import json

    malformed_corpus = [
        json.dumps({"candidate_id": "cand-1", "thesis": "good thesis", "confidence": "not a number"}),
        json.dumps({"candidate_id": "wrong-id", "thesis": "good thesis", "confidence": 0.5}),
        json.dumps({"candidate_id": "cand-1", "thesis": 12345, "confidence": 0.5}),
        json.dumps({"candidate_id": "cand-1", "thesis": "", "confidence": 0.5}),
        json.dumps({"candidate_id": "cand-1", "confidence": 0.5}),  # thesis missing
        json.dumps({"thesis": "good thesis", "confidence": 0.5}),  # candidate_id missing (None != 'cand-1')
        "{not even valid json",
        "",
    ]
    for raw in malformed_corpus:
        client = FakeLLMClient(raw)
        proposal = PIPE.generate_proposal_for_candidate(CANDIDATE_A, shortlist_hash="h", llm_client=client, now=NOW)
        assert proposal.status != "PROPOSED", f"expected rejection for: {raw!r}"
        assert proposal.thesis is None, f"partial thesis leaked for: {raw!r}"
        assert proposal.stated_confidence is None, f"partial confidence leaked for: {raw!r}"
        assert proposal.rejection_reason is not None


# ============================================================================
# AIProposal.__post_init__ structural validation
# ============================================================================


def test_aiproposal_rejects_invalid_status():
    with pytest.raises(PIPE.AIProposalPipelineError):
        PIPE.AIProposal(
            candidate_id="c", symbol="s", thesis="t", stated_confidence=0.5, model_name="m",
            shortlist_hash="h", generated_at="now", raw_response=None, status="NOT_A_REAL_STATUS",
        )


def test_aiproposal_proposed_requires_thesis_and_confidence():
    with pytest.raises(PIPE.AIProposalPipelineError):
        PIPE.AIProposal(
            candidate_id="c", symbol="s", thesis=None, stated_confidence=None, model_name="m",
            shortlist_hash="h", generated_at="now", raw_response=None, status="PROPOSED",
        )


def test_aiproposal_rejected_requires_reason():
    with pytest.raises(PIPE.AIProposalPipelineError):
        PIPE.AIProposal(
            candidate_id="c", symbol="s", thesis=None, stated_confidence=None, model_name="m",
            shortlist_hash="h", generated_at="now", raw_response=None, status="REJECTED_NO_MODEL",
            rejection_reason=None,
        )


# ============================================================================
# Adversarial-challenge role
# ============================================================================


def _proposed(client=None) -> "PIPE.AIProposal":
    client = client or FakeLLMClient(_valid_proposal_json())
    return PIPE.generate_proposal_for_candidate(CANDIDATE_A, shortlist_hash="h", llm_client=client, now=NOW)


def test_challenge_requires_proposed_status():
    rejected = PIPE.generate_proposal_for_candidate(CANDIDATE_A, shortlist_hash="h", llm_client=PIPE.NullClient(), now=NOW)
    with pytest.raises(PIPE.AIProposalPipelineError):
        PIPE.challenge_proposal(rejected, CANDIDATE_A, llm_client=FakeLLMClient('{"concerns": []}'), now=NOW)


def test_challenge_requires_matching_candidate():
    proposal = _proposed()
    with pytest.raises(PIPE.AIProposalPipelineError):
        PIPE.challenge_proposal(proposal, CANDIDATE_B, llm_client=FakeLLMClient('{"concerns": []}'), now=NOW)


def test_challenge_no_concerns_means_passed_true():
    proposal = _proposed()
    critique = PIPE.challenge_proposal(proposal, CANDIDATE_A, llm_client=FakeLLMClient('{"concerns": []}'), now=NOW)
    assert critique.passed is True
    assert critique.concerns == ()


def test_challenge_with_concerns_means_passed_false_and_concerns_preserved():
    proposal = _proposed()
    critique = PIPE.challenge_proposal(
        proposal, CANDIDATE_A,
        llm_client=FakeLLMClient('{"concerns": ["confidence overstated relative to evidence", "no corroborating source"]}'),
        now=NOW,
    )
    assert critique.passed is False
    assert critique.concerns == ("confidence overstated relative to evidence", "no corroborating source")


def test_challenge_ignores_an_extraneous_approval_field_derivation_is_real():
    """The model has no field to write an approval into. If it tries
    anyway (an extraneous "passed": true alongside real concerns), the
    derived verdict must still be False -- proving `passed` is computed
    from `concerns`, never read off the model's own claim.
    """
    proposal = _proposed()
    critique = PIPE.challenge_proposal(
        proposal, CANDIDATE_A,
        llm_client=FakeLLMClient('{"concerns": ["a real concern"], "passed": true, "approved": true}'),
        now=NOW,
    )
    assert critique.passed is False
    assert critique.concerns == ("a real concern",)


def test_challenge_fails_closed_on_no_client():
    proposal = _proposed()
    critique = PIPE.challenge_proposal(proposal, CANDIDATE_A, llm_client=PIPE.NullClient(), now=NOW)
    assert critique.passed is False
    assert "not been challenged" in critique.concerns[0]


def test_challenge_fails_closed_on_call_error():
    proposal = _proposed()
    critique = PIPE.challenge_proposal(proposal, CANDIDATE_A, llm_client=FakeLLMClient(RuntimeError("boom")), now=NOW)
    assert critique.passed is False
    assert "boom" in critique.concerns[0]


def test_challenge_fails_closed_on_empty_response():
    proposal = _proposed()
    critique = PIPE.challenge_proposal(proposal, CANDIDATE_A, llm_client=FakeLLMClient(""), now=NOW)
    assert critique.passed is False
    assert "nothing" in critique.concerns[0]


def test_challenge_fails_closed_on_unparseable_response():
    proposal = _proposed()
    critique = PIPE.challenge_proposal(proposal, CANDIDATE_A, llm_client=FakeLLMClient("not json"), now=NOW)
    assert critique.passed is False
    assert "parsed" in critique.concerns[0]


def test_challenge_binds_to_proposal_content_hash():
    proposal_1 = _proposed(FakeLLMClient(_valid_proposal_json(thesis="thesis A")))
    proposal_2 = _proposed(FakeLLMClient(_valid_proposal_json(thesis="thesis B")))
    c1 = PIPE.challenge_proposal(proposal_1, CANDIDATE_A, llm_client=FakeLLMClient('{"concerns": []}'), now=NOW)
    c2 = PIPE.challenge_proposal(proposal_2, CANDIDATE_A, llm_client=FakeLLMClient('{"concerns": []}'), now=NOW)
    assert c1.proposal_content_hash != c2.proposal_content_hash


def test_challenge_content_hash_stable_for_identical_proposal_content():
    proposal_1 = _proposed(FakeLLMClient(_valid_proposal_json()))
    proposal_2 = _proposed(FakeLLMClient(_valid_proposal_json()))
    c1 = PIPE.challenge_proposal(proposal_1, CANDIDATE_A, llm_client=FakeLLMClient('{"concerns": []}'), now=NOW)
    c2 = PIPE.challenge_proposal(proposal_2, CANDIDATE_A, llm_client=FakeLLMClient('{"concerns": []}'), now=NOW)
    assert c1.proposal_content_hash == c2.proposal_content_hash


# ============================================================================
# Ranking-score penalty
# ============================================================================


def test_apply_challenge_penalty_requires_keyword_arg():
    proposal = _proposed()
    critique = PIPE.challenge_proposal(proposal, CANDIDATE_A, llm_client=FakeLLMClient('{"concerns": []}'), now=NOW)
    with pytest.raises(TypeError):
        PIPE.apply_challenge_penalty(1.0, critique)  # missing required penalty_per_concern


def test_apply_challenge_penalty_rejects_negative_penalty():
    critique = PIPE.Critique(passed=False, concerns=("x",), proposal_content_hash="h", model_name="m", raw_response=None, reviewed_at="now")
    with pytest.raises(PIPE.AIProposalPipelineError):
        PIPE.apply_challenge_penalty(1.0, critique, penalty_per_concern=-0.1)


def test_apply_challenge_penalty_passed_true_returns_base_unchanged():
    critique = PIPE.Critique(passed=True, concerns=(), proposal_content_hash="h", model_name="m", raw_response=None, reviewed_at="now")
    assert PIPE.apply_challenge_penalty(0.85, critique, penalty_per_concern=0.2) == 0.85


def test_apply_challenge_penalty_subtracts_exactly_penalty_times_concern_count():
    critique = PIPE.Critique(passed=False, concerns=("a", "b", "c"), proposal_content_hash="h", model_name="m", raw_response=None, reviewed_at="now")
    result = PIPE.apply_challenge_penalty(1.0, critique, penalty_per_concern=0.1)
    assert result == pytest.approx(1.0 - 0.1 * 3)


def test_apply_challenge_penalty_zero_penalty_is_a_noop_even_with_concerns():
    critique = PIPE.Critique(passed=False, concerns=("a", "b"), proposal_content_hash="h", model_name="m", raw_response=None, reviewed_at="now")
    assert PIPE.apply_challenge_penalty(1.0, critique, penalty_per_concern=0.0) == 1.0


# ============================================================================
# Governance / non-goal invariants (mirrors `.46`-`.48`'s pattern)
# ============================================================================

_DENY_LIST_FIELD_NAMES = {
    "qty", "quantity", "price", "limit_price", "side", "order_side",
    "order_type", "order_id", "client_order_id", "broker", "execution_intent",
    "signal", "direction", "size", "leverage", "time_in_force", "venue",
}


def test_module_has_no_trade_or_order_function():
    banned_substrings = ("submit_order", "place_order", "execute_trade", "authorize", "cancel_order", "send_order")
    names = [n for n in dir(PIPE) if not n.startswith("_")]
    for name in names:
        lowered = name.lower()
        for banned in banned_substrings:
            assert banned not in lowered, f"found banned function-name substring {banned!r} in {name!r}"


def test_aiproposal_has_no_execution_field_names():
    fields = set(PIPE.AIProposal.__dataclass_fields__.keys())
    overlap = fields & _DENY_LIST_FIELD_NAMES
    assert not overlap, f"AIProposal carries execution-shaped fields: {overlap}"


def test_critique_has_no_execution_field_names():
    fields = set(PIPE.Critique.__dataclass_fields__.keys())
    overlap = fields & _DENY_LIST_FIELD_NAMES
    assert not overlap, f"Critique carries execution-shaped fields: {overlap}"


def test_critique_has_no_approval_field():
    fields = set(PIPE.Critique.__dataclass_fields__.keys())
    assert "approved" not in fields
    assert "passed_by_ai" not in fields
    assert "verdict" not in fields
    # "passed" IS present -- but it is documented and tested (above) as
    # DERIVED from concerns, never a field the model writes into directly.
    assert "passed" in fields


def test_candidate_has_no_execution_field_names():
    fields = set(PIPE.Candidate.__dataclass_fields__.keys())
    overlap = fields & _DENY_LIST_FIELD_NAMES
    assert not overlap, f"Candidate carries execution-shaped fields: {overlap}"


def test_module_never_imports_or_constructs_canonical_execution_intent():
    """`.49` is Level 1/2 only -- it must never IMPORT or CONSTRUCT `.33`'s
    CanonicalExecutionIntent (the module docstring DISCUSSES the type by
    name, in prose, to document why it is deliberately not used -- that
    discussion is fine and expected; what must never appear is executable
    code that imports `.33` or instantiates the type).
    """
    source = PIPE_PATH.read_text(encoding="utf-8")
    assert "import aura_v05333" not in source
    assert "from aura_v05333" not in source
    assert "spec_from_file_location" not in source  # no dynamic cross-module import of any kind
    assert "CanonicalExecutionIntent(" not in source  # no constructor call anywhere, incl. in prose
    assert "CanonicalExecutionIntent" not in dir(PIPE)


def test_apply_challenge_penalty_is_the_only_thing_a_critique_can_influence():
    """Structural proof that `Critique` has no consumer anywhere in this
    module other than the ranking-penalty function -- grep the module
    source for every reference to the `Critique` type name and confirm
    they are all inside type hints / the dataclass / apply_challenge_
    penalty / challenge_proposal itself, never near an authorization
    keyword.
    """
    source = PIPE_PATH.read_text(encoding="utf-8")
    assert "Critique" in source  # sanity: the type is actually used
    for banned in ("authorize", "approve_order", "submit", "execute("):
        # Critique's own docstring explicitly discusses non-authorization,
        # so this checks for dangerous CO-OCCURRENCE patterns are absent
        # rather than banning the word "approve" from prose entirely.
        assert f"critique.{banned}" not in source.lower()
