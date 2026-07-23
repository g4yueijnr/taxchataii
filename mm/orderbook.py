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

    @staticmethod
    def _price_cents(p) -> int:
        """Accept legacy integer cents (42) and current dollar strings
        ('0.4200', sub-penny possible) — Kalshi migrated formats."""
        if isinstance(p, str) and "." in p:
            return int(round(float(p) * 100))
        if isinstance(p, float) and p <= 1.0:
            return int(round(p * 100))
        return int(p)

    @classmethod
    def _parse_levels(cls, raw) -> dict[int, int]:
        out: dict[int, int] = {}
        for lvl in raw or []:
            price = cls._price_cents(lvl[0])
            size = int(float(lvl[1]))
            if 1 <= price <= 99 and size > 0:
                out[price] = size
        return out

    def apply_snapshot(self, msg: dict) -> None:
        # Formats seen in the wild: {'yes':[[42,10]]}, {'yes_dollars':
        # [['0.4200','10.00']]}, and either nested under 'orderbook' /
        # 'orderbook_fp' (REST) or flat (websocket).
        src = msg.get("orderbook_fp") or msg.get("orderbook") or msg
        yes = src.get("yes") if src.get("yes") is not None else src.get("yes_dollars")
        no = src.get("no") if src.get("no") is not None else src.get("no_dollars")
        self.yes = self._parse_levels(yes)
        self.no = self._parse_levels(no)
        self.last_update = time.time()

    def apply_delta(self, msg: dict) -> None:
        side = msg.get("side")
        price_raw = msg.get("price")
        if price_raw is None:
            price_raw = msg.get("price_dollars", msg.get("price_fp", 0))
        delta_raw = msg.get("delta")
        if delta_raw is None:
            delta_raw = msg.get("delta_fp", msg.get("delta_dollars", 0))
        try:
            price = self._price_cents(price_raw)
            delta = int(float(delta_raw))
        except (TypeError, ValueError):
            return
        if not (1 <= price <= 99):
            return
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
