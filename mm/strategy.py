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
            d.crosses = self._flatten_if_needed(mkt, book, position, t_left,
                                                force=False)
            return d
        if mkt.strike <= 0:
            d.reason = "no_strike"
            return d
        if book.crossed:
            # Corrupt local book: those "great prices" are phantoms.
            d.reason = "book_invalid"
            d.crosses = self._flatten_if_needed(mkt, book, position,
                                                mkt.seconds_to_close(now),
                                                force=False)
            return d

        sigma = spot.vol.sigma_per_sec
        fair = fair_value_cents(spot.price, mkt.strike, sigma, t_left)
        fv_vol = fair_value_vol_cents(spot.price, mkt.strike, sigma, t_left)
        d.fair, d.fv_vol = fair, fv_vol

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
        pick = self._maybe_pick(mkt, book, position, fair, fv_vol,
                                strike_is_proxy, now)
        if pick:
            d.crosses.append(pick)

        # ---------------- two-sided quotes around fair -------------------
        half = (cfg.base_edge_cents
                + maker_fee_per_contract(int(round(fair)) or 1, cfg.maker_fee_mult)
                + cfg.as_vol_mult * fv_vol)
        skew = cfg.inventory_skew_cents * (position / max(cfg.max_position, 1))

        bid_target = fair - half - skew          # our YES buy
        ask_target = fair + half - skew          # our YES sell == NO buy at 100-ask

        bid = int(math.floor(bid_target))
        ask = int(math.ceil(ask_target))

        # Price-improve: sit 1c inside the current best, but never give up
        # more edge than the target allows, and never cross.
        if cfg.improve_tick:
            if book.yes and book.best_yes_bid + 1 <= bid:
                bid = book.best_yes_bid + 1
            if book.no and book.best_yes_ask - 1 >= ask:
                ask = book.best_yes_ask - 1
        if book.no:
            bid = min(bid, book.best_yes_ask - 1)   # resting, not taking
        if book.yes:
            ask = max(ask, book.best_yes_bid + 1)

        if ask - bid < cfg.min_capture_cents:
            d.reason = "spread_too_tight"
            return d

        bid_ok = 1 <= bid <= 99
        ask_ok = 1 <= ask <= 99
        # Deep favorites/longshots: fee-adjusted maker edge dies at extremes
        # and a 1c ladder can't express the required edge — stop adding risk,
        # but keep a reduce-only quote working so a winning position can be
        # sold near $1 instead of waiting for the forced flatten.
        if fair < 5 or fair > 95:
            d.reason = "extreme_prob"
            if fair > 95 and position > 0:
                px = min(99, max(ask, int(math.ceil(fair)) + 1))
                if book.yes:
                    px = max(px, book.best_yes_bid + 1)
                if fair < px <= 99:
                    d.desired.append(
                        DesiredOrder("no", 100 - px, min(cfg.quote_size, position)))
            elif fair < 5 and position < 0:
                px = max(1, min(bid, int(math.floor(fair)) - 1))
                if book.no:
                    px = min(px, book.best_yes_ask - 1)
                if 1 <= px < fair:
                    d.desired.append(
                        DesiredOrder("yes", px, min(cfg.quote_size, -position)))
            return d

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
        extra = cfg.pick_proxy_penalty_cents if strike_is_proxy else 0.0

        # Cheap YES ask: someone is selling below fair.
        if book.no:
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
        if book.yes:
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
            crosses.append(self._cross_out(book, position, "flatten_close"))
        # Scratch: fair value moved through our entry — pay the spread to
        # stop the bleeding instead of hoping.
        elif avg_entry is not None:
            if position > 0 and fair <= avg_entry - cfg.scratch_cents:
                crosses.append(self._cross_out(book, position, "scratch"))
            elif position < 0 and fair >= avg_entry + cfg.scratch_cents:
                crosses.append(self._cross_out(book, position, "scratch"))
        return [c for c in crosses if c and c.size > 0]

    def _flatten_if_needed(self, mkt: MarketInfo, book: Book, position: int,
                           t_left: float, force: bool) -> list[CrossExit]:
        if position != 0 and (force or t_left < self.cfg.flatten_seconds):
            c = self._cross_out(book, position, "flatten")
            return [c] if c.size > 0 else []
        return []

    @staticmethod
    def _cross_out(book: Book, position: int, reason: str) -> CrossExit:
        if position > 0:
            # Long YES: buy NO to net out. NO ask price = 100 - best_yes_bid.
            limit = 100 - book.best_yes_bid if book.yes else 99
            return CrossExit("no", position, min(max(limit, 1), 99), reason)
        limit = book.best_yes_ask if book.no else 99
        return CrossExit("yes", -position, min(max(limit, 1), 99), reason)
