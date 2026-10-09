#!/usr/bin/env python3
"""Fixes a real deployment-blocking bug in 3 pre-existing institutional
files: `institutional/options_o10/signal_mapping.py`,
`institutional/track_b/short_protective_gates.py`, and
`../mexc_bot/institutional/track_a/conftest.py`.

ROOT CAUSE
------------------------------------------------------------------------
All three files were built and tested in Claude's own cloud sandbox,
where the real AURA repos happen to be staged at a fixed path
(`/mnt/user-data/uploads/AI automated trading/...`). Each file's
dependency-resolution fallback was hardcoded to that sandbox-only path
instead of the `AURA_CORE_DIR` environment variable this build already
establishes as the real-deployment convention (see
`portfolio/macro_buckets.py`'s `_find_aura_core_dir`, and the earlier
`setx AURA_CORE_DIR ...` step of this deployment checklist). On a real
machine that path does not exist, so:

  - `institutional/options_o10/signal_mapping.py` raised
    `RuntimeError: MISSING_DEPENDENCY:aura_v05373_options_instrument_
    metadata.py not found ...` (confirmed live on Martin's machine,
    2026-10-09) -- it never checked `AURA_CORE_DIR` at all.
  - `institutional/track_b/short_protective_gates.py` raised
    `ModuleNotFoundError: No module named 'aura_v05368_earnings_
    blackout_gate'` (confirmed live on Martin's machine, 2026-10-09) --
    it checked an `AURA_STAGED_REPO_DIR` env var that nothing in this
    build ever sets; `AURA_CORE_DIR` is the one that is actually set.
  - `../mexc_bot/institutional/track_a/conftest.py` would fail the
    same way (not yet observed live, pre-empted here) -- it hardcoded
    the sandbox's mexc_bot path with no env var fallback at all.

THE FIX
------------------------------------------------------------------------
Each file gets an additional/corrected candidate that resolves against
`AURA_CORE_DIR` (the env var already set on this machine), matching the
pattern the rest of this build already uses. The hardcoded sandbox path
in each file is left in place as a final fallback (so Claude's own
sandbox keeps working out of the box) -- this only ADDS the real
resolution path in front of it; nothing existing is removed.

Same anchor-verified design as `apply_institutional_hooks_2026-10-09.py`:
every patch searches for an exact, byte-for-byte snippet of the file's
OWN real current content and only proceeds if every anchor for a given
file matches exactly once. No anchor match -> that file is skipped,
all other files still attempted independently.

USAGE -- run from the `aura-autonomous-trading-agent` repo root (same
place `apply_institutional_hooks_2026-10-09.py` was run from):
    python apply_institutional_hooks_2026-10-09.py --dry-run   # (that script, already run)
    python fix_staged_path_fallbacks_2026-10-09.py --dry-run
    python fix_staged_path_fallbacks_2026-10-09.py             # applies for real
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# ---------------------------------------------------------------------
# institutional/options_o10/signal_mapping.py
# ---------------------------------------------------------------------
SIGNAL_MAPPING_PATCHES = [
    (
        "add `import os`",
        "import importlib.util\nimport logging\nimport sys\nimport uuid\n",
        "import importlib.util\nimport logging\nimport os\nimport sys\nimport uuid\n",
    ),
    (
        "add AURA_CORE_DIR candidate to _load_staged_module",
        '    if module_name in sys.modules:\n'
        '        return sys.modules[module_name]\n'
        '    try:\n'
        '        return importlib.import_module(module_name)\n'
        '    except ImportError:\n'
        '        pass\n'
        '\n'
        '    candidates = [\n'
        '        Path(__file__).resolve().parent / filename,\n'
        '        _STAGED_REFERENCE_DIR / filename,\n'
        '    ]\n'
        '    for path in candidates:\n'
        '        if path.is_file():\n'
        '            spec = importlib.util.spec_from_file_location(module_name, path)\n'
        '            if spec is None or spec.loader is None:\n'
        '                continue\n'
        '            module = importlib.util.module_from_spec(spec)\n'
        '            sys.modules[module_name] = module\n'
        '            spec.loader.exec_module(module)\n'
        '            return module\n'
        '    raise RuntimeError(\n'
        '        f"MISSING_DEPENDENCY:{filename} not found on sys.path, next to this file, "\n'
        '        f"or in the staged reference directory {_STAGED_REFERENCE_DIR!s}"\n'
        '    )\n',
        '    if module_name in sys.modules:\n'
        '        return sys.modules[module_name]\n'
        '    try:\n'
        '        return importlib.import_module(module_name)\n'
        '    except ImportError:\n'
        '        pass\n'
        '\n'
        '    candidates = [Path(__file__).resolve().parent / filename]\n'
        '    _env_core_dir = os.environ.get("AURA_CORE_DIR")\n'
        '    if _env_core_dir:\n'
        '        candidates.append(Path(_env_core_dir) / filename)\n'
        '    candidates.append(_STAGED_REFERENCE_DIR / filename)\n'
        '    for path in candidates:\n'
        '        if path.is_file():\n'
        '            spec = importlib.util.spec_from_file_location(module_name, path)\n'
        '            if spec is None or spec.loader is None:\n'
        '                continue\n'
        '            module = importlib.util.module_from_spec(spec)\n'
        '            sys.modules[module_name] = module\n'
        '            spec.loader.exec_module(module)\n'
        '            return module\n'
        '    raise RuntimeError(\n'
        '        f"MISSING_DEPENDENCY:{filename} not found on sys.path, next to this "\n'
        '        f"file, via the AURA_CORE_DIR environment variable"\n'
        '        + (f" ({_env_core_dir!r})" if _env_core_dir else " (not set)")\n'
        '        + f", or in the staged reference directory {_STAGED_REFERENCE_DIR!s}"\n'
        '    )\n',
    ),
]

# ---------------------------------------------------------------------
# institutional/track_b/short_protective_gates.py
# ---------------------------------------------------------------------
SHORT_PROTECTIVE_GATES_PATCHES = [
    (
        "read AURA_CORE_DIR instead of the unused AURA_STAGED_REPO_DIR",
        'except ImportError:  # pragma: no cover -- local-testing-only fallback, see module docstring\n'
        '    _STAGED_AURA_DIR = os.environ.get(\n'
        '        "AURA_STAGED_REPO_DIR",\n'
        '        "/mnt/user-data/uploads/AI automated trading/aura-autonomous-trading-agent",\n'
        '    )\n',
        'except ImportError:  # pragma: no cover -- local-testing-only fallback, see module docstring\n'
        '    _STAGED_AURA_DIR = os.environ.get(\n'
        '        "AURA_CORE_DIR",\n'
        '        "/mnt/user-data/uploads/AI automated trading/aura-autonomous-trading-agent",\n'
        '    )\n',
    ),
]

# ---------------------------------------------------------------------
# ../mexc_bot/institutional/track_a/conftest.py
# ---------------------------------------------------------------------
CONFTEST_PATCHES = [
    (
        "derive _MEXC_BOT_DIR from AURA_CORE_DIR's sibling",
        'import sys\n'
        'from pathlib import Path\n'
        '\n'
        '_TRACK_A_DIR = Path(__file__).resolve().parent\n'
        '_MEXC_BOT_DIR = Path("/mnt/user-data/uploads/AI automated trading/mexc_bot")\n'
        '\n'
        'for p in (_TRACK_A_DIR, _MEXC_BOT_DIR):\n',
        'import os\n'
        'import sys\n'
        'from pathlib import Path\n'
        '\n'
        '_TRACK_A_DIR = Path(__file__).resolve().parent\n'
        '\n'
        '_env_core_dir = os.environ.get("AURA_CORE_DIR")\n'
        'if _env_core_dir and (Path(_env_core_dir).resolve().parent / "mexc_bot").is_dir():\n'
        '    _MEXC_BOT_DIR = Path(_env_core_dir).resolve().parent / "mexc_bot"\n'
        'else:\n'
        '    _MEXC_BOT_DIR = Path("/mnt/user-data/uploads/AI automated trading/mexc_bot")\n'
        '\n'
        'for p in (_TRACK_A_DIR, _MEXC_BOT_DIR):\n',
    ),
]

FILES = [
    ("institutional/options_o10/signal_mapping.py", SIGNAL_MAPPING_PATCHES),
    ("institutional/track_b/short_protective_gates.py", SHORT_PROTECTIVE_GATES_PATCHES),
    ("../mexc_bot/institutional/track_a/conftest.py", CONFTEST_PATCHES),
]


def check_and_apply(relpath: str, patches, dry_run: bool) -> bool:
    path = ROOT / relpath
    print(f"\n--- {relpath} ---")
    if not path.is_file():
        print(f"  [FILE NOT FOUND] {path}")
        return False

    text = path.read_text(encoding="utf-8")
    all_ok = True
    for label, anchor, _replacement in patches:
        count = text.count(anchor)
        if count == 1:
            print(f"  [match]     {label}")
        elif count == 0:
            print(f"  [NOT FOUND] {label}")
            all_ok = False
        else:
            print(f"  [AMBIGUOUS] {label} ({count} occurrences)")
            all_ok = False

    if not all_ok:
        print(f"  => SKIPPING {relpath} (not all anchors matched exactly once)")
        return False

    if dry_run:
        print(f"  => would patch {relpath} ({len(patches)} change(s))")
        return True

    new_text = text
    for _label, anchor, replacement in patches:
        new_text = new_text.replace(anchor, replacement, 1)

    backup = path.with_suffix(path.suffix + f".bak-{datetime.now():%Y%m%d-%H%M%S}")
    backup.write_text(text, encoding="utf-8")
    path.write_text(new_text, encoding="utf-8")
    print(f"  => PATCHED {relpath} (backup: {backup.name})")
    return True


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    print(f"Mode: {'DRY RUN (no files written)' if dry_run else 'APPLY'}")
    results = {relpath: check_and_apply(relpath, patches, dry_run) for relpath, patches in FILES}

    print("\n=== SUMMARY ===")
    for relpath, ok in results.items():
        print(f"  {'OK' if ok else 'FAILED'}: {relpath}")

    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
