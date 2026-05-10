from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
import time


@dataclass
class OrderBook:
    symbol: str
    exchange: str
    bids: List[Tuple[float, float]]  # [(price, qty), ...]
    asks: List[Tuple[float, float]]
    timestamp: float = field(default_factory=time.time)

    @property
    def best_bid(self) -> Optional[Tuple[float, float]]:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> Optional[Tuple[float, float]]:
        return self.asks[0] if self.asks else None

    @property
    def mid_price(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return (self.best_bid[0] + self.best_ask[0]) / 2
        return None

    @property
    def spread_pct(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return ((self.best_ask[0] - self.best_bid[0]) / self.best_ask[0]) * 100
        return None

    def age_ms(self) -> float:
        return (time.time() - self.timestamp) * 1000


@dataclass
class Balance:
    currency: str
    free: float
    locked: float

    @property
    def total(self) -> float:
        return self.free + self.locked


@dataclass
class Order:
    id: str
    symbol: str
    side: str          # "buy" | "sell"
    type: str          # "limit" | "market"
    amount: float
    price: Optional[float]
    status: str        # "open" | "closed" | "canceled"
    filled: float = 0.0
    cost: float = 0.0
    fee: float = 0.0
    timestamp: float = field(default_factory=time.time)


class BaseExchange(ABC):
    def __init__(self, config: dict):
        self.config = config
        self.name: str = ""
        self.order_books: Dict[str, OrderBook] = {}
        self.balances: Dict[str, Balance] = {}
        self._connected: bool = False

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def fetch_order_book(self, symbol: str, depth: int = 5) -> OrderBook: ...

    @abstractmethod
    async def fetch_balances(self) -> Dict[str, Balance]: ...

    @abstractmethod
    async def create_market_order(self, symbol: str, side: str, amount: float) -> Order: ...

    @abstractmethod
    async def create_limit_order(
        self, symbol: str, side: str, amount: float, price: float
    ) -> Order: ...

    @abstractmethod
    async def cancel_order(self, order_id: str, symbol: str) -> bool: ...

    @abstractmethod
    async def fetch_order(self, order_id: str, symbol: str) -> Order: ...

    @abstractmethod
    async def subscribe_order_books(self, symbols: List[str]) -> None: ...

    def get_order_book(self, symbol: str) -> Optional[OrderBook]:
        return self.order_books.get(symbol)

    def get_balance(self, currency: str) -> float:
        bal = self.balances.get(currency)
        return bal.free if bal else 0.0

    @property
    def is_connected(self) -> bool:
        return self._connected
