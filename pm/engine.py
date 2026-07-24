"""Order book, maker quoting, positions, and paper fills.

Prices are handled in whole cents (1-99); Polymarket ticks are 1c. A market
is one CLOB token (the YES outcome). We quote a bid and an ask around the
book, capture the spread, and — the whole point of moving here — CREDIT the
maker rebate on every fill instead of paying a fee.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field


def d2c(price) -> int:
    """Dollars (0-1, int/float/str) -> whole cents (0-100)."""
    return int(round(float(price) * 100))


def c2d(cents: int) -> float:
    return cents / 100.0


@dataclass
class Book:
    token_id: str
    bids: dict[int, float] = field(default_factory=dict)   # cents -> size
    asks: dict[int, float] = field(default_factory=dict)
    last_update: float = 0.0
    last_trade: int = 0

    def apply_snapshot(self, bids, asks) -> None:
        self.bids = {d2c(float(p)): float(s) for p, s in bids if 0 < d2c(float(p)) < 100}
        self.asks = {d2c(float(p)): float(s) for p, s in asks if 0 < d2c(float(p)) < 100}
        self.last_update = time.time()

    def set_level(self, side: str, price_cents: int, size: float) -> None:
        ladder = self.bids if side == "bid" else self.asks
        if size > 0:
            ladder[price_cents] = size
        else:
            ladder.pop(price_cents, None)
        self.last_update = time.time()

    @property
    def best_bid(self) -> int:
        return max(self.bids) if self.bids else 0

    @property
    def best_ask(self) -> int:
        return min(self.asks) if self.asks else 100

    @property
    def spread(self) -> int:
        if not self.bids or not self.asks:
            return 100
        return self.best_ask - self.best_bid

    @property
    def mid(self) -> float:
        if not self.bids or not self.asks:
            return 50.0
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def crossed(self) -> bool:
        return bool(self.bids and self.asks and self.best_bid >= self.best_ask)


def maker_rebate_cents(price_cents: int, size: float, mult: float) -> float:
    """Rebate CREDITED to the maker, in cents, per fill."""
    p = price_cents / 100.0
    return mult * p * (1.0 - p) * 100.0 * size


@dataclass
class Position:
    token_id: str
    shares: float = 0.0        # + long YES
    avg_cost: float = 0.0      # cents
    realized: float = 0.0      # cents (incl. rebates)
    rebates: float = 0.0       # cents credited


@dataclass
class DesiredQuote:
    bid: int          # cents
    ask: int
    bid_size: float
    ask_size: float


class MakerStrategy:
    def __init__(self, cfg):
        self.cfg = cfg

    def compute(self, book: Book, position: float) -> DesiredQuote | None:
        cfg = self.cfg
        if book.crossed or not book.bids or not book.asks:
            return None
        if book.spread < cfg.min_book_spread_ticks:
            return None
        mid = book.mid
        if mid < 3 or mid > 97:
            return None

        bid = book.best_bid + (1 if cfg.join_inside else 0)
        ask = book.best_ask - (1 if cfg.join_inside else 0)
        if ask - bid < 1:
            bid, ask = book.best_bid, book.best_ask

        skew = int(round(cfg.inventory_skew_ticks * position
                         / max(cfg.max_position, 1)))
        bid -= skew
        ask -= skew
        bid = max(1, min(bid, book.best_ask - 1))
        ask = min(99, max(ask, book.best_bid + 1))
        if ask - bid < 1:
            return None

        bid_size = cfg.quote_size if position < cfg.max_position else 0.0
        ask_size = cfg.quote_size if position > -cfg.max_position else 0.0
        return DesiredQuote(bid, ask, bid_size, ask_size)


class PaperBook:
    """Paper positions + fills for one bot (all markets)."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.positions: dict[str, Position] = {}
        self.realized_cents = 0.0
        self.rebates_cents = 0.0
        self.fills = 0
        self.on_fill = None   # callback(token, side, price, size, realized, rebate)

    def pos(self, token: str) -> Position:
        if token not in self.positions:
            self.positions[token] = Position(token)
        return self.positions[token]

    def fill(self, token: str, side: str, price_cents: int, size: float) -> None:
        """side 'buy' -> long more YES at price; 'sell' -> reduce/short."""
        p = self.pos(token)
        qty = size if side == "buy" else -size
        rebate = maker_rebate_cents(price_cents, size, self.cfg.maker_rebate_mult)
        p.rebates += rebate
        self.rebates_cents += rebate
        self.realized_cents += rebate
        self.fills += 1

        if p.shares == 0 or (p.shares > 0) == (qty > 0):
            total = p.shares + qty
            if total != 0:
                p.avg_cost = (p.avg_cost * abs(p.shares)
                              + price_cents * abs(qty)) / abs(total)
            p.shares = total
        else:
            closed = min(abs(qty), abs(p.shares))
            pnl = (price_cents - p.avg_cost) * closed if p.shares > 0 \
                else (p.avg_cost - price_cents) * closed
            self.realized_cents += pnl
            p.realized += pnl
            p.shares += qty
            if p.shares == 0:
                p.avg_cost = 0.0
            elif (p.shares > 0) != (qty < 0):
                p.avg_cost = price_cents
        if self.on_fill:
            self.on_fill(token, side, price_cents, size,
                         p.realized, rebate)

    def gross_dollars(self) -> float:
        tot = 0.0
        for p in self.positions.values():
            if p.shares > 0:
                tot += p.avg_cost * p.shares / 100.0
            elif p.shares < 0:
                tot += (100 - p.avg_cost) * -p.shares / 100.0
        return tot

    @property
    def net_pnl_cents(self) -> float:
        return self.realized_cents
