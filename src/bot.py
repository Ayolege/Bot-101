import asyncio
import time
from typing import Optional
from loguru import logger

from src.exchanges.binance import BinanceExchange
from src.strategies.triangular import TriangularStrategy
from src.risk.manager import RiskManager
from src.utils.performance import PerformanceTracker
from src.utils.notifier import TelegramNotifier


class ArbitrageBot:
    """
    Binance-only triangular arbitrage bot.
    Runs a tight async scan loop — designed for Tokyo VPS
    (co-located with Binance's AWS ap-northeast-1 cluster).
    Auto-executes all opportunities that clear the profit threshold
    and pass risk checks.
    """

    def __init__(self, config: dict):
        self.cfg = config
        self._running = False

        self.binance: Optional[BinanceExchange] = None
        self.risk = RiskManager(config["risk"])
        self.perf = PerformanceTracker(config["performance"])
        self.notifier = TelegramNotifier()
        self.strategy: Optional[TriangularStrategy] = None

        self._scan_count = 0
        self._last_report = time.time()
        self._report_interval = (
            config["performance"].get("report_interval_minutes", 60) * 60
        )

    async def start(self) -> None:
        logger.info("[Bot] Initialising Binance connection…")
        await self._init_exchange()
        await self._init_strategy()

        self._running = True
        logger.info("[Bot] Auto-execute loop started.")
        await asyncio.gather(
            self._scan_loop(),
            self._housekeeping_loop(),
        )

    async def stop(self) -> None:
        logger.info("[Bot] Stopping…")
        self._running = False
        if self.binance:
            await self.binance.disconnect()
        report = self.perf.report()
        logger.info(report)
        await self.notifier.send(f"Bot stopped.\n{report}")

    async def _init_exchange(self) -> None:
        cfg = self.cfg["exchanges"]["binance"]
        self.binance = BinanceExchange(cfg)
        await self.binance.connect()
        await self.binance.fetch_balances()
        usdt = self.binance.get_balance("USDT")
        min_bal = self.cfg["risk"]["min_balance_usdt"]
        trade_amt = self.cfg["triangular"]["trade_amount_usdt"]

        logger.info(f"[Bot] Binance USDT balance: ${usdt:.2f}")

        if usdt < min_bal:
            raise RuntimeError(
                f"Balance ${usdt:.2f} is below the minimum reserve of ${min_bal}. "
                "Top up your Binance USDT balance and restart."
            )

        if usdt < trade_amt + min_bal:
            logger.warning(
                f"[Bot] Balance ${usdt:.2f} is tight. "
                f"Recommended minimum for this config: ${trade_amt + min_bal:.0f} "
                f"(${trade_amt} trade + ${min_bal} reserve). "
                "Bot will trade with reduced size."
            )

        self.risk.set_starting_balance(usdt)
        logger.info(
            f"[Bot] Capital: ${usdt:.2f} | "
            f"Trade size: ${trade_amt} | "
            f"Daily loss limit: ${self.cfg['risk']['max_daily_loss_usdt']} | "
            f"Fee budget: ${self.cfg['risk']['max_daily_fees_usdt']}/day"
        )

    async def _init_strategy(self) -> None:
        tri_cfg = self.cfg["triangular"]
        # Build the full symbol set needed across all triangles
        symbols: set[str] = set()
        for t in tri_cfg.get("triangles", []):
            a, b, c = t
            symbols.update([f"{b}/{a}", f"{c}/{b}", f"{c}/{a}"])

        await self.binance.subscribe_order_books(list(symbols))
        # Warm-up: let WebSocket populate order books before scanning
        logger.info(f"[Bot] Warming up order books ({len(symbols)} symbols)…")
        await asyncio.sleep(3)

        self.strategy = TriangularStrategy(
            exchange=self.binance,
            config=tri_cfg,
            risk=self.risk,
        )
        logger.info(
            f"[Bot] Strategy ready — {len(tri_cfg.get('triangles', []))} triangles, "
            f"{len(symbols)} symbols"
        )

    async def _scan_loop(self) -> None:
        while self._running:
            if self.risk.state.halted:
                await asyncio.sleep(5)
                continue

            if self.strategy:
                for opp in self.strategy.scan():
                    logger.info(f"[Bot] Opportunity: {opp}")
                    await self.notifier.alert_trade(
                        "triangular", opp.expected_profit_usdt, str(opp)
                    )
                    asyncio.create_task(self.strategy.execute(opp))

            self._scan_count += 1
            await asyncio.sleep(0.01)   # yield to event loop

    async def _housekeeping_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self.cfg["bot"].get("heartbeat_interval", 30))

            # Refresh balance
            try:
                await self.binance.fetch_balances()
            except Exception as e:
                logger.warning(f"[Bot] Balance refresh error: {e}")

            # Periodic report
            if time.time() - self._last_report >= self._report_interval:
                report = self.perf.report()
                logger.info(report)
                await self.notifier.send(report)
                self._last_report = time.time()

            # Midnight daily PnL reset
            if time.localtime().tm_hour == 0 and time.localtime().tm_min == 0:
                self.risk.reset_daily()

            if self.risk.state.halted:
                await self.notifier.alert_halt(self.risk.state.halt_reason)

            risk_summary = self.risk.summary()
            logger.debug(f"[Bot] Heartbeat | scans={self._scan_count} | {risk_summary}")
