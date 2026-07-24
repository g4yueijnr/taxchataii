"""Dashboard + health for the Polymarket maker."""

from __future__ import annotations

import html
import time

from aiohttp import web

from . import __version__

_CSS = """
body{background:#0d1117;color:#c9d1d9;font:13px/1.5 ui-monospace,Menlo,monospace;margin:20px}
h1{font-size:16px;color:#e6edf3} h2{font-size:13px;color:#8b949e;margin:18px 0 6px}
table{border-collapse:collapse;width:100%;margin-bottom:8px}
th,td{border:1px solid #21262d;padding:4px 8px;text-align:right}
th{background:#161b22;color:#8b949e} td:first-child,th:first-child{text-align:left}
.g{color:#3fb950}.r{color:#f85149}.y{color:#d29922}.m{color:#8b949e}.big{font-size:22px;color:#e6edf3}
code{color:#8b949e;font-size:11px}
"""


def _status(bot) -> dict:
    now = time.time()
    mkts = []
    for token, m in bot.markets.items():
        book = bot.feed.books.get(token)
        r = bot.om.resting.get(token)
        pos = bot.positions.pos(token)
        mkts.append({
            "question": m.question, "volume": m.volume,
            "book_bid": book.best_bid if book and book.bids else None,
            "book_ask": book.best_ask if book and book.asks else None,
            "our_bid": r.bid if r else None, "our_ask": r.ask if r else None,
            "position": round(pos.shares, 1),
            "pnl_cents": round(pos.realized, 1),
        })
    mkts.sort(key=lambda x: x["volume"], reverse=True)
    return {
        "status": "halted" if bot.halted else "ok",
        "version": __version__, "dry_run": bot.cfg.dry_run,
        "uptime_s": round(now - bot.started_at, 1),
        "equity": round(bot.equity, 2), "bankroll": bot.cfg.sim_bankroll,
        "pnl_cents": round(bot.positions.net_pnl_cents, 1),
        "rebates_cents": round(bot.positions.rebates_cents, 1),
        "fills": bot.positions.fills,
        "feed_msgs": bot.feed.msg_count, "trades_seen": bot.feed.trade_count,
        "trade_sample": bot.feed.last_message,
        "markets": mkts,
    }


def _dash(bot) -> str:
    s = _status(bot)
    pcls = "g" if s["pnl_cents"] > 0 else ("r" if s["pnl_cents"] < 0 else "m")
    rows = ""
    for m in s["markets"]:
        pc = "g" if m["pnl_cents"] > 0 else ("r" if m["pnl_cents"] < 0 else "m")
        rows += (
            f"<tr><td>{html.escape(m['question'][:48])}</td>"
            f"<td>${m['volume']/1000:.0f}k</td>"
            f"<td>{m['book_bid'] or '—'} / {m['book_ask'] or '—'}</td>"
            f"<td><b>{m['our_bid'] or '—'} / {m['our_ask'] or '—'}</b></td>"
            f"<td>{m['position']:+.0f}</td>"
            f"<td class={pc}>{m['pnl_cents']:+.1f}c</td></tr>")
    fills = bot.journal.recent(60)
    frows = ""
    for f in fills:
        sc = "g" if f["side"] == "sell" else "r"
        frows += (
            f"<tr><td>{time.strftime('%H:%M:%S', time.gmtime(f['ts']))}</td>"
            f"<td>{html.escape((f['question'] or '')[:36])}</td>"
            f"<td class={sc}>{f['side'].upper()}</td>"
            f"<td>{f['price_cents']}c ×{f['size']:.0f}</td>"
            f"<td class=g>+{f['rebate_cents']:.2f}c</td>"
            f"<td>{f['realized_cents']:+.1f}c</td></tr>")
    return f"""<!doctype html><html><head><meta charset=utf-8>
<meta http-equiv=refresh content=3><title>polymarket mm</title><style>{_CSS}</style></head><body>
<h1>Polymarket Market Maker</h1>
<div class=big>{'PAPER' if s['dry_run'] else 'LIVE'} &nbsp; ${s['equity']:.2f}
<span class=m>/ ${s['bankroll']:.0f}</span> &nbsp;
<span class={pcls}>{s['pnl_cents']:+.1f}c</span></div>
<p class=m>v{s['version']} · up {s['uptime_s']/60:.0f}m · rebates +{s['rebates_cents']:.1f}c ·
fills {s['fills']} · feed msgs {s['feed_msgs']} · trades seen {s['trades_seen']} ·
status <b class={'r' if s['status']!='ok' else 'g'}>{s['status']}</b></p>
<p><code>ws msg: {html.escape((s['trade_sample'] or '')[:220])}</code></p>
<h2>MARKETS (by volume) — makers are PAID a rebate here, so tight spreads can profit</h2>
<table><tr><th>market</th><th>vol</th><th>book bid/ask</th><th>OUR bid/ask</th>
<th>pos</th><th>pnl</th></tr>
{rows or '<tr><td colspan=6 class=m>discovering markets…</td></tr>'}</table>
<h2>FILLS (newest first; rebate credited every fill)</h2>
<table><tr><th>time</th><th>market</th><th>side</th><th>price×size</th>
<th>rebate</th><th>realized</th></tr>
{frows or '<tr><td colspan=6 class=m>no fills yet</td></tr>'}</table>
</body></html>"""


def make_app(bot):
    async def dash(_): return web.Response(text=_dash(bot), content_type="text/html")
    async def hz(_): return web.json_response(_status(bot))
    app = web.Application()
    app.router.add_get("/", dash)
    app.router.add_get("/healthz", hz)
    return app


async def start_http(bot, port: int):
    runner = web.AppRunner(make_app(bot))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    return runner
