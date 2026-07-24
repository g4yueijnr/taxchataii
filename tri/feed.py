"""Binance bookTicker websocket feed.

The @bookTicker stream pushes the best bid/ask (and their sizes) for a symbol
on every change — typically many updates per second per symbol on liquid
pairs. That update rate is exactly the speed edge a triangular-arb bot needs:
we re-evaluate every affected triangle the instant any leg moves.

Override BINANCE_WS_BASE to wss://stream.binance.us:9443 on US-hosted boxes
if binance.com is geo-blocked (note: binance.us lists fewer cross pairs).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import websockets

from .engine import BookStore

log = logging.getLogger("tri.feed")
BINANCE_WS = os.environ.get("BINANCE_WS_BASE", "wss://stream.binance.com:9443")


class BinanceFeed:
    def __init__(self, symbols: list[str], books: BookStore,
                 on_update=None):
        self.symbols = [s.lower() for s in symbols]
        self.books = books
        self.on_update = on_update       # async callback(symbol)
        self.msg_count = 0
        self.last_msg_ts = 0.0

    async def run_forever(self) -> None:
        streams = "/".join(f"{s}@bookTicker" for s in self.symbols)
        url = f"{BINANCE_WS}/stream?streams={streams}"
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(url, ping_interval=15,
                                              max_queue=8192) as ws:
                    log.info("binance feed up: %d symbols", len(self.symbols))
                    backoff = 1.0
                    async for raw in ws:
                        await self._handle(raw)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("binance feed dropped: %s; retry in %.0fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _handle(self, raw: str) -> None:
        import time
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return
        data = msg.get("data") or msg
        sym = data.get("s")
        if not sym:
            return
        try:
            bid = float(data["b"]); ask = float(data["a"])
            bq = float(data.get("B", 0)); aq = float(data.get("A", 0))
        except (KeyError, ValueError):
            return
        self.books.update(sym, bid, ask, bq, aq, time.time())
        self.msg_count += 1
        self.last_msg_ts = time.time()
        if self.on_update:
            await self.on_update(sym)
