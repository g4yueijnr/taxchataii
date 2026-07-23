"""Kalshi websocket: orderbook deltas, public trades, and our fills.

Auth is the same RSA-PSS signature, applied to the upgrade request for
GET /trade-api/ws/v2. Reconnects with backoff and re-subscribes to the
current market set; books are rebuilt from fresh snapshots on reconnect.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable

import websockets

from .kalshi_rest import KalshiRest, WS_URL
from .orderbook import Book

log = logging.getLogger("mm.ws")

# Public channels work unauthenticated; 'fill' requires auth.
PUBLIC_CHANNELS = ["orderbook_delta", "trade"]


class KalshiWs:
    def __init__(self, rest: KalshiRest,
                 on_book_update: Callable[[Book], Awaitable[None]] | None = None,
                 on_fill: Callable[[dict], Awaitable[None]] | None = None,
                 on_trade: Callable[[dict], Awaitable[None]] | None = None):
        self.rest = rest
        self.books: dict[str, Book] = {}
        self.on_book_update = on_book_update
        self.on_fill = on_fill
        self.on_trade = on_trade
        self._tickers: set[str] = set()
        self._ws: websockets.ClientProtocol | None = None
        self._cmd_id = 0
        self._connected = asyncio.Event()
        self.last_msg_ts: float = 0.0
        self.msg_counts: dict[str, int] = {}
        self.last_error: str = ""

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def book(self, ticker: str) -> Book:
        if ticker not in self.books:
            self.books[ticker] = Book(ticker)
        return self.books[ticker]

    async def set_markets(self, tickers: set[str]) -> None:
        """Adjust subscriptions to exactly this market set."""
        added = tickers - self._tickers
        self._tickers = set(tickers)
        for t in tickers:
            self.book(t)
        if added and self._ws is not None and self._connected.is_set():
            await self._subscribe(sorted(added))

    async def run_forever(self) -> None:
        backoff = 1.0
        while True:
            try:
                headers = self.rest.auth_headers("GET", "/trade-api/ws/v2")
                async with websockets.connect(
                        WS_URL, additional_headers=headers,
                        ping_interval=10, ping_timeout=10,
                        max_queue=4096) as ws:
                    self._ws = ws
                    self._connected.set()
                    log.info("kalshi ws connected")
                    backoff = 1.0
                    if self._tickers:
                        await self._subscribe(sorted(self._tickers))
                    async for raw in ws:
                        await self._handle(raw)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("kalshi ws dropped: %s; reconnect in %.0fs", e, backoff)
            self._connected.clear()
            self._ws = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _subscribe(self, tickers: list[str]) -> None:
        # Market-scoped channels and the account-scoped fill channel go in
        # SEPARATE commands — mixing them can invalidate the whole subscribe.
        self._cmd_id += 1
        await self._ws.send(json.dumps({
            "id": self._cmd_id,
            "cmd": "subscribe",
            "params": {"channels": list(PUBLIC_CHANNELS),
                       "market_tickers": tickers},
        }))
        if self.rest.can_trade:
            self._cmd_id += 1
            await self._ws.send(json.dumps({
                "id": self._cmd_id,
                "cmd": "subscribe",
                "params": {"channels": ["fill"]},
            }))

    async def _handle(self, raw: str | bytes) -> None:
        self.last_msg_ts = time.time()
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return
        mtype = msg.get("type")
        self.msg_counts[str(mtype)] = self.msg_counts.get(str(mtype), 0) + 1
        body = msg.get("msg") or {}
        ticker = body.get("market_ticker", "")

        if mtype == "orderbook_snapshot" and ticker:
            book = self.book(ticker)
            book.apply_snapshot(body)
            if self.on_book_update:
                await self.on_book_update(book)
        elif mtype == "orderbook_delta" and ticker:
            book = self.book(ticker)
            book.apply_delta(body)
            if self.on_book_update:
                await self.on_book_update(book)
        elif mtype == "trade" and ticker:
            book = self.book(ticker)
            book.last_trade_price = int(body.get("yes_price", 0))
            book.last_trade_ts = time.time()
            if self.on_trade:
                await self.on_trade(body)
        elif mtype == "fill":
            log.info("fill: %s", body)
            if self.on_fill:
                await self.on_fill(body)
        elif mtype == "error":
            self.last_error = json.dumps(msg)[:300]
            log.warning("kalshi ws error: %s", msg)
