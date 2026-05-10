import time
from dataclasses import dataclass, field
from typing import Optional
from loguru import logger


@dataclass
class RiskState:
    daily_pnl: float = 0.0
    starting_balance: float = 0.0
    current_balance: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    open_orders: int = 0
    halted: bool = False
    halt_reason: str = ""
    session_start: float = field(default_factory=time.time)


class RiskManager:
    def __init__(self, config: dict):
        self.cfg = config
        self.state = RiskState()
        self._max_daily_loss = config.get("max_daily_loss_usdt", 50)
        self._max_drawdown_pct = config.get("max_drawdown_pct", 5)
        self._max_open_orders = config.get("max_open_orders", 3)
        self._min_balance = config.get("min_balance_usdt", 20)
        self._max_position_pct = config.get("max_position_pct", 30)
        self._order_timeout = config.get("order_timeout_seconds", 10)

    def set_starting_balance(self, balance_usdt: float) -> None:
        self.state.starting_balance = balance_usdt
        self.state.current_balance = balance_usdt

    def record_trade(self, pnl_usdt: float) -> None:
        self.state.daily_pnl += pnl_usdt
        self.state.current_balance += pnl_usdt
        self.state.total_trades += 1
        if pnl_usdt > 0:
            self.state.winning_trades += 1
        else:
            self.state.losing_trades += 1
        self._check_limits()

    def can_trade(self, trade_amount_usdt: float, current_balance_usdt: float) -> tuple[bool, str]:
        if self.state.halted:
            return False, f"Bot halted: {self.state.halt_reason}"

        if self.state.open_orders >= self._max_open_orders:
            return False, f"Max open orders reached ({self._max_open_orders})"

        if current_balance_usdt < self._min_balance:
            return False, f"Balance {current_balance_usdt:.2f} below minimum {self._min_balance}"

        max_position = current_balance_usdt * (self._max_position_pct / 100)
        if trade_amount_usdt > max_position:
            return False, f"Trade size {trade_amount_usdt:.2f} exceeds position limit {max_position:.2f}"

        if self.state.daily_pnl <= -self._max_daily_loss:
            self._halt(f"Daily loss limit hit: {self.state.daily_pnl:.2f} USDT")
            return False, self.state.halt_reason

        return True, ""

    def safe_trade_amount(self, desired: float, balance: float) -> float:
        """Clamp trade amount to risk limits."""
        max_pos = balance * (self._max_position_pct / 100)
        return min(desired, max_pos, balance - self._min_balance)

    def open_order(self) -> None:
        self.state.open_orders += 1

    def close_order(self) -> None:
        self.state.open_orders = max(0, self.state.open_orders - 1)

    def _check_limits(self) -> None:
        if self.state.daily_pnl <= -self._max_daily_loss:
            self._halt(f"Daily loss limit exceeded: {self.state.daily_pnl:.2f} USDT")
            return

        if self.state.starting_balance > 0:
            drawdown_pct = ((self.state.starting_balance - self.state.current_balance) /
                            self.state.starting_balance) * 100
            if drawdown_pct >= self._max_drawdown_pct:
                self._halt(f"Max drawdown exceeded: {drawdown_pct:.1f}%")

    def _halt(self, reason: str) -> None:
        if not self.state.halted:
            self.state.halted = True
            self.state.halt_reason = reason
            logger.critical(f"[RiskManager] TRADING HALTED — {reason}")

    def resume(self) -> None:
        """Manually resume after reviewing the halt condition."""
        self.state.halted = False
        self.state.halt_reason = ""
        logger.info("[RiskManager] Trading resumed.")

    def reset_daily(self) -> None:
        self.state.daily_pnl = 0.0
        logger.info("[RiskManager] Daily PnL counter reset.")

    @property
    def win_rate(self) -> float:
        if self.state.total_trades == 0:
            return 0.0
        return (self.state.winning_trades / self.state.total_trades) * 100

    def summary(self) -> dict:
        elapsed = time.time() - self.state.session_start
        return {
            "daily_pnl_usdt": round(self.state.daily_pnl, 4),
            "total_trades": self.state.total_trades,
            "win_rate_pct": round(self.win_rate, 1),
            "open_orders": self.state.open_orders,
            "halted": self.state.halted,
            "session_hours": round(elapsed / 3600, 2),
        }
