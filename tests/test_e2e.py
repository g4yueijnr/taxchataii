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

    # 3. Window nears settlement -> inventory force-flattened by crossing.
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
