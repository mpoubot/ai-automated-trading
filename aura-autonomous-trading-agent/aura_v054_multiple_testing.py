"""
AURA v0.5.4 -- generic multiple-testing / null-model significance library.

Carried over UNCHANGED (logic-wise) from the cloud-sandbox prototype built
2026-10-08 (`strategy_validation_platform/validation/multiple_testing.py`),
which itself extracted and generalized these exact techniques -- IID
permutation null, block-bootstrap null, Bonferroni correction,
Benjamini-Hochberg FDR, percentile-bootstrap confidence intervals, and a
selection-aware max-statistic test -- from
`caura/aura_v0522_multiple_testing.py` (AURA v0.5.2.2), which welded them
to one frozen, single-candidate research script (hardcoded 8-cell
BTC/ETH regime scheme, a specific ledger CSV schema, one named
candidate). That script is left completely untouched, per the
Preservation Rule (ARCHITECTURE_APPROVAL_BASELINE_2026-09-22.md section 8).

This module is 100% interface-agnostic -- it operates on any (value,
group_label) pairs and any family of group keys, regardless of whether
those values come from Track A (MEXC crypto) or Track B (Alpaca
equities). Confirmed this session: it transfers to Track B with zero
logic changes, only this docstring. It is used by
`aura_v054_permutation_test.py` (same directory) to attach a
Monte-Carlo empirical p-value to real-vs-random-timing comparisons, and
is equally usable later for a selection-aware multiple-candidate gate
once more than one Track B signal source is being compared at once.

RESEARCH-ONLY: pure statistics over whatever values/labels the caller
supplies. Nothing here touches live trading, orders, or account state.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

import numpy as np


# ---------------------------------------------------------------------------
# Core statistics -- verbatim logic from aura_v0522_multiple_testing.py,
# generalized only where noted (no hardcoded cell/candidate names).
# ---------------------------------------------------------------------------

def empirical_p(exceedances: int, iterations: int) -> float:
    """Conservative Monte Carlo p-value: (exceedances + 1) / (iterations + 1).
    Verbatim from aura_v0522_multiple_testing.py's empirical_p."""
    return (float(exceedances) + 1.0) / (float(iterations) + 1.0)


def bonferroni_adjust(p_values: Mapping[str, float], family_size: int | None = None) -> dict[str, float]:
    """p * family_size, capped at 1.0.

    family_size defaults to len(p_values); the original script always
    passed the fixed N_CELLS=8 for its one 8-cell family. A caller testing
    a differently-sized hypothesis family passes that size explicitly so
    the correction reflects the true number of hypotheses tested, not just
    how many happen to be in this particular dict.
    """
    n = family_size if family_size is not None else len(p_values)
    return {k: min(1.0, float(v) * n) for k, v in p_values.items()}


def bh_adjust(p_values: Mapping[str, float]) -> dict[str, float]:
    """Benjamini-Hochberg q-values for a finite family of one-sided
    p-values. Verbatim logic from aura_v0522_multiple_testing.py's
    bh_adjust, generalized only in that it no longer assumes any
    particular set of keys."""
    keys = list(p_values.keys())
    finite = [(k, float(v)) for k, v in p_values.items() if np.isfinite(v)]
    finite.sort(key=lambda kv: kv[1])
    m = len(finite)
    q: dict[str, float] = {k: float("nan") for k in keys}
    running = 1.0
    for rank in range(m, 0, -1):
        k, p = finite[rank - 1]
        value = min(running, p * m / rank)
        q[k] = value
        running = value
    return q


def wilson_ci(k: int, n: int, z: float = 1.959963984540054) -> tuple[float, float]:
    """95%-by-default Wilson score interval for a hit rate k/n.
    Verbatim from aura_v0522_multiple_testing.py's wilson_ci."""
    if n <= 0:
        return float("nan"), float("nan")
    p = k / n
    den = 1.0 + z * z / n
    center = (p + z * z / (2.0 * n)) / den
    half = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * n)) / n) / den
    return max(0.0, center - half), min(1.0, center + half)


def bootstrap_ci(
    values: Sequence[float],
    iterations: int = 5000,
    seed: int = 52202,
    quantiles: tuple[float, float] = (0.025, 0.975),
) -> tuple[float, float]:
    """Percentile bootstrap CI of the mean.

    Verbatim logic from aura_v0522_multiple_testing.py's bootstrap_ci,
    generalized only in that the quantile pair is now a parameter instead
    of hardcoded to the 95% interval.
    """
    x = np.asarray(values, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n == 0:
        return float("nan"), float("nan")
    if n == 1:
        return float(x[0]), float(x[0])
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(iterations, n))
    means = x[idx].mean(axis=1)
    return float(np.quantile(means, quantiles[0])), float(np.quantile(means, quantiles[1]))


def moving_block_sample(values: Sequence[float], n: int, block_length: int, rng: np.random.Generator) -> np.ndarray:
    """Build a length-n series from circular contiguous blocks of `values`.
    Verbatim from aura_v0522_multiple_testing.py's moving_block_sample."""
    x = np.asarray(values, dtype=float)
    N = len(x)
    if N == 0:
        return np.array([], dtype=float)
    L = max(1, min(int(block_length), N))
    out = np.empty(n, dtype=float)
    pos = 0
    starts = np.arange(N)
    while pos < n:
        start = int(rng.choice(starts))
        take = min(L, n - pos)
        idx = (start + np.arange(take)) % N
        out[pos:pos + take] = x[idx]
        pos += take
    return out


# ---------------------------------------------------------------------------
# Null models -- generalized form of run_iid_null / run_block_null: any
# group_keys instead of the fixed 8-cell BTC/ETH regime scheme.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NullModelResult:
    """Generalized form of aura_v0522_multiple_testing.py's run_iid_null /
    run_block_null return dict."""

    group_pvalues: dict[str, float]
    max_pvalue: float
    max_exceedances: int
    group_exceedances: dict[str, int]
    group_means: np.ndarray  # shape (iterations, n_groups)
    max_means: np.ndarray  # shape (iterations,)


def _run_null(
    values: np.ndarray,
    labels: np.ndarray,
    group_keys: Sequence[str],
    iterations: int,
    seed: int,
    resample: Callable[[np.ndarray, np.random.Generator], np.ndarray],
) -> NullModelResult:
    masks = [labels == g for g in group_keys]
    observed = {
        g: (float(np.mean(values[m])) if m.any() else float("nan")) for g, m in zip(group_keys, masks)
    }
    finite_observed = [v for v in observed.values() if np.isfinite(v)]
    if not finite_observed:
        raise ValueError("No group has any observations with a finite value.")
    max_observed = max(finite_observed)

    exceed = {g: 0 for g in group_keys}
    max_exceed = 0
    rng = np.random.default_rng(seed)
    group_means = np.empty((iterations, len(group_keys)), dtype=float)
    max_means = np.empty(iterations, dtype=float)

    for i in range(iterations):
        sample = resample(values, rng)
        vals = np.array(
            [(float(np.mean(sample[m])) if m.any() else float("nan")) for m in masks], dtype=float
        )
        group_means[i, :] = vals
        finite_vals = vals[np.isfinite(vals)]
        mx = float(np.max(finite_vals)) if len(finite_vals) else float("nan")
        max_means[i] = mx
        if np.isfinite(mx):
            max_exceed += int(mx >= max_observed)
        for j, g in enumerate(group_keys):
            if np.isfinite(vals[j]) and vals[j] >= observed[g]:
                exceed[g] += 1

    pvals = {g: empirical_p(exceed[g], iterations) for g in group_keys}
    max_p = empirical_p(max_exceed, iterations)
    return NullModelResult(
        group_pvalues=pvals,
        max_pvalue=max_p,
        max_exceedances=max_exceed,
        group_exceedances=exceed,
        group_means=group_means,
        max_means=max_means,
    )


def iid_permutation_null(
    values: Sequence[float], labels: Sequence[str], group_keys: Sequence[str], iterations: int, seed: int
) -> NullModelResult:
    """Randomly reassign `values` to the frozen `labels` membership,
    breaking the value/label association while preserving exact group
    sizes and the exact empirical value distribution.

    Generalized form of aura_v0522_multiple_testing.py's run_iid_null --
    identical mechanics, any group_keys instead of the fixed 8 regime cells.
    """
    values_arr = np.asarray(values, dtype=float)
    labels_arr = np.asarray(labels, dtype=object)

    def resample(x: np.ndarray, rng: np.random.Generator) -> np.ndarray:
        return rng.permutation(x)

    return _run_null(values_arr, labels_arr, group_keys, iterations, seed, resample)


def block_bootstrap_null(
    values: Sequence[float],
    labels: Sequence[str],
    group_keys: Sequence[str],
    iterations: int,
    seed: int,
    block_length: int,
) -> NullModelResult:
    """Chronological block-resampling null: labels stay attached to their
    original positions; only the value sequence is resampled in contiguous
    circular blocks, preserving local serial structure while breaking the
    label/outcome association.

    Generalized form of aura_v0522_multiple_testing.py's run_block_null.
    """
    values_arr = np.asarray(values, dtype=float)
    labels_arr = np.asarray(labels, dtype=object)

    def resample(x: np.ndarray, rng: np.random.Generator) -> np.ndarray:
        return moving_block_sample(x, len(x), block_length, rng)

    return _run_null(values_arr, labels_arr, group_keys, iterations, seed, resample)


# ---------------------------------------------------------------------------
# Reporting -- generalized form of build_multiple_testing_table /
# candidate_summary.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GroupSignificance:
    """Per-group row of the final multiple-testing table. Generalized form
    of aura_v0522_multiple_testing.py's build_multiple_testing_table output
    row."""

    group: str
    n: int
    observed_mean: float
    iid_p: float
    iid_bonferroni_p: float
    iid_bh_q: float
    block_p: float
    block_bonferroni_p: float
    block_bh_q: float


def build_significance_table(
    values: Sequence[float],
    labels: Sequence[str],
    group_keys: Sequence[str],
    iid: NullModelResult,
    block: NullModelResult,
) -> list[GroupSignificance]:
    values_arr = np.asarray(values, dtype=float)
    labels_arr = np.asarray(labels, dtype=object)
    iid_bonf = bonferroni_adjust(iid.group_pvalues, family_size=len(group_keys))
    block_bonf = bonferroni_adjust(block.group_pvalues, family_size=len(group_keys))
    iid_bh = bh_adjust(iid.group_pvalues)
    block_bh = bh_adjust(block.group_pvalues)

    rows = []
    for g in group_keys:
        mask = labels_arr == g
        x = values_arr[mask]
        rows.append(
            GroupSignificance(
                group=g,
                n=int(mask.sum()),
                observed_mean=float(np.mean(x)) if len(x) else float("nan"),
                iid_p=iid.group_pvalues[g],
                iid_bonferroni_p=iid_bonf[g],
                iid_bh_q=iid_bh[g],
                block_p=block.group_pvalues[g],
                block_bonferroni_p=block_bonf[g],
                block_bh_q=block_bh[g],
            )
        )
    return rows


def selection_aware_gate(
    candidate: str,
    table: Sequence[GroupSignificance],
    iid: NullModelResult,
    block: NullModelResult,
    alpha: float = 0.05,
) -> dict:
    """The primary, conservative multiple-testing decision: does
    `candidate` clear the selection-aware max-statistic test in BOTH null
    models.

    Generalized form of aura_v0522_multiple_testing.py's candidate_summary
    gate_pass logic -- same two-null-model-AND requirement, any candidate
    key instead of the one hardcoded "BEAR|LOW|POSITIVE" cell.
    """
    row = next((r for r in table if r.group == candidate), None)
    if row is None:
        raise KeyError(f"candidate group {candidate!r} not found in significance table")
    iid_pass = iid.max_pvalue < alpha
    block_pass = block.max_pvalue < alpha
    gate_pass = bool(iid_pass and block_pass)
    return {
        "candidate": candidate,
        "n": row.n,
        "observed_mean": row.observed_mean,
        "iid_max_statistic_p": iid.max_pvalue,
        "block_max_statistic_p": block.max_pvalue,
        "gate_pass": gate_pass,
    }
