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
    base: str           # e.g. "USDT"
    mid: str            # e.g. "BTC"
    quote: str          # e.g. "ETH"
    path: str           # human-readable path string
    # Direction 1: base → mid → quote → base
    rate_bm: float      # bid of BASE/MID (sell base, get mid)
    rate_mq: float      # bid of MID/QUOTE (sell mid, get quote)
    rate_qb: float      # bid of QUOTE/BASE (sell quote, get base)
    profit_pct: float
    trade_amount_usdt: float
    expected_profit_usdt: float
    detected_at: float = field(default_factory=time.time)

    def __str__(self) -> str:
        return (
            f"{self.path} | profit={self.profit_pct:.4f}% "
            f"| expected=${self.expected_profit_usdt:.4f}"
        )


class TriangularStrategy:
    """
    Detects and executes triangular arbitrage on a single exchange.
    All three legs are on the same exchange → no withdrawal delays,
    no transfer fees — ideal for high-frequency cycling.

    Triangle anatomy (example USDT→BTC→ETH→USDT):
      Leg 1: Buy BTC with USDT   → use ASK price of BTC/USDT
      Leg 2: Buy ETH with BTC    → use ASK price of ETH/BTC
      Leg 3: Sell ETH for USDT   → use BID price of ETH/USDT
    """

    def __init__(
        self,
        exchange: BaseExchange,
        config: dict,
        risk: RiskManager,
        paper_mode: bool = True,
    ):
        self.exchange = exchange
        self.cfg = config
        self.risk = risk
        self.paper = paper_mode
        self.fee = config.get("fee_rate", 0.001)
        self.min_profit = config.get("min_profit_pct", 0.15)
        self.base_amount = config.get("trade_amount_usdt", 100)
        self.max_amount = config.get("max_trade_amount_usdt", 1000)
        self.triangles: List[Tuple[str, str, str]] = [
            tuple(t) for t in config.get("triangles", [])
        ]
        self._opportunities_found = 0
        self._trades_executed = 0
        self._total_profit = 0.0

    def _build_symbol(self, base: str, quote: str) -> str:
        return f"{base}/{quote}"

    def _get_best_ask(self, symbol: str) -> Optional[float]:
        ob = self.exchange.get_order_book(symbol)
        if ob and ob.best_ask:
            return ob.best_ask[0]
        return None

    def _get_best_bid(self, symbol: str) -> Optional[float]:
        ob = self.exchange.get_order_book(symbol)
        if ob and ob.best_bid:
            return ob.best_bid[0]
        return None

    def _get_ask_qty(self, symbol: str) -> float:
        ob = self.exchange.get_order_book(symbol)
        if ob and ob.best_ask:
            return ob.best_ask[1]
        return 0.0

    def _get_bid_qty(self, symbol: str) -> float:
        ob = self.exchange.get_order_book(symbol)
        if ob and ob.best_bid:
            return ob.best_bid[1]
        return 0.0

    def _ob_age_ok(self, *symbols: str, max_age_ms: float = 2000) -> bool:
        for sym in symbols:
            ob = self.exchange.get_order_book(sym)
            if not ob or ob.age_ms() > max_age_ms:
                return False
        return True

    def scan(self) -> List[TriangleOpportunity]:
        opportunities = []
        for (a, b, c) in self.triangles:
            opp = self._evaluate_triangle(a, b, c)
            if opp:
                opportunities.append(opp)
        return opportunities

    def _evaluate_triangle(
        self, a: str, b: str, c: str
    ) -> Optional[TriangleOpportunity]:
        """
        Triangle A→B→C→A
          Leg 1: sell A, buy B  → buy B/A pair at ASK  → rate = 1 / ask(B/A)
          Leg 2: sell B, buy C  → buy C/B pair at ASK  → rate = 1 / ask(C/B)
          Leg 3: sell C, buy A  → sell C/A pair at BID → rate = bid(C/A)

        Or if pairs exist in reverse:
          Leg 1: sell A, buy B  → sell A/B pair at BID → rate = bid(A/B)
        """
        # Determine available pair directions
        sym_ba = self._build_symbol(b, a)   # BTC/USDT
        sym_cb = self._build_symbol(c, b)   # ETH/BTC
        sym_ca = self._build_symbol(c, a)   # ETH/USDT

        markets = self.exchange._exchange.markets if self.exchange._exchange else {}
        # All three must exist in this direction on Binance
        if not all(s in markets for s in [sym_ba, sym_cb, sym_ca]):
            return None

        if not self._ob_age_ok(sym_ba, sym_cb, sym_ca):
            return None

        # Leg 1: Buy B using A → pay ask of B/A (e.g. buy BTC with USDT at BTC/USDT ask)
        ask_ba = self._get_best_ask(sym_ba)
        # Leg 2: Buy C using B → pay ask of C/B (e.g. buy ETH with BTC at ETH/BTC ask)
        ask_cb = self._get_best_ask(sym_cb)
        # Leg 3: Sell C for A → receive bid of C/A (e.g. sell ETH for USDT at ETH/USDT bid)
        bid_ca = self._get_best_bid(sym_ca)

        if not (ask_ba and ask_cb and bid_ca):
            return None

        # Rates: how much of next currency per unit of current
        rate_ab = 1 / ask_ba    # units of B per unit of A
        rate_bc = 1 / ask_cb    # units of C per unit of B
        rate_ca = bid_ca        # units of A per unit of C

        profit_pct = triangle_profit(rate_ab, rate_bc, rate_ca, self.fee)

        if profit_pct < self.min_profit:
            return None

        amount = self.risk.safe_trade_amount(self.base_amount, self._usdt_balance())
        expected_profit = amount * profit_pct / 100

        self._opportunities_found += 1
        return TriangleOpportunity(
            base=a, mid=b, quote=c,
            path=f"{a}→{b}→{c}→{a}",
            rate_bm=rate_ab,
            rate_mq=rate_bc,
            rate_qb=rate_ca,
            profit_pct=profit_pct,
            trade_amount_usdt=amount,
            expected_profit_usdt=expected_profit,
        )

    async def execute(self, opp: TriangleOpportunity) -> bool:
        balance = self._usdt_balance()
        ok, reason = self.risk.can_trade(opp.trade_amount_usdt, balance)
        if not ok:
            logger.warning(f"[Triangular] Skipping: {reason}")
            return False

        logger.info(f"[Triangular] Executing: {opp}")

        if self.paper:
            return await self._paper_execute(opp)
        else:
            return await self._live_execute(opp)

    async def _paper_execute(self, opp: TriangleOpportunity) -> bool:
        """Simulate execution — no real orders placed."""
        await asyncio.sleep(0.05)   # simulate exchange round-trip latency
        self.risk.record_trade(opp.expected_profit_usdt)
        self._trades_executed += 1
        self._total_profit += opp.expected_profit_usdt
        logger.success(
            f"[Triangular][PAPER] {opp.path} | "
            f"+{opp.expected_profit_usdt:.4f} USDT | "
            f"session total: {self._total_profit:.4f} USDT"
        )
        return True

    async def _live_execute(self, opp: TriangleOpportunity) -> bool:
        """
        Execute three legs sequentially with timeout guard.
        If any leg fails, the bot logs the partial fill for manual reconciliation.
        """
        sym_ba = self._build_symbol(opp.mid, opp.base)
        sym_cb = self._build_symbol(opp.quote, opp.mid)
        sym_ca = self._build_symbol(opp.quote, opp.base)

        self.risk.open_order()
        try:
            # --- Leg 1: Buy MID with BASE (e.g. buy BTC with USDT) ---
            amt_base = opp.trade_amount_usdt   # USDT amount
            ask_ba = self._get_best_ask(sym_ba)
            if not ask_ba:
                raise RuntimeError(f"No ask for {sym_ba}")
            amt_mid = round_down(amt_base / ask_ba, 6)

            leg1 = await asyncio.wait_for(
                retry_async(
                    lambda: self.exchange.create_market_order(sym_ba, "buy", amt_mid),
                    label="Leg1",
                ),
                timeout=self.cfg.get("order_timeout_seconds", 10),
            )
            logger.debug(f"[Triangular][L1] {leg1}")

            # --- Leg 2: Buy QUOTE with MID (e.g. buy ETH with BTC) ---
            filled_mid = leg1.filled or amt_mid
            ask_cb = self._get_best_ask(sym_cb)
            if not ask_cb:
                raise RuntimeError(f"No ask for {sym_cb}")
            amt_quote = round_down(filled_mid / ask_cb, 6)

            leg2 = await asyncio.wait_for(
                retry_async(
                    lambda: self.exchange.create_market_order(sym_cb, "buy", amt_quote),
                    label="Leg2",
                ),
                timeout=self.cfg.get("order_timeout_seconds", 10),
            )
            logger.debug(f"[Triangular][L2] {leg2}")

            # --- Leg 3: Sell QUOTE for BASE (e.g. sell ETH for USDT) ---
            filled_quote = leg2.filled or amt_quote
            amt_sell = round_down(filled_quote, 6)

            leg3 = await asyncio.wait_for(
                retry_async(
                    lambda: self.exchange.create_market_order(sym_ca, "sell", amt_sell),
                    label="Leg3",
                ),
                timeout=self.cfg.get("order_timeout_seconds", 10),
            )
            logger.debug(f"[Triangular][L3] {leg3}")

            actual_profit = leg3.cost - opp.trade_amount_usdt
            self.risk.record_trade(actual_profit)
            self._trades_executed += 1
            self._total_profit += actual_profit
            logger.success(
                f"[Triangular][LIVE] {opp.path} | "
                f"profit={actual_profit:.4f} USDT | "
                f"session total={self._total_profit:.4f} USDT"
            )
            return True

        except asyncio.TimeoutError:
            logger.error(f"[Triangular] Order timeout during {opp.path} — PARTIAL FILL RISK")
            return False
        except Exception as e:
            logger.error(f"[Triangular] Execution error: {e}")
            return False
        finally:
            self.risk.close_order()

    def _usdt_balance(self) -> float:
        return self.exchange.get_balance("USDT")

    def stats(self) -> dict:
        return {
            "strategy": "triangular",
            "opportunities_found": self._opportunities_found,
            "trades_executed": self._trades_executed,
            "total_profit_usdt": round(self._total_profit, 4),
        }
