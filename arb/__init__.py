"""Kalshi <-> Polymarket cross-venue arbitrage finder.

Two entry points:
  * `python -m arb`         — one-shot / looping CLI scanner (+ live --execute)
  * `python -m arb.daemon`  — always-on paper daemon with a live dashboard
                              (the Railway deployment target)
"""

__version__ = "0.2.0"
