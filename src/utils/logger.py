import sys
import os
from loguru import logger
from pathlib import Path


def setup_logger(log_level: str = "INFO", log_file: str = "logs/bot.log") -> None:
    Path("logs").mkdir(exist_ok=True)
    logger.remove()
    logger.add(
        sys.stdout,
        level=log_level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{line}</cyan> — <level>{message}</level>",
        colorize=True,
    )
    logger.add(
        log_file,
        level=log_level,
        rotation="50 MB",
        retention="7 days",
        compression="zip",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{line} — {message}",
        enqueue=True,  # thread-safe async logging
    )


def get_logger(name: str):
    return logger.bind(name=name)
