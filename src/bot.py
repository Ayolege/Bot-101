import asyncio
import time
from typing import List, Optional
from loguru import logger

from src.exchanges.binance import BinanceExchange
from src.exchanges.kucoin import KuCoinExchange
from src.strategies.triangular import TriangularStrategy
from src.strategies.cross_exchange import CrossExchangeStrategy
from src.risk.manager import RiskManager
from src.utils.performance import PerformanceTracker, TradeRecord
from src.utils.notifier import TelegramNotifier


class ArbitrageBot:
    """
    Orchestrates all exchange connections, strategies, and risk management.
    Runs a tight async scan loop — designed for a Singapore VPS
    (~1-5ms to Binance & KuCoin) to maximise arbitrage capture rate.
    """

    def __init__(self, config: dict):
        self.cfg = config
        self.paper = config["bot"]["mode"] == "paper"
        self._running = False

        # --- Exchanges ---
        self.binance: Optional[BinanceExchange] = None
        self.kucoin: Optional[KuCoinExchange] = None

        # --- Risk ---
        self.risk = RiskManager(config["risk"])

        # --- Performance ---
        self.perf = PerformanceTracker(config["performance"])

        # --- Notifications ---
        self.notifier = TelegramNotifier()

        # --- Strategy handles (created after exchange connect) ---
        self.tri_strategy: Optional[TriangularStrategy] = None
        self.cross_strategy: Optional[CrossExchangeStrategy] = None

        self._scan_count = 0
        self._last_report = time.time()
        self._report_interval = config["performance"].get("report_interval_minutes", 60) * 60

    async def start(self) -> None:
        logger.info(f"[Bot] Starting in {'PAPER' if self.paper else 'LIVE'} mode")
        if not self.paper:
            logger.warning("[Bot] LIVE MODE — real orders will be placed!")

        await self._init_exchanges()
        await self._init_strategies()

        self._running = True
        await asyncio.gather(
            self._scan_loop(),
            self._housekeeping_loop(),
        )

    async def stop(self) -> None:
        logger.info("[Bot] Stopping…")
        self._running = False
        await self._disconnect_exchanges()
        logger.info(self.perf.report())

    async def _init_exchanges(self) -> None:
        ex_cfg = self.cfg["exchanges"]
        import os

        if ex_cfg.get("binance", {}).get("enabled", False):
            binance_cfg = {
                **ex_cfg["binance"],
                "api_key": os.getenv("BINANCE_API_KEY", ""),
                "api_secret": os.getenv("BINANCE_API_SECRET", ""),
            }
            self.binance = BinanceExchange(binance_cfg)
            await self.binance.connect()
            await self.binance.fetch_balances()
            usdt_bal = self.binance.get_balance("USDT")
            logger.info(f"[Bot] Binance USDT balance: {usdt_bal:.2f}")

        if ex_cfg.get("kucoin", {}).get("enabled", False):
            kucoin_cfg = {
                **ex_cfg["kucoin"],
                "api_key": os.getenv("KUCOIN_API_KEY", ""),
                "api_secret": os.getenv("KUCOIN_API_SECRET", ""),
                "passphrase": os.getenv("KUCOIN_PASSPHRASE", ""),
            }
            self.kucoin = KuCoinExchange(kucoin_cfg)
            await self.kucoin.connect()
            await self.kucoin.fetch_balances()

    async def _init_strategies(self) -> None:
        strat_cfg = self.cfg["strategies"]
        tri_cfg = strat_cfg.get("triangular", {})
        cross_cfg = strat_cfg.get("cross_exchange", {})

        # --- Triangular on Binance ---
        if tri_cfg.get("enabled") and self.binance:
            # Build symbol list from triangle definitions
            symbols = set()
            for triangle in tri_cfg.get("triangles", []):
                a, b, c = triangle
                symbols.add(f"{b}/{a}")   # e.g. BTC/USDT
                symbols.add(f"{c}/{b}")   # e.g. ETH/BTC
                symbols.add(f"{c}/{a}")   # e.g. ETH/USDT

            await self.binance.subscribe_order_books(list(symbols))
            # Allow WebSocket to warm up
            await asyncio.sleep(2)

            self.tri_strategy = TriangularStrategy(
                exchange=self.binance,
                config=tri_cfg,
                risk=self.risk,
                paper_mode=self.paper,
            )
            logger.info(f"[Bot] Triangular strategy ready — {len(symbols)} symbols")

        # --- Cross-exchange Binance ↔ KuCoin ---
        if cross_cfg.get("enabled") and self.binance and self.kucoin:
            pairs = cross_cfg.get("pairs", [])
            await self.binance.subscribe_order_books(pairs)
            await self.kucoin.subscribe_order_books(pairs)
            await asyncio.sleep(2)

            self.cross_strategy = CrossExchangeStrategy(
                exchange_a=self.binance,
                exchange_b=self.kucoin,
                config=cross_cfg,
                risk=self.risk,
                paper_mode=self.paper,
            )
            logger.info(f"[Bot] Cross-exchange strategy ready — {len(pairs)} pairs")

        # Set starting balance for risk manager
        starting = self.binance.get_balance("USDT") if self.binance else 1000.0
        self.risk.set_starting_balance(starting)

    async def _scan_loop(self) -> None:
        """Hot loop — scans for opportunities as fast as order books update."""
        logger.info("[Bot] Scan loop started.")
        while self._running:
            if self.risk.state.halted:
                await asyncio.sleep(5)
                continue

            tasks = []

            if self.tri_strategy:
                opportunities = self.tri_strategy.scan()
                for opp in opportunities:
                    logger.info(f"[Bot] Triangle opportunity: {opp}")
                    tasks.append(self.tri_strategy.execute(opp))

            if self.cross_strategy:
                opportunities = self.cross_strategy.scan()
                for opp in opportunities:
                    logger.info(f"[Bot] Cross-exchange opportunity: {opp}")
                    tasks.append(self.cross_strategy.execute(opp))

            if tasks:
                await asyncio.gather(*tasks)

            self._scan_count += 1
            # Yield to event loop — avoid CPU spin
            await asyncio.sleep(0.01)

    async def _housekeeping_loop(self) -> None:
        """Periodic tasks: balance refresh, reporting, daily reset."""
        while self._running:
            await asyncio.sleep(self.cfg["bot"].get("heartbeat_interval", 30))

            # Refresh balances
            try:
                if self.binance:
                    await self.binance.fetch_balances()
                if self.kucoin:
                    await self.kucoin.fetch_balances()
            except Exception as e:
                logger.warning(f"[Bot] Balance refresh error: {e}")

            # Periodic performance report
            if time.time() - self._last_report >= self._report_interval:
                report = self.perf.report()
                logger.info(report)
                await self.notifier.send(report)
                self._last_report = time.time()

            # Midnight daily reset
            current_hour = time.localtime().tm_hour
            if current_hour == 0:
                self.risk.reset_daily()

            logger.debug(
                f"[Bot] Heartbeat | scans={self._scan_count} | "
                f"risk={self.risk.summary()}"
            )

    async def _disconnect_exchanges(self) -> None:
        if self.binance:
            await self.binance.disconnect()
        if self.kucoin:
            await self.kucoin.disconnect()
