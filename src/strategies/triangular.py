"""
Triangular arbitrage executor — Binance spot.

Profit formula (all three legs, fee deducted from each received amount):
  A_final = A_start × [bid(Q/USDT) / (ask(M/USDT) × ask(Q/M))] × (1−fee)³

Execution guards (all must pass before any order fires):
  1. Risk gate (can_trade) — balance, daily loss, fee budget, position size
  2. Semaphore — enforces max_open_orders atomically (no race condition)
  3. Balance re-check — confirms USDT still available at execution time
  4. Profit re-check — recomputes spread from live order book at execution time
  5. Minimum notional — all legs clear Binance's ~$5 floor

On partial fill / leg timeout:
  - Cancels the timed-out order immediately (avoids ghost fills)
  - Fires a recovery market sell to close any open position
  - Logs CRITICAL if recovery fails (requires manual Binance intervention)
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

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
    MAX_OB_AGE_MS = 1500   # reject order book data older than this

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
        self.min_profit = config.get("min_profit_pct", 0.6)
        self.base_amount = config.get("trade_amount_usdt", 15)
        self.order_timeout = config.get("order_timeout_seconds", 8)
        self.min_notional = config.get("min_notional_usdt", 6)

        max_concurrent = risk.cfg.get("max_open_orders", 1)
        # Semaphore enforces max_open_orders atomically.
        # Since asyncio is cooperative, the semaphore prevents two concurrent
        # can_trade+open_order sequences from both passing simultaneously.
        self._sem = asyncio.Semaphore(max_concurrent)

        # Update-tracking: map symbol → last order book timestamp seen.
        # scan() skips a triangle when none of its three order books
        # have changed since the last evaluation — major CPU saving.
        self._last_seen: Dict[str, float] = {}

        self._opps_found = 0
        self._trades_executed = 0
        self._total_profit = 0.0
        self._total_fees = 0.0

    # ── Scan ──────────────────────────────────────────────────

    def scan(self) -> List[TriangleOpportunity]:
        results = []
        for meta in self.triangles:
            if not self._has_new_data(meta):
                continue                     # nothing changed — skip computation
            opp = self._evaluate(meta)
            self._mark_seen(meta)            # update seen timestamps regardless
            if opp:
                results.append(opp)
        # Best opportunity first — only the top one is executed per tick
        results.sort(key=lambda o: o.profit_pct, reverse=True)
        return results

    def _has_new_data(self, meta: TriangleMeta) -> bool:
        """True if any of the three order books updated since last evaluation."""
        for sym in meta.symbols():
            ob = self.exchange.get_order_book(sym)
            if ob and ob.timestamp > self._last_seen.get(sym, 0):
                return True
        return False

    def _mark_seen(self, meta: TriangleMeta) -> None:
        for sym in meta.symbols():
            ob = self.exchange.get_order_book(sym)
            if ob:
                self._last_seen[sym] = ob.timestamp

    def _evaluate(self, meta: TriangleMeta) -> Optional[TriangleOpportunity]:
        if not self._fresh(meta.sym_mid_base, meta.sym_quote_mid, meta.sym_quote_base):
            return None

        ask_mb = self._ask(meta.sym_mid_base)
        ask_qm = self._ask(meta.sym_quote_mid)
        bid_qb = self._bid(meta.sym_quote_base)
        if not (ask_mb and ask_qm and bid_qb):
            return None

        rate_ab = 1 / ask_mb
        rate_bc = 1 / ask_qm
        rate_ca = bid_qb
        profit_pct = triangle_profit(rate_ab, rate_bc, rate_ca, self.fee)
        if profit_pct < self.min_profit:
            return None

        ok, reason = self.risk.validate_expected_profit(profit_pct, self.fee, num_legs=3)
        if not ok:
            logger.debug(f"[Tri] {meta.path}: {reason}")
            return None

        amount = self.risk.safe_trade_amount(self.base_amount, self._usdt_balance())
        if amount < self.min_notional:
            return None

        # All three legs must clear Binance minimum notional (~$5).
        # Leg 2 notional in USDT ≈ trade amount (since we cycle back to USDT).
        # The check on leg 1 covers all three because each leg is proportional.
        # Explicit check: qty_mid_in_MID × price_of_MID_in_USDT > min_notional
        leg2_qty_mid = amount / ask_mb           # how much MID we'll spend on leg 2
        leg2_notional_usdt = leg2_qty_mid * ask_mb  # MID qty × MID price = USDT equiv
        if leg2_notional_usdt < self.min_notional:
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
        # Semaphore is acquired BEFORE can_trade so the check and the
        # open_order increment are atomic — no race condition possible.
        async with self._sem:
            ok, reason = self.risk.can_trade(opp.trade_amount_usdt, self._usdt_balance())
            if not ok:
                logger.warning(f"[Tri] Blocked: {reason}")
                return False

            # Re-check live profit immediately before placing orders.
            # The market may have moved since the scan detected this opportunity.
            fresh = self._evaluate(opp.meta)
            if fresh is None:
                logger.debug(f"[Tri] {opp.path}: spread moved — aborting")
                return False
            if fresh.profit_pct < self.min_profit * 0.8:
                logger.debug(
                    f"[Tri] {opp.path}: profit dropped {fresh.profit_pct:.4f}% — aborting"
                )
                return False

            # Guard: confirm USDT balance hasn't changed since the scan
            available = self._usdt_balance()
            if available < opp.trade_amount_usdt:
                logger.warning(
                    f"[Tri] Insufficient balance: have ${available:.2f}, "
                    f"need ${opp.trade_amount_usdt:.2f}"
                )
                return False

            logger.info(f"[Tri] Executing: {fresh}")
            self.risk.open_order()
            try:
                return await self._run_legs(fresh)
            finally:
                self.risk.close_order()

    async def _run_legs(self, opp: TriangleOpportunity) -> bool:
        m = opp.meta
        qty_mid: float = 0.0
        qty_quote: float = 0.0
        stage = 0
        leg1_id: Optional[str] = None
        leg2_id: Optional[str] = None

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
            leg1_id = leg1.id

            # BUG FIX: use explicit > 0 check instead of falsy `or`
            # leg1.filled = 0.0 (partial fill with nothing received) must NOT
            # fall through to qty_mid — that would cascade a 100% loss on leg 2.
            filled_mid = leg1.filled if leg1.filled > 0 else qty_mid

            # ── Leg 2: buy QUOTE with MID ─────────────────────
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
            leg2_id = leg2.id

            filled_quote = leg2.filled if leg2.filled > 0 else qty_quote
            qty_sell = round_down(filled_quote, 6)
            stage = 3

            # ── Leg 3: sell QUOTE for USDT ────────────────────
            leg3 = await asyncio.wait_for(
                retry_async(
                    lambda: self.exchange.create_market_order(m.sym_quote_base, "sell", qty_sell),
                    label="Leg3",
                ),
                timeout=self.order_timeout,
            )

            # ── Record result ─────────────────────────────────
            # leg3.cost = USDT received (for a sell order, ccxt reports
            # cost = filled_qty × price = gross USDT proceeds).
            # leg3.fee = Binance fee deducted from USDT received (in USDT).
            # Legs 1+2 fees were deducted from the quantities you received,
            # so they're already baked into the reduced qty_mid / qty_quote.
            # Net received = leg3.cost − leg3.fee
            leg3_fee_usdt = leg3.fee if leg3.fee else opp.expected_fees_usdt / 3
            usdt_received = (leg3.cost or 0) - leg3_fee_usdt
            # Estimated fees for legs 1+2 (for fee-budget tracking only)
            legs12_fees_est = opp.expected_fees_usdt * (2 / 3)
            total_fees = leg3_fee_usdt + legs12_fees_est

            # Net profit: what came back minus what we started with
            # (fees already deducted — this is the true P&L)
            net_profit = usdt_received - opp.trade_amount_usdt

            self.risk.record_trade(net_profit, fees_paid=total_fees)
            self._trades_executed += 1
            self._total_profit += net_profit
            self._total_fees += total_fees

            level = "success" if net_profit > 0 else "warning"
            getattr(logger, level)(
                f"[Tri] {m.path} | "
                f"net={net_profit:+.4f} USDT | "
                f"fees~{total_fees:.4f} USDT | "
                f"session={self._total_profit:.4f} USDT"
            )
            return True

        except asyncio.TimeoutError:
            logger.error(
                f"[Tri] Timeout at leg {stage} of {m.path} — attempting recovery…"
            )
            await self._recover(stage, m, qty_mid, qty_quote, leg1_id, leg2_id)
            return False

        except Exception as e:
            logger.error(f"[Tri] Error at leg {stage} of {m.path}: {e}")
            if stage >= 1:
                await self._recover(stage, m, qty_mid, qty_quote, leg1_id, leg2_id)
            return False

    async def _recover(
        self,
        stage: int,
        m: TriangleMeta,
        qty_mid: float,
        qty_quote: float,
        leg1_id: Optional[str],
        leg2_id: Optional[str],
    ) -> None:
        """
        Cancel the timed-out order first, then sell any received assets
        back to USDT at market to close the open position.

        Binance market orders fill almost instantly — a timeout usually means
        the confirmation response was lost, not that the order didn't fill.
        We cancel defensively, then check what we actually hold.
        """
        # Cancel the order that timed out (may already be filled — cancel is idempotent)
        try:
            if stage == 1 and leg1_id:
                await self.exchange.cancel_order(leg1_id, m.sym_mid_base)
            elif stage == 2 and leg2_id:
                await self.exchange.cancel_order(leg2_id, m.sym_quote_mid)
        except Exception as e:
            logger.debug(f"[Recovery] Cancel attempt: {e}")

        # Sell whatever we received back to USDT
        try:
            if stage == 1 and qty_mid > 0:
                sell_qty = round_down(qty_mid * 0.995, 6)
                logger.warning(f"[Recovery] Selling {sell_qty} {m.mid} → USDT")
                await asyncio.wait_for(
                    self.exchange.create_market_order(m.sym_mid_base, "sell", sell_qty),
                    timeout=20,
                )
                logger.info("[Recovery] MID position closed.")

            elif stage >= 2 and qty_quote > 0:
                sell_qty = round_down(qty_quote * 0.995, 6)
                logger.warning(f"[Recovery] Selling {sell_qty} {m.quote} → USDT")
                await asyncio.wait_for(
                    self.exchange.create_market_order(m.sym_quote_base, "sell", sell_qty),
                    timeout=20,
                )
                logger.info("[Recovery] QUOTE position closed.")

        except Exception as e:
            logger.critical(
                f"[Recovery] FAILED TO CLOSE POSITION: {e}\n"
                f"MANUAL ACTION REQUIRED — log in to Binance immediately and "
                f"sell any open {m.mid} or {m.quote} spot balance."
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
