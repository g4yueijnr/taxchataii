"""End-to-end lifecycle test: drives the real Bot (dry-run) through the
complete trade cycle with synthetic feeds — the machinery a live 6-hour run
would exercise, verified in milliseconds:

  warmup -> maker quotes -> tape fill -> journal row -> position ->
  forced flatten before settlement -> sniper take near close -> settlement

Only the network transports (websockets/REST) are excluded; those are
proven separately by the live deployment's /healthz.
"""

import asyncio
import time

from mm.config import Config, CoinConfig
from mm.main import ActiveMarket, Bot
from mm.strategy import MarketInfo

TICKER = "KXDOGE15M-E2E"


def make_bot(tmp_path):
    cfg = Config()
    cfg.coins = [CoinConfig("DOGE", "KXDOGE15M", "coinbase", "DOGE-USD")]
    cfg.data_dir = str(tmp_path)
    cfg.dry_run = True
    return Bot(cfg)


def warm_spot(bot, now, price=0.1):
    spot = bot.spots.state("DOGE")
    for i in range(40):
        spot.on_tick(price * (1 + 0.0001 * ((-1) ** i)), now - 41 + i)
    spot.on_tick(price, now)
    return spot


def test_full_trade_lifecycle(tmp_path):
    bot = make_bot(tmp_path)
    now = time.time()
    info = MarketInfo(TICKER, 0.1, now + 600, now - 300)
    bot.active[TICKER] = ActiveMarket(bot.cfg.coins[0], info)
    bot._ticker_coin[TICKER] = "DOGE"
    warm_spot(bot, now)

    book = bot.ws.book(TICKER)
    book.apply_snapshot({"yes": [[30, 50]], "no": [[30, 50]]})  # 30 bid / 70 ask

    # 1. Maker quotes appear on both sides.
    asyncio.run(bot._eval_once())
    orders = bot.om.orders_for(TICKER)
    assert "yes" in orders and "no" in orders
    bid = orders["yes"].price
    assert 30 < bid < 50 < 100 - orders["no"].price

    # 2. The public tape trades through our bid -> maker fill, journaled.
    bot.om.on_public_trade({"market_ticker": TICKER, "count": 5,
                            "yes_price": bid - 1, "taker_side": "no"})
    assert bot.positions.pos(TICKER).net == 5
    assert bot.positions.pos(TICKER).avg_entry == float(bid)
    assert bot.journal.coin_stats()["DOGE"]["fills"] == 1

    # 3. Window nears settlement -> inventory force-flattened by crossing
    #    (bid within the exit-slippage cap of fair, so the cross is taken).
    book.apply_snapshot({"yes": [[45, 50]], "no": [[30, 50]]})
    info.close_ts = now + 80
    bot.active[TICKER].dirty = True
    asyncio.run(bot._eval_once())
    assert bot.positions.pos(TICKER).net == 0
    assert bot.positions.realized_cents != 0.0   # exit realized P&L
    assert bot.journal.coin_stats()["DOGE"]["fills"] == 2

    # 4. Final minute: settlement is ~decided up, a stale 92c offer remains
    #    -> sniper takes it.
    info.close_ts = now + 30
    st = bot.active[TICKER]
    warm_spot(bot, now, price=0.102)
    tracker = bot.sniper.tracker(info)
    for i in range(30):
        tracker.samples[i] = 0.102
    book.apply_snapshot({"yes": [[5, 20]], "no": [[8, 50]]})   # ask 92c
    st.dirty = True
    asyncio.run(bot._eval_once())
    assert st.sniped
    pos = bot.positions.pos(TICKER)
    assert pos.net == bot.cfg.sniper_size and pos.avg_entry == 92.0

    # 5. Market settles YES -> residual position realized at $1.
    before = bot.positions.realized_cents
    bot.positions.settle(TICKER, "yes")
    assert bot.positions.realized_cents - before == 8.0 * bot.cfg.sniper_size

    # 6. Equity math: bankroll + net P&L, and the journal saw every fill.
    assert bot.sim_equity_cents == 100 * 100 + bot.positions.net_pnl_cents
    assert bot.journal.coin_stats()["DOGE"]["fills"] == 3
    bot.journal.close()


def test_risk_halt_cancels_quotes(tmp_path):
    bot = make_bot(tmp_path)
    now = time.time()
    info = MarketInfo(TICKER, 0.1, now + 600, now - 300)
    bot.active[TICKER] = ActiveMarket(bot.cfg.coins[0], info)
    bot._ticker_coin[TICKER] = "DOGE"
    warm_spot(bot, now)
    book = bot.ws.book(TICKER)
    book.apply_snapshot({"yes": [[30, 50]], "no": [[30, 50]]})
    asyncio.run(bot._eval_once())
    assert bot.om.orders_for(TICKER)

    # Trip the daily loss limit -> kill switch -> all quotes cancelled.
    bot.positions.realized_cents = -bot.cfg.daily_loss_limit_dollars * 100 - 1
    bot.active[TICKER].dirty = True
    asyncio.run(bot._eval_once())
    assert bot.risk.halted
    assert not bot.om.orders_for(TICKER)
    bot.journal.close()


def test_dashboard_renders_with_trades(tmp_path):
    from mm.health import _dashboard_html, _status_body
    bot = make_bot(tmp_path)
    now = time.time()
    info = MarketInfo(TICKER, 0.1, now + 600, now - 300)
    bot.active[TICKER] = ActiveMarket(bot.cfg.coins[0], info)
    bot._ticker_coin[TICKER] = "DOGE"
    warm_spot(bot, now)
    book = bot.ws.book(TICKER)
    book.apply_snapshot({"yes": [[30, 50]], "no": [[30, 50]]})
    asyncio.run(bot._eval_once())
    bid = bot.om.orders_for(TICKER)["yes"].price
    bot.om.on_public_trade({"market_ticker": TICKER, "count": 5,
                            "yes_price": bid - 1, "taker_side": "no"})

    page = _dashboard_html(bot)
    assert "PAPER" in page and "DOGE" in page and "maker" in page
    assert f"{bid}c" in page                      # the fill's entry price
    body = _status_body(bot)
    assert body["version"] and body["pnl"]["fills"] == 1
    fills = bot.journal.recent_fills(10)
    assert fills[0]["price_cents"] == bid and fills[0]["count"] == 5
    bot.journal.close()


def test_rest_fallback_seeds_book_and_tape(tmp_path):
    """When the Kalshi websocket can't deliver (keyless dry run), REST
    polling must populate the book and replay only NEW tape into the sim."""
    import datetime as _dt

    bot = make_bot(tmp_path)
    now = time.time()
    info = MarketInfo(TICKER, 0.1, now + 600, now - 300)
    bot.active[TICKER] = ActiveMarket(bot.cfg.coins[0], info)
    bot._ticker_coin[TICKER] = "DOGE"
    warm_spot(bot, now)

    def iso(ts):
        return _dt.datetime.utcfromtimestamp(ts).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z")

    calls = {"n": 0}
    old_trade = {"created_time": iso(now - 60), "count": 7,
                 "yes_price": 41, "taker_side": "no"}
    new_trade = {"created_time": iso(now + 1), "count": 3,
                 "yes_price": 41, "taker_side": "no"}

    async def fake_orderbook(ticker, depth=20):
        return {"yes": [[40, 25]], "no": [[52, 30]]}

    async def fake_trades(ticker, limit=50):
        calls["n"] += 1
        return [new_trade, old_trade] if calls["n"] > 1 else [old_trade]

    bot.rest.get_orderbook = fake_orderbook
    bot.rest.get_trades = fake_trades

    # Poll 1: book seeded, history NOT replayed (cursor established).
    asyncio.run(bot._sync_market_rest(TICKER))
    book = bot.ws.book(TICKER)
    assert book.best_yes_bid == 40 and book.best_yes_ask == 48
    assert bot.positions.pos(TICKER).net == 0

    # Quote off the freshly seeded book, then poll 2 delivers a new trade
    # through our bid -> sim maker fill.
    asyncio.run(bot._eval_once())
    our_bid = bot.om.orders_for(TICKER)["yes"].price
    assert our_bid >= 41  # improves on the 40 book bid within edge budget
    asyncio.run(bot._sync_market_rest(TICKER))
    assert bot.positions.pos(TICKER).net == 3
    assert bot.positions.pos(TICKER).avg_entry == float(our_bid)

    # Poll 3 with no new trades: nothing double-counted.
    asyncio.run(bot._sync_market_rest(TICKER))
    assert bot.positions.pos(TICKER).net == 3
    assert bot.data_mode == "rest_poll"
    bot.journal.close()
