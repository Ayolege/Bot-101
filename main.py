#!/usr/bin/env python3
"""
Arbitrage Bot — entry point.
Optimised for Nigerian traders running on a Singapore VPS.

Usage:
  python main.py                  # paper mode (from config)
  python main.py --mode live      # override to live
  python main.py --mode paper     # force paper mode
  python main.py --config path/to/config.yaml
"""

import asyncio
import argparse
import signal
import sys
from pathlib import Path
import yaml
from dotenv import load_dotenv
from loguru import logger

from src.utils.logger import setup_logger
from src.bot import ArbitrageBot


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Crypto Arbitrage Bot")
    parser.add_argument("--config", default="config/config.yaml", help="Config file path")
    parser.add_argument("--mode", choices=["paper", "live"], help="Override trading mode")
    parser.add_argument("--log-level", default=None, help="Log level (DEBUG/INFO/WARNING)")
    return parser.parse_args()


async def main() -> None:
    load_dotenv()
    args = parse_args()

    config = load_config(args.config)

    if args.mode:
        config["bot"]["mode"] = args.mode

    log_level = args.log_level or config["bot"].get("log_level", "INFO")
    setup_logger(log_level)

    bot = ArbitrageBot(config)

    # Graceful shutdown on SIGINT / SIGTERM
    loop = asyncio.get_event_loop()

    def shutdown(sig_name: str):
        logger.info(f"[Main] Received {sig_name} — shutting down…")
        asyncio.create_task(bot.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig.name: shutdown(s))

    logger.info("=" * 60)
    logger.info("  Arbitrage Bot — Optimised for Nigerian Traders")
    logger.info("  Recommended VPS: Singapore (AWS ap-southeast-1)")
    logger.info(f"  Mode: {config['bot']['mode'].upper()}")
    logger.info("=" * 60)

    try:
        await bot.start()
    except KeyboardInterrupt:
        await bot.stop()
    except Exception as e:
        logger.critical(f"[Main] Fatal error: {e}")
        await bot.stop()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
