"""Paper-trading order manager: same interface as OrderManager, fills
simulated from Kalshi's public trade tape and book state.

Fill model (deliberately pessimistic-ish):
- Our resting YES bid fills when a public trade prints with the taker
  selling YES at a price at or below our bid (symmetrically for NO).
  Trades at exactly our price fill us for at most the traded count — no
  assumption of queue priority.
- Cross exits fill immediately at the current touch if the book shows size.

Paper fills pay the same fee schedule as live ones.
"""

from __future__ import annotations

import itertools
import logging
import time

from .config import Config
from .execution import LiveOrder, OrderManager, PositionBook
from .orderbook import Book
from .strategy import CrossExit, DesiredOrder

log = logging.getLogger("mm.sim")
_ids = itertools.count(1)


class SimOrderManager(OrderManager):
    def __init__(self, cfg: Config, book: PositionBook):
        # No REST client needed; everything happens locally.
        super().__init__(rest=None, cfg=cfg, book=book)  # type: ignore[arg-type]

    async def reconcile(self, ticker: str, desired: list[DesiredOrder]) -> None:
        current = self.orders_for(ticker)
        want = {d.side: d for d in desired}
        for side in list(current):
            d = want.get(side)
            if d is None or d.price != current[side].price or d.size > current[side].size:
                current.pop(side, None)
        for side, d in want.items():
            if side not in current:
                current[side] = LiveOrder(f"sim-{next(_ids)}", side, d.price, d.size)

    async def cancel_all(self, ticker: str | None = None) -> None:
        for t in ([ticker] if ticker else list(self.live)):
            self.orders_for(t).clear()

    async def cross(self, ticker: str, c: CrossExit, book: Book | None = None) -> None:
        if book is None:
            return
        if c.side == "no":
            avail = sum(s for p, s in book.yes.items())  # we lift YES bids
            no_price = c.limit_price
            yes_price = 100 - no_price
        else:
            avail = sum(s for p, s in book.no.items())
            yes_price = c.limit_price
            no_price = 100 - yes_price
        size = min(c.size, avail) if avail else c.size
        if size <= 0:
            return
        from .fees import fee_cents
        fee = float(fee_cents(c.limit_price, size, self.cfg.taker_fee_mult))
        self.positions.on_fill(ticker, c.side, "buy", size, yes_price, no_price, fee)
        if self.on_booked:
            qty = size if c.side == "yes" else -size
            self.on_booked(ticker, c.side, "buy", size, c.limit_price, qty, fee,
                           True, c.reason)
        log.info("[sim] cross %s buy %s %d@%dc (%s)", ticker, c.side, size,
                 c.limit_price, c.reason)

    def on_public_trade(self, msg: dict) -> None:
        """Match the public tape against our virtual resting orders."""
        ticker = msg.get("market_ticker", "")
        count = int(msg.get("count", 0))
        yes_price = int(msg.get("yes_price", 0))
        taker_side = msg.get("taker_side", "")
        if not ticker or count <= 0:
            return
        orders = self.orders_for(ticker)

        if taker_side == "no":
            # Taker bought NO == sold YES into the bids at yes_price.
            o = orders.get("yes")
            if o and yes_price <= o.price:
                self._fill(ticker, o, min(count, o.size), o.price, 100 - o.price)
        elif taker_side == "yes":
            # Taker bought YES == sold NO into NO bids at no_price.
            o = orders.get("no")
            no_price = 100 - yes_price
            if o and no_price <= o.price:
                self._fill(ticker, o, min(count, o.size), 100 - o.price, o.price,
                           side="no")

    def _fill(self, ticker: str, order: LiveOrder, count: int,
              yes_price: int, no_price: int, side: str = "yes") -> None:
        from .fees import fee_cents
        price = yes_price if side == "yes" else no_price
        fee = float(fee_cents(price, count, self.cfg.maker_fee_mult))
        self.positions.on_fill(ticker, side, "buy", count, yes_price, no_price, fee)
        if self.on_booked:
            qty = count if side == "yes" else -count
            self.on_booked(ticker, side, "buy", count, price, qty, fee,
                           False, "maker")
        order.size -= count
        if order.size <= 0:
            self.orders_for(ticker).pop(side, None)
        log.info("[sim] maker fill %s buy %s %d@%dc", ticker, side, count, price)
