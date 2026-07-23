"""Status/health HTTP endpoint (Railway healthcheck + human dashboard)."""

from __future__ import annotations

import time

from aiohttp import web


def make_app(bot) -> web.Application:
    async def status(_req: web.Request) -> web.Response:
        now = time.time()
        markets = {}
        for ticker, st in bot.active.items():
            book = bot.ws.books.get(ticker)
            markets[ticker] = {
                "coin": st.coin.symbol,
                "strike": st.info.strike,
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
        return web.json_response(body)

    async def stats(_req: web.Request) -> web.Response:
        import datetime as dt
        day_start = dt.datetime.combine(
            dt.date.today(), dt.time.min).timestamp()
        return web.json_response({
            "all_time": bot.journal.coin_stats(),
            "today": bot.journal.coin_stats(since_ts=day_start),
        })

    app = web.Application()
    app.router.add_get("/", status)
    app.router.add_get("/healthz", status)
    app.router.add_get("/stats", stats)
    return app


async def start_http(bot, port: int) -> web.AppRunner:
    runner = web.AppRunner(make_app(bot))
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    return runner
