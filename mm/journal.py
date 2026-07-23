"""SQLite trade journal + markout analytics.

Every fill is recorded with the model fair value at fill time; ~30s later
the markout (how far fair moved for/against us since the fill, signed by
our direction) is written back. Positive average markout = we're picking
up good fills; persistently negative = we're the ones getting run over and
the edge parameters need widening. Per-coin aggregates feed /stats and the
per-coin circuit breaker.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

MARKOUT_HORIZON_S = 30.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    coin TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    action TEXT NOT NULL,
    count INTEGER NOT NULL,
    price_cents INTEGER NOT NULL,
    yes_equiv_qty INTEGER NOT NULL,
    fee_cents REAL NOT NULL,
    is_taker INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    fair_at_fill REAL,
    markout_cents REAL
);
CREATE INDEX IF NOT EXISTS fills_coin_ts ON fills(coin, ts);
"""


class Journal:
    def __init__(self, data_dir: str):
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(Path(data_dir) / "mm.sqlite3")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(_SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def record_fill(self, coin: str, ticker: str, side: str, action: str,
                    count: int, price_cents: int, yes_equiv_qty: int,
                    fee_cents: float, is_taker: bool, fair_at_fill: float | None,
                    reason: str = "") -> int:
        cur = self.db.execute(
            "INSERT INTO fills (ts, coin, ticker, side, action, count, "
            "price_cents, yes_equiv_qty, fee_cents, is_taker, reason, fair_at_fill) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), coin, ticker, side, action, count, price_cents,
             yes_equiv_qty, fee_cents, int(is_taker), reason, fair_at_fill))
        self.db.commit()
        return int(cur.lastrowid)

    def set_markout(self, fill_id: int, markout_cents: float) -> None:
        self.db.execute("UPDATE fills SET markout_cents=? WHERE id=?",
                        (markout_cents, fill_id))
        self.db.commit()

    def recent_fills(self, limit: int = 100) -> list[dict]:
        cols = ("ts", "coin", "ticker", "side", "action", "count",
                "price_cents", "yes_equiv_qty", "fee_cents", "is_taker",
                "reason", "fair_at_fill", "markout_cents")
        rows = self.db.execute(
            f"SELECT {', '.join(cols)} FROM fills ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def reason_stats(self, since_ts: float = 0.0) -> dict[str, dict]:
        """Per-strategy scoreboard. reason is the fill's origin tag
        (maker / pick / snipe / scratch / flatten...), collapsed to its
        first word. avg_markout is the honest per-strategy edge signal."""
        rows = self.db.execute(
            "SELECT reason, COUNT(*), SUM(count), SUM(fee_cents), "
            "AVG(markout_cents) FROM fills WHERE ts >= ? GROUP BY reason",
            (since_ts,)).fetchall()
        agg: dict[str, dict] = {}
        for reason, fills, contracts, fees, mo in rows:
            key = (reason or "?").split()[0].split("_")[0]
            a = agg.setdefault(key, {"fills": 0, "contracts": 0,
                                     "fees_cents": 0.0, "_mo_sum": 0.0,
                                     "_mo_n": 0})
            a["fills"] += fills
            a["contracts"] += contracts or 0
            a["fees_cents"] += fees or 0.0
            if mo is not None:
                a["_mo_sum"] += mo * fills
                a["_mo_n"] += fills
        for a in agg.values():
            a["avg_markout_cents"] = (round(a["_mo_sum"] / a["_mo_n"], 2)
                                      if a["_mo_n"] else None)
            a["fees_cents"] = round(a["fees_cents"], 1)
            del a["_mo_sum"], a["_mo_n"]
        return agg

    def coin_stats(self, since_ts: float = 0.0) -> dict[str, dict]:
        rows = self.db.execute(
            "SELECT coin, COUNT(*), SUM(count), SUM(fee_cents), "
            "AVG(markout_cents), SUM(CASE WHEN is_taker=1 THEN 1 ELSE 0 END) "
            "FROM fills WHERE ts >= ? GROUP BY coin", (since_ts,)).fetchall()
        return {
            coin: {
                "fills": fills,
                "contracts": contracts or 0,
                "fees_cents": round(fees or 0.0, 1),
                "avg_markout_cents": round(mo, 3) if mo is not None else None,
                "taker_fills": takers,
            }
            for coin, fills, contracts, fees, mo, takers in rows
        }
