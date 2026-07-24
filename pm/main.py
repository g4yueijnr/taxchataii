"""Wiring: discover markets -> feed books/trades -> quote -> paper-fill."""

from __future__ import annotations

import asyncio
import logging
import time

from .client import Market, PolyClient
from .config import Config, load_config
from .engine import MakerStrategy, PaperBook
from .executor import Journal, PaperOrderManager
from .feed import ClobFeed
from .health import start_http

log = logging.getLogger("pm")


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.started_at = time.time()
        self.client = PolyClient(cfg)
        self.positions = PaperBook(cfg)
        self.om = PaperOrderManager(cfg, self.positions)
        self.strategy = MakerStrategy(cfg)
        self.journal = Journal(cfg.data_dir)
        self.feed = ClobFeed(on_trade=self._on_trade, on_book=self._on_book)
        self.markets: dict[str, Market] = {}      # token -> Market
        self.positions.on_fill = self._on_booked
        self.halted = False

    @property
    def equity(self) -> float:
        return self.cfg.sim_bankroll + self.positions.net_pnl_cents / 100.0

    def _on_booked(self, token, side, price, size, realized, rebate):
        m = self.markets.get(token)
        try:
            self.journal.record(token, m.question if m else token, side,
                                price, size, rebate, realized)
        except Exception:
            log.exception("journal write failed")

    async def _on_book(self, token: str) -> None:
        self._requote(token)

    async def _on_trade(self, token: str, price_cents: int, size: float) -> None:
        if self.cfg.dry_run:
            self.om.on_trade(token, price_cents, size)
        self._requote(token)

    def _requote(self, token: str) -> None:
        if self.halted or token not in self.markets:
            return
        # Risk gate.
        if self.positions.gross_dollars() >= self.cfg.max_gross_dollars:
            self.om.set_quote(token, None)
            return
        if self.positions.net_pnl_cents <= -self.cfg.daily_loss_limit_dollars * 100:
            self.halted = True
            log.error("KILL SWITCH: daily loss limit")
            self.om.resting.clear()
            return
        book = self.feed.book(token)
        pos = self.positions.pos(token).shares
        self.om.set_quote(token, self.strategy.compute(book, pos))

    async def discovery_loop(self) -> None:
        while True:
            try:
                found = await self.client.discover()
                if found:
                    self.markets = {m.yes_token: m for m in found}
                    await self.feed.set_assets(set(self.markets))
                    # Seed books over REST so we can quote before the first
                    # ws snapshot arrives.
                    for m in found:
                        try:
                            b = await self.client.get_book(m.yes_token)
                            self.feed.book(m.yes_token).apply_snapshot(
                                [(x["price"], x["size"]) for x in b.get("bids", [])],
                                [(x["price"], x["size"]) for x in b.get("asks", [])])
                        except Exception:
                            pass
                    log.info("tracking %d markets: %s", len(found),
                             [m.question[:40] for m in found[:3]])
            except Exception:
                log.exception("discovery failed")
            await asyncio.sleep(self.cfg.discovery_interval)

    async def report_loop(self) -> None:
        while True:
            await asyncio.sleep(300)
            log.info("[report %.0fm] equity=$%.2f pnl=%.1fc rebates=%.1fc "
                     "fills=%d trades=%d msgs=%d",
                     (time.time() - self.started_at) / 60, self.equity,
                     self.positions.net_pnl_cents, self.positions.rebates_cents,
                     self.positions.fills, self.feed.trade_count,
                     self.feed.msg_count)

    async def run(self) -> None:
        from . import __version__
        log.info("starting pm v%s dry_run=%s category=%s", __version__,
                 self.cfg.dry_run, self.cfg.category)
        await self.client.start()
        http = await start_http(self, self.cfg.port)
        tasks = [
            asyncio.create_task(self.feed.run_forever(), name="feed"),
            asyncio.create_task(self.discovery_loop(), name="discovery"),
            asyncio.create_task(self.report_loop(), name="report"),
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            await http.cleanup()
            await self.client.close()
            self.journal.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(Bot(load_config()).run())
    except KeyboardInterrupt:
        pass
