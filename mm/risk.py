"""Global risk gate. Consulted before every quote cycle; trips a kill
switch (cancel everything, stop quoting) that only a restart resets."""

from __future__ import annotations

import datetime as dt
import logging

from .config import Config
from .execution import PositionBook

log = logging.getLogger("mm.risk")


class RiskManager:
    def __init__(self, cfg: Config, positions: PositionBook):
        self.cfg = cfg
        self.positions = positions
        self.halted = False
        self.halt_reason = ""
        self._day = dt.date.today()
        self._day_start_pnl = 0.0
        self.balance_cents: int | None = None   # updated by main loop when live
        self._benched: dict[str, dt.date] = {}  # coin -> day it was benched

    def _roll_day(self) -> None:
        today = dt.date.today()
        if today != self._day:
            self._day = today
            self._day_start_pnl = self.positions.net_pnl_cents

    @property
    def daily_pnl_cents(self) -> float:
        self._roll_day()
        return self.positions.net_pnl_cents - self._day_start_pnl

    def check(self) -> tuple[bool, str]:
        """(ok_to_quote, reason). Trips the halt latch on hard breaches."""
        if self.halted:
            return False, self.halt_reason
        self._roll_day()
        if self.daily_pnl_cents <= -self.cfg.daily_loss_limit_dollars * 100:
            self._halt(f"daily loss limit hit ({self.daily_pnl_cents/100:.2f}$)")
            return False, self.halt_reason
        if self.positions.gross_collateral_cents() >= self.cfg.max_gross_dollars * 100:
            return False, "gross exposure cap"
        if self.balance_cents is not None and self.balance_cents < self.cfg.min_balance_cents:
            self._halt(f"balance below floor ({self.balance_cents}c)")
            return False, self.halt_reason
        return True, "ok"

    def _halt(self, reason: str) -> None:
        self.halted = True
        self.halt_reason = reason
        log.error("KILL SWITCH: %s — cancelling all quotes, no further trading", reason)

    # ---------------------------------------------- per-coin circuit breaker

    def coin_allowed(self, coin: str, day_net_cents: float) -> bool:
        """Bench a coin for the rest of the day once it bleeds past its
        limit. Exits/flattens still run; only new quotes stop."""
        self._roll_day()
        if coin in self._benched and self._benched[coin] == self._day:
            return False
        if day_net_cents <= -self.cfg.coin_daily_loss_limit_dollars * 100:
            self._benched[coin] = self._day
            log.warning("benched %s for the day (net %.0fc)", coin, day_net_cents)
            return False
        return True

    @property
    def benched_coins(self) -> list[str]:
        return [c for c, d in self._benched.items() if d == self._day]
