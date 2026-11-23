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


def hour_rate(ts: list[float], vs: list[float], now: float, hours: float = 1.0) -> float | None:
    """How much this index has been moving over the last hour, per square-root second in log terms: the root-mean-square of its one-minute moves over that hour, over sqrt(60). Times sqrt(seconds) it is the typical move over that many seconds. None when the hour has under MIN_MOVES one-minute moves on record.
    The one reading of volatility for both ends of a bet (2026-11-14, user, on a ladder width read from six hours of bars times a multiplier fitted on six hours of outcomes: "These markets shift in behavior hourly if you're looking at 6 hours to base a decision on right now that is totally irrelevant"): the exit's long layer (long_chance) and the ladder's width for a market priced from the Kalshi perp (commodities.price_event)."""
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
    """The long layer's movement read at several scales together (2026-11-15, user: "the long layer should look further, but we need the context of the current hour ... in 15, 30, 60, 120, 180 mins"): for each scale m minutes, the root-mean-square one-minute move over the last m minutes over sqrt(60) (what hour_rate reads for m = 60), and the geometric mean of those rates, so each scale counts as a factor of the next and none is picked. A scale is used only when the history holds at least min(m, MIN_MOVES) of its one-minute moves; None when no scale can be read. studies/results/2026-11-15-multiscale-hour-check.txt and 2026-11-15-market-start-segments-check.txt: the log-mean of 15, 30, 60, 120 and 180 beat the 60-minute block by 0.0035 log loss (0.0024 on the last third, which no weight was fitted on); with the market so far (scale 0) added to 15, 60 and 180 it beat it by 0.0031, and by 0.0040 on the last third; the cumulative chain [0, -15, -75, -255] (the market so far, then windows reaching 15, 75 and 255 minutes past the open) by 0.0036 and by 0.0041 on the last third, where separate adjacent stretches (-0.0013) did not hold."""
    if not ts or len(ts) != len(vs) or not scales:
        return None
    # A scale of 0 is the market so far (2026-11-15 night, user: "start with the view from now - market start, 15 after that, 60, 180"): from the market's open to now, `elapsed` seconds, at least two minutes. Every scale ends now.
    # A negative scale reaches back that many minutes PAST the market's open (2026-11-15 night, user: "I meant 15 min after the first segment"): -15 is the market so far plus the 15 minutes before it, every window still ending now.
    so_far = max(2, round(elapsed / MINUTE)) if elapsed is not None else None
    minutes = [m if m > 0 else (so_far or 0) - m for m in scales if m > 0 or so_far is not None]
    if not minutes:
        return None
    # Late in a market the market so far (0) and the 15-minute segment are the same stretch of history and would count twice in the mean (user, 2026-11-16: "0 being the current market, so nearly the end of a market the 0 and 15 values are likely identical and count double"). Windows within one minute of each other, the resolution of the moves, are one segment.
    kept: list[int] = []
    for m in sorted(set(minutes)):
        if not kept or m - kept[-1] > 1:
            kept.append(m)
    scales = tuple(kept)
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
    """The long layer's chance that the side pays (2026-11-13, user, after a live sale of a bet that dipped briefly, never crossed the strike and went on to win: "the long layer showed it likely staying positive"): the distance of the Kalshi perp from its own value at the window open, over the typical movement of the perp across the last hour (rms of its 60 s moves, per sqrt second) for the time left. None when the hour is too short, the newest tick is stale, or the reference is unusable (the sale then does not wait for this layer)."""
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


SPIKE_LOOK = 90.0  # seconds: the stretch a spike is read over, as in the study
SPIKE_STRETCHES = 120  # back-to-back stretches of that length before it: 3 hours


def spike_view(ts: list[float], vs: list[float], now: float, side: str) -> tuple[float, int] | None:
    """How big the last 90 seconds were on the 3-hour view of the market's Kalshi perp (2026-11-16, user, on the faded spikes that never came back: "What did their historical view look like? How much of a spike was it? Does the 3 hour view show similar spikes?"; "Yes, log it").
    Returns (z, similar): z is the perp's log move over the last SPIKE_LOOK seconds in units of its own typical move over that span (the root-mean-square of the SPIKE_STRETCHES back-to-back stretches before it), signed by the side (+ = toward the bet side); similar is how many of those stretches moved as far or further. None without a tick within two minutes of every mark, or with a flat 3 hours.
    studies/results/2026-11-16-spike-failures-history.txt (1,295 spikes against the market since 11-09): where |z| was under 0.5 the faded side won back half the spike in 88% and holding it made +19.1% (n 124); at 3 and over, 53% and -36.8% (n 68); with 25 or more similar moves in the 3 hours 82% and +6.3% (n 585); with none 45%, and the spiking side won 81% (n 31). SHADOW: a reading for the note, it gates nothing."""
    if not ts or len(ts) != len(vs):
        return None
    levels: list[float] = []
    for n in range(SPIKE_STRETCHES + 2):
        at = now - SPIKE_LOOK * n
        i = bisect.bisect_right(ts, at) - 1
        if i < 0 or at - ts[i] > 120.0 or vs[i] <= 0:
            return None
        levels.append(math.log(vs[i]))
    last = levels[0] - levels[1]
    past = [levels[n] - levels[n + 1] for n in range(1, SPIKE_STRETCHES + 1)]
    rms = math.sqrt(sum(x * x for x in past) / len(past))
    if rms <= 0:
        return None
    return (1.0 if side == "yes" else -1.0) * last / rms, sum(1 for x in past if abs(x) >= abs(last))


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


def trail_hit(gain: float, rebuy_fee: float, bid: float, peak: float, unit: float | None, units: float) -> bool:
    """True when a bet that is ahead has come off its high far enough to cash what is left of the gain (lane field curve_take_trail; 2026-11-16, user, on an XRP NO bought at 0.60 that reached 0.80 with five minutes left, short of the take's bar, and was sold at 0.25: "we should be tracking every market that we buy with a trail").
    `gain` is the profit per contract after the sell fee, `rebuy_fee` the fee to buy the side back, `peak` the highest bid since the buy, `unit` the market's typical minute, `units` how many of them under the peak the bid must be. The fall is measured in the market's own swing, not in cents, so a jumpy market is given more rope than a quiet one. False with no typical minute (no reading, no rule), with the trail off, or once the gain is gone: a bet under water is the loss exit's business."""
    if units <= 0 or unit is None or unit <= 0 or peak <= 0 or gain <= rebuy_fee:
        return False
    return bid <= peak - units * unit + 1e-9


CUSHION_NUDGE = 0.02  # 2026-11-15 (user: "Yes the .02", after ETH YES 07:23 sold at 0.88 on a cushion 0.015 short and went to 0.98): the most the curve's chance may fall short of what the sale pays and still count as backing it. The same two cents as the buy's stretch and the sale's squeeze.


def cushion_holds(chance: float, net_bid: float, allowance: float = 0.0) -> bool:
    """True when the spot's cushion says the bet is worth more held than sold (2026-11-14, user, after a BTC NO sold at 0.83 that "was never close to losing" and a ZEC NO sold at 0.89: "we just need a cushion check before we decide to sell (in the profit flow)").
    `chance` is the curve's chance, from the spot's distance to the strike over the time left in the market's own variance (history widened), that the side pays; `net_bid` is what the sale pays after its fee. Holding is worth `chance` of a dollar on that read, the sale `net_bid`: when the cushion's chance is the larger, the cushion says hold. No share, price level or number of cents is fixed; the chance and the bid are the market's own."""
    return chance > net_bid - allowance  # with the allowance (CUSHION_NUDGE on the live take) a chance within that much under the price still holds


LATE_SHARE = 0.2  # the last fifth of a market (three minutes of fifteen): where a flat bid is held, not sold (2026-11-14, user, "a flat slope under 3 mins is perfectly fine, unless it starts to dip while profitable we should not sell"); a share of the market's length, which is structural




def sure_bar(left: float, sure_late: float, window: float = 900.0, slide: float = -1.0) -> float | None:
    """The bid at or over which a bet counts as a sure win with `left` seconds to go, when the lane slides it (lane field curve_take_sure_slide), else None.
    2026-11-17, user, after cashing out KXNEAR15M-26NOV171500 by hand at about 0.95 with over four minutes left while the bot held it as a sure win: "we want the sure win, but it should be time based"; "if there's 90s left and we are in the 90s, probably a good chance of winning"; "5 mins left? that is too much time"; and, on a version with a cut-off at 90 s: "do not use hard limits".
    No step anywhere: the bar is `sure_late` at the close and rises in a straight line to 1.00 (nothing is sure) when `slide` of the market is still to run. With sure_late 0.90 and slide a third of a 15-minute market: 0.90 at the close, 0.92 with a minute left, 0.93 with 90 s, 0.96 with three minutes, 1.00 from five minutes out."""
    if sure_late <= 0 or slide <= 0 or window <= 0:
        return None
    return sure_late + (1.0 - sure_late) * min(1.0, max(left, 0.0) / (window * slide))


def sure_win(bid: float, left: float, sure: float = -1.0, sure_late: float = -1.0, window: float = 900.0, slide: float = -1.0) -> bool:
    """True when a bet is far enough ahead that the profit take should hold it to settlement (2026-11-15 night, user: "we keep selling our profit at the last second, always taking a fee ... if it stops these sure wins from selling, sure"): the bid is at or over `sure` at any time, or at or over `sure_late` in the last LATE_SHARE of the market. A sale there gives up the cents to a full dollar and pays the fee for protection the market's price already covers: studies/results/2026-11-15-late-take-check.txt, 82 live takes: at a bid of 0.95 and over 0 of 12 saved a bet (sale less hold -7.07), at 0.90 to 0.95 in the last 180 s 0 of 8 (-18.45), while the takes earlier and lower earned +115 and saved 12 bets. Off (-1) by default."""
    if sure > 0 and bid >= sure:
        return True
    bar = sure_bar(left, sure_late, window, slide)
    if bar is not None:
        return bar < 1.0 and bid >= bar  # the sliding bar: no bid is a sure win while the bar is at 1.00
    return sure_late > 0 and window > 0 and left <= window * LATE_SHARE and bid >= sure_late


def confirm_bar(confirm: float, left: float, window: float = 900.0) -> float:
    """The chance the long layer must reach to hold a risk sale (lane field curve_risk_confirm), faded as the close nears (2026-11-15, user: "Yes replay it so it does the fade", after ETH 09:00 YES was held to a full loss: the stop wanted out from 104 s left and the long layer, which is slow, stayed at 0.44 to 0.61 until 37 s left).
    The lane's `confirm` with more than the last LATE_SHARE of the market left, rising straight to 1.0 (the view can never hold) at the close: a dip with time left may still come back, one with a minute left cannot. Same last fifth as the flat hold; replayed on 98 live bets: +$47.32 against the constant bar (5 better, 1 worse), studies/results/2026-11-15-hour-view-fade-replay.txt."""
    if confirm <= 0:
        return confirm
    clock = min(max(1.0 - left / (window * LATE_SHARE), 0.0), 1.0) if window > 0 else 1.0
    return confirm + (1.0 - confirm) * clock


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


UNDER_EVEN_MIN_WAIT = 15.0  # seconds a bid must stay under even before the under-even sale, however little time is left (2026-11-19, user, on copper 14:00: 7 s under even was enough with 51 s left: "it needs to wait longer, or have a min wait"). The price recovered within 8 s. A chosen setting, not fitted
EVEN = 0.5  # the price at which the market says the side is no longer the favorite: structural, not fitted


def under_even(bid: float, entry: float, under_for: float, left: float, share: float, unit: float | None = None) -> bool:
    """The under-even sale's condition (lane field curve_risk_under_even; 2026-11-17, user, after three live commodity bets bought near 0.90 went to zero inside an hour: "we need to fix this"): a bet bought as the favorite (at or over even) whose bid has now been under even, without a break, for `share` of the time left. The wait shrinks with the clock: 30 s with five minutes to go, 3 s with thirty seconds. Evidence: studies/results/2026-11-17-commodity-loss-trace.txt (live commodity bets: +$6.45 on 48 overnight, +$13.44 on 96 of 11-13 to 11-16, +$1.38 on 422 of 11-09 to 11-13, against what happened) and the seven bets of 11-17 09:20 to 10:20 ET (sells two of the three that went to zero at about 0.40, leaves the four winners; +$8.59)."""
    if not (share > 0 and entry >= EVEN and bid < EVEN and left > 0 and under_for >= max(share * left, UNDER_EVEN_MIN_WAIT)):
        return False
    # Depth (2026-11-19, user, on copper 14:00: NO bought 0.87, sold 0.35 with 51 s left after 7 s under even, settled NO: "it shouldn't cash out if its 50/50", "way too early"): the wait shrinks with the clock, so late a short dip qualified. The bid must also be under even by more than the market's own typical swing over the time left (unit x sqrt(left / 60)): a bid that can walk back to even inside the time left is a coin flip, not a loss. No reading of the unit = the old rule.
    return not (unit is not None and unit > 0 and EVEN - bid <= unit * math.sqrt(left / MINUTE))


def never_ahead(ever_ahead: bool, left: float, at: float, bought_left: float, seen_whole: bool, lead: float = 30.0) -> bool:
    """The never-ahead sale's condition at its one look (lane field curve_risk_never_ahead; 2026-11-17, user: "never profitable and 90s left. Let's add that to the live lanes"): `at` seconds or fewer are left, the bet was bought at least `lead` seconds before that mark (`bought_left` is what was left at the buy), every second of it was watched (`seen_whole`: a bet picked up after a restart has an unknown past and is left alone), and its bid after the sell fee was never once over what it cost with the buy fee. Evidence: studies/results/2026-11-17-time-in-profit.txt (150 live commodity bets since 11-13: never in profit at 90 s left, 6 bets, 2 won if held, selling there +$12.84; at 180 s, 14 bets, 6 won, +$18.12; a bet in profit even briefly won 20 of 24 and is not sold)."""
    return at > 0 and left <= at and seen_whole and bought_left >= at + lead and not ever_ahead


TREND_MIN_MOVES = 12  # mark-to-mark moves (a minute of them) the market must have BEFORE the look back for its typical move to be read
TREND_MIN_SEEN = 100  # readings of the last hour needed before one can be called outside the hour's range
LONG_WEIGHT = 0.5  # how far the long layer counts when it is the worse cushion: risk()'s own default (the price is trusted more)

