"""Kalshi API client.

Public market data needs no auth. Trading uses an API key ID plus an RSA
private key: each request is signed with RSA-PSS over
``timestamp_ms + METHOD + path``.
"""

from __future__ import annotations

import base64
import datetime as dt
import os
import time
import uuid
from dataclasses import dataclass, field

import requests

KALSHI_BASE = os.environ.get("KALSHI_BASE", "https://api.elections.kalshi.com")
API_PREFIX = "/trade-api/v2"


@dataclass
class KalshiMarket:
    ticker: str
    title: str
    yes_ask: int  # cents, 1-99 (0 means no ask)
    no_ask: int   # cents
    yes_bid: int
    no_bid: int
    close_time: dt.datetime | None
    volume: int
    raw: dict = field(repr=False, default_factory=dict)

    @property
    def url(self) -> str:
        return f"https://kalshi.com/markets/{self.ticker}"


def _parse_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class KalshiClient:
    def __init__(self, api_key_id: str | None = None, private_key_pem: bytes | None = None,
                 base_url: str = KALSHI_BASE, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.api_key_id = api_key_id
        self.timeout = timeout
        self._private_key = None
        if private_key_pem:
            from cryptography.hazmat.primitives.serialization import load_pem_private_key
            self._private_key = load_pem_private_key(private_key_pem, password=None)
        self._session = requests.Session()

    # ------------------------------------------------------------------ auth

    @property
    def can_trade(self) -> bool:
        return bool(self.api_key_id and self._private_key)

    def _auth_headers(self, method: str, path: str) -> dict:
        if not self.can_trade:
            raise RuntimeError(
                "Kalshi trading requires KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH")
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        timestamp = str(int(time.time() * 1000))
        message = (timestamp + method.upper() + path).encode()
        signature = self._private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
        }

    def _request(self, method: str, path: str, *, auth: bool = False,
                 params: dict | None = None, json_body: dict | None = None) -> dict:
        full_path = API_PREFIX + path
        # Sign whenever we hold keys, not just for trading: Kalshi's
        # authenticated rate-limit tier is far higher than the anonymous one,
        # and signing a public GET costs nothing. This is what keeps a full
        # market scan from tripping 429s.
        headers = (self._auth_headers(method, full_path)
                   if (auth or self.can_trade) else {})
        backoff = 0.5
        last = ""
        for _ in range(5):
            resp = self._session.request(
                method, self.base_url + full_path,
                params=params, json=json_body, headers=headers,
                timeout=self.timeout)
            if resp.status_code == 429 or resp.status_code >= 500:
                # Respect Retry-After when present, else exponential backoff.
                try:
                    wait = float(resp.headers.get("Retry-After", "")) or backoff
                except ValueError:
                    wait = backoff
                last = f"{resp.status_code}: {resp.text[:200]}"
                time.sleep(min(wait, 8.0))
                backoff = min(backoff * 2, 8.0)
                continue
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"Kalshi {method} {path} -> {resp.status_code}: {resp.text[:500]}")
            return resp.json()
        raise RuntimeError(f"Kalshi {method} {path} rate-limited after retries ({last})")

    # ----------------------------------------------------------- market data

    def fetch_open_markets(self, min_volume: int = 0, log=None,
                           max_pages: int = 10, page_pause: float = 0.25
                           ) -> list[KalshiMarket]:
        """Page through open markets. Capped at ``max_pages`` and returns
        whatever it has if a page hard-fails (rate limits shouldn't zero out a
        whole scan). Add Kalshi keys to lift the rate ceiling and raise the cap.
        """
        markets: list[KalshiMarket] = []
        cursor = None
        page = 0
        while page < max_pages:
            params = {"limit": 1000, "status": "open"}
            if cursor:
                params["cursor"] = cursor
            try:
                data = self._request("GET", "/markets", params=params)
            except RuntimeError as e:
                if log:
                    log(f"  Kalshi: stopped early after {len(markets)} "
                        f"markets ({e})")
                break
            for m in data.get("markets", []):
                if m.get("volume", 0) < min_volume:
                    continue
                title = m.get("title") or ""
                sub = m.get("yes_sub_title") or m.get("subtitle") or ""
                if sub and sub.lower() not in title.lower():
                    title = f"{title} ({sub})"
                markets.append(KalshiMarket(
                    ticker=m["ticker"],
                    title=title,
                    yes_ask=m.get("yes_ask") or 0,
                    no_ask=m.get("no_ask") or 0,
                    yes_bid=m.get("yes_bid") or 0,
                    no_bid=m.get("no_bid") or 0,
                    close_time=_parse_time(m.get("close_time")),
                    volume=m.get("volume", 0),
                    raw=m,
                ))
            cursor = data.get("cursor")
            page += 1
            if log:
                log(f"  Kalshi: page {page}, {len(markets)} markets so far")
            if not cursor:
                break
            if page_pause:
                time.sleep(page_pause)
        return markets

    def get_orderbook(self, ticker: str, depth: int = 10) -> dict:
        return self._request("GET", f"/markets/{ticker}/orderbook",
                             params={"depth": depth})

    # -------------------------------------------------------------- trading

    def get_balance(self) -> int:
        """Available balance in cents."""
        return self._request("GET", "/portfolio/balance", auth=True).get("balance", 0)

    def buy_at_ask(self, ticker: str, side: str, count: int, price_cents: int) -> dict:
        """Place a limit buy at the current ask so it crosses and fills immediately.

        side: "yes" or "no". price_cents: limit price (the ask you observed).
        """
        assert side in ("yes", "no")
        body = {
            "ticker": ticker,
            "client_order_id": str(uuid.uuid4()),
            "action": "buy",
            "side": side,
            "count": count,
            "type": "limit",
            f"{side}_price": price_cents,
        }
        return self._request("POST", "/portfolio/orders", auth=True, json_body=body)


def taker_fee_cents(price_cents: int, count: int = 1) -> int:
    """Kalshi taker fee: ceil(0.07 * count * P * (1-P)) rounded up to the cent."""
    import math
    p = price_cents / 100.0
    fee_dollars = 0.07 * count * p * (1 - p)
    return math.ceil(fee_dollars * 100)
