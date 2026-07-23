"""Wiring: discovery, feeds, quote loop.

Event flow:
  spot tick / book delta  ->  mark market dirty
  100ms evaluator         ->  QuoteEngine.compute -> reconcile orders/crosses
  discovery loop (15s)    ->  find each series' current window, resolve
                              strikes, settle residual positions, refresh
                              balance, prune closed markets
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from dataclasses import dataclass, field

from .config import CoinConfig, Config, load_config
from .execution import OrderManager, PositionBook
from .health import start_http
from .kalshi_rest import KalshiRest
from .kalshi_ws import KalshiWs
from .risk import RiskManager
from .sim import SimOrderManager
from .spot import SpotFeeds
from .strategy import MarketInfo, QuoteEngine

log = logging.getLogger("mm")

EVAL_INTERVAL = 0.1   # seconds between dirty-market evaluations


def _parse_ts(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


@dataclass
class ActiveMarket:
    coin: CoinConfig
    info: MarketInfo
    strike_is_proxy: bool = False
    last_fair: float = 0.0
    last_fv_vol: float = 0.0
    last_reason: str = "new"
    last_eval: float = 0.0
    dirty: bool = True


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.started_at = time.time()
        self.positions = PositionBook()
        self.risk = RiskManager(cfg, self.positions)
        self.engine = QuoteEngine(cfg)
        self.spots = SpotFeeds(cfg.coins)
        self.rest = KalshiRest(cfg.kalshi_api_key_id, cfg.kalshi_private_key_pem,
                               write_rate=cfg.write_rate_per_sec)
        self.ws = KalshiWs(self.rest, on_book_update=self._on_book,
                           on_fill=self._on_fill, on_trade=self._on_trade)
        if cfg.dry_run:
            self.om: OrderManager = SimOrderManager(cfg, self.positions)
        else:
            self.om = OrderManager(self.rest, cfg, self.positions)
        self.active: dict[str, ActiveMarket] = {}       # ticker -> state
        self._settling: dict[str, CoinConfig] = {}      # closed, awaiting result

    # ------------------------------------------------------------ callbacks

    async def _on_book(self, book) -> None:
        st = self.active.get(book.ticker)
        if st:
            st.dirty = True

    async def _on_trade(self, msg: dict) -> None:
        if self.cfg.dry_run and isinstance(self.om, SimOrderManager):
            self.om.on_public_trade(msg)
        st = self.active.get(msg.get("market_ticker", ""))
        if st:
            st.dirty = True

    async def _on_fill(self, msg: dict) -> None:
        if not self.cfg.dry_run:
            self.om.on_fill_msg(msg)
        st = self.active.get(msg.get("market_ticker", ""))
        if st:
            st.dirty = True

    # ------------------------------------------------------------ discovery

    async def discovery_loop(self) -> None:
        while True:
            try:
                await self._discover_once()
            except Exception:
                log.exception("discovery failed")
            await asyncio.sleep(self.cfg.discovery_interval)

    async def _discover_once(self) -> None:
        now = time.time()
        for coin in self.cfg.coins:
            try:
                markets = await self.rest.get_markets(coin.series_ticker)
            except Exception as e:
                log.warning("no markets for %s (%s)", coin.series_ticker, e)
                continue
            # Current window: open market with the earliest future close.
            candidates = [m for m in markets
                          if _parse_ts(m.get("close_time")) > now + 5]
            candidates.sort(key=lambda m: _parse_ts(m.get("close_time")))
            for m in candidates[:2]:   # current window + on-deck window
                ticker = m["ticker"]
                if ticker in self.active:
                    self._maybe_update_strike(self.active[ticker], m)
                    continue
                info = MarketInfo(
                    ticker=ticker,
                    strike=self._extract_strike(m) or 0.0,
                    close_ts=_parse_ts(m.get("close_time")),
                    open_ts=_parse_ts(m.get("open_time")),
                )
                st = ActiveMarket(coin, info)
                if info.strike <= 0:
                    spot = self.spots.state(coin.symbol)
                    if spot.price > 0 and info.open_ts and now >= info.open_ts:
                        info.strike = spot.price
                        st.strike_is_proxy = True
                self.active[ticker] = st
                log.info("tracking %s strike=%s%s close=%s", ticker, info.strike,
                         " (spot proxy)" if st.strike_is_proxy else "",
                         m.get("close_time"))

        # Prune closed markets; carry residual positions to settlement check.
        for ticker in [t for t, s in self.active.items()
                       if s.info.close_ts <= now]:
            st = self.active.pop(ticker)
            await self.om.cancel_all(ticker)
            if self.positions.pos(ticker).net != 0:
                self._settling[ticker] = st.coin
        await self.ws.set_markets(set(self.active))
        await self._resolve_settlements()

        if not self.cfg.dry_run and self.rest.can_trade:
            try:
                self.risk.balance_cents = await self.rest.get_balance()
            except Exception as e:
                log.warning("balance fetch failed: %s", e)

    def _extract_strike(self, m: dict) -> float:
        for key in ("floor_strike", "cap_strike", "strike"):
            v = m.get(key)
            if isinstance(v, (int, float)) and v > 0:
                return float(v)
        return 0.0

    def _maybe_update_strike(self, st: ActiveMarket, m: dict) -> None:
        real = self._extract_strike(m)
        if real > 0 and (st.strike_is_proxy or st.info.strike <= 0):
            st.info.strike = real
            st.strike_is_proxy = False

    async def _resolve_settlements(self) -> None:
        for ticker in list(self._settling):
            try:
                m = await self.rest.get_market(ticker)
            except Exception:
                continue
            result = m.get("result")
            if result in ("yes", "no"):
                self.positions.settle(ticker, result)
                del self._settling[ticker]

    # ------------------------------------------------------------ evaluator

    async def eval_loop(self) -> None:
        while True:
            try:
                await self._eval_once()
            except Exception:
                log.exception("eval failed")
            await asyncio.sleep(EVAL_INTERVAL)

    async def _eval_once(self) -> None:
        now = time.time()
        ok, reason = self.risk.check()
        if not ok:
            # Soft breach or kill switch: pull every resting quote, but keep
            # running exit logic below so inventory still gets managed.
            await self.om.cancel_all()

        for ticker, st in list(self.active.items()):
            spot = self.spots.state(st.coin.symbol)
            # Re-eval on book/trade events, any spot tick since last eval,
            # or at least 1/s for time decay.
            if (not st.dirty and spot.last_update < st.last_eval
                    and now - st.last_eval < 1.0):
                continue
            st.dirty = False
            st.last_eval = now
            book = self.ws.book(ticker)
            p = self.positions.pos(ticker)
            decision = self.engine.compute(
                st.info, book, spot, p.net,
                p.avg_entry if p.net != 0 else None, now)
            st.last_fair = decision.fair
            st.last_fv_vol = decision.fv_vol
            st.last_reason = decision.reason if ok else f"risk:{reason}"

            for c in decision.crosses:
                if isinstance(self.om, SimOrderManager):
                    await self.om.cross(ticker, c, book)
                else:
                    await self.om.cross(ticker, c)
            if decision.desired and ok:
                await self.om.reconcile(ticker, decision.desired)
            else:
                await self.om.cancel_all(ticker)

    # ------------------------------------------------------------ lifecycle

    async def run(self) -> None:
        log.info("starting: dry_run=%s coins=%s", self.cfg.dry_run,
                 [c.symbol for c in self.cfg.coins])
        if not self.cfg.dry_run and not self.cfg.can_trade:
            raise SystemExit(
                "DRY_RUN=false but KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY missing")
        await self.rest.start()
        http = await start_http(self, self.cfg.port)
        tasks = [
            asyncio.create_task(self.spots.run_forever(), name="spot"),
            asyncio.create_task(self.ws.run_forever(), name="kalshi-ws"),
            asyncio.create_task(self.discovery_loop(), name="discovery"),
            asyncio.create_task(self.eval_loop(), name="eval"),
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            if not self.cfg.dry_run:
                try:
                    await self.om.cancel_all()
                except Exception:
                    log.exception("final cancel_all failed")
            await http.cleanup()
            await self.rest.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    bot = Bot(cfg)
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        pass
