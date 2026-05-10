import time
from dataclasses import dataclass, field
from typing import Optional
from loguru import logger


@dataclass
class RiskState:
    # PnL tracking
    daily_pnl: float = 0.0
    daily_fees_paid: float = 0.0
    starting_balance: float = 0.0
    current_balance: float = 0.0
    peak_balance: float = 0.0

    # Trade counters
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    consecutive_losses: int = 0
    open_orders: int = 0

    # Circuit-breaker state
    halted: bool = False
    halt_reason: str = ""
    paused_until: float = 0.0    # epoch seconds — temporary cool-down

    session_start: float = field(default_factory=time.time)


class RiskManager:
    """
    Multi-layer loss protection.

    Layer 1 — Pre-trade gate: checks every limit before an order fires.
    Layer 2 — Per-trade cap: any single cycle losing more than max_loss_per_trade halts immediately.
    Layer 3 — Fee budget: daily fee spend is tracked separately so fee drag is visible.
    Layer 4 — Consecutive loss cool-down: N losses in a row → pause, not full halt.
    Layer 5 — Daily loss ceiling and drawdown ceiling: permanent halt for the day.

    Nothing trades unless all five layers pass.
    """

    def __init__(self, config: dict):
        self.cfg = config
        self.state = RiskState()

        self._max_daily_loss = config.get("max_daily_loss_usdt", 50)
        self._max_drawdown_pct = config.get("max_drawdown_pct", 5)
        self._max_open_orders = config.get("max_open_orders", 2)
        self._min_balance = config.get("min_balance_usdt", 25)
        self._max_position_pct = config.get("max_position_pct", 25)

        # Per-trade cap: a single cycle cannot lose more than this in USDT
        self._max_loss_per_trade = config.get("max_loss_per_trade_usdt", 5)

        # After this many consecutive losses, pause for cool-down instead of trading
        self._max_consecutive_losses = config.get("max_consecutive_losses", 3)
        self._cooldown_seconds = config.get("consecutive_loss_cooldown_seconds", 300)

        # Fee budget: if total fees paid today exceed this, halt (overtrading guard)
        self._max_daily_fees = config.get("max_daily_fees_usdt", 30)

    # ── Public API ────────────────────────────────────────────

    def set_starting_balance(self, balance: float) -> None:
        self.state.starting_balance = balance
        self.state.current_balance = balance
        self.state.peak_balance = balance
        logger.info(f"[Risk] Starting balance set: {balance:.2f} USDT")

    def can_trade(self, trade_amount: float, current_balance: float) -> tuple[bool, str]:
        """
        Returns (allowed, reason).
        Called before EVERY trade — all five layers must pass.
        """
        s = self.state

        if s.halted:
            return False, f"Halted: {s.halt_reason}"

        if time.time() < s.paused_until:
            remaining = int(s.paused_until - time.time())
            return False, f"Cooling down after consecutive losses — {remaining}s remaining"

        if s.open_orders >= self._max_open_orders:
            return False, f"Max open orders ({self._max_open_orders}) reached"

        if current_balance < self._min_balance:
            return False, f"Balance {current_balance:.2f} USDT below minimum {self._min_balance} USDT"

        max_pos = current_balance * (self._max_position_pct / 100)
        if trade_amount > max_pos:
            return False, (
                f"Trade size {trade_amount:.2f} USDT exceeds "
                f"{self._max_position_pct}% position limit ({max_pos:.2f} USDT)"
            )

        if s.daily_pnl <= -self._max_daily_loss:
            self._halt(f"Daily loss limit reached: {s.daily_pnl:.2f} USDT")
            return False, self.state.halt_reason

        if s.daily_fees_paid >= self._max_daily_fees:
            self._halt(
                f"Daily fee budget exhausted: {s.daily_fees_paid:.2f} USDT paid in fees today — "
                "bot may be overtrading unprofitable cycles"
            )
            return False, self.state.halt_reason

        return True, ""

    def validate_expected_profit(
        self,
        gross_profit_pct: float,
        fee_rate: float,
        num_legs: int = 3,
    ) -> tuple[bool, str]:
        """
        Check that expected profit is meaningfully above fee cost, not just above zero.
        This prevents chasing marginal opportunities where slippage wipes the gain.
        """
        total_fee_pct = fee_rate * num_legs * 100
        # Require profit to exceed fees by at least 50% (configurable safety margin)
        safety_margin = self.cfg.get("min_profit_fee_ratio", 1.5)
        required_pct = total_fee_pct * safety_margin
        if gross_profit_pct < required_pct:
            return False, (
                f"Profit {gross_profit_pct:.4f}% too close to fee cost {total_fee_pct:.4f}% "
                f"(requires {required_pct:.4f}% with {safety_margin}× safety margin)"
            )
        return True, ""

    def record_trade(self, net_pnl: float, fees_paid: float = 0.0) -> None:
        """
        Call after every completed trade cycle.
        net_pnl is the actual profit or loss in USDT after fees.
        fees_paid is the gross fee cost in USDT (sum of all legs).
        """
        s = self.state
        s.daily_pnl += net_pnl
        s.daily_fees_paid += fees_paid
        s.current_balance += net_pnl
        s.total_trades += 1

        if net_pnl > 0:
            s.winning_trades += 1
            s.consecutive_losses = 0
            if s.current_balance > s.peak_balance:
                s.peak_balance = s.current_balance
        else:
            s.losing_trades += 1
            s.consecutive_losses += 1

            # Per-trade loss cap
            if abs(net_pnl) > self._max_loss_per_trade:
                self._halt(
                    f"Single trade loss {net_pnl:.4f} USDT exceeded cap "
                    f"of {self._max_loss_per_trade} USDT"
                )
                return

            # Consecutive loss cool-down (pause, not hard halt)
            if s.consecutive_losses >= self._max_consecutive_losses:
                s.paused_until = time.time() + self._cooldown_seconds
                logger.warning(
                    f"[Risk] {s.consecutive_losses} consecutive losses — "
                    f"cooling down for {self._cooldown_seconds}s"
                )
                s.consecutive_losses = 0

        self._check_limits()

    def estimate_fees(self, trade_amount_usdt: float, fee_rate: float, legs: int = 3) -> float:
        """
        Estimate total fees for a triangular cycle in USDT.
        Used to verify the fee budget before committing.
        """
        return trade_amount_usdt * fee_rate * legs

    def open_order(self) -> None:
        self.state.open_orders += 1

    def close_order(self) -> None:
        self.state.open_orders = max(0, self.state.open_orders - 1)

    def resume(self) -> None:
        self.state.halted = False
        self.state.halt_reason = ""
        logger.info("[Risk] Trading manually resumed.")

    def reset_daily(self) -> None:
        self.state.daily_pnl = 0.0
        self.state.daily_fees_paid = 0.0
        logger.info("[Risk] Daily counters reset.")

    def safe_trade_amount(self, desired: float, balance: float) -> float:
        """Clamp requested trade size to all position limits."""
        max_pos = balance * (self._max_position_pct / 100)
        return min(desired, max_pos, balance - self._min_balance)

    # ── Internal ──────────────────────────────────────────────

    def _check_limits(self) -> None:
        s = self.state
        if s.daily_pnl <= -self._max_daily_loss:
            self._halt(f"Daily loss limit exceeded: {s.daily_pnl:.2f} USDT")
            return

        if s.daily_fees_paid >= self._max_daily_fees:
            self._halt(
                f"Daily fee budget exhausted: {s.daily_fees_paid:.2f} USDT paid in fees — "
                "bot may be overtrading unprofitable cycles"
            )
            return

        if s.starting_balance > 0:
            drawdown = ((s.peak_balance - s.current_balance) / s.peak_balance) * 100
            if drawdown >= self._max_drawdown_pct:
                self._halt(f"Drawdown {drawdown:.2f}% from peak exceeded {self._max_drawdown_pct}%")

    def _halt(self, reason: str) -> None:
        if not self.state.halted:
            self.state.halted = True
            self.state.halt_reason = reason
            logger.critical(f"[Risk] *** TRADING HALTED *** — {reason}")

    # ── Reporting ─────────────────────────────────────────────

    @property
    def win_rate(self) -> float:
        t = self.state.total_trades
        return (self.state.winning_trades / t * 100) if t else 0.0

    @property
    def drawdown_pct(self) -> float:
        if self.state.peak_balance <= 0:
            return 0.0
        return ((self.state.peak_balance - self.state.current_balance) / self.state.peak_balance) * 100

    def summary(self) -> dict:
        s = self.state
        return {
            "daily_pnl_usdt": round(s.daily_pnl, 4),
            "daily_fees_usdt": round(s.daily_fees_paid, 4),
            "total_trades": s.total_trades,
            "win_rate_pct": round(self.win_rate, 1),
            "drawdown_pct": round(self.drawdown_pct, 2),
            "consecutive_losses": s.consecutive_losses,
            "open_orders": s.open_orders,
            "halted": s.halted,
            "session_hours": round((time.time() - s.session_start) / 3600, 2),
        }
