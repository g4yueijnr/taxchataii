import time

from tri.config import Config, Pair, Triangle
from tri.engine import BookStore, evaluate, scan
from tri.executor import Journal, PaperExecutor


def make_cfg(tmp_path, fee=0.001, min_edge=0.0005):
    cfg = Config()
    cfg.data_dir = str(tmp_path)
    cfg.fee_rate = fee
    cfg.min_net_edge = min_edge
    cfg.pairs = {
        "BTCUSDT": Pair("BTCUSDT", "BTC", "USDT"),
        "ETHBTC": Pair("ETHBTC", "ETH", "BTC"),
        "ETHUSDT": Pair("ETHUSDT", "ETH", "USDT"),
    }
    cfg.triangles = [Triangle("USDT-BTC-ETH", "USDT",
                              ["BTCUSDT", "ETHBTC", "ETHUSDT"])]
    return cfg


def seed(books, now, btc=(50000, 50001), ethbtc=(0.05, 0.0500), ethusdt=(2500, 2501)):
    books.update("BTCUSDT", btc[0], btc[1], 100, 100, now)
    books.update("ETHBTC", ethbtc[0], ethbtc[1], 1000, 1000, now)
    books.update("ETHUSDT", ethusdt[0], ethusdt[1], 1000, 1000, now)


def test_no_edge_on_consistent_prices(tmp_path):
    """Perfectly consistent cross prices -> gross edge ~0, net negative
    (fees) -> not actionable. This is the normal state of the market."""
    cfg = make_cfg(tmp_path)
    books = BookStore()
    now = time.time()
    # ETH/USDT == ETH/BTC * BTC/USDT exactly: 0.05 * 50000 = 2500.
    seed(books, now, btc=(50000, 50000), ethbtc=(0.05, 0.05),
         ethusdt=(2500, 2500))
    opp = evaluate(cfg.triangles[0], cfg.pairs, books, cfg.fee_rate, now)
    assert opp is not None
    assert abs(opp.gross_edge) < 1e-6          # no free lunch
    assert opp.net_edge < 0                     # fees make it a loss
    assert scan(cfg, books, now) == []          # nothing actionable


def test_detects_real_mispricing(tmp_path):
    """ETH/USDT quoted rich vs the BTC cross -> a real cycle edge that
    clears fees. Sell ETH high, buy it back cheap through BTC."""
    cfg = make_cfg(tmp_path, fee=0.001, min_edge=0.0005)
    books = BookStore()
    now = time.time()
    # BTC cross implies ETH ~2500, but ETH/USDT bids 2530 -> ~1.2% gross.
    seed(books, now, btc=(50000, 50010), ethbtc=(0.0500, 0.0501),
         ethusdt=(2530, 2531))
    opp = evaluate(cfg.triangles[0], cfg.pairs, books, cfg.fee_rate, now)
    assert opp is not None
    assert opp.gross_edge > 0.003               # real gross edge
    assert opp.net_edge > cfg.min_net_edge      # survives 3x fees
    got = scan(cfg, books, now)
    assert got and got[0].triangle == "USDT-BTC-ETH"


def test_stale_leg_is_skipped(tmp_path):
    cfg = make_cfg(tmp_path)
    books = BookStore()
    now = time.time()
    seed(books, now, ethusdt=(2530, 2531))
    # Age one leg past the limit.
    books.update("ETHBTC", 0.05, 0.0501, 1000, 1000, now - 5)
    assert evaluate(cfg.triangles[0], cfg.pairs, books, cfg.fee_rate, now,
                    max_age=1.0) is None


def test_fee_tier_changes_actionability(tmp_path):
    """The core finding: the same small edge is a loss at 0.1% fees and a
    win at 0.02% (VIP) fees."""
    books = BookStore()
    now = time.time()
    seed(books, now, ethusdt=(2505, 2506))      # ~0.2% gross: between the
                                                # 0.06% and 0.3% fee thresholds
    hi = make_cfg(tmp_path, fee=0.001, min_edge=0.0)
    lo = make_cfg(tmp_path, fee=0.0002, min_edge=0.0)
    o_hi = evaluate(hi.triangles[0], hi.pairs, books, hi.fee_rate, now)
    o_lo = evaluate(lo.triangles[0], lo.pairs, books, lo.fee_rate, now)
    assert o_hi.net_edge < 0 < o_lo.net_edge


def test_paper_executor_books_pnl(tmp_path):
    cfg = make_cfg(tmp_path, fee=0.001, min_edge=0.0005)
    books = BookStore()
    now = time.time()
    seed(books, now, btc=(50000, 50010), ethbtc=(0.0500, 0.0501),
         ethusdt=(2530, 2531))
    j = Journal(str(tmp_path))
    ex = PaperExecutor(cfg, books, j)
    opp = evaluate(cfg.triangles[0], cfg.pairs, books, cfg.fee_rate, now)
    assert ex.try_execute(opp, now)
    assert ex.stats.executed == 1
    # Realized P&L should be positive and match the net edge on the notional.
    expected = opp.net_edge * cfg.trade_notional
    assert abs(ex.stats.realized_pnl - expected) < 0.05
    assert ex.stats.realized_pnl > 0
    j.close()


def test_executor_misses_when_edge_evaporates(tmp_path):
    """Detected on a rich book, but the book reverts before execution ->
    the cycle is abandoned, not filled at a loss."""
    cfg = make_cfg(tmp_path, fee=0.001, min_edge=0.0005)
    books = BookStore()
    now = time.time()
    seed(books, now, ethusdt=(2530, 2531))      # rich -> opportunity
    opp = evaluate(cfg.triangles[0], cfg.pairs, books, cfg.fee_rate, now)
    assert opp.net_edge >= cfg.min_net_edge
    # Market reverts to consistent pricing before we execute.
    seed(books, now, btc=(50000, 50000), ethbtc=(0.05, 0.05),
         ethusdt=(2500, 2500))
    j = Journal(str(tmp_path))
    ex = PaperExecutor(cfg, books, j)
    assert not ex.try_execute(opp, now)
    assert ex.stats.executed == 0 and ex.stats.missed == 1
    assert ex.stats.realized_pnl == 0.0
    j.close()
