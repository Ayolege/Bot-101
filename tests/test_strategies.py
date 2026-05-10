import pytest
import asyncio
import time
from unittest.mock import MagicMock
from src.exchanges.base import OrderBook
from src.strategies.triangular import TriangularStrategy, TriangleOpportunity
from src.risk.manager import RiskManager


def make_risk():
    r = RiskManager({
        "max_daily_loss_usdt": 1000,
        "max_drawdown_pct": 50,
        "max_open_orders": 10,
        "order_timeout_seconds": 5,
        "min_balance_usdt": 1,
        "max_position_pct": 90,
    })
    r.set_starting_balance(10000)
    return r


def make_ob(symbol, exchange, bid, ask, qty=1.0):
    return OrderBook(
        symbol=symbol,
        exchange=exchange,
        bids=[(bid, qty)],
        asks=[(ask, qty)],
        timestamp=time.time(),
    )


def make_exchange(order_books, usdt_balance=10000.0):
    ex = MagicMock()
    ex.name = "binance"
    ex.order_books = order_books
    ex.get_order_book = lambda sym: order_books.get(sym)
    ex.get_balance = lambda currency: usdt_balance if currency == "USDT" else 0.0
    ex._exchange = MagicMock()
    ex._exchange.markets = {sym: {"id": sym.replace("/", "")} for sym in order_books}
    return ex


def make_strategy(order_books, overrides=None):
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
        risk=make_risk(),
    )


class TestTriangularStrategy:
    def _obs(self, btc_ask=60001, eth_btc_ask=0.05001, eth_usdt_bid=3002):
        return {
            "BTC/USDT": make_ob("BTC/USDT", "binance", bid=60000, ask=btc_ask),
            "ETH/BTC":  make_ob("ETH/BTC",  "binance", bid=0.05,   ask=eth_btc_ask),
            "ETH/USDT": make_ob("ETH/USDT", "binance", bid=eth_usdt_bid, ask=3003),
        }

    def test_scan_returns_list(self):
        strat = make_strategy(self._obs())
        assert isinstance(strat.scan(), list)

    def test_evaluate_no_exception(self):
        strat = make_strategy(self._obs())
        result = strat._evaluate("USDT", "BTC", "ETH")
        assert result is None or isinstance(result, TriangleOpportunity)

    def test_no_opportunity_when_markets_missing(self):
        strat = make_strategy({})   # empty order books
        result = strat._evaluate("USDT", "BTC", "ETH")
        assert result is None

    def test_paper_execute_increments_counter(self):
        strat = make_strategy(self._obs())
        opp = TriangleOpportunity(
            base="USDT", mid="BTC", quote="ETH",
            path="USDT→BTC→ETH→USDT",
            rate_bm=1 / 60001, rate_mq=1 / 0.05001, rate_qb=3002,
            profit_pct=0.05,
            trade_amount_usdt=100,
            expected_profit_usdt=0.05,
        )
        # Patch execute to paper path
        strat.cfg["mode"] = "paper"
        result = asyncio.run(strat._paper_execute(opp))
        assert result is True
        assert strat._trades_executed == 1
        assert strat._total_profit > 0

    def test_stats_structure(self):
        strat = make_strategy(self._obs())
        stats = strat.stats()
        assert "opportunities_found" in stats
        assert "trades_executed" in stats
        assert "total_profit_usdt" in stats

    def test_fresh_rejects_stale_orderbook(self):
        obs = self._obs()
        for ob in obs.values():
            ob.timestamp = time.time() - 5  # 5 seconds old
        strat = make_strategy(obs)
        result = strat._evaluate("USDT", "BTC", "ETH")
        assert result is None  # stale data should be rejected

    def test_risk_blocks_trade_on_halt(self):
        strat = make_strategy(self._obs())
        strat.risk._halt("test halt")
        opp = TriangleOpportunity(
            base="USDT", mid="BTC", quote="ETH",
            path="USDT→BTC→ETH→USDT",
            rate_bm=1 / 60001, rate_mq=1 / 0.05001, rate_qb=3002,
            profit_pct=0.5,
            trade_amount_usdt=100,
            expected_profit_usdt=0.5,
        )
        result = asyncio.run(strat.execute(opp))
        assert result is False
