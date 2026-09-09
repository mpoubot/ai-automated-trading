from __future__ import annotations

from dataclasses import dataclass

from alpaca.trading.client import TradingClient

from .config import load_settings


@dataclass(frozen=True)
class PaperSnapshot:
    account_id: str
    status: str
    equity: str
    cash: str
    positions: int


def get_paper_snapshot() -> PaperSnapshot:
    """Read-only paper-account connectivity check.

    No order endpoint is called by this module. Order submission will only be
    added after the strategy, risk controls and forward-test protocol are frozen.
    """
    settings = load_settings(require_credentials=True)
    client = TradingClient(settings.api_key, settings.api_secret, paper=True)
    account = client.get_account()
    positions = client.get_all_positions()
    return PaperSnapshot(
        account_id=str(account.id),
        status=str(account.status),
        equity=str(account.equity),
        cash=str(account.cash),
        positions=len(positions),
    )


if __name__ == "__main__":
    snapshot = get_paper_snapshot()
    print(f"paper_account={snapshot.account_id}")
    print(f"status={snapshot.status}")
    print(f"equity={snapshot.equity}")
    print(f"cash={snapshot.cash}")
    print(f"positions={snapshot.positions}")
