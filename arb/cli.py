"""CLI: scan for cross-venue arbitrage, optionally execute at the ask.

Usage:
  python -m arb scan                       # one scan, report only
  python -m arb scan --loop 30             # rescan every 30s
  python -m arb scan --execute --size 10   # live-trade confirmed pairs
  python -m arb matches                    # just show matched market pairs
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .arbitrage import find_opportunities
from .execution import execute
from .kalshi import KalshiClient
from .matching import match_markets
from .polymarket import PolymarketClient


def load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass


def build_clients() -> tuple[KalshiClient, PolymarketClient]:
    key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    pem = Path(key_path).read_bytes() if key_path and Path(key_path).exists() else None
    kalshi = KalshiClient(api_key_id=os.getenv("KALSHI_API_KEY_ID"),
                          private_key_pem=pem)
    poly = PolymarketClient(
        private_key=os.getenv("POLYMARKET_PRIVATE_KEY"),
        funder=os.getenv("POLYMARKET_FUNDER_ADDRESS"),
        signature_type=int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0")))
    return kalshi, poly


def load_manual_matches(path: str) -> dict[str, str]:
    p = Path(path)
    if not p.exists():
        return {}
    import yaml
    data = yaml.safe_load(p.read_text()) or {}
    return {str(k): str(v) for k, v in (data.get("matches") or {}).items()}


def scan_once(kalshi: KalshiClient, poly: PolymarketClient, args,
              manual: dict[str, str]):
    print("Fetching markets...")
    k_markets = kalshi.fetch_open_markets(min_volume=args.min_volume, log=print)
    p_markets = poly.fetch_open_markets(min_volume=args.min_volume, log=print)
    print(f"Kalshi: {len(k_markets)} open markets | "
          f"Polymarket: {len(p_markets)} active binary markets")

    pairs = match_markets(k_markets, p_markets,
                          min_score=args.min_score,
                          max_days_apart=args.max_days_apart,
                          manual=manual)
    print(f"Matched {len(pairs)} overlapping markets "
          f"({sum(p.confirmed for p in pairs)} confirmed, rest fuzzy)")

    if args.fresh_prices and pairs:
        print("Refreshing Polymarket asks from CLOB...")
        poly.refresh_asks([p.poly for p in pairs])

    opps = find_opportunities(pairs, min_edge=args.min_edge)
    return pairs, opps


def cmd_matches(args) -> int:
    kalshi, poly = build_clients()
    manual = load_manual_matches(args.matches_file)
    pairs, _ = scan_once(kalshi, poly, args, manual)
    for pair in sorted(pairs, key=lambda p: -p.score):
        k, p = pair.kalshi, pair.poly
        print(f"\n{pair.label}")
        print(f"  Kalshi : {k.title}  (yes {k.yes_ask}c / no {k.no_ask}c)  {k.url}")
        print(f"  Poly   : {p.question}  (yes {p.yes_ask} / no {p.no_ask})  {p.url}")
    return 0


def cmd_scan(args) -> int:
    kalshi, poly = build_clients()
    manual = load_manual_matches(args.matches_file)

    if args.execute:
        if not kalshi.can_trade or not poly.can_trade:
            print("--execute needs Kalshi AND Polymarket credentials in .env "
                  "(see .env.example)", file=sys.stderr)
            return 1
        print(f"LIVE EXECUTION ON — size {args.size} contracts/opportunity, "
              f"{'fuzzy matches allowed' if args.allow_fuzzy_exec else 'confirmed pairs only'}")

    executed_keys: set[str] = set()
    while True:
        pairs, opps = scan_once(kalshi, poly, args, manual)

        if not opps:
            print(f"\nNo arbitrage above {args.min_edge * 100:.0f}c edge right now.")
        else:
            print(f"\n=== {len(opps)} ARBITRAGE OPPORTUNITIES "
                  f"(edge >= {args.min_edge * 100:.0f}c after fees) ===")
            for i, opp in enumerate(opps, 1):
                print(f"\n#{i} {opp.describe()}")

        if args.json:
            Path(args.json).write_text(json.dumps([{
                "kalshi_ticker": o.pair.kalshi.ticker,
                "kalshi_url": o.pair.kalshi.url,
                "poly_question": o.pair.poly.question,
                "poly_url": o.pair.poly.url,
                "confirmed_match": o.pair.confirmed,
                "match_score": o.pair.score,
                "direction": o.direction,
                "kalshi_side": o.kalshi_side,
                "kalshi_price": o.kalshi_price,
                "poly_side": o.poly_side_label,
                "poly_price": o.poly_price,
                "gross_cost": round(o.gross_cost, 4),
                "kalshi_fee": o.kalshi_fee,
                "net_edge": round(o.net_edge, 4),
            } for o in opps], indent=2))
            print(f"\nWrote {args.json}")

        if args.execute:
            for opp in opps:
                if not opp.pair.confirmed and not args.allow_fuzzy_exec:
                    print(f"\nSkipping unconfirmed fuzzy match "
                          f"(add to matches.yaml to trade): {opp.pair.label}")
                    continue
                key = f"{opp.pair.kalshi.ticker}:{opp.direction}"
                if key in executed_keys:
                    continue
                print(f"\nEXECUTING: {opp.pair.label}")
                res = execute(opp, kalshi, poly, contracts=args.size)
                executed_keys.add(key)
                if res.error and "hedge manually" in res.error:
                    print("Stopping loop after one-sided fill — check positions.")
                    return 2

        if not args.loop:
            return 0
        print(f"\n-- sleeping {args.loop}s --")
        time.sleep(args.loop)


def main(argv=None) -> int:
    load_env()
    ap = argparse.ArgumentParser(prog="arb",
                                 description="Kalshi <-> Polymarket arbitrage finder")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--min-edge", type=float, default=0.05,
                       help="min net edge in dollars, 0.05 = combined cost under ~95c (default 0.05)")
        p.add_argument("--min-score", type=float, default=90,
                       help="min fuzzy title match score 0-100 (default 90)")
        p.add_argument("--max-days-apart", type=float, default=3,
                       help="max days between the two markets' close dates (default 3)")
        p.add_argument("--min-volume", type=float, default=100,
                       help="ignore markets with volume below this (default 100)")
        p.add_argument("--matches-file", default="matches.yaml",
                       help="YAML file of hand-confirmed market pairs")
        p.add_argument("--fresh-prices", action="store_true", default=True,
                       help="re-pull live asks from the Polymarket CLOB (default on)")
        p.add_argument("--no-fresh-prices", dest="fresh_prices", action="store_false")

    ps = sub.add_parser("scan", help="find (and optionally execute) arbitrage")
    common(ps)
    ps.add_argument("--execute", action="store_true",
                    help="LIVE: buy both legs at the ask when an arb is found")
    ps.add_argument("--size", type=int, default=10,
                    help="contracts per leg when executing (default 10)")
    ps.add_argument("--allow-fuzzy-exec", action="store_true",
                    help="allow executing fuzzy-matched pairs (RISKY: verify "
                         "resolution rules match first)")
    ps.add_argument("--loop", type=int, default=0, metavar="SECONDS",
                    help="rescan continuously every N seconds")
    ps.add_argument("--json", metavar="FILE",
                    help="also write opportunities to a JSON file")
    ps.set_defaults(func=cmd_scan)

    pm = sub.add_parser("matches", help="list matched overlapping markets")
    common(pm)
    pm.set_defaults(func=cmd_matches)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
