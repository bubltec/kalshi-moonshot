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


def hour_rate(ts: list[float], vs: list[float], now: float, hours: float = 1.0) -> float | None:
    """How much this index has been moving over the last hour, per square-root second in log terms: the root-mean-square of its one-minute moves over that hour, over sqrt(60). Times sqrt(seconds) it is the typical move over that many seconds. None when the hour has under MIN_MOVES one-minute moves on record.
    The one reading of volatility for both ends of a bet (2026-11-14, user, on a ladder width read from six hours of bars times a multiplier fitted on six hours of outcomes: "These markets shift in behavior hourly if you're looking at 6 hours to base a decision on right now that is totally irrelevant"): the exit's hour view (long_chance) and the ladder's width for a market priced from the Kalshi perp (commodities.price_event)."""
    if not ts or len(ts) != len(vs):
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
    return math.sqrt(sum(m * m for m in moves) / len(moves) / MINUTE)


def long_chance(ts: list[float], vs: list[float], now: float, ref: float, side: str, left: float, hours: float = 1.0, stale: float = 30.0) -> float | None:
    """The long layer's chance that the side pays (2026-11-13, user, after a live sale of a bet that dipped briefly, never crossed the strike and went on to win: "the hour view showed it likely staying positive"): the distance of the Kalshi perp from its own value at the window open, over the typical movement of the perp across the last hour (rms of its 60 s moves, per sqrt second) for the time left. None when the hour is too short, the newest tick is stale, or the reference is unusable (the sale then does not wait for this layer)."""
    if ref <= 0 or left <= 0 or not ts or len(ts) != len(vs):
        return None
    j = bisect.bisect_right(ts, now) - 1
    if j < 0 or now - ts[j] > stale or vs[j] <= 0:
        return None
    rate = hour_rate(ts, vs, now, hours)
    if rate is None:
        return None
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


def take_bar(gain: float, room: float, left: float, window: float, unit: float | None = None) -> float:
    """The gain a bet must have before the profit take sells it, per contract in probability.
    The clock's bar: the share of the bet's own room (1 less its all-in cost) that the market's time has used up, none at the open and all of it at the close. Early, any gain is worth cashing since the side can be bought back; late, the bar climbs toward the whole room.
    The late cap (2026-11-14, user, on KXSOL15M-26NOV141530 NO bought at 0.78, back at 0.96 with 30 s left after a fall to 0.37, then lost on a flip at the bell: "Yes we should have cashed that out"): once what is left to win (the room less the gain) is no more than this market's own swing over the time left (unit * sqrt(left / 60)), the bar is never over half the room. A bet that has made more than it still stands to make, in a market that can take the rest back, is cashed; in a market too quiet to move that far in the time left, the clock's bar stands and the bet rides to its win. `unit` is the market's typical minute (quoteshock.typical_minute); with no reading of it the clock's bar stands."""
    if room <= 0 or window <= 0:
        return float("inf")
    bar = min(max(1.0 - left / window, 0.0), 1.0) * room
    if unit is not None and unit > 0 and left > 0 and room - gain <= unit * math.sqrt(left / MINUTE):
        bar = min(bar, room / 2.0)
    return bar


def take_profit(gain: float, room: float, left: float, window: float, rebuy_fee: float = 0.0, unit: float | None = None) -> bool:
    """True when a profit is worth taking now, read off the bet itself, the clock and the market's own swing (2026-11-14, user, on a BTC NO bought at 0.77 that rose to 0.85, fell to 0.26 and was sold for -67%: "why not just use the bet?"; "there needs to be the time factor too, where earlier in the market is a better favor to sell for profit, since we can always buy back").
    `gain` is the profit per contract in probability (the bid after its sell fee less the cost with its buy fee), `room` the most this bet could still have made (1 less that cost), `left` the seconds to the close, `window` the market's length in seconds (900 for a 15-minute market, structural), `rebuy_fee` the fee to buy the side back, `unit` the market's typical minute. The gain has to pay for the buy-back fee and reach take_bar. No share, price level, coin or number of cents is fixed."""
    if gain <= 0 or room <= 0 or window <= 0 or left <= 0 or gain <= rebuy_fee:
        return False
    return gain >= take_bar(gain, room, left, window, unit)


def still_rising(samples: list[tuple[float, float]], now: float, seconds: float, unit: float | None = None) -> bool:
    """True while the bid is still sloping up over the last `seconds` (2026-11-14, user: "if it's sloping in profit direction don't sell until it stops sloping"), measured in the market's own noise: `unit` is its typical minute (quoteshock.typical_minute), so what it normally moves in the look back is unit * sqrt(seconds / 60).
    Sloping = the bid has risen over the look back by more than that noise AND sits within that noise of the look back's high. A one-tick pause in a climb is inside the noise and does not end it; a bid that has levelled out (the rise has aged out of the look back) or fallen back from its high by more than the noise has stopped.
    With no reading of the unit (under five minutes of quotes) the plain test is used: the newest bid is the look back's high and above where it started. With under `seconds` of readings the slope is unknown and the answer is True (no sale on a bet just opened)."""
    if seconds <= 0:
        return False
    window = [(t, b) for t, b in samples if t >= now - seconds]
    if not window or not samples or samples[0][0] > now - seconds:
        return True
    bid, high, start = window[-1][1], max(b for _, b in window), window[0][1]
    if unit is None or unit <= 0:
        return bid >= high and bid > start
    noise = unit * math.sqrt(seconds / MINUTE)
    return bid - start > noise and high - bid < noise


def still_falling(samples: list[tuple[float, float]], now: float, seconds: float, unit: float | None = None, stay: float = 0.5) -> bool:
    """True while the bid is sliding, not dipping, over the last `seconds` (the squeeze's reading of history, 2026-11-14, user: "If something missed the bar to sell and history thinks we should"; earlier the same day, on a sale they did not want: "It was gradual and it didn't stay").
    A slide = the bid has fallen over the look back by more than the market's own noise for it (unit * sqrt(seconds / 60)), sits within that noise of the look back's low (it has not bounced), AND was already under the look back's start half the look back ago (it has stayed down: one bad print is not a slide until it has lasted half the look back).
    With no reading of the unit the plain test is used (the newest bid is the look back's low and under its start, and the half-way reading was under the start too). With under `seconds` of readings the slope is unknown and the answer is False: no squeeze on a bet just opened."""
    if seconds <= 0:
        return False
    window = [(t, b) for t, b in samples if t >= now - seconds]
    if not window or not samples or samples[0][0] > now - seconds:
        return False
    bid, low, start = window[-1][1], min(b for _, b in window), window[0][1]
    half = [b for t, b in window if t <= now - seconds * stay]  # `stay` is the share of the look back the bid must already have been down for: half for a plain bet, a quarter for one that got in on a stretch (lanes.STRETCHED_STAY)
    if not half or half[-1] >= start:
        return False
    if unit is None or unit <= 0:
        return bid <= low and bid < start
    noise = unit * math.sqrt(seconds / MINUTE)
    return start - bid > noise and bid - low < noise


def stop_miss(net: float, basis: float, q: float, max_loss: float) -> float:
    """How far the bet is from the max-loss stop, per contract in probability: zero or less = at or past it (stopped() is true), positive = that much short of it."""
    return (net - basis) + max_loss * q * basis


def squeeze_loss(pnl: float, miss: float, q: float, line: float, max_loss: float, noise: float) -> bool:
    """Whether a losing bet may be squeezed (lane field squeeze_seconds): it has FLIPPED, the chance the side pays is under the lane's line (the market now prices a win as the less likely result), and it is at, past or within the market's own noise of the max-loss stop.
    A loss on a bet still priced over the line is never squeezed, however deep (2026-11-14, user, on sales at 0.52 and 0.46 with nine minutes left: "it didn't even cross the strike"; then, asked whether three minutes left is too early: "Yes 3 minutes is too soon, unless the vision has flipped and win looks unlikely"). The caller adds the time left (the swing hold comes first), the history read (still_falling) and the hour view."""
    if pnl >= 0 or noise <= 0 or line <= 0 or q >= line:
        return False
    return max_loss <= 0 or miss < noise


def squeeze_profit(gain: float, high_gain: float, room: float, left: float, window: float, rebuy_fee: float, noise: float) -> bool:
    """Whether a profit that had cleared the take's bar and was held for its slope has now slipped just under it (lane field squeeze_seconds): the gain at the look back's high was over the bar, the gain now is under it by less than the market's own noise, and it still pays the buy-back fee. The caller adds that the bid is under that high. A gain that never cleared the bar is not squeezed: on a profit the squeeze only stops a cleared gain from getting away."""
    if gain <= 0 or room <= 0 or window <= 0 or left <= 0 or gain <= rebuy_fee or noise <= 0:
        return False
    bar = min(max(1.0 - left / window, 0.0), 1.0) * room
    return high_gain >= bar and 0.0 < bar - gain < noise
