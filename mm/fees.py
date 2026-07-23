"""Kalshi fee math.

Fee per order = ceil_to_cent(mult * count * P * (1-P)), P in dollars.
Taker mult is 0.07 on general markets; maker fees (charged when a resting
order fills) are 25% of taker. Multipliers are configurable because Kalshi
revises the schedule — verify yours at kalshi.com/fee-schedule.
"""

from __future__ import annotations

import math


def fee_cents(price_cents: int, count: int, mult: float) -> int:
    p = price_cents / 100.0
    # Epsilon guards float noise (0.07*100*0.25*100 -> 175.00000000000003).
    return math.ceil(mult * count * p * (1.0 - p) * 100.0 - 1e-9)


def maker_fee_cents(price_cents: int, count: int, mult: float = 0.0175) -> int:
    return fee_cents(price_cents, count, mult)


def taker_fee_cents(price_cents: int, count: int, mult: float = 0.07) -> int:
    return fee_cents(price_cents, count, mult)


def maker_fee_per_contract(price_cents: int, mult: float = 0.0175) -> float:
    """Un-rounded per-contract maker fee in cents, for edge math."""
    p = price_cents / 100.0
    return mult * p * (1.0 - p) * 100.0


def round_trip_maker_cost(bid_cents: int, ask_cents: int, mult: float = 0.0175) -> float:
    """Fees in cents to buy 1 at bid and sell 1 at ask, both as maker."""
    return maker_fee_per_contract(bid_cents, mult) + maker_fee_per_contract(ask_cents, mult)
