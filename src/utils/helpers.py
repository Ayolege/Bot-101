import time
import asyncio
from decimal import Decimal, ROUND_DOWN
from typing import Optional
from loguru import logger


def round_down(value: float, decimals: int) -> float:
    factor = Decimal(10) ** decimals
    return float(Decimal(str(value)).quantize(Decimal(1) / factor, rounding=ROUND_DOWN))


def pct_profit(buy_price: float, sell_price: float, fee_rate: float) -> float:
    effective_sell = sell_price * (1 - fee_rate)
    effective_buy = buy_price * (1 + fee_rate)
    return ((effective_sell - effective_buy) / effective_buy) * 100


def triangle_profit(rate_ab: float, rate_bc: float, rate_ca: float, fee: float) -> float:
    """
    Net profit % for USDT → MID → QUOTE → USDT.
    Exact formula: A_final = A_start × rate_ab × rate_bc × rate_ca × (1−fee)³
    Returns profit as percentage; positive = profitable after all three fees.
    """
    gross = rate_ab * rate_bc * rate_ca
    net = gross * ((1 - fee) ** 3)
    return (net - 1) * 100


# Errors Binance returns that are permanent — never retry these.
# Retrying would place duplicate orders or waste time on definitively rejected orders.
_NON_RETRYABLE_MESSAGES = frozenset([
    "insufficient balance",
    "insufficient funds",
    "invalid quantity",
    "lot size",
    "min notional",
    "permission denied",
    "account suspended",
    "api key",
    "unauthorized",
    "invalid symbol",
])


def _is_retryable(exc: Exception) -> bool:
    msg = str(exc).lower()
    return not any(kw in msg for kw in _NON_RETRYABLE_MESSAGES)


async def retry_async(coro_fn, retries: int = 3, base_delay: float = 0.3, label: str = ""):
    """
    Retry a coroutine on transient errors (network, rate-limit).
    Raises immediately on permanent errors (insufficient funds, invalid order).
    Delay doubles on each attempt: 0.3s → 0.6s → 1.2s.
    """
    for attempt in range(retries):
        try:
            return await coro_fn()
        except Exception as e:
            if not _is_retryable(e):
                logger.error(f"[{label}] Non-retryable error — aborting: {e}")
                raise
            if attempt == retries - 1:
                raise
            delay = base_delay * (2 ** attempt)
            logger.warning(f"[{label}] Attempt {attempt + 1} failed: {e}. Retrying in {delay:.2f}s")
            await asyncio.sleep(delay)


def timestamp_ms() -> int:
    return int(time.time() * 1000)


def format_currency(value: float, symbol: str = "USDT") -> str:
    return f"{value:,.4f} {symbol}"
