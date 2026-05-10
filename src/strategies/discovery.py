"""
Triangle auto-discovery.

Scans all active Binance spot markets at startup and builds every valid
USDT triangle: USDT → MID → QUOTE → USDT

A triangle (USDT, MID, QUOTE) is valid when all three pairs exist and
are actively traded:
  MID/USDT   — buy MID with USDT
  QUOTE/MID  — buy QUOTE with MID
  QUOTE/USDT — sell QUOTE back to USDT

Pairs are ranked by the minimum 24h USDT volume across the triangle so
the hottest, tightest-spread opportunities bubble to the top of the
scan order.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Tuple

from loguru import logger

if TYPE_CHECKING:
    from src.exchanges.binance import BinanceExchange


@dataclass(frozen=True)
class TriangleMeta:
    base: str           # always USDT
    mid: str            # bridge currency  (BTC / ETH / BNB / …)
    quote: str          # target asset     (ETH / SOL / XRP / …)
    sym_mid_base: str   # e.g. BTC/USDT
    sym_quote_mid: str  # e.g. ETH/BTC
    sym_quote_base: str # e.g. ETH/USDT
    min_volume_usdt: float  # min 24h vol across the two USDT legs

    @property
    def path(self) -> str:
        return f"USDT→{self.mid}→{self.quote}→USDT"

    def symbols(self) -> List[str]:
        return [self.sym_mid_base, self.sym_quote_mid, self.sym_quote_base]


async def discover_triangles(
    exchange: "BinanceExchange",
    min_volume_usdt: float = 500_000,
    max_triangles: int = 300,
    bridge_currencies: Optional[List[str]] = None,
) -> List[TriangleMeta]:
    """
    Fetch live 24h tickers and return all valid triangles sorted by liquidity.

    Args:
        min_volume_usdt: Minimum 24h volume (USDT) for each USDT leg.
                         Lower = more triangles but thinner spreads.
                         $500k is a reasonable floor; lower to $100k to cast
                         a wider net on quieter markets.
        max_triangles:   Cap on returned triangles (keep WebSocket load sane).
        bridge_currencies: Which currencies to use as the MID leg.
                           Defaults to [BTC, ETH, BNB] — the three with the
                           most cross-pair coverage on Binance.
    """
    if bridge_currencies is None:
        bridge_currencies = ["BTC", "ETH", "BNB"]

    logger.info(
        f"[Discovery] Scanning Binance markets for triangles "
        f"(min volume ${min_volume_usdt:,.0f}, bridges={bridge_currencies})…"
    )

    markets = exchange._exchange.markets
    if not markets:
        raise RuntimeError("[Discovery] Markets not loaded — call exchange.connect() first.")

    # ── Step 1: fetch 24h tickers for volume data ─────────────────────────
    try:
        tickers: Dict = await asyncio.wait_for(
            exchange._exchange.fetch_tickers(), timeout=30
        )
    except asyncio.TimeoutError:
        logger.error("[Discovery] Ticker fetch timed out — falling back to zero-volume filter.")
        tickers = {}

    def usdt_volume(symbol: str) -> float:
        t = tickers.get(symbol, {})
        return float(t.get("quoteVolume") or t.get("baseVolume") or 0)

    # ── Step 2: all active USDT pairs above the volume floor ──────────────
    liquid_assets: Dict[str, float] = {}   # asset → 24h USDT volume
    for sym, mkt in markets.items():
        if not mkt.get("active", False):
            continue
        base = mkt.get("base", "")
        quote = mkt.get("quote", "")
        if quote == "USDT" and base:
            vol = usdt_volume(sym)
            if vol >= min_volume_usdt:
                liquid_assets[base] = vol

    logger.info(
        f"[Discovery] Found {len(liquid_assets)} liquid USDT assets "
        f"(≥${min_volume_usdt:,.0f} volume)."
    )

    # ── Step 3: build all valid triangles ─────────────────────────────────
    triangles: List[TriangleMeta] = []
    seen: Set[Tuple[str, str]] = set()   # (mid, quote) dedup

    # Sort liquid assets by volume descending so high-volume pairs scan first
    sorted_assets = sorted(liquid_assets, key=liquid_assets.get, reverse=True)  # type: ignore[arg-type]

    for mid in bridge_currencies:
        if mid not in liquid_assets:
            logger.warning(f"[Discovery] Bridge currency {mid} has no liquid USDT pair — skipping.")
            continue

        for quote in sorted_assets:
            if quote == mid or quote == "USDT":
                continue
            if (mid, quote) in seen:
                continue

            sym_mid_base = f"{mid}/USDT"
            sym_quote_mid = f"{quote}/{mid}"
            sym_quote_base = f"{quote}/USDT"

            # All three pairs must exist and be active
            if not all(
                markets.get(s, {}).get("active", False)
                for s in [sym_mid_base, sym_quote_mid, sym_quote_base]
            ):
                continue

            # Volume floor: both USDT legs must be liquid
            # (bridge pair liquidity is implied by the USDT legs)
            vol_mid_base = liquid_assets.get(mid, 0)
            vol_quote_base = liquid_assets.get(quote, 0)
            min_vol = min(vol_mid_base, vol_quote_base)

            seen.add((mid, quote))
            triangles.append(
                TriangleMeta(
                    base="USDT",
                    mid=mid,
                    quote=quote,
                    sym_mid_base=sym_mid_base,
                    sym_quote_mid=sym_quote_mid,
                    sym_quote_base=sym_quote_base,
                    min_volume_usdt=min_vol,
                )
            )

    # Sort by liquidity: highest min-volume triangle first (tightest spreads)
    triangles.sort(key=lambda t: t.min_volume_usdt, reverse=True)
    triangles = triangles[:max_triangles]

    # Summarise by bridge
    by_bridge: Dict[str, int] = {}
    for t in triangles:
        by_bridge[t.mid] = by_bridge.get(t.mid, 0) + 1

    logger.success(
        f"[Discovery] {len(triangles)} triangles ready: "
        + ", ".join(f"{k}={v}" for k, v in sorted(by_bridge.items()))
    )

    return triangles


def triangles_to_symbols(triangles: List[TriangleMeta]) -> List[str]:
    """Deduplicated symbol list for WebSocket subscription."""
    seen: Set[str] = set()
    result: List[str] = []
    for t in triangles:
        for sym in t.symbols():
            if sym not in seen:
                seen.add(sym)
                result.append(sym)
    return result
