# Kalshi 15-Minute Crypto Market Maker

Quotes both sides of Kalshi's 15-minute up/down crypto markets (DOGE, BNB,
SOL, XRP by default — any `KX*15M` series works) to capture the wide
bid/ask spreads on the alt-coin books, with the whole design built around
one fact: **your bid usually fills because the price just moved through
it.** Naive "bid 1¢ over, flip at the ask" bots donate money to whoever is
watching the spot feed. This bot *is* the one watching the spot feed.

## Why this can have edge (research summary)

- Kalshi crypto contracts settle on the **CF Benchmarks real-time index**
  (Coinbase/Kraken/Bitstamp/LMAX), averaged 1/sec over the **final 60
  seconds** of the window. Spot exchanges lead Kalshi's book by seconds.
- Documented profitable bots in 5/15-minute up/down markets all share the
  same shape: price the contract from live spot + realized vol, act only
  when the book deviates, and never hold into the settlement window.
- The alt-coin 15M books (DOGE/BNB & co.) carry 3–8¢ spreads vs 1–2¢ on
  BTC/ETH — enough to pay maker fees and adverse selection and keep some.

## How it defends against adverse selection

Every quote is priced off fair value from a live spot websocket
(Coinbase/Kraken — the settlement index constituents — or Binance for BNB),
never off the Kalshi book:

```
half_spread = base_edge                 (1¢ default)
            + maker_fee(price)          (~0.4¢ at mid-book)
            + 2 × fair_value_vol(3s)    (how far truth moves before we can cancel)
            + inventory_skew
```

`fair_value_vol` is recomputed every tick from EWMA realized vol and the
binary's local delta — when the coin gets fast, quotes automatically widen
or vanish. On top of that:

- **Cancel-first reconciliation** — stale quotes are pulled before new ones
  are placed; unchanged quotes keep their queue priority.
- **Vol-spike breaker** — 30s realized vol > 3.5× baseline ⇒ pull all
  quotes, 20s cooldown.
- **Stale-feed breaker** — no spot tick for 3s ⇒ pull all quotes.
- **Scratch rule** — fair value moves 3¢ through a fill ⇒ cross out
  immediately instead of hoping.
- **No-quote zone** — no new quotes in the last 150s of a window; inventory
  force-flattened by 100s out (settlement averaging starts at 60s; binary
  gamma near the strike makes maker quotes toxic there).
- **Kill switch** — daily loss limit, gross exposure cap, balance floor;
  trips cancel-all and stays down until restart.

## Run it

```bash
pip install -r requirements-mm.txt
python -m mm          # DRY RUN by default: paper-trades against the live tape
```

Watch `http://localhost:8080/healthz` — fair value vs book, quotes,
positions, paper P&L per market.

**Run days of dry-run first.** The paper P&L uses the real public trade
tape with no queue-priority assumption, so it's a fair (slightly
optimistic) preview. If paper P&L isn't positive after fees, live won't be.

## Go live

```bash
DRY_RUN=false KALSHI_API_KEY_ID=... KALSHI_PRIVATE_KEY_PATH=key.pem python -m mm
```

## Deploy on Railway

1. Push this repo to GitHub, create a Railway project from it — the
   `Dockerfile` + `railway.json` are picked up automatically.
2. Set variables: `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY` (paste the PEM
   itself; newlines can be `\n`), keep `DRY_RUN=true` for the first days,
   optionally `MM_COINS=DOGE,BNB,SOL,XRP`, `BINANCE_WS_BASE=wss://stream.binance.us:9443`
   if the region blocks binance.com.
3. Railway health-checks `/healthz`; the same URL is your dashboard.

## Tuning knobs (env)

| Var | Default | Meaning |
|---|---|---|
| `MM_COINS` | `DOGE,BNB,SOL,XRP` | Coins to quote (`BTC,ETH,ZEC,NEAR` also wired) |
| `MM_QUOTE_SIZE` | 5 | Contracts per side |
| `MM_MAX_POSITION` | 20 | Max net contracts per market |
| `MM_BASE_EDGE_CENTS` | 1.0 | Profit floor per side beyond fees/buffers |
| `MM_MIN_CAPTURE_CENTS` | 2 | Min gap between our bid and our ask |
| `MM_NO_QUOTE_SECONDS` | 150 | Stop quoting this close to settlement |
| `MM_FLATTEN_SECONDS` | 100 | Force-flatten inventory this close |
| `MM_MAX_GROSS_DOLLARS` | 200 | Collateral cap across all markets |
| `MM_DAILY_LOSS_LIMIT` | 50 | Daily realized loss ⇒ kill switch |
| `MM_WRITE_RATE` | 4 | Order writes/sec (match your Kalshi API tier) |
| `MM_TAKER_FEE_MULT` / `MM_MAKER_FEE_MULT` | 0.07 / 0.0175 | Verify at kalshi.com/fee-schedule |

## Honest expectations

- ZEC and NEAR **don't have Kalshi 15M series today** (BTC, ETH, SOL, XRP,
  BNB, HYPE, DOGE do). They're pre-wired: if `KXZEC15M`/`KXNEAR15M` ever
  list, adding them to `MM_COINS` just works.
- Wider spread = less competition, but also fewer fills. Expect many
  windows with zero trades; the P&L comes from being consistently on the
  right side of the fills you do get.
- Kalshi's basic API tier rate-limits writes; the bot throttles itself
  (`MM_WRITE_RATE`). Milliseconds matter most on *cancels*, which is why
  cancels always go first.
- This is real-money trading software. No bot is "guaranteed profit";
  this one is built so that when it's wrong it loses 1–3¢, and when it's
  right it collects the spread. Size accordingly, start tiny.
