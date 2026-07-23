"""Settlement-window sniper.

Kalshi crypto contracts settle on the average of ~60 once-per-second index
prints over the final minute. That average is progressively *observable*:
with k of 60 samples locked in, the undecided part shrinks every second,
and near the end the outcome is effectively known while stale quotes can
still offer the winning side under $1. This is the documented shape of the
profitable bots in these markets: buy near-certainty at a discount.

We proxy the CF index with our spot feed (Coinbase/Kraken are index
constituents), demand a high probability floor AND a positive EV after
taker fees, and never snipe off a proxy strike.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from .config import Config
from .fees import taker_fee_cents
from .model import SpotState, prob_up
from .orderbook import Book
from .strategy import CrossExit, MarketInfo


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


N_SAMPLES = 60


@dataclass
class SettlementTracker:
    """Once-per-second spot samples over a market's final 60 seconds."""
    close_ts: float
    samples: dict[int, float] = field(default_factory=dict)   # 0..59 -> price

    def record(self, price: float, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        idx = int(now - (self.close_ts - N_SAMPLES))
        if 0 <= idx < N_SAMPLES and price > 0:
            self.samples.setdefault(idx, price)

    def prob_up(self, strike: float, spot: float, sigma_per_sec: float) -> float:
        """P(60s average >= strike) given the samples already locked in.

        Remaining seconds are modeled as arithmetic Brownian motion from the
        current spot: the time-average of BM over tau has std sigma*sqrt(tau/3).
        """
        k = len(self.samples)
        locked = sum(self.samples.values())
        tau = N_SAMPLES - k
        need = N_SAMPLES * strike
        if tau <= 0:
            return 1.0 if locked >= need else 0.0
        mean_total = locked + tau * spot
        std_total = spot * sigma_per_sec * (tau ** 1.5) / math.sqrt(3.0)
        if std_total <= 0:
            return 1.0 if mean_total >= need else 0.0
        return _phi((mean_total - need) / std_total)


class Sniper:
    """Evaluates take opportunities in the final `sniper_window_s` seconds."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.trackers: dict[str, SettlementTracker] = {}
        self.sniped: dict[str, set[str]] = {}    # ticker -> sides already taken

    def tracker(self, mkt: MarketInfo) -> SettlementTracker:
        t = self.trackers.get(mkt.ticker)
        if t is None:
            t = SettlementTracker(mkt.close_ts)
            self.trackers[mkt.ticker] = t
        return t

    def prune(self, active_tickers: set[str]) -> None:
        for t in list(self.trackers):
            if t not in active_tickers:
                self.trackers.pop(t, None)
                self.sniped.pop(t, None)

    def evaluate(self, mkt: MarketInfo, book: Book, spot: SpotState,
                 strike_is_proxy: bool,
                 now: float | None = None) -> CrossExit | None:
        cfg = self.cfg
        if not cfg.sniper_enabled or strike_is_proxy or mkt.strike <= 0:
            return None
        now = now if now is not None else time.time()
        t_left = mkt.close_ts - now
        if t_left <= 1 or t_left > cfg.sniper_window_s:
            return None
        if spot.is_stale(cfg.spot_stale_seconds):
            return None
        sigma = spot.vol.sigma_per_sec
        if sigma <= 0:
            return None

        tracker = self.tracker(mkt)
        if t_left <= N_SAMPLES:
            tracker.record(spot.price, now)
            p_up = tracker.prob_up(mkt.strike, spot.price, sigma)
        else:
            p_up = prob_up(spot.price, mkt.strike, sigma, t_left)

        taken = self.sniped.setdefault(mkt.ticker, set())
        for side, prob in (("yes", p_up), ("no", 1.0 - p_up)):
            if side in taken or prob < cfg.sniper_min_prob:
                continue
            if side == "yes":
                ask = book.best_yes_ask
                depth = book.depth_at("no", 100 - ask) if book.no else 0
            else:
                ask = 100 - book.best_yes_bid if book.yes else 100
                depth = book.depth_at("yes", 100 - ask) if book.yes else 0
            if not (1 <= ask <= 99) or depth <= 0:
                continue
            size = min(cfg.sniper_size, depth)
            fee = taker_fee_cents(ask, size, cfg.taker_fee_mult) / size
            ev = prob * 100.0 - ask - fee
            if ev < cfg.sniper_min_ev_cents:
                continue
            taken.add(side)
            return CrossExit(side, size, ask, f"snipe p={prob:.3f} ev={ev:.1f}c")
        return None
