"""Cycle math and opportunity detection.

For a triangle that starts and ends in `start`, we walk the legs converting
1 unit of the start asset through each pair, paying `fee` on every leg, and
report the net multiplier minus 1 (the edge). We evaluate both directions
(legs in order, and reversed) and keep the better one.

Conversion at each leg, given we currently hold `asset`:
  - asset is the pair's BASE  -> we SELL base for quote at the best BID
  - asset is the pair's QUOTE -> we BUY base with quote at the best ASK
Each conversion also pays the taker fee, i.e. multiply by (1 - fee).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .config import Config, Pair, Triangle


@dataclass
class Bbo:
    bid: float = 0.0
    ask: float = 0.0
    bid_qty: float = 0.0
    ask_qty: float = 0.0
    ts: float = 0.0

    def ok(self) -> bool:
        return self.bid > 0 and self.ask > 0 and self.ask >= self.bid

    def age(self, now: float | None = None) -> float:
        return (now if now is not None else time.time()) - self.ts


class BookStore:
    def __init__(self) -> None:
        self._books: dict[str, Bbo] = {}

    def update(self, symbol: str, bid: float, ask: float,
               bid_qty: float = 0.0, ask_qty: float = 0.0,
               ts: float | None = None) -> None:
        self._books[symbol] = Bbo(bid, ask, bid_qty, ask_qty,
                                  ts if ts is not None else time.time())

    def get(self, symbol: str) -> Bbo | None:
        return self._books.get(symbol)

    def all(self) -> dict[str, Bbo]:
        return self._books


@dataclass
class Leg:
    symbol: str
    action: str      # "buy" (spend quote, receive base) or "sell"
    price: float     # ask for buy, bid for sell


@dataclass
class Opportunity:
    triangle: str
    direction: str            # "fwd" | "rev"
    net_edge: float           # fraction, after fees; >0 is profit
    gross_edge: float         # before fees
    start: str = "USDT"       # asset the cycle begins and ends in
    legs: list[Leg] = field(default_factory=list)
    limiting_qty_usdt: float = 0.0   # top-of-book depth bound on the start leg


def _walk(start: str, leg_syms: list[str], pairs: dict[str, Pair],
          books: BookStore, fee: float) -> tuple[float, list[Leg]] | None:
    amt = 1.0
    asset = start
    legs: list[Leg] = []
    for sym in leg_syms:
        p = pairs.get(sym)
        b = books.get(sym)
        if p is None or b is None or not b.ok():
            return None
        if asset == p.base:
            amt = amt * b.bid * (1.0 - fee)   # sell base -> quote
            legs.append(Leg(sym, "sell", b.bid))
            asset = p.quote
        elif asset == p.quote:
            amt = amt / b.ask * (1.0 - fee)    # buy base <- quote
            legs.append(Leg(sym, "buy", b.ask))
            asset = p.base
        else:
            return None                         # leg doesn't connect
    if asset != start:
        return None
    return amt, legs


def _gross(start: str, leg_syms: list[str], pairs: dict[str, Pair],
           books: BookStore) -> float:
    res = _walk(start, leg_syms, pairs, books, 0.0)
    return res[0] if res else 0.0


def evaluate(triangle: Triangle, pairs: dict[str, Pair], books: BookStore,
             fee: float, now: float | None = None,
             max_age: float = 1.0) -> Opportunity | None:
    """Best of the two directions for one triangle, or None if unpriceable
    or any leg is stale."""
    now = now if now is not None else time.time()
    for sym in triangle.legs:
        b = books.get(sym)
        if b is None or not b.ok() or b.age(now) > max_age:
            return None

    best: Opportunity | None = None
    for direction, syms in (("fwd", triangle.legs),
                            ("rev", list(reversed(triangle.legs)))):
        res = _walk(triangle.start, syms, pairs, books, fee)
        if res is None:
            continue
        mult, legs = res
        net = mult - 1.0
        gross = _gross(triangle.start, syms, pairs, books) - 1.0
        if best is None or net > best.net_edge:
            first = books.get(legs[0].symbol)
            depth = 0.0
            if first:
                depth = (first.ask * first.ask_qty if legs[0].action == "buy"
                         else first.bid * first.bid_qty)
            best = Opportunity(triangle.name, direction, net, gross,
                               triangle.start, legs, depth)
    return best


def scan(cfg: Config, books: BookStore,
         now: float | None = None) -> list[Opportunity]:
    """All triangles whose best direction clears the net-edge threshold,
    richest first."""
    out: list[Opportunity] = []
    for tri in cfg.triangles:
        opp = evaluate(tri, cfg.pairs, books, cfg.fee_rate, now,
                       cfg.max_book_age_s)
        if opp and opp.net_edge >= cfg.min_net_edge:
            out.append(opp)
    out.sort(key=lambda o: o.net_edge, reverse=True)
    return out
