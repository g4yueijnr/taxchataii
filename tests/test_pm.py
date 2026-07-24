from pm.config import Config
from pm.engine import (Book, MakerStrategy, PaperBook, d2c, c2d,
                       maker_rebate_cents)
from pm.executor import PaperOrderManager


def make_book(token="T", bids=None, asks=None):
    b = Book(token)
    b.apply_snapshot(bids or [(0.44, 100)], asks or [(0.48, 100)])
    return b


# ------------------------------------------------------------------- book

def test_book_best_and_spread():
    b = make_book(bids=[(0.44, 50), (0.43, 20)], asks=[(0.48, 30), (0.49, 10)])
    assert b.best_bid == 44 and b.best_ask == 48 and b.spread == 4
    assert b.mid == 46.0 and not b.crossed


def test_price_conversions():
    assert d2c(0.44) == 44 and c2d(44) == 0.44
    assert d2c("0.4800") == 48


# --------------------------------------------------------------- strategy

def test_maker_joins_inside_wide_book():
    cfg = Config()
    q = MakerStrategy(cfg).compute(make_book(bids=[(0.40, 100)],
                                             asks=[(0.50, 100)]), 0.0)
    assert q is not None
    assert q.bid == 41 and q.ask == 49          # 1 tick inside each touch


def test_maker_skips_tight_book():
    cfg = Config()                               # min_book_spread_ticks = 2
    q = MakerStrategy(cfg).compute(make_book(bids=[(0.47, 100)],
                                             asks=[(0.48, 100)]), 0.0)
    assert q is None                             # 1c spread, no room


def test_maker_skews_with_inventory():
    cfg = Config()
    strat = MakerStrategy(cfg)
    flat = strat.compute(make_book(bids=[(0.40, 100)], asks=[(0.50, 100)]), 0.0)
    long = strat.compute(make_book(bids=[(0.40, 100)], asks=[(0.50, 100)]),
                         cfg.max_position)        # fully long -> shift down
    assert long.bid < flat.bid and long.ask < flat.ask
    assert long.bid_size == 0                     # can't add to a full long


# --------------------------------------------------------------- economics

def test_maker_rebate_is_a_positive_credit():
    # At 50c the rebate is largest; it is a credit, not a charge.
    assert maker_rebate_cents(50, 20, 0.0125) > 0
    assert maker_rebate_cents(50, 20, 0.0125) > maker_rebate_cents(20, 20, 0.0125)


def test_round_trip_profits_from_spread_plus_rebate():
    """The whole reason to move here: capture the spread AND get paid a
    rebate, so a tight round trip is net POSITIVE (it was negative on Kalshi
    where the maker pays a fee)."""
    cfg = Config()
    pb = PaperBook(cfg)
    pb.fill("T", "buy", 44, 20)      # buy 20 @ 44c
    pb.fill("T", "sell", 46, 20)     # sell 20 @ 46c
    assert pb.pos("T").shares == 0
    # 2c spread * 20 shares = 40c, plus rebates on both fills, all positive.
    assert pb.realized_cents > 40
    assert pb.rebates_cents > 0


# ---------------------------------------------------------------- fills

def test_paper_fill_from_trade_tape():
    cfg = Config()
    pb = PaperBook(cfg)
    om = PaperOrderManager(cfg, pb)
    from pm.engine import DesiredQuote
    om.set_quote("T", DesiredQuote(bid=44, ask=48, bid_size=20, ask_size=20))
    # A sell prints at 43 (through our 44 bid) -> we buy 20 @ 44.
    assert om.on_trade("T", 43, 20)
    assert pb.pos("T").shares == 20 and pb.pos("T").avg_cost == 44
    # A buy prints at 49 (through our 48 ask) -> we sell 20 @ 48 -> flat + pnl.
    assert om.on_trade("T", 49, 20)
    assert pb.pos("T").shares == 0
    assert pb.realized_cents > 0                  # profitable round trip


def test_no_fill_inside_the_spread():
    cfg = Config()
    pb = PaperBook(cfg)
    om = PaperOrderManager(cfg, pb)
    from pm.engine import DesiredQuote
    om.set_quote("T", DesiredQuote(bid=44, ask=48, bid_size=20, ask_size=20))
    assert not om.on_trade("T", 46, 20)           # 46 is between our quotes
    assert pb.pos("T").shares == 0
