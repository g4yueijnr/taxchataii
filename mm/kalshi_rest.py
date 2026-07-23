"""Async Kalshi REST client (aiohttp) with RSA-PSS request signing.

Same auth scheme as arb/kalshi.py, async so order placement/cancels never
block the market-data loops. Writes go through a token bucket so we stay
inside Kalshi's per-tier rate limits.
"""

from __future__ import annotations

import asyncio
import base64
import time
import uuid

import aiohttp

KALSHI_BASE = "https://api.elections.kalshi.com"
API_PREFIX = "/trade-api/v2"
WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"


class TokenBucket:
    def __init__(self, rate_per_sec: float, burst: int = 4):
        self.rate = rate_per_sec
        self.capacity = float(burst)
        self.tokens = float(burst)
        self.last = time.monotonic()
        self._lock = asyncio.Lock()

    async def take(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
                self.last = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                await asyncio.sleep((1.0 - self.tokens) / self.rate)


class KalshiRest:
    def __init__(self, api_key_id: str = "", private_key_pem: bytes = b"",
                 base_url: str = KALSHI_BASE, write_rate: float = 4.0):
        self.base_url = base_url.rstrip("/")
        self.api_key_id = api_key_id
        self._private_key = None
        if private_key_pem:
            from cryptography.hazmat.primitives.serialization import load_pem_private_key
            self._private_key = load_pem_private_key(private_key_pem, password=None)
        self._session: aiohttp.ClientSession | None = None
        self._write_bucket = TokenBucket(write_rate)

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10))

    async def close(self) -> None:
        if self._session:
            await self._session.close()

    @property
    def can_trade(self) -> bool:
        return bool(self.api_key_id and self._private_key)

    def auth_headers(self, method: str, path: str) -> dict:
        """RSA-PSS over timestamp_ms + METHOD + path. Public method because
        the websocket handshake signs the same way."""
        if not self.can_trade:
            return {}
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        ts = str(int(time.time() * 1000))
        sig = self._private_key.sign(
            (ts + method.upper() + path).encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

    async def _request(self, method: str, path: str, *, auth: bool = False,
                       params: dict | None = None, json_body: dict | None = None) -> dict:
        assert self._session is not None, "call start() first"
        full = API_PREFIX + path
        headers = self.auth_headers(method, full) if auth else {}
        async with self._session.request(
                method, self.base_url + full, params=params,
                json=json_body, headers=headers) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise KalshiApiError(resp.status, f"{method} {path}: {text[:300]}")
            import json as _json
            return _json.loads(text) if text else {}

    # ------------------------------------------------------------- markets

    async def get_markets(self, series_ticker: str, status: str = "open") -> list[dict]:
        data = await self._request("GET", "/markets", params={
            "series_ticker": series_ticker, "status": status, "limit": 100})
        return data.get("markets", [])

    async def get_market(self, ticker: str) -> dict:
        data = await self._request("GET", f"/markets/{ticker}")
        return data.get("market", {})

    async def get_orderbook(self, ticker: str, depth: int = 20) -> dict:
        data = await self._request("GET", f"/markets/{ticker}/orderbook",
                                   params={"depth": depth})
        return data.get("orderbook") or {}

    async def get_trades(self, ticker: str, limit: int = 50) -> list[dict]:
        data = await self._request("GET", "/markets/trades",
                                   params={"ticker": ticker, "limit": limit})
        return data.get("trades", [])

    async def list_series(self, category: str = "Crypto") -> list[dict]:
        data = await self._request("GET", "/series", params={"category": category})
        return data.get("series", [])

    # ----------------------------------------------------------- portfolio

    async def get_balance(self) -> int:
        data = await self._request("GET", "/portfolio/balance", auth=True)
        return int(data.get("balance", 0))

    async def get_positions(self) -> list[dict]:
        data = await self._request("GET", "/portfolio/positions", auth=True,
                                   params={"limit": 200})
        return data.get("market_positions", [])

    async def get_resting_orders(self, ticker: str | None = None) -> list[dict]:
        params: dict = {"status": "resting", "limit": 200}
        if ticker:
            params["ticker"] = ticker
        data = await self._request("GET", "/portfolio/orders", auth=True,
                                   params=params)
        return data.get("orders", [])

    async def create_order(self, ticker: str, action: str, side: str,
                           count: int, price_cents: int,
                           post_only: bool = False,
                           expiration_ts: int | None = None) -> dict:
        assert action in ("buy", "sell") and side in ("yes", "no")
        await self._write_bucket.take()
        body: dict = {
            "ticker": ticker,
            "client_order_id": str(uuid.uuid4()),
            "action": action,
            "side": side,
            "count": count,
            "type": "limit",
            f"{side}_price": price_cents,
        }
        if post_only:
            body["post_only"] = True
        if expiration_ts:
            body["expiration_ts"] = expiration_ts
        data = await self._request("POST", "/portfolio/orders", auth=True,
                                   json_body=body)
        return data.get("order", data)

    async def cancel_order(self, order_id: str) -> None:
        await self._write_bucket.take()
        try:
            await self._request("DELETE", f"/portfolio/orders/{order_id}", auth=True)
        except KalshiApiError as e:
            # Already filled/canceled races are normal for a market maker.
            if e.status not in (404, 400):
                raise


class KalshiApiError(RuntimeError):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status
