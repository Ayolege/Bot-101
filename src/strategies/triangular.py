"""
Triangular arbitrage executor.

Receives pre-validated TriangleMeta objects from the discovery module,
scans their order books every loop tick, and executes when a spread
passes all profit + risk checks.

Profit formula — three legs, fee deducted from each received amount:

  A_final = A_start × [bid(QUOTE/USDT) / (ask(MID/USDT) × ask(QUOTE/MID))]
             × (1 − fee)³

Only executes when:
  1. profit_pct ≥ min_profit_pct  (net of fees)
  2. profit ≥ fee_cost × min_profit_fee_ratio  (safety margin)
  3. All order book data is fresh (< MAX_OB_AGE_MS)
  4. All risk limits pass (can_trade gate)
  5. All legs clear Binance's minimum notional (~$5)

On partial fill / leg timeout, fires a recovery market order immediately.
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import List, Optional

from loguru import logger

from src.exchanges.base import BaseExchange
from src.risk.manager import RiskManager
from src.strategies.discovery import TriangleMeta
from src.utils.helpers import triangle_profit, round_down, retry_async


@dataclass
class TriangleOpportunity:
    meta: TriangleMeta
    profit_pct: float
    trade_amount_usdt: float
    expected_profit_usdt: float
    expected_fees_usdt: float
    detected_at: float = field(default_factory=time.time)

    @property
    def path(self) -> str:
        return self.meta.path

    def __str__(self) -> str:
        return (
            f"{self.path} | profit={self.profit_pct:.4f}% "
            f"(~${self.expected_profit_usdt:.4f}) fees~${self.expected_fees_usdt:.4f}"
        )


class TriangularStrategy:
    MAX_OB_AGE_MS = 1500

    def __init__(
        self,
        exchange: BaseExchange,
        triangles: List[TriangleMeta],
        config: dict,
        risk: RiskManager,
    ):
        self.exchange = exchange
        self.triangles = triangles
        self.cfg = config
        self.risk = risk

        self.fee = config.get("fee_rate", 0.001)
        self.min_profit = config.get("min_profit_pct", 0.5)
        self.base_amount = config.get("trade_amount_usdt", 15)
        self.order_timeout = config.get("order_timeout_seconds", 8)
        self.min_notional = config.get("min_notional_usdt", 6)

        self._opps_found = 0
        self._trades_executed = 0
        self._total_profit = 0.0
        self._total_fees = 0.0

    # ── Scan ──────────────────────────────────────────────────

    def scan(self) -> List[TriangleOpportunity]:
        results = []
        for meta in self.triangles:
            opp = self._evaluate(meta)
            if opp:
                results.append(opp)
        # Return sorted: highest profit first so the best trade executes first
        results.sort(key=lambda o: o.profit_pct, reverse=True)
        return results

    def _evaluate(self, meta: TriangleMeta) -> Optional[TriangleOpportunity]:
        # Freshness check — stale data is worse than a missed trade
        if not self._fresh(meta.sym_mid_base, meta.sym_quote_mid, meta.sym_quote_base):
            return None

        ask_mid_base = self._ask(meta.sym_mid_base)   # ask(BTC/USDT)
        ask_quote_mid = self._ask(meta.sym_quote_mid)  # ask(ETH/BTC)
        bid_quote_base = self._bid(meta.sym_quote_base) # bid(ETH/USDT)

        if not (ask_mid_base and ask_quote_mid and bid_quote_base):
            return None

        rate_ab = 1 / ask_mid_base    # USDT → MID conversion rate
        rate_bc = 1 / ask_quote_mid   # MID  → QUOTE conversion rate
        rate_ca = bid_quote_base      # QUOTE → USDT conversion rate

        profit_pct = triangle_profit(rate_ab, rate_bc, rate_ca, self.fee)
        if profit_pct < self.min_profit:
            return None

        # Profit must be meaningfully above fees, not just above zero
        ok, reason = self.risk.validate_expected_profit(profit_pct, self.fee, num_legs=3)
        if not ok:
            logger.debug(f"[Tri] {meta.path}: {reason}")
            return None

        amount = self.risk.safe_trade_amount(self.base_amount, self._usdt_balance())
        if amount < self.min_notional:
            return None

        # Check leg 2 clears minimum notional (in MID units, compared proportionally)
        leg2_mid_qty = amount / ask_mid_base
        leg2_notional_approx = leg2_mid_qty * ask_quote_mid  # approximate QUOTE cost
        if leg2_notional_approx < self.min_notional / ask_mid_base:
            return None

        fees = self.risk.estimate_fees(amount, self.fee, legs=3)
        self._opps_found += 1

        return TriangleOpportunity(
            meta=meta,
            profit_pct=profit_pct,
            trade_amount_usdt=amount,
            expected_profit_usdt=amount * profit_pct / 100,
            expected_fees_usdt=fees,
        )

    # ── Execute ───────────────────────────────────────────────

    async def execute(self, opp: TriangleOpportunity) -> bool:
        ok, reason = self.risk.can_trade(opp.trade_amount_usdt, self._usdt_balance())
        if not ok:
            logger.warning(f"[Tri] Blocked: {reason}")
            return False

        logger.info(f"[Tri] Executing: {opp}")
        self.risk.open_order()
        try:
            return await self._run_legs(opp)
        finally:
            self.risk.close_order()

    async def _run_legs(self, opp: TriangleOpportunity) -> bool:
        m = opp.meta
        qty_mid: float = 0.0
        qty_quote: float = 0.0
        stage = 0

        try:
            # ── Leg 1: buy MID with USDT ──────────────────────
            ask_mb = self._ask(m.sym_mid_base)
            if not ask_mb:
                raise RuntimeError(f"No ask for {m.sym_mid_base}")
            qty_mid = round_down(opp.trade_amount_usdt / ask_mb, 6)
            stage = 1

            leg1 = await asyncio.wait_for(
                retry_async(
                    lambda: self.exchange.create_market_order(m.sym_mid_base, "buy", qty_mid),
                    label="Leg1",
                ),
                timeout=self.order_timeout,
            )

            # ── Leg 2: buy QUOTE with MID ─────────────────────
            filled_mid = leg1.filled or qty_mid
            ask_qm = self._ask(m.sym_quote_mid)
            if not ask_qm:
                raise RuntimeError(f"No ask for {m.sym_quote_mid}")
            qty_quote = round_down(filled_mid / ask_qm, 6)
            stage = 2

            leg2 = await asyncio.wait_for(
                retry_async(
                    lambda: self.exchange.create_market_order(m.sym_quote_mid, "buy", qty_quote),
                    label="Leg2",
                ),
                timeout=self.order_timeout,
            )

            # ── Leg 3: sell QUOTE for USDT ────────────────────
            filled_quote = leg2.filled or qty_quote
            qty_sell = round_down(filled_quote, 6)
            stage = 3

            leg3 = await asyncio.wait_for(
                retry_async(
                    lambda: self.exchange.create_market_order(m.sym_quote_base, "sell", qty_sell),
                    label="Leg3",
                ),
                timeout=self.order_timeout,
            )

            # ── Record ────────────────────────────────────────
            actual_profit = (leg3.cost or 0) - opp.trade_amount_usdt
            fees = (leg1.fee or 0) + (leg2.fee or 0) + (leg3.fee or 0) or opp.expected_fees_usdt

            self.risk.record_trade(actual_profit, fees_paid=fees)
            self._trades_executed += 1
            self._total_profit += actual_profit
            self._total_fees += fees

            level = "success" if actual_profit > 0 else "warning"
            getattr(logger, level)(
                f"[Tri] {m.path} | "
                f"profit={actual_profit:+.4f} USDT | "
                f"fees={fees:.4f} USDT | "
                f"session={self._total_profit:.4f} USDT"
            )
            return True

        except asyncio.TimeoutError:
            logger.error(
                f"[Tri] Timeout at leg {stage} of {m.path} — attempting recovery…"
            )
            await self._recover(stage, m, qty_mid, qty_quote)
            return False

        except Exception as e:
            logger.error(f"[Tri] Error at leg {stage} of {m.path}: {e}")
            if stage >= 1:
                await self._recover(stage, m, qty_mid, qty_quote)
            return False

    async def _recover(
        self,
        stage: int,
        m: TriangleMeta,
        qty_mid: float,
        qty_quote: float,
    ) -> None:
        """Sell open position back to USDT to limit loss after a failed leg."""
        try:
            if stage == 1 and qty_mid > 0:
                sell_qty = round_down(qty_mid * 0.995, 6)
                logger.warning(f"[Recovery] Selling {sell_qty} {m.mid} → USDT")
                await asyncio.wait_for(
                    self.exchange.create_market_order(m.sym_mid_base, "sell", sell_qty),
                    timeout=15,
                )
                logger.info("[Recovery] Position closed.")
            elif stage >= 2 and qty_quote > 0:
                sell_qty = round_down(qty_quote * 0.995, 6)
                logger.warning(f"[Recovery] Selling {sell_qty} {m.quote} → USDT")
                await asyncio.wait_for(
                    self.exchange.create_market_order(m.sym_quote_base, "sell", sell_qty),
                    timeout=15,
                )
                logger.info("[Recovery] Position closed.")
        except Exception as e:
            logger.critical(
                f"[Recovery] FAILED: {e}. "
                "MANUAL INTERVENTION REQUIRED — log in to Binance immediately."
            )

    async def _paper_execute(self, opp: TriangleOpportunity) -> bool:
        await asyncio.sleep(0.05)
        self.risk.record_trade(opp.expected_profit_usdt, fees_paid=opp.expected_fees_usdt)
        self._trades_executed += 1
        self._total_profit += opp.expected_profit_usdt
        self._total_fees += opp.expected_fees_usdt
        logger.success(
            f"[Tri][DRY RUN] {opp.path} | "
            f"+{opp.expected_profit_usdt:.4f} USDT | "
            f"session: {self._total_profit:.4f} USDT"
        )
        return True

    # ── Helpers ───────────────────────────────────────────────

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
            "triangles_active": len(self.triangles),
            "opportunities_found": self._opps_found,
            "trades_executed": self._trades_executed,
            "total_profit_usdt": round(self._total_profit, 4),
            "total_fees_usdt": round(self._total_fees, 4),
            "net_pnl_usdt": round(self._total_profit - self._total_fees, 4),
        }
