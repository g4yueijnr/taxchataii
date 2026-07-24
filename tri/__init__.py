"""Single-exchange triangular arbitrage bot.

Monitors triangles of trading pairs on one high-liquidity venue (Binance by
default) over a websocket best-bid/offer feed, computes the fee-adjusted
return of walking each 3-leg cycle in both directions hundreds of times a
second, and (in dry-run) paper-executes the cycles whose net edge clears
fees + a safety buffer.

This is the retail-viable HFT strategy: one venue (no cross-exchange latency
race), opportunities that last seconds (catchable), and an edge that lives
or dies on your fee tier — which the dashboard makes explicit.
"""

__version__ = "0.1.0"
