"""
Asset Selection & Executability Experiment V1
Observation-only recorder.

This module deliberately does NOT contain trading rules.
It creates stable IDs so entries/exits can be linked without FIFO guessing.
"""

from __future__ import annotations
import csv
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


FIELDS = [
    "event_id", "timestamp", "event_type", "signal_id", "position_id",
    "symbol", "side", "price", "quantity", "risk_usd", "leverage",
    "signal_strength", "reason",
    "market_cap_rank", "market_cap_usd", "volume_24h_usd",
    "volume_mcap_pct", "bid", "ask", "spread_bps",
    "depth_5bps_usd", "depth_10bps_usd", "depth_25bps_usd", "depth_50bps_usd",
    "volatility_5m_pct", "volatility_1h_pct", "volatility_24h_pct",
    "listing_age_days", "asset_category", "asset_category_source",
    "mexc_api_eligible", "innovation_zone",
    "price_jump_flag", "thin_orderbook_flag", "spread_expansion_flag",
    "rapid_reversal_flag", "low_trade_count_flag", "abnormal_volume_flag",
    "estimated_slippage_bps", "estimated_fee_pct_per_side",
    "estimated_total_execution_cost_pct",
    "metadata_json"
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class ResearchRecorder:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        if not self.path.exists() or self.path.stat().st_size == 0:
            with self.path.open("w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=FIELDS).writeheader()

    def _write(self, row: Dict[str, Any]) -> None:
        data = {k: row.get(k, "") for k in FIELDS}
        with self._lock:
            with self.path.open("a", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=FIELDS).writerow(data)

    def record_candidate(
        self,
        symbol: str,
        timestamp: Optional[datetime] = None,
        side: Optional[str] = None,
        price: Optional[float] = None,
        signal_strength: Optional[float] = None,
        reason: Optional[str] = None,
        **metadata: Any,
    ) -> str:
        event_id = new_id("candidate")
        self._write({
            "event_id": event_id,
            "timestamp": (timestamp or datetime.now(timezone.utc)).isoformat(),
            "event_type": "candidate",
            "symbol": symbol,
            "side": side or "",
            "price": price if price is not None else "",
            "signal_strength": signal_strength if signal_strength is not None else "",
            "reason": reason or "",
            "metadata_json": metadata or "",
        })
        return event_id

    def record_signal(
        self,
        symbol: str,
        timestamp: Optional[datetime] = None,
        side: Optional[str] = None,
        price: Optional[float] = None,
        quantity: Optional[float] = None,
        risk_usd: Optional[float] = None,
        leverage: Optional[float] = None,
        signal_strength: Optional[float] = None,
        reason: Optional[str] = None,
        signal_id: Optional[str] = None,
        position_id: Optional[str] = None,
        **metadata: Any,
    ) -> str:
        signal_id = signal_id or new_id("signal")
        position_id = position_id or new_id("position")
        self._write({
            "event_id": new_id("signal_event"),
            "timestamp": (timestamp or datetime.now(timezone.utc)).isoformat(),
            "event_type": "signal",
            "signal_id": signal_id,
            "position_id": position_id,
            "symbol": symbol,
            "side": side or "",
            "price": price if price is not None else "",
            "quantity": quantity if quantity is not None else "",
            "risk_usd": risk_usd if risk_usd is not None else "",
            "leverage": leverage if leverage is not None else "",
            "signal_strength": signal_strength if signal_strength is not None else "",
            "reason": reason or "",
            "metadata_json": metadata or "",
        })
        return signal_id

    def record_enrichment(self, signal_id: str, position_id: str = "", **fields: Any) -> None:
        fields["event_id"] = new_id("enrichment")
        fields["timestamp"] = fields.get("timestamp") or utc_now_iso()
        fields["event_type"] = "enrichment"
        fields["signal_id"] = signal_id
        fields["position_id"] = position_id
        self._write(fields)

    def record_exit(
        self,
        position_id: str,
        symbol: str,
        side: str,
        price: float,
        pnl_usd: Optional[float] = None,
        timestamp: Optional[datetime] = None,
        **metadata: Any,
    ) -> None:
        self._write({
            "event_id": new_id("exit"),
            "timestamp": (timestamp or datetime.now(timezone.utc)).isoformat(),
            "event_type": "exit",
            "position_id": position_id,
            "symbol": symbol,
            "side": side,
            "price": price,
            "metadata_json": {"pnl_usd": pnl_usd, **metadata},
        })
