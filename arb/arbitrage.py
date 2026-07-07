"""Arbitrage math.

A cross-venue arb exists when buying YES on one venue and NO on the other
costs less than $1.00 combined (one of the two legs must pay out $1).
Two directions per matched pair:

  A: Kalshi YES ask + Polymarket NO ask < 1.00
  B: Kalshi NO ask + Polymarket YES ask < 1.00

Edge is reported after Kalshi taker fees (Polymarket charges no trading fee
on standard markets).
"""

from __future__ import annotations

from dataclasses import dataclass

from .kalshi import taker_fee_cents
from .matching import MatchedPair


@dataclass
class Opportunity:
    pair: MatchedPair
    direction: str            # "kalshi_yes_poly_no" or "kalshi_no_poly_yes"
    kalshi_side: str          # "yes" | "no"
    kalshi_price: float       # dollars
    poly_token: str
    poly_side_label: str      # "NO" | "YES"
    poly_price: float         # dollars
    gross_cost: float         # dollars per contract pair, before fees
    kalshi_fee: float         # dollars per contract
    net_edge: float           # 1.00 - gross_cost - fees

    def describe(self) -> str:
        k = self.pair.kalshi
        p = self.pair.poly
        return (
            f"{self.pair.label}\n"
            f"  BUY Kalshi {self.kalshi_side.upper()} @ ${self.kalshi_price:.2f}  "
            f"({k.url})\n"
            f"  BUY Poly   {self.poly_side_label} @ ${self.poly_price:.2f}  "
            f"({p.url})\n"
            f"  cost ${self.gross_cost:.3f} + fee ${self.kalshi_fee:.3f}"
            f"  =>  net edge ${self.net_edge:.3f}/contract"
            f"  ({self.net_edge * 100:.1f}c)"
        )


def find_opportunities(pairs: list[MatchedPair],
                       min_edge: float = 0.02) -> list[Opportunity]:
    """min_edge in dollars: 0.05 means combined cost under ~95c after fees."""
    opps: list[Opportunity] = []
    for pair in pairs:
        k, p = pair.kalshi, pair.poly

        combos = []
        if k.yes_ask and p.no_ask is not None and p.no_ask > 0:
            combos.append(("kalshi_yes_poly_no", "yes", k.yes_ask,
                           p.no_token, "NO", p.no_ask))
        if k.no_ask and p.yes_ask is not None and p.yes_ask > 0:
            combos.append(("kalshi_no_poly_yes", "no", k.no_ask,
                           p.yes_token, "YES", p.yes_ask))

        for direction, k_side, k_cents, token, p_label, p_price in combos:
            k_price = k_cents / 100.0
            if k_price >= 1 or p_price >= 1:
                continue
            gross = k_price + p_price
            fee = taker_fee_cents(k_cents) / 100.0
            edge = 1.0 - gross - fee
            if edge >= min_edge:
                opps.append(Opportunity(
                    pair=pair, direction=direction,
                    kalshi_side=k_side, kalshi_price=k_price,
                    poly_token=token, poly_side_label=p_label,
                    poly_price=p_price,
                    gross_cost=gross, kalshi_fee=fee, net_edge=edge))
    opps.sort(key=lambda o: o.net_edge, reverse=True)
    return opps
