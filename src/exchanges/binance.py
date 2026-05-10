import asyncio
import json
from typing import Dict, List, Optional
import ccxt.async_support as ccxt
import websockets
from loguru import logger

from .base import BaseExchange, OrderBook, Balance, Order


class BinanceExchange(BaseExchange):
    """
    Binance spot connector.
    Designed for Tokyo VPS (co-located with Binance's AWS ap-northeast-1 cluster).
    Uses WebSocket bookTicker for sub-millisecond order-book updates;
    REST is used only for order placement and account queries.
    """

    WS_BASE = "wss://stream.binance.com:9443/stream"

    def __init__(self, config: dict):
        super().__init__(config)
        self.name = "binance"
        self._exchange: Optional[ccxt.binance] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._reconnect_delay = config.get("websocket_reconnect_delay", 1)

    async def connect(self) -> None:
        import os
        self._exchange = ccxt.binance(
            {
                "apiKey": os.getenv("BINANCE_API_KEY", ""),
                "secret": os.getenv("BINANCE_API_SECRET", ""),
                "enableRateLimit": True,
                "options": {
                    "recvWindow": self.config.get("recv_window", 5000),
                    "defaultType": "spot",
                },
            }
        )
        await self._exchange.load_markets()
        self._connected = True
        logger.info("[Binance] Connected. Markets loaded.")

    async def disconnect(self) -> None:
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
        if self._exchange:
            await self._exchange.close()
        self._connected = False
        logger.info("[Binance] Disconnected.")

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
            logger.error(f"[Binance] Cancel {order_id} failed: {e}")
            return False

    async def fetch_order(self, order_id: str, symbol: str) -> Order:
        raw = await self._exchange.fetch_order(order_id, symbol)
        return self._parse_order(raw)

    async def subscribe_order_books(self, symbols: List[str]) -> None:
        self._ws_task = asyncio.create_task(self._ws_loop(symbols))
        logger.info(f"[Binance] WebSocket subscribing to {len(symbols)} symbols.")

    async def _ws_loop(self, symbols: List[str]) -> None:
        streams = "/".join(
            f"{s.replace('/', '').lower()}@bookTicker" for s in symbols
        )
        url = f"{self.WS_BASE}?streams={streams}"
        while True:
            try:
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    logger.info("[Binance] WebSocket connected.")
                    async for raw in ws:
                        self._handle_book_ticker(json.loads(raw))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(
                    f"[Binance] WebSocket error: {e}. "
                    f"Reconnecting in {self._reconnect_delay}s…"
                )
                await asyncio.sleep(self._reconnect_delay)

    def _handle_book_ticker(self, msg: dict) -> None:
        data = msg.get("data", msg)
        raw_symbol = data.get("s", "")
        symbol = self._normalise_symbol(raw_symbol)
        if not symbol:
            return
        bid_price = float(data.get("b", 0))
        bid_qty = float(data.get("B", 0))
        ask_price = float(data.get("a", 0))
        ask_qty = float(data.get("A", 0))
        if bid_price and ask_price:
            self.order_books[symbol] = OrderBook(
                symbol=symbol,
                exchange=self.name,
                bids=[(bid_price, bid_qty)],
                asks=[(ask_price, ask_qty)],
            )

    def _normalise_symbol(self, raw: str) -> Optional[str]:
        if self._exchange and self._exchange.markets:
            for sym, mkt in self._exchange.markets.items():
                if mkt.get("id", "").upper() == raw.upper():
                    return sym
        for q in ["USDT", "BTC", "ETH", "BNB", "BUSD"]:
            if raw.endswith(q):
                return f"{raw[:-len(q)]}/{q}"
        return None

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
            fee=float(raw["fee"]["cost"]) if raw.get("fee") else 0.0,
        )
