import asyncio
import time
from typing import Optional

from loguru import logger

from src.exchanges.binance import BinanceExchange
from src.strategies.discovery import discover_triangles, triangles_to_symbols
from src.strategies.triangular import TriangularStrategy
from src.risk.manager import RiskManager
from src.utils.performance import PerformanceTracker
from src.utils.notifier import TelegramNotifier


class ArbitrageBot:
    """
    Binance-only triangular arbitrage bot.

    Startup sequence:
      1. Connect to Binance REST + verify account permissions
      2. Auto-discover all valid liquid USDT triangles
      3. Subscribe to WebSocket bookTicker for all required symbols
      4. Run scan loop — checks every discovered triangle every 10ms
      5. Auto-execute whenever profit + risk checks pass
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
        # Date-based daily reset — avoids the race where a trade that completes
        # at 00:00:01 has its PnL wiped by a reset triggered at the same second.
        self._last_reset_date: str = ""

    async def start(self) -> None:
        logger.info("[Bot] Initialising…")
        await self._init_exchange()
        await self._init_strategy()
        self._running = True
        logger.info("[Bot] Auto-execute scan loop started.")
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

    # ── Initialisation ────────────────────────────────────────

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
                f"Balance ${usdt:.2f} is below the ${min_bal} reserve minimum. "
                "Top up your Binance USDT spot balance and restart."
            )

        if usdt < trade_amt + min_bal:
            logger.warning(
                f"[Bot] Tight balance: ${usdt:.2f}. "
                f"Recommended: ${trade_amt + min_bal:.0f} "
                f"(${trade_amt} trade + ${min_bal} reserve). "
                "Bot will trade with reduced size."
            )

        self.risk.set_starting_balance(usdt)
        logger.info(
            f"[Bot] Capital=${usdt:.2f} | "
            f"TradeSize=${trade_amt} | "
            f"DailyLossLimit=${self.cfg['risk']['max_daily_loss_usdt']} | "
            f"FeeBudget=${self.cfg['risk']['max_daily_fees_usdt']}/day"
        )

    async def _init_strategy(self) -> None:
        tri_cfg = self.cfg["triangular"]
        disc_cfg = self.cfg.get("discovery", {})

        # ── Triangle discovery ─────────────────────────────────
        manual = tri_cfg.get("triangles", [])
        auto_discover = tri_cfg.get("auto_discover", True)

        if auto_discover:
            triangles = await discover_triangles(
                exchange=self.binance,
                min_volume_usdt=disc_cfg.get("min_pair_volume_usdt", 500_000),
                max_triangles=disc_cfg.get("max_triangles", 300),
                bridge_currencies=disc_cfg.get("bridge_currencies", ["BTC", "ETH", "BNB"]),
            )
            # Prepend any manually specified triangles (in config) as overrides
            if manual:
                from src.strategies.discovery import TriangleMeta
                manual_metas = []
                existing_paths = {(t.mid, t.quote) for t in triangles}
                for t in manual:
                    a, b, c = t
                    if (b, c) not in existing_paths:
                        manual_metas.append(
                            TriangleMeta(
                                base=a, mid=b, quote=c,
                                sym_mid_base=f"{b}/{a}",
                                sym_quote_mid=f"{c}/{b}",
                                sym_quote_base=f"{c}/{a}",
                                min_volume_usdt=0,
                            )
                        )
                triangles = manual_metas + triangles
        else:
            # Manual-only mode — build TriangleMeta from config list
            from src.strategies.discovery import TriangleMeta
            triangles = [
                TriangleMeta(
                    base=t[0], mid=t[1], quote=t[2],
                    sym_mid_base=f"{t[1]}/{t[0]}",
                    sym_quote_mid=f"{t[2]}/{t[1]}",
                    sym_quote_base=f"{t[2]}/{t[0]}",
                    min_volume_usdt=0,
                )
                for t in manual
            ]

        if not triangles:
            raise RuntimeError(
                "[Bot] No triangles discovered. "
                "Check min_pair_volume_usdt or add manual triangles."
            )

        # ── WebSocket subscription ─────────────────────────────
        symbols = triangles_to_symbols(triangles)
        logger.info(
            f"[Bot] Subscribing to {len(symbols)} symbols "
            f"for {len(triangles)} triangles…"
        )
        await self.binance.subscribe_order_books(symbols)

        # Warm-up: let WebSocket populate order books
        logger.info("[Bot] Warming up order books (3s)…")
        await asyncio.sleep(3)

        self.strategy = TriangularStrategy(
            exchange=self.binance,
            triangles=triangles,
            config=tri_cfg,
            risk=self.risk,
            notifier=self.notifier,
        )
        logger.info(
            f"[Bot] Ready — scanning {len(triangles)} triangles "
            f"across {len(symbols)} symbols."
        )

    # ── Loops ─────────────────────────────────────────────────

    async def _scan_loop(self) -> None:
        while self._running:
            if self.risk.state.halted:
                await asyncio.sleep(5)
                continue

            if self.strategy:
                opportunities = self.strategy.scan()
                if opportunities:
                    best = opportunities[0]
                    logger.info(f"[Bot] Opportunity: {best}")
                    # create_task: non-blocking — scan continues immediately.
                    # The semaphore inside execute() enforces max_open_orders.
                    # Telegram alert fires from inside execute() AFTER the
                    # trade actually completes — alerting on every spotted
                    # opportunity would spam the chat in volatile markets.
                    asyncio.create_task(self.strategy.execute(best))

            self._scan_count += 1
            # 1ms sleep is enough to keep CPU usage healthy on a 1-vCPU VPS
            # while still giving 1000 scan iterations per second. The bot is
            # WebSocket-bound (market updates arrive at most ~100/sec per
            # symbol), so faster scanning doesn't translate to faster trades.
            # asyncio.sleep(0) hogged 96%+ CPU and starved the housekeeping
            # loop on small VPSes.
            await asyncio.sleep(0.001)

    async def _housekeeping_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self.cfg["bot"].get("heartbeat_interval", 30))

            try:
                await self.binance.fetch_balances()
            except Exception as e:
                logger.warning(f"[Bot] Balance refresh error: {e}")

            if time.time() - self._last_report >= self._report_interval:
                report = self.perf.report()
                logger.info(report)
                await self.notifier.send(report)
                self._last_report = time.time()

            # Date-based daily reset — safe against the midnight race where a
            # trade completing at 00:00:01 would have its PnL wiped immediately.
            today = time.strftime("%Y-%m-%d")
            if self._last_reset_date and self._last_reset_date != today:
                self.risk.reset_daily()
            self._last_reset_date = today

            if self.risk.state.halted:
                await self.notifier.alert_halt(self.risk.state.halt_reason)

            stats = self.strategy.stats() if self.strategy else {}
            risk = self.risk.summary()
            logger.info(
                f"[Bot] Heartbeat | scans={self._scan_count} | "
                f"triangles={stats.get('triangles_active', 0)} | "
                f"trades={stats.get('trades_executed', 0)} | "
                f"pnl={risk.get('daily_pnl_usdt', 0):.4f} USDT"
            )
