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


def stop_wait(q: float, line: float, left: float, base: float, share: float, window: float = 900.0) -> float:
    """Seconds the max-loss stop waits before it sells (lane field curve_risk_stop_hold = `share`; 0 = sells at once).
    Until 2026-11-14 it was `share` of the time left and nothing else: 100 s with three and a half minutes left, 270 s with nine, the same at 2 cents past the stop as at 40. Three bets rode to nothing inside that wait (ZEC 18:37, BTC 16:35, BTC 18:28). User: "The stops wait sounds incomplete"; "Is it missing a cap or something?"; "it also needs to be scaled on time left. Just like the nudge behavior we added".
    Now: the market's own recovery time (`base`: the median seconds its fast falls took to regain half of themselves over the last hour, or the lane's default), less the deeper the chance `q` is under the lane's `line` (none at q = 0), times the share of the market still to run (left / window: with less time left there is less time for a dip to come back, so less patience), and never over `share` of the time left."""
    if share <= 0 or left <= 0:
        return 0.0
    severity = min(1.0, max(0.0, (line - q) / line)) if line > 0 else 0.0
    clock = min(1.0, max(0.0, left / window)) if window > 0 else 1.0
    return max(0.0, min(base * (1.0 - severity) * clock, share * left))


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


def multiscale_rate(ts: list[float], vs: list[float], now: float, scales: tuple[int, ...], elapsed: float | None = None) -> float | None:
    """The hour view's movement read at several scales together (2026-11-15, user: "the hour view should look further, but we need the context of the current hour ... in 15, 30, 60, 120, 180 mins"): for each scale m minutes, the root-mean-square one-minute move over the last m minutes over sqrt(60) (what hour_rate reads for m = 60), and the geometric mean of those rates, so each scale counts as a factor of the next and none is picked. A scale is used only when the history holds at least min(m, MIN_MOVES) of its one-minute moves; None when no scale can be read. studies/results/2026-11-15-multiscale-hour-check.txt and 2026-11-15-market-start-segments-check.txt: the log-mean of 15, 30, 60, 120 and 180 beat the 60-minute block by 0.0035 log loss (0.0024 on the last third, which no weight was fitted on); with the market so far (scale 0) added to 15, 60 and 180 it beat it by 0.0031, and by 0.0040 on the last third; the cumulative chain [0, -15, -75, -255] (the market so far, then windows reaching 15, 75 and 255 minutes past the open) by 0.0036 and by 0.0041 on the last third, where separate adjacent stretches (-0.0013) did not hold."""
    if not ts or len(ts) != len(vs) or not scales:
        return None
    # A scale of 0 is the market so far (2026-11-15 night, user: "start with the view from now - market start, 15 after that, 60, 180"): from the market's open to now, `elapsed` seconds, at least two minutes. Every scale ends now.
    # A negative scale reaches back that many minutes PAST the market's open (2026-11-15 night, user: "I meant 15 min after the first segment"): -15 is the market so far plus the 15 minutes before it, every window still ending now.
    so_far = max(2, int(round(elapsed / MINUTE))) if elapsed is not None else None
    minutes = [m if m > 0 else so_far - m for m in scales if m > 0 or so_far is not None]
    if not minutes:
        return None
    scales = tuple(minutes)
    levels: list[float] = []
    for k in range(max(scales) + 1):
        i = bisect.bisect_right(ts, now - k * MINUTE) - 1
        if i < 0 or vs[i] <= 0:
            break
        levels.append(math.log(vs[i]))
    moves2 = [(levels[k] - levels[k + 1]) ** 2 for k in range(len(levels) - 1)]
    logs: list[float] = []
    for m in scales:
        block = moves2[:m]
        if len(block) < min(m, MIN_MOVES):
            continue
        v = sum(block) / len(block) / MINUTE
        if v <= 0:
            return None
        logs.append(0.5 * math.log(v))
    return math.exp(sum(logs) / len(logs)) if logs else None


def long_chance(ts: list[float], vs: list[float], now: float, ref: float, side: str, left: float, hours: float = 1.0, stale: float = 30.0, scales: tuple[int, ...] = (), elapsed: float | None = None) -> float | None:
    """The long layer's chance that the side pays (2026-11-13, user, after a live sale of a bet that dipped briefly, never crossed the strike and went on to win: "the hour view showed it likely staying positive"): the distance of the Kalshi perp from its own value at the window open, over the typical movement of the perp across the last hour (rms of its 60 s moves, per sqrt second) for the time left. None when the hour is too short, the newest tick is stale, or the reference is unusable (the sale then does not wait for this layer)."""
    if ref <= 0 or left <= 0 or not ts or len(ts) != len(vs):
        return None
    j = bisect.bisect_right(ts, now) - 1
    if j < 0 or now - ts[j] > stale or vs[j] <= 0:
        return None
    rate = multiscale_rate(ts, vs, now, scales, elapsed) if scales else hour_rate(ts, vs, now, hours)
    if rate is None:
        return None
    sign = 1.0 if side == "yes" else -1.0
    cushion = (math.log(vs[j]) - math.log(ref)) * sign
    if rate <= 0:
        return 1.0 if cushion > 0 else 0.0
    return _N.cdf(cushion / (rate * math.sqrt(left)))


def side_slopes(ts: list[float], vs: list[float], now: float, side: str, scales: tuple[int, ...] = (3, 5, 13, 21), hour: int = 60) -> dict[int, float] | None:
    """Where the market stands in its wave, side-signed (2026-11-15 night, user: "betting when we shouldn't, and on the wrong side ... crypto moves in waves, directions shift in minutes"): for each scale m minutes, the side's move over the last m minutes in units of that scale's own typical move, z_m = side x (L(now) - L(now - m)) / (s1 x sqrt(m)), s1 the root-mean-square one-minute log move of the last `hour` minutes. Positive = the side's price has been rising at that scale. None without a tick at every minute mark of the last hour and the longest scale, or with a flat hour. studies/results/2026-11-15-rotation-at-entry.txt: buys with z13 of 1.5 or more won 35% (n 26, -25.0%); the bottom fifth of z5 - z21 won 48% (n 105, -14.7%) against 63 to 69% elsewhere."""
    if not ts or len(ts) != len(vs):
        return None
    marks = max(max(scales), hour) + 1
    levels: list[float] = []
    for k in range(marks):
        i = bisect.bisect_right(ts, now - k * MINUTE) - 1
        if i < 0 or vs[i] <= 0 or (k == 0 and now - ts[i] > 30.0):
            return None
        levels.append(math.log(vs[i]))
    s1 = math.sqrt(sum((levels[k] - levels[k + 1]) ** 2 for k in range(hour)) / hour)
    if s1 <= 0:
        return None
    sign = 1.0 if side == "yes" else -1.0
    return {m: sign * (levels[0] - levels[m]) / (s1 * math.sqrt(m)) for m in scales}


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


CUSHION_NUDGE = 0.02  # 2026-11-15 (user: "Yes the .02", after ETH YES 07:23 sold at 0.88 on a cushion 0.015 short and went to 0.98): the most the curve's chance may fall short of what the sale pays and still count as backing it. The same two cents as the buy's stretch and the sale's squeeze.


def cushion_holds(chance: float, net_bid: float, allowance: float = 0.0) -> bool:
    """True when the spot's cushion says the bet is worth more held than sold (2026-11-14, user, after a BTC NO sold at 0.83 that "was never close to losing" and a ZEC NO sold at 0.89: "we just need a cushion check before we decide to sell (in the profit flow)").
    `chance` is the curve's chance, from the spot's distance to the strike over the time left in the market's own variance (history widened), that the side pays; `net_bid` is what the sale pays after its fee. Holding is worth `chance` of a dollar on that read, the sale `net_bid`: when the cushion's chance is the larger, the cushion says hold. No share, price level or number of cents is fixed; the chance and the bid are the market's own."""
    return chance > net_bid - allowance  # with the allowance (CUSHION_NUDGE on the live take) a chance within that much under the price still holds


LATE_SHARE = 0.2  # the last fifth of a market (three minutes of fifteen): where a flat bid is held, not sold (2026-11-14, user, "a flat slope under 3 mins is perfectly fine, unless it starts to dip while profitable we should not sell"); a share of the market's length, which is structural


HIGH_BID = 0.95  # 2026-11-14 (user: "I don't think when it's above 95 we need to consider an early sale as long as it has cushion"): from this bid up, a flat bid is held whenever the spot's cushion backs the price, at any time left, as in the last fifth of the market. The user's own number: five cents is all that is left to win.


def confirm_bar(confirm: float, left: float, window: float = 900.0) -> float:
    """The chance the hour view must reach to hold a risk sale (lane field curve_risk_confirm), faded as the close nears (2026-11-15, user: "Yes replay it so it does the fade", after ETH 09:00 YES was held to a full loss: the stop wanted out from 104 s left and the hour view, which is slow, stayed at 0.44 to 0.61 until 37 s left).
    The lane's `confirm` with more than the last LATE_SHARE of the market left, rising straight to 1.0 (the view can never hold) at the close: a dip with time left may still come back, one with a minute left cannot. Same last fifth as the flat hold; replayed on 98 live bets: +$47.32 against the constant bar (5 better, 1 worse), studies/results/2026-11-15-hour-view-fade-replay.txt."""
    if confirm <= 0:
        return confirm
    clock = min(max(1.0 - left / (window * LATE_SHARE), 0.0), 1.0) if window > 0 else 1.0
    return confirm + (1.0 - confirm) * clock


def squeeze_clock(left: float, window: float = 900.0) -> float:
    """The clock's share for the squeeze, 0 to 1 (2026-11-14, user: "squeeze shouldn't even apply at 6 minutes in, i thought we covered this"; earlier "at that time distance it should be nonexistent"). Nothing in the first half of the market, then rising to all of it at the close: a linear share of the market gone still allowed 0.8 of a cent six minutes in."""
    if window <= 0:
        return 0.0
    used = min(max(1.0 - left / window, 0.0), 1.0)
    return max(0.0, 2.0 * used - 1.0)


def flat_holds(net_bid: float, chance: float | None, unit: float | None, left: float) -> bool:
    """True when the spot's cushion backs the price well enough for a flat bid to be held late (2026-11-14, user: "flat and low cushion is an acceptable sell"; "slopping upward with high cushion is not"; on SOL NO 15:30, back at 0.95 with 30 s left on a curve chance of 0.61, which lost at the bell).
    The gap is what the sale pays net of its fee less the curve's chance that the side pays (the cushion check's own two numbers); the cushion is thin when that gap is more than the market's swing over the time left (the typical minute x sqrt(left / 60), the late cap's own yardstick). With no reading of the chance or the unit the cushion is unknown and a flat bid is not held."""
    if chance is None or unit is None or unit <= 0 or left <= 0:
        return False
    return net_bid - chance <= unit * math.sqrt(left / MINUTE)


def still_rising(samples: list[tuple[float, float]], now: float, seconds: float, unit: float | None = None, flat_ok: bool = False) -> bool:
    """True while the bid is still sloping up over the last `seconds` (2026-11-14, user: "if it's sloping in profit direction don't sell until it stops sloping"), measured in the market's own noise: `unit` is its typical minute (quoteshock.typical_minute), so what it normally moves in the look back is unit * sqrt(seconds / 60).
    Sloping = the bid has risen over the look back AND sits within that noise of the look back's high (the rise itself needs no minimum: the noise is read over the last 15 minutes, which a climb inflates). With `flat_ok` (the last LATE_SHARE of the market) a flat bid holds too: only a dip below the noise from the high ends the hold. A one-tick pause in a climb is inside the noise and does not end it; a bid that has levelled out (the rise has aged out of the look back) or fallen back from its high by more than the noise has stopped.
    With no reading of the unit (under five minutes of quotes) the plain test is used: the newest bid is the look back's high and above where it started. With under `seconds` of readings the slope is unknown and the answer is True (no sale on a bet just opened)."""
    if seconds <= 0:
        return False
    window = [(t, b) for t, b in samples if t >= now - seconds]
    if not window or not samples or samples[0][0] > now - seconds:
        return True
    bid, high, start = window[-1][1], max(b for _, b in window), window[0][1]
    if unit is None or unit <= 0:
        return bid >= high and (flat_ok or bid > start)
    noise = unit * math.sqrt(seconds / MINUTE)
    return (flat_ok or bid > start) and high - bid < noise  # 2026-11-14 (user: "the increase in profit should be the main driver for holding"): any rise over the look back counts; the noise only says how far under its high the bid may sit. Was: the rise had to beat the noise, which the climb itself inflates (SOL 21:15 YES, a rise of 0.102 against a bar of 0.105, sold at 0.982 and the bid went on to 0.996)


def still_falling(samples: list[tuple[float, float]], now: float, seconds: float, unit: float | None = None) -> bool:
    """True while the bid is sliding, not dipping, over the last `seconds` (the squeeze's reading of history, 2026-11-14, user: "If something missed the bar to sell and history thinks we should"; earlier the same day, on a sale they did not want: "It was gradual and it didn't stay").
    A slide = the bid has fallen over the look back by more than the market's own noise for it (unit * sqrt(seconds / 60)), sits within that noise of the look back's low (it has not bounced), AND was already under the look back's start half the look back ago (it has stayed down: one bad print is not a slide until it has lasted half the look back).
    With no reading of the unit the plain test is used (the newest bid is the look back's low and under its start, and the half-way reading was under the start too). With under `seconds` of readings the slope is unknown and the answer is False: no squeeze on a bet just opened."""
    if seconds <= 0:
        return False
    window = [(t, b) for t, b in samples if t >= now - seconds]
    if not window or not samples or samples[0][0] > now - seconds:
        return False
    bid, low, start = window[-1][1], min(b for _, b in window), window[0][1]
    half = [b for t, b in window if t <= now - seconds / 2.0]
    if not half or half[-1] >= start:
        return False
    if unit is None or unit <= 0:
        return bid <= low and bid < start
    noise = unit * math.sqrt(seconds / MINUTE)
    return start - bid > noise and bid - low < noise


def stop_miss(net: float, basis: float, q: float, max_loss: float) -> float:
    """How far the bet is from the max-loss stop, per contract in probability: zero or less = at or past it (stopped() is true), positive = that much short of it."""
    return (net - basis) + max_loss * q * basis
