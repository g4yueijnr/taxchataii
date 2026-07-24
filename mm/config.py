"""Configuration: environment variables first, optional mm.yaml overrides.

Everything risk-related has a conservative default. DRY_RUN defaults ON —
the bot paper-trades until you explicitly set DRY_RUN=false.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    v = os.environ.get(name)
    return float(v) if v not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    return int(v) if v not in (None, "") else default


@dataclass
class CoinConfig:
    """Per-coin wiring: Kalshi series ticker + spot feed."""
    symbol: str                 # e.g. "DOGE"
    series_ticker: str          # e.g. "KXDOGE15M"
    spot_source: str            # "coinbase" | "binance" | "kraken"
    spot_symbol: str            # e.g. "DOGE-USD" (coinbase) / "dogeusdt" (binance)


# Default wiring for Kalshi's 15-minute crypto series. Spot sources are chosen
# to overlap with the CF Benchmarks index constituents (Coinbase/Kraken) where
# the coin is listed there; BNB only trades with real depth on Binance.
DEFAULT_COINS: dict[str, CoinConfig] = {
    "DOGE": CoinConfig("DOGE", "KXDOGE15M", "coinbase", "DOGE-USD"),
    "SOL": CoinConfig("SOL", "KXSOL15M", "coinbase", "SOL-USD"),
    "XRP": CoinConfig("XRP", "KXXRP15M", "coinbase", "XRP-USD"),
    # OKX for BNB: Coinbase/Kraken don't list it and binance.com/us websockets
    # are unreliable from US cloud hosts; OKX public market data isn't gated.
    "BNB": CoinConfig("BNB", "KXBNB15M", "okx", "BNB-USDT"),
    "BTC": CoinConfig("BTC", "KXBTC15M", "coinbase", "BTC-USD"),
    "ETH": CoinConfig("ETH", "KXETH15M", "coinbase", "ETH-USD"),
    "ZEC": CoinConfig("ZEC", "KXZEC15M", "kraken", "ZEC/USD"),
    # Kraken over Coinbase for NEAR: its BBO-triggered ticker keeps thin
    # coins fresh between trades.
    "NEAR": CoinConfig("NEAR", "KXNEAR15M", "kraken", "NEAR/USD"),
}


@dataclass
class Config:
    # --- credentials -----------------------------------------------------
    kalshi_api_key_id: str = ""
    kalshi_private_key_pem: bytes = b""

    # --- what to trade ---------------------------------------------------
    coins: list[CoinConfig] = field(default_factory=list)
    dry_run: bool = True
    sim_bankroll_dollars: float = 100.0   # virtual capital in dry-run mode

    # --- quoting ---------------------------------------------------------
    quote_size: int = 5           # contracts per side
    max_position: int = 20        # max net contracts per market (either sign)
    base_edge_cents: float = 1.0  # minimum half-spread beyond fees/buffers
    min_capture_cents: int = 2    # min distance between our bid and our ask
    as_vol_mult: float = 1.0      # adverse-selection buffer = mult * fair-value vol
                                  # (tightened so the maker actually competes
                                  #  for fills; 2.0 quoted so wide it never did)
    inventory_skew_cents: float = 2.0   # extra skew at full inventory
    improve_tick: bool = True     # step 1c inside the current best when profitable
    # Anchor quotes to the MARKET (book mid), not our independent fair —
    # quoting around our own opinion when it disagrees with the book leaves
    # us off-market and never filling. Fair still nudges the center (bounded)
    # and drives inventory skew.
    fair_lean_frac: float = 0.4        # fraction of the fair-vs-mid gap to lean
    max_fair_lean_cents: float = 6.0   # hard cap on that lean

    # --- stale-quote picker (mid-window latency taker) --------------------
    # OFF by default: measured -2.36c avg markout over many fills — it bets
    # our fair value beats the market's and it systematically loses. This is
    # a pure market maker unless you opt back in with MM_PICK_ENABLED=true.
    pick_enabled: bool = False
    pick_min_edge_cents: float = 4.0   # edge beyond taker fee + fv_vol buffer
    pick_cooldown_s: float = 8.0       # per market+side between picks
    pick_proxy_penalty_cents: float = 2.0  # extra edge required on proxy strikes
    pick_min_open_seconds: int = 90    # no picks early in a window: the crowd
                                       # prices the open drift before our model
    pick_fair_band: tuple = (15.0, 85.0)  # no picks at extremes: model error
                                          # dominates the binary's tails

    # --- timing guards (seconds before market close) ---------------------
    no_quote_seconds: int = 80    # stop posting new quotes (60s settle avg + buffer)
    flatten_seconds: int = 65     # start crossing out of inventory
    min_open_seconds: int = 5     # don't quote a window until it's this old

    # --- adverse-selection circuit breakers ------------------------------
    spot_stale_seconds: float = 3.0    # pull quotes if the spot feed goes quiet
    vol_spike_mult: float = 15.0       # pull quotes when 30s vol > mult * baseline
                                       # (high: a maker should quote through
                                       #  normal chop, only bail on real shocks)
    vol_spike_cooldown: float = 5.0    # seconds to stay out after a spike
    scratch_cents: int = 6             # cross out if fair moves this far against inventory
                                       # (raised: +18c scratch markouts showed we
                                       #  were realizing losses that then reverted)
    max_exit_slippage_cents: int = 10  # never exit further than this through fair;
                                       # settlement pays ~fair, so a worse exit is a donation
    pick_trend_guard_cents: float = 3.0  # no dip-side picks while fair fell this much in 30s

    # --- settlement sniper -----------------------------------------------
    sniper_enabled: bool = True
    sniper_window_s: int = 90          # active this close to settlement
    sniper_min_prob: float = 0.985     # probability floor to take a side
    sniper_min_ev_cents: float = 1.5   # EV after taker fee must clear this
    sniper_max_ev_cents: float = 12.0  # REJECT above this: a huge "edge" means
                                       # our model disagrees violently with the
                                       # market, and the market is right
    sniper_size: int = 10              # max contracts per snipe

    # --- fees (see mm/fees.py; override if Kalshi's schedule changes) ----
    taker_fee_mult: float = 0.07
    maker_fee_mult: float = 0.0175     # 25% of taker per July 2026 schedule

    # --- risk ------------------------------------------------------------
    max_gross_dollars: float = 200.0   # max total collateral at risk
    daily_loss_limit_dollars: float = 50.0
    min_balance_cents: int = 500       # halt if balance drops below this

    # --- per-coin circuit breaker ---------------------------------------
    coin_daily_loss_limit_dollars: float = 15.0  # bench a coin for the day

    # --- plumbing --------------------------------------------------------
    write_rate_per_sec: float = 4.0    # order create/cancel throttle
    discovery_interval: float = 15.0   # how often to look for the next window
    port: int = 8080                   # health/status HTTP port
    order_ttl_s: int = 120             # dead-man expiry on every resting order
    order_refresh_s: int = 90          # replace resting orders before TTL
    data_dir: str = "data"             # trade journal (SQLite) location

    @property
    def can_trade(self) -> bool:
        return bool(self.kalshi_api_key_id and self.kalshi_private_key_pem)


def load_config() -> Config:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    cfg = Config()
    cfg.kalshi_api_key_id = os.environ.get("KALSHI_API_KEY_ID", "")

    # Railway-friendly: accept the PEM either inline or as a file path.
    pem_inline = os.environ.get("KALSHI_PRIVATE_KEY", "")
    pem_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
    if pem_inline:
        cfg.kalshi_private_key_pem = pem_inline.replace("\\n", "\n").encode()
    elif pem_path and Path(pem_path).exists():
        cfg.kalshi_private_key_pem = Path(pem_path).read_bytes()

    coin_list = os.environ.get("MM_COINS", "DOGE,BNB,ZEC,NEAR")
    for sym in [c.strip().upper() for c in coin_list.split(",") if c.strip()]:
        if sym in DEFAULT_COINS:
            cfg.coins.append(DEFAULT_COINS[sym])
        else:
            # Unknown coin: assume Kalshi naming convention and Coinbase spot.
            cfg.coins.append(CoinConfig(sym, f"KX{sym}15M", "coinbase", f"{sym}-USD"))

    cfg.dry_run = _env_bool("DRY_RUN", True)
    cfg.quote_size = _env_int("MM_QUOTE_SIZE", cfg.quote_size)
    cfg.max_position = _env_int("MM_MAX_POSITION", cfg.max_position)
    cfg.base_edge_cents = _env_float("MM_BASE_EDGE_CENTS", cfg.base_edge_cents)
    cfg.min_capture_cents = _env_int("MM_MIN_CAPTURE_CENTS", cfg.min_capture_cents)
    cfg.no_quote_seconds = _env_int("MM_NO_QUOTE_SECONDS", cfg.no_quote_seconds)
    cfg.flatten_seconds = _env_int("MM_FLATTEN_SECONDS", cfg.flatten_seconds)
    cfg.max_gross_dollars = _env_float("MM_MAX_GROSS_DOLLARS", cfg.max_gross_dollars)
    cfg.daily_loss_limit_dollars = _env_float(
        "MM_DAILY_LOSS_LIMIT", cfg.daily_loss_limit_dollars)
    cfg.write_rate_per_sec = _env_float("MM_WRITE_RATE", cfg.write_rate_per_sec)
    cfg.taker_fee_mult = _env_float("MM_TAKER_FEE_MULT", cfg.taker_fee_mult)
    cfg.maker_fee_mult = _env_float("MM_MAKER_FEE_MULT", cfg.maker_fee_mult)
    cfg.pick_enabled = _env_bool("MM_PICK_ENABLED", cfg.pick_enabled)
    cfg.pick_min_edge_cents = _env_float("MM_PICK_MIN_EDGE_CENTS",
                                         cfg.pick_min_edge_cents)
    cfg.sniper_enabled = _env_bool("MM_SNIPER_ENABLED", cfg.sniper_enabled)
    cfg.sniper_min_prob = _env_float("MM_SNIPER_MIN_PROB", cfg.sniper_min_prob)
    cfg.sniper_size = _env_int("MM_SNIPER_SIZE", cfg.sniper_size)
    cfg.coin_daily_loss_limit_dollars = _env_float(
        "MM_COIN_DAILY_LOSS_LIMIT", cfg.coin_daily_loss_limit_dollars)
    cfg.data_dir = os.environ.get("MM_DATA_DIR", cfg.data_dir)
    cfg.port = _env_int("PORT", cfg.port)
    cfg.sim_bankroll_dollars = _env_float("MM_SIM_BANKROLL", cfg.sim_bankroll_dollars)
    if cfg.dry_run:
        # The paper account can't deploy more collateral than it has.
        cfg.max_gross_dollars = min(cfg.max_gross_dollars, cfg.sim_bankroll_dollars)
    return cfg
