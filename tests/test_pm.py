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


def test_round_trip_profits_from_spread_with_zero_fees():
    """The honest Polymarket edge: zero maker fees, so a 2c round trip nets
    the full spread (on Kalshi the ~0.44c/contract maker fee ate most of
    it). Rebate defaults to 0 -- it's upside, not the thesis."""
    cfg = Config()
    assert cfg.maker_rebate_mult == 0.0        # honest default
    pb = PaperBook(cfg)
    pb.fill("T", "buy", 44, 20)      # buy 20 @ 44c
    pb.fill("T", "sell", 46, 20)     # sell 20 @ 46c
    assert pb.pos("T").shares == 0
    assert pb.realized_cents == 40.0           # full 2c * 20 spread, no fee drag
    assert pb.rebates_cents == 0.0


def test_round_trip_with_rebate_adds_upside():
    """If a maker rebate does exist, it's credited on top of the spread."""
    cfg = Config()
    cfg.maker_rebate_mult = 0.0125
    pb = PaperBook(cfg)
    pb.fill("T", "buy", 44, 20)
    pb.fill("T", "sell", 46, 20)
    assert pb.realized_cents > 40.0            # spread + rebate
    assert pb.rebates_cents > 0.0


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
