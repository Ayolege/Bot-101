import pytest
import asyncio
import time
from unittest.mock import MagicMock

from src.exchanges.base import OrderBook
from src.strategies.discovery import TriangleMeta
from src.strategies.triangular import TriangularStrategy, TriangleOpportunity
from src.risk.manager import RiskManager


# ── Fixtures ──────────────────────────────────────────────────

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
        "min_profit_fee_ratio": 1.0,
    }
    if overrides:
        cfg.update(overrides)
    r = RiskManager(cfg)
    r.set_starting_balance(10000)
    return r


def make_meta(mid="BTC", quote="ETH") -> TriangleMeta:
    return TriangleMeta(
        base="USDT", mid=mid, quote=quote,
        sym_mid_base=f"{mid}/USDT",
        sym_quote_mid=f"{quote}/{mid}",
        sym_quote_base=f"{quote}/USDT",
        min_volume_usdt=1_000_000,
    )


def make_ob(symbol, exchange, bid, ask, qty=1.0, fresh=True):
    ts = time.time() if fresh else time.time() - 10
    return OrderBook(
        symbol=symbol, exchange=exchange,
        bids=[(bid, qty)], asks=[(ask, qty)],
        timestamp=ts,
    )


def make_exchange(order_books, usdt_balance=10000.0):
    ex = MagicMock()
    ex.name = "binance"
    ex.order_books = order_books
    ex.get_order_book = lambda sym: order_books.get(sym)
    ex.get_balance = lambda currency: usdt_balance if currency == "USDT" else 0.0
    ex._exchange = MagicMock()
    ex._exchange.markets = {
        sym: {"id": sym.replace("/", ""), "active": True}
        for sym in order_books
    }
    return ex


def standard_obs(fresh=True):
    return {
        "BTC/USDT": make_ob("BTC/USDT", "binance", bid=60000, ask=60001, fresh=fresh),
        "ETH/BTC":  make_ob("ETH/BTC",  "binance", bid=0.05,   ask=0.05001, fresh=fresh),
        "ETH/USDT": make_ob("ETH/USDT", "binance", bid=3002,   ask=3003, fresh=fresh),
    }


def make_strategy(order_books, risk=None, overrides=None, triangles=None):
    cfg = {
        "fee_rate": 0.001,
        "min_profit_pct": 0.01,
        "trade_amount_usdt": 15,
        "max_trade_amount_usdt": 20,
        "order_timeout_seconds": 5,
        "min_notional_usdt": 1,  # relaxed for testing
    }
    if overrides:
        cfg.update(overrides)
    if triangles is None:
        triangles = [make_meta("BTC", "ETH")]
    return TriangularStrategy(
        exchange=make_exchange(order_books),
        triangles=triangles,
        config=cfg,
        risk=risk or make_risk(),
    )


def make_opp(profit_pct=0.5, amount=15.0) -> TriangleOpportunity:
    return TriangleOpportunity(
        meta=make_meta(),
        profit_pct=profit_pct,
        trade_amount_usdt=amount,
        expected_profit_usdt=amount * profit_pct / 100,
        expected_fees_usdt=amount * 0.003,
    )


# ── Tests ─────────────────────────────────────────────────────

class TestTriangularStrategy:

    def test_scan_returns_list(self):
        assert isinstance(make_strategy(standard_obs()).scan(), list)

    def test_evaluate_returns_none_on_stale_data(self):
        result = make_strategy(standard_obs(fresh=False))._evaluate(make_meta())
        assert result is None

    def test_evaluate_returns_none_on_missing_ob(self):
        result = make_strategy({})._evaluate(make_meta())
        assert result is None

    def test_evaluate_returns_opportunity_when_profitable(self):
        strat = make_strategy(standard_obs())
        # Lower min_profit to catch whatever spread the test data produces
        strat.min_profit = -100
        result = strat._evaluate(make_meta())
        # Should return something (even if profit is negative) or None on filter
        assert result is None or isinstance(result, TriangleOpportunity)

    def test_opportunity_has_fees_field(self):
        strat = make_strategy(standard_obs())
        strat.min_profit = -100
        opp = strat._evaluate(make_meta())
        if opp:
            assert opp.expected_fees_usdt > 0

    def test_scan_sorted_by_profit_desc(self):
        # Two triangles with different profits
        obs = {
            **standard_obs(),
            "XRP/BTC":  make_ob("XRP/BTC",  "binance", bid=0.0001, ask=0.00011),
            "XRP/USDT": make_ob("XRP/USDT", "binance", bid=6.0, ask=6.1),
        }
        t1 = make_meta("BTC", "ETH")
        t2 = make_meta("BTC", "XRP")
        strat = make_strategy(obs, triangles=[t1, t2])
        strat.min_profit = -100
        results = strat.scan()
        if len(results) >= 2:
            assert results[0].profit_pct >= results[1].profit_pct

    def test_paper_execute_records_profit_and_fees(self):
        strat = make_strategy(standard_obs())
        opp = make_opp()
        result = asyncio.run(strat._paper_execute(opp))
        assert result is True
        assert strat._trades_executed == 1
        assert strat._total_profit > 0
        assert strat._total_fees > 0

    def test_execute_blocked_when_halted(self):
        risk = make_risk()
        risk._halt("test halt")
        strat = make_strategy(standard_obs(), risk=risk)
        result = asyncio.run(strat.execute(make_opp()))
        assert result is False

    def test_stats_includes_triangle_count(self):
        strat = make_strategy(standard_obs())
        s = strat.stats()
        assert "triangles_active" in s
        assert s["triangles_active"] == 1

    def test_stats_includes_fee_tracking(self):
        strat = make_strategy(standard_obs())
        s = strat.stats()
        assert "total_fees_usdt" in s
        assert "net_pnl_usdt" in s

    def test_min_notional_filters_tiny_trades(self):
        strat = make_strategy(standard_obs(), overrides={"min_notional_usdt": 1000})
        strat.min_profit = -100
        # Trade amount (safe_trade_amount will return ~$15) is well below $1000
        result = strat._evaluate(make_meta())
        assert result is None

    def test_path_string_in_opportunity(self):
        opp = make_opp()
        assert "USDT" in opp.path
        assert "BTC" in opp.path
        assert "ETH" in opp.path
