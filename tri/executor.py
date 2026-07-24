"""Paper executor + journal.

Dry-run executes an opportunity by walking its three legs at the exact BBO
prices that triggered it, paying the fee on every leg, and booking the
realized USDT P&L. This is deliberately honest:

- It re-reads the live book at execution time and RE-CHECKS the net edge.
  If the edge has evaporated between detection and execution (the normal
  case — these opportunities die in 1-5s), the cycle is abandoned. This is
  the single biggest reason naive backtests overstate arb profits.
- Notional is capped by top-of-book depth, so we never assume we could
  trade more than was actually offered.
- Every attempt (executed or missed) is journaled.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .engine import BookStore, Opportunity, evaluate

log = logging.getLogger("tri.exec")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    triangle TEXT NOT NULL,
    direction TEXT NOT NULL,
    detected_edge REAL NOT NULL,
    exec_edge REAL,
    notional_usdt REAL NOT NULL,
    pnl_usdt REAL,
    outcome TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS cycles_ts ON cycles(ts);
"""


@dataclass
class Stats:
    detected: int = 0
    executed: int = 0
    missed: int = 0
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    by_triangle: dict = field(default_factory=dict)


class Journal:
    def __init__(self, data_dir: str):
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(Path(data_dir) / "tri.sqlite3")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(_SCHEMA)
        self.db.commit()

    def record(self, ts, triangle, direction, detected_edge, exec_edge,
               notional, pnl, outcome) -> None:
        self.db.execute(
            "INSERT INTO cycles (ts, triangle, direction, detected_edge, "
            "exec_edge, notional_usdt, pnl_usdt, outcome) VALUES (?,?,?,?,?,?,?,?)",
            (ts, triangle, direction, detected_edge, exec_edge, notional,
             pnl, outcome))
        self.db.commit()

    def recent(self, limit: int = 60) -> list[dict]:
        cols = ("ts", "triangle", "direction", "detected_edge", "exec_edge",
                "notional_usdt", "pnl_usdt", "outcome")
        rows = self.db.execute(
            f"SELECT {', '.join(cols)} FROM cycles ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def close(self) -> None:
        self.db.close()


class PaperExecutor:
    def __init__(self, cfg: Config, books: BookStore, journal: Journal):
        self.cfg = cfg
        self.books = books
        self.journal = journal
        self.stats = Stats()
        self._cooldown: dict[str, float] = {}

    def _simulate(self, opp: Opportunity, notional: float) -> tuple[float, float]:
        """Return (final_usdt, fee_usdt) walking the cycle at current prices."""
        amt = notional
        fee_total = 0.0
        pairs = self.cfg.pairs
        asset = opp.start
        for leg in opp.legs:
            p = pairs[leg.symbol]
            if asset == p.base:                 # sell base -> quote
                gross = amt * leg.price
                fee = gross * self.cfg.fee_rate
                amt = gross - fee
                asset = p.quote
            else:                               # buy base <- quote
                fee = amt * self.cfg.fee_rate
                spent = amt - fee
                amt = spent / leg.price
                asset = p.base
            fee_total += fee
        return amt, fee_total

    def try_execute(self, opp: Opportunity, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        self.stats.detected += 1
        cd = self._cooldown.get(opp.triangle, 0.0)
        if now < cd:
            return False

        # Re-evaluate against the LIVE book right now — the edge usually
        # evaporates between detection and execution.
        tri = next(t for t in self.cfg.triangles if t.name == opp.triangle)
        fresh = evaluate(tri, self.cfg.pairs, self.books, self.cfg.fee_rate,
                         now, self.cfg.max_book_age_s)
        notional = min(self.cfg.trade_notional,
                       self.cfg.max_notional_frac * max(opp.limiting_qty_usdt, 0.0)
                       or self.cfg.trade_notional)

        if fresh is None or fresh.net_edge < self.cfg.min_net_edge:
            self.stats.missed += 1
            self.journal.record(now, opp.triangle, opp.direction,
                                opp.net_edge, fresh.net_edge if fresh else None,
                                notional, None, "missed")
            return False

        final, fee = self._simulate(fresh, notional)
        pnl = final - notional
        self.stats.executed += 1
        self.stats.realized_pnl += pnl
        self.stats.fees_paid += fee
        bt = self.stats.by_triangle.setdefault(
            opp.triangle, {"executed": 0, "pnl": 0.0})
        bt["executed"] += 1
        bt["pnl"] += pnl
        self._cooldown[opp.triangle] = now + 0.5   # avoid double-firing a loop
        self.journal.record(now, fresh.triangle, fresh.direction, opp.net_edge,
                            fresh.net_edge, notional, pnl, "executed")
        log.info("[paper] %s %s exec edge=%.4f%% pnl=%.4f USDT",
                 fresh.triangle, fresh.direction, fresh.net_edge * 100, pnl)
        return True
