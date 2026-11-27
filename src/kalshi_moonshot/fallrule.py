"""The one exit rule: early profit, salvage and no loss as one function of the same reading, on every tick of the whole market, with no zones (user, 2026-11-26, after BTC 17:00 was held through its profit by two holds of early_take, SOL 20:30 collapsed between early_take's and late_dip's zones and NEAR 21:00 was sold at 0.46 on a spot a hair under the target and settled YES: "We keep adding patches instead of fixing the core issue", "we need to consolidate one rule that handles them all", "Based on things like time, cushion (positive and negative), and I think the early profit concept needs to expire but not with a hard stop it should just approach no action after about 10 mins elapsed", "We need to return to our original concepts like early profit, salvage, and no loss"). It replaces the sell steps early_take and late_dip.

ONE READING: where the bid is against its own swing, in the bid's own wobbles (its typical 10 s move: the typical minute x sqrt(10/60)). A move under a wobble is the bid's ordinary motion and is never acted on.

THREE FACTORS move the bar the reading has to pass:
  time     the bar is 1 + 2 x the share of the market still to run wobbles (3 at the open, 1 at the close), and EARLY PROFIT FADES with the time elapsed: its bar and its stall wait are divided by exp(-(elapsed / FADE_S)^8), which is 1.0 at minute 5, 0.8 at 7.5, 0.4 at 9, 0.1 at 10 and 0.01 at 11, so it approaches no action after about ten minutes with no line anywhere.
  cushion  z = the spot's cushion over the target for the bet / (the spot's typical minute x sqrt(minutes left)), positive on the bet's side and negative on the wrong side. z >= Z_HOLD holds a profit whatever the bid does; under it the bar is multiplied by 2 x Phi(z) (wide with a cushion, tight against one). The spot has CROSSED only when its cushion is under zero by a wobble of its own (its typical 10 s move): a hair under the target is not a cross.
  profit   once the bet has been ahead by a wobble after both fees, the fall allowed is at most the gain at the high less one wobble.

THE THREE CONCEPTS:
  early profit  the bet is ahead after both fees by a wobble: follow the high of this profit run and sell, at a gain, when the bid falls the bar under it or makes no new high for STALL_SHARE of the time left. Fades with the time elapsed (above).
  no loss       nothing here sells under the price paid while the spot has not crossed. A bet that falls back under cost ends its profit run; it is not sold on the way down.
  salvage       the bet is under cost and the spot has crossed: it is not sold at its low. It is sold when it has bounced a bar off its low and the bounce has failed (the bid is a bar under the bounce's high): a small loss on a lower high in place of the low or a total loss. A bet that never bounces is the time backstop's (the no_recovery step).

Pure and stdlib only: the watcher's step and the replay call the same function. Z_HOLD and STALL_SHARE are the numbers early_take ran with; FADE_S is "about ten minutes" read as a curve that is a tenth at minute 10; its power was 4 on the first run and was made 8 after seeing that 4 already widened the bar at minute 5.6 enough to miss BTC 17:00 (said so: chosen after the result)."""
from __future__ import annotations

import math
from dataclasses import dataclass

MARKET_S = 900.0
Z_HOLD = 1.0  # sigma of the time left on the bet's side that holds a profit (early_take_cushion_units)
STALL_SHARE = 0.1  # a profit with no new high for this share of the time left has stopped climbing (early_take_stall_share)
FADE_S = 0.6 * MARKET_S  # early profit's fade: exp(-(elapsed / FADE_S)^8), a tenth at minute 10


@dataclass
class State:
    """What the rule remembers about one bet between ticks."""

    run_high: float = 0.0  # the high of the present profit run; 0 = the bet is not in one
    run_high_ts: float = 0.0
    low: float = 0.0  # the bid's low since it went under cost; 0 = it is not under cost
    bounce_high: float = 0.0  # the high of the bounce off that low; 0 = no bounce of a bar yet


@dataclass
class Read:
    """One tick's answer: sell or not, the kind of sale, and every number it was judged on (for the log and the record)."""

    sell: bool
    kind: str  # "fall", "stall", "salvage", or why it held: "cushion", "under bar", "no cross", "no bounce", "nothing"
    net: float = 0.0
    fall: float = 0.0  # the fall from the high used, in price
    bar: float = 0.0  # the fall needed, in price
    high: float = 0.0
    z: float | None = None
    fade: float = 1.0


def cushion_z(cushion: float | None, spot_unit: float | None, left: float) -> float | None:
    """The spot's cushion in sigma of the time left; None when the spot is not read."""
    if cushion is None or not spot_unit or spot_unit <= 0:
        return None
    return cushion / (spot_unit * math.sqrt(max(left, 1.0) / 60.0))


def bar_wobbles(left: float, z: float | None) -> float:
    """The fall needed, in wobbles, before the profit cap: the time factor times the cushion factor, never under 1."""
    time_f = 1.0 + 2.0 * min(max(left, 0.0), MARKET_S) / MARKET_S
    cushion_f = 1.0 if z is None else 1.0 + math.erf(z / math.sqrt(2.0))  # 2 x Phi(z)
    return max(1.0, time_f * cushion_f)


def fade(left: float) -> float:
    """Early profit's weight by the time elapsed in the market: 1 at the open, approaching 0 after about ten minutes, with no line."""
    elapsed = MARKET_S - min(max(left, 0.0), MARKET_S)
    return math.exp(-((elapsed / FADE_S) ** 8))


def crossed(cushion: float | None, spot_unit: float | None) -> bool:
    """The spot is on the wrong side of the target by at least a wobble of its own (its typical 10 s move). Unread is not a cross."""
    return cushion is not None and bool(spot_unit) and cushion <= -spot_unit * math.sqrt(10.0 / 60.0)


def decide(s: State, bid: float, entry: float, fee_bid: float, fee_entry: float, fee_of, wobble: float, now: float, left: float, cushion: float | None, spot_unit: float | None) -> Read:
    """Ask the rule on one tick. `fee_bid` and `fee_entry` are the fee per contract at the bid and at the price paid, `fee_of` the fee function (for the gain at the high), `wobble` the bid's typical 10 s move (a tick when it has no reading)."""
    cost = entry + fee_entry
    net = bid - fee_bid - cost
    z = cushion_z(cushion, spot_unit, left)
    bar_w = bar_wobbles(left, z)
    if net > 0:  # EARLY PROFIT
        s.low = s.bounce_high = 0.0
        f = fade(left)
        if s.run_high <= 0:
            if net < wobble - 1e-9:
                return Read(False, "nothing", net, 0.0, 0.0, bid, z, f)  # ahead by less than a wobble is not a profit yet
            s.run_high, s.run_high_ts = bid, now
        elif bid > s.run_high + 1e-9:
            s.run_high, s.run_high_ts = bid, now
        fall = s.run_high - bid
        if z is not None and z >= Z_HOLD:
            return Read(False, "cushion", net, fall, 0.0, s.run_high, z, f)
        high_net = s.run_high - fee_of(s.run_high) - cost
        bar = max(wobble, min(bar_w * wobble, high_net - wobble)) / max(f, 1e-9)
        if fall >= bar - 1e-9:
            return Read(True, "fall", net, fall, bar, s.run_high, z, f)
        if now - s.run_high_ts >= STALL_SHARE * left / max(f, 1e-9):
            return Read(True, "stall", net, fall, bar, s.run_high, z, f)
        return Read(False, "under bar", net, fall, bar, s.run_high, z, f)
    s.run_high = s.run_high_ts = 0.0  # under cost: the profit run is over (NO LOSS: it is not sold on the way down)
    bar = bar_w * wobble
    if s.low <= 0 or bid < s.low - 1e-9:
        s.low, s.bounce_high = bid, 0.0  # a new low: any bounce before it is over
    if bid - s.low >= bar - 1e-9 or s.bounce_high > 0:
        s.bounce_high = max(s.bounce_high, bid)
    if s.bounce_high <= 0:
        return Read(False, "no bounce", net, 0.0, bar, s.low, z)
    fall = s.bounce_high - bid
    if fall < bar - 1e-9:
        return Read(False, "under bar", net, fall, bar, s.bounce_high, z)
    if not crossed(cushion, spot_unit):
        return Read(False, "no cross", net, fall, bar, s.bounce_high, z)  # NO LOSS while the spot has not crossed
    return Read(True, "salvage", net, fall, bar, s.bounce_high, z)  # SALVAGE: the bounce off the low has failed with the spot on the wrong side
