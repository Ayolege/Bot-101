import time
import asyncio
from decimal import Decimal, ROUND_DOWN
from typing import Optional
from loguru import logger


def round_down(value: float, decimals: int) -> float:
    factor = Decimal(10) ** decimals
    return float(Decimal(str(value)).quantize(Decimal(1) / factor, rounding=ROUND_DOWN))


def pct_profit(buy_price: float, sell_price: float, fee_rate: float) -> float:
    """Net profit % after fees on both legs."""
    effective_sell = sell_price * (1 - fee_rate)
    effective_buy = buy_price * (1 + fee_rate)
    return ((effective_sell - effective_buy) / effective_buy) * 100


def triangle_profit(
    rate_ab: float,
    rate_bc: float,
    rate_ca: float,
    fee: float,
) -> float:
    """
    Compute net profit % for A→B→C→A triangle.
    rate_ab: how much B you get per unit of A (bid price of A/B pair or 1/ask of B/A)
    rate_bc: how much C you get per unit of B
    rate_ca: how much A you get per unit of C
    Returns profit as a percentage (positive = profitable).
    """
    gross = rate_ab * rate_bc * rate_ca
    net = gross * ((1 - fee) ** 3)
    return (net - 1) * 100


async def retry_async(coro_fn, retries: int = 3, base_delay: float = 0.5, label: str = ""):
    for attempt in range(retries):
        try:
            return await coro_fn()
        except Exception as e:
            if attempt == retries - 1:
                raise
            delay = base_delay * (2 ** attempt)
            logger.warning(f"[{label}] Attempt {attempt+1} failed: {e}. Retrying in {delay:.1f}s")
            await asyncio.sleep(delay)


def timestamp_ms() -> int:
    return int(time.time() * 1000)


def format_currency(value: float, symbol: str = "USDT") -> str:
    return f"{value:,.4f} {symbol}"
