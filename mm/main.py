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
import signal
import time
from dataclasses import dataclass, field

from .config import CoinConfig, Config, load_config
from .execution import OrderManager, PositionBook
from .health import start_http
from .journal import MARKOUT_HORIZON_S, Journal
from .kalshi_rest import KalshiRest
from .kalshi_ws import KalshiWs
from .risk import RiskManager
from .settlement import Sniper
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
    sniped: bool = False   # holding a settlement snipe to expiry


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
        self.om.on_booked = self._on_booked
        self.sniper = Sniper(cfg)
        self.journal = Journal(cfg.data_dir)
        self.active: dict[str, ActiveMarket] = {}       # ticker -> state
        self._settling: dict[str, CoinConfig] = {}      # closed, awaiting result
        self._ticker_coin: dict[str, str] = {}          # ticker -> coin symbol
        self._pending_markouts: list[tuple[int, str, float, float, int]] = []
        self._coin_day_base: dict[str, float] = {}      # coin -> net at day start
        self._coin_day: dt.date = dt.date.today()
        self._last_report = time.time()

    @property
    def sim_equity_cents(self) -> float:
        """Paper account equity: bankroll + net P&L booked so far."""
        return self.cfg.sim_bankroll_dollars * 100 + self.positions.net_pnl_cents

    # ----------------------------------------------------------- coin P&L

    def coin_net_cents(self, coin: str) -> float:
        """Net realized P&L (cents, after fees) for a coin's markets."""
        return sum(p.realized - p.fees for t, p in self.positions.positions.items()
                   if self._ticker_coin.get(t) == coin)

    def coin_day_net_cents(self, coin: str) -> float:
        today = dt.date.today()
        if today != self._coin_day:
            self._coin_day = today
            self._coin_day_base = {c.symbol: self.coin_net_cents(c.symbol)
                                   for c in self.cfg.coins}
        return self.coin_net_cents(coin) - self._coin_day_base.get(coin, 0.0)

    # ----------------------------------------------------------- journaling

    def _on_booked(self, ticker: str, side: str, action: str, count: int,
                   price: int, yes_equiv_qty: int, fee: float,
                   is_taker: bool, reason: str) -> None:
        coin = self._ticker_coin.get(ticker, "?")
        st = self.active.get(ticker)
        fair = st.last_fair if st and st.last_fair > 0 else None
        try:
            fill_id = self.journal.record_fill(
                coin, ticker, side, action, count, price, yes_equiv_qty,
                fee, is_taker, fair, reason)
        except Exception:
            log.exception("journal write failed")
            return
        if fair is not None:
            sign = 1 if yes_equiv_qty > 0 else -1
            self._pending_markouts.append(
                (fill_id, ticker, time.time(), fair, sign))

    async def markout_loop(self) -> None:
        while True:
            await asyncio.sleep(5.0)
            now = time.time()
            keep = []
            for fill_id, ticker, ts, fair_at, sign in self._pending_markouts:
                if now - ts < MARKOUT_HORIZON_S:
                    keep.append((fill_id, ticker, ts, fair_at, sign))
                    continue
                st = self.active.get(ticker)
                if st and st.last_fair > 0:
                    try:
                        self.journal.set_markout(
                            fill_id, sign * (st.last_fair - fair_at))
                    except Exception:
                        log.exception("markout write failed")
            self._pending_markouts = keep

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
                self._ticker_coin[ticker] = coin.symbol
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
        self.sniper.prune(set(self.active))
        await self._resolve_settlements()

        if self.cfg.dry_run:
            self.risk.balance_cents = int(self.sim_equity_cents)
        elif self.rest.can_trade:
            try:
                self.risk.balance_cents = await self.rest.get_balance()
            except Exception as e:
                log.warning("balance fetch failed: %s", e)

        now2 = time.time()
        if now2 - self._last_report >= 3600:
            self._last_report = now2
            hours = (now2 - self.started_at) / 3600
            per_coin = {c.symbol: round(self.coin_net_cents(c.symbol), 1)
                        for c in self.cfg.coins}
            if self.cfg.dry_run:
                log.info("[report %.1fh] $%.2f -> $%.2f | net %.1fc | "
                         "fills %d | per-coin %s",
                         hours, self.cfg.sim_bankroll_dollars,
                         self.sim_equity_cents / 100,
                         self.positions.net_pnl_cents,
                         self.positions.fills, per_coin)
            else:
                log.info("[report %.1fh] net %.1fc | fills %d | per-coin %s",
                         hours, self.positions.net_pnl_cents,
                         self.positions.fills, per_coin)

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
            coin_ok = self.risk.coin_allowed(
                st.coin.symbol, self.coin_day_net_cents(st.coin.symbol))

            if st.sniped and p.net != 0:
                # Snipes are held to settlement — keep fair fresh for the
                # dashboard/markouts but don't let the MM flatten them.
                st.last_reason = "sniped"
                self._update_fair(st, spot, now)
            else:
                decision = self.engine.compute(
                    st.info, book, spot, p.net,
                    p.avg_entry if p.net != 0 else None, now)
                st.last_fair = decision.fair
                st.last_fv_vol = decision.fv_vol
                if not ok:
                    st.last_reason = f"risk:{reason}"
                elif not coin_ok:
                    st.last_reason = "coin_benched"
                else:
                    st.last_reason = decision.reason

                for c in decision.crosses:
                    if isinstance(self.om, SimOrderManager):
                        await self.om.cross(ticker, c, book)
                    else:
                        await self.om.cross(ticker, c)
                if decision.desired and ok and coin_ok:
                    await self.om.reconcile(ticker, decision.desired)
                else:
                    await self.om.cancel_all(ticker)

            # Settlement sniper: flat markets only, near the close.
            if (ok and coin_ok and not st.sniped and p.net == 0
                    and st.info.seconds_to_close(now) <= self.cfg.sniper_window_s):
                take = self.sniper.evaluate(st.info, book, spot,
                                            st.strike_is_proxy, now)
                if take:
                    st.sniped = True
                    st.last_reason = "sniping"
                    log.info("SNIPE %s buy %s %d@%dc (%s)", ticker, take.side,
                             take.size, take.limit_price, take.reason)
                    if isinstance(self.om, SimOrderManager):
                        await self.om.cross(ticker, take, book)
                    else:
                        await self.om.cross(ticker, take)

    def _update_fair(self, st: ActiveMarket, spot, now: float) -> None:
        from .model import fair_value_cents
        if spot.price > 0 and st.info.strike > 0:
            st.last_fair = fair_value_cents(
                spot.price, st.info.strike, spot.vol.sigma_per_sec,
                st.info.seconds_to_close(now))

    # ------------------------------------------------------------ lifecycle

    async def run(self) -> None:
        log.info("starting: dry_run=%s coins=%s", self.cfg.dry_run,
                 [c.symbol for c in self.cfg.coins])
        if not self.cfg.dry_run and not self.cfg.can_trade:
            raise SystemExit(
                "DRY_RUN=false but KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY missing")
        await self.rest.start()
        if not self.cfg.dry_run:
            await self._boot_sweep()
        http = await start_http(self, self.cfg.port)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                pass
        tasks = [
            asyncio.create_task(self.spots.run_forever(), name="spot"),
            asyncio.create_task(self.ws.run_forever(), name="kalshi-ws"),
            asyncio.create_task(self.discovery_loop(), name="discovery"),
            asyncio.create_task(self.eval_loop(), name="eval"),
            asyncio.create_task(self.markout_loop(), name="markout"),
        ]
        stopper = asyncio.create_task(stop.wait(), name="stop")
        try:
            done, _ = await asyncio.wait([*tasks, stopper],
                                         return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t is not stopper and t.exception():
                    raise t.exception()
            log.info("shutdown signal received")
        finally:
            for t in [*tasks, stopper]:
                t.cancel()
            if not self.cfg.dry_run:
                try:
                    await self.om.cancel_all()
                except Exception:
                    log.exception("final cancel_all failed")
            await http.cleanup()
            await self.rest.close()
            self.journal.close()

    async def _boot_sweep(self) -> None:
        """Cancel stray resting orders left by a previous run/crash."""
        try:
            orders = await self.rest.get_resting_orders()
        except Exception as e:
            log.warning("boot sweep failed to list orders: %s", e)
            return
        for o in orders:
            oid = o.get("order_id") or o.get("id")
            if oid:
                try:
                    await self.rest.cancel_order(oid)
                    log.info("boot sweep: cancelled stray order %s (%s)",
                             oid, o.get("ticker"))
                except Exception as e:
                    log.warning("boot sweep cancel failed %s: %s", oid, e)


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
