from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    api_key: str
    api_secret: str
    paper: bool
    data_feed: str
    timeframe: str
    initial_equity: float

    @property
    def trading_base_url(self) -> str:
        return "https://paper-api.alpaca.markets" if self.paper else "https://api.alpaca.markets"


def load_settings(require_credentials: bool = False) -> Settings:
    key = os.getenv("ALPACA_API_KEY", "").strip()
    secret = os.getenv("ALPACA_API_SECRET", "").strip()
    paper = os.getenv("ALPACA_PAPER", "true").strip().lower() == "true"

    # This repository is deliberately paper-only during bootstrap.
    if not paper:
        raise RuntimeError("Live Alpaca mode is disabled in aura-alpaca-crypto-research")
    if require_credentials and (not key or not secret):
        raise RuntimeError("Missing ALPACA_API_KEY / ALPACA_API_SECRET in local .env")

    return Settings(
        api_key=key,
        api_secret=secret,
        paper=paper,
        data_feed=os.getenv("ALPACA_DATA_FEED", "us"),
        timeframe=os.getenv("RESEARCH_TIMEFRAME", "1Hour"),
        initial_equity=float(os.getenv("INITIAL_EQUITY", "100000")),
    )


if __name__ == "__main__":
    s = load_settings(False)
    print(f"paper={s.paper}")
    print(f"trading_base_url={s.trading_base_url}")
    print(f"data_feed={s.data_feed}")
    print(f"credentials_present={bool(s.api_key and s.api_secret)}")
