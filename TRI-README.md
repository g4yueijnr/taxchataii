# Triangular Arbitrage Bot (Binance)

The retail-viable high-frequency strategy: on a **single** exchange, watch
triangles of pairs (e.g. USDT→BTC→ETH→USDT) over a websocket best-bid/offer
feed, and when walking the loop returns more than you started with **after
fees**, execute all three legs. One venue means no cross-exchange latency
race; opportunities last 1–5 seconds, which a bot catches and a human can't.

## The honest math (read this first)

Every leg pays a taker fee, so a 3-leg cycle costs **3× your fee rate**:

| Fee tier | Cost/cycle | Min gross edge to profit |
|---|---|---|
| 0.10% (retail) | 0.30% | ~0.4%+ (rare) |
| 0.02% (VIP / high volume) | 0.06% | ~0.1% (3–5× more opportunities) |
| Maker rebate | ~0 | tiny (but fills aren't guaranteed) |

**This is why fee tier is the whole game.** At standard 0.1% retail fees,
academic studies find almost all triangular opportunities are eaten by fees.
The dashboard shows this to you directly: gross edge vs net edge per triangle,
live. If everything is red, fees are winning — that's the normal, honest state
of an efficient market.

## Run it (paper by default)

```bash
pip install -r requirements-mm.txt
python -m tri           # DRY RUN: paper-trades vs the live Binance book
```

Open `http://localhost:8080/` — a live dashboard: per-triangle gross vs net
edge, evaluations/sec, executed cycles, and paper P&L. `/healthz` is the same
data as JSON.

## What the dashboard proves

- **LIVE TRIANGLES**: real-time net edge (in bps = 0.01%) for every triangle.
  Green = actionable after fees. You'll mostly see red/grey — that's fees
  eating the gross edge, exactly as the research predicts.
- **CYCLES**: every attempt. `missed` means the edge vanished between
  detection and execution (1–5s is all you get) — the honest arb killer that
  naive backtests ignore. This bot re-checks the live book at execution time
  and abandons dead edges instead of pretending it filled.

If paper P&L is positive after a real session, the edge survives *your* fees.
If not, it doesn't — and you'll know before risking a dollar.

## Deploy on Railway

Use `Dockerfile.tri` (set it as the Dockerfile path in Railway settings, or
rename it). Variables:

| Var | Default | Meaning |
|---|---|---|
| `DRY_RUN` | `true` | Keep true until paper P&L is convincingly positive |
| `TRI_FEE_RATE` | `0.001` | Your **actual** Binance taker fee (set this honestly!) |
| `TRI_MIN_NET_EDGE` | `0.0005` | Min net edge to fire (fraction; 0.0005 = 5bps) |
| `TRI_TRADE_NOTIONAL` | `50` | USDT deployed per cycle |
| `TRI_SIM_BANKROLL` | `100` | Paper bankroll |
| `BINANCE_WS_BASE` | binance.com | Set to `wss://stream.binance.us:9443` if geo-blocked |

## Going live

Live order execution is intentionally **not wired in this build** — it needs
signed Binance order placement with atomic 3-leg handling and partial-fill
recovery, which is only worth writing once paper trading proves the edge
clears your fees. Prove it on paper first; if it's real, that's the next
build.

## Why this and not the Kalshi bot

The Kalshi 15-min market maker (`mm/`) is well-built but its markets lack the
flow to fill a retail maker. Binance spot has millions in flow and hundreds of
book updates per second — the environment where a bot's speed is an actual
weapon. Same disciplined approach (paper first, honest markout/edge
measurement, full dashboard), pointed at a venue where the strategy can work.
