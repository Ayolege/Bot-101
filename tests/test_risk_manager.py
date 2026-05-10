import pytest
import time
from src.risk.manager import RiskManager


@pytest.fixture
def risk():
    r = RiskManager({
        "max_daily_loss_usdt": 50,
        "max_drawdown_pct": 5,
        "max_open_orders": 3,
        "min_balance_usdt": 20,
        "max_position_pct": 30,
        "max_loss_per_trade_usdt": 5,
        "max_consecutive_losses": 3,
        "consecutive_loss_cooldown_seconds": 300,
        "max_daily_fees_usdt": 30,
        "min_profit_fee_ratio": 1.5,
    })
    r.set_starting_balance(1000.0)
    return r


# ── can_trade gate ────────────────────────────────────────────

def test_can_trade_normal(risk):
    ok, reason = risk.can_trade(100, 500)
    assert ok and reason == ""


def test_can_trade_below_min_balance(risk):
    ok, reason = risk.can_trade(100, 15)
    assert not ok
    assert "minimum" in reason.lower()


def test_can_trade_position_too_large(risk):
    # 30% of 500 = 150; requesting 200 exceeds limit
    ok, reason = risk.can_trade(200, 500)
    assert not ok
    assert "position limit" in reason.lower()


def test_can_trade_max_open_orders(risk):
    risk.open_order(); risk.open_order(); risk.open_order()
    ok, reason = risk.can_trade(10, 500)
    assert not ok
    assert "open orders" in reason.lower()


def test_can_trade_halted(risk):
    risk._halt("test halt")
    ok, _ = risk.can_trade(10, 500)
    assert not ok


# ── daily limits ──────────────────────────────────────────────

def test_daily_loss_halt(risk):
    # Use multiple small losses to avoid triggering per-trade cap (max=5)
    for _ in range(11):
        risk.record_trade(-5, fees_paid=0)  # 11 × $5 = $55 total > $50 limit
    assert risk.state.halted
    assert "daily loss" in risk.state.halt_reason.lower()


def test_daily_fees_budget_halt(risk):
    # Exceed the $30 daily fee budget
    risk.record_trade(5, fees_paid=31)
    assert risk.state.halted
    assert "fee" in risk.state.halt_reason.lower()


def test_drawdown_halt(risk):
    # Starting at 1000, peak stays 1000
    # Record a big loss to trigger 5% drawdown
    risk.record_trade(-55, fees_paid=0)
    assert risk.state.halted


# ── per-trade cap ─────────────────────────────────────────────

def test_per_trade_loss_cap_halts(risk):
    risk.record_trade(-6, fees_paid=0)   # exceeds max_loss_per_trade_usdt=5
    assert risk.state.halted
    assert "single trade" in risk.state.halt_reason.lower()


def test_per_trade_within_cap_does_not_halt(risk):
    risk.record_trade(-4, fees_paid=0)   # within cap
    assert not risk.state.halted


# ── consecutive loss cool-down ────────────────────────────────

def test_consecutive_losses_trigger_cooldown(risk):
    risk.record_trade(-1, fees_paid=0)
    risk.record_trade(-1, fees_paid=0)
    risk.record_trade(-1, fees_paid=0)  # 3rd consecutive loss
    assert risk.state.paused_until > time.time()
    ok, reason = risk.can_trade(10, 500)
    assert not ok
    assert "cooling down" in reason.lower()


def test_win_resets_consecutive_losses(risk):
    risk.record_trade(-1, fees_paid=0)
    risk.record_trade(-1, fees_paid=0)
    risk.record_trade(5, fees_paid=0)   # win resets streak
    assert risk.state.consecutive_losses == 0


# ── profit/fee safety margin ──────────────────────────────────

def test_validate_profit_passes(risk):
    # fee=0.1%×3legs=0.3%, ratio=1.5 → need 0.45%
    ok, _ = risk.validate_expected_profit(0.5, fee_rate=0.001, num_legs=3)
    assert ok


def test_validate_profit_fails_below_ratio(risk):
    # profit=0.31% barely above fees=0.3% but below 1.5× ratio
    ok, reason = risk.validate_expected_profit(0.31, fee_rate=0.001, num_legs=3)
    assert not ok
    assert "fee" in reason.lower()


def test_validate_profit_fails_below_fees(risk):
    ok, _ = risk.validate_expected_profit(0.1, fee_rate=0.001, num_legs=3)
    assert not ok


# ── fee estimation ────────────────────────────────────────────

def test_estimate_fees(risk):
    fees = risk.estimate_fees(100, fee_rate=0.001, legs=3)
    assert abs(fees - 0.30) < 1e-9


# ── metrics ───────────────────────────────────────────────────

def test_win_rate(risk):
    risk.record_trade(5, fees_paid=0.1)
    risk.record_trade(-2, fees_paid=0.1)
    risk.record_trade(3, fees_paid=0.1)
    assert abs(risk.win_rate - 66.67) < 0.1


def test_safe_trade_amount(risk):
    safe = risk.safe_trade_amount(500, 1000)
    # max_position_pct=30 of 1000 = 300
    assert safe <= 300


def test_resume_after_halt(risk):
    risk._halt("test")
    risk.resume()
    assert not risk.state.halted


def test_reset_daily(risk):
    risk.record_trade(-10, fees_paid=5)
    risk.reset_daily()
    assert risk.state.daily_pnl == 0.0
    assert risk.state.daily_fees_paid == 0.0


def test_drawdown_calculated_from_peak(risk):
    risk.record_trade(100, fees_paid=0)   # balance goes to 1100, peak=1100
    assert risk.state.peak_balance == 1100
    risk.record_trade(-20, fees_paid=0)   # balance 1080, drawdown = 20/1100 = 1.8%
    assert risk.drawdown_pct < 5  # not halted yet


def test_summary_keys(risk):
    s = risk.summary()
    assert all(k in s for k in [
        "daily_pnl_usdt", "daily_fees_usdt", "total_trades",
        "win_rate_pct", "drawdown_pct", "consecutive_losses",
        "open_orders", "halted",
    ])
