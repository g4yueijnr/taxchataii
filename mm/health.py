"""HTTP endpoints.

/          — human dashboard (auto-refreshes every 5s): equity, positions,
             live quotes vs fair, and every trade with entry price, size,
             fees, and markout.
/healthz   — same data as JSON (Railway healthcheck + machines).
/trades    — recent fills as JSON.
/stats     — per-coin aggregates (fills, fees, avg markout).
"""

from __future__ import annotations

import datetime as dt
import html
import time

from aiohttp import web

from . import __version__


def _status_body(bot) -> dict:
    now = time.time()
    markets = {}
    for ticker, st in bot.active.items():
        book = bot.ws.books.get(ticker)
        markets[ticker] = {
            "coin": st.coin.symbol,
            "strike": st.info.strike,
            "strike_is_proxy": st.strike_is_proxy,
            "seconds_to_close": round(st.info.close_ts - now, 1),
            "fair": round(st.last_fair, 2),
            "fv_vol": round(st.last_fv_vol, 2),
            "state": st.last_reason,
            "best_bid": book.best_yes_bid if book else None,
            "best_ask": book.best_yes_ask if book else None,
            "our_orders": {
                s: {"price": o.price, "size": o.size}
                for s, o in bot.om.orders_for(ticker).items()},
            "position": bot.positions.pos(ticker).net,
        }
    spot = {
        sym: {"price": s.price, "age_s": round(now - s.last_update, 2),
              "sigma_per_sec": round(s.vol.sigma_per_sec, 8)}
        for sym, s in bot.spots.states.items()}
    body = {
        "status": "halted" if bot.risk.halted else "ok",
        "version": __version__,
        "data_mode": bot.data_mode,
        "last_data_error": bot.last_data_error,
        "halt_reason": bot.risk.halt_reason,
        "dry_run": bot.cfg.dry_run,
        "uptime_s": round(now - bot.started_at, 1),
        "pnl": {
            "realized_cents": round(bot.positions.realized_cents, 1),
            "fees_cents": round(bot.positions.fees_cents, 1),
            "net_cents": round(bot.positions.net_pnl_cents, 1),
            "daily_cents": round(bot.risk.daily_pnl_cents, 1),
            "fills": bot.positions.fills,
        },
        "coins": {
            c.symbol: {"net_cents": round(bot.coin_net_cents(c.symbol), 1),
                       "benched": c.symbol in bot.risk.benched_coins}
            for c in bot.cfg.coins},
        "balance_cents": bot.risk.balance_cents,
        "spot": spot,
        "markets": markets,
    }
    if bot.cfg.dry_run:
        body["sim"] = {
            "bankroll_dollars": bot.cfg.sim_bankroll_dollars,
            "equity_dollars": round(bot.sim_equity_cents / 100, 2),
        }
    return body


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


def _fmt_ts(ts: float) -> str:
    return dt.datetime.utcfromtimestamp(ts).strftime("%H:%M:%S")


def _pnl_cls(v) -> str:
    if v is None:
        return "m"
    return "g" if v > 0 else ("r" if v < 0 else "m")


def _dashboard_html(bot) -> str:
    s = _status_body(bot)
    now = time.time()
    pnl = s["pnl"]
    sim = s.get("sim", {})
    equity = sim.get("equity_dollars")
    head = (
        f"<div class=big>{'PAPER' if s['dry_run'] else 'LIVE'} &nbsp; "
        + (f"${equity:.2f} <span class=m>/ ${sim['bankroll_dollars']:.0f} "
           f"bankroll</span>" if equity is not None else "")
        + f" &nbsp; <span class={_pnl_cls(pnl['net_cents'])}>"
          f"{pnl['net_cents']:+.1f}c net</span></div>"
        f"<p class=m>v{s['version']} · up {s['uptime_s']/60:.0f}m · "
        f"{pnl['fills']} fills · fees {pnl['fees_cents']:.1f}c · "
        f"kalshi data <b class={'g' if s['data_mode'] == 'websocket' else 'y'}>"
        f"{s['data_mode']}</b> · "
        + (f"<b class=r>DATA ERROR: {html.escape(s['last_data_error'][:160])}"
           f"</b> · " if s["last_data_error"] else "")
        + f"status <b class={'r' if s['status'] != 'ok' else 'g'}>{s['status']}"
          f"</b> {html.escape(s['halt_reason'])}</p>")

    spot_rows = "".join(
        f"<tr><td>{sym}</td><td>{v['price']:.6g}</td>"
        f"<td class={'r' if v['age_s'] > 5 or v['price'] <= 0 else 'g'}>"
        f"{v['age_s']:.1f}s</td><td>{v['sigma_per_sec']:.2e}</td></tr>"
        for sym, v in s["spot"].items())

    mkt_rows = ""
    for ticker, m in sorted(s["markets"].items()):
        oo = m["our_orders"]
        bid = oo.get("yes", {}).get("price", "—")
        ask = 100 - oo["no"]["price"] if "no" in oo else "—"
        mkt_rows += (
            f"<tr><td>{m['coin']} <span class=m>{ticker}</span></td>"
            f"<td>{m['strike']:.6g}{'*' if m['strike_is_proxy'] else ''}</td>"
            f"<td>{int(m['seconds_to_close'])}s</td><td>{m['fair']:.1f}</td>"
            f"<td class=y>{m['state']}</td>"
            f"<td>{m['best_bid'] or '—'} / "
            f"{m['best_ask'] if m['best_ask'] not in (None, 100) else '—'}</td>"
            f"<td><b>{bid} / {ask}</b></td>"
            f"<td class={_pnl_cls(m['position'])}>{m['position']:+d}</td></tr>")

    fills = bot.journal.recent_fills(60)
    trade_rows = ""
    for f in fills:
        mo = f["markout_cents"]
        yq = f["yes_equiv_qty"]
        trade_rows += (
            f"<tr><td>{_fmt_ts(f['ts'])}</td><td>{f['coin']}</td>"
            f"<td class={'g' if yq > 0 else 'r'}>"
            f"{'BUY' if yq > 0 else 'SELL'} YES×{f['count']}</td>"
            f"<td>{f['price_cents']}c <span class=m>{f['side']}</span></td>"
            f"<td>{'taker' if f['is_taker'] else 'maker'}</td>"
            f"<td>{f['fee_cents']:.1f}c</td>"
            f"<td>{f['fair_at_fill'] if f['fair_at_fill'] is None else round(f['fair_at_fill'], 1)}</td>"
            f"<td class={_pnl_cls(mo)}>"
            f"{'—' if mo is None else f'{mo:+.1f}c'}</td>"
            f"<td class=m>{html.escape(str(f['reason']))}</td></tr>")

    coin_rows = "".join(
        f"<tr><td>{c}</td><td class={_pnl_cls(v['net_cents'])}>"
        f"{v['net_cents']:+.1f}c</td>"
        f"<td>{'BENCHED' if v['benched'] else 'active'}</td></tr>"
        for c, v in s["coins"].items())

    return f"""<!doctype html><html><head><meta charset=utf-8>
<meta http-equiv=refresh content=5><title>kalshi mm</title>
<style>{_CSS}</style></head><body>
<h1>Kalshi 15m Market Maker</h1>{head}
<h2>SPOT FEEDS</h2>
<table><tr><th>coin</th><th>price</th><th>age</th><th>vol/s</th></tr>{spot_rows}</table>
<h2>MARKETS <span class=m>(* = proxy strike)</span></h2>
<table><tr><th>market</th><th>strike</th><th>closes</th><th>fair</th>
<th>state</th><th>book bid/ask</th><th>OUR bid/ask</th><th>pos</th></tr>{mkt_rows}</table>
<h2>PER-COIN P&amp;L (after fees)</h2>
<table><tr><th>coin</th><th>net</th><th>status</th></tr>{coin_rows}</table>
<h2>TRADES <span class=m>(newest first; markout = fair move 30s after fill,
+ is good)</span></h2>
<table><tr><th>time UTC</th><th>coin</th><th>trade</th><th>price</th>
<th>role</th><th>fee</th><th>fair@fill</th><th>markout</th><th>why</th></tr>
{trade_rows or '<tr><td colspan=9 class=m>no fills yet — the maker earns when someone crosses the spread; picks/snipes fire on mispricings</td></tr>'}</table>
</body></html>"""


def make_app(bot) -> web.Application:
    async def healthz(_req: web.Request) -> web.Response:
        return web.json_response(_status_body(bot))

    async def dashboard(_req: web.Request) -> web.Response:
        return web.Response(text=_dashboard_html(bot), content_type="text/html")

    async def trades(req: web.Request) -> web.Response:
        limit = min(int(req.query.get("limit", 100)), 1000)
        return web.json_response(bot.journal.recent_fills(limit))

    async def debug(_req: web.Request) -> web.Response:
        """Raw truth: websocket message counts, per-book depth, and a live
        unfiltered REST orderbook response — for diagnosing empty books."""
        out: dict = {
            "version": __version__,
            "data_mode": bot.data_mode,
            "ws_connected": bot.ws.connected,
            "ws_msg_counts": bot.ws.msg_counts,
            "ws_last_error": bot.ws.last_error,
            "ws_last_snapshot_raw": bot.ws.last_snapshot_raw,
            "ws_last_delta_raw": bot.ws.last_delta_raw,
            "last_data_error": bot.last_data_error,
            "subscribed_markets": sorted(bot.ws._tickers),
            "books": {
                t: {"yes_levels": len(b.yes), "no_levels": len(b.no),
                    "best_bid": b.best_yes_bid, "best_ask": b.best_yes_ask,
                    "age_s": round(time.time() - b.last_update, 1)
                    if b.last_update else None}
                for t, b in bot.ws.books.items()},
        }
        if bot.active:
            t = min(bot.active, key=lambda x: bot.active[x].info.close_ts)
            try:
                raw = await bot.rest._request(
                    "GET", f"/markets/{t}/orderbook", params={"depth": 10})
                out["rest_orderbook_raw"] = {"ticker": t, "response": raw}
            except Exception as e:
                out["rest_orderbook_raw"] = {"ticker": t, "error": str(e)}
        return web.json_response(out)

    async def stats(_req: web.Request) -> web.Response:
        day_start = dt.datetime.combine(dt.date.today(), dt.time.min).timestamp()
        return web.json_response({
            "all_time": bot.journal.coin_stats(),
            "today": bot.journal.coin_stats(since_ts=day_start),
        })

    app = web.Application()
    app.router.add_get("/", dashboard)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/trades", trades)
    app.router.add_get("/stats", stats)
    app.router.add_get("/debug", debug)
    return app


async def start_http(bot, port: int) -> web.AppRunner:
    runner = web.AppRunner(make_app(bot))
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    return runner
