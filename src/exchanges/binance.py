import asyncio
import json
import os
from typing import Dict, List, Optional
import ccxt.async_support as ccxt
import websockets
from loguru import logger

from .base import BaseExchange, OrderBook, Balance, Order


# Binance error codes that mean "account/region restricted" vs "retryable"
_RESTRICTION_CODES = {
    -2015,   # Invalid API key, IP, or permissions
    -2014,   # API key format invalid
    -1003,   # Too many requests
    -1130,   # Invalid symbol
    -3045,   # System does not support this operation
}

_REGION_ERRORS = {
    "nigeria",
    "restricted",
    "not available in your region",
    "compliance",
    "geographic",
}


class BinanceExchange(BaseExchange):
    """
    Binance spot connector, hardened for Nigerian accounts.

    Geo-restriction context:
      The bot runs on a Tokyo VPS — API requests originate from Japan,
      not Nigeria, so Binance's IP-based geo-fencing does not apply.
      However, accounts registered in Nigeria may have restrictions on:
        - Certain trading pairs (handled: skipped gracefully)
        - Fiat gateways (irrelevant — bot uses USDT only)
        - Withdrawal (irrelevant — bot never withdraws)
      The startup permission check verifies spot trading is enabled
      before the scan loop starts.

    Proxy:
      Set BINANCE_PROXY=socks5h://user:pass@host:port in .env
      if running locally in Nigeria rather than on a VPS.
    """

    WS_BASE = "wss://stream.binance.com:9443/stream"

    def __init__(self, config: dict):
        super().__init__(config)
        self.name = "binance"
        self._exchange: Optional[ccxt.binance] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._reconnect_delay = config.get("websocket_reconnect_delay", 1)
        self._verified_markets: set[str] = set()  # pairs confirmed tradeable on this account

    async def connect(self) -> None:
        proxy = os.getenv("BINANCE_PROXY", "")
        options: dict = {
            "apiKey": os.getenv("BINANCE_API_KEY", ""),
            "secret": os.getenv("BINANCE_API_SECRET", ""),
            "enableRateLimit": True,
            "options": {
                "recvWindow": self.config.get("recv_window", 5000),
                "defaultType": "spot",
            },
        }
        if proxy:
            options["proxies"] = {"http": proxy, "https": proxy}
            logger.info(f"[Binance] Using proxy: {proxy}")

        self._exchange = ccxt.binance(options)

        try:
            await self._exchange.load_markets()
        except ccxt.AuthenticationError as e:
            raise RuntimeError(
                f"[Binance] Authentication failed — check BINANCE_API_KEY and "
                f"BINANCE_API_SECRET in your .env file. Detail: {e}"
            )
        except ccxt.ExchangeNotAvailable as e:
            raise RuntimeError(f"[Binance] Exchange unavailable: {e}")

        self._connected = True
        logger.info("[Binance] Connected. Markets loaded.")
        await self._check_account_permissions()

    async def _check_account_permissions(self) -> None:
        """
        Verify the API key has spot trading enabled.
        Catches account-level restrictions (e.g. Nigerian accounts with
        limited permissions) before the scan loop starts — fail fast.
        """
        try:
            info = await self._exchange.fetch_account()
            permissions = info.get("info", {}).get("permissions", [])
            if permissions and "SPOT" not in permissions:
                raise RuntimeError(
                    f"[Binance] API key does not have SPOT trading permission. "
                    f"Permissions found: {permissions}. "
                    "Enable spot trading in Binance API management."
                )

            can_trade = info.get("info", {}).get("canTrade", True)
            if not can_trade:
                raise RuntimeError(
                    "[Binance] Account canTrade=False. Your account may be restricted. "
                    "Log in to Binance and check account status."
                )

            logger.info(f"[Binance] Account permissions verified. canTrade=True")
        except (ccxt.AuthenticationError, ccxt.PermissionDenied) as e:
            raise RuntimeError(f"[Binance] Permission check failed: {e}")
        except ccxt.NetworkError:
            logger.warning("[Binance] Could not verify account permissions (network error) — continuing.")
        except Exception as e:
            # Non-fatal — log and continue; live orders will fail explicitly if restricted
            logger.warning(f"[Binance] Permission check skipped: {e}")

    async def verify_symbols(self, symbols: List[str]) -> List[str]:
        """
        Return only symbols actually tradeable on this account in this region.
        Skips symbols that return restriction errors rather than crashing.
        """
        tradeable = []
        for sym in symbols:
            if sym in self._exchange.markets:
                market = self._exchange.markets[sym]
                # Check if market is active
                if market.get("active", True):
                    tradeable.append(sym)
                    self._verified_markets.add(sym)
                else:
                    logger.warning(f"[Binance] {sym} is inactive — skipping.")
            else:
                logger.warning(f"[Binance] {sym} not found in markets — skipping.")
        return tradeable

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
        try:
            raw = await self._exchange.create_order(symbol, "market", side, amount)
            return self._parse_order(raw)
        except ccxt.InsufficientFunds as e:
            raise RuntimeError(f"Insufficient funds for {side} {amount} {symbol}: {e}")
        except ccxt.InvalidOrder as e:
            raise RuntimeError(f"Invalid order ({side} {amount} {symbol}): {e}")
        except ccxt.PermissionDenied as e:
            self._handle_restriction_error(e, symbol)
            raise
        except ccxt.ExchangeError as e:
            self._classify_exchange_error(e, symbol)
            raise

    async def create_limit_order(
        self, symbol: str, side: str, amount: float, price: float
    ) -> Order:
        raw = await self._exchange.create_order(symbol, "limit", side, amount, price)
        return self._parse_order(raw)

    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        try:
            await self._exchange.cancel_order(order_id, symbol)
            return True
        except ccxt.OrderNotFound:
            logger.warning(f"[Binance] Order {order_id} not found (may already be filled).")
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

    def _handle_restriction_error(self, error: Exception, symbol: str) -> None:
        msg = str(error).lower()
        if any(kw in msg for kw in _REGION_ERRORS):
            logger.error(
                f"[Binance] Regional restriction on {symbol}. "
                "Since the bot runs on Tokyo VPS this should not happen via IP. "
                "Your Binance account may have a country-level restriction — "
                "check your account compliance status at binance.com/en/user/dashboard."
            )
        else:
            logger.error(f"[Binance] Permission denied for {symbol}: {error}")

    def _classify_exchange_error(self, error: ccxt.ExchangeError, symbol: str) -> None:
        msg = str(error)
        # Extract Binance error code if present
        code = None
        if hasattr(error, "args") and error.args:
            import re
            m = re.search(r"-?\d{4,}", str(error.args[0]))
            if m:
                code = int(m.group())

        if code in _RESTRICTION_CODES:
            logger.error(
                f"[Binance] Restriction error (code {code}) on {symbol}: {msg}. "
                "This pair may not be available for your account."
            )
        else:
            logger.error(f"[Binance] Exchange error on {symbol}: {msg}")

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
