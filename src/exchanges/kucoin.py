import asyncio
import json
import time
from typing import Dict, List, Optional
import aiohttp
import ccxt.async_support as ccxt
from loguru import logger

from .base import BaseExchange, OrderBook, Balance, Order


class KuCoinExchange(BaseExchange):
    """
    KuCoin connector.
    KuCoin's matching engine is also Singapore-based — ideal for a
    Singapore VPS trading cross-exchange arbitrage vs Binance.
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.name = "kucoin"
        self._exchange: Optional[ccxt.kucoin] = None
        self._ws_token: Optional[str] = None
        self._ws_url: Optional[str] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._subscribed_symbols: List[str] = []
        self._reconnect_delay = config.get("websocket_reconnect_delay", 1)

    async def connect(self) -> None:
        self._exchange = ccxt.kucoin(
            {
                "apiKey": self.config.get("api_key", ""),
                "secret": self.config.get("api_secret", ""),
                "password": self.config.get("passphrase", ""),
                "enableRateLimit": True,
            }
        )
        await self._exchange.load_markets()
        self._connected = True
        logger.info("[KuCoin] Connected via REST. Markets loaded.")

    async def disconnect(self) -> None:
        if self._ws_task:
            self._ws_task.cancel()
        if self._exchange:
            await self._exchange.close()
        self._connected = False
        logger.info("[KuCoin] Disconnected.")

    async def fetch_order_book(self, symbol: str, depth: int = 5) -> OrderBook:
        raw = await self._exchange.fetch_order_book(symbol, depth)
        ob = OrderBook(
            symbol=symbol,
            exchange=self.name,
            bids=[(float(p), float(q)) for p, q in raw["bids"][:depth]],
            asks=[(float(p), float(q)) for p, q in raw["asks"][:depth]],
        )
        self.order_books[symbol] = ob
        return ob

    async def fetch_balances(self) -> Dict[str, Balance]:
        raw = await self._exchange.fetch_balance()
        self.balances = {
            currency: Balance(
                currency=currency,
                free=float(data["free"] or 0),
                locked=float(data["used"] or 0),
            )
            for currency, data in raw.items()
            if isinstance(data, dict) and "free" in data and float(data.get("free") or 0) > 0
        }
        return self.balances

    async def create_market_order(self, symbol: str, side: str, amount: float) -> Order:
        raw = await self._exchange.create_order(symbol, "market", side, amount)
        return self._parse_order(raw)

    async def create_limit_order(
        self, symbol: str, side: str, amount: float, price: float
    ) -> Order:
        raw = await self._exchange.create_order(symbol, "limit", side, amount, price)
        return self._parse_order(raw)

    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        try:
            await self._exchange.cancel_order(order_id, symbol)
            return True
        except Exception as e:
            logger.error(f"[KuCoin] Cancel order {order_id} failed: {e}")
            return False

    async def fetch_order(self, order_id: str, symbol: str) -> Order:
        raw = await self._exchange.fetch_order(order_id, symbol)
        return self._parse_order(raw)

    async def subscribe_order_books(self, symbols: List[str]) -> None:
        self._subscribed_symbols = symbols
        await self._get_ws_token()
        if self._ws_url:
            self._ws_task = asyncio.create_task(self._ws_loop(symbols))
            logger.info(f"[KuCoin] WebSocket subscription started for {len(symbols)} symbols.")
        else:
            logger.warning("[KuCoin] Could not get WebSocket token — falling back to REST polling.")
            self._ws_task = asyncio.create_task(self._rest_poll_loop(symbols))

    async def _get_ws_token(self) -> None:
        try:
            token_data = await self._exchange.publicGetBulletPublic()
            self._ws_token = token_data["data"]["token"]
            endpoint = token_data["data"]["instanceServers"][0]["endpoint"]
            self._ws_url = f"{endpoint}?token={self._ws_token}"
        except Exception as e:
            logger.error(f"[KuCoin] Failed to get WS token: {e}")

    async def _ws_loop(self, symbols: List[str]) -> None:
        import websockets
        import random
        import string

        topic_syms = ",".join(s.replace("/", "-") for s in symbols)
        sub_id = "".join(random.choices(string.digits, k=10))

        while True:
            try:
                async with websockets.connect(
                    self._ws_url,
                    ping_interval=20,
                    ping_timeout=10,
                ) as ws:
                    # Subscribe to level1 (best bid/ask) for all symbols
                    await ws.send(json.dumps({
                        "id": sub_id,
                        "type": "subscribe",
                        "topic": f"/market/ticker:{topic_syms}",
                        "privateChannel": False,
                        "response": True,
                    }))
                    logger.info("[KuCoin] WebSocket subscribed.")
                    async for raw in ws:
                        self._handle_ticker(json.loads(raw))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[KuCoin] WebSocket error: {e}. Reconnecting in {self._reconnect_delay}s…")
                await self._get_ws_token()  # refresh token on reconnect
                await asyncio.sleep(self._reconnect_delay)

    def _handle_ticker(self, msg: dict) -> None:
        if msg.get("type") != "message":
            return
        data = msg.get("data", {})
        raw_symbol = msg.get("subject", "")  # e.g. "BTC-USDT"
        symbol = raw_symbol.replace("-", "/")
        best_bid = float(data.get("bestBid", 0))
        best_bid_size = float(data.get("bestBidSize", 0))
        best_ask = float(data.get("bestAsk", 0))
        best_ask_size = float(data.get("bestAskSize", 0))

        if best_bid and best_ask:
            self.order_books[symbol] = OrderBook(
                symbol=symbol,
                exchange=self.name,
                bids=[(best_bid, best_bid_size)],
                asks=[(best_ask, best_ask_size)],
            )

    async def _rest_poll_loop(self, symbols: List[str]) -> None:
        """Fallback: poll REST every 500ms when WebSocket is unavailable."""
        while True:
            for symbol in symbols:
                try:
                    await self.fetch_order_book(symbol, depth=1)
                except Exception as e:
                    logger.debug(f"[KuCoin] REST poll error for {symbol}: {e}")
            await asyncio.sleep(0.5)

    def _parse_order(self, raw: dict) -> Order:
        return Order(
            id=str(raw.get("id", "")),
            symbol=raw.get("symbol", ""),
            side=raw.get("side", ""),
            type=raw.get("type", ""),
            amount=float(raw.get("amount") or 0),
            price=float(raw["price"]) if raw.get("price") else None,
            status=raw.get("status", "open"),
            filled=float(raw.get("filled") or 0),
            cost=float(raw.get("cost") or 0),
            fee=float(raw.get("fee", {}).get("cost") or 0) if raw.get("fee") else 0.0,
        )
