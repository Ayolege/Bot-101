import asyncio
import time
from dataclasses import dataclass, field
from typing import List, Optional
from loguru import logger

from src.exchanges.base import BaseExchange
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
    expected_fees_usdt: float     # estimated gross fees for this cycle
    detected_at: float = field(default_factory=time.time)

    def __str__(self) -> str:
        return (
            f"{self.path} | profit={self.profit_pct:.4f}% "
            f"(~${self.expected_profit_usdt:.4f}) "
            f"fees~${self.expected_fees_usdt:.4f}"
        )


class TriangularStrategy:
    """
    Triangular arbitrage on Binance.

    Profit formula (all three legs, fees deducted from each received amount):
      A_final = A_start × (bid_CA / (ask_BA × ask_CB)) × (1 - fee)³

    The bot only executes when:
      1. profit_pct > min_profit_pct  (net of fees)
      2. profit passes the fee-safety-margin check in RiskManager
      3. All order book data is fresh (< MAX_OB_AGE_MS)
      4. All risk limits pass

    On partial fill (a leg times out or fails), the strategy
    attempts a recovery market order to close the open leg rather
    than leaving an unhedged position.
    """

    MAX_OB_AGE_MS = 1500   # reject stale data rather than trade on it

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
        self.order_timeout = config.get("order_timeout_seconds", 8)
        self.triangles = [tuple(t) for t in config.get("triangles", [])]

        self._opportunities_found = 0
        self._trades_executed = 0
        self._total_profit = 0.0
        self._total_fees = 0.0

    def scan(self) -> List[TriangleOpportunity]:
        return [
            opp
            for (a, b, c) in self.triangles
            if (opp := self._evaluate(a, b, c)) is not None
        ]

    def _evaluate(self, a: str, b: str, c: str) -> Optional[TriangleOpportunity]:
        sym_ba = f"{b}/{a}"
        sym_cb = f"{c}/{b}"
        sym_ca = f"{c}/{a}"

        markets = self.exchange._exchange.markets if self.exchange._exchange else {}
        if not all(s in markets for s in [sym_ba, sym_cb, sym_ca]):
            return None

        if not self._fresh(sym_ba, sym_cb, sym_ca):
            return None

        ask_ba = self._ask(sym_ba)
        ask_cb = self._ask(sym_cb)
        bid_ca = self._bid(sym_ca)
        if not (ask_ba and ask_cb and bid_ca):
            return None

        rate_ab = 1 / ask_ba
        rate_bc = 1 / ask_cb
        rate_ca = bid_ca
        profit_pct = triangle_profit(rate_ab, rate_bc, rate_ca, self.fee)

        if profit_pct < self.min_profit:
            return None

        # Validate profit is sufficiently above fees (not just above zero)
        ok, reason = self.risk.validate_expected_profit(profit_pct, self.fee, num_legs=3)
        if not ok:
            logger.debug(f"[Triangular] Skipping {a}→{b}→{c}: {reason}")
            return None

        amount = self.risk.safe_trade_amount(self.base_amount, self._usdt_balance())
        if amount <= 0:
            return None

        fees = self.risk.estimate_fees(amount, self.fee, legs=3)
        self._opportunities_found += 1

        return TriangleOpportunity(
            base=a, mid=b, quote=c,
            path=f"{a}→{b}→{c}→{a}",
            rate_bm=rate_ab, rate_mq=rate_bc, rate_qb=rate_ca,
            profit_pct=profit_pct,
            trade_amount_usdt=amount,
            expected_profit_usdt=amount * profit_pct / 100,
            expected_fees_usdt=fees,
        )

    async def execute(self, opp: TriangleOpportunity) -> bool:
        ok, reason = self.risk.can_trade(opp.trade_amount_usdt, self._usdt_balance())
        if not ok:
            logger.warning(f"[Triangular] Blocked: {reason}")
            return False

        logger.info(f"[Triangular] Executing: {opp}")
        self.risk.open_order()
        try:
            success = await self._run_legs(opp)
        finally:
            self.risk.close_order()
        return success

    async def _run_legs(self, opp: TriangleOpportunity) -> bool:
        sym_ba = f"{opp.mid}/{opp.base}"   # e.g. BTC/USDT
        sym_cb = f"{opp.quote}/{opp.mid}"  # e.g. ETH/BTC
        sym_ca = f"{opp.quote}/{opp.base}" # e.g. ETH/USDT

        qty_mid: float = 0.0
        qty_quote: float = 0.0
        stage = 0  # tracks how far we got for recovery

        try:
            # ── Leg 1: buy MID with BASE ──────────────────────────
            ask_ba = self._ask(sym_ba)
            if not ask_ba:
                raise RuntimeError(f"No ask price for {sym_ba}")
            qty_mid = round_down(opp.trade_amount_usdt / ask_ba, 6)
            stage = 1

            leg1 = await asyncio.wait_for(
                retry_async(
                    lambda: self.exchange.create_market_order(sym_ba, "buy", qty_mid),
                    label="Leg1",
                ),
                timeout=self.order_timeout,
            )

            # ── Leg 2: buy QUOTE with MID ─────────────────────────
            filled_mid = leg1.filled or qty_mid
            ask_cb = self._ask(sym_cb)
            if not ask_cb:
                raise RuntimeError(f"No ask price for {sym_cb}")
            qty_quote = round_down(filled_mid / ask_cb, 6)
            stage = 2

            leg2 = await asyncio.wait_for(
                retry_async(
                    lambda: self.exchange.create_market_order(sym_cb, "buy", qty_quote),
                    label="Leg2",
                ),
                timeout=self.order_timeout,
            )

            # ── Leg 3: sell QUOTE for BASE ────────────────────────
            filled_quote = leg2.filled or qty_quote
            qty_sell = round_down(filled_quote, 6)
            stage = 3

            leg3 = await asyncio.wait_for(
                retry_async(
                    lambda: self.exchange.create_market_order(sym_ca, "sell", qty_sell),
                    label="Leg3",
                ),
                timeout=self.order_timeout,
            )

            # ── Record result ─────────────────────────────────────
            actual_cost = leg3.cost or 0
            actual_profit = actual_cost - opp.trade_amount_usdt
            total_fees = (leg1.fee or 0) + (leg2.fee or 0) + (leg3.fee or 0)
            if total_fees == 0:
                total_fees = opp.expected_fees_usdt  # fallback estimate

            self.risk.record_trade(actual_profit, fees_paid=total_fees)
            self._trades_executed += 1
            self._total_profit += actual_profit
            self._total_fees += total_fees

            level = "success" if actual_profit > 0 else "warning"
            getattr(logger, level)(
                f"[Triangular] {opp.path} | "
                f"profit={actual_profit:+.4f} USDT | "
                f"fees={total_fees:.4f} USDT | "
                f"session={self._total_profit:.4f} USDT"
            )
            return True

        except asyncio.TimeoutError:
            logger.error(
                f"[Triangular] Timeout at leg {stage} of {opp.path} — "
                "attempting emergency recovery…"
            )
            await self._recover(stage, sym_ba, sym_cb, sym_ca, qty_mid, qty_quote)
            return False

        except Exception as e:
            logger.error(f"[Triangular] Error at leg {stage} of {opp.path}: {e}")
            if stage >= 2 and qty_quote > 0:
                await self._recover(stage, sym_ba, sym_cb, sym_ca, qty_mid, qty_quote)
            return False

    async def _recover(
        self,
        stage: int,
        sym_ba: str,
        sym_cb: str,
        sym_ca: str,
        qty_mid: float,
        qty_quote: float,
    ) -> None:
        """
        Emergency recovery for partial fills.
        If we're stuck holding MID or QUOTE after a leg failure,
        sell back to USDT immediately at market to limit loss exposure.
        """
        try:
            if stage == 1 and qty_mid > 0:
                # Leg 1 filled, leg 2 failed — sell MID back to BASE
                logger.warning(f"[Recovery] Selling {qty_mid} {sym_ba.split('/')[0]} back to USDT")
                sell_qty = round_down(qty_mid * 0.995, 6)  # slight buffer for fees
                await asyncio.wait_for(
                    self.exchange.create_market_order(sym_ba, "sell", sell_qty),
                    timeout=15,
                )
                logger.info("[Recovery] MID position closed.")

            elif stage >= 2 and qty_quote > 0:
                # Leg 2 filled, leg 3 failed — sell QUOTE back to BASE directly
                logger.warning(f"[Recovery] Selling {qty_quote} {sym_ca.split('/')[0]} back to USDT")
                sell_qty = round_down(qty_quote * 0.995, 6)
                await asyncio.wait_for(
                    self.exchange.create_market_order(sym_ca, "sell", sell_qty),
                    timeout=15,
                )
                logger.info("[Recovery] QUOTE position closed.")

        except Exception as e:
            logger.critical(
                f"[Recovery] FAILED to close open position: {e}. "
                "MANUAL INTERVENTION REQUIRED — log in to Binance immediately "
                "and close any open spot positions."
            )

    async def _paper_execute(self, opp: TriangleOpportunity) -> bool:
        await asyncio.sleep(0.05)
        self.risk.record_trade(opp.expected_profit_usdt, fees_paid=opp.expected_fees_usdt)
        self._trades_executed += 1
        self._total_profit += opp.expected_profit_usdt
        self._total_fees += opp.expected_fees_usdt
        logger.success(
            f"[Triangular][DRY RUN] {opp.path} | "
            f"+{opp.expected_profit_usdt:.4f} USDT | "
            f"session: {self._total_profit:.4f} USDT"
        )
        return True

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
            "total_fees_usdt": round(self._total_fees, 4),
            "net_pnl_usdt": round(self._total_profit - self._total_fees, 4),
        }
