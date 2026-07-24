"""Configuration for the triangular arbitrage bot."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    v = os.environ.get(name)
    return float(v) if v not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    return int(v) if v not in (None, "") else default


@dataclass
class Pair:
    """A spot trading pair and its base/quote assets."""
    symbol: str   # exchange symbol, e.g. "BTCUSDT"
    base: str     # "BTC"
    quote: str    # "USDT"


@dataclass
class Triangle:
    """A 3-leg cycle that starts and ends in `start`, walking `legs` in order.

    The reverse direction is evaluated automatically (legs reversed), so one
    Triangle covers both ways around the loop.
    """
    name: str
    start: str
    legs: list[str]   # ordered pair symbols forming the cycle


# Default universe: liquid Binance triangles anchored on USDT, bridged by BTC
# / ETH / BNB. Every symbol here is a real, deep Binance spot pair.
DEFAULT_PAIRS: dict[str, Pair] = {
    "BTCUSDT": Pair("BTCUSDT", "BTC", "USDT"),
    "ETHUSDT": Pair("ETHUSDT", "ETH", "USDT"),
    "BNBUSDT": Pair("BNBUSDT", "BNB", "USDT"),
    "SOLUSDT": Pair("SOLUSDT", "SOL", "USDT"),
    "XRPUSDT": Pair("XRPUSDT", "XRP", "USDT"),
    "ETHBTC": Pair("ETHBTC", "ETH", "BTC"),
    "BNBBTC": Pair("BNBBTC", "BNB", "BTC"),
    "SOLBTC": Pair("SOLBTC", "SOL", "BTC"),
    "XRPBTC": Pair("XRPBTC", "XRP", "BTC"),
    "BNBETH": Pair("BNBETH", "BNB", "ETH"),
    "SOLETH": Pair("SOLETH", "SOL", "ETH"),
}

DEFAULT_TRIANGLES: list[Triangle] = [
    Triangle("USDT-BTC-ETH", "USDT", ["BTCUSDT", "ETHBTC", "ETHUSDT"]),
    Triangle("USDT-BTC-BNB", "USDT", ["BTCUSDT", "BNBBTC", "BNBUSDT"]),
    Triangle("USDT-BTC-SOL", "USDT", ["BTCUSDT", "SOLBTC", "SOLUSDT"]),
    Triangle("USDT-BTC-XRP", "USDT", ["BTCUSDT", "XRPBTC", "XRPUSDT"]),
    Triangle("USDT-ETH-BNB", "USDT", ["ETHUSDT", "BNBETH", "BNBUSDT"]),
    Triangle("USDT-ETH-SOL", "USDT", ["ETHUSDT", "SOLETH", "SOLUSDT"]),
]


@dataclass
class Config:
    dry_run: bool = True
    sim_bankroll: float = 100.0        # paper USDT bankroll
    trade_notional: float = 50.0       # USDT deployed per cycle attempt

    pairs: dict[str, Pair] = field(default_factory=lambda: dict(DEFAULT_PAIRS))
    triangles: list[Triangle] = field(default_factory=lambda: list(DEFAULT_TRIANGLES))

    # Fees & thresholds -- the whole game.
    fee_rate: float = 0.001            # taker fee per leg (0.1% retail default)
    min_net_edge: float = 0.0005       # required net edge after fees (0.05%)
    max_book_age_s: float = 1.0        # skip a triangle if any leg is stale
    max_notional_frac: float = 0.5     # cap deployed vs available top-of-book

    # Live-trading credentials (only used when dry_run is False).
    binance_api_key: str = ""
    binance_api_secret: str = ""

    port: int = 8080
    data_dir: str = "data"

    @property
    def symbols(self) -> list[str]:
        syms: set[str] = set()
        for t in self.triangles:
            syms.update(t.legs)
        return sorted(syms)


def load_config() -> Config:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    cfg = Config()
    cfg.dry_run = _env_bool("DRY_RUN", cfg.dry_run)
    cfg.sim_bankroll = _env_float("TRI_SIM_BANKROLL", cfg.sim_bankroll)
    cfg.trade_notional = _env_float("TRI_TRADE_NOTIONAL", cfg.trade_notional)
    cfg.fee_rate = _env_float("TRI_FEE_RATE", cfg.fee_rate)
    cfg.min_net_edge = _env_float("TRI_MIN_NET_EDGE", cfg.min_net_edge)
    cfg.binance_api_key = os.environ.get("BINANCE_API_KEY", "")
    cfg.binance_api_secret = os.environ.get("BINANCE_API_SECRET", "")
    cfg.data_dir = os.environ.get("TRI_DATA_DIR", cfg.data_dir)
    cfg.port = _env_int("PORT", cfg.port)
    return cfg
