"""Polymarket market maker.

Quotes both sides of high-volume Polymarket CLOB markets (table tennis /
sports by default — auto-discovered, ranked by volume), capturing the spread.
Unlike Kalshi, Polymarket PAYS makers: a taker-funded maker rebate plus the
platform's liquidity-reward program. That inverts the fee arithmetic that made
Kalshi market-making a losing grind — here the rebate is credited on every
maker fill, on top of the spread.

Paper-trades by default (public CLOB data, no keys needed) until you flip
DRY_RUN=false.
"""

__version__ = "0.2.0"
