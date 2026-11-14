"""The curve exit as one risk reading (2026-11-13, user: "the curve exit logic needs to be the single handler for all exits based around curve logic and time left ... Our risk factor is what the exit should be. And it's based on kalshis current price compared to ours (price cushion) or the spot to strike distance cushion, whichever is relatively worse; factored by curve event history in the past hour and time remaining in the market. Spot prices don't flip the price cushion, and vice versa, so knowing that we should be more biased of the price cushion. We should have a min profit and a max loss factor and a wait scale but not a specific time at all.").

Two cushions, both read as the chance that the side we hold pays:
  spot cushion  q_spot: the curve's chance from the spot's distance to the strike over the time left, in the market's own variance (curvevalue.read), then widened by the market's own last hour (history_factor).
  price cushion q_price: what Kalshi's own bid, net of the sell fee, says the position is worth.
The worse of the two is the risk. The price is the one we trust: when the price is the worse one it counts in full, when the spot is the worse one only `spot_weight` of the way down to it (a spot move does not flip Kalshi's price, so a spot alarm alone is a hint, a price alarm is the market's own).
Time left is inside both readings (the spot chance shrinks with the square root of the time left; the bid is the market's chance with that time). Nothing here is a number of seconds, a price level, a coin or a commodity.

The sale: sell when q is under the lane's line, for a loss at any depth or a profit of at least min_profit of the cost, after a wait (the market's own recovery time cut by how far q is under the line, never over a share of the time left: wait()). The two bounds are not fixed numbers: the loss we carry before selling at once is max_loss * q of the cost, and the profit we ask for is min_profit * q (stopped(), sale_ok()), so both tighten as the risk rises.
Evidence: none yet. This is a first build, shadow only (lane field curve_risk_live off), and its settings are guesses to be scored against the bets that follow."""

from __future__ import annotations

import bisect
import math
from statistics import NormalDist
from typing import NamedTuple

_N = NormalDist()
MINUTE = 60.0
MIN_MOVES = 30  # one-minute moves needed in the hour before the history reading is trusted
HISTORY_CAP = 3.0  # the history factor never widens the spot's uncertainty more than this many times


class Risk(NamedTuple):
    q: float  # the chance the side pays that the exit acts on (the worse cushion, the price one counted in full)
    q_spot: float  # the spot cushion's chance, after the history factor
    q_price: float  # Kalshi's bid, net of the sell fee
    worse: str  # "price" or "spot"


def history_factor(ts: list[float], vs: list[float], now: float, hours: float = 1.0, cap: float = HISTORY_CAP) -> float:
    """How bunched into jumps this market's last hour was: the rms of its one-minute moves over their robust sigma (1.4826 times the median absolute move). 1.0 for an hour as even as a normal one; the more of the hour's movement sits in a few big minutes, the larger (capped). 1.0 when the hour is too short or too flat to read."""
    if not ts or len(ts) != len(vs):
        return 1.0
    minutes = int(hours * 3600.0 // MINUTE)
    levels: list[float] = []
    for k in range(minutes + 1):
        i = bisect.bisect_right(ts, now - k * MINUTE) - 1
        if i < 0 or vs[i] <= 0:
            break
        levels.append(math.log(vs[i]))
    moves = [levels[k] - levels[k + 1] for k in range(len(levels) - 1)]
    if len(moves) < MIN_MOVES:
        return 1.0
    sigma = 1.4826 * sorted(abs(m) for m in moves)[len(moves) // 2]
    if sigma <= 0:
        return 1.0
    rms = math.sqrt(sum(m * m for m in moves) / len(moves))
    return min(max(rms / sigma, 1.0), cap)


def risk(p_spot: float, net_bid: float, hist: float = 1.0, spot_weight: float = 0.5) -> Risk:
    """The two cushions and the worse of them. `p_spot` is the curve's chance that the side pays, `net_bid` the bid after the sell fee, `hist` the history factor (a jumpy hour pulls the spot's chance toward even, as more can happen than the quiet rate says), `spot_weight` how far a spot alarm counts when it is the worse one."""
    p = min(max(p_spot, 1e-6), 1.0 - 1e-6)
    q_spot = _N.cdf(_N.inv_cdf(p) / max(hist, 1.0))
    q_price = min(max(net_bid, 0.0), 1.0)
    if q_price <= q_spot:
        return Risk(q_price, q_spot, q_price, "price")
    w = min(max(spot_weight, 0.0), 1.0)
    return Risk(q_price - w * (q_price - q_spot), q_spot, q_price, "spot")


def stopped(pnl: float, q: float, max_loss: float) -> bool:
    """The loss the bet may carry, scaled by the risk: max_loss * q of the cost. With plenty of cushion (q near 1) the bet may dip the whole of max_loss before it is cut; as the risk rises (q falls) the loss we accept shrinks with it, so a bet at q 0.5 is cut at half the dip. At or past that depth the exit sells at once, whatever the wait."""
    return max_loss > 0 and pnl <= -max_loss * q


def sale_ok(q: float, line: float, pnl: float, min_profit: float) -> bool:
    """Whether the risk is high and a sale is worth making. A loss is worth cutting once the risk is under the line (a small loss beats a full one). A profit is worth taking when it is at least min_profit * q of the cost: with plenty of cushion the profit has to be worth the fee and the spread, as the risk rises the profit we ask for shrinks toward nothing, because a profit at high risk is the one most likely to vanish."""
    if line <= 0 or q >= line:
        return False
    return pnl >= min_profit * q or pnl < 0.0


def wait(q: float, line: float, left: float, base: float, share: float) -> float:
    """Seconds the risk must stay high before the sale: the market's own recovery time (`base`), less the further q is under the line (zero at q = 0), never more than `share` of the time left."""
    severity = min(1.0, max(0.0, (line - q) / line)) if line > 0 else 1.0
    return max(0.0, min(base * (1.0 - severity), share * max(left, 0.0)))


def long_chance(ts: list[float], vs: list[float], now: float, ref: float, side: str, left: float, hours: float = 1.0, stale: float = 30.0) -> float | None:
    """The long layer's chance that the side pays (2026-11-13, user, after a live sale of a bet that dipped briefly, never crossed the strike and went on to win: "the hour view showed it likely staying positive"): the distance of the Kalshi perp from its own value at the window open, over the typical movement of the perp across the last hour (rms of its 60 s moves, per sqrt second) for the time left. None when the hour is too short, the newest tick is stale, or the reference is unusable (the sale then does not wait for this layer)."""
    if ref <= 0 or left <= 0 or not ts or len(ts) != len(vs):
        return None
    j = bisect.bisect_right(ts, now) - 1
    if j < 0 or now - ts[j] > stale or vs[j] <= 0:
        return None
    levels: list[float] = []
    for k in range(int(hours * 3600.0 // MINUTE) + 1):
        i = bisect.bisect_right(ts, now - k * MINUTE) - 1
        if i < 0 or vs[i] <= 0:
            break
        levels.append(math.log(vs[i]))
    moves = [levels[k] - levels[k + 1] for k in range(len(levels) - 1)]
    if len(moves) < MIN_MOVES:
        return None
    rate = math.sqrt(sum(m * m for m in moves) / len(moves) / MINUTE)
    sign = 1.0 if side == "yes" else -1.0
    cushion = (math.log(vs[j]) - math.log(ref)) * sign
    if rate <= 0:
        return 1.0 if cushion > 0 else 0.0
    return _N.cdf(cushion / (rate * math.sqrt(left)))


def swing_hold(drop: float, unit: float | None, left: float, k: float) -> bool:
    """True when a loss is still inside the market's own swings, so it is no reason to sell (2026-11-14, user, after a NEAR NO bought at 0.56 with 295 s left was sold at 0.29 and then won: "the look back should have seen that the curve was dipping and rising ... So why would a 56c care if it dips to 12c for a moment? Thats not a reason to sell at 5mins left").
    `drop` is the loss per contract in probability (the cost less the bid after its fee), `unit` the market's own typical minute (quoteshock.typical_minute: sqrt(60) times the root-mean-square one-second change of its mid over the last 15 minutes, in probability), `left` the seconds to the close, `k` the lane's setting (0 = off).
    A move over the time left is about unit * sqrt(left / 60), so a loss under k of that is what this market does in the time it has: more time left means a wider band, and the band narrows to one typical minute as the close nears. No reading of the unit means no hold (the plain stop applies)."""
    if k <= 0 or unit is None or unit <= 0 or left <= 0:
        return False
    return drop < k * unit * math.sqrt(left / MINUTE)


def take_profit(gain: float, room: float, unit: float | None, left: float, share: float, cover: float) -> bool:
    """True when a profit is worth taking now, read off the bet itself and its own market (2026-11-14, user, on a BTC NO bought at 0.77 that rose to 0.85, fell to 0.26 and was sold for -67%: "that needs to scale it needs to be based on the bet").
    `gain` is the profit per contract in probability (the bid after its sell fee less the cost with its buy fee), `room` the most this bet could still have made (1 less that cost), `unit` the market's own typical minute (quoteshock.typical_minute), `left` the seconds to the close, `share` the lane's setting (0 = off), `cover` how many times the gain the market's own swing over the time left must be.
    Two readings, both from the bet and not a fixed cent figure: the gain has to be at least `share` of the room the bet had (a 0.77 buy has 0.21 of room, a 0.30 buy has 0.70, so the same share asks 0.05 of the first and 0.18 of the second), and the market's swing over the time left (unit * sqrt(left / 60)) has to be at least `cover` times the gain, so the gain is small enough for this market to take back. As the close nears the swing shrinks and a held gain turns into a won bet instead of a sale. No reading of the unit means no sale."""
    if share <= 0 or gain <= 0 or room <= 0 or unit is None or unit <= 0 or left <= 0:
        return False
    return gain >= share * room and unit * math.sqrt(left / MINUTE) >= cover * gain
