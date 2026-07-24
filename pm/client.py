"""Async Polymarket data client: Gamma discovery + CLOB REST book snapshots.

Public market data needs no keys, so paper trading runs unauthenticated.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import aiohttp

from .config import CLOB_BASE, GAMMA_BASE, Config

log = logging.getLogger("pm.client")


@dataclass
class Market:
    condition_id: str
    question: str
    slug: str
    yes_token: str
    volume: float


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
        params = {"active": "true", "closed": "false", "limit": 500,
                  "order": "volumeNum", "ascending": "false"}
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
        for m in batch or []:
            mk = self._parse(m)
            if mk and mk.volume >= self.cfg.min_volume:
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
        yes_idx = 0 if outcomes[0].lower() == "yes" else 1
        return Market(
            condition_id=m.get("conditionId", ""),
            question=m.get("question") or "",
            slug=m.get("slug") or "",
            yes_token=tokens[yes_idx],
            volume=float(m.get("volumeNum") or m.get("volume") or 0),
        )

    async def get_book(self, token_id: str) -> dict:
        assert self._session is not None
        async with self._session.get(f"{CLOB_BASE}/book",
                                     params={"token_id": token_id}) as r:
            return await r.json()
