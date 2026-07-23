"""Fair value for an up/down binary from live spot.

P(settle up) is modeled as driftless GBM: Phi(ln(S/K) / (sigma * sqrt(tau))).
Settlement is the average of ~60 one-per-second index prints over the final
minute, so effective settlement time is close_time - 30s and terminal variance
is reduced by ~1/3 (variance of the average of a Brownian path over the last
minute); we apply both corrections rather than pretend to more precision.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


class EwmaVol:
    """Per-second log-return volatility, EWMA over ~5 minutes of ticks.

    Also keeps a short 30s window to detect vol spikes vs baseline.
    """

    def __init__(self, halflife_s: float = 300.0):
        self.halflife_s = halflife_s
        self.var_per_sec: float = 0.0   # EWMA variance of 1s log returns
        self._last_price: float | None = None
        self._last_ts: float = 0.0
        self._recent: list[tuple[float, float]] = []  # (ts, logret^2/dt)

    def update(self, price: float, ts: float | None = None) -> None:
        ts = ts if ts is not None else time.time()
        if self._last_price is not None and price > 0 and self._last_price > 0:
            dt = max(ts - self._last_ts, 1e-3)
            r = math.log(price / self._last_price)
            inst_var = (r * r) / dt          # variance per second
            if self.var_per_sec == 0.0:
                self.var_per_sec = inst_var
            else:
                alpha = 1.0 - 0.5 ** (dt / self.halflife_s)
                self.var_per_sec += alpha * (inst_var - self.var_per_sec)
            self._recent.append((ts, inst_var))
            cutoff = ts - 30.0
            while self._recent and self._recent[0][0] < cutoff:
                self._recent.pop(0)
        self._last_price = price
        self._last_ts = ts

    @property
    def sigma_per_sec(self) -> float:
        return math.sqrt(self.var_per_sec) if self.var_per_sec > 0 else 0.0

    def spike_ratio(self) -> float:
        """30s realized variance / EWMA baseline. >1 means hotter than usual."""
        if not self._recent or self.var_per_sec <= 0:
            return 1.0
        recent = sum(v for _, v in self._recent) / len(self._recent)
        return recent / self.var_per_sec


@dataclass
class SpotState:
    """Latest spot price + vol for one coin, fed by the spot websocket."""
    symbol: str
    price: float = 0.0
    last_update: float = 0.0
    vol: EwmaVol = field(default_factory=EwmaVol)

    def on_tick(self, price: float, ts: float | None = None) -> None:
        ts = ts if ts is not None else time.time()
        self.vol.update(price, ts)
        self.price = price
        self.last_update = ts

    def is_stale(self, max_age_s: float) -> bool:
        return self.price <= 0 or (time.time() - self.last_update) > max_age_s


# Variance multiplier for settling on the mean of the final 60s of a Brownian
# path: Var[avg] ≈ Var[S(T-60)] + (60s leg) / 3. For tau >> 60s this is close
# to using tau_eff = tau - 30 with a small haircut; we fold it in directly.
SETTLE_AVG_WINDOW_S = 60.0


def prob_up(spot: float, strike: float, sigma_per_sec: float,
            seconds_to_close: float) -> float:
    """P(60s-average settlement >= strike) under driftless GBM."""
    if spot <= 0 or strike <= 0:
        return 0.5
    if seconds_to_close <= 0:
        return 1.0 if spot >= strike else 0.0
    # Effective variance to the settlement average.
    fixed_leg = max(seconds_to_close - SETTLE_AVG_WINDOW_S, 0.0)
    avg_leg = min(seconds_to_close, SETTLE_AVG_WINDOW_S)
    var = sigma_per_sec ** 2 * (fixed_leg + avg_leg / 3.0)
    if var <= 0:
        return 1.0 if spot >= strike else 0.0
    d = math.log(spot / strike) / math.sqrt(var)
    return _phi(d)


def fair_value_cents(spot: float, strike: float, sigma_per_sec: float,
                     seconds_to_close: float) -> float:
    return 100.0 * prob_up(spot, strike, sigma_per_sec, seconds_to_close)


def fair_value_vol_cents(spot: float, strike: float, sigma_per_sec: float,
                         seconds_to_close: float, horizon_s: float = 3.0) -> float:
    """Expected one-sigma move of the fair value (in cents) over horizon_s.

    This is the adverse-selection unit: how far the 'true' price walks in the
    time it takes us to notice and cancel. d(fair)/d(lnS) = phi(d)/sqrt(var),
    scaled by the spot's own sigma over the horizon.
    """
    if spot <= 0 or strike <= 0 or sigma_per_sec <= 0 or seconds_to_close <= 1:
        return 0.0
    fixed_leg = max(seconds_to_close - SETTLE_AVG_WINDOW_S, 0.0)
    avg_leg = min(seconds_to_close, SETTLE_AVG_WINDOW_S)
    var = sigma_per_sec ** 2 * (fixed_leg + avg_leg / 3.0)
    if var <= 0:
        return 0.0
    d = math.log(spot / strike) / math.sqrt(var)
    density = math.exp(-0.5 * d * d) / math.sqrt(2.0 * math.pi)
    dP_dlnS = density / math.sqrt(var)
    spot_move = sigma_per_sec * math.sqrt(horizon_s)
    return 100.0 * dP_dlnS * spot_move
