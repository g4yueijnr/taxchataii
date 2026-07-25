"""Polymarket CLOB market websocket: real-time book + trade prints.

Subscribes to the `market` channel for the YES token of each tracked market.
Message shapes are captured raw (last_message) so the first live run can
confirm the exact format on the dashboard — the same diagnostic that made
the Kalshi wiring debuggable in one screenshot.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import aiohttp

from .config import CLOB_WS
from .engine import Book, d2c

log = logging.getLogger("pm.feed")


class ClobFeed:
    def __init__(self, on_trade=None, on_book=None):
        self.books: dict[str, Book] = {}
        self.on_trade = on_trade          # async (token, price_cents, size)
        self.on_book = on_book            # async (token)
        self._assets: set[str] = set()
        self._ws = None
        self.msg_count = 0
        self.trade_count = 0
        self.last_message = ""
        self.last_ts = 0.0

    def book(self, token: str) -> Book:
        if token not in self.books:
            self.books[token] = Book(token)
        return self.books[token]

    async def set_assets(self, assets: set[str]) -> None:
        self._assets = set(assets)
        for a in assets:
            self.book(a)
        if self._ws is not None and not self._ws.closed:
            await self._subscribe()

    async def _subscribe(self) -> None:
        if not self._assets:
            return
        await self._ws.send_json({"type": "market",
                                  "assets_ids": sorted(self._assets)})

    async def run_forever(self) -> None:
        backoff = 1.0
        while True:
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.ws_connect(CLOB_WS, heartbeat=10) as ws:
                        self._ws = ws
                        log.info("clob ws connected")
                        backoff = 1.0
                        await self._subscribe()
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                await self._handle(msg.data)
                            elif msg.type in (aiohttp.WSMsgType.CLOSED,
                                              aiohttp.WSMsgType.ERROR):
                                break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("clob ws dropped: %s; retry %.0fs", e, backoff)
            self._ws = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _handle(self, raw: str) -> None:
        self.msg_count += 1
        self.last_message = raw[:400]
        self.last_ts = time.time()
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return
        events = data if isinstance(data, list) else [data]
        for ev in events:
            await self._handle_event(ev)

    async def _handle_event(self, ev: dict) -> None:
        # Real Polymarket market-channel format: a message with a
        # "price_changes" list, each entry {asset_id, price, size, side}.
        if "price_changes" in ev:
            for ch in ev["price_changes"]:
                tok = ch.get("asset_id")
                if not tok:
                    continue
                book = self.book(tok)
                side = "bid" if str(ch.get("side", "")).lower() in (
                    "buy", "bid") else "ask"
                book.set_level(side, d2c(float(ch.get("price", 0))),
                               float(ch.get("size", 0)))
                if self.on_book:
                    await self.on_book(tok)
            return
        et = ev.get("event_type") or ev.get("type")
        token = ev.get("asset_id") or ev.get("market") or ev.get("token_id")
        if not token:
            return
        book = self.book(token)
        if et == "book":
            bids = [(lvl["price"], lvl["size"]) for lvl in ev.get("bids", [])]
            asks = [(lvl["price"], lvl["size"]) for lvl in ev.get("asks", [])]
            book.apply_snapshot(bids, asks)
            if self.on_book:
                await self.on_book(token)
        elif et == "price_change":
            for ch in ev.get("changes", [ev]):
                side = "bid" if str(ch.get("side", "")).lower() in ("buy", "bid") else "ask"
                book.set_level(side, d2c(float(ch.get("price", 0))),
                               float(ch.get("size", 0)))
            if self.on_book:
                await self.on_book(token)
        elif et in ("last_trade_price", "trade", "tick_size_change"):
            price = ev.get("price")
            if price is not None:
                pc = d2c(float(price))
                book.last_trade = pc
                self.trade_count += 1
                if self.on_trade:
                    await self.on_trade(token, pc, float(ev.get("size", 0) or 0))
