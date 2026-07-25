"""Polymarket client.

Market metadata comes from the public Gamma API; live best-ask prices from the
public CLOB API. Trading goes through ``py-clob-client`` (imported lazily so
scanning works without it installed) using a Polygon wallet private key.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass, field

import requests

# Overridable so the same code can point at Polymarket US hosts. Set
# PM_GAMMA_BASE / PM_CLOB_BASE in the environment (Railway Variables) if the
# US exchange serves different endpoints.
GAMMA_BASE = os.environ.get("PM_GAMMA_BASE", "https://gamma-api.polymarket.com")
CLOB_BASE = os.environ.get("PM_CLOB_BASE", "https://clob.polymarket.com")


@dataclass
class PolyMarket:
    condition_id: str
    question: str
    yes_token: str
    no_token: str
    yes_ask: float | None  # dollars 0-1
    no_ask: float | None
    yes_bid: float | None
    end_date: dt.datetime | None
    volume: float
    slug: str
    raw: dict = field(repr=False, default_factory=dict)

    @property
    def url(self) -> str:
        return f"https://polymarket.com/market/{self.slug}" if self.slug else ""


def _parse_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class PolymarketClient:
    def __init__(self, private_key: str | None = None, funder: str | None = None,
                 signature_type: int = 0, timeout: float = 30.0):
        self.private_key = private_key
        self.funder = funder
        self.signature_type = signature_type
        self.timeout = timeout
        self._session = requests.Session()
        self._clob = None  # lazy py_clob_client

    # ----------------------------------------------------------- market data

    def fetch_open_markets(self, min_volume: float = 0, log=None) -> list[PolyMarket]:
        """Page through every active binary market on Gamma."""
        markets: list[PolyMarket] = []
        offset = 0
        limit = 500
        while True:
            resp = self._session.get(
                f"{GAMMA_BASE}/markets",
                params={"active": "true", "closed": "false",
                        "limit": limit, "offset": offset},
                timeout=self.timeout)
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            for m in batch:
                pm = self._parse_market(m, min_volume)
                if pm:
                    markets.append(pm)
            offset += limit
            if log:
                log(f"  Polymarket: offset {offset}, {len(markets)} binary markets so far")
            if len(batch) < limit:
                break
        return markets

    @staticmethod
    def _parse_market(m: dict, min_volume: float) -> PolyMarket | None:
        try:
            outcomes = json.loads(m.get("outcomes") or "[]")
            tokens = json.loads(m.get("clobTokenIds") or "[]")
        except (TypeError, json.JSONDecodeError):
            return None
        # Only plain Yes/No binary markets are comparable to Kalshi contracts.
        if len(outcomes) != 2 or len(tokens) != 2:
            return None
        if sorted(o.lower() for o in outcomes) != ["no", "yes"]:
            return None
        yes_idx = 0 if outcomes[0].lower() == "yes" else 1
        volume = float(m.get("volumeNum") or m.get("volume") or 0)
        if volume < min_volume:
            return None
        if not m.get("enableOrderBook", True):
            return None

        best_ask = m.get("bestAsk")   # ask for the YES token
        best_bid = m.get("bestBid")
        yes_ask = float(best_ask) if best_ask is not None else None
        yes_bid = float(best_bid) if best_bid is not None else None
        # NO ask = 1 - YES bid (buying NO == selling YES on a CLOB with merged books)
        no_ask = round(1 - yes_bid, 4) if yes_bid is not None else None

        return PolyMarket(
            condition_id=m.get("conditionId", ""),
            question=m.get("question") or "",
            yes_token=tokens[yes_idx],
            no_token=tokens[1 - yes_idx],
            yes_ask=yes_ask,
            no_ask=no_ask,
            yes_bid=yes_bid,
            end_date=_parse_time(m.get("endDate")),
            volume=volume,
            slug=m.get("slug") or "",
            raw=m,
        )

    def refresh_asks(self, markets: list[PolyMarket]) -> None:
        """Refresh best asks for both tokens straight from the CLOB (batched)."""
        queries = []
        for m in markets:
            queries.append({"token_id": m.yes_token, "side": "BUY"})
            queries.append({"token_id": m.no_token, "side": "BUY"})
        prices: dict = {}
        for i in range(0, len(queries), 500):
            resp = self._session.post(f"{CLOB_BASE}/prices",
                                      json=queries[i:i + 500], timeout=self.timeout)
            resp.raise_for_status()
            prices.update(resp.json())
        for m in markets:
            yes = prices.get(m.yes_token, {}).get("BUY")
            no = prices.get(m.no_token, {}).get("BUY")
            if yes is not None:
                m.yes_ask = float(yes)
            if no is not None:
                m.no_ask = float(no)

    def get_book(self, token_id: str) -> dict:
        resp = self._session.get(f"{CLOB_BASE}/book",
                                 params={"token_id": token_id}, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    # -------------------------------------------------------------- trading

    @property
    def can_trade(self) -> bool:
        return bool(self.private_key)

    def _clob_client(self):
        if self._clob is None:
            if not self.can_trade:
                raise RuntimeError(
                    "Polymarket trading requires POLYMARKET_PRIVATE_KEY")
            from py_clob_client.client import ClobClient
            kwargs = {"key": self.private_key, "chain_id": 137}
            if self.funder:
                kwargs["funder"] = self.funder
                kwargs["signature_type"] = self.signature_type
            client = ClobClient(CLOB_BASE, **kwargs)
            client.set_api_creds(client.create_or_derive_api_creds())
            self._clob = client
        return self._clob

    def buy_at_ask(self, token_id: str, usdc_amount: float, max_price: float) -> dict:
        """Fill-or-kill market buy: spend `usdc_amount` USDC on the token,
        rejecting entirely if it can't fill at or under `max_price`."""
        from py_clob_client.clob_types import MarketOrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY

        client = self._clob_client()
        order = client.create_market_order(MarketOrderArgs(
            token_id=token_id, amount=round(usdc_amount, 2),
            side=BUY, price=max_price))
        return client.post_order(order, OrderType.FOK)
