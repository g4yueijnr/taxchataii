"""Local Kalshi orderbook, maintained from websocket snapshot + deltas.

Kalshi books are two ladders of resting BUY orders: 'yes' bids and 'no' bids.
The YES ask is implied: best_yes_ask = 100 - best_no_bid.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class Book:
    ticker: str
    yes: dict[int, int] = field(default_factory=dict)  # price -> size
    no: dict[int, int] = field(default_factory=dict)
    last_update: float = 0.0
    last_trade_price: int = 0   # yes price of last public trade
    last_trade_ts: float = 0.0

    def apply_snapshot(self, msg: dict) -> None:
        self.yes = {int(p): int(s) for p, s in (msg.get("yes") or [])}
        self.no = {int(p): int(s) for p, s in (msg.get("no") or [])}
        self.last_update = time.time()

    def apply_delta(self, msg: dict) -> None:
        side = msg.get("side")
        price = int(msg.get("price", 0))
        delta = int(msg.get("delta", 0))
        ladder = self.yes if side == "yes" else self.no
        size = ladder.get(price, 0) + delta
        if size > 0:
            ladder[price] = size
        else:
            ladder.pop(price, None)
        self.last_update = time.time()

    # ------------------------------------------------------------- queries

    @property
    def best_yes_bid(self) -> int:
        return max(self.yes) if self.yes else 0

    @property
    def best_no_bid(self) -> int:
        return max(self.no) if self.no else 0

    @property
    def best_yes_ask(self) -> int:
        """100 - best NO bid; 100 means no offers."""
        return 100 - self.best_no_bid if self.no else 100

    @property
    def spread(self) -> int:
        if not self.yes or not self.no:
            return 100
        return self.best_yes_ask - self.best_yes_bid

    @property
    def mid(self) -> float:
        if not self.yes or not self.no:
            return 50.0
        return (self.best_yes_ask + self.best_yes_bid) / 2.0

    def depth_at(self, side: str, price: int) -> int:
        return (self.yes if side == "yes" else self.no).get(price, 0)
