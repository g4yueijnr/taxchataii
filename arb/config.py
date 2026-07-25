"""Config for the continuous cross-venue arbitrage paper daemon.

All knobs are env-driven (Railway Variables). Paper by default: it detects
and books opportunities against a simulated bankroll, no keys required, no
real orders. Flip nothing to go live from here — live execution stays in the
`python -m arb scan --execute` CLI, which needs credentials.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _b(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def _f(name: str, default: float) -> float:
    v = os.environ.get(name)
    return float(v) if v not in (None, "") else default


def _i(name: str, default: int) -> int:
    v = os.environ.get(name)
    return int(v) if v not in (None, "") else default


@dataclass
class ArbConfig:
    # --- paper account ---------------------------------------------------
    bankroll: float = 100.0            # simulated USD

    # --- opportunity filters --------------------------------------------
    # Net edge in DOLLARS after Kalshi fees. 0.02 => combined cost of the two
    # legs is under ~98c, so each hedged pair locks >=2c toward the $1 payout.
    min_edge: float = 0.02
    min_score: float = 92.0            # fuzzy title-match cutoff (0-100)
    max_days_apart: float = 3.0        # close-date agreement guard
    min_volume: float = 1000.0         # ignore thin markets on both venues
    # How many pages to pull per venue per scan. Anonymous Kalshi has a tiny
    # rate limit, so we cap paging (and back off on 429). Add Kalshi API keys
    # to jump to the authenticated tier, then raise this for full coverage.
    kalshi_max_pages: int = 10
    poly_max_pages: int = 10

    # --- sizing ----------------------------------------------------------
    size: int = 20                     # contracts per opportunity (capped by
    #                                    available paper capital)

    # --- matching trust --------------------------------------------------
    # Fuzzy title matches can pair markets with subtly different resolution
    # rules -- the "arb" is only real if both settle identically. In PAPER we
    # book them so you can see the flow, but they're flagged UNVERIFIED and
    # tallied separately from confirmed pairs. Live execution (the CLI) never
    # trades a fuzzy pair unless you pass --allow-fuzzy-exec.
    book_fuzzy: bool = True
    matches_file: str = "matches.yaml"  # hand-confirmed {ticker: cond/slug}

    # --- plumbing --------------------------------------------------------
    scan_interval: float = 45.0        # seconds between full re-scans
    fresh_prices: bool = True          # re-pull live asks from the CLOB
    port: int = 8080


def load_config() -> ArbConfig:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    c = ArbConfig()
    c.bankroll = _f("ARB_BANKROLL", c.bankroll)
    c.min_edge = _f("ARB_MIN_EDGE", c.min_edge)
    c.min_score = _f("ARB_MIN_SCORE", c.min_score)
    c.max_days_apart = _f("ARB_MAX_DAYS", c.max_days_apart)
    c.min_volume = _f("ARB_MIN_VOLUME", c.min_volume)
    c.kalshi_max_pages = _i("ARB_KALSHI_MAX_PAGES", c.kalshi_max_pages)
    c.poly_max_pages = _i("ARB_POLY_MAX_PAGES", c.poly_max_pages)
    c.size = _i("ARB_SIZE", c.size)
    c.book_fuzzy = _b("ARB_BOOK_FUZZY", c.book_fuzzy)
    c.matches_file = os.environ.get("ARB_MATCHES_FILE", c.matches_file)
    c.scan_interval = _f("ARB_SCAN_INTERVAL", c.scan_interval)
    c.fresh_prices = _b("ARB_FRESH_PRICES", c.fresh_prices)
    c.port = _i("PORT", c.port)
    return c
