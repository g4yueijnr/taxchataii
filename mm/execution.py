"""Order management, positions, and P&L.

One resting order per side per market, reconciled against the strategy's
desired quotes: unchanged quotes are left alone (queue priority is money),
changed ones are cancel/replaced, cancels ahead of placements. Cross exits
are sent as short-expiry limits at the touch so a miss can't rest and get
picked off. Everything books through PositionBook in YES-equivalent terms:
buying NO at q is selling YES at 100-q.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .config import Config
from .fees import fee_cents
from .kalshi_rest import KalshiApiError, KalshiRest
from .orderbook import Book
from .strategy import CrossExit, DesiredOrder

log = logging.getLogger("mm.exec")


@dataclass
class Position:
    ticker: str
    net: int = 0                # + long YES, - short YES (long NO)
    avg_entry: float = 0.0      # yes-equivalent cents basis of open position


@dataclass
class PositionBook:
    positions: dict[str, Position] = field(default_factory=dict)
    realized_cents: float = 0.0
    fees_cents: float = 0.0
    fills: int = 0

    def pos(self, ticker: str) -> Position:
        if ticker not in self.positions:
            self.positions[ticker] = Position(ticker)
        return self.positions[ticker]

    def on_fill(self, ticker: str, side: str, action: str, count: int,
                yes_price: int, no_price: int, fee: float) -> None:
        """Book a fill in YES-equivalent terms."""
        p = self.pos(ticker)
        # buy yes / sell no => +qty at yes_price; buy no / sell yes => -qty
        if (side == "yes") == (action == "buy"):
            qty, px = count, float(yes_price)
        else:
            qty, px = -count, float(100 - no_price)
        self.fees_cents += fee
        self.fills += 1

        if p.net == 0 or (p.net > 0) == (qty > 0):
            total = p.net + qty
            p.avg_entry = (p.avg_entry * abs(p.net) + px * abs(qty)) / max(abs(total), 1)
            p.net = total
            return
        # Reducing / flipping: realize P&L on the closed portion.
        closed = min(abs(qty), abs(p.net))
        pnl_per = (px - p.avg_entry) if p.net > 0 else (p.avg_entry - px)
        self.realized_cents += pnl_per * closed
        p.net += qty
        if p.net == 0:
            p.avg_entry = 0.0
        elif (p.net > 0) != (qty < 0):   # flipped through zero
            p.avg_entry = px

    def settle(self, ticker: str, result: str) -> None:
        """Realize residual inventory at settlement (result 'yes' or 'no')."""
        p = self.positions.get(ticker)
        if not p or p.net == 0:
            return
        settle_px = 100.0 if result == "yes" else 0.0
        pnl_per = (settle_px - p.avg_entry) if p.net > 0 else (p.avg_entry - settle_px)
        self.realized_cents += pnl_per * abs(p.net)
        log.info("settled %s result=%s residual=%+d pnl=%.0fc",
                 ticker, result, p.net, pnl_per * abs(p.net))
        p.net = 0
        p.avg_entry = 0.0

    def gross_collateral_cents(self) -> float:
        """Worst-case collateral tied up across open positions."""
        total = 0.0
        for p in self.positions.values():
            if p.net > 0:
                total += p.avg_entry * p.net
            elif p.net < 0:
                total += (100.0 - p.avg_entry) * -p.net
        return total

    @property
    def net_pnl_cents(self) -> float:
        return self.realized_cents - self.fees_cents


@dataclass
class LiveOrder:
    order_id: str
    side: str
    price: int
    size: int
    placed_at: float = field(default_factory=time.time)


class OrderManager:
    """Live order reconciliation against Kalshi."""

    def __init__(self, rest: KalshiRest, cfg: Config, book: PositionBook):
        self.rest = rest
        self.cfg = cfg
        self.positions = book
        self.live: dict[str, dict[str, LiveOrder]] = {}   # ticker -> side -> order

    def orders_for(self, ticker: str) -> dict[str, LiveOrder]:
        return self.live.setdefault(ticker, {})

    async def reconcile(self, ticker: str, desired: list[DesiredOrder]) -> None:
        current = self.orders_for(ticker)
        want = {d.side: d for d in desired}

        # Cancels first: stale quotes are the adverse-selection surface.
        for side in list(current):
            have = current[side]
            d = want.get(side)
            if d is None or d.price != have.price or d.size > have.size:
                await self._cancel(ticker, side)

        for side, d in want.items():
            if side in current:
                continue
            await self._place(ticker, side, d.price, d.size)

    async def cancel_all(self, ticker: str | None = None) -> None:
        tickers = [ticker] if ticker else list(self.live)
        for t in tickers:
            for side in list(self.orders_for(t)):
                await self._cancel(t, side)

    async def cross(self, ticker: str, c: CrossExit) -> None:
        """Take liquidity to exit. Short expiration so leftovers can't rest."""
        try:
            resp = await self.rest.create_order(
                ticker, "buy", c.side, c.size, c.limit_price,
                expiration_ts=int(time.time()) + 5)
            log.info("cross %s buy %s %d@%dc (%s) -> %s",
                     ticker, c.side, c.size, c.limit_price, c.reason,
                     resp.get("order_id", "?"))
        except KalshiApiError as e:
            log.warning("cross failed %s: %s", ticker, e)

    async def _place(self, ticker: str, side: str, price: int, size: int) -> None:
        try:
            resp = await self.rest.create_order(ticker, "buy", side, size, price)
            oid = resp.get("order_id") or resp.get("id", "")
            self.orders_for(ticker)[side] = LiveOrder(oid, side, price, size)
        except KalshiApiError as e:
            log.warning("place failed %s %s %d@%dc: %s", ticker, side, size, price, e)

    async def _cancel(self, ticker: str, side: str) -> None:
        order = self.orders_for(ticker).pop(side, None)
        if order and order.order_id:
            try:
                await self.rest.cancel_order(order.order_id)
            except KalshiApiError as e:
                log.warning("cancel failed %s: %s", ticker, e)

    def on_fill_msg(self, msg: dict) -> None:
        """Fill from the websocket: book it and shrink/clear the live order."""
        ticker = msg.get("market_ticker", "")
        side = msg.get("side", "yes")
        action = msg.get("action", "buy")
        count = int(msg.get("count", 0))
        yes_price = int(msg.get("yes_price", 0))
        no_price = int(msg.get("no_price", 100 - yes_price))
        is_taker = bool(msg.get("is_taker"))
        mult = self.cfg.taker_fee_mult if is_taker else self.cfg.maker_fee_mult
        price = yes_price if side == "yes" else no_price
        fee = float(fee_cents(price, count, mult))
        self.positions.on_fill(ticker, side, action, count, yes_price, no_price, fee)

        current = self.orders_for(ticker).get(side)
        if current and msg.get("order_id") == current.order_id:
            current.size -= count
            if current.size <= 0:
                self.orders_for(ticker).pop(side, None)
