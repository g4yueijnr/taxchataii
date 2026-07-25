"""Paper accounting for cross-venue arbitrage.

The economics of a matched pair: you buy YES on one venue and NO on the other
for a combined cost of ``gross_cost`` dollars per contract pair. At settlement
exactly ONE leg pays $1, so the payout is $1/pair regardless of outcome. The
profit ``net_edge = 1 - gross_cost - kalshi_fee`` is therefore LOCKED the
instant both legs fill -- there is no market risk (the only risk is the two
markets settling differently, which is why fuzzy matches are flagged).

So we book the edge to realized P&L at fill time. Capital, though, is tied up
until each market resolves (days), which we model conservatively: cost stays
`deployed` for the life of the session, so a $100 account can only hold ~$100
of positions at once -- it fills a handful of arbs and then waits. That's the
honest shape of hold-to-settlement arb on a tiny bankroll.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .arbitrage import Opportunity


@dataclass
class ArbFill:
    ts: float
    label: str
    ticker: str
    direction: str
    kalshi_side: str
    kalshi_price: float
    poly_side: str
    poly_price: float
    contracts: int
    edge: float           # net edge per contract (dollars)
    profit: float         # edge * contracts (dollars, locked)
    confirmed: bool       # from a hand-verified pair


@dataclass
class PaperArbBook:
    bankroll: float = 100.0
    locked_profit: float = 0.0     # realized (riskless) edge, dollars
    deployed: float = 0.0          # capital tied up in open positions
    confirmed_profit: float = 0.0  # subset from verified pairs
    fills: list[ArbFill] = field(default_factory=list)
    taken: set[str] = field(default_factory=set)  # dedupe: ticker:direction

    @property
    def equity(self) -> float:
        return self.bankroll + self.locked_profit

    @property
    def available(self) -> float:
        return self.bankroll - self.deployed

    @staticmethod
    def _key(opp: Opportunity) -> str:
        return f"{opp.pair.kalshi.ticker}:{opp.direction}"

    def already_took(self, opp: Opportunity) -> bool:
        return self._key(opp) in self.taken

    def execute(self, opp: Opportunity, max_contracts: int) -> ArbFill | None:
        """Book a paper arb. Sizes down to what the paper bankroll can fund.
        An opportunity is taken at most once (it vanishes once you hit it)."""
        key = self._key(opp)
        if key in self.taken:
            return None
        cost_per = opp.gross_cost + opp.kalshi_fee   # dollars per pair
        if cost_per <= 0 or self.available < cost_per:
            return None
        contracts = min(max_contracts, int(self.available // cost_per))
        if contracts <= 0:
            return None
        profit = opp.net_edge * contracts
        self.deployed += cost_per * contracts
        self.locked_profit += profit
        if opp.pair.confirmed:
            self.confirmed_profit += profit
        self.taken.add(key)
        fill = ArbFill(
            ts=time.time(), label=opp.pair.label,
            ticker=opp.pair.kalshi.ticker, direction=opp.direction,
            kalshi_side=opp.kalshi_side, kalshi_price=opp.kalshi_price,
            poly_side=opp.poly_side_label, poly_price=opp.poly_price,
            contracts=contracts, edge=opp.net_edge, profit=profit,
            confirmed=opp.pair.confirmed)
        self.fills.append(fill)
        return fill
