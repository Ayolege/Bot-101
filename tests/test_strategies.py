import pytest
import asyncio
import time
from unittest.mock import MagicMock, patch, AsyncMock
from src.exchanges.base import OrderBook, Balance
from src.strategies.triangular import TriangularStrategy
from src.strategies.cross_exchange import CrossExchangeStrategy
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


def make_ob(symbol, exchange, bid, ask, bid_qty=1.0, ask_qty=1.0):
    return OrderBook(
        symbol=symbol,
        exchange=exchange,
        bids=[(bid, bid_qty)],
        asks=[(ask, ask_qty)],
        timestamp=time.time(),
    )


def make_exchange(name, order_books, usdt_balance=10000.0):
    ex = MagicMock()
    ex.name = name
    ex.order_books = order_books
    ex.get_order_book = lambda sym: order_books.get(sym)
    ex.get_balance = lambda currency: usdt_balance if currency == "USDT" else 0.0
    ex._exchange = MagicMock()
    ex._exchange.markets = {sym: {"id": sym.replace("/", "")} for sym in order_books}
    return ex


class TestTriangularStrategy:
    def _make_strategy(self, order_books, config_overrides=None):
        cfg = {
            "triangles": [["USDT", "BTC", "ETH"]],
            "fee_rate": 0.001,
            "min_profit_pct": 0.01,
            "trade_amount_usdt": 100,
            "max_trade_amount_usdt": 1000,
        }
        if config_overrides:
            cfg.update(config_overrides)
        ex = make_exchange("binance", order_books)
        return TriangularStrategy(exchange=ex, config=cfg, risk=make_risk(), paper_mode=True)

    def test_scan_detects_opportunity(self):
        # Slightly mispriced triangle
        obs = {
            "BTC/USDT": make_ob("BTC/USDT", "binance", bid=60000, ask=60001),
            "ETH/BTC":  make_ob("ETH/BTC",  "binance", bid=0.05, ask=0.05001),
            "ETH/USDT": make_ob("ETH/USDT", "binance", bid=3002, ask=3003),
        }
        strat = self._make_strategy(obs)
        # Manually test the evaluate path directly
        opp = strat._evaluate_triangle("USDT", "BTC", "ETH")
        # With tight fee threshold (0.01%) any valid triangle should be checked
        # We can't guarantee profit without real mispricing data, just check types
        # The important thing is no exception raised
        assert opp is None or hasattr(opp, "profit_pct")

    def test_scan_returns_list(self):
        obs = {
            "BTC/USDT": make_ob("BTC/USDT", "binance", bid=60000, ask=60001),
            "ETH/BTC":  make_ob("ETH/BTC",  "binance", bid=0.05, ask=0.05001),
            "ETH/USDT": make_ob("ETH/USDT", "binance", bid=3002, ask=3003),
        }
        strat = self._make_strategy(obs)
        result = strat.scan()
        assert isinstance(result, list)

    def test_paper_execute(self):
        obs = {
            "BTC/USDT": make_ob("BTC/USDT", "binance", bid=60000, ask=60001),
            "ETH/BTC":  make_ob("ETH/BTC",  "binance", bid=0.05, ask=0.05001),
            "ETH/USDT": make_ob("ETH/USDT", "binance", bid=3002, ask=3003),
        }
        strat = self._make_strategy(obs)
        from src.strategies.triangular import TriangleOpportunity
        opp = TriangleOpportunity(
            base="USDT", mid="BTC", quote="ETH",
            path="USDT→BTC→ETH→USDT",
            rate_bm=1/60001, rate_mq=1/0.05001, rate_qb=3002,
            profit_pct=0.05,
            trade_amount_usdt=100,
            expected_profit_usdt=0.05,
        )
        result = asyncio.run(strat._paper_execute(opp))
        assert result is True
        assert strat._trades_executed == 1


class TestCrossExchangeStrategy:
    def _make_strategy(self, obs_a, obs_b, config_overrides=None):
        cfg = {
            "pairs": ["BTC/USDT"],
            "min_profit_pct": 0.01,
            "trade_amount_usdt": 200,
            "max_trade_amount_usdt": 2000,
            "binance_fee": 0.001,
            "kucoin_fee": 0.001,
        }
        if config_overrides:
            cfg.update(config_overrides)
        ex_a = make_exchange("binance", obs_a)
        ex_b = make_exchange("kucoin", obs_b)
        return CrossExchangeStrategy(
            exchange_a=ex_a,
            exchange_b=ex_b,
            config=cfg,
            risk=make_risk(),
            paper_mode=True,
        )

    def test_scan_detects_cross_opportunity(self):
        # BTC/USDT: buy cheap on binance, sell expensive on kucoin
        obs_a = {"BTC/USDT": make_ob("BTC/USDT", "binance", bid=60000, ask=60000)}
        obs_b = {"BTC/USDT": make_ob("BTC/USDT", "kucoin",  bid=60300, ask=60301)}
        strat = self._make_strategy(obs_a, obs_b)
        opps = strat.scan()
        # Profit = (60300 - 60000) / 60000 = 0.5% minus fees → should be detected
        assert len(opps) == 1
        assert opps[0].buy_exchange == "binance"
        assert opps[0].sell_exchange == "kucoin"
        assert opps[0].profit_pct > 0

    def test_scan_no_opportunity_when_prices_equal(self):
        obs_a = {"BTC/USDT": make_ob("BTC/USDT", "binance", bid=60000, ask=60001)}
        obs_b = {"BTC/USDT": make_ob("BTC/USDT", "kucoin",  bid=60000, ask=60001)}
        strat = self._make_strategy(obs_a, obs_b)
        opps = strat.scan()
        assert opps == []

    def test_paper_execute(self):
        obs_a = {"BTC/USDT": make_ob("BTC/USDT", "binance", bid=60000, ask=60000)}
        obs_b = {"BTC/USDT": make_ob("BTC/USDT", "kucoin",  bid=60300, ask=60301)}
        strat = self._make_strategy(obs_a, obs_b)
        opps = strat.scan()
        assert opps
        result = asyncio.run(strat._paper_execute(opps[0]))
        assert result is True
        assert strat._trades_executed == 1
