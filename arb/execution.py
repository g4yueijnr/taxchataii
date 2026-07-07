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
class ExecutionResult:
    opportunity: Opportunity
    contracts: int
    poly_order: dict | None = None
    kalshi_order: dict | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def execute(opp: Opportunity, kalshi: KalshiClient, poly: PolymarketClient,
            contracts: int, log=print) -> ExecutionResult:
    result = ExecutionResult(opportunity=opp, contracts=contracts)
    usdc = round(contracts * opp.poly_price, 2)
    k_cents = round(opp.kalshi_price * 100)

    log(f"  -> leg 1/2 Polymarket FOK: buy {opp.poly_side_label} "
        f"~{contracts} @ ${opp.poly_price:.2f} (${usdc} USDC)")
    try:
        result.poly_order = poly.buy_at_ask(
            opp.poly_token, usdc_amount=usdc, max_price=opp.poly_price)
    except Exception as exc:  # noqa: BLE001 - report, don't crash the scan loop
        result.error = f"Polymarket leg failed (nothing bought): {exc}"
        log(f"  !! {result.error}")
        return result

    status = (result.poly_order or {}).get("status", "")
    if status and status.lower() not in ("matched", "live", "success"):
        result.error = f"Polymarket FOK not matched (status={status}); Kalshi leg skipped"
        log(f"  !! {result.error}")
        return result

    log(f"  -> leg 2/2 Kalshi: buy {contracts}x {opp.kalshi_side.upper()} "
        f"{opp.pair.kalshi.ticker} @ {k_cents}c")
    try:
        result.kalshi_order = kalshi.buy_at_ask(
            opp.pair.kalshi.ticker, opp.kalshi_side, contracts, k_cents)
    except Exception as exc:  # noqa: BLE001
        result.error = (
            f"WARNING: Polymarket leg FILLED but Kalshi leg failed: {exc}. "
            f"You hold a one-sided Polymarket position — hedge manually!")
        log(f"  !! {result.error}")
        return result

    log(f"  ✓ both legs sent — locked ~${opp.net_edge * contracts:.2f} "
        f"({opp.net_edge * 100:.1f}c x {contracts})")
    return result
