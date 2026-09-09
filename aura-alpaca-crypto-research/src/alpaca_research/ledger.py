from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .config import ROOT


def append_event(event: dict) -> Path:
    """Append a research event to a local JSONL ledger."""
    out = ROOT / "data" / "outputs"
    out.mkdir(parents=True, exist_ok=True)
    path = out / "research_ledger.jsonl"
    record = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        **event,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True, default=str) + "\n")
    return path
