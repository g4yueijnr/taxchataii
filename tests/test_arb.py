"""Offline tests: matching, arb math, fees, and Gamma payload parsing."""

import datetime as dt

from arb.arbitrage import find_opportunities
from arb.kalshi import KalshiMarket, taker_fee_cents
from arb.matching import MatchedPair, match_markets, normalize
from arb.paper import PaperArbBook
from arb.polymarket import PolymarketClient, PolyMarket

NOW = dt.datetime(2026, 7, 7, tzinfo=dt.timezone.utc)


def km(ticker="FED-26SEP-C25", title="Will the Fed cut rates by 25 bps in September 2026?",
       yes_ask=40, no_ask=62, close=NOW, volume=5000):
    return KalshiMarket(ticker=ticker, title=title, yes_ask=yes_ask, no_ask=no_ask,
                        yes_bid=yes_ask - 2, no_bid=no_ask - 2,
                        close_time=close, volume=volume)


def pm(cond="0xabc", question="Will the Fed cut rates by 25 bps in September 2026?",
       yes_ask=0.42, no_ask=0.50, end=NOW, volume=9000.0):
    return PolyMarket(condition_id=cond, question=question,
                      yes_token="ytok", no_token="ntok",
                      yes_ask=yes_ask, no_ask=no_ask, yes_bid=yes_ask - 0.02,
                      end_date=end, volume=volume, slug="fed-cut-sep-2026")


def test_fuzzy_match_same_question():
    pairs = match_markets([km()], [pm(question="Fed cuts rates by 25 bps in September 2026")])
    assert len(pairs) == 1
    assert not pairs[0].confirmed


def test_number_guard_blocks_different_thresholds():
    pairs = match_markets([km()], [pm(question="Will the Fed cut rates by 50 bps in September 2026?")])
    assert pairs == []


def test_date_guard_blocks_far_apart_closes():
    far = pm(end=NOW + dt.timedelta(days=30))
    assert match_markets([km()], [far]) == []
    near = pm(end=NOW + dt.timedelta(days=2))
    assert len(match_markets([km()], [near])) == 1


def test_manual_match_wins_and_is_confirmed():
    p = pm(question="Completely different wording about the FOMC decision")
    pairs = match_markets([km()], [p], manual={"FED-26SEP-C25": "0xabc"})
    assert len(pairs) == 1 and pairs[0].confirmed
    # slug also works as the manual key
    pairs = match_markets([km()], [p], manual={"FED-26SEP-C25": "fed-cut-sep-2026"})
    assert len(pairs) == 1 and pairs[0].confirmed


def test_arbitrage_both_directions():
    # Kalshi YES 40c + Poly NO 50c = 90c -> ~10c gross edge minus ~2c fee
    pair = MatchedPair(km(yes_ask=40, no_ask=62), pm(yes_ask=0.42, no_ask=0.50), 95.0)
    opps = find_opportunities([pair], min_edge=0.05)
    assert len(opps) == 1
    o = opps[0]
    assert o.direction == "kalshi_yes_poly_no"
    assert o.gross_cost == 0.90
    assert abs(o.net_edge - (1 - 0.90 - taker_fee_cents(40) / 100)) < 1e-9

    # Kalshi NO 45c + Poly YES 48c = 93c -> the other direction
    pair2 = MatchedPair(km(yes_ask=57, no_ask=45), pm(yes_ask=0.48, no_ask=0.55), 95.0)
    opps2 = find_opportunities([pair2], min_edge=0.03)
    assert [o.direction for o in opps2] == ["kalshi_no_poly_yes"]


def test_no_arb_when_costs_exceed_threshold():
    pair = MatchedPair(km(yes_ask=50, no_ask=52), pm(yes_ask=0.51, no_ask=0.51), 95.0)
    assert find_opportunities([pair], min_edge=0.05) == []


def test_missing_ask_is_skipped():
    pair = MatchedPair(km(yes_ask=0), pm(no_ask=None), 95.0)
    assert find_opportunities([pair], min_edge=0.0) == []


def test_taker_fee_matches_kalshi_formula():
    assert taker_fee_cents(50) == 2      # ceil(0.07*0.25*100)=ceil(1.75)=2
    assert taker_fee_cents(5) == 1       # ceil(0.3325)=1
    assert taker_fee_cents(50, count=10) == 18  # ceil(17.5)


def test_gamma_payload_parsing():
    raw = {
        "conditionId": "0xdeadbeef",
        "question": "Will BTC be above $150k on Dec 31, 2026?",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["111", "222"]',
        "bestAsk": 0.31, "bestBid": 0.29,
        "volumeNum": 12345.0,
        "endDate": "2026-12-31T00:00:00Z",
        "slug": "btc-150k-2026",
        "enableOrderBook": True,
    }
    m = PolymarketClient._parse_market(raw, min_volume=0)
    assert m.yes_token == "111" and m.no_token == "222"
    assert m.yes_ask == 0.31
    assert abs(m.no_ask - 0.71) < 1e-9  # 1 - bestBid
    # non-binary and low-volume markets are dropped
    multi = dict(raw, outcomes='["A","B","C"]', clobTokenIds='["1","2","3"]')
    assert PolymarketClient._parse_market(multi, 0) is None
    assert PolymarketClient._parse_market(raw, min_volume=99999) is None


def test_normalize_strips_noise():
    assert normalize("Will the Fed cut rates?") == normalize("Fed cut rates")


# --------------------------------------------------------------- paper book

def _opp(yes_ask=40, poly_no=0.50, confirmed=False, ticker="FED-26SEP-C25"):
    pair = MatchedPair(km(ticker=ticker, yes_ask=yes_ask),
                       pm(no_ask=poly_no), 95.0, confirmed=confirmed)
    opps = find_opportunities([pair], min_edge=0.01)
    return opps[0]


def test_paper_books_locked_edge():
    """A hedged pair locks its net edge the instant both legs fill."""
    book = PaperArbBook(bankroll=100.0)
    opp = _opp(yes_ask=40, poly_no=0.50)          # ~8c edge after fee
    fill = book.execute(opp, max_contracts=20)
    assert fill is not None and fill.contracts == 20
    # profit = net_edge * contracts, booked to realized immediately.
    assert abs(book.locked_profit - opp.net_edge * 20) < 1e-9
    assert abs(book.equity - (100.0 + opp.net_edge * 20)) < 1e-9
    # capital tied up = cost of both legs.
    assert abs(book.deployed - (opp.gross_cost + opp.kalshi_fee) * 20) < 1e-9


def test_paper_takes_each_opp_once():
    """An arb vanishes once hit — the same key never books twice (the replay
    bug that faked P&L on the maker bot must not recur here)."""
    book = PaperArbBook(bankroll=100.0)
    opp = _opp()
    assert book.execute(opp, 10) is not None
    assert book.execute(opp, 10) is None
    assert len(book.fills) == 1


def test_paper_sizes_down_to_available_capital():
    """A $100 account can't fund unlimited contracts; size caps to bankroll."""
    book = PaperArbBook(bankroll=1.0)             # tiny bankroll
    opp = _opp(yes_ask=40, poly_no=0.50)          # ~90c cost per pair
    fill = book.execute(opp, max_contracts=1000)
    assert fill is not None and fill.contracts == 1   # only one pair affordable
    # Now fully (nearly) deployed: a second, different arb can't fund a pair.
    opp2 = _opp(ticker="OTHER-TICKER")
    assert book.execute(opp2, 1000) is None


def test_paper_tracks_confirmed_profit_separately():
    book = PaperArbBook(bankroll=100.0)
    book.execute(_opp(confirmed=False, ticker="FUZZY-1"), 5)
    book.execute(_opp(confirmed=True, ticker="CONF-1"), 5)
    assert book.confirmed_profit > 0
    assert book.confirmed_profit < book.locked_profit   # fuzzy adds on top


def test_polymarket_endpoints_are_env_overridable(monkeypatch):
    """PM US support: the base URLs must honor env overrides at import time."""
    import importlib
    monkeypatch.setenv("PM_GAMMA_BASE", "https://gamma.example.us")
    monkeypatch.setenv("PM_CLOB_BASE", "https://clob.example.us")
    import arb.polymarket as pmod
    importlib.reload(pmod)
    assert pmod.GAMMA_BASE == "https://gamma.example.us"
    assert pmod.CLOB_BASE == "https://clob.example.us"
    monkeypatch.delenv("PM_GAMMA_BASE")
    monkeypatch.delenv("PM_CLOB_BASE")
    importlib.reload(pmod)                         # restore defaults for others
