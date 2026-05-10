import pytest
from src.utils.helpers import triangle_profit, pct_profit, round_down


def test_triangle_profit_profitable():
    # Idealised: no fees, perfect 1:1:1 triangle should return 0%
    assert abs(triangle_profit(1.0, 1.0, 1.0, 0.0)) < 1e-9


def test_triangle_profit_with_opportunity():
    # Simulate a 0.5% mispricing opportunity
    rate_ab = 1.005  # slightly favourable
    rate_bc = 1.005
    rate_ca = 1.005
    profit = triangle_profit(rate_ab, rate_bc, rate_ca, fee=0.0)
    assert profit > 0


def test_triangle_profit_fee_eats_profit():
    # Small mispricing should be eaten by 0.1% fees on each leg
    rate_ab = 1.001
    rate_bc = 1.001
    rate_ca = 1.001
    profit_no_fee = triangle_profit(rate_ab, rate_bc, rate_ca, fee=0.0)
    profit_with_fee = triangle_profit(rate_ab, rate_bc, rate_ca, fee=0.001)
    assert profit_with_fee < profit_no_fee


def test_pct_profit_basic():
    profit = pct_profit(buy_price=100.0, sell_price=101.0, fee_rate=0.0)
    assert abs(profit - 1.0) < 0.01


def test_pct_profit_fee_reduces_profit():
    p_no_fee = pct_profit(100.0, 101.0, 0.0)
    p_with_fee = pct_profit(100.0, 101.0, 0.001)
    assert p_with_fee < p_no_fee


def test_round_down():
    assert round_down(1.23456789, 4) == 1.2345
    assert round_down(0.999999, 2) == 0.99
    assert round_down(100.0, 0) == 100.0


def test_triangle_negative_without_opportunity():
    # Balanced market with fees should yield negative
    profit = triangle_profit(1.0, 1.0, 1.0, fee=0.001)
    assert profit < 0
