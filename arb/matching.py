"""Match overlapping markets across Kalshi and Polymarket.

Matching is fuzzy-title based with guards (numbers must agree, end dates must
be close). Fuzzy matches can still pair markets with subtly different
resolution rules, so execution should prefer pairs confirmed by hand in
``matches.yaml``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz, process, utils

from .kalshi import KalshiMarket
from .polymarket import PolyMarket

_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
_K_RE = re.compile(r"(\d+(?:\.\d+)?)\s*k\b")
_M_RE = re.compile(r"(\d+(?:\.\d+)?)\s*m\b")

_STOPWORDS = {
    "will", "the", "a", "an", "in", "on", "at", "by", "be", "to", "of",
    "before", "or", "and", "for", "this", "is", "are", "does", "do",
}


def normalize(title: str) -> str:
    t = title.lower()
    t = re.sub(r"[^\w\s.%$]", " ", t)
    words = [w for w in t.split() if w not in _STOPWORDS]
    return " ".join(words)


def _numbers(title: str) -> set[str]:
    """Canonical numeric tokens, robust to the ways two venues write the same
    threshold: '$120,000' == '120k', '1.5M' == '1500000'. Commas stripped,
    k/M suffixes expanded, then each number canonicalized to an int/float
    string so the guard compares values, not spellings."""
    t = title.lower().replace(",", "")
    t = _K_RE.sub(lambda m: str(int(float(m.group(1)) * 1000)), t)
    t = _M_RE.sub(lambda m: str(int(float(m.group(1)) * 1_000_000)), t)
    nums: set[str] = set()
    for n in _NUM_RE.findall(t):
        f = float(n)
        nums.add(str(int(f)) if f == int(f) else str(f))
    return nums


@dataclass
class MatchedPair:
    kalshi: KalshiMarket
    poly: PolyMarket
    score: float
    confirmed: bool = False  # came from matches.yaml

    @property
    def label(self) -> str:
        flag = "CONFIRMED" if self.confirmed else f"fuzzy {self.score:.0f}"
        return f"[{flag}] {self.kalshi.ticker} <-> {self.poly.question[:60]}"


def match_markets(kalshi_markets: list[KalshiMarket],
                  poly_markets: list[PolyMarket],
                  min_score: float = 90.0,
                  max_days_apart: float = 3.0,
                  manual: dict[str, str] | None = None) -> list[MatchedPair]:
    """Return matched pairs, best fuzzy match per Kalshi market.

    manual: {kalshi_ticker: polymarket condition_id or slug} confirmed pairs.
    """
    pairs: list[MatchedPair] = []
    manual = manual or {}
    poly_by_cond = {m.condition_id: m for m in poly_markets}
    poly_by_slug = {m.slug: m for m in poly_markets if m.slug}
    kalshi_by_ticker = {m.ticker: m for m in kalshi_markets}

    matched_kalshi: set[str] = set()
    matched_poly: set[str] = set()

    # 1. Manual, human-confirmed pairs always win.
    for k_ticker, p_key in manual.items():
        km = kalshi_by_ticker.get(k_ticker)
        pm = poly_by_cond.get(p_key) or poly_by_slug.get(p_key)
        if km and pm:
            pairs.append(MatchedPair(km, pm, 100.0, confirmed=True))
            matched_kalshi.add(km.ticker)
            matched_poly.add(pm.condition_id)

    # 2. Fuzzy match the rest.
    poly_pool = [m for m in poly_markets if m.condition_id not in matched_poly]
    poly_norm = [normalize(m.question) for m in poly_pool]

    for km in kalshi_markets:
        if km.ticker in matched_kalshi:
            continue
        query = normalize(km.title)
        if not query:
            continue
        result = process.extractOne(
            query, poly_norm, scorer=fuzz.token_sort_ratio,
            processor=utils.default_process, score_cutoff=min_score)
        if not result:
            continue
        _, score, idx = result
        pm = poly_pool[idx]

        # Guard: numeric thresholds/dates in the titles must agree.
        if _numbers(km.title) != _numbers(pm.question):
            continue
        # Guard: resolution dates must be close.
        if km.close_time and pm.end_date:
            days = abs((km.close_time - pm.end_date).total_seconds()) / 86400
            if days > max_days_apart:
                continue
        pairs.append(MatchedPair(km, pm, score))

    return pairs
