"""The one fall rule: a bet is sold when its bid has fallen from its high and stayed down, on every tick of the whole market, with no zones (user, 2026-11-26, after BTC 17:00 was held through its profit by two holds of early_take and SOL 20:30 collapsed between early_take's and late_dip's zones: "We keep adding patches instead of fixing the core issue by adding factors to existing logic", "we need to consolidate one rule that handles them all"). It replaces the sell steps early_take and late_dip, which were this rule written twice with different cut-offs.

ONE READING: how far the bid is under its high, in the bid's own wobbles (its typical 10 s move: the typical minute x sqrt(10/60)). The high is the bid's high since the buy and is never forgotten; for a profit it is the high of the present profit run (a fall under cost ends a run, so a recovery is not sold on a high from before the fall).

ONE BAR the fall has to pass, in wobbles, moved by three factors:
  time left     1 + 2 x the share of the market still to run: 3 wobbles at the open, 2 at the half, 1 at the close. It slides; there is no half or quarter line.
  spot cushion  z = the spot's cushion over the target for the bet / (the spot's typical minute x sqrt(minutes left)): how many sigma of the time left the spot is on the bet's side. z >= Z_HOLD holds the bet whatever the bid does. Under it the bar is multiplied by 2 x Phi(z) (0 to 1.7: wide with a cushion, tight with the spot on the wrong side; 1 when the spot is not read). The same cushion is small in sigma early and large late, so "sell a stalled profit at minute 7" and "do not sell the winning side late" are this one number.
  profit        once the bet has been ahead by a wobble after both fees, the fall allowed is at most the gain at the high less one wobble, so a bet that was ahead is sold while it still is.
The bar is never under one wobble: a move that size is the bid's ordinary motion.

A GAIN IS TAKEN AT ONCE; A LOSS MUST HOLD AND NEEDS THE SPOT. A sale that still pays over the price paid after both fees is made on the tick the fall passes the bar, and also when the bid has made no new high for STALL_SHARE of the time left (a profit that stopped climbing). A sale under the price paid is made only when the spot's cushion is negative (the spot has crossed to the wrong side of the target; unread = no loss sale) and the fall has held for LOSS_WAIT_SHARE of the time left (a share, never fixed seconds).

Pure and stdlib only: the watcher's step and the replay call the same function. The numbers Z_HOLD, STALL_SHARE and LOSS_WAIT_SHARE are the ones early_take and late_dip already ran with, not fitted."""
from __future__ import annotations

import math
from dataclasses import dataclass

MARKET_S = 900.0
Z_HOLD = 1.0  # sigma of the time left on the bet's side that holds the bet (early_take_cushion_units)
STALL_SHARE = 0.1  # a profit with no new high for this share of the time left has stopped climbing (early_take_stall_share)
LOSS_WAIT_SHARE = 0.1  # a fall under cost must hold this share of the time left (late_dip_wait_share)


@dataclass
class State:
    """What the rule remembers about one bet between ticks."""

    high: float = 0.0  # the bid's high since the buy
    run_high: float = 0.0  # the high of the present profit run; 0 = the bet is not in one
    run_high_ts: float = 0.0
    fall_since: float = 0.0  # when the present fall under cost first passed the bar; 0 = it has not


@dataclass
class Read:
    """One tick's answer: sell or not, the kind of sale, and every number it was judged on (for the log and the record)."""

    sell: bool
    kind: str  # "fall", "stall", "loss", or why it held: "cushion", "under bar", "no cross", "waiting", "nothing"
    net: float = 0.0
    fall: float = 0.0  # the fall from the high used, in price
    bar: float = 0.0  # the fall needed, in price
    high: float = 0.0
    z: float | None = None
    wait: float = 0.0


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


def decide(s: State, bid: float, entry: float, fee_bid: float, fee_entry: float, fee_of, wobble: float, now: float, left: float, cushion: float | None, spot_unit: float | None) -> Read:
    """Ask the rule on one tick. `fee_bid` and `fee_entry` are the fee per contract at the bid and at the price paid, `fee_of` the fee function (for the gain at the high), `wobble` the bid's typical 10 s move (a tick when it has no reading)."""
    cost = entry + fee_entry
    net = bid - fee_bid - cost
    s.high = max(s.high, bid)
    z = cushion_z(cushion, spot_unit, left)
    bar_w = bar_wobbles(left, z)
    if net > 0:
        s.fall_since = 0.0
        if s.run_high <= 0:
            if net < wobble - 1e-9:
                return Read(False, "nothing", net, 0.0, 0.0, s.high, z)  # ahead by less than a wobble is not a profit yet
            s.run_high, s.run_high_ts = bid, now
        elif bid > s.run_high + 1e-9:
            s.run_high, s.run_high_ts = bid, now
        if z is not None and z >= Z_HOLD:
            return Read(False, "cushion", net, s.run_high - bid, 0.0, s.run_high, z)
        high_net = s.run_high - fee_of(s.run_high) - cost
        bar = max(wobble, min(bar_w * wobble, high_net - wobble))
        fall = s.run_high - bid
        if fall >= bar - 1e-9:
            return Read(True, "fall", net, fall, bar, s.run_high, z)
        if now - s.run_high_ts >= STALL_SHARE * left:
            return Read(True, "stall", net, fall, bar, s.run_high, z, STALL_SHARE * left)
        return Read(False, "under bar", net, fall, bar, s.run_high, z)
    s.run_high = s.run_high_ts = 0.0  # under cost: the profit run is over
    bar, fall = bar_w * wobble, s.high - bid
    if fall < bar - 1e-9:
        s.fall_since = 0.0
        return Read(False, "under bar", net, fall, bar, s.high, z)
    if z is None or z >= 0:
        s.fall_since = 0.0
        return Read(False, "no cross", net, fall, bar, s.high, z)  # a loss is not sold while the spot is on the bet's side of the target, or unread
    if s.fall_since <= 0:
        s.fall_since = now
    wait = LOSS_WAIT_SHARE * left
    if now - s.fall_since < wait:
        return Read(False, "waiting", net, fall, bar, s.high, z, wait)
    return Read(True, "loss", net, fall, bar, s.high, z, wait)
