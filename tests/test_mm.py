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


def make_mkt(t_left=600.0, strike=0.1, now=None):
    now = now or time.time()
    return MarketInfo("KXDOGE15M-TEST", strike, now + t_left, now - 60), now


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
