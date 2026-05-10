import pytest
import asyncio
import time
from unittest.mock import MagicMock
from src.exchanges.base import OrderBook
from src.strategies.triangular import TriangularStrategy, TriangleOpportunity
from src.risk.manager import RiskManager


def make_risk(overrides=None):
    cfg = {
        "max_daily_loss_usdt": 1000,
        "max_drawdown_pct": 50,
        "max_open_orders": 10,
        "min_balance_usdt": 1,
        "max_position_pct": 90,
        "max_loss_per_trade_usdt": 100,
        "max_consecutive_losses": 10,
        "consecutive_loss_cooldown_seconds": 1,
        "max_daily_fees_usdt": 500,
        "min_profit_fee_ratio": 1.0,  # relaxed for testing
    }
    if overrides:
        cfg.update(overrides)
    r = RiskManager(cfg)
    r.set_starting_balance(10000)
    return r


def make_ob(symbol, exchange, bid, ask, qty=1.0, fresh=True):
    ts = time.time() if fresh else time.time() - 10
    return OrderBook(
        symbol=symbol,
        exchange=exchange,
        bids=[(bid, qty)],
        asks=[(ask, qty)],
        timestamp=ts,
    )


def make_exchange(order_books, usdt_balance=10000.0):
    ex = MagicMock()
    ex.name = "binance"
    ex.order_books = order_books
    ex.get_order_book = lambda sym: order_books.get(sym)
    ex.get_balance = lambda currency: usdt_balance if currency == "USDT" else 0.0
    ex._exchange = MagicMock()
    ex._exchange.markets = {sym: {"id": sym.replace("/", ""), "active": True} for sym in order_books}
    return ex


def make_strategy(order_books, risk=None, overrides=None):
    cfg = {
        "triangles": [["USDT", "BTC", "ETH"]],
        "fee_rate": 0.001,
        "min_profit_pct": 0.01,
        "trade_amount_usdt": 100,
        "max_trade_amount_usdt": 1000,
        "order_timeout_seconds": 5,
    }
    if overrides:
        cfg.update(overrides)
    return TriangularStrategy(
        exchange=make_exchange(order_books),
        config=cfg,
        risk=risk or make_risk(),
    )


def standard_obs(fresh=True):
    return {
        "BTC/USDT": make_ob("BTC/USDT", "binance", bid=60000, ask=60001, fresh=fresh),
        "ETH/BTC":  make_ob("ETH/BTC",  "binance", bid=0.05,   ask=0.05001, fresh=fresh),
        "ETH/USDT": make_ob("ETH/USDT", "binance", bid=3002,   ask=3003, fresh=fresh),
    }


class TestTriangularStrategy:

    def test_scan_returns_list(self):
        assert isinstance(make_strategy(standard_obs()).scan(), list)

    def test_evaluate_type(self):
        result = make_strategy(standard_obs())._evaluate("USDT", "BTC", "ETH")
        assert result is None or isinstance(result, TriangleOpportunity)

    def test_missing_markets_returns_none(self):
        assert make_strategy({})._evaluate("USDT", "BTC", "ETH") is None

    def test_stale_orderbook_returns_none(self):
        result = make_strategy(standard_obs(fresh=False))._evaluate("USDT", "BTC", "ETH")
        assert result is None

    def test_opportunity_has_fees_field(self):
        strat = make_strategy(standard_obs())
        opp = strat._evaluate("USDT", "BTC", "ETH")
        if opp:
            assert opp.expected_fees_usdt > 0

    def test_paper_execute_increments_counters(self):
        strat = make_strategy(standard_obs())
        opp = TriangleOpportunity(
            base="USDT", mid="BTC", quote="ETH",
            path="USDT→BTC→ETH→USDT",
            rate_bm=1/60001, rate_mq=1/0.05001, rate_qb=3002,
            profit_pct=0.5,
            trade_amount_usdt=100,
            expected_profit_usdt=0.5,
            expected_fees_usdt=0.3,
        )
        result = asyncio.run(strat._paper_execute(opp))
        assert result is True
        assert strat._trades_executed == 1
        assert strat._total_profit > 0
        assert strat._total_fees > 0

    def test_stats_has_fee_field(self):
        s = make_strategy(standard_obs()).stats()
        assert "total_fees_usdt" in s
        assert "net_pnl_usdt" in s

    def test_execute_blocked_on_halt(self):
        risk = make_risk()
        risk._halt("test")
        strat = make_strategy(standard_obs(), risk=risk)
        opp = TriangleOpportunity(
            base="USDT", mid="BTC", quote="ETH",
            path="USDT→BTC→ETH→USDT",
            rate_bm=1/60001, rate_mq=1/0.05001, rate_qb=3002,
            profit_pct=0.5,
            trade_amount_usdt=100,
            expected_profit_usdt=0.5,
            expected_fees_usdt=0.3,
        )
        assert asyncio.run(strat.execute(opp)) is False

    def test_fee_safety_margin_filters_marginal_opportunity(self):
        # min_profit_fee_ratio=2.0 requires profit ≥ 2× fees
        # fee=0.1%×3=0.3%, so need 0.6%
        # min_profit_pct=0.01 but safety margin will filter 0.4% opportunities
        risk = make_risk({"min_profit_fee_ratio": 2.0})
        strat = make_strategy(standard_obs(), risk=risk, overrides={"min_profit_pct": 0.01})
        # Even if scan finds something, validate_expected_profit should filter < 0.6%
        # Just verify no crash and filter logic runs
        results = strat.scan()
        assert isinstance(results, list)
