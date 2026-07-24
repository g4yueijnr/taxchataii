"""Web app for the Kalshi <-> Polymarket arbitrage finder.

Serves a single-page UI where you paste your keys and scan both venues for
cross-venue arbitrage. Designed to run on Railway (or any host): binds to
$PORT, needs no database, and keeps credentials only in server memory.

Scanning needs NO keys (market data is public). Keys are only used if you
click "Buy both legs" on an opportunity.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from arb.arbitrage import find_opportunities
from arb.execution import execute_legs
from arb.kalshi import KalshiClient
from arb.matching import match_markets
from arb.polymarket import PolymarketClient

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Kalshi ⇄ Polymarket Arbitrage Finder")

def _read_env_pem() -> str:
    """Kalshi private key from either an inline env var or a file path."""
    inline = os.getenv("KALSHI_PRIVATE_KEY", "")
    if inline:
        return inline.replace("\\n", "\n")
    path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
    if path and Path(path).exists():
        return Path(path).read_text()
    return ""


# Credentials live only in process memory. Seeded from env so you can also set
# them as Railway variables instead of typing them into the browser.
_creds_lock = threading.Lock()
CREDS: dict[str, str] = {
    "kalshi_api_key_id": os.getenv("KALSHI_API_KEY_ID", ""),
    "kalshi_private_key": _read_env_pem(),
    "polymarket_private_key": os.getenv("POLYMARKET_PRIVATE_KEY", ""),
    "polymarket_funder": os.getenv("POLYMARKET_FUNDER_ADDRESS", ""),
    "polymarket_sig_type": os.getenv("POLYMARKET_SIGNATURE_TYPE", "1"),
}


def build_clients() -> tuple[KalshiClient, PolymarketClient]:
    with _creds_lock:
        c = dict(CREDS)
    pem = c["kalshi_private_key"].encode() if c["kalshi_private_key"].strip() else None
    kalshi = KalshiClient(
        api_key_id=c["kalshi_api_key_id"].strip() or None,
        private_key_pem=pem)
    try:
        sig = int(c["polymarket_sig_type"] or "1")
    except ValueError:
        sig = 1
    poly = PolymarketClient(
        private_key=c["polymarket_private_key"].strip() or None,
        funder=c["polymarket_funder"].strip() or None,
        signature_type=sig)
    return kalshi, poly


# ------------------------------------------------------------------- schemas

class KeysIn(BaseModel):
    kalshi_api_key_id: str | None = None
    kalshi_private_key: str | None = None
    polymarket_private_key: str | None = None
    polymarket_funder: str | None = None
    polymarket_sig_type: str | None = None


class ScanIn(BaseModel):
    min_edge: float = 0.02
    min_volume: float = 1000
    min_score: float = 90
    max_days_apart: float = 3
    fresh_prices: bool = True


class ExecIn(BaseModel):
    kalshi_ticker: str
    kalshi_side: str          # "yes" | "no"
    kalshi_cents: int
    poly_token: str
    poly_side_label: str      # "YES" | "NO"
    poly_price: float
    net_edge: float = 0.0
    contracts: int = 1


# --------------------------------------------------------------------- routes

@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/status")
def status() -> dict:
    with _creds_lock:
        c = dict(CREDS)
    return {
        "kalshi_key_set": bool(c["kalshi_api_key_id"].strip()),
        "kalshi_pem_set": bool(c["kalshi_private_key"].strip()),
        "poly_key_set": bool(c["polymarket_private_key"].strip()),
        "poly_funder": c["polymarket_funder"],
        "poly_sig_type": c["polymarket_sig_type"],
        "can_trade_kalshi": bool(c["kalshi_api_key_id"].strip()
                                 and c["kalshi_private_key"].strip()),
        "can_trade_poly": bool(c["polymarket_private_key"].strip()),
    }


@app.post("/api/keys")
def set_keys(keys: KeysIn) -> dict:
    with _creds_lock:
        for field, value in keys.model_dump().items():
            if value is not None:
                CREDS[field] = value
    return status()


@app.post("/api/scan")
def scan(params: ScanIn) -> dict:
    kalshi, poly = build_clients()
    errors: list[str] = []

    try:
        k_markets = kalshi.fetch_open_markets(min_volume=int(params.min_volume))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Kalshi fetch failed: {exc}") from exc
    try:
        p_markets = poly.fetch_open_markets(min_volume=params.min_volume)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Polymarket fetch failed: {exc}") from exc

    pairs = match_markets(
        k_markets, p_markets,
        min_score=params.min_score, max_days_apart=params.max_days_apart)

    if params.fresh_prices and pairs:
        try:
            poly.refresh_asks([p.poly for p in pairs])
        except Exception as exc:  # noqa: BLE001 - non-fatal, use Gamma asks
            errors.append(f"Could not refresh live Polymarket asks: {exc}")

    opps = find_opportunities(pairs, min_edge=params.min_edge)

    return {
        "kalshi_markets": len(k_markets),
        "poly_markets": len(p_markets),
        "matched_pairs": len(pairs),
        "errors": errors,
        "opportunities": [{
            "kalshi_ticker": o.pair.kalshi.ticker,
            "kalshi_title": o.pair.kalshi.title,
            "kalshi_url": o.pair.kalshi.url,
            "poly_question": o.pair.poly.question,
            "poly_url": o.pair.poly.url,
            "poly_token": o.poly_token,
            "match_score": round(o.pair.score, 1),
            "confirmed": o.pair.confirmed,
            "direction": o.direction,
            "kalshi_side": o.kalshi_side,
            "kalshi_price": round(o.kalshi_price, 2),
            "kalshi_cents": round(o.kalshi_price * 100),
            "poly_side": o.poly_side_label,
            "poly_price": round(o.poly_price, 3),
            "gross_cost": round(o.gross_cost, 3),
            "kalshi_fee": round(o.kalshi_fee, 3),
            "net_edge": round(o.net_edge, 3),
            "net_edge_cents": round(o.net_edge * 100, 1),
        } for o in opps],
    }


@app.post("/api/execute")
def execute_endpoint(order: ExecIn) -> dict:
    kalshi, poly = build_clients()
    if not kalshi.can_trade:
        raise HTTPException(400, "Kalshi keys missing — set them before trading.")
    if not poly.can_trade:
        raise HTTPException(400, "Polymarket key missing — set it before trading.")
    if order.contracts < 1:
        raise HTTPException(400, "contracts must be >= 1")
    if order.kalshi_side not in ("yes", "no"):
        raise HTTPException(400, "kalshi_side must be 'yes' or 'no'")

    logs: list[str] = []
    res = execute_legs(
        kalshi, poly,
        kalshi_ticker=order.kalshi_ticker, kalshi_side=order.kalshi_side,
        kalshi_cents=order.kalshi_cents, poly_token=order.poly_token,
        poly_side_label=order.poly_side_label, poly_price=order.poly_price,
        contracts=order.contracts, net_edge=order.net_edge,
        log=logs.append)
    return {
        "ok": res.ok,
        "error": res.error,
        "logs": logs,
        "poly_order": res.poly_order,
        "kalshi_order": res.kalshi_order,
    }


@app.get("/health")
def health() -> dict:
    return {"ok": True}
