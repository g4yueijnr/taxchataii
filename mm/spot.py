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
        tasks = [self._watchdog()]
        if "coinbase" in self._by_source:
            tasks.append(self._run_coinbase(self._by_source["coinbase"]))
        if "binance" in self._by_source:
            tasks.append(self._run_binance(self._by_source["binance"]))
        if "kraken" in self._by_source:
            tasks.append(self._run_kraken(self._by_source["kraken"]))
        await asyncio.gather(*tasks)

    async def _watchdog(self) -> None:
        """Make silent feeds loud: a coin with no price can't be traded."""
        while True:
            await asyncio.sleep(60)
            for sym, s in self.states.items():
                if s.price <= 0:
                    log.warning(
                        "NO DATA for %s — its spot feed has never ticked; "
                        "the bot cannot trade this coin until it does", sym)

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
                    "channels": ["ticker", "heartbeat"],
                }))
                log.info("coinbase feed up: %s", list(products))
                async for raw in ws:
                    msg = json.loads(raw)
                    mtype = msg.get("type")
                    sym = products.get(msg.get("product_id", ""))
                    if not sym:
                        continue
                    if mtype == "ticker":
                        # Prefer BBO mid (moves without trades); fall back
                        # to last trade price.
                        bid = float(msg.get("best_bid") or 0)
                        ask = float(msg.get("best_ask") or 0)
                        px = (bid + ask) / 2 if bid > 0 and ask > 0 else \
                            float(msg.get("price") or 0)
                        if px > 0:
                            self.states[sym].on_tick(px)
                    elif mtype == "heartbeat":
                        self.states[sym].touch()

        await self._loop("coinbase", connect)

    # ------------------------------------------------------------ binance

    async def _run_binance(self, coins: list[CoinConfig]) -> None:
        streams: dict[str, str] = {}
        for c in coins:
            symbols = {c.spot_symbol}
            # binance.us names USD pairs without the T (bnbusd vs bnbusdt);
            # subscribe to both variants — the dead one just stays silent.
            if c.spot_symbol.endswith("usdt"):
                symbols.add(c.spot_symbol[:-1])
            for s in symbols:
                streams[f"{s}@trade"] = c.symbol
                streams[f"{s}@bookTicker"] = c.symbol
        url = f"{BINANCE_WS}/stream?streams={'/'.join(streams)}"

        async def connect():
            async with websockets.connect(url, ping_interval=15) as ws:
                log.info("binance feed up: %s", list(streams))
                async for raw in ws:
                    msg = json.loads(raw)
                    stream = msg.get("stream", "")
                    sym = streams.get(stream)
                    data = msg.get("data") or {}
                    if not sym:
                        continue
                    if stream.endswith("@bookTicker"):
                        bid, ask = float(data.get("b") or 0), float(data.get("a") or 0)
                        if bid > 0 and ask > 0:
                            self.states[sym].on_tick((bid + ask) / 2)
                    elif "p" in data:
                        self.states[sym].on_tick(float(data["p"]))

        await self._loop("binance", connect)

    # ------------------------------------------------------------- kraken

    async def _run_kraken(self, coins: list[CoinConfig]) -> None:
        pairs = {c.spot_symbol: c.symbol for c in coins}

        async def connect():
            async with websockets.connect(KRAKEN_WS, ping_interval=15) as ws:
                # BBO-triggered ticker: updates whenever the top of book
                # moves, which is what keeps thin coins (ZEC, NEAR) fresh.
                await ws.send(json.dumps({
                    "method": "subscribe",
                    "params": {"channel": "ticker", "symbol": list(pairs),
                               "event_trigger": "bbo"},
                }))
                log.info("kraken feed up: %s", list(pairs))
                async for raw in ws:
                    msg = json.loads(raw)
                    channel = msg.get("channel")
                    if channel == "ticker":
                        for t in msg.get("data") or []:
                            sym = pairs.get(t.get("symbol", ""))
                            if not sym:
                                continue
                            bid = float(t.get("bid") or 0)
                            ask = float(t.get("ask") or 0)
                            px = (bid + ask) / 2 if bid > 0 and ask > 0 else \
                                float(t.get("last") or 0)
                            if px > 0:
                                self.states[sym].on_tick(px)
                    elif channel == "heartbeat":
                        # Kraken heartbeats ~1/s while the connection lives.
                        for sym in pairs.values():
                            self.states[sym].touch()

        await self._loop("kraken", connect)
