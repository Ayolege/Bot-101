"""
Tests for triangle auto-discovery.
Uses a mock exchange with a small set of markets to verify
discovery logic without hitting real Binance APIs.
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from src.strategies.discovery import (
    discover_triangles,
    triangles_to_symbols,
    TriangleMeta,
)


def make_market(base: str, quote: str, active: bool = True) -> dict:
    return {"base": base, "quote": quote, "active": active, "id": f"{base}{quote}"}


def make_mock_exchange(markets: dict, tickers: dict):
    ex = MagicMock()
    ex._exchange = MagicMock()
    ex._exchange.markets = markets
    ex._exchange.fetch_tickers = AsyncMock(return_value=tickers)
    return ex


def build_test_markets():
    """
    Small but realistic market set:
      USDT pairs: BTC, ETH, BNB, XRP, ADA, SOL
      BTC  pairs: ETH/BTC, BNB/BTC, XRP/BTC, ADA/BTC, SOL/BTC
      ETH  pairs: BNB/ETH, XRP/ETH
      BNB  pairs: XRP/BNB, ADA/BNB
    """
    markets = {}
    # USDT base pairs
    for asset in ["BTC", "ETH", "BNB", "XRP", "ADA", "SOL"]:
        sym = f"{asset}/USDT"
        markets[sym] = make_market(asset, "USDT")
    # BTC bridge
    for asset in ["ETH", "BNB", "XRP", "ADA", "SOL"]:
        sym = f"{asset}/BTC"
        markets[sym] = make_market(asset, "BTC")
    # ETH bridge
    for asset in ["BNB", "XRP"]:
        sym = f"{asset}/ETH"
        markets[sym] = make_market(asset, "ETH")
    # BNB bridge
    for asset in ["XRP", "ADA"]:
        sym = f"{asset}/BNB"
        markets[sym] = make_market(asset, "BNB")
    return markets


def build_test_tickers(volume: float = 1_000_000):
    """All pairs get the same volume for simplicity."""
    tickers = {}
    for asset in ["BTC", "ETH", "BNB", "XRP", "ADA", "SOL"]:
        tickers[f"{asset}/USDT"] = {"quoteVolume": volume}
    for pair in ["ETH/BTC", "BNB/BTC", "XRP/BTC", "ADA/BTC", "SOL/BTC",
                 "BNB/ETH", "XRP/ETH", "XRP/BNB", "ADA/BNB"]:
        tickers[pair] = {"quoteVolume": volume}
    return tickers


class TestDiscoverTriangles:

    def test_discovers_btc_triangles(self):
        markets = build_test_markets()
        tickers = build_test_tickers(volume=2_000_000)
        ex = make_mock_exchange(markets, tickers)
        triangles = asyncio.run(
            discover_triangles(ex, min_volume_usdt=500_000, bridge_currencies=["BTC"])
        )
        mids = {t.mid for t in triangles}
        quotes = {t.quote for t in triangles}
        assert "BTC" in mids
        assert "ETH" in quotes or "SOL" in quotes

    def test_discovers_eth_triangles(self):
        markets = build_test_markets()
        tickers = build_test_tickers(volume=2_000_000)
        ex = make_mock_exchange(markets, tickers)
        triangles = asyncio.run(
            discover_triangles(ex, min_volume_usdt=500_000, bridge_currencies=["ETH"])
        )
        assert any(t.mid == "ETH" for t in triangles)

    def test_volume_filter_excludes_low_volume(self):
        markets = build_test_markets()
        # All pairs below the threshold
        tickers = build_test_tickers(volume=100_000)
        ex = make_mock_exchange(markets, tickers)
        triangles = asyncio.run(
            discover_triangles(ex, min_volume_usdt=500_000, bridge_currencies=["BTC"])
        )
        assert len(triangles) == 0

    def test_max_triangles_cap(self):
        markets = build_test_markets()
        tickers = build_test_tickers(volume=2_000_000)
        ex = make_mock_exchange(markets, tickers)
        triangles = asyncio.run(
            discover_triangles(ex, min_volume_usdt=100_000, max_triangles=3)
        )
        assert len(triangles) <= 3

    def test_sorted_by_volume_descending(self):
        markets = build_test_markets()
        # Give ETH/USDT higher volume to push ETH triangles to top
        tickers = build_test_tickers(volume=1_000_000)
        tickers["ETH/USDT"]["quoteVolume"] = 5_000_000
        tickers["BTC/USDT"]["quoteVolume"] = 1_000_000
        ex = make_mock_exchange(markets, tickers)
        triangles = asyncio.run(
            discover_triangles(ex, min_volume_usdt=500_000, bridge_currencies=["BTC", "ETH"])
        )
        if len(triangles) >= 2:
            assert triangles[0].min_volume_usdt >= triangles[-1].min_volume_usdt

    def test_no_duplicate_mid_quote_pairs(self):
        markets = build_test_markets()
        tickers = build_test_tickers(volume=2_000_000)
        ex = make_mock_exchange(markets, tickers)
        triangles = asyncio.run(
            discover_triangles(ex, min_volume_usdt=500_000)
        )
        seen = set()
        for t in triangles:
            key = (t.mid, t.quote)
            assert key not in seen, f"Duplicate triangle: {key}"
            seen.add(key)

    def test_skips_inactive_markets(self):
        markets = build_test_markets()
        # Mark ETH/BTC inactive
        markets["ETH/BTC"]["active"] = False
        tickers = build_test_tickers(volume=2_000_000)
        ex = make_mock_exchange(markets, tickers)
        triangles = asyncio.run(
            discover_triangles(ex, min_volume_usdt=500_000, bridge_currencies=["BTC"])
        )
        # USDT→BTC→ETH→USDT should not appear
        assert not any(t.mid == "BTC" and t.quote == "ETH" for t in triangles)

    def test_triangle_meta_symbols(self):
        t = TriangleMeta(
            base="USDT", mid="BTC", quote="ETH",
            sym_mid_base="BTC/USDT",
            sym_quote_mid="ETH/BTC",
            sym_quote_base="ETH/USDT",
            min_volume_usdt=1_000_000,
        )
        syms = t.symbols()
        assert "BTC/USDT" in syms
        assert "ETH/BTC" in syms
        assert "ETH/USDT" in syms

    def test_triangles_to_symbols_deduplicates(self):
        t1 = TriangleMeta("USDT", "BTC", "ETH", "BTC/USDT", "ETH/BTC", "ETH/USDT", 1e6)
        t2 = TriangleMeta("USDT", "BTC", "XRP", "BTC/USDT", "XRP/BTC", "XRP/USDT", 1e6)
        syms = triangles_to_symbols([t1, t2])
        # BTC/USDT should appear only once
        assert syms.count("BTC/USDT") == 1
        assert len(syms) == 5   # BTC/USDT, ETH/BTC, ETH/USDT, XRP/BTC, XRP/USDT

    def test_path_string(self):
        t = TriangleMeta("USDT", "BTC", "ETH", "BTC/USDT", "ETH/BTC", "ETH/USDT", 1e6)
        assert t.path == "USDT→BTC→ETH→USDT"
