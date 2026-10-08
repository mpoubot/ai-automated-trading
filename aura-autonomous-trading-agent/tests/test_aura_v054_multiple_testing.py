"""
Unit tests for aura_v054_multiple_testing -- the generalized multiple-
testing / null-model library. These are pure-statistics tests: no
SignalSource, no bars, no live/paper trading anywhere in this file.
Carried over unchanged from the 2026-10-08 sandbox prototype's
tests/test_multiple_testing.py (same logic, same assertions) -- this
module's content is identical except for its docstring, so the same
tests apply verbatim.
"""
from __future__ import annotations

import numpy as np
import pytest

from aura_v054_multiple_testing import (
    GroupSignificance,
    bh_adjust,
    block_bootstrap_null,
    bonferroni_adjust,
    bootstrap_ci,
    build_significance_table,
    empirical_p,
    iid_permutation_null,
    moving_block_sample,
    selection_aware_gate,
    wilson_ci,
)


def test_empirical_p_conservative_correction():
    assert empirical_p(0, 999) == pytest.approx(1.0 / 1000.0)
    assert empirical_p(999, 999) == pytest.approx(1000.0 / 1000.0)
    assert empirical_p(49, 99) == pytest.approx(50.0 / 100.0)


def test_bonferroni_adjust_caps_at_one():
    p = {"a": 0.5, "b": 0.01, "c": 0.9}
    adj = bonferroni_adjust(p, family_size=8)
    assert adj["a"] == pytest.approx(1.0)
    assert adj["b"] == pytest.approx(0.08)
    assert adj["c"] == pytest.approx(1.0)


def test_bonferroni_adjust_default_family_size_is_len():
    p = {"a": 0.1, "b": 0.2}
    adj = bonferroni_adjust(p)
    assert adj["a"] == pytest.approx(0.2)
    assert adj["b"] == pytest.approx(0.4)


def test_bh_adjust_known_example():
    p = {"w": 0.01, "x": 0.02, "y": 0.03, "z": 0.9}
    q = bh_adjust(p)
    assert q["z"] == pytest.approx(0.9)
    assert q["y"] == pytest.approx(0.04)
    assert q["x"] == pytest.approx(0.04)
    assert q["w"] == pytest.approx(0.04)


def test_bh_adjust_handles_nan():
    p = {"a": 0.01, "b": float("nan")}
    q = bh_adjust(p)
    assert np.isnan(q["b"])
    assert q["a"] == pytest.approx(0.01)


def test_wilson_ci_contains_point_estimate():
    lo, hi = wilson_ci(k=30, n=50)
    assert lo < 0.6 < hi
    assert 0.0 <= lo <= hi <= 1.0


def test_wilson_ci_empty_n():
    lo, hi = wilson_ci(k=0, n=0)
    assert np.isnan(lo) and np.isnan(hi)


def test_bootstrap_ci_brackets_sample_mean():
    rng = np.random.default_rng(7)
    data = rng.normal(loc=5.0, scale=1.0, size=200)
    lo, hi = bootstrap_ci(data, iterations=2000, seed=1)
    assert lo < float(np.mean(data)) < hi
    assert lo < hi


def test_bootstrap_ci_single_value():
    lo, hi = bootstrap_ci([3.0])
    assert lo == hi == 3.0


def test_bootstrap_ci_empty():
    lo, hi = bootstrap_ci([])
    assert np.isnan(lo) and np.isnan(hi)


def test_moving_block_sample_preserves_length_and_values():
    rng = np.random.default_rng(3)
    values = np.arange(20, dtype=float)
    sample = moving_block_sample(values, n=20, block_length=4, rng=rng)
    assert len(sample) == 20
    assert set(sample.tolist()) <= set(values.tolist())


def test_moving_block_sample_empty_input():
    rng = np.random.default_rng(1)
    assert moving_block_sample([], n=5, block_length=2, rng=rng).tolist() == []


def _planted_signal_dataset(seed: int = 123, n_per_group: int = 60):
    rng = np.random.default_rng(seed)
    group_keys = ["A", "B", "C", "D"]
    values = []
    labels = []
    for g in group_keys:
        mean = 0.02 if g == "A" else 0.0
        x = rng.normal(loc=mean, scale=0.03, size=n_per_group)
        values.extend(x.tolist())
        labels.extend([g] * n_per_group)
    return np.array(values), np.array(labels, dtype=object), group_keys


def test_iid_permutation_null_flags_planted_winner():
    values, labels, group_keys = _planted_signal_dataset()
    result = iid_permutation_null(values, labels, group_keys, iterations=2000, seed=99)
    assert result.group_pvalues["A"] < 0.05
    assert result.max_pvalue < 0.05


def test_iid_permutation_null_no_signal_gives_large_pvalues():
    rng = np.random.default_rng(55)
    group_keys = ["A", "B", "C"]
    values = rng.normal(loc=0.0, scale=0.05, size=180)
    labels = np.array(["A"] * 60 + ["B"] * 60 + ["C"] * 60, dtype=object)
    result = iid_permutation_null(values, labels, group_keys, iterations=1000, seed=42)
    assert all(p > 0.01 for p in result.group_pvalues.values())


def test_block_bootstrap_null_flags_planted_winner():
    values, labels, group_keys = _planted_signal_dataset(seed=321)
    result = block_bootstrap_null(values, labels, group_keys, iterations=2000, seed=11, block_length=5)
    assert result.group_pvalues["A"] < 0.05
    assert result.max_pvalue < 0.05


def test_build_significance_table_and_gate_pass_on_planted_signal():
    values, labels, group_keys = _planted_signal_dataset(seed=777)
    iid = iid_permutation_null(values, labels, group_keys, iterations=2000, seed=1)
    block = block_bootstrap_null(values, labels, group_keys, iterations=2000, seed=2, block_length=5)
    table = build_significance_table(values, labels, group_keys, iid, block)
    assert len(table) == len(group_keys)
    assert all(isinstance(row, GroupSignificance) for row in table)

    gate = selection_aware_gate("A", table, iid, block, alpha=0.05)
    assert gate["gate_pass"] is True
    assert gate["candidate"] == "A"


def test_selection_aware_gate_fails_for_unknown_candidate_key():
    values, labels, group_keys = _planted_signal_dataset(seed=1)
    iid = iid_permutation_null(values, labels, group_keys, iterations=500, seed=1)
    block = block_bootstrap_null(values, labels, group_keys, iterations=500, seed=2, block_length=5)
    table = build_significance_table(values, labels, group_keys, iid, block)
    with pytest.raises(KeyError):
        selection_aware_gate("NOT_A_REAL_GROUP", table, iid, block)


def test_iid_permutation_null_raises_on_all_nan_input():
    values = np.array([float("nan"), float("nan")])
    labels = np.array(["A", "A"], dtype=object)
    with pytest.raises(ValueError):
        iid_permutation_null(values, labels, ["A"], iterations=10, seed=1)
