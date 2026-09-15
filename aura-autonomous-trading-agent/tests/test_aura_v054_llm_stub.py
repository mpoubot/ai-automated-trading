"""AURA v0.5.4 tests -- neutral deterministic LLM stub for .50's decide()."""

from __future__ import annotations

import json

import aura_v05349_ai_proposal_pipeline as PROPOSAL
from aura_v054_llm_stub import NeutralDeterministicLLMClient


def test_stub_always_proposes_with_matching_candidate_id():
    stub = NeutralDeterministicLLMClient()
    candidate = PROPOSAL.Candidate(candidate_id="cand-1", symbol="AAPL", evidence_summary="symbol=AAPL; no data")
    shortlist_hash = PROPOSAL.build_shortlist_hash((candidate,))
    proposal = PROPOSAL.generate_proposal_for_candidate(candidate, shortlist_hash=shortlist_hash, llm_client=stub)
    assert proposal.status == "PROPOSED"
    assert proposal.candidate_id == "cand-1"
    assert proposal.stated_confidence is not None


def test_stub_challenge_always_passes_with_zero_concerns():
    stub = NeutralDeterministicLLMClient()
    candidate = PROPOSAL.Candidate(candidate_id="cand-2", symbol="MSFT", evidence_summary="symbol=MSFT; no data")
    shortlist_hash = PROPOSAL.build_shortlist_hash((candidate,))
    proposal = PROPOSAL.generate_proposal_for_candidate(candidate, shortlist_hash=shortlist_hash, llm_client=stub)
    critique = PROPOSAL.challenge_proposal(proposal, candidate, llm_client=stub)
    assert critique.passed is True
    assert critique.concerns == ()


def test_apply_challenge_penalty_is_a_no_op_under_the_stub():
    stub = NeutralDeterministicLLMClient()
    candidate = PROPOSAL.Candidate(candidate_id="cand-3", symbol="GOOGL", evidence_summary="symbol=GOOGL; no data")
    shortlist_hash = PROPOSAL.build_shortlist_hash((candidate,))
    proposal = PROPOSAL.generate_proposal_for_candidate(candidate, shortlist_hash=shortlist_hash, llm_client=stub)
    critique = PROPOSAL.challenge_proposal(proposal, candidate, llm_client=stub)
    base_score = 0.42
    final_score = PROPOSAL.apply_challenge_penalty(base_score, critique, penalty_per_concern=0.2)
    assert final_score == base_score  # ai_penalty_per_concern structurally never fires


def test_stub_is_deterministic_across_repeated_calls():
    stub = NeutralDeterministicLLMClient()
    candidate = PROPOSAL.Candidate(candidate_id="cand-4", symbol="TSLA", evidence_summary="symbol=TSLA; no data")
    shortlist_hash = PROPOSAL.build_shortlist_hash((candidate,))
    p1 = PROPOSAL.generate_proposal_for_candidate(candidate, shortlist_hash=shortlist_hash, llm_client=stub)
    p2 = PROPOSAL.generate_proposal_for_candidate(candidate, shortlist_hash=shortlist_hash, llm_client=stub)
    assert p1.thesis == p2.thesis
    assert p1.stated_confidence == p2.stated_confidence
