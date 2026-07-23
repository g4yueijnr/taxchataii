"""Spot price feeds: Coinbase / Binance / Kraken trade websockets.

Coinbase and Kraken are CF Benchmarks index constituents (what Kalshi crypto
markets actually settle on), so they're preferred wherever the coin is listed
there. Binance is used for coins without US-exchange depth (BNB); override
BINANCE_WS_BASE to wss://stream.binance.us:9443 on US-hosted boxes if
binance.com is geo-blocked.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import websockets

from .config import CoinConfig
from .model import SpotState

log = logging.getLogger("mm.spot")

COINBASE_WS = "wss://ws-feed.exchange.coinbase.com"
BINANCE_WS = os.environ.get("BINANCE_WS_BASE", "wss://stream.binance.com:9443")
KRAKEN_WS = "wss://ws.kraken.com/v2"


class SpotFeeds:
    def __init__(self, coins: list[CoinConfig]):
        self.states: dict[str, SpotState] = {
            c.symbol: SpotState(c.symbol) for c in coins}
        self._by_source: dict[str, list[CoinConfig]] = {}
        for c in coins:
            self._by_source.setdefault(c.spot_source, []).append(c)

    def state(self, symbol: str) -> SpotState:
        return self.states[symbol]

    async def run_forever(self) -> None:
        tasks = []
        if "coinbase" in self._by_source:
            tasks.append(self._run_coinbase(self._by_source["coinbase"]))
        if "binance" in self._by_source:
            tasks.append(self._run_binance(self._by_source["binance"]))
        if "kraken" in self._by_source:
            tasks.append(self._run_kraken(self._by_source["kraken"]))
        if tasks:
            await asyncio.gather(*tasks)

    async def _loop(self, name: str, connect_fn) -> None:
        backoff = 1.0
        while True:
            try:
                await connect_fn()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("%s feed dropped: %s; retry in %.0fs", name, e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    # ----------------------------------------------------------- coinbase

    async def _run_coinbase(self, coins: list[CoinConfig]) -> None:
        products = {c.spot_symbol: c.symbol for c in coins}

        async def connect():
            async with websockets.connect(COINBASE_WS, ping_interval=15) as ws:
                await ws.send(json.dumps({
                    "type": "subscribe",
                    "product_ids": list(products),
                    "channels": ["matches", "heartbeat"],
                }))
                log.info("coinbase feed up: %s", list(products))
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("type") in ("match", "last_match"):
                        sym = products.get(msg.get("product_id", ""))
                        if sym:
                            self.states[sym].on_tick(float(msg["price"]))

        await self._loop("coinbase", connect)

    # ------------------------------------------------------------ binance

    async def _run_binance(self, coins: list[CoinConfig]) -> None:
        streams = {f"{c.spot_symbol}@trade": c.symbol for c in coins}
        url = f"{BINANCE_WS}/stream?streams={'/'.join(streams)}"

        async def connect():
            async with websockets.connect(url, ping_interval=15) as ws:
                log.info("binance feed up: %s", list(streams))
                async for raw in ws:
                    msg = json.loads(raw)
                    sym = streams.get(msg.get("stream", ""))
                    data = msg.get("data") or {}
                    if sym and "p" in data:
                        self.states[sym].on_tick(float(data["p"]))

        await self._loop("binance", connect)

    # ------------------------------------------------------------- kraken

    async def _run_kraken(self, coins: list[CoinConfig]) -> None:
        pairs = {c.spot_symbol: c.symbol for c in coins}

        async def connect():
            async with websockets.connect(KRAKEN_WS, ping_interval=15) as ws:
                await ws.send(json.dumps({
                    "method": "subscribe",
                    "params": {"channel": "trade", "symbol": list(pairs)},
                }))
                log.info("kraken feed up: %s", list(pairs))
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("channel") == "trade":
                        for t in msg.get("data") or []:
                            sym = pairs.get(t.get("symbol", ""))
                            if sym and "price" in t:
                                self.states[sym].on_tick(float(t["price"]))

        await self._loop("kraken", connect)
