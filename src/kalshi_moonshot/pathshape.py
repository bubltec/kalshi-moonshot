"""The shape of a bet's own price path over a look-back: flat, a trend (against or for the bet), or wavy (sell step `shape`, shadow only).

2026-11-18, user, on the loss sale: "it also depends on the lookback. if the path is wavy it means there's likely a comeback, which will at bare minimum get us a better sale"; "if look back is trend or flat, and we're losing, we should probably exit though"; "we need to test these concepts. they sound good as ideas but depend on your implementation"; "is the trend against the buy path? how far does it look back for the wavy paths? flat paths?". Two tape studies used a stand-in (changes of favorite) and said the opposite; so the idea is measured directly here and scored forward on the bot's own bets (scripts/shape_shadow_score.py). Nothing in this module sells.

The path is OUR side's bid, (time, bid) oldest first. On a window [start, now]:
  net        last bid less the first (under zero = the price fell = against the bet)
  ground     the sum of the absolute moves, quote to quote
  eff        |net| / ground (1 = a straight run, 0 = back and forth): marketprofile's path efficiency, on the bid
  reversals  turns between legs that each ran at least the market's typical move for a third of the window (two turns cut a window in three legs)
  typical    the market's typical minute times sqrt(window / 60): what a market this size moves in a window this long
  net_z, ground_z  net and ground in typical moves

The shapes, with every bar the market's own:
  unknown        too few quotes, quotes that do not reach back to the window's start, no typical minute, or too few earlier readings to say what "most" is
  anomaly        net_z outside the 1st to 99th percentile of the series' own readings of the current hour, or ground_z over their 99th (CLAUDE.md rule 3): no rule is assumed to hold. Ground is judged on the high side only: a path that did not move at all is the flat shape itself, not an anomaly
  trend_against  it moved more than typical and at least as straight as the series' median reading this hour, downward
  trend_for      the same, upward (behind and rising: a recovery)
  wavy           two or more reversals, and either it ended within a typical move of where it began having covered at least the series' median ground, or it moved further but less straight than the median
  flat           the rest: it ended within a typical move of where it began without waving

The user's rule (the one scored): a losing bet whose path SINCE THE BUY is trend_against or flat would be sold; wavy, trend_for, anomaly and unknown would not. The second look-back, a quarter of the time left, is recorded beside it so the score can say which look-back means something. Memory only.
"""

from __future__ import annotations

import itertools
import math
import statistics
from collections import deque
from collections.abc import Sequence
from typing import NamedTuple

LOOKS = ("buy", "quarter", "timed")  # the two look-backs: since the buy; a share of the time left
QUARTER = 0.25  # the second look-back's share of the time left (the share the buy's risk trend reads); it shortens toward the close
MIN_QUOTES = 6  # quotes a window needs before it is judged
COVER = 0.8  # the share of the window the quotes must span
MIN_SEEN = 20  # earlier readings of the series this hour before "most" and "a lot" can be said
MIN_SEEN_RANGE = 100  # readings before one can be called outside the hour's range (curverisk.TREND_MIN_SEEN)
TIMED_SHARE = 0.1  # sell step shape_timed: the bet must have been behind, and its path flat or trending against, for this share of the time left (2026-11-18, user: "they should be time based where it waits or has to exhibit the behavior for a certain time relative to the time remaining"). A FIRST SETTING chosen without a test, not fitted: it is a tenth of the time left, 90 s at the open, 45 s with 7.5 minutes left
TIMED_FLOOR = 90.0  # the time left counts as no less than this (the user's cap on the volatility looks, "around 90s left", applied here too): from 90 s to the close the wait stays 9 s
STEP = 5.0  # seconds between readings (curverisk.TREND_STEP)
HOUR = 3600.0
WOULD_SELL = ("trend_against", "flat")  # the user's rule, on the since-the-buy shape

SEEN: dict[tuple[str, str], deque[tuple[float, float, float, float]]] = {}  # (series, look-back) -> (time, net_z, eff, ground_z) of the current hour: what a reading is judged against


class Shape(NamedTuple):
    shape: str
    net: float = 0.0
    ground: float = 0.0
    eff: float = 0.0
    reversals: int = 0
    quotes: int = 0
    typical: float = 0.0  # the market's typical move for the window
    window: float = 0.0  # seconds

    @property
    def direction(self) -> str:
        return "against" if self.net < 0 else "for" if self.net > 0 else "none"

    @property
    def net_z(self) -> float:
        return self.net / self.typical if self.typical > 0 else 0.0

    @property
    def ground_z(self) -> float:
        return self.ground / self.typical if self.typical > 0 else 0.0

    def row(self) -> dict[str, float | int | str]:
        """The numbers behind the shape, as they go into data/risk_shadow.jsonl."""
        return {"shape": self.shape, "direction": self.direction, "net": round(self.net, 4), "ground": round(self.ground, 4), "eff": round(self.eff, 3), "reversals": self.reversals, "quotes": self.quotes, "typical": round(self.typical, 4), "window": round(self.window, 1)}


def reversals(bids: Sequence[float], leg: float) -> int:
    """Turns in the path between legs of at least `leg`: a move of `leg` from the running extreme, against the direction of travel, is a turn."""
    if leg <= 0 or len(bids) < 3:
        return 0
    turns, way, high, low = 0, 0, bids[0], bids[0]
    for b in bids[1:]:
        high, low = max(high, b), min(low, b)
        if way >= 0 and high - b >= leg:
            turns += 1 if way > 0 else 0
            way, low = -1, b
        elif way <= 0 and b - low >= leg:
            turns += 1 if way < 0 else 0
            way, high = 1, b
    return turns


def _top(ranked: list[float]) -> float:
    return ranked[math.ceil(0.99 * (len(ranked) - 1))]


def _outside(v: float, ranked: list[float]) -> bool:
    return v < ranked[int(0.01 * (len(ranked) - 1))] or v > _top(ranked)


def shape(path: Sequence[tuple[float, float]], start: float, now: float, unit: float | None, seen: Sequence[tuple[float, float, float]] = ()) -> Shape:
    """The shape of `path` ((time, our side's bid), oldest first) over [start, now]. `unit` is the market's typical minute; `seen` the series' earlier readings of the current hour on this look-back, as (net_z, eff, ground_z)."""
    window = now - start
    before = [p for p in path if p[0] <= start and p[1] > 0]
    inside = [p for p in path if start < p[0] <= now and p[1] > 0]
    pts = ([(start, before[-1][1])] if before else []) + inside  # the price standing at the window's start is its first point
    n = len(pts)
    if window <= 0 or n < MIN_QUOTES or pts[0][0] - start > (1.0 - COVER) * window or now - pts[-1][0] > (1.0 - COVER) * window:
        return Shape("unknown", quotes=n, window=max(window, 0.0))
    bids = [p[1] for p in pts]
    net = bids[-1] - bids[0]
    ground = sum(abs(b - a) for a, b in itertools.pairwise(bids))
    eff = abs(net) / ground if ground > 0 else 0.0
    if unit is None or unit <= 0:
        return Shape("unknown", net, ground, eff, 0, n, 0.0, window)
    typical = unit * math.sqrt(window / 60.0)
    turns = reversals(bids, typical / math.sqrt(3.0))
    got = Shape("unknown", net, ground, eff, turns, n, typical, window)
    if len(seen) < MIN_SEEN:
        return got
    if len(seen) >= MIN_SEEN_RANGE and (_outside(got.net_z, sorted(x[0] for x in seen)) or got.ground_z > _top(sorted(x[2] for x in seen))):
        return got._replace(shape="anomaly")
    straight = eff >= statistics.median(x[1] for x in seen)
    if abs(net) >= typical:
        if straight or turns < 2:
            return got._replace(shape="trend_against" if net < 0 else "trend_for")
        return got._replace(shape="wavy")
    if turns >= 2 and got.ground_z >= statistics.median(x[2] for x in seen):
        return got._replace(shape="wavy")
    return got._replace(shape="flat")


def seen_of(series: str, look: str, now: float) -> list[tuple[float, float, float]]:
    """The series' readings of the current hour on one look-back, oldest dropped."""
    rows = SEEN.setdefault((series, look), deque())
    while rows and rows[0][0] < now - HOUR:
        rows.popleft()
    return [r[1:] for r in rows]


def note(series: str, look: str, now: float, got: Shape) -> None:
    """Keep a reading in the series' hour (one that could be measured: it has a typical move)."""
    if got.typical > 0:
        SEEN.setdefault((series, look), deque()).append((now, got.net_z, got.eff, got.ground_z))


def timed_window(left: float) -> float:
    """The seconds a bet must have been behind, with its path flat or trending against, for the shape_timed step: TIMED_SHARE of the time left, the time left counted as no less than TIMED_FLOOR."""
    return TIMED_SHARE * max(left, TIMED_FLOOR)


def would_sell(buy_shape: str) -> bool:
    return buy_shape in WOULD_SELL
