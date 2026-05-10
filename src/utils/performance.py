import csv
import os
import time
from dataclasses import dataclass, asdict
from typing import List
from pathlib import Path
from loguru import logger


@dataclass
class TradeRecord:
    timestamp: float
    strategy: str
    symbol: str
    exchange_buy: str
    exchange_sell: str
    amount_usdt: float
    profit_usdt: float
    profit_pct: float
    mode: str   # "paper" | "live"


class PerformanceTracker:
    def __init__(self, config: dict):
        self.cfg = config
        self.trades: List[TradeRecord] = []
        self.csv_path = config.get("trades_file", "logs/trades.csv")
        Path("logs").mkdir(exist_ok=True)
        self._init_csv()

    def _init_csv(self) -> None:
        if not os.path.exists(self.csv_path):
            with open(self.csv_path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=TradeRecord.__dataclass_fields__)
                writer.writeheader()

    def record(self, trade: TradeRecord) -> None:
        self.trades.append(trade)
        if self.cfg.get("save_trades_csv", True):
            with open(self.csv_path, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=TradeRecord.__dataclass_fields__)
                writer.writerow(asdict(trade))

    def report(self) -> str:
        if not self.trades:
            return "No trades recorded yet."

        total_profit = sum(t.profit_usdt for t in self.trades)
        wins = sum(1 for t in self.trades if t.profit_usdt > 0)
        losses = len(self.trades) - wins
        win_rate = (wins / len(self.trades)) * 100 if self.trades else 0

        by_strategy: dict = {}
        for t in self.trades:
            by_strategy.setdefault(t.strategy, {"profit": 0, "count": 0})
            by_strategy[t.strategy]["profit"] += t.profit_usdt
            by_strategy[t.strategy]["count"] += 1

        lines = [
            "=" * 50,
            f"  Performance Report — {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "=" * 50,
            f"  Total trades : {len(self.trades)}",
            f"  Wins / Losses: {wins} / {losses}",
            f"  Win rate     : {win_rate:.1f}%",
            f"  Total profit : {total_profit:.4f} USDT",
            "",
            "  By strategy:",
        ]
        for strat, data in by_strategy.items():
            lines.append(f"    {strat}: {data['count']} trades, {data['profit']:.4f} USDT")
        lines.append("=" * 50)
        return "\n".join(lines)
