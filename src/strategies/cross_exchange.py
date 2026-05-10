import asyncio
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from loguru import logger

from src.exchanges.base import BaseExchange, OrderBook
from src.risk.manager import RiskManager
from src.utils.helpers import pct_profit, round_down, retry_async


@dataclass
class CrossExchangeOpportunity:
    symbol: str
    buy_exchange: str
    sell_exchange: str
    buy_price: float
    sell_price: float
    profit_pct: float
    trade_amount_usdt: float
    expected_profit_usdt: float
    detected_at: float = field(default_factory=time.time)

    def __str__(self) -> str:
        return (
            f"{self.symbol} | buy@{self.buy_exchange}={self.buy_price:.6f} "
            f"sell@{self.sell_exchange}={self.sell_price:.6f} | "
            f"profit={self.profit_pct:.4f}% | ~${self.expected_profit_usdt:.4f}"
        )


class CrossExchangeStrategy:
    """
    Detects price discrepancies for the same pair across two exchanges
    (Binance and KuCoin) and executes simultaneous buy+sell using
    pre-positioned funds on both sides.

    IMPORTANT: This strategy does NOT transfer crypto between exchanges.
    Both exchanges must have pre-funded balances in both the base and
    quote currencies before running this strategy.

    Example: BTC/USDT arbitrage
      - Binance has: USDT (to buy BTC)
      - KuCoin has:  BTC  (to sell BTC)
      After the trade, balances are rebalanced offline periodically.
    """

    def __init__(
        self,
        exchange_a: BaseExchange,
        exchange_b: BaseExchange,
        config: dict,
        risk: RiskManager,
        paper_mode: bool = True,
    ):
        self.ex_a = exchange_a
        self.ex_b = exchange_b
        self.cfg = config
        self.risk = risk
        self.paper = paper_mode
        self.min_profit = config.get("min_profit_pct", 0.3)
        self.base_amount = config.get("trade_amount_usdt", 200)
        self.max_amount = config.get("max_trade_amount_usdt", 2000)
        self.fee_a = config.get("binance_fee", 0.001)
        self.fee_b = config.get("kucoin_fee", 0.001)
        self.pairs: List[str] = config.get("pairs", [])
        self._opportunities_found = 0
        self._trades_executed = 0
        self._total_profit = 0.0

    def scan(self) -> List[CrossExchangeOpportunity]:
        opportunities = []
        for pair in self.pairs:
            opp = self._evaluate_pair(pair)
            if opp:
                opportunities.append(opp)
        return opportunities

    def _evaluate_pair(self, symbol: str) -> Optional[CrossExchangeOpportunity]:
        ob_a = self.ex_a.get_order_book(symbol)
        ob_b = self.ex_b.get_order_book(symbol)

        if not ob_a or not ob_b:
            return None

        # Reject stale data (> 2 seconds old)
        if ob_a.age_ms() > 2000 or ob_b.age_ms() > 2000:
            return None

        ask_a = ob_a.best_ask
        bid_a = ob_a.best_bid
        ask_b = ob_b.best_ask
        bid_b = ob_b.best_bid

        if not (ask_a and bid_a and ask_b and bid_b):
            return None

        # Direction 1: Buy on A, Sell on B
        profit_ab = pct_profit(ask_a[0], bid_b[0], max(self.fee_a, self.fee_b))
        # Direction 2: Buy on B, Sell on A
        profit_ba = pct_profit(ask_b[0], bid_a[0], max(self.fee_a, self.fee_b))

        if profit_ab >= self.min_profit:
            amount = self.risk.safe_trade_amount(
                self.base_amount, self.ex_a.get_balance("USDT")
            )
            self._opportunities_found += 1
            return CrossExchangeOpportunity(
                symbol=symbol,
                buy_exchange=self.ex_a.name,
                sell_exchange=self.ex_b.name,
                buy_price=ask_a[0],
                sell_price=bid_b[0],
                profit_pct=profit_ab,
                trade_amount_usdt=amount,
                expected_profit_usdt=amount * profit_ab / 100,
            )

        if profit_ba >= self.min_profit:
            amount = self.risk.safe_trade_amount(
                self.base_amount, self.ex_b.get_balance("USDT")
            )
            self._opportunities_found += 1
            return CrossExchangeOpportunity(
                symbol=symbol,
                buy_exchange=self.ex_b.name,
                sell_exchange=self.ex_a.name,
                buy_price=ask_b[0],
                sell_price=bid_a[0],
                profit_pct=profit_ba,
                trade_amount_usdt=amount,
                expected_profit_usdt=amount * profit_ba / 100,
            )

        return None

    async def execute(self, opp: CrossExchangeOpportunity) -> bool:
        buy_ex = self.ex_a if opp.buy_exchange == self.ex_a.name else self.ex_b
        sell_ex = self.ex_b if opp.sell_exchange == self.ex_b.name else self.ex_a

        balance = buy_ex.get_balance("USDT")
        ok, reason = self.risk.can_trade(opp.trade_amount_usdt, balance)
        if not ok:
            logger.warning(f"[CrossExchange] Skipping: {reason}")
            return False

        logger.info(f"[CrossExchange] Executing: {opp}")

        if self.paper:
            return await self._paper_execute(opp)
        else:
            return await self._live_execute(opp, buy_ex, sell_ex)

    async def _paper_execute(self, opp: CrossExchangeOpportunity) -> bool:
        await asyncio.sleep(0.05)
        self.risk.record_trade(opp.expected_profit_usdt)
        self._trades_executed += 1
        self._total_profit += opp.expected_profit_usdt
        logger.success(
            f"[CrossExchange][PAPER] {opp.symbol} "
            f"{opp.buy_exchange}→{opp.sell_exchange} | "
            f"+{opp.expected_profit_usdt:.4f} USDT | "
            f"session total: {self._total_profit:.4f} USDT"
        )
        return True

    async def _live_execute(
        self,
        opp: CrossExchangeOpportunity,
        buy_ex: BaseExchange,
        sell_ex: BaseExchange,
    ) -> bool:
        """
        Simultaneously buy on buy_ex and sell on sell_ex.
        Both legs are launched concurrently to minimise price drift.
        """
        self.risk.open_order()
        self.risk.open_order()

        # Calculate base asset quantity from USDT trade amount
        base_asset = opp.symbol.split("/")[0]
        qty = round_down(opp.trade_amount_usdt / opp.buy_price, 6)

        timeout = self.cfg.get("order_timeout_seconds", 10)

        try:
            buy_task = asyncio.create_task(
                asyncio.wait_for(
                    retry_async(
                        lambda: buy_ex.create_market_order(opp.symbol, "buy", qty),
                        label=f"Buy@{opp.buy_exchange}",
                    ),
                    timeout=timeout,
                )
            )
            sell_task = asyncio.create_task(
                asyncio.wait_for(
                    retry_async(
                        lambda: sell_ex.create_market_order(opp.symbol, "sell", qty),
                        label=f"Sell@{opp.sell_exchange}",
                    ),
                    timeout=timeout,
                )
            )

            results = await asyncio.gather(buy_task, sell_task, return_exceptions=True)

            buy_result, sell_result = results
            if isinstance(buy_result, Exception):
                logger.error(f"[CrossExchange] BUY leg failed: {buy_result}")
                return False
            if isinstance(sell_result, Exception):
                logger.error(f"[CrossExchange] SELL leg failed: {sell_result}")
                return False

            buy_cost = buy_result.cost
            sell_cost = sell_result.cost
            actual_profit = sell_cost - buy_cost
            self.risk.record_trade(actual_profit)
            self._trades_executed += 1
            self._total_profit += actual_profit
            logger.success(
                f"[CrossExchange][LIVE] {opp.symbol} | "
                f"profit={actual_profit:.4f} USDT | "
                f"session total={self._total_profit:.4f} USDT"
            )
            return True

        except Exception as e:
            logger.error(f"[CrossExchange] Execution error: {e}")
            return False
        finally:
            self.risk.close_order()
            self.risk.close_order()

    def stats(self) -> dict:
        return {
            "strategy": "cross_exchange",
            "opportunities_found": self._opportunities_found,
            "trades_executed": self._trades_executed,
            "total_profit_usdt": round(self._total_profit, 4),
        }
