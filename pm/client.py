"""Async Polymarket data client: Gamma discovery + CLOB REST book snapshots.

Public market data needs no keys, so paper trading runs unauthenticated.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime

import aiohttp

from .config import CLOB_BASE, DATA_BASE, GAMMA_BASE, Config

log = logging.getLogger("pm.client")


def _parse_end_date(raw) -> float | None:
    """Gamma endDate is ISO-8601 (e.g. '2026-07-31T12:00:00Z'). -> unix secs."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


@dataclass
class Market:
    condition_id: str
    question: str
    slug: str
    yes_token: str
    volume: float          # 24h volume ($), the ranking signal
    best_bid: float | None = None
    best_ask: float | None = None
    end_date: float | None = None   # unix seconds; market settlement deadline


class PolyClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15))

    async def close(self) -> None:
        if self._session:
            await self._session.close()

    async def discover(self) -> list[Market]:
        """Active binary markets, filtered by category/volume, top-N by volume.

        The YES token is the one we quote; NO is just 1 - YES on a merged CLOB.
        """
        assert self._session is not None
        # Rank by RECENT (24h) volume so we get markets trading NOW, not dead
        # longshots with huge lifetime volume pinned at 1-2c.
        params = {"active": "true", "closed": "false", "limit": 500,
                  "order": "volume24hr", "ascending": "false"}
        if self.cfg.category:
            params["tag_slug"] = self.cfg.category
        markets: list[Market] = []
        try:
            async with self._session.get(f"{GAMMA_BASE}/markets",
                                         params=params) as r:
                batch = await r.json()
        except Exception as e:
            log.warning("discovery failed: %s", e)
            return []
        sniper = self.cfg.mode == "sniper"
        lo, hi = self.cfg.min_mid_cents / 100.0, self.cfg.max_mid_cents / 100.0
        for m in batch or []:
            mk = self._parse(m)
            if not mk or mk.volume < self.cfg.min_volume:
                continue
            if sniper:
                # Sniper only trades crypto price-target markets; the mid can
                # legitimately sit at the extremes ("reach $X" at 4c), so we
                # skip the two-sided-mid filter and require a parseable target.
                from .crypto import parse_question
                if parse_question(mk.question) is None:
                    continue
                markets.append(mk)
                continue
            # Skip markets pinned at the extremes -- no real two-sided market.
            if mk.best_bid is None or mk.best_ask is None:
                continue
            mid = (mk.best_bid + mk.best_ask) / 2
            if not (lo <= mid <= hi):
                continue
            markets.append(mk)
        if self.cfg.explicit_slugs:
            markets = [m for m in markets if m.slug in self.cfg.explicit_slugs]
        markets.sort(key=lambda m: m.volume, reverse=True)
        return markets[: self.cfg.max_markets]

    @staticmethod
    def _parse(m: dict) -> Market | None:
        try:
            outcomes = json.loads(m.get("outcomes") or "[]")
            tokens = json.loads(m.get("clobTokenIds") or "[]")
        except (TypeError, json.JSONDecodeError):
            return None
        if len(outcomes) != 2 or len(tokens) != 2:
            return None
        if sorted(o.lower() for o in outcomes) != ["no", "yes"]:
            return None
        if not m.get("enableOrderBook", True):
            return None
        if m.get("acceptingOrders") is False:
            return None
        yes_idx = 0 if outcomes[0].lower() == "yes" else 1
        bb = m.get("bestBid")
        ba = m.get("bestAsk")
        return Market(
            condition_id=m.get("conditionId", ""),
            question=m.get("question") or "",
            slug=m.get("slug") or "",
            yes_token=tokens[yes_idx],
            volume=float(m.get("volume24hr") or m.get("volumeNum") or 0),
            best_bid=float(bb) if bb is not None else None,
            best_ask=float(ba) if ba is not None else None,
            end_date=_parse_end_date(m.get("endDate")),
        )

    async def get_book(self, token_id: str) -> dict:
        assert self._session is not None
        async with self._session.get(f"{CLOB_BASE}/book",
                                     params={"token_id": token_id}) as r:
            return await r.json()

    async def get_trades(self, condition_id: str, limit: int = 50) -> list[dict]:
        """Public recent trades for a market (data-api), for paper fills when
        the websocket tape isn't flowing."""
        assert self._session is not None
        try:
            async with self._session.get(
                    f"{DATA_BASE}/trades",
                    params={"market": condition_id, "limit": limit}) as r:
                data = await r.json()
                return data if isinstance(data, list) else data.get("data", [])
        except Exception:
            return []
