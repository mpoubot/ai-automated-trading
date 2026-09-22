"""
tests/conftest.py

Puts the repo root on sys.path (so `import core...` / `import strategies...`
/ `import research...` work when pytest is run from the repo root or
anywhere else) and provides the shared `dataset_root` fixture used by the
two tests that must read the REAL frozen dataset
(test_dataset_loading.py, test_dataset_validation.py).

Dataset location resolution mirrors research/run_first_experiment.py:
defaults to the sibling-folder path (../mexc_bot/data/...), overridable via
the AURA_DATASET_ROOT environment variable for the cloud-sandbox
development/testing environment.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_DATASET_ROOT = REPO_ROOT.parent / "mexc_bot" / "data" / "native_v2_full720day_20260917"
DATASET_ROOT = Path(os.environ.get("AURA_DATASET_ROOT", str(DEFAULT_DATASET_ROOT)))


@pytest.fixture(scope="session")
def dataset_root() -> Path:
    if not DATASET_ROOT.exists():
        pytest.skip(f"Real frozen dataset not found at {DATASET_ROOT} "
                     f"(set AURA_DATASET_ROOT to override)")
    return DATASET_ROOT
