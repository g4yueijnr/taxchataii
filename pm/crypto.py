"""Crypto-target market parsing, live spot feed, and fair-value math for the
news/event sniper.

The 'event' is a spot price move. When BTC/ETH moves on Coinbase, the fair
probability of a Polymarket crypto-target market ("reach $X", "dip to $X")
moves instantly, but the resting Polymarket orders lag seconds behind. We
compute fair from spot and take the stale mispriced side.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from dataclasses import dataclass

import aiohttp

from mm.model import EwmaVol

log = logging.getLogger("pm.crypto")

COINBASE_WS = "wss://ws-feed.exchange.coinbase.com"
PRODUCTS = {"BTC": "BTC-USD", "ETH": "ETH-USD"}
_ASSET_WORDS = {"bitcoin": "BTC", "btc": "BTC",
                "ethereum": "ETH", "eth": "ETH", "ether": "ETH"}


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass
class CryptoTarget:
    asset: str          # BTC | ETH
    threshold: float    # dollars
    kind: str           # up_touch | down_touch | up_term | down_term


def parse_question(q: str) -> CryptoTarget | None:
    """Parse a crypto price-target question, or None if it isn't one."""
    ql = q.lower()
    asset = None
    for word, sym in _ASSET_WORDS.items():
        if re.search(rf"\b{word}\b", ql):
            asset = sym
            break
    if not asset:
        return None
    m = re.search(r"\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(k)?", ql)
    if not m:
        return None
    val = float(m.group(1).replace(",", ""))
    if m.group(2) == "k":
        val *= 1000
    if val <= 0:
        return None
    up_words = ("reach", "hit", "touch", "above", "exceed", "surpass",
                "over", "cross")
    down_words = ("dip", "fall", "drop", "below", "under")
    if any(w in ql for w in up_words):
        up = True
    elif any(w in ql for w in down_words):
        up = False
    else:
        return None
    touch = any(w in ql for w in ("reach", "hit", "touch", "dip", "cross"))
    kind = ("up_" if up else "down_") + ("touch" if touch else "term")
    return CryptoTarget(asset, val, kind)


def fair_yes(t: CryptoTarget, spot: float, sigma_per_sec: float,
             seconds_left: float) -> float:
    """P(YES) for the target under driftless GBM. Touch uses the reflection
    principle; terminal uses the endpoint distribution. Clamped to [0,1]."""
    if spot <= 0 or t.threshold <= 0:
        return 0.5
    # Already-decided barriers.
    if t.kind == "up_touch" and spot >= t.threshold:
        return 1.0
    if t.kind == "down_touch" and spot <= t.threshold:
        return 1.0
    if seconds_left <= 0:
        if t.kind == "up_term":
            return 1.0 if spot >= t.threshold else 0.0
        if t.kind == "down_term":
            return 1.0 if spot <= t.threshold else 0.0
        return 0.0                      # touch not yet hit and time's up
    sd = sigma_per_sec * math.sqrt(seconds_left)
    if sd <= 0:
        return 0.5
    m = math.log(t.threshold / spot) / sd
    if t.kind == "up_term":
        return min(1.0, max(0.0, _phi(-m)))          # P(S_T >= B)
    if t.kind == "down_term":
        return min(1.0, max(0.0, _phi(m)))           # P(S_T <= B)
    if t.kind == "up_touch":                          # P(max >= B), B > spot
        return min(1.0, max(0.0, 2.0 * _phi(-m)))
    # down_touch: P(min <= B), B < spot
    return min(1.0, max(0.0, 2.0 * _phi(m)))


@dataclass
class TakeOrder:
    side: str          # buy | sell (YES)
    price_cents: int   # the book price we cross to
    size: float
    fair_cents: float  # our computed fair value (for logging)
    edge_cents: float  # how far the book was from fair


class SniperStrategy:
    """Compare the live-spot fair value against the resting Polymarket book and
    take a mispriced order. BUY the ask when it's cheaper than fair; SELL the
    bid when it's richer than fair. Guards against acting on a warming-up vol
    estimate, a stale spot, or an implausibly large (model-error) gap."""

    def __init__(self, cfg):
        self.cfg = cfg

    def decide(self, t: "CryptoTarget", spot: "Spot", book,
               seconds_left: float) -> "TakeOrder | None":
        cfg = self.cfg
        if spot.is_stale() or spot.price <= 0:
            return None
        if spot.vol.n_samples < EwmaVol.WARMUP_SAMPLES:
            return None                       # don't trade a cold vol estimate
        if not (cfg.sniper_min_seconds <= seconds_left <= cfg.sniper_max_seconds):
            return None
        sigma = spot.vol.sigma_per_sec
        if sigma <= 0:
            return None
        fair_c = fair_yes(t, spot.price, sigma, seconds_left) * 100.0
        edge = cfg.sniper_edge_cents
        cap = cfg.sniper_max_edge_cents

        # Cheapest ask to BUY YES: profitable if fair is above it by the edge.
        if book.asks:
            ask = book.best_ask
            gap = fair_c - ask
            if edge <= gap <= cap:
                size = min(cfg.sniper_size, float(book.asks.get(ask, 0.0)))
                if size > 0:
                    return TakeOrder("buy", ask, size, fair_c, gap)
        # Richest bid to SELL YES: profitable if fair is below it by the edge.
        if book.bids:
            bid = book.best_bid
            gap = bid - fair_c
            if edge <= gap <= cap:
                size = min(cfg.sniper_size, float(book.bids.get(bid, 0.0)))
                if size > 0:
                    return TakeOrder("sell", bid, size, fair_c, gap)
        return None


@dataclass
class Spot:
    price: float = 0.0
    last_update: float = 0.0
    vol: EwmaVol = None

    def __post_init__(self):
        if self.vol is None:
            self.vol = EwmaVol()

    def on_tick(self, price: float) -> None:
        self.vol.update(price)
        self.price = price
        self.last_update = time.time()

    def is_stale(self, max_age: float = 5.0) -> bool:
        return self.price <= 0 or (time.time() - self.last_update) > max_age


class SpotFeed:
    """Coinbase trade feed for BTC/ETH."""

    def __init__(self):
        self.spots: dict[str, Spot] = {"BTC": Spot(), "ETH": Spot()}
        self.msg_count = 0

    def get(self, asset: str) -> Spot:
        return self.spots.setdefault(asset, Spot())

    async def run_forever(self) -> None:
        backoff = 1.0
        while True:
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.ws_connect(COINBASE_WS, heartbeat=15) as ws:
                        await ws.send_json({
                            "type": "subscribe",
                            "product_ids": list(PRODUCTS.values()),
                            "channels": ["ticker", "heartbeat"]})
                        log.info("coinbase spot feed up")
                        backoff = 1.0
                        rev = {v: k for k, v in PRODUCTS.items()}
                        async for msg in ws:
                            if msg.type != aiohttp.WSMsgType.TEXT:
                                continue
                            d = json.loads(msg.data)
                            if d.get("type") == "ticker":
                                a = rev.get(d.get("product_id", ""))
                                px = d.get("price")
                                if a and px:
                                    self.get(a).on_tick(float(px))
                                    self.msg_count += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("spot feed dropped: %s; retry %.0fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
