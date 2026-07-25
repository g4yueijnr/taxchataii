"""Always-on cross-venue arbitrage paper daemon.

Continuously re-scans Kalshi + Polymarket, matches overlapping markets, and
books every arbitrage above the edge threshold against a simulated $100
bankroll. Serves a live dashboard (also the Railway health check) so the app
stays warm and you can watch what it finds. No keys, no real orders.

Run:  python -m arb.daemon
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

from .arbitrage import find_opportunities
from .config import ArbConfig, load_config
from .health import start_http
from .kalshi import KalshiClient
from .matching import match_markets
from .paper import PaperArbBook
from .polymarket import PolymarketClient

log = logging.getLogger("arb")


def _load_manual(path: str) -> dict[str, str]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        import yaml
        data = yaml.safe_load(p.read_text()) or {}
        return {str(k): str(v) for k, v in (data.get("matches") or {}).items()}
    except Exception:
        log.warning("could not read matches file %s", path)
        return {}


class ArbDaemon:
    def __init__(self, cfg: ArbConfig):
        self.cfg = cfg
        self.started_at = time.time()
        self.book = PaperArbBook(bankroll=cfg.bankroll)
        self.manual = _load_manual(cfg.matches_file)
        # Market-data only -- credentials (if present) let Kalshi's orderbook
        # endpoint respond, but we never place an order from the daemon.
        key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
        pem = (Path(key_path).read_bytes()
               if key_path and Path(key_path).exists() else None)
        self.kalshi = KalshiClient(api_key_id=os.getenv("KALSHI_API_KEY_ID"),
                                   private_key_pem=pem)
        self.poly = PolymarketClient()
        self.kalshi_authed = self.kalshi.can_trade   # on the higher rate tier?
        # dashboard state
        self.scans = 0
        self.last_scan = 0.0
        self.last_error: str | None = None
        self.k_count = 0
        self.p_count = 0
        self.pair_count = 0
        self.confirmed_count = 0
        self.last_opps: list = []

    def _scan_sync(self):
        """Blocking full scan (runs in a thread). Returns (pairs, opps)."""
        k = self.kalshi.fetch_open_markets(
            min_volume=int(self.cfg.min_volume),
            max_pages=self.cfg.kalshi_max_pages, log=log.debug)
        p = self.poly.fetch_open_markets(
            min_volume=self.cfg.min_volume,
            max_pages=self.cfg.poly_max_pages, log=log.debug)
        self.k_count, self.p_count = len(k), len(p)
        pairs = match_markets(k, p, min_score=self.cfg.min_score,
                              max_days_apart=self.cfg.max_days_apart,
                              manual=self.manual)
        if self.cfg.fresh_prices and pairs:
            try:
                self.poly.refresh_asks([pr.poly for pr in pairs])
            except Exception as e:
                log.warning("CLOB price refresh failed: %s", e)
        opps = find_opportunities(pairs, min_edge=self.cfg.min_edge)
        return pairs, opps

    async def scan_loop(self):
        while True:
            try:
                pairs, opps = await asyncio.to_thread(self._scan_sync)
                self.pair_count = len(pairs)
                self.confirmed_count = sum(1 for pr in pairs if pr.confirmed)
                self.last_opps = opps
                self.scans += 1
                self.last_scan = time.time()
                self.last_error = None
                booked = 0
                for opp in opps:
                    if not opp.pair.confirmed and not self.cfg.book_fuzzy:
                        continue
                    if self.book.already_took(opp):
                        continue
                    fill = self.book.execute(opp, self.cfg.size)
                    if fill:
                        booked += 1
                        log.info("BOOKED %s %s ×%d edge=%.1fc -> +$%.2f%s",
                                 fill.ticker, fill.direction, fill.contracts,
                                 fill.edge * 100, fill.profit,
                                 "" if fill.confirmed else " (FUZZY)")
                log.info("scan #%d: K=%d P=%d matched=%d (%d confirmed) "
                         "opps=%d booked=%d equity=$%.2f",
                         self.scans, self.k_count, self.p_count,
                         self.pair_count, self.confirmed_count, len(opps),
                         booked, self.book.equity)
            except Exception as e:
                self.last_error = str(e)
                log.exception("scan failed")
            await asyncio.sleep(self.cfg.scan_interval)

    async def run(self):
        log.info("starting arb daemon v%s bankroll=$%.0f min_edge=%.0fc "
                 "interval=%.0fs manual_pairs=%d",
                 _ver(), self.cfg.bankroll, self.cfg.min_edge * 100,
                 self.cfg.scan_interval, len(self.manual))
        http = await start_http(self, self.cfg.port)
        try:
            await self.scan_loop()
        finally:
            await http.cleanup()
            self.kalshi._session.close()
            self.poly._session.close()


def _ver() -> str:
    from . import __version__
    return __version__


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(ArbDaemon(load_config()).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
