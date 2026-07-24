"""Live dashboard + health endpoint for the triangular-arb bot."""

from __future__ import annotations

import time

from aiohttp import web

from . import __version__
from .engine import evaluate

_CSS = """
body{background:#0d1117;color:#c9d1d9;font:13px/1.5 ui-monospace,Menlo,monospace;
     margin:20px}
h1{font-size:16px;color:#e6edf3} h2{font-size:13px;color:#8b949e;margin:18px 0 6px}
table{border-collapse:collapse;width:100%;margin-bottom:8px}
th,td{border:1px solid #21262d;padding:4px 8px;text-align:right}
th{background:#161b22;color:#8b949e} td:first-child,th:first-child{text-align:left}
.g{color:#3fb950}.r{color:#f85149}.y{color:#d29922}.m{color:#8b949e}
.big{font-size:22px;color:#e6edf3}
"""


def _status(bot) -> dict:
    s = bot.executor.stats
    now = time.time()
    live = []
    for tri in bot.cfg.triangles:
        opp = evaluate(tri, bot.cfg.pairs, bot.books, bot.cfg.fee_rate, now,
                       bot.cfg.max_book_age_s)
        live.append({
            "triangle": tri.name,
            "best_net_edge_bps": round(opp.net_edge * 1e4, 2) if opp else None,
            "gross_edge_bps": round(opp.gross_edge * 1e4, 2) if opp else None,
            "direction": opp.direction if opp else None,
            "actionable": bool(opp and opp.net_edge >= bot.cfg.min_net_edge),
        })
    return {
        "status": "ok",
        "version": __version__,
        "dry_run": bot.cfg.dry_run,
        "uptime_s": round(now - bot.started_at, 1),
        "fee_rate_bps": round(bot.cfg.fee_rate * 1e4, 2),
        "min_net_edge_bps": round(bot.cfg.min_net_edge * 1e4, 2),
        "feed_msgs": bot.feed.msg_count,
        "evals": bot.eval_count,
        "equity_usdt": round(bot.equity, 4),
        "bankroll_usdt": bot.cfg.sim_bankroll,
        "stats": {
            "detected": s.detected, "executed": s.executed, "missed": s.missed,
            "realized_pnl_usdt": round(s.realized_pnl, 4),
            "fees_paid_usdt": round(s.fees_paid, 4),
            "by_triangle": s.by_triangle,
        },
        "live_triangles": live,
    }


def _dashboard(bot) -> str:
    st = _status(bot)
    s = st["stats"]
    live_rows = ""
    for t in sorted(st["live_triangles"],
                    key=lambda x: (x["best_net_edge_bps"] is None,
                                   -(x["best_net_edge_bps"] or -1e9))):
        net = t["best_net_edge_bps"]
        cls = "g" if (net is not None and t["actionable"]) else (
            "y" if net is not None and net > 0 else "m")
        live_rows += (
            f"<tr><td>{t['triangle']}</td><td>{t['direction'] or '—'}</td>"
            f"<td>{'—' if t['gross_edge_bps'] is None else t['gross_edge_bps']}</td>"
            f"<td class={cls}>{'—' if net is None else net}</td>"
            f"<td>{'YES' if t['actionable'] else ''}</td></tr>")

    fills = bot.journal.recent(50)
    trade_rows = ""
    for f in fills:
        pnl = f["pnl_usdt"]
        cls = "g" if (pnl or 0) > 0 else ("r" if (pnl or 0) < 0 else "m")
        trade_rows += (
            f"<tr><td>{time.strftime('%H:%M:%S', time.gmtime(f['ts']))}</td>"
            f"<td>{f['triangle']}</td><td>{f['direction']}</td>"
            f"<td>{round(f['detected_edge']*1e4,2)}</td>"
            f"<td>{'—' if f['exec_edge'] is None else round(f['exec_edge']*1e4,2)}</td>"
            f"<td class={cls}>{'—' if pnl is None else round(pnl,4)}</td>"
            f"<td class={'g' if f['outcome']=='executed' else 'm'}>{f['outcome']}</td></tr>")

    tri_rows = "".join(
        f"<tr><td>{name}</td><td>{v['executed']}</td>"
        f"<td class={'g' if v['pnl']>0 else 'r'}>{round(v['pnl'],4)}</td></tr>"
        for name, v in sorted(s["by_triangle"].items(),
                              key=lambda kv: kv[1]["pnl"], reverse=True))

    pnl_cls = "g" if s["realized_pnl_usdt"] > 0 else (
        "r" if s["realized_pnl_usdt"] < 0 else "m")
    return f"""<!doctype html><html><head><meta charset=utf-8>
<meta http-equiv=refresh content=3><title>tri arb</title><style>{_CSS}</style>
</head><body>
<h1>Triangular Arbitrage — Binance</h1>
<div class=big>{'PAPER' if st['dry_run'] else 'LIVE'} &nbsp;
${st['equity_usdt']:.4f} <span class=m>/ ${st['bankroll_usdt']:.0f}</span>
&nbsp; <span class={pnl_cls}>{s['realized_pnl_usdt']:+.4f} USDT</span></div>
<p class=m>v{st['version']} · up {st['uptime_s']/60:.0f}m · fee {st['fee_rate_bps']}bps/leg ·
threshold {st['min_net_edge_bps']}bps · {st['feed_msgs']} feed msgs ·
{st['evals']} evals · detected {s['detected']} · executed {s['executed']} ·
missed {s['missed']} · fees {s['fees_paid_usdt']:.4f}</p>
<h2>LIVE TRIANGLES <span class=m>(edge in bps = 0.01%; green = actionable
after fees; a healthy market usually shows all red/grey — that's fees eating
the gross edge)</span></h2>
<table><tr><th>triangle</th><th>dir</th><th>gross bps</th><th>NET bps</th>
<th>act</th></tr>{live_rows}</table>
<h2>BY TRIANGLE (executed P&amp;L)</h2>
<table><tr><th>triangle</th><th>executed</th><th>pnl USDT</th></tr>
{tri_rows or '<tr><td colspan=3 class=m>none executed yet</td></tr>'}</table>
<h2>CYCLES <span class=m>(newest first; edges in bps; 'missed' = edge gone by
execution — the honest arb killer)</span></h2>
<table><tr><th>time UTC</th><th>triangle</th><th>dir</th><th>detected</th>
<th>exec</th><th>pnl USDT</th><th>outcome</th></tr>
{trade_rows or '<tr><td colspan=7 class=m>no cycles yet</td></tr>'}</table>
</body></html>"""


def make_app(bot) -> web.Application:
    async def dashboard(_r): return web.Response(
        text=_dashboard(bot), content_type="text/html")

    async def healthz(_r): return web.json_response(_status(bot))

    app = web.Application()
    app.router.add_get("/", dashboard)
    app.router.add_get("/healthz", healthz)
    return app


async def start_http(bot, port: int) -> web.AppRunner:
    runner = web.AppRunner(make_app(bot))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    return runner
