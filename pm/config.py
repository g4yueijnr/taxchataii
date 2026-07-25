"""Configuration for the Polymarket market maker."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _b(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def _f(name: str, default: float) -> float:
    v = os.environ.get(name)
    return float(v) if v not in (None, "") else default


def _i(name: str, default: int) -> int:
    v = os.environ.get(name)
    return int(v) if v not in (None, "") else default


# Polymarket CLOB / Gamma endpoints. Overridable via env (PM_GAMMA_BASE,
# PM_CLOB_BASE, PM_CLOB_WS, PM_DATA_BASE) to point at the US exchange hosts.
GAMMA_BASE = os.environ.get("PM_GAMMA_BASE", "https://gamma-api.polymarket.com")
CLOB_BASE = os.environ.get("PM_CLOB_BASE", "https://clob.polymarket.com")
CLOB_WS = os.environ.get(
    "PM_CLOB_WS", "wss://ws-subscriptions-clob.polymarket.com/ws/market")
DATA_BASE = os.environ.get("PM_DATA_BASE", "https://data-api.polymarket.com")


@dataclass
class Config:
    dry_run: bool = True
    sim_bankroll: float = 100.0        # paper USDC bankroll

    # --- what to trade ---------------------------------------------------
    # Category tag to filter on (Gamma), e.g. "table-tennis", "sports", or
    # "" for ALL markets ranked purely by volume (default: max volume).
    category: str = ""
    max_markets: int = 15              # quote the top-N by RECENT (24h) volume
    min_volume: float = 5000.0         # min 24h volume ($) — actively trading
    # Only make markets that aren't pinned at the extremes (dead longshots
    # like "Jesus returns" sit at 1-2c with no real two-sided market).
    min_mid_cents: int = 8
    max_mid_cents: int = 92
    explicit_slugs: list[str] = field(default_factory=list)  # override discovery

    # --- quoting (prices are dollars 0-1; 1 tick = 1c) -------------------
    tick: float = 0.01
    quote_size: float = 20.0           # shares per side
    max_position: float = 100.0        # max net shares per market
    min_book_spread_ticks: int = 2     # only make markets at least this wide
    inventory_skew_ticks: float = 3.0  # skew at full inventory
    join_inside: bool = True           # rest 1 tick inside the touch

    # --- economics -------------------------------------------------------
    # Makers pay ZERO fee on Polymarket (the whole reason it beats Kalshi for
    # market-making). A maker REBATE is unconfirmed on the US exchange, so it
    # defaults to 0 -- set PM_MAKER_REBATE_MULT>0 only if you confirm one.
    # Edge here is spread - adverse selection, with no fee drag.
    maker_rebate_mult: float = 0.0
    taker_fee_mult: float = 0.0

    # --- risk ------------------------------------------------------------
    max_gross_dollars: float = 100.0
    daily_loss_limit_dollars: float = 50.0

    # --- credentials (live only) ----------------------------------------
    poly_private_key: str = ""
    poly_funder: str = ""
    poly_signature_type: int = 0

    # --- plumbing --------------------------------------------------------
    discovery_interval: float = 60.0
    book_poll_interval: float = 2.0    # REST book refresh (WS is primary)
    port: int = 8080
    data_dir: str = "data"


def load_config() -> Config:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    c = Config()
    c.dry_run = _b("DRY_RUN", c.dry_run)
    c.sim_bankroll = _f("PM_SIM_BANKROLL", c.sim_bankroll)
    c.category = os.environ.get("PM_CATEGORY", c.category)
    c.max_markets = _i("PM_MAX_MARKETS", c.max_markets)
    c.min_volume = _f("PM_MIN_VOLUME", c.min_volume)
    c.min_mid_cents = _i("PM_MIN_MID_CENTS", c.min_mid_cents)
    c.max_mid_cents = _i("PM_MAX_MID_CENTS", c.max_mid_cents)
    slugs = os.environ.get("PM_SLUGS", "")
    if slugs:
        c.explicit_slugs = [s.strip() for s in slugs.split(",") if s.strip()]
    c.quote_size = _f("PM_QUOTE_SIZE", c.quote_size)
    c.max_position = _f("PM_MAX_POSITION", c.max_position)
    c.min_book_spread_ticks = _i("PM_MIN_SPREAD_TICKS", c.min_book_spread_ticks)
    c.maker_rebate_mult = _f("PM_MAKER_REBATE_MULT", c.maker_rebate_mult)
    c.taker_fee_mult = _f("PM_TAKER_FEE_MULT", c.taker_fee_mult)
    c.poly_private_key = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    c.poly_funder = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "")
    c.poly_signature_type = _i("POLYMARKET_SIGNATURE_TYPE", c.poly_signature_type)
    c.data_dir = os.environ.get("PM_DATA_DIR", c.data_dir)
    c.port = _i("PORT", c.port)
    return c
