"""
Risk management layer. This is the module that keeps "aggressive + leveraged"
from turning into "account blown in a week." Position size and leverage are
DERIVED from volatility and a fixed risk-per-trade percentage — never picked
directly.
"""
from dataclasses import dataclass
import config as cfg


@dataclass
class PositionPlan:
    side: str                  # "long" or "short"
    entry_price: float
    stop_price: float
    stop_distance: float
    position_size_usdt: float  # notional size
    quantity: float            # size in base asset units
    leverage: float
    risk_amount_usdt: float


def calc_position_plan(entry_price: float, atr: float, side: str,
                        equity_usdt: float) -> PositionPlan | None:
    """
    Given entry price, current ATR, side, and account equity, derive:
      - stop-loss price (ATR-based)
      - risk amount in USDT (fixed % of equity)
      - required leverage to achieve that risk with the stop distance
      - position notional size, capped by MAX_LEVERAGE

    Returns None if the ATR is invalid or leverage would need to be zero/negative.
    """
    if atr is None or atr <= 0 or entry_price <= 0:
        return None

    stop_distance = atr * cfg.ATR_STOP_MULT
    if stop_distance <= 0:
        return None

    risk_amount_usdt = equity_usdt * cfg.RISK_PER_TRADE_PCT

    if side == "long":
        stop_price = entry_price - stop_distance
    elif side == "short":
        stop_price = entry_price + stop_distance
    else:
        return None

    # Quantity such that (quantity * stop_distance) == risk_amount_usdt
    quantity = risk_amount_usdt / stop_distance
    position_size_usdt = quantity * entry_price

    # Leverage implied by this position size relative to equity
    implied_leverage = position_size_usdt / equity_usdt if equity_usdt > 0 else 0
    leverage = max(cfg.MIN_LEVERAGE, min(implied_leverage, cfg.MAX_LEVERAGE))

    # If implied leverage exceeds the cap, scale position size DOWN to respect
    # the cap — this means realized risk may be less than RISK_PER_TRADE_PCT
    # in very volatile pairs, which is the safe direction to err.
    if implied_leverage > cfg.MAX_LEVERAGE:
        position_size_usdt = equity_usdt * cfg.MAX_LEVERAGE
        quantity = position_size_usdt / entry_price
        risk_amount_usdt = quantity * stop_distance

    return PositionPlan(
        side=side,
        entry_price=entry_price,
        stop_price=stop_price,
        stop_distance=stop_distance,
        position_size_usdt=round(position_size_usdt, 2),
        quantity=quantity,
        leverage=round(leverage, 2),
        risk_amount_usdt=round(risk_amount_usdt, 2),
    )


class CircuitBreaker:
    """Tracks daily loss and drawdown-from-peak to gate whether new trades
    are allowed, and whether the bot should halt entirely."""

    def __init__(self, starting_equity: float):
        self.peak_equity = starting_equity
        self.day_start_equity = starting_equity
        self.current_day = None
        self.halted = False
        self.halt_reason = ""

    def update(self, equity: float, today_key: str):
        if self.current_day != today_key:
            self.current_day = today_key
            self.day_start_equity = equity

        if equity > self.peak_equity:
            self.peak_equity = equity

    def can_open_new_trades(self, equity: float) -> tuple[bool, str]:
        if self.halted:
            return False, self.halt_reason

        daily_loss_pct = (self.day_start_equity - equity) / self.day_start_equity \
            if self.day_start_equity > 0 else 0
        if daily_loss_pct >= cfg.DAILY_LOSS_LIMIT_PCT:
            return False, f"Daily loss limit hit ({daily_loss_pct:.1%})"

        drawdown_pct = (self.peak_equity - equity) / self.peak_equity \
            if self.peak_equity > 0 else 0
        if drawdown_pct >= cfg.MAX_DRAWDOWN_LIMIT_PCT:
            self.halted = True
            self.halt_reason = f"Max drawdown limit hit ({drawdown_pct:.1%}) — bot halted"
            return False, self.halt_reason

        return True, "ok"
