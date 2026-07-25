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
    # Queue AHEAD of us at our price (others' resting orders that were there
    # first). We only fill after this is consumed by trades — the realistic
    # "we're one trader in line" model. Only counts when we sit AT the touch;
    # if we improve to a new best price, we're first in line (queue 0).
    bid_queue: float = 0.0
    ask_queue: float = 0.0


class PaperOrderManager:
    def __init__(self, cfg, positions: PaperBook):
        self.cfg = cfg
        self.positions = positions
        self.resting: dict[str, Resting] = {}

    def set_quote(self, token: str, q: DesiredQuote | None, book=None) -> None:
        if q is None:
            self.resting.pop(token, None)
            return
        prev = self.resting.get(token)
        # Queue ahead = size already resting at our price (all of it is other
        # people, since our paper order isn't really in the book). If we're
        # improving to a price with nothing there, we're first in line.
        bid_q = float(book.bids.get(q.bid, 0.0)) if book else 0.0
        ask_q = float(book.asks.get(q.ask, 0.0)) if book else 0.0
        # Keep our earned queue progress if the price didn't move.
        if prev and prev.bid == q.bid:
            bid_q = prev.bid_queue
        if prev and prev.ask == q.ask:
            ask_q = prev.ask_queue
        self.resting[token] = Resting(q.bid, q.ask, q.bid_size, q.ask_size,
                                      bid_q, ask_q)

    def on_trade(self, token: str, price_cents: int, size: float) -> bool:
        """A public trade at price_cents. Consume the queue ahead of us first;
        only the remainder fills our order. Returns True if we filled."""
        r = self.resting.get(token)
        if not r or not (1 <= price_cents <= 99) or size <= 0:
            return False
        # Sell swept to/through our bid -> hits the bid queue, then us.
        if r.bid_size > 0 and price_cents <= r.bid:
            if r.bid_queue >= size:
                r.bid_queue -= size          # all went to traders ahead of us
                return False
            to_us = size - r.bid_queue
            r.bid_queue = 0.0
            fill = min(r.bid_size, to_us)
            self.positions.fill(token, "buy", r.bid, fill)
            r.bid_size -= fill
            return True
        # Buy lifted to/through our ask -> hits the ask queue, then us.
        if r.ask_size > 0 and price_cents >= r.ask:
            if r.ask_queue >= size:
                r.ask_queue -= size
                return False
            to_us = size - r.ask_queue
            r.ask_queue = 0.0
            fill = min(r.ask_size, to_us)
            self.positions.fill(token, "sell", r.ask, fill)
            r.ask_size -= fill
            return True
        return False
