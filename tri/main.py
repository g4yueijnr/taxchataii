"""Wiring: feed -> re-evaluate affected triangles on every tick -> execute.

The hot path is event-driven: every bookTicker update re-scans only the
triangles that use that symbol, so detection latency is a few hundred
microseconds of Python per update, not a polling interval.
"""

from __future__ import annotations

import asyncio
import logging
import time

from .config import Config, load_config
from .engine import BookStore, evaluate
from .executor import Journal, PaperExecutor
from .feed import BinanceFeed
from .health import start_http

log = logging.getLogger("tri")


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.started_at = time.time()
        self.books = BookStore()
        self.journal = Journal(cfg.data_dir)
        self.executor = PaperExecutor(cfg, self.books, self.journal)
        self.feed = BinanceFeed(cfg.symbols, self.books, on_update=self._on_tick)
        self.eval_count = 0
        # symbol -> triangles that reference it (for event-driven scanning)
        self._by_symbol: dict[str, list] = {}
        for tri in cfg.triangles:
            for sym in tri.legs:
                self._by_symbol.setdefault(sym, []).append(tri)

    @property
    def equity(self) -> float:
        return self.cfg.sim_bankroll + self.executor.stats.realized_pnl

    async def _on_tick(self, symbol: str) -> None:
        now = time.time()
        for tri in self._by_symbol.get(symbol, []):
            self.eval_count += 1
            opp = evaluate(tri, self.cfg.pairs, self.books, self.cfg.fee_rate,
                           now, self.cfg.max_book_age_s)
            if opp and opp.net_edge >= self.cfg.min_net_edge:
                if self.cfg.dry_run:
                    self.executor.try_execute(opp, now)
                else:
                    log.warning("live execution not enabled in this build; "
                                "would execute %s edge=%.4f%%",
                                opp.triangle, opp.net_edge * 100)

    async def _report_loop(self) -> None:
        while True:
            await asyncio.sleep(300)
            s = self.executor.stats
            log.info("[report %.0fm] equity=%.4f USDT pnl=%.4f exec=%d miss=%d "
                     "evals=%d msgs=%d", (time.time() - self.started_at) / 60,
                     self.equity, s.realized_pnl, s.executed, s.missed,
                     self.eval_count, self.feed.msg_count)

    async def run(self) -> None:
        from . import __version__
        log.info("starting tri v%s dry_run=%s triangles=%d symbols=%d",
                 __version__, self.cfg.dry_run, len(self.cfg.triangles),
                 len(self.cfg.symbols))
        http = await start_http(self, self.cfg.port)
        tasks = [
            asyncio.create_task(self.feed.run_forever(), name="feed"),
            asyncio.create_task(self._report_loop(), name="report"),
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            await http.cleanup()
            self.journal.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    try:
        asyncio.run(Bot(cfg).run())
    except KeyboardInterrupt:
        pass
