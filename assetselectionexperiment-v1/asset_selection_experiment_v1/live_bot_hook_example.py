"""
COPY/REFERENCE ONLY.

This file shows how to add observation-only research logging to live_bot.py.
Do not replace your existing trading logic with this file.
"""

from datetime import datetime, timezone

# In live_bot.py:
#
# from research.asset_selection.asset_selection_recorder import ResearchRecorder
#
# research = ResearchRecorder("research/asset_selection/asset_events.csv")


def example_candidate_hook(research, symbol, side, price, signal_strength, reason):
    """
    Call after the existing strategy has evaluated a pair.
    This does not approve/reject the signal.
    """
    return research.record_candidate(
        symbol=symbol,
        timestamp=datetime.now(timezone.utc),
        side=side,
        price=price,
        signal_strength=signal_strength,
        reason=reason,
    )


def example_signal_hook(
    research, symbol, side, price, quantity, risk_usd, leverage,
    signal_strength, reason
):
    """
    Call immediately before the existing order-placement/logging code.
    The returned IDs should ideally be carried into the normal trade log.
    """
    signal_id = research.record_signal(
        symbol=symbol,
        timestamp=datetime.now(timezone.utc),
        side=side,
        price=price,
        quantity=quantity,
        risk_usd=risk_usd,
        leverage=leverage,
        signal_strength=signal_strength,
        reason=reason,
    )

    # IMPORTANT:
    # Continue with the existing bot logic after this function returns.
    # Do not use signal_id as a new trading decision.
    return signal_id
