#!/usr/bin/env python3
"""
Binance Triangular Arbitrage Bot
Auto-executes opportunities — real money on live mode.

Usage:
  python main.py                    # live mode (default)
  python main.py --dry-run          # paper/simulation mode
  python main.py --config path.yaml
"""

import asyncio
import argparse
import signal
import sys
import time
import yaml
from dotenv import load_dotenv
from loguru import logger

from src.utils.logger import setup_logger
from src.bot import ArbitrageBot


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Binance Triangular Arbitrage Bot")
    p.add_argument("--config", default="config/config.yaml")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Paper mode: scan and log but place no real orders",
    )
    p.add_argument("--log-level", default=None)
    return p.parse_args()


async def countdown(seconds: int) -> None:
    """Give operator a window to abort before live trading begins."""
    for i in range(seconds, 0, -1):
        logger.warning(f"  LIVE MODE — starting in {i}s … (Ctrl-C to abort)")
        await asyncio.sleep(1)


async def main() -> None:
    load_dotenv()
    args = parse_args()
    config = load_config(args.config)

    if args.dry_run:
        config["bot"]["mode"] = "paper"

    log_level = args.log_level or config["bot"].get("log_level", "INFO")
    setup_logger(log_level)

    live = config["bot"]["mode"] == "live"

    logger.info("=" * 62)
    logger.info("  Binance Triangular Arbitrage Bot")
    logger.info("  Optimised VPS: Tokyo (Vultr HF / AWS ap-northeast-1)")
    logger.info(f"  Mode : {'LIVE — AUTO-EXECUTING REAL ORDERS' if live else 'DRY RUN (paper)'}")
    logger.info("=" * 62)

    if live:
        await countdown(config["bot"].get("startup_countdown", 5))

    bot = ArbitrageBot(config)

    loop = asyncio.get_event_loop()

    def _shutdown(sig_name: str) -> None:
        logger.info(f"[Main] {sig_name} received — shutting down…")
        asyncio.create_task(bot.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig.name: _shutdown(s))

    try:
        await bot.start()
    except KeyboardInterrupt:
        await bot.stop()
    except Exception as e:
        logger.critical(f"[Main] Fatal: {e}")
        await bot.stop()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
