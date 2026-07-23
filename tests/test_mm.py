import asyncio
import time

from mm.config import Config
from mm.execution import PositionBook
from mm.fees import maker_fee_per_contract, taker_fee_cents
from mm.model import EwmaVol, SpotState, fair_value_cents, fair_value_vol_cents, prob_up
from mm.orderbook import Book
from mm.sim import SimOrderManager
from mm.strategy import MarketInfo, QuoteEngine


def make_spot(price=0.1, sigma_per_sec=1e-4):
    s = SpotState("DOGE")
    s.price = price
    s.last_update = time.time()
    s.vol.var_per_sec = sigma_per_sec ** 2
    return s


def make_mkt(t_left=600.0, strike=0.1, now=None, open_age=300.0):
    now = now or time.time()
    return MarketInfo("KXDOGE15M-TEST", strike, now + t_left, now - open_age), now


# ------------------------------------------------------------------- fees

def test_taker_fee_peaks_at_50c():
    assert taker_fee_cents(50, 1) == 2      # ceil(1.75)
    assert taker_fee_cents(50, 100) == 175
    assert taker_fee_cents(95, 100) == 34   # ceil(33.25)
    assert maker_fee_per_contract(50) < maker_fee_per_contract(50, 0.07)


# ------------------------------------------------------------------ model

def test_prob_up_basics():
    assert prob_up(0.1, 0.1, 1e-4, 600) == 0.5  # at strike
    assert prob_up(0.11, 0.1, 1e-4, 600) > 0.95  # far above
    assert prob_up(0.11, 0.1, 1e-4, 0) == 1.0    # expired above
    assert prob_up(0.09, 0.1, 1e-4, 0) == 0.0


def test_fair_value_vol_positive_near_strike():
    v = fair_value_vol_cents(0.1, 0.1, 1e-4, 600)
    assert 0.5 < v < 20
    # Deep ITM: binary delta collapses, so does adverse-selection risk.
    assert fair_value_vol_cents(0.2, 0.1, 1e-4, 600) < 0.01


def test_ewma_vol_updates():
    v = EwmaVol()
    t = 1000.0
    for i in range(100):
        v.update(100.0 * (1 + 0.0001 * (i % 2)), t + i)
    assert v.sigma_per_sec > 0


# -------------------------------------------------------------- orderbook

def test_book_snapshot_delta():
    b = Book("T")
    b.apply_snapshot({"yes": [[40, 100], [41, 50]], "no": [[57, 80]]})
    assert b.best_yes_bid == 41
    assert b.best_yes_ask == 43
    assert b.spread == 2
    b.apply_delta({"side": "yes", "price": 41, "delta": -50})
    assert b.best_yes_bid == 40


# --------------------------------------------------------------- strategy

def test_quotes_symmetric_around_fair():
    cfg = Config()
    eng = QuoteEngine(cfg)
    mkt, now = make_mkt()
    d = eng.compute(mkt, Book(mkt.ticker), make_spot(), 0, None, now)
    assert d.reason == "quoting"
    assert 49 < d.fair < 51
    sides = {o.side: o for o in d.desired}
    assert set(sides) == {"yes", "no"}
    bid = sides["yes"].price
    ask = 100 - sides["no"].price
    assert bid < d.fair < ask
    assert ask - bid >= cfg.min_capture_cents
    # Half-spread must cover fees + adverse-selection buffer.
    assert d.fair - bid >= cfg.base_edge_cents + 2 * d.fv_vol - 1


def test_no_quotes_near_close_and_flatten():
    cfg = Config()
    eng = QuoteEngine(cfg)
    mkt, now = make_mkt(t_left=120)
    d = eng.compute(mkt, Book(mkt.ticker), make_spot(), 0, None, now)
    assert d.reason == "near_close" and not d.desired

    mkt, now = make_mkt(t_left=80)
    d = eng.compute(mkt, Book(mkt.ticker), make_spot(), 5, 40.0, now)
    assert not d.desired
    assert d.crosses and d.crosses[0].side == "no" and d.crosses[0].size == 5


def test_stale_spot_pulls_quotes():
    cfg = Config()
    eng = QuoteEngine(cfg)
    mkt, now = make_mkt()
    spot = make_spot()
    spot.last_update = now - 10
    d = eng.compute(mkt, Book(mkt.ticker), spot, 0, None, now)
    assert d.reason == "spot_stale" and not d.desired


def test_scratch_crosses_out_losing_inventory():
    cfg = Config()
    eng = QuoteEngine(cfg)
    mkt, now = make_mkt(t_left=300)
    spot = make_spot(price=0.0995)   # fair collapses well below entry
    d = eng.compute(mkt, Book(mkt.ticker), spot, 5, 48.0, now)
    assert d.crosses and d.crosses[0].reason == "scratch"
    assert d.crosses[0].side == "no" and d.crosses[0].size == 5


def test_inventory_caps_size():
    cfg = Config()
    eng = QuoteEngine(cfg)
    mkt, now = make_mkt()
    d = eng.compute(mkt, Book(mkt.ticker), make_spot(), cfg.max_position, 50.0, now)
    sides = {o.side for o in d.desired}
    assert "yes" not in sides       # can't add to a full long
    assert "no" in sides            # but can quote the reducing side


def test_never_cross_the_book():
    cfg = Config()
    eng = QuoteEngine(cfg)
    mkt, now = make_mkt()
    book = Book(mkt.ticker)
    # Tight book straddling fair: 49 bid / 51 ask.
    book.apply_snapshot({"yes": [[49, 10]], "no": [[49, 10]]})
    d = eng.compute(mkt, book, make_spot(), 0, None, now)
    for o in d.desired:
        if o.side == "yes":
            assert o.price < book.best_yes_ask
        else:
            assert 100 - o.price > book.best_yes_bid


# -------------------------------------------------------------- positions

def test_position_book_round_trip():
    pb = PositionBook()
    pb.on_fill("T", "yes", "buy", 5, 40, 60, fee=2.0)
    assert pb.pos("T").net == 5 and pb.pos("T").avg_entry == 40.0
    # Exit by buying NO at 58 == selling YES at 42.
    pb.on_fill("T", "no", "buy", 5, 42, 58, fee=2.0)
    assert pb.pos("T").net == 0
    assert pb.realized_cents == 10.0
    assert pb.net_pnl_cents == 6.0


def test_position_book_settlement():
    pb = PositionBook()
    pb.on_fill("T", "yes", "buy", 3, 40, 60, fee=0.0)
    pb.settle("T", "yes")
    assert pb.realized_cents == 180.0
    pb.on_fill("T2", "no", "buy", 2, 70, 30, fee=0.0)  # short YES at 70
    pb.settle("T2", "no")
    assert pb.realized_cents == 180.0 + 140.0


# -------------------------------------------------------------------- sim

def test_sim_maker_fill_from_tape():
    cfg = Config()
    pb = PositionBook()
    om = SimOrderManager(cfg, pb)
    from mm.strategy import DesiredOrder
    asyncio.run(om.reconcile("T", [DesiredOrder("yes", 42, 5)]))
    # Tape: taker sells YES at 41 (through our 42 bid) -> we fill at 42.
    om.on_public_trade({"market_ticker": "T", "count": 3, "yes_price": 41,
                        "taker_side": "no"})
    assert pb.pos("T").net == 3 and pb.pos("T").avg_entry == 42.0
    assert om.orders_for("T")["yes"].size == 2
    # A trade above our bid must NOT fill us.
    om.on_public_trade({"market_ticker": "T", "count": 3, "yes_price": 45,
                        "taker_side": "no"})
    assert pb.pos("T").net == 3


def test_sim_reconcile_replaces_on_price_change():
    cfg = Config()
    om = SimOrderManager(cfg, PositionBook())
    from mm.strategy import DesiredOrder
    asyncio.run(om.reconcile("T", [DesiredOrder("yes", 42, 5)]))
    oid1 = om.orders_for("T")["yes"].order_id
    asyncio.run(om.reconcile("T", [DesiredOrder("yes", 42, 5)]))
    assert om.orders_for("T")["yes"].order_id == oid1   # unchanged -> kept
    asyncio.run(om.reconcile("T", [DesiredOrder("yes", 43, 5)]))
    assert om.orders_for("T")["yes"].order_id != oid1   # repriced -> replaced


# --------------------------------------------------------------- settlement

def test_settlement_tracker_decided():
    from mm.settlement import SettlementTracker
    tr = SettlementTracker(close_ts=1000.0)
    # 59 of 60 samples locked in well above strike: outcome ~certain.
    tr.samples = {i: 0.102 for i in range(59)}
    assert tr.prob_up(0.1, 0.102, 1e-4) > 0.999
    # All samples in and below strike: fully decided down.
    tr.samples = {i: 0.099 for i in range(60)}
    assert tr.prob_up(0.1, 0.102, 1e-4) == 0.0


def test_sniper_takes_cheap_certainty():
    from mm.settlement import Sniper
    cfg = Config()
    sn = Sniper(cfg)
    now = time.time()
    mkt = MarketInfo("T", 0.1, now + 30, now - 870)
    sn.tracker(mkt).samples = {i: 0.102 for i in range(30)}
    spot = make_spot(price=0.102)
    book = Book("T")
    book.apply_snapshot({"yes": [[5, 20]], "no": [[8, 50]]})   # ask 92c
    take = sn.evaluate(mkt, book, spot, strike_is_proxy=False, now=now)
    assert take is not None and take.side == "yes"
    assert take.limit_price == 92 and take.size == cfg.sniper_size
    # Same side never taken twice.
    assert sn.evaluate(mkt, book, spot, strike_is_proxy=False, now=now) is None


def test_sniper_refuses_proxy_strike_and_fair_price():
    from mm.settlement import Sniper
    cfg = Config()
    sn = Sniper(cfg)
    now = time.time()
    mkt = MarketInfo("T", 0.1, now + 30, now - 870)
    sn.tracker(mkt).samples = {i: 0.102 for i in range(30)}
    spot = make_spot(price=0.102)
    book = Book("T")
    book.apply_snapshot({"yes": [[5, 20]], "no": [[8, 50]]})
    assert sn.evaluate(mkt, book, spot, strike_is_proxy=True, now=now) is None
    # Fully-priced book (ask 99c): no EV left, no take.
    book2 = Book("T")
    book2.apply_snapshot({"yes": [[5, 20]], "no": [[1, 50]]})
    sn2 = Sniper(cfg)
    sn2.tracker(mkt).samples = {i: 0.102 for i in range(30)}
    assert sn2.evaluate(mkt, book2, spot, strike_is_proxy=False, now=now) is None


# ------------------------------------------------------------------ journal

def test_journal_roundtrip(tmp_path):
    from mm.journal import Journal
    j = Journal(str(tmp_path))
    fid = j.record_fill("DOGE", "T", "yes", "buy", 5, 42, 5, 2.0, False, 50.0)
    j.set_markout(fid, 1.5)
    stats = j.coin_stats()
    assert stats["DOGE"]["fills"] == 1
    assert stats["DOGE"]["contracts"] == 5
    assert stats["DOGE"]["avg_markout_cents"] == 1.5
    j.close()


# ------------------------------------------------------------ per-coin risk

def test_coin_bench_sticks_for_day():
    from mm.risk import RiskManager
    cfg = Config()
    rm = RiskManager(cfg, PositionBook())
    assert rm.coin_allowed("DOGE", 0.0)
    assert not rm.coin_allowed("DOGE", -cfg.coin_daily_loss_limit_dollars * 100)
    assert not rm.coin_allowed("DOGE", 0.0)   # benched even after recovery
    assert rm.coin_allowed("BNB", 0.0)        # others unaffected
    assert rm.benched_coins == ["DOGE"]


def test_position_tracks_per_market_net():
    pb = PositionBook()
    pb.on_fill("T", "yes", "buy", 5, 40, 60, fee=2.0)
    pb.on_fill("T", "no", "buy", 5, 42, 58, fee=2.0)
    p = pb.pos("T")
    assert p.realized == 10.0 and p.fees == 4.0


# ------------------------------------------------------------------- picker

def test_pick_takes_stale_cheap_ask():
    cfg = Config()
    eng = QuoteEngine(cfg)
    mkt, now = make_mkt()
    book = Book(mkt.ticker)
    # Fair ~50 but someone's stale offer sells YES at 38 (no bid at 62).
    book.apply_snapshot({"yes": [[30, 10]], "no": [[62, 25]]})
    d = eng.compute(mkt, book, make_spot(), 0, None, now)
    picks = [c for c in d.crosses if c.reason.startswith("pick")]
    assert picks and picks[0].side == "yes"
    assert picks[0].limit_price == 38
    assert picks[0].size == cfg.quote_size
    # Cooldown: immediate second evaluation must not re-pick.
    d2 = eng.compute(mkt, book, make_spot(), 0, None, now + 1)
    assert not [c for c in d2.crosses if c.reason.startswith("pick")]


def test_pick_takes_rich_bid():
    cfg = Config()
    eng = QuoteEngine(cfg)
    mkt, now = make_mkt()
    book = Book(mkt.ticker)
    # Fair ~50 but someone bids YES at 63.
    book.apply_snapshot({"yes": [[63, 15]], "no": [[30, 10]]})
    d = eng.compute(mkt, book, make_spot(), 0, None, now)
    picks = [c for c in d.crosses if c.reason.startswith("pick")]
    assert picks and picks[0].side == "no"
    assert picks[0].limit_price == 100 - 63


def test_no_pick_on_fairly_priced_book():
    cfg = Config()
    eng = QuoteEngine(cfg)
    mkt, now = make_mkt()
    book = Book(mkt.ticker)
    book.apply_snapshot({"yes": [[47, 10]], "no": [[50, 10]]})  # 47 bid / 50 ask
    d = eng.compute(mkt, book, make_spot(), 0, None, now)
    assert not [c for c in d.crosses if c.reason.startswith("pick")]


def test_pick_requires_more_edge_on_proxy_strike():
    cfg = Config()
    eng = QuoteEngine(cfg)
    mkt, now = make_mkt()
    book = Book(mkt.ticker)
    # Ask at 40 (~10c through fair): clears the real-strike requirement
    # (~8.6c) but not the proxy requirement (~10.6c) — threshold ordering.
    book.apply_snapshot({"yes": [[30, 10]], "no": [[60, 25]]})
    d_real = eng.compute(mkt, book, make_spot(), 0, None, now)
    eng2 = QuoteEngine(cfg)
    d_proxy = eng2.compute(mkt, book, make_spot(), 0, None, now,
                           strike_is_proxy=True)
    real_picks = [c for c in d_real.crosses if c.reason.startswith("pick")]
    proxy_picks = [c for c in d_proxy.crosses if c.reason.startswith("pick")]
    assert real_picks         # 42c ask vs ~50 fair: real strike takes it
    assert not proxy_picks    # proxy strike demands 2c more edge


def test_extreme_zone_reduce_only_quote():
    cfg = Config()
    eng = QuoteEngine(cfg)
    mkt, now = make_mkt()
    spot = make_spot(price=0.10045)   # ITM: fair ~97 (95 < fair < 99)
    book = Book(mkt.ticker)
    book.apply_snapshot({"yes": [[95, 10]], "no": [[2, 10]]})
    d = eng.compute(mkt, book, spot, 5, 60.0, now)
    assert d.reason == "extreme_prob"
    assert len(d.desired) == 1 and d.desired[0].side == "no"
    assert d.desired[0].size == 5          # reduce-only: capped at position
    px = 100 - d.desired[0].price
    assert px > 95                          # selling near $1, above fair


def test_sim_cross_is_level_aware():
    cfg = Config()
    pb = PositionBook()
    om = SimOrderManager(cfg, pb)
    book = Book("T")
    book.apply_snapshot({"yes": [[40, 5], [38, 5]], "no": [[50, 10]]})
    from mm.strategy import CrossExit
    # Sell 8 YES with limit 60 on NO side => only the 40-bid (5) qualifies.
    asyncio.run(om.cross("T", CrossExit("no", 8, 60, "flatten"), book))
    assert pb.pos("T").net == -5
    assert 40 not in book.yes               # liquidity consumed


# ------------------------------------------------- kalshi orderbook formats

def test_book_parses_dollar_format_from_debug_screenshot():
    """Exact shape Kalshi returned live on 2026-07-23: orderbook_fp with
    yes_dollars/no_dollars as decimal-dollar strings."""
    b = Book("KXDOGE15M")
    b.apply_snapshot({"orderbook_fp": {
        "no_dollars": [["0.1000", "8035.00"], ["0.2000", "47.00"]],
        "yes_dollars": [["0.7000", "107.14"], ["0.7900", "30.00"]],
    }})
    assert b.best_yes_bid == 79
    assert b.best_no_bid == 20
    assert b.best_yes_ask == 80
    assert b.yes[70] == 107 and b.no[10] == 8035


def test_book_parses_flat_dollar_websocket_shape():
    b = Book("T")
    b.apply_snapshot({"yes_dollars": [["0.4200", "10.00"]],
                      "no_dollars": [["0.5500", "5.00"]]})
    assert b.best_yes_bid == 42 and b.best_yes_ask == 45


def test_book_legacy_integer_format_still_works():
    b = Book("T")
    b.apply_snapshot({"orderbook": {"yes": [[40, 10]], "no": [[57, 8]]}})
    assert b.best_yes_bid == 40 and b.best_yes_ask == 43


def test_delta_accepts_dollar_prices():
    b = Book("T")
    b.apply_snapshot({"yes": [[40, 10]], "no": [[57, 8]]})
    b.apply_delta({"side": "yes", "price_dollars": "0.4100", "delta": 5})
    assert b.best_yes_bid == 41
    b.apply_delta({"side": "yes", "price_dollars": "0.4100", "delta": -5})
    assert b.best_yes_bid == 40
    # Sub-penny price rounds to a whole cent instead of crashing; the 3
    # contracts land on either neighbor of 57.5c.
    b.apply_delta({"side": "no", "price_dollars": "0.5750", "delta": 3})
    assert b.no.get(57, 0) + b.no.get(58, 0) == 11


# --------------------------------------------------- book integrity guards

def test_crossed_book_detected_and_untradeable():
    b = Book("T")
    b.apply_snapshot({"yes": [[73, 10]], "no": [[42, 10]]})  # ask 58 < bid 73
    assert b.crossed
    cfg = Config()
    eng = QuoteEngine(cfg)
    mkt, now = make_mkt()
    d = eng.compute(mkt, b, make_spot(), 0, None, now)
    assert d.reason == "book_invalid"
    assert not d.desired and not d.crosses

    from mm.settlement import Sniper
    sn = Sniper(cfg)
    mkt2 = MarketInfo("T", 0.1, now + 30, now - 870)
    sn.tracker(mkt2).samples = {i: 0.102 for i in range(30)}
    assert sn.evaluate(mkt2, b, make_spot(price=0.102), False, now) is None


def test_seq_gap_invalidates_book():
    import json
    from types import SimpleNamespace
    from mm.kalshi_ws import KalshiWs
    ws = KalshiWs(rest=SimpleNamespace(can_trade=False))

    async def run():
        await ws._handle(json.dumps({
            "type": "orderbook_snapshot", "seq": 10,
            "msg": {"market_ticker": "T", "yes": [[40, 5]], "no": [[55, 5]]}}))
        await ws._handle(json.dumps({
            "type": "orderbook_delta", "seq": 11,
            "msg": {"market_ticker": "T", "side": "yes", "price": 41, "delta": 3}}))
        assert ws.book("T").best_yes_bid == 41
        # seq jumps 11 -> 13: a delta was lost; the book must be dropped.
        await ws._handle(json.dumps({
            "type": "orderbook_delta", "seq": 13,
            "msg": {"market_ticker": "T", "side": "yes", "price": 45, "delta": 9}}))

    asyncio.run(run())
    b = ws.book("T")
    assert not b.yes and not b.no and b.last_update == 0.0
    assert ws.gap_resyncs == 1


# ------------------------------------------------------- exit slippage cap

def test_exit_never_dumps_far_through_fair():
    """The NEAR incident: 20 long, fair 25, only bid on the book is 2c.
    The old code sold at 2c (-23c slippage each); now the exit is capped
    at fair - max_exit_slippage and simply doesn't fill against that bid."""
    cfg = Config()
    eng = QuoteEngine(cfg)
    mkt, now = make_mkt(t_left=80, strike=0.1)
    spot = make_spot(price=0.09965, sigma_per_sec=8.3e-4)  # fair ~25
    book = Book(mkt.ticker)
    book.apply_snapshot({"yes": [[2, 500]], "no": [[70, 10]]})
    d = eng.compute(mkt, book, spot, 20, 30.0, now)
    assert 15 < d.fair < 35
    assert d.crosses
    c = d.crosses[0]
    assert c.side == "no"
    sell_price = 100 - c.limit_price
    assert sell_price >= d.fair - cfg.max_exit_slippage_cents - 1
    # And the sim refuses to fill it against the 2c bid.
    pb = PositionBook()
    om = SimOrderManager(cfg, pb)
    asyncio.run(om.cross(mkt.ticker, c, book))
    assert pb.pos(mkt.ticker).net == 0   # no fill at donation prices


def test_pick_skips_falling_knife():
    cfg = Config()
    eng = QuoteEngine(cfg)
    mkt, now = make_mkt()
    book = Book(mkt.ticker)
    book.apply_snapshot({"yes": [[30, 10]], "no": [[62, 25]]})  # cheap 38c ask
    # Seed history: fair was 58 thirty seconds ago; it's ~50 now -> -8 drift.
    eng._record_fair(mkt.ticker, 58.0, now - 30)
    d = eng.compute(mkt, book, make_spot(), 0, None, now)
    assert not [c for c in d.crosses if c.reason.startswith("pick")]
    # Stable fair (fresh engine, no adverse drift): same book gets picked.
    eng2 = QuoteEngine(cfg)
    eng2._record_fair(mkt.ticker, 50.0, now - 30)
    d2 = eng2.compute(mkt, book, make_spot(), 0, None, now)
    assert [c for c in d2.crosses if c.reason.startswith("pick")]


# ------------------------------------------------------- pick discipline

def test_no_picks_in_young_window():
    cfg = Config()
    eng = QuoteEngine(cfg)
    mkt, now = make_mkt(open_age=30)   # window opened 30s ago
    book = Book(mkt.ticker)
    book.apply_snapshot({"yes": [[30, 10]], "no": [[62, 25]]})  # juicy 38c ask
    d = eng.compute(mkt, book, make_spot(), 0, None, now)
    assert not [c for c in d.crosses if c.reason.startswith("pick")]


def test_no_picks_at_probability_extremes():
    cfg = Config()
    eng = QuoteEngine(cfg)
    mkt, now = make_mkt()
    spot = make_spot(price=0.1006)     # fair ~99: deep in the tail
    book = Book(mkt.ticker)
    book.apply_snapshot({"yes": [[99, 10]], "no": [[15, 10]]})
    d = eng.compute(mkt, book, spot, 0, None, now)
    assert not [c for c in d.crosses if c.reason.startswith("pick")]


def test_picks_capped_at_half_inventory():
    cfg = Config()
    eng = QuoteEngine(cfg)
    mkt, now = make_mkt()
    book = Book(mkt.ticker)
    book.apply_snapshot({"yes": [[30, 10]], "no": [[62, 25]]})
    d = eng.compute(mkt, book, make_spot(), cfg.max_position // 2, 40.0, now)
    assert not [c for c in d.crosses if c.reason.startswith("pick")]


def test_no_blind_exit_without_fair():
    """The DOGE 1c dump: flatten with fair unknown must HOLD, not sell."""
    cfg = Config()
    eng = QuoteEngine(cfg)
    mkt, now = make_mkt(t_left=80)
    spot = make_spot()
    spot.last_update = now - 10        # spot stale -> no fresh fair
    book = Book(mkt.ticker)
    book.apply_snapshot({"yes": [[1, 500]], "no": [[98, 10]]})
    d = eng.compute(mkt, book, spot, 20, 30.0, now)   # long 20, no fair known
    assert d.reason == "spot_stale"
    assert not d.crosses                                # held for settlement
