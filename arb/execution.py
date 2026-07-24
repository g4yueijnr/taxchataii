"""Two-leg execution: buy both sides at the ask immediately.

Order of operations: the Polymarket leg goes first as a fill-or-kill — if it
can't fill at the observed ask it is rejected atomically and we skip the pair.
Only after Polymarket fills do we send the Kalshi limit-at-ask order. That
way a miss leaves you flat, not one-legged (except if Kalshi's ask moved in
the milliseconds between — the Kalshi leg is a resting limit at your price,
so worst case it sits unfilled and you can cancel, it never overpays).
"""

from __future__ import annotations

from dataclasses import dataclass

from .arbitrage import Opportunity
from .kalshi import KalshiClient
from .polymarket import PolymarketClient


@dataclass
class LegResult:
    """Result of firing both legs, independent of the Opportunity object."""
    contracts: int
    poly_order: dict | None = None
    kalshi_order: dict | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class ExecutionResult(LegResult):
    opportunity: Opportunity | None = None


def execute_legs(kalshi: KalshiClient, poly: PolymarketClient, *,
                 kalshi_ticker: str, kalshi_side: str, kalshi_cents: int,
                 poly_token: str, poly_side_label: str, poly_price: float,
                 contracts: int, net_edge: float = 0.0,
                 result: LegResult | None = None, log=print) -> LegResult:
    """Fire both legs from raw primitives. Polymarket FOK first, then Kalshi."""
    result = result or LegResult(contracts=contracts)
    usdc = round(contracts * poly_price, 2)

    log(f"  -> leg 1/2 Polymarket FOK: buy {poly_side_label} "
        f"~{contracts} @ ${poly_price:.2f} (${usdc} USDC)")
    try:
        result.poly_order = poly.buy_at_ask(
            poly_token, usdc_amount=usdc, max_price=poly_price)
    except Exception as exc:  # noqa: BLE001 - report, don't crash the caller
        result.error = f"Polymarket leg failed (nothing bought): {exc}"
        log(f"  !! {result.error}")
        return result

    status = (result.poly_order or {}).get("status", "")
    if status and status.lower() not in ("matched", "live", "success"):
        result.error = f"Polymarket FOK not matched (status={status}); Kalshi leg skipped"
        log(f"  !! {result.error}")
        return result

    log(f"  -> leg 2/2 Kalshi: buy {contracts}x {kalshi_side.upper()} "
        f"{kalshi_ticker} @ {kalshi_cents}c")
    try:
        result.kalshi_order = kalshi.buy_at_ask(
            kalshi_ticker, kalshi_side, contracts, kalshi_cents)
    except Exception as exc:  # noqa: BLE001
        result.error = (
            f"WARNING: Polymarket leg FILLED but Kalshi leg failed: {exc}. "
            f"You hold a one-sided Polymarket position — hedge manually!")
        log(f"  !! {result.error}")
        return result

    log(f"  ✓ both legs sent — locked ~${net_edge * contracts:.2f} "
        f"({net_edge * 100:.1f}c x {contracts})")
    return result


def execute(opp: Opportunity, kalshi: KalshiClient, poly: PolymarketClient,
            contracts: int, log=print) -> ExecutionResult:
    result = ExecutionResult(contracts=contracts, opportunity=opp)
    execute_legs(
        kalshi, poly,
        kalshi_ticker=opp.pair.kalshi.ticker, kalshi_side=opp.kalshi_side,
        kalshi_cents=round(opp.kalshi_price * 100),
        poly_token=opp.poly_token, poly_side_label=opp.poly_side_label,
        poly_price=opp.poly_price, contracts=contracts,
        net_edge=opp.net_edge, result=result, log=log)
    return result
