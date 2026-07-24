"""Paper order manager (resting quotes + fills from the trade tape) + journal."""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .engine import DesiredQuote, PaperBook

log = logging.getLogger("pm.exec")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY, ts REAL, token TEXT, question TEXT,
    side TEXT, price_cents INTEGER, size REAL, rebate_cents REAL,
    realized_cents REAL
);
"""


class Journal:
    def __init__(self, data_dir: str):
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(Path(data_dir) / "pm.sqlite3")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(_SCHEMA)
        self.db.commit()

    def record(self, token, question, side, price, size, rebate, realized):
        self.db.execute(
            "INSERT INTO fills (ts, token, question, side, price_cents, size, "
            "rebate_cents, realized_cents) VALUES (?,?,?,?,?,?,?,?)",
            (time.time(), token, question, side, price, size, rebate, realized))
        self.db.commit()

    def recent(self, limit=60):
        cols = ("ts", "question", "side", "price_cents", "size",
                "rebate_cents", "realized_cents")
        rows = self.db.execute(
            f"SELECT {', '.join(cols)} FROM fills ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def close(self):
        self.db.close()


@dataclass
class Resting:
    bid: int
    ask: int
    bid_size: float
    ask_size: float


class PaperOrderManager:
    def __init__(self, cfg, positions: PaperBook):
        self.cfg = cfg
        self.positions = positions
        self.resting: dict[str, Resting] = {}

    def set_quote(self, token: str, q: DesiredQuote | None) -> None:
        if q is None:
            self.resting.pop(token, None)
        else:
            self.resting[token] = Resting(q.bid, q.ask, q.bid_size, q.ask_size)

    def on_trade(self, token: str, price_cents: int, size: float) -> bool:
        """A public trade at price_cents. Fill our resting quote if crossed.
        Returns True if we filled."""
        r = self.resting.get(token)
        if not r or not (1 <= price_cents <= 99):
            return False
        # Sell swept down to/through our bid -> we buy at our bid.
        if r.bid_size > 0 and price_cents <= r.bid:
            fill = min(r.bid_size, size) if size > 0 else r.bid_size
            self.positions.fill(token, "buy", r.bid, fill)
            r.bid_size -= fill
            return True
        # Buy lifted up to/through our ask -> we sell at our ask.
        if r.ask_size > 0 and price_cents >= r.ask:
            fill = min(r.ask_size, size) if size > 0 else r.ask_size
            self.positions.fill(token, "sell", r.ask, fill)
            r.ask_size -= fill
            return True
        return False
