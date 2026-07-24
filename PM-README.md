# Polymarket Market Maker

Quotes both sides of high-volume Polymarket CLOB markets (table tennis /
sports by default, ranked by volume) and captures the spread — in an
environment that **pays makers** instead of charging them.

## Why this can work where Kalshi couldn't

On Kalshi the maker *pays* a fee (~0.44¢/contract), so a 2¢ spread nets
near zero after costs — a losing grind. **Polymarket US pays the maker a
rebate** (taker-funded, ~−0.0125 mult) *and* runs a liquidity-reward
program for resting orders. So the same tight spread is:

```
edge = captured spread  +  maker rebate (credited)  −  adverse selection
```

The rebate flips the sign of the fee term from negative to positive. That's
the whole thesis for moving here.

## Run it (paper by default, no keys needed)

```bash
pip install -r requirements-mm.txt
python -m pm          # DRY RUN: paper-trades vs the live public CLOB
```

Dashboard at `http://localhost:8080/` — per-market book vs our quotes,
positions, rebates credited, and every fill. `/healthz` is the JSON.

## What to watch

- **feed msgs / trades seen** climbing = the CLOB websocket is delivering.
- **ws msg** on the dashboard shows a raw message so the exact wire format
  is verifiable on the first run (this is how the Kalshi wiring got debugged
  fast).
- **rebates** — credited on every maker fill; this is the structural edge.
- **pnl** per market and total. With the rebate, a positive total on tight
  spreads is the result Kalshi could never reach.

## Config (env)

| Var | Default | Meaning |
|---|---|---|
| `DRY_RUN` | `true` | Keep true until paper P&L is convincingly positive |
| `PM_CATEGORY` | `table-tennis` | Gamma tag to filter (`sports`, `""` = all) |
| `PM_MAX_MARKETS` | 12 | Quote the top-N by volume |
| `PM_MIN_VOLUME` | 5000 | Skip markets below this $ volume |
| `PM_SLUGS` | — | Comma-sep market slugs to force (overrides discovery) |
| `PM_QUOTE_SIZE` | 20 | Shares per side |
| `PM_MAX_POSITION` | 100 | Max net shares per market |
| `PM_MIN_SPREAD_TICKS` | 2 | Only make markets at least this wide |
| `PM_MAKER_REBATE_MULT` | 0.0125 | Maker rebate magnitude (US exchange) |

## Deploy on Railway

Use `Dockerfile.pm`. Keep `DRY_RUN=true`. Public data needs no keys; going
live later needs `POLYMARKET_PRIVATE_KEY` (+ funder/signature type) and the
US CLOB endpoints (swap the hosts in `pm/config.py` when Polymarket US API
access is set up).

## Honest note

Table-tennis / sports spreads are often just 1–2¢, so the bot skips
anything below `PM_MIN_SPREAD_TICKS`. Fills come from real counterparties,
so volume matters — the discovery ranks by it. The paper run tells you,
honestly, whether spread + rebate beats adverse selection on these markets.
If it does, that's a real edge; going live then adds the CLOB order
placement (not wired yet — prove it on paper first).
