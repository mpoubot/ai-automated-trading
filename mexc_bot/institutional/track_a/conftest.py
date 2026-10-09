"""
Test-collection setup only — not part of the production module set.

Puts this directory on sys.path (so `import data_feeds` etc. resolve) and
puts the staged mexc_bot repo on sys.path too (so `from core import
indicators` / `import core.mexc_native`, used by macro_regime_overlay.py
and data_feeds.py, resolve against the real `core` package rather than
needing a copy of it under track_a/). Mirrors how these modules would
actually be dropped into the mexc_bot repo and run from there.
"""
import os
import sys
from pathlib import Path

_TRACK_A_DIR = Path(__file__).resolve().parent

_env_core_dir = os.environ.get("AURA_CORE_DIR")
if _env_core_dir and (Path(_env_core_dir).resolve().parent / "mexc_bot").is_dir():
    _MEXC_BOT_DIR = Path(_env_core_dir).resolve().parent / "mexc_bot"
else:
    _MEXC_BOT_DIR = Path("/mnt/user-data/uploads/AI automated trading/mexc_bot")

for p in (_TRACK_A_DIR, _MEXC_BOT_DIR):
    p_str = str(p)
    if p_str not in sys.path:
        sys.path.insert(0, p_str)
