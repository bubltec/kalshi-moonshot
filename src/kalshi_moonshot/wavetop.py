"""The wave-top reversal scalp: buy the cheap side at the top of a spot wave that is bigger than the waves before it, and sell a bet that dipped once it has come back.

2026-11-21, user (how they trade these markets by hand): "you need to look at the spot curve. I usually bet on when the slope is larger than previous slopes. So it made a larger wave, bet around the top of that one ... You need to watch it go back and forth a little and then the spot will start to go the opposite slope"; "you buy the low price either down on the peak or up on the dip"; "we want the wave history seamless, not cut off at the start of the market". On the exit, of a bet that went against them first: "it will come back down and we might be able to salvage this for a small profit or small loss ... this is a combination of the spot + kalshi price".

The spot is Kalshi's own: a coin's settlement index or a commodity's perp reference (binaryrisk.curve_index). Everything is in the spot's own typical minute (the root-mean-square 60-second change over the hour before the market opened), never in dollars or by asset.

The buy (buy step `wave_top`):
  curve   the 60-SECOND AVERAGE of the spot (what Kalshi settles on, and what its chart draws), second by second from 3,500 s before the market opened through now, as one unbroken history. Its typical minute is the root-mean-square 60-second change of that average over the hour before the open (the unit of everything below).
  target  the market's strike. A PEAK is the average above the target, a DIP the average below it (user, 2026-11-22: "that's how it determines if it's a peak or dip, which is relative to the target").
          The whole leg stays on that side: a rise that began under the target and crossed it is a flip and not a peak (user, 2026-11-22: "the flip had already happened when you bought").
  wave    the running leg of the average is at least MIN_MOVE_A units long and its slope is above each of the previous PREV legs (user: "I usually bet on when the slope is larger than previous slopes"). Earlier legs are the zigzag of the average with turns confirmed at LEG_A units.
  turn    the raw spot has crossed to the other side of the average (below it on a peak, above it on a dip) while the average has not yet come off its extreme by more than TURN_A units: the spot has started to go the opposite way and the average has not shown it yet. This is the buy: the average lags the spot by about half a minute and Kalshi's price follows the spot, so waiting for the average itself to turn buys after the flip (user, 2026-11-22, on ZEC 18:18 and XRP 02:31: "the flip had already happened when you bought").
  side    NO on a peak, YES on a dip (the side the average is NOT on). A lane's price band says how cheap it must be.
  trend   a dip bet (YES) is not taken when the spot at the open is above its value wave_top_trend_hours before (user: "if the past hour has been rising. i wouldn't give it a whole day"); with no value in memory a dip bet is not taken.
The sale (sell steps `wave_take` and `wave_salvage`): the STAIRCASE (user, 2026-11-22: "staircase, with this: the size should scale with the bid's own typical minute and with time left, not sit at a fixed 5c"; "I like wide early with a back up for avoid loss that accepts a small loss"): the bid's swings are cut at a reversal of rev = max(TICK, wave_rev_k x the bid's typical minute x (max(time left, 60) / 60)^wave_rev_p x the cushion factor 2 x (1 - P_lose)) (wide with a lot of time left and with the spot safely on the bet's side, narrowing toward the close and when it is on the wrong side), and the bet is sold the first time the bid falls under the previous swing low (a lower low: the climb is done) once the bet has been ahead after both fees, at a gain or at a small loss; and the salvage of a bet that went under water: its bid at least one of its own typical minutes under the bid at the buy is sold once its bid has come at least half a unit off its low, the climb over the last quarter of the time since the low has slowed to a quarter of the run's pace, and the spot has stopped moving the bet's way over that stretch.

2026-11-22: the buy was rebuilt on the average curve against the target. The first version (raw spot, a wave of at least 2 typical minutes, top 10% of the market's own legs, bought AFTER the leg and its bounce) bought nine bets on 11-21 and 11-22 for -$0.56: six lost, and the user's review found the entries came after the flip, on the wrong end (ZEC 18:18 bought YES 14 s after the drop through the target that followed the spike; XRP 02:31 bought NO 49 s after the jump through it). The settings below (MIN_MOVE_A, TURN_A, LEG_A, TAKE) are mine, chosen after those cases, not studied: a lead, on paper first (CLAUDE.md rule 5). The earlier evidence for the first version is in scripts/spot_wave_replay_cache.py and studies/results/2026-11-21-spot-wave-*.txt, which still run `Zig` on the raw spot.

Memory only: the tick history and the quote history. Nothing here reads the database. stdlib only.
"""

from __future__ import annotations

import bisect
import itertools
import math
import threading
from dataclasses import dataclass
from typing import Any

R_TURN = 1.0  # typical minutes back from the extreme that confirm a turn
MIN_MOVE = 2.0  # the wave's least size, in typical minutes
PREV = 3  # the wave's slope is above each of this many legs before it
MIN_LEGS = 8  # legs needed before the quantile is read
HOLD_F = 0.25  # the top has stood for this share of the wave's own length
BACK_U = 0.25  # the spot is back this far from the top ...
BOUNCE_U = 0.10  # ... and has bounced this far off its low since
LOOK = 3500  # seconds of legs before the open
UNIT_SPAN = 3600  # the typical minute is read over this long before the open
TICK = 0.01  # the exchange's price step: a turn down from the high is at least one tick (a bid that stays at its high is not a turn)
# the staircase's size is the lane's wave_rev_k, wave_rev_p and wave_cushion (lanes.py): rev = max(TICK, k x u x (max(T, 60) / 60)^p x 2 x (1 - P_lose)); the defaults are mine, from two NEAR bets, to be set by the replay (CLAUDE.md rule 4)
AVG_SECONDS = 60  # the average curve: the settlement's own span
LEG_A = 0.25  # legs of the average are confirmed when it comes this far (in its own typical minutes) off the extreme
MIN_MOVE_A = 0.5  # the running leg of the average is at least this long
TURN_A = 0.25  # the buy is open until the average has come this far off its extreme


@dataclass
class Wave:
    d: int  # +1 an up wave (the bet is NO), -1 a down wave (the bet is YES)
    slope: float  # typical minutes per minute
    prev_max: float
    cut: float  # the quantile's value among the legs
    legs: int
    move: float  # the wave's size in typical minutes
    back: float
    bounce: float
    unit: float
    ext_ts: float
    ext: float
    spot_open: float

    @property
    def side(self) -> str:
        return "no" if self.d == 1 else "yes"


def grid(T: list[float], V: list[float], a: int, b: int) -> list[float | None]:
    """The last value at or before each whole second in [a, b)."""
    j = bisect.bisect_right(T, a) - 1
    cur = V[j] if j >= 0 else None
    out: list[float | None] = []
    n = len(T)
    for s_ in range(a, b):
        while j + 1 < n and T[j + 1] <= s_:
            j += 1
            cur = V[j]
        out.append(cur)
    return out


def typical_minute(sp: list[float], i_open: int) -> float:
    """The root-mean-square 60-second change over the UNIT_SPAN seconds before index i_open, read every 10 s."""
    ch = [sp[k] - sp[k - 60] for k in range(max(60, i_open - UNIT_SPAN + 60), i_open, 10)]
    return math.sqrt(sum(c * c for c in ch) / len(ch)) if ch else 0.0


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def spot_read(T: list[float], V: list[float], now: float, strike: float, side: str, avg_seconds: int = AVG_SECONDS) -> tuple[float, float] | None:
    """The spot's cushion over the target for the bet and its typical minute, from the last ten minutes of ticks: (cushion, unit), the cushion the 60-second average's distance past the strike on the bet's side (negative = on the wrong side), the unit the root-mean-square one-second change times sqrt(60). None when the ticks do not cover the last minute or are too few."""
    if not T or now - T[-1] > 10.0 or strike <= 0:
        return None
    end = int(now) + 1
    sp = grid(T, V, end - 600, end)
    last = sp[-avg_seconds:]
    if len(last) < avg_seconds or None in last:
        return None
    avg = sum(last) / avg_seconds  # type: ignore[arg-type]
    ch = [b - a for a, b in itertools.pairwise(sp) if a is not None and b is not None]
    if len(ch) < 60:
        return None
    unit = math.sqrt(sum(c * c for c in ch) / len(ch)) * math.sqrt(60.0)
    if unit <= 0:
        return None
    return ((avg - strike) if side == "yes" else (strike - avg)), unit


def p_lose(cushion: float, unit: float, left: float) -> float:
    """The chance the spot's average ends on the wrong side: the cushion over what a random walk moves in the time left (its typical minute x sqrt(left / 60))."""
    return norm_cdf(-cushion / (unit * math.sqrt(max(left, 1.0) / 60.0)))


class Zig:
    """The zigzag, fed one second at a time. `step` moves it; `signal` answers whether the running leg is a wave top at this second."""

    def __init__(self, k0: int, v0: float, unit: float, quantile: float, r_turn: float = R_TURN) -> None:
        self.unit, self.quantile, self.r_turn = unit, quantile, r_turn
        self.legs: list[tuple[int, float]] = []  # (direction, slope) of each confirmed leg
        self.d = 0
        self.piv = (k0, v0)
        self.ext = (k0, v0)

    def step(self, k: int, v: float) -> None:
        d = self.d
        if d == 0:
            if abs(v - self.piv[1]) >= self.r_turn * self.unit:
                self.d = 1 if v > self.piv[1] else -1
                self.ext = (k, v)
            return
        if (v - self.ext[1]) * d > 0:
            self.ext = (k, v)
        elif (self.ext[1] - v) * d >= self.r_turn * self.unit:  # the turn is confirmed: the running leg is complete
            dur = max(self.ext[0] - self.piv[0], 1)
            self.legs.append((d, abs(self.ext[1] - self.piv[1]) / self.unit / (dur / 60.0)))
            self.piv = self.ext
            self.d = -d
            self.ext = (k, v)

    def signal(self, k: int, sp: list[float]) -> tuple[Wave | None, str]:
        """(the wave, "") when second k is a wave top, else (None, why not)."""
        d, unit = self.d, self.unit
        if d == 0 or len(self.legs) < 2:
            return None, f"only {len(self.legs)} legs so far"
        v = sp[k]
        dur = self.ext[0] - self.piv[0]
        move = abs(self.ext[1] - self.piv[1]) / unit
        if dur < 1 or move < MIN_MOVE:
            return None, f"the running leg is {move:.1f} typical minutes (needs {MIN_MOVE:g})"
        slope = move / (dur / 60.0)
        prev = max(x[1] for x in self.legs[-PREV:])
        if slope <= prev:
            return None, f"the leg's slope {slope:.2f} is not above the last {PREV} legs' {prev:.2f}"
        cut = 0.0
        if self.quantile > 0:
            allv = sorted(x[1] for x in self.legs)
            if len(allv) < MIN_LEGS:
                return None, f"{len(allv)} legs in the history (needs {MIN_LEGS} to read the quantile)"
            cut = allv[min(int(self.quantile * len(allv)), len(allv) - 1)]
            if slope < cut:
                return None, f"the leg's slope {slope:.2f} is under the {self.quantile:.0%} mark of the market's own legs ({cut:.2f})"
        if k - self.ext[0] < HOLD_F * dur:
            return None, f"the top is {k - self.ext[0]}s old (needs {HOLD_F:g} of the wave's {dur}s)"
        low_w = min(x * d for x in sp[self.ext[0] : k + 1])
        back = (self.ext[1] - v) * d / unit
        bounce = (v * d - low_w) / unit
        if back < BACK_U or bounce < BOUNCE_U:
            return None, f"no back and forth yet: {back:.2f} back from the top (needs {BACK_U:g}), {bounce:.2f} off the low (needs {BOUNCE_U:g})"
        return Wave(d, slope, prev, cut, len(self.legs), move, back, bounce, unit, float(self.ext[0]), self.ext[1], 0.0), ""


def average(sp: list[float | None]) -> list[float | None]:
    """The trailing AVG_SECONDS-second average of a second-by-second path; None until a full span of values is behind a second."""
    out: list[float | None] = [None] * len(sp)
    run, bad = 0.0, 0
    for k, v in enumerate(sp):
        if v is None:
            bad += 1
        else:
            run += v
        if k >= AVG_SECONDS:
            old = sp[k - AVG_SECONDS]
            if old is None:
                bad -= 1
            else:
                run -= old
        if k >= AVG_SECONDS - 1 and bad == 0:
            out[k] = run / AVG_SECONDS
    return out


def signal_at(z: Zig, avg: float, raw: float, strike: float, unit: float, spot_open: float) -> tuple[Wave | None, str]:
    """Whether the zigzag `z` of the average (already stepped through this second) is a wave top now: `avg` and `raw` are the average and the spot at this second, `unit` the average's typical minute, `spot_open` the spot at the open. (Wave, "") or (None, why not). The live read and the replay (scripts/wave_avg_replay_cache.py) both ask this."""
    d = z.d
    if d == 0 or len(z.legs) < 2:
        return None, f"only {len(z.legs)} legs of the average so far"
    dur = z.ext[0] - z.piv[0]
    move = abs(z.ext[1] - z.piv[1]) / unit
    if dur < 1 or move < MIN_MOVE_A:
        return None, f"the average's running leg is {move:.2f} typical minutes (needs {MIN_MOVE_A:g})"
    slope = move / (dur / 60.0)
    same = [x[1] for x in z.legs if x[0] == d][-PREV:]  # the earlier waves the same way: a rise is judged against earlier rises, a fall against earlier falls
    prev = max(same) if same else 0.0
    if slope <= prev:
        return None, f"the leg's slope {slope:.2f} is not above the last {PREV} {'rises' if d == 1 else 'falls'}' {prev:.2f}"
    if (avg - strike) * d <= 0:
        return None, f"the average {avg:.6g} is {'under' if d == 1 else 'over'} the target {strike:.6g}, so the {'rise' if d == 1 else 'fall'} is not a {'peak' if d == 1 else 'dip'}"
    if (z.piv[1] - strike) * d <= 0:
        return None, f"the leg began at {z.piv[1]:.6g}, on the other side of the target {strike:.6g}: it crossed the target, which is a flip and not a {'peak' if d == 1 else 'dip'}"
    back = (z.ext[1] - avg) * d / unit
    if back > TURN_A:
        return None, f"the average is already {back:.2f} off its extreme (the buy is open to {TURN_A:g}): the flip has started"
    gap = (avg - raw) * d / unit
    if gap <= 0:
        return None, f"the spot {raw:.6g} has not crossed to the other side of its average {avg:.6g} yet"
    return Wave(d, slope, prev, 0.0, len(z.legs), move, back, gap, unit, float(z.ext[0]), z.ext[1], spot_open), ""


def read(T: list[float], V: list[float], opened: float, now: float, strike: float) -> tuple[Wave | None, str]:
    """The wave top at `now` for a market that opened at `opened` and has target `strike`, from the spot's ticks (T ascending, covering opened - 3,700 s to now). (None, why) when there is none or the history is short."""
    a = int(opened) - (UNIT_SPAN + 100)
    ia = UNIT_SPAN + 100
    sp = grid(T, V, a, int(now) + 1)
    if len(sp) <= ia or None in sp[ia - UNIT_SPAN - AVG_SECONDS :]:
        return None, "the spot's history does not cover the hour before the open"
    A = average(sp)
    unit = typical_minute(A, ia)  # type: ignore[arg-type]
    if unit <= 0:
        return None, "the spot's average did not move in the hour before the open"
    z0 = ia - LOOK
    z = Zig(z0, A[z0], unit, 0.0, LEG_A)  # type: ignore[arg-type]
    for k in range(z0 + 1, len(sp)):
        z.step(k, A[k])  # type: ignore[arg-type]
    k = len(sp) - 1
    w, why = signal_at(z, A[k], sp[k], strike, unit, sp[ia])  # type: ignore[arg-type]
    if w is not None:
        w.ext_ts += a
    return w, why


_memo: dict[tuple[str, int, int, float], tuple[Wave | None, str]] = {}
_memo_lock = threading.Lock()


def read_index(index: str, opened: float, now: float, strike: float) -> tuple[Wave | None, str]:
    """`read` on the tick memory, kept for the second it was read in (both sides and every lane of a market ask the same question)."""
    from . import ticks as ticks_mod

    key = (index, int(opened), int(now), strike)
    with _memo_lock:
        if key in _memo:
            return _memo[key]
    got = ticks_mod.history.window(index, opened - (UNIT_SPAN + 100), now)
    out = (None, f"no tick history in memory for {index} back to an hour before the open") if not got or not got[0] else read(got[0], got[1], opened, now, strike)
    with _memo_lock:
        if len(_memo) > 400:
            _memo.clear()
        _memo[key] = out
    return out


def admits(s: Any, shot: Any, spec: Any, now: float, strike: float | None) -> tuple[bool, str]:
    """The buy step's answer for one side: (buy?, the sentence for the decision's note). Memory only."""
    from . import binaryrisk

    index = binaryrisk.curve_index(shot.series or shot.ticker.split("-")[0])
    if not index:
        return False, f"no Kalshi index or perp reference is read for {shot.series}"
    if not strike:
        return False, "the market's target is not in memory yet"
    opened = round((now + shot.hours_to_close * 3600.0) / 900.0) * 900.0 - 900.0
    w, why = read_index(index, opened, now, strike)
    if w is None:
        return False, f"no wave top: {why}"
    told = (
        f"{'peak' if w.d == 1 else 'dip'}: the average {'rose' if w.d == 1 else 'fell'} {w.move:.2f} typical minutes at slope {w.slope:.2f} (last {PREV} legs {w.prev_max:.2f}, {w.legs} legs), "
        f"{w.back:.2f} off its extreme, the spot {w.bounce:.2f} on the other side of the average"
    )
    if shot.side != w.side:
        return False, f"{told}: the bet is {w.side.upper()}, not this side"
    if w.d == -1 and spec.wave_top_trend_hours > 0:
        from . import ticks as ticks_mod

        at = opened - spec.wave_top_trend_hours * 3600.0
        got = ticks_mod.history.window(index, at - 600.0, at)
        if not got or not got[1]:
            return False, f"{told}: a dip bet needs the spot's value {spec.wave_top_trend_hours:g} h before the open, which is not in memory"
        if w.spot_open > got[1][-1]:
            return False, f"{told}: a dip bet after a rising {spec.wave_top_trend_hours:g} h (the spot at the open {w.spot_open:g} is above its {got[1][-1]:g} then) is not taken"
    return True, f"wave top: {told}; buys {w.side.upper()}"


def side_bids(quotes: list[tuple[float, float, float]], side: str, a: int, b: int) -> list[float | None]:
    """The side's own bid at each second in [a, b) from the market's (time, YES bid, YES ask) rows; the seconds before the first quote carry the first quote."""
    T = [q[0] for q in quotes]
    B = [(q[1] if side == "yes" else 1.0 - q[2]) for q in quotes]
    sb = grid(T, B, a, b)
    first = next((x for x in sb if x is not None), None)
    return [first if x is None else x for x in sb]


def salvage(quotes: list[tuple[float, float, float]], side: str, opened: float, bought: float, now: float, T: list[float], V: list[float], give_share: float = 0.0, give_turn: float = 0.5, give_peak: float = 1.0 / 3.0, arm_share: float = 0.0, left_share: float = 0.0) -> tuple[bool, dict[str, float | None]]:
    """Whether a bet bought at `bought` is sold now by the salvage rule, and the readings. quotes = the market's (time, YES bid, YES ask) rows; T, V = the spot's ticks.

    The GIVE-UP branch (2026-11-23, user: "the other exit needs to be a safety exit. If the scalp isn't working we should exit ... we just need to exit if it dips and doesn't turn"; "if it DOES [turn], we can wait"): with give_share > 0, a bet that is armed (its bid fell one typical minute under the bid at the buy) and whose bid has not TURNED (come give_turn x the typical minute off the lowest bid since the buy) is sold once the time since the later of its arming and the last turn reaches W = give_share x min(e, left x p / (1 - p)), e = seconds since the open, left = seconds to the close, p = give_peak (user, 2026-11-23: "slightly slanted, so peak around 5 mins"): a line rising from 0 at the open to give_share x p x 900 at the share p of the market (minute 5 at 1/3) and falling linearly to 0 at the close (short early so there is time to re-buy, short late). A turn restarts the clock; the rebound sale keeps running unchanged. The info then carries give_up = 1 with the readings. An unreadable typical minute (under a minute of bids before the buy) is no sale: the info says unit_unreadable = 1."""
    a = int(opened)
    i0, i = int(bought) - a, int(now) - a
    if i0 < 1 or i <= i0:
        return False, {}
    sb = side_bids(quotes, side, a, int(now) + 1)
    if sb[i0] is None:
        return False, {}
    ch = [sb[k] - sb[k - 60] for k in range(60, i0, 5)]  # type: ignore[operator]
    if not ch:  # user, 2026-11-23: "a size is never a fixed number": no typical minute, no sale
        return False, {"unit_unreadable": 1.0}
    qu = max(math.sqrt(sum(c * c for c in ch) / len(ch)), 0.01)  # the side bid's own typical minute before the buy
    b0 = sb[i0]
    # ARM SHARE (2026-11-26, user, on PLATINUM 10:15 YES bought at a 0.05 bid and held to zero: "it also got a full 0, which isn't acceptable"; "Why didn't it sell?"): a bid that has just fallen to a few cents has a typical minute as large as itself, so `b0 - qu` is at or under zero and the give-up could never arm on a cheap bet. With arm_share > 0 the size that arms the bet and counts as a turn is the smaller of the typical minute and arm_share x the bid at the buy (a share of what was paid, never a number of cents). 0 = the typical minute alone, as before.
    qa = min(qu, arm_share * b0) if arm_share > 0 and b0 > 0 else qu  # type: ignore[operator]
    armed, lo_b, ilo = False, b0, i0
    iarm, iturn, lo_run = -1, -1, b0
    for j in range(i0 + 1, i + 1):
        if sb[j] < b0 - qa:  # type: ignore[operator]
            if not armed:
                iarm = j
            armed = True
        if armed and sb[j] < lo_b:  # type: ignore[operator]
            lo_b, ilo = sb[j], j
        if sb[j] < lo_run:  # type: ignore[operator]
            lo_run = sb[j]
        elif armed and sb[j] - lo_run >= give_turn * qa:  # type: ignore[operator]
            iturn = j
    info = {"unit": qu, "arm_unit": qa, "entry_bid": b0, "low": lo_b, "armed": float(armed)}
    sold = _rebound_sale(sb, armed, i0, i, ilo, lo_b, qu, now, T, V, side, info)  # type: ignore[arg-type]
    if sold or (give_share <= 0.0 and left_share <= 0.0) or not armed:
        return sold, info
    left = max(opened + 900.0 - now, 0.0)
    elapsed = max(now - opened, 0.0)
    if left_share > 0.0:
        # THE WAIT IS A SHARE OF THE TIME LEFT (2026-11-27, user: "The exit is based on time remaining. Obviously we want fast profit, but if time allows, wait, if there not a lot of time left (under 3 mins) it should be a short wait, but still wait. Since these are low buys anyway a small increase in price is worth a lot"; and on the buy: "blips ... will still happen but we can usually recover from"). Long while the market has long to run, short near its close, never nothing: a line, with no cut at any minute. It replaces the peaked wait below for a lane that sets it. Why: under the peaked wait at 0.1 a blip had about 24 s (PALLADIUM 08:45 on 2026-11-27: a YES bought near the low at 08:34:58 was sold at the second low 0.10 after 26 s unturned with 487 s left, and the price then climbed); on the reader's 239 bets the give-up's 90 sales were all losses (-18.9c a contract, -232.57) while every other sale together made +197.81.
        W = left_share * left
        since = max(iarm, iturn)
        waited = float(i - since)
        if waited >= W:
            info.update({"give_up": 1.0, "armed_at": float(iarm + a), "since": float(since + a), "waited": waited, "allowed": W, "share": left_share, "left": left, "elapsed": elapsed, "turn": give_turn, "peak": give_peak, "by_left": 1.0})
            return True, info
        return False, info
    if not 0.0 < give_peak < 1.0:
        return False, info
    W = give_share * min(elapsed, left * give_peak / (1.0 - give_peak))  # user, 2026-11-23: "the earlier in the market it is, the less we should wait" (time to re-buy) and "we can't wait too long": short early, short late, longest mid-market
    since = max(iarm, iturn)
    waited = float(i - since)
    if waited >= W:
        info.update({"give_up": 1.0, "armed_at": float(iarm + a), "since": float(since + a), "waited": waited, "allowed": W, "share": give_share, "left": left, "elapsed": elapsed, "turn": give_turn, "peak": give_peak})
        return True, info
    return False, info


def _rebound_sale(sb: list[float | None], armed: bool, i0: int, i: int, ilo: int, lo_b: float, qu: float, now: float, T: list[float], V: list[float], side: str, info: dict[str, float]) -> bool:
    """The salvage rule's own sale: the bid has come half a unit off its low, the climb over the last quarter of the time since the low has slowed to a quarter of the run's pace, and the spot has stopped moving the bet's way."""
    if not armed or ilo == i or sb[i] - lo_b < 0.5 * qu or i - ilo < 4:  # type: ignore[operator]
        return False
    wl = max(int(0.25 * (i - ilo)), 1)
    s_run = (max(sb[i0 : i + 1]) - lo_b) / max(i - ilo, 1)  # type: ignore[type-var,operator]
    s_rec = (sb[i] - sb[i - wl]) / wl  # type: ignore[operator]
    sp = grid(T, V, int(now) - wl, int(now) + 1)
    if sp[0] is None or sp[-1] is None:
        return False
    d = 1 if side == "no" else -1  # the wave the bet was against: the bet gains while the spot moves against d
    spot_turn = (sp[-1] - sp[0]) * d >= 0
    info.update({"run": s_run, "recent": s_rec, "window": float(wl), "spot_turn": float(spot_turn)})
    return bool(s_rec <= 0.25 * s_run and spot_turn)


def time_factor(curve: tuple[tuple[float, float], ...], left: float, p: float) -> float:
    """The staircase's time multiplier (2026-11-23). With a lane curve ((seconds left, multiplier) pairs, largest seconds first): straight lines between the points, flat beyond the ends. Without one: (max(left, 60) / 60)^p, the power law as it was."""
    if not curve:
        return (max(left, 60.0) / 60.0) ** p
    if left >= curve[0][0]:
        return curve[0][1]
    for (s0, m0), (s1, m1) in itertools.pairwise(curve):
        if left >= s1:
            return m1 + (m0 - m1) * (left - s1) / (s0 - s1) if s0 > s1 else m1
    return curve[-1][1]
