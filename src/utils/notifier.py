import os
import aiohttp
from loguru import logger


class TelegramNotifier:
    """Optional Telegram alert channel for trade notifications and risk events."""

    def __init__(self):
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self.enabled = bool(self.token and self.chat_id)

    async def send(self, message: str) -> None:
        if not self.enabled:
            return
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": message, "parse_mode": "Markdown"}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status != 200:
                        logger.warning(f"[Telegram] Send failed: {await resp.text()}")
        except Exception as e:
            logger.warning(f"[Telegram] Notification failed: {e}")

    async def alert_trade(self, strategy: str, profit: float, detail: str) -> None:
        emoji = "✅" if profit > 0 else "⚠️"
        msg = f"{emoji} *{strategy}*\n{detail}\nProfit: `{profit:+.4f} USDT`"
        await self.send(msg)

    async def alert_halt(self, reason: str) -> None:
        await self.send(f"🛑 *BOT HALTED*\nReason: {reason}")
