"""Kalshi 15-minute crypto market maker.

Quotes both sides of Kalshi's 15-minute up/down crypto markets (DOGE, BNB,
SOL, XRP, ... — auto-discovered) around a fair value derived from live spot
exchange feeds, capturing the bid/ask spread while defending against the
adverse selection that kills naive spread bots.
"""

__version__ = "0.6.0"
