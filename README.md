# Kalshi ⇄ Polymarket Arbitrage Finder

Scans **all open markets on both Kalshi and Polymarket**, matches the
overlapping ones, and flags cross-venue arbitrage: when the **YES ask on one
venue + the NO ask on the other is under $1.00** (e.g. under 95¢ combined),
buying both sides locks in the difference — exactly one side pays out $1.

With `--execute` it buys both legs **at the ask immediately** (buy-now)
using your API keys.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # then fill in your keys (only needed for --execute)
```

Scanning needs **no keys** — both venues' market data is public.

## Usage

```bash
# Find arbitrage with at least 5c of edge (combined cost under ~95c after fees)
python -m arb scan --min-edge 0.05

# See every overlapping market pair the matcher found (with current prices)
python -m arb matches

# Rescan every 30 seconds and also dump JSON
python -m arb scan --loop 30 --json opportunities.json

# LIVE trading: buy both legs at the ask, 10 contracts per opportunity.
# Only trades pairs you confirmed by hand in matches.yaml.
python -m arb scan --execute --size 10 --loop 30
```

### Key flags

| Flag | Default | Meaning |
|---|---|---|
| `--min-edge` | `0.05` | Net profit per contract required (0.05 = pair costs < ~95¢ after fees) |
| `--min-score` | `90` | Fuzzy title-match threshold (0–100) |
| `--min-volume` | `100` | Skip dead markets below this volume |
| `--size` | `10` | Contracts per leg when executing |
| `--allow-fuzzy-exec` | off | Let `--execute` trade unconfirmed fuzzy matches (risky) |

## How matching works

1. Pulls every open Kalshi market (paginated REST) and every active binary
   Yes/No Polymarket market (Gamma API).
2. Fuzzy-matches titles (rapidfuzz `token_sort_ratio`), with guards:
   numbers in the titles must agree (so "above 3.5%" can't match "above 4%")
   and close dates must be within `--max-days-apart` days.
3. Refreshes Polymarket best asks live from the CLOB before computing edges.
4. Edge = `1.00 − (leg1 ask + leg2 ask) − Kalshi taker fee`
   (fee = `⌈0.07·P·(1−P)⌉` per contract; Polymarket charges no trading fee).

## Execution safety

- **Confirmed pairs only by default.** Fuzzy title matches can pair markets
  whose resolution rules differ subtly (different data source, deadline,
  or threshold wording) — that turns an "arb" into a directional bet. Run
  `python -m arb matches`, verify the rules on both sites, then whitelist
  the pair in `matches.yaml`.
- **Polymarket leg first, fill-or-kill.** If it can't fill at your price it
  rejects atomically and the Kalshi leg is never sent — a miss leaves you
  flat, not one-legged. The Kalshi leg is a limit at the ask, so it can
  cross immediately but can never fill worse than the price you saw.
- If the Kalshi leg errors *after* Polymarket filled, the tool stops the
  loop and tells you to hedge manually.
- Each opportunity is executed at most once per run (no accidental
  re-buying every loop iteration).

## Credentials (`.env`)

| Var | What it is |
|---|---|
| `KALSHI_API_KEY_ID` | Key ID from kalshi.com → Account → API keys |
| `KALSHI_PRIVATE_KEY_PATH` | Path to the RSA private-key `.pem` Kalshi generated |
| `POLYMARKET_PRIVATE_KEY` | Polygon wallet private key holding your USDC |
| `POLYMARKET_FUNDER_ADDRESS` | Your Polymarket profile address (proxy wallet), if you use the web UI |
| `POLYMARKET_SIGNATURE_TYPE` | `1` email login, `2` browser wallet, `0` plain EOA |

Keep `.env` and `*.pem` out of git — they're already in `.gitignore`.

## Risks to understand before going live

- **Resolution mismatch is the #1 way this loses money.** "Under $1
  combined" is only an arb if both markets resolve identically.
- **Capital is tied up until resolution** on both venues; the edge is per
  contract, not annualized.
- Prices move: the ask you scanned can be gone by execution time (the FOK
  ordering above protects you from the worst case).
- Kalshi is US-regulated (CFTC); Polymarket has its own eligibility rules.
  Make sure you're allowed to trade on both.
