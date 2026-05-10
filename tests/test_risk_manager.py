import pytest
from src.risk.manager import RiskManager


@pytest.fixture
def risk():
    config = {
        "max_daily_loss_usdt": 50,
        "max_drawdown_pct": 5,
        "max_open_orders": 3,
        "order_timeout_seconds": 10,
        "min_balance_usdt": 20,
        "max_position_pct": 30,
    }
    r = RiskManager(config)
    r.set_starting_balance(1000.0)
    return r


def test_can_trade_normal(risk):
    ok, reason = risk.can_trade(100, 500)
    assert ok
    assert reason == ""


def test_can_trade_below_min_balance(risk):
    ok, reason = risk.can_trade(100, 15)
    assert not ok
    assert "minimum" in reason.lower()


def test_can_trade_position_too_large(risk):
    # 30% of 500 = 150; requesting 200 should be blocked
    ok, reason = risk.can_trade(200, 500)
    assert not ok


def test_daily_loss_halt(risk):
    risk.record_trade(-55)   # exceeds $50 daily loss limit
    assert risk.state.halted
    ok, _ = risk.can_trade(10, 500)
    assert not ok


def test_max_open_orders(risk):
    risk.open_order()
    risk.open_order()
    risk.open_order()
    ok, reason = risk.can_trade(10, 500)
    assert not ok
    assert "open orders" in reason.lower()


def test_win_rate(risk):
    risk.record_trade(5)
    risk.record_trade(-2)
    risk.record_trade(3)
    assert abs(risk.win_rate - 66.67) < 0.1


def test_safe_trade_amount(risk):
    # max_position_pct=30 of 1000 = 300, minus min_balance 20 → 980
    safe = risk.safe_trade_amount(500, 1000)
    assert safe <= 300


def test_resume_after_halt(risk):
    risk._halt("test")
    assert risk.state.halted
    risk.resume()
    assert not risk.state.halted
