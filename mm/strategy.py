"""Quoting engine.

The naive spread bot ("bid 1c over, flip at the ask") loses money precisely
because bids fill when price is moving through them. Every quote here is
therefore anchored to a spot-derived fair value, not to the Kalshi book:

  half_spread = base_edge + maker_fee + as_mult * fair_value_vol + inv_skew

- fair_value_vol is how far fair value walks in ~3s (our cancel horizon):
  the literal price of being adversely selected, recomputed every tick.
- Quotes are pulled outright on: stale spot feed, 30s vol spike, the final
  minutes of the window (settlement averaging + binary gamma), or risk halt.
- Inventory is exited by skewed quotes, a scratch rule that crosses out when
  fair moves against a fill, and a hard flatten before settlement.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from .config import Config
from .fees import maker_fee_per_contract, taker_fee_per_contract
from .model import SpotState, fair_value_cents, fair_value_vol_cents
from .orderbook import Book


@dataclass
class DesiredOrder:
    side: str      # "yes" or "no" (both are BUY orders on Kalshi)
    price: int     # cents, price of that side
    size: int


@dataclass
class CrossExit:
    """Take liquidity now to close inventory. side is the side we BUY."""
    side: str
    size: int
    limit_price: int   # cross up to this price
    reason: str


@dataclass
class Decision:
    desired: list[DesiredOrder] = field(default_factory=list)
    crosses: list[CrossExit] = field(default_factory=list)
    fair: float = 0.0
    fv_vol: float = 0.0
    reason: str = ""


@dataclass
class MarketInfo:
    ticker: str
    strike: float
    close_ts: float          # unix seconds
    open_ts: float = 0.0

    def seconds_to_close(self, now: float | None = None) -> float:
        return self.close_ts - (now if now is not None else time.time())


class QuoteEngine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._cooldown_until: dict[str, float] = {}
        self._pick_last: dict[tuple[str, str], float] = {}
        self._fair_hist: dict[str, list[tuple[float, float]]] = {}

    def _record_fair(self, ticker: str, fair: float, now: float) -> None:
        hist = self._fair_hist.setdefault(ticker, [])
        hist.append((now, fair))
        cutoff = now - 60.0
        while hist and hist[0][0] < cutoff:
            hist.pop(0)

    def _fair_drift(self, ticker: str, fair: float, now: float,
                    lookback: float = 30.0) -> float:
        """fair now minus fair ~lookback seconds ago (0 if unknown)."""
        for ts, f in self._fair_hist.get(ticker, []):
            if ts <= now - lookback * 0.8:
                return fair - f
        return 0.0

    def last_fair(self, ticker: str, now: float, max_age: float = 90.0) -> float:
        hist = self._fair_hist.get(ticker, [])
        if hist and now - hist[-1][0] <= max_age:
            return hist[-1][1]
        return 0.0

    def compute(self, mkt: MarketInfo, book: Book, spot: SpotState,
                position: int, avg_entry: float | None,
                now: float | None = None,
                strike_is_proxy: bool = False) -> Decision:
        cfg = self.cfg
        now = now if now is not None else time.time()
        t_left = mkt.seconds_to_close(now)
        d = Decision()

        if t_left <= 0:
            d.reason = "closed"
            return d

        # ---------------- hard guards: no fair value, no quotes ----------
        if spot.is_stale(cfg.spot_stale_seconds):
            d.reason = "spot_stale"
            d.crosses = self._flatten_if_needed(
                mkt, book, position, t_left,
                fair=self.last_fair(mkt.ticker, now), force=False)
            return d
        if mkt.strike <= 0:
            d.reason = "no_strike"
            return d
        if book.crossed:
            # Corrupt local book: those "great prices" are phantoms.
            d.reason = "book_invalid"
            d.crosses = self._flatten_if_needed(
                mkt, book, position, mkt.seconds_to_close(now),
                fair=self.last_fair(mkt.ticker, now), force=False)
            return d

        sigma = spot.vol.sigma_per_sec
        fair = fair_value_cents(spot.price, mkt.strike, sigma, t_left)
        fv_vol = fair_value_vol_cents(spot.price, mkt.strike, sigma, t_left)
        d.fair, d.fv_vol = fair, fv_vol
        self._record_fair(mkt.ticker, fair, now)

        # ---------------- inventory exits always run ---------------------
        if position != 0:
            d.crosses = self._exit_crosses(mkt, book, position, avg_entry,
                                           fair, t_left)
            if d.crosses:
                # Never add quotes while actively exiting — a resting quote
                # filling mid-exit would flip us through zero.
                d.reason = "exiting"
                return d

        # ---------------- quote suppression ------------------------------
        if now < self._cooldown_until.get(mkt.ticker, 0.0):
            d.reason = "vol_cooldown"
            return d
        if spot.vol.spike_ratio() > cfg.vol_spike_mult:
            self._cooldown_until[mkt.ticker] = now + cfg.vol_spike_cooldown
            d.reason = "vol_spike"
            return d
        if t_left < cfg.no_quote_seconds:
            d.reason = "near_close"
            return d
        if mkt.open_ts and (now - mkt.open_ts) < cfg.min_open_seconds:
            d.reason = "just_opened"
            return d
        if sigma <= 0:
            d.reason = "warming_up"
            return d

        # ---------------- stale-quote picker (latency taker) -------------
        # Spot leads Kalshi by seconds. When a resting quote is priced far
        # enough through our fair value to pay the taker fee, the vol
        # buffer, and a profit margin, take it before it's repriced.
        # Runs AFTER every suppression guard — taking is the aggressive
        # side of the strategy and deserves the strictest conditions.
        pick = self._maybe_pick(mkt, book, position, fair, fv_vol,
                                strike_is_proxy, now)
        if pick:
            d.crosses.append(pick)

        # ---------------- touch-joining market maker ---------------------
        # Real market making, no directional view: rest just inside the best
        # bid AND the best ask, capture whatever spread exists, and manage
        # inventory purely by skewing both quotes against the position. The
        # book is treated as truth — our fair value played no useful part in
        # centering (near the money it disagrees with the market and only
        # pushed us off-book, so we never filled). Adverse-selection defence
        # is the vol-spike / stale-spot guards above plus the scratch/flatten
        # exits, not a fat fair-value buffer that keeps us from ever trading.
        if book.crossed:
            d.reason = "book_invalid"
            return d
        have_bid = bool(book.yes) and book.best_yes_bid > 0
        have_ask = bool(book.no) and book.best_yes_ask < 100
        gap = max(cfg.min_capture_cents, 2)
        if have_bid and have_ask:
            # Two-sided: only make the market if the spread is wide enough to
            # profit after fees; churning a 2-3c book just donates fees.
            if book.best_yes_ask - book.best_yes_bid < cfg.min_book_spread_cents:
                d.reason = "spread_too_tight"
                return d
            # Rest just inside both touches, capturing the spread.
            bid = book.best_yes_bid + 1
            ask = book.best_yes_ask - 1
        elif have_bid:
            # Only bids in the book: join the bid at the touch and make a
            # tight market just above it so BOTH sides sit where trading is.
            bid = book.best_yes_bid + 1
            ask = bid + gap
        elif have_ask:
            # Only offers: join the ask at the touch, bid just below it.
            ask = book.best_yes_ask - 1
            bid = ask - gap
        else:
            # Empty book: seed a market from fair value.
            if fair < 5 or fair > 95:
                d.reason = "extreme_prob"
                return d
            half = (cfg.base_edge_cents
                    + maker_fee_per_contract(int(round(fair)) or 1, cfg.maker_fee_mult)
                    + cfg.as_vol_mult * fv_vol)
            bid = int(math.floor(fair - half))
            ask = int(math.ceil(fair + half))

        # Pinned binary: nothing to make.
        ref = (bid + ask) / 2
        if ref < 5 or ref > 95:
            d.reason = "extreme_prob"
            return d

        # Inventory skew: long -> shift both down (sell eagerly), short -> up.
        skew = int(round(cfg.inventory_skew_cents * position
                         / max(cfg.max_position, 1)))
        bid -= skew
        ask -= skew

        # Trend guard: fair has been moving -> shift quotes WITH it so we
        # ride the move instead of feeding liquidity into it (the falling-
        # knife bids that lost $15 on BTC as its fair fell to ~9).
        drift = self._fair_drift(mkt.ticker, fair, now)
        trend = int(round(cfg.trend_skew_mult * drift))
        trend = max(-cfg.max_trend_skew_cents, min(cfg.max_trend_skew_cents, trend))
        bid += trend
        ask += trend

        # Toxic-fill guard: never quote so far through our own fair that a
        # one-sided or absurdly-priced book fills us at a terrible price
        # (e.g. selling YES at 18c when fair is 49c). A generous band, so
        # normal book-joining near fair is untouched — this only clips the
        # egregious fills that drove the big losses.
        band = cfg.fair_sanity_band_cents
        if 5 <= fair <= 95:
            fair_i = int(round(fair))
            bid = min(bid, fair_i + band)     # don't buy far above fair
            ask = max(ask, fair_i - band)     # don't sell far below fair

        bid = max(1, min(bid, 98))
        ask = min(99, max(ask, bid + cfg.min_capture_cents))

        if ask - bid < cfg.min_capture_cents:
            d.reason = "spread_too_tight"
            return d

        bid_ok = 1 <= bid <= 99
        ask_ok = 1 <= ask <= 99
        yes_size = min(cfg.quote_size, cfg.max_position - position)
        no_size = min(cfg.quote_size, cfg.max_position + position)
        if bid_ok and yes_size > 0:
            d.desired.append(DesiredOrder("yes", bid, yes_size))
        if ask_ok and no_size > 0:
            d.desired.append(DesiredOrder("no", 100 - ask, no_size))
        d.reason = "quoting"
        return d

    # ------------------------------------------------------------------ picks

    def _maybe_pick(self, mkt: MarketInfo, book: Book, position: int,
                    fair: float, fv_vol: float, strike_is_proxy: bool,
                    now: float) -> CrossExit | None:
        cfg = self.cfg
        if not cfg.pick_enabled:
            return None
        # Early window: the crowd prices the opening drift before our model
        # has this window's context — a "mispriced" book is them, not us.
        if mkt.open_ts and now - mkt.open_ts < cfg.pick_min_open_seconds:
            return None
        # Probability extremes: tail model error dominates the edge there.
        lo, hi = cfg.pick_fair_band
        if not (lo <= fair <= hi):
            return None
        # Picks build inventory aggressively; cap them at half the book so
        # the maker always keeps room to work.
        if abs(position) >= max(cfg.max_position // 2, 1):
            return None
        extra = cfg.pick_proxy_penalty_cents if strike_is_proxy else 0.0
        # Falling-knife guard: a "cheap" ask while fair itself is dropping
        # is usually the market repricing faster than our model, not free
        # money — skip the dip side until fair stabilises.
        drift = self._fair_drift(mkt.ticker, fair, now)

        # Cheap YES ask: someone is selling below fair.
        if book.no and drift > -cfg.pick_trend_guard_cents:
            ask = book.best_yes_ask
            if 1 <= ask <= 99:
                req = (cfg.pick_min_edge_cents + fv_vol + extra
                       + taker_fee_per_contract(ask, cfg.taker_fee_mult))
                size = min(cfg.quote_size, cfg.max_position - position,
                           book.depth_at("no", 100 - ask))
                if (fair - ask >= req and size > 0
                        and now - self._pick_last.get((mkt.ticker, "yes"), 0)
                        >= cfg.pick_cooldown_s):
                    self._pick_last[(mkt.ticker, "yes")] = now
                    return CrossExit("yes", size, ask,
                                     f"pick fair={fair:.1f} ask={ask}")

        # Rich YES bid: someone is buying above fair — sell to them (buy NO).
        if book.yes and drift < cfg.pick_trend_guard_cents:
            bid = book.best_yes_bid
            no_px = 100 - bid
            if 1 <= no_px <= 99:
                req = (cfg.pick_min_edge_cents + fv_vol + extra
                       + taker_fee_per_contract(no_px, cfg.taker_fee_mult))
                size = min(cfg.quote_size, cfg.max_position + position,
                           book.depth_at("yes", bid))
                if (bid - fair >= req and size > 0
                        and now - self._pick_last.get((mkt.ticker, "no"), 0)
                        >= cfg.pick_cooldown_s):
                    self._pick_last[(mkt.ticker, "no")] = now
                    return CrossExit("no", size, no_px,
                                     f"pick fair={fair:.1f} bid={bid}")
        return None

    # ------------------------------------------------------------------ exits

    def _exit_crosses(self, mkt: MarketInfo, book: Book, position: int,
                      avg_entry: float | None, fair: float,
                      t_left: float) -> list[CrossExit]:
        cfg = self.cfg
        crosses: list[CrossExit] = []

        # Hard flatten before the settlement-averaging window.
        if t_left < cfg.flatten_seconds:
            crosses.append(self._cross_out(book, position, fair, "flatten_close"))
        # Scratch: fair value moved through our entry — pay the spread to
        # stop the bleeding instead of hoping.
        elif avg_entry is not None:
            if position > 0 and fair <= avg_entry - cfg.scratch_cents:
                crosses.append(self._cross_out(book, position, fair, "scratch"))
            elif position < 0 and fair >= avg_entry + cfg.scratch_cents:
                crosses.append(self._cross_out(book, position, fair, "scratch"))
        return [c for c in crosses if c and c.size > 0]

    def _flatten_if_needed(self, mkt: MarketInfo, book: Book, position: int,
                           t_left: float, fair: float, force: bool) -> list[CrossExit]:
        if position != 0 and (force or t_left < self.cfg.flatten_seconds):
            c = self._cross_out(book, position, fair, "flatten")
            return [c] if c and c.size > 0 else []
        return []

    def _cross_out(self, book: Book, position: int, fair: float,
                   reason: str) -> CrossExit | None:
        """Exit by crossing — but never further than max_exit_slippage
        through fair. Settlement pays out ~fair on average, so dumping a
        25c-fair position into a 2c bid is a donation; if no liquidity
        exists within the cap, hold and let settlement (or a later book)
        do better. With NO fair at all we don't trade — a blind exit at
        whatever the book shows is strictly worse than settling.
        """
        if fair <= 0:
            return None
        slip = self.cfg.max_exit_slippage_cents
        if position > 0:
            # Long YES: buy NO to net out. NO ask price = 100 - best_yes_bid.
            limit = 100 - book.best_yes_bid if book.yes else 99
            if fair > 0:
                allowed = 100 - max(int(math.floor(fair - slip)), 0)
                limit = min(limit, allowed)
            return CrossExit("no", position, min(max(limit, 1), 99), reason)
        limit = book.best_yes_ask if book.no else 99
        if fair > 0:
            limit = min(limit, min(int(math.ceil(fair + slip)), 99))
        return CrossExit("yes", -position, min(max(limit, 1), 99), reason)
