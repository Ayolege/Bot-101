import asyncio
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from loguru import logger

from src.exchanges.base import BaseExchange, OrderBook
from src.risk.manager import RiskManager
from src.utils.helpers import triangle_profit, round_down, retry_async


@dataclass
class TriangleOpportunity:
    base: str
    mid: str
    quote: str
    path: str
    rate_bm: float
    rate_mq: float
    rate_qb: float
    profit_pct: float
    trade_amount_usdt: float
    expected_profit_usdt: float
    detected_at: float = field(default_factory=time.time)

    def __str__(self) -> str:
        return (
            f"{self.path} | profit={self.profit_pct:.4f}% "
            f"(~${self.expected_profit_usdt:.4f} USDT)"
        )


class TriangularStrategy:
    """
    Triangular arbitrage on Binance.

    Triangle anatomy — example USDT → BTC → ETH → USDT:
      Leg 1: Buy BTC with USDT   → pay ASK of BTC/USDT
      Leg 2: Buy ETH with BTC    → pay ASK of ETH/BTC
      Leg 3: Sell ETH for USDT   → receive BID of ETH/USDT

    All three legs are on Binance — no withdrawal delays,
    no transfer fees, no counterparty risk between exchanges.
    Executes only when net profit (after 3× fee) exceeds threshold.
    """

    # Stale order book data is more dangerous than a missed trade
    MAX_OB_AGE_MS = 1500

    def __init__(
        self,
        exchange: BaseExchange,
        config: dict,
        risk: RiskManager,
    ):
        self.exchange = exchange
        self.cfg = config
        self.risk = risk
        self.fee = config.get("fee_rate", 0.001)
        self.min_profit = config.get("min_profit_pct", 0.12)
        self.base_amount = config.get("trade_amount_usdt", 100)
        self.max_amount = config.get("max_trade_amount_usdt", 1000)
        self.order_timeout = config.get("order_timeout_seconds", 8)
        self.triangles: List[Tuple[str, str, str]] = [
            tuple(t) for t in config.get("triangles", [])
        ]
        self._opportunities_found = 0
        self._trades_executed = 0
        self._total_profit = 0.0

    def scan(self) -> List[TriangleOpportunity]:
        return [
            opp
            for (a, b, c) in self.triangles
            if (opp := self._evaluate(a, b, c)) is not None
        ]

    def _evaluate(self, a: str, b: str, c: str) -> Optional[TriangleOpportunity]:
        sym_ba = f"{b}/{a}"   # e.g. BTC/USDT
        sym_cb = f"{c}/{b}"   # e.g. ETH/BTC
        sym_ca = f"{c}/{a}"   # e.g. ETH/USDT

        markets = self.exchange._exchange.markets if self.exchange._exchange else {}
        if not all(s in markets for s in [sym_ba, sym_cb, sym_ca]):
            return None

        if not self._fresh(sym_ba, sym_cb, sym_ca):
            return None

        # Leg 1: buy B using A → pay ask(B/A)
        ask_ba = self._ask(sym_ba)
        # Leg 2: buy C using B → pay ask(C/B)
        ask_cb = self._ask(sym_cb)
        # Leg 3: sell C for A → receive bid(C/A)
        bid_ca = self._bid(sym_ca)

        if not (ask_ba and ask_cb and bid_ca):
            return None

        rate_ab = 1 / ask_ba
        rate_bc = 1 / ask_cb
        rate_ca = bid_ca
        profit_pct = triangle_profit(rate_ab, rate_bc, rate_ca, self.fee)

        if profit_pct < self.min_profit:
            return None

        amount = self.risk.safe_trade_amount(self.base_amount, self._usdt_balance())
        if amount <= 0:
            return None

        self._opportunities_found += 1
        return TriangleOpportunity(
            base=a, mid=b, quote=c,
            path=f"{a}→{b}→{c}→{a}",
            rate_bm=rate_ab,
            rate_mq=rate_bc,
            rate_qb=rate_ca,
            profit_pct=profit_pct,
            trade_amount_usdt=amount,
            expected_profit_usdt=amount * profit_pct / 100,
        )

    async def execute(self, opp: TriangleOpportunity) -> bool:
        ok, reason = self.risk.can_trade(opp.trade_amount_usdt, self._usdt_balance())
        if not ok:
            logger.warning(f"[Triangular] Blocked: {reason}")
            return False

        logger.info(f"[Triangular] Executing: {opp}")
        self.risk.open_order()
        try:
            return await self._run_legs(opp)
        finally:
            self.risk.close_order()

    async def _paper_execute(self, opp: TriangleOpportunity) -> bool:
        """Simulate execution — logs the trade without placing any orders."""
        await asyncio.sleep(0.05)
        self.risk.record_trade(opp.expected_profit_usdt)
        self._trades_executed += 1
        self._total_profit += opp.expected_profit_usdt
        logger.success(
            f"[Triangular][DRY RUN] {opp.path} | "
            f"+{opp.expected_profit_usdt:.4f} USDT | "
            f"session total: {self._total_profit:.4f} USDT"
        )
        return True

    async def _run_legs(self, opp: TriangleOpportunity) -> bool:
        sym_ba = f"{opp.mid}/{opp.base}"
        sym_cb = f"{opp.quote}/{opp.mid}"
        sym_ca = f"{opp.quote}/{opp.base}"

        try:
            # Leg 1 — buy MID with BASE
            ask_ba = self._ask(sym_ba)
            if not ask_ba:
                raise RuntimeError(f"No ask for {sym_ba}")
            qty_mid = round_down(opp.trade_amount_usdt / ask_ba, 6)

            leg1 = await asyncio.wait_for(
                retry_async(
                    lambda: self.exchange.create_market_order(sym_ba, "buy", qty_mid),
                    label="Leg1",
                ),
                timeout=self.order_timeout,
            )

            # Leg 2 — buy QUOTE with MID
            filled_mid = leg1.filled or qty_mid
            ask_cb = self._ask(sym_cb)
            if not ask_cb:
                raise RuntimeError(f"No ask for {sym_cb}")
            qty_quote = round_down(filled_mid / ask_cb, 6)

            leg2 = await asyncio.wait_for(
                retry_async(
                    lambda: self.exchange.create_market_order(sym_cb, "buy", qty_quote),
                    label="Leg2",
                ),
                timeout=self.order_timeout,
            )

            # Leg 3 — sell QUOTE for BASE
            filled_quote = leg2.filled or qty_quote
            qty_sell = round_down(filled_quote, 6)

            leg3 = await asyncio.wait_for(
                retry_async(
                    lambda: self.exchange.create_market_order(sym_ca, "sell", qty_sell),
                    label="Leg3",
                ),
                timeout=self.order_timeout,
            )

            actual_profit = (leg3.cost or 0) - opp.trade_amount_usdt
            self.risk.record_trade(actual_profit)
            self._trades_executed += 1
            self._total_profit += actual_profit
            logger.success(
                f"[Triangular] {opp.path} | "
                f"profit={actual_profit:+.4f} USDT | "
                f"session={self._total_profit:.4f} USDT"
            )
            return True

        except asyncio.TimeoutError:
            logger.error(
                f"[Triangular] Order timeout on {opp.path} — "
                "PARTIAL FILL POSSIBLE — check Binance manually"
            )
            return False
        except Exception as e:
            logger.error(f"[Triangular] Leg error: {e}")
            return False

    # ── Helpers ──────────────────────────────────────────────

    def _ask(self, symbol: str) -> Optional[float]:
        ob = self.exchange.get_order_book(symbol)
        return ob.best_ask[0] if ob and ob.best_ask else None

    def _bid(self, symbol: str) -> Optional[float]:
        ob = self.exchange.get_order_book(symbol)
        return ob.best_bid[0] if ob and ob.best_bid else None

    def _fresh(self, *symbols: str) -> bool:
        for sym in symbols:
            ob = self.exchange.get_order_book(sym)
            if not ob or ob.age_ms() > self.MAX_OB_AGE_MS:
                return False
        return True

    def _usdt_balance(self) -> float:
        return self.exchange.get_balance("USDT")

    def stats(self) -> dict:
        return {
            "opportunities_found": self._opportunities_found,
            "trades_executed": self._trades_executed,
            "total_profit_usdt": round(self._total_profit, 4),
        }
