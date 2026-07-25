"""Dashboard + /healthz for the arbitrage paper daemon."""

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
.g{color:#3fb950}.r{color:#f85149}.y{color:#d29922}.m{color:#8b949e}
.big{font-size:22px;color:#e6edf3} code{color:#8b949e;font-size:11px}
.tag{font-size:10px;padding:1px 5px;border-radius:3px}
.conf{background:#132e1a;color:#3fb950}.fuzz{background:#2e2413;color:#d29922}
"""


def _status(d) -> dict:
    now = time.time()
    b = d.book
    opps = []
    for o in d.last_opps[:40]:
        opps.append({
            "label": o.pair.label, "confirmed": o.pair.confirmed,
            "score": round(o.pair.score, 0),
            "kalshi": f"{o.kalshi_side.upper()} @ {o.kalshi_price*100:.0f}c",
            "poly": f"{o.poly_side_label} @ {o.poly_price*100:.0f}c",
            "edge_c": round(o.net_edge * 100, 1),
        })
    return {
        "status": "ok" if d.last_error is None else "degraded",
        "version": __version__, "mode": "PAPER",
        "uptime_s": round(now - d.started_at, 1),
        "equity": round(b.equity, 2), "bankroll": round(b.bankroll, 2),
        "locked_profit": round(b.locked_profit, 4),
        "confirmed_profit": round(b.confirmed_profit, 4),
        "deployed": round(b.deployed, 2), "available": round(b.available, 2),
        "fills": len(b.fills),
        "scans": d.scans,
        "last_scan_age_s": round(now - d.last_scan, 1) if d.last_scan else None,
        "kalshi_authed": d.kalshi_authed,
        "kalshi_note": d.kalshi_note, "poly_note": d.poly_note,
        "kalshi_markets": d.k_count, "poly_markets": d.p_count,
        "matched_pairs": d.pair_count, "confirmed_pairs": d.confirmed_count,
        "live_opps": len(d.last_opps),
        "last_error": d.last_error,
        "opps": opps,
    }


def _dash(d) -> str:
    s = _status(d)
    pcls = "g" if s["locked_profit"] > 0 else ("r" if s["locked_profit"] < 0 else "m")
    orows = ""
    for o in s["opps"]:
        tag = ("<span class='tag conf'>CONFIRMED</span>" if o["confirmed"]
               else f"<span class='tag fuzz'>FUZZY {o['score']:.0f}</span>")
        orows += (
            f"<tr><td>{tag} {html.escape(o['label'][:70])}</td>"
            f"<td>{html.escape(o['kalshi'])}</td>"
            f"<td>{html.escape(o['poly'])}</td>"
            f"<td class=g>+{o['edge_c']:.1f}c</td></tr>")
    frows = ""
    for f in reversed(d.book.fills[-60:]):
        tag = ("<span class='tag conf'>OK</span>" if f.confirmed
               else "<span class='tag fuzz'>FZ</span>")
        frows += (
            f"<tr><td>{time.strftime('%H:%M:%S', time.gmtime(f.ts))}</td>"
            f"<td>{tag} {html.escape(f.label[:52])}</td>"
            f"<td>K {f.kalshi_side.upper()} {f.kalshi_price*100:.0f}c / "
            f"P {f.poly_side} {f.poly_price*100:.0f}c</td>"
            f"<td>×{f.contracts}</td>"
            f"<td class=g>+${f.profit:.2f}</td></tr>")
    age = s["last_scan_age_s"]
    return f"""<!doctype html><html><head><meta charset=utf-8>
<meta http-equiv=refresh content=5><title>kalshi×poly arb</title>
<style>{_CSS}</style></head><body>
<h1>Kalshi &times; Polymarket — Cross-Venue Arbitrage</h1>
<div class=big>{s['mode']} &nbsp; ${s['equity']:.2f}
<span class=m>/ ${s['bankroll']:.0f}</span> &nbsp;
<span class={pcls}>+${s['locked_profit']:.2f} locked</span></div>
<p class=m>v{s['version']} · up {s['uptime_s']/60:.0f}m ·
confirmed +${s['confirmed_profit']:.2f} ·
deployed ${s['deployed']:.2f} / avail ${s['available']:.2f} ·
fills {s['fills']} · scans {s['scans']} ·
last scan {f'{age:.0f}s ago' if age is not None else '—'} ·
status <b class={'g' if s['status']=='ok' else 'r'}>{s['status']}</b></p>
<p class=m>markets: Kalshi {s['kalshi_markets']} · Poly {s['poly_markets']} ·
matched {s['matched_pairs']} ({s['confirmed_pairs']} confirmed) ·
live opps {s['live_opps']}</p>
{"" if s['kalshi_authed'] else "<p class=y>⚠ No Kalshi API keys — running on the anonymous rate tier (429s cap how many markets each scan can pull). Add KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY in Railway Variables for full coverage.</p>"}
<p><code>kalshi: {html.escape(s['kalshi_note'] or '—')}</code></p>
<p><code>poly: {html.escape(s['poly_note'] or '—')}</code></p>
{f"<p><code>last error: {html.escape(str(s['last_error'])[:200])}</code></p>" if s['last_error'] else ""}
<h2>LIVE OPPORTUNITIES (edge after Kalshi fees) — FUZZY = unverified settlement, treat as a lead not a lock</h2>
<table><tr><th>matched market</th><th>Kalshi leg</th><th>Poly leg</th><th>edge</th></tr>
{orows or '<tr><td colspan=4 class=m>no arbitrage above threshold right now (this is normal — cross-venue edges are rare and fleeting)</td></tr>'}</table>
<h2>PAPER FILLS (each hedged pair locks its edge at settlement)</h2>
<table><tr><th>time</th><th>market</th><th>legs</th><th>size</th><th>locked</th></tr>
{frows or '<tr><td colspan=5 class=m>no arbs booked yet</td></tr>'}</table>
</body></html>"""


def make_app(d):
    async def dash(_): return web.Response(text=_dash(d), content_type="text/html")
    async def hz(_): return web.json_response(_status(d))
    app = web.Application()
    app.router.add_get("/", dash)
    app.router.add_get("/healthz", hz)
    return app


async def start_http(d, port: int):
    runner = web.AppRunner(make_app(d))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    return runner
