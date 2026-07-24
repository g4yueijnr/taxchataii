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
        # Buying `c.side` at limit L crosses the *other* side's resting bids
        # priced >= 100 - L. Only those levels can fill us; consume them so
        # repeated picks can't fill against the same liquidity twice.
        ladder = book.yes if c.side == "no" else book.no
        thresh = 100 - c.limit_price
        crossable = sorted((p for p in ladder if p >= thresh), reverse=True)
        size = 0
        for p in crossable:
            take = min(c.size - size, ladder[p])
            ladder[p] -= take
            if ladder[p] <= 0:
                del ladder[p]
            size += take
            if size >= c.size:
                break
        if size <= 0:
            return
        yes_price = c.limit_price if c.side == "yes" else 100 - c.limit_price
        no_price = 100 - yes_price
        from .fees import fee_cents
        fee = float(fee_cents(c.limit_price, size, self.cfg.taker_fee_mult))
        self.positions.on_fill(ticker, c.side, "buy", size, yes_price, no_price, fee)
        if self.on_booked:
            qty = size if c.side == "yes" else -size
            self.on_booked(ticker, c.side, "buy", size, c.limit_price, qty, fee,
                           True, c.reason)
        log.info("[sim] cross %s buy %s %d@%dc (%s)", ticker, c.side, size,
                 c.limit_price, c.reason)

    # Diagnostics for the "flow but no fills" investigation.
    trades_seen: int = 0
    trades_parsed: int = 0        # had ticker + valid yes_price
    trades_on_our_market: int = 0  # we had a resting order on that ticker
    trades_crossed: int = 0       # price crossed one of our quotes -> fill
    last_trade_sample: str = ""

    def on_public_trade(self, msg: dict) -> None:
        """Match the public tape against our virtual resting orders.

        Price-based (robust to a missing/renamed taker_side field, which was
        silently zeroing all maker fills): a trade printing at yes_price P
        fills our resting YES bid B if P <= B (a sell swept down to/through
        our bid), and fills our resting NO bid N — i.e. sells our YES at the
        100-N ask — if 100-P <= N (a buy lifted up to/through our offer).
        Our bid < ask always, so a single print can trigger at most one.
        """
        self.trades_seen += 1
        if self.trades_seen <= 3 or not self.last_trade_sample:
            self.last_trade_sample = str(msg)[:300]
        ticker = msg.get("market_ticker") or msg.get("ticker") or ""
        count = int(msg.get("count", 0) or 0)
        yes_price = int(msg.get("yes_price") or 0)
        if yes_price <= 0 and msg.get("no_price"):
            yes_price = 100 - int(msg["no_price"])
        if not ticker or count <= 0 or not (1 <= yes_price <= 99):
            return
        self.trades_parsed += 1
        orders = self.orders_for(ticker)
        if orders:
            self.trades_on_our_market += 1

        yo = orders.get("yes")
        if yo and yes_price <= yo.price:
            self.trades_crossed += 1
            self._fill(ticker, yo, min(count, yo.size), yo.price,
                       100 - yo.price, side="yes")
            return
        no = orders.get("no")
        if no and (100 - yes_price) <= no.price:
            self.trades_crossed += 1
            self._fill(ticker, no, min(count, no.size), 100 - no.price,
                       no.price, side="no")

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
