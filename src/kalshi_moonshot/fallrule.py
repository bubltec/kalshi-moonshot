"""The one exit rule: early profit, salvage and no loss as one function of the same reading, on every tick of the whole market, with no zones (user, 2026-11-26, after BTC 17:00 was held through its profit by two holds of early_take, SOL 20:30 collapsed between early_take's and late_dip's zones and NEAR 21:00 was sold at 0.46 on a spot a hair under the target and settled YES: "We keep adding patches instead of fixing the core issue", "we need to consolidate one rule that handles them all", "Based on things like time, cushion (positive and negative), and I think the early profit concept needs to expire but not with a hard stop it should just approach no action after about 10 mins elapsed", "We need to return to our original concepts like early profit, salvage, and no loss"). It replaces the sell steps early_take and late_dip.

ONE READING: where the bid is against its own swing, in the bid's own wobbles (its typical 10 s move: the typical minute x sqrt(10/60)). A move under a wobble is the bid's ordinary motion and is never acted on.

THREE FACTORS move the bar the reading has to pass:
  time     the bar is 1 + 2 x the share of the market still to run wobbles (3 at the open, 1 at the close), and EARLY PROFIT FADES with the bet's own age (the time since its buy, not the market's clock: see fade): its bar and its stall wait are divided by exp(-(age / (FADE_SHARE x the time the bet had left at its buy))^8): for a bet bought at the open that is 1.0 at minute 5, 0.8 at 7.5, 0.4 at 9, 0.1 at 10 and 0.01 at 11, and a bet bought later runs through the same curve in the time it has, so early profit approaches no action two thirds of the way through the bet's own life with no line anywhere.
  cushion  z = the spot's cushion over the target for the bet / (the spot's typical minute x sqrt(minutes left)), positive on the bet's side and negative on the wrong side. z >= Z_HOLD holds a profit whatever the bid does; under it the bar is multiplied by 2 x Phi(z) (wide with a cushion, tight against one). The spot has CROSSED only when its cushion is under zero by a wobble of its own (its typical 10 s move): a hair under the target is not a cross.
  profit   once the bet has been ahead by a wobble after both fees, the fall allowed is at most the gain at the high less one wobble.

A PROFIT GAINED TOO FAST IS TAKEN: once a profit run has started, a bet whose share of its possible profit in hand is FAST_PACE times the share of its life used is sold, cushion or not (FAST_PACE).

THE THREE CONCEPTS:
  early profit  the bet is ahead after both fees by two wobbles (ARM_WOBBLES): follow the high of this profit run and sell, at a gain, when the bid falls the bar under it or makes no new high for STALL_SHARE of the time left. Fades with the time elapsed (above).
  no loss       nothing here sells under the price paid while the spot has not crossed. FOR A LOSS, CROSSED MEANS AS FAR UNDER THE TARGET AS THE SPOT HAS BEEN OVER IT (2026-11-27, user, on BNB 00:45 YES: bought 0.84 with the spot about $1.00 over the target, the spot went $0.20 to $0.33 under for three minutes, the salvage sold at 0.32 and the market settled YES: "why did this one sell? It was barely under the target for a short period"; "compared to how far the cushion was above earlier in the market it should allow a wait before selling to see if it comes back up"; on measuring it since the buy: "It can't be since the buy. It needs to be the entire market"). State.best_cushion is the best cushion the spot has had on the bet's side since the market opened; a loss is sold only when the spot is a wobble under the target AND at least that far under. ETH 00:45 YES of the same night (best $1.76, $1.77 under 82 s after the buy and falling) is crossed; BNB is not. On the 210 live bets of 11-25/26 the loss side with this: +$116.02 against +$110.14 as it was (13 salvage sales for 37); measured since the buy +$137.72, with the typical cushion over the whole market +$107.90, with a time limit worse (+$80.31): the whole market and the best cushion are the user's rule and the better of the whole-market two. A bet with no cushion in the whole market (best 0) is crossed by a wobble as before. A bet that falls back under cost ends its profit run; it is not sold on the way down.
  salvage       the bet is under cost and the spot has crossed: it is not sold at its low. It is sold when it has bounced a bar off its low and the bounce has failed (the bid is a bar under the bounce's high): a small loss on a lower high in place of the low or a total loss. A bet that does not bounce is sold by the time backstop: NO_RECOVERY_SHARE of the market's length under cost since it was last ahead (or first seen), the spot crossed.
  the top       a bid that leaves a tick or less to win is sold: nothing more to gain, the whole bid to lose. UNLESS THE SETTLEMENT IS LOCKED IN (user, 2026-11-26, on NEAR 21:45 YES sold at 0.991 with 14 s left and settled YES: "still selling when it's like a 99% chance, how can we stop this?"; "Option 2"): Kalshi settles on the spot's average over the market's last 60 s, so inside that window part of the result is already banked. settle_z is how many sigma of the time left the spot would have to move against the bet, from where it is now, for the rest of the window to drag the whole average across the target; the top holds when the chance of that not happening, Phi(settle_z), is worth more than the bid pays after its fee. No number of its own: the bid sets the bar (about 2.3 sigma at 0.99). With nothing banked (60 s or more left, ZEC 15:45) the top sells as before.
LATE PROFIT IS LEFT ALONE WHILE THE SPOT HAS NOT CROSSED (user, 2026-11-26: "make sure late profit doesn't sell if it's not under the target. We keep losing a ton late market from sales for no reason"): the fade applies to every profit whose spot is not a wobble on the wrong side of the target.

Pure and stdlib only: the watcher's step and the replay call the same function. Z_HOLD and STALL_SHARE are the numbers early_take ran with; FADE_SHARE is "about ten minutes" of fifteen read as a curve that is a tenth two thirds of the way through the bet's life; its power was 4 on the first run and was made 8 after seeing that 4 already widened the bar at minute 5.6 enough to miss BTC 17:00 (said so: chosen after the result)."""
from __future__ import annotations

import math
from dataclasses import dataclass

MARKET_S = 900.0
Z_HOLD = 1.0  # sigma of the time left on the bet's side that holds a profit (early_take_cushion_units)
ARM_WOBBLES = 2.0  # a profit run starts when the bet is ahead by two wobbles after both fees, so that a sale after the smallest fall that counts (one wobble) still keeps a wobble of profit. It was one wobble until 2026-11-26 ~23:40 ET: DOGE 23:30 YES was sold twice "for garbage profit, while it was winning" (user): bought 0.66, high 0.76 (+7c, 1.6 wobbles), sold at 0.69 for +2.0c on a fall of one wobble, the bid went on to 0.80; bought back at 0.78, high 0.87, sold at 0.80 for +0.4c. HYPE 21:45 the same (+0.7c). A profit under two wobbles left nothing to keep once a wobble had been given back, and one wobble is the bid's ordinary motion. Structural (it is what makes the profit cap mean something), not a fitted number; chosen after seeing those sales
FAST_PACE = 2.0  # a profit gained too fast to risk (2026-11-27 ~01:00 ET, user, on NEAR 00:00 NO: bought 0.64, +22c within two minutes of a bet with nine left, held by the cushion and gone to zero: "We need a rule where the time to profit increase becomes too valuable to risk, and it needs to sell it"; "Let's add the profit gained too fast rule"). PACE is the share of the bet's possible profit already in hand (the gain after both fees over 1 less the cost) divided by the share of the bet's life used (its age over the time it had left at its buy): 1 is a straight line from the buy to a full win at the close. At FAST_PACE or more, with a profit run started (two wobbles), the bet is sold: it has been paid at twice the pace a full win needs, and what the rest of the market can add is small against what is in hand. A ratio of the bet's own profit and life, no cents and no seconds. On NEAR's tape it sells at 0.83 at 23:53:14, +$4.29 against -$15.23. SAID PLAINLY: on the 210 live bets of 11-25/26 it makes 99 such sales at a median 0.87, 86 of which would have gone on to win, and the replay total is +$110.14 against +$149.66 without it (2 was tried beside 3 and 4, +$124.72 and +$134.76, which do not sell NEAR; 2 chosen for that, after seeing; no buy-back modelled). On the user's word; score the `fast` sales and the buy-backs after them forward
STALL_SHARE = 0.1  # a profit with no new high for this share of the time left has stopped climbing (early_take_stall_share)
TICK = 0.01
SETTLE_S = 60.0  # structural: Kalshi settles these markets on the index average over the last 60 s
GRID = TICK / 2.0  # the bid moves in ticks, so a fall is compared with its bar to the nearest tick: a fall of 0.06 passes a bar of 0.0603 (BTC 17:00 sat on exactly that edge; user, 2026-11-26: "close enough")
NO_RECOVERY_SHARE = 0.33  # the time backstop: this share of the market's length under cost with no profit (the priskp lane's no_recovery_share)
FADE_SHARE = 0.6  # early profit's fade: exp(-(the bet's age / (FADE_SHARE x the time it had left at its buy))^8), a tenth two thirds of the way through the bet's life (ten minutes of fifteen)


@dataclass
class State:
    """What the rule remembers about one bet between ticks."""

    run_high: float = 0.0  # the high of the present profit run; 0 = the bet is not in one
    run_high_ts: float = 0.0
    low: float = 0.0  # the bid's low since it went under cost; 0 = it is not under cost
    bounce_high: float = 0.0  # the high of the bounce off that low; 0 = no bounce of a bar yet
    best_cushion: float = 0.0  # the best cushion the spot has had on the bet's side over the WHOLE MARKET (seeded from the open by the caller, then every tick)
    first_ts: float = 0.0  # the first tick the rule saw this bet (the buy, or the pick-up after a restart)
    last_ahead_ts: float = 0.0  # the last tick the bet was ahead after both fees


@dataclass
class Read:
    """One tick's answer: sell or not, the kind of sale, and every number it was judged on (for the log and the record)."""

    sell: bool
    kind: str  # "top", "fast", "fall", "stall", "salvage", "no recovery", or why it held: "locked", "cushion", "under bar", "no cross", "no bounce", "nothing"
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


def fade(age: float, life: float = MARKET_S) -> float:
    """Early profit's weight by the BET'S OWN AGE, the seconds since its buy: 1 at the buy, approaching 0 after about ten minutes, with no line. It counted the market's clock until 2026-11-26 ~23:10 ET: ZEC 22:30 NO, HYPE 22:45 YES and XRP 22:45 YES were bought at minute 6 to 9 of their markets, were 9c to 19c ahead after both fees at minute 9 to 10.7, where the market's clock had already faded early profit to nothing, and went to zero (user: "And those should have been Profits And rebuys"; "When we lose that bad on so many things at a problem with not waiting"). A bet bought late had no early-profit window at all; "early" is early in the bet. AND IT FADES QUICKER THE LESS TIME THE BET HAD (user, 2026-11-26 ~23:20 ET: "Yes that's how the fade should work but it should fade quicker based o the time left"): `life` is the time the bet had left when it was bought (its age plus the time left now), and the fade's scale is FADE_SHARE of it, so every bet's early profit is a tenth two thirds of the way through its own life: ten minutes in for a bet bought at the open (the user's "about 10 mins"), four minutes in for one bought with six minutes left. A ratio of the bet's life, never a fixed number of seconds."""
    return math.exp(-((max(age, 0.0) / (FADE_SHARE * max(life, 1.0))) ** 8))


def crossed(cushion: float | None, spot_unit: float | None) -> bool:
    """The spot is on the wrong side of the target by at least a wobble of its own (its typical 10 s move). Unread is not a cross."""
    return cushion is not None and bool(spot_unit) and cushion <= -spot_unit * math.sqrt(10.0 / 60.0)


def settle_z(banked: float | None, cushion_now: float | None, spot_unit: float | None, left: float, window: float = 60.0) -> float | None:
    """Inside the settlement window: the move against the bet the spot needs, from its level now, for the seconds left to pull the window's average across the target (the banked average x the seconds banked / the seconds left, plus the cushion now), in sigma of the time left. Taking the whole move at once is the cautious reading: an average over the seconds left moves less than the spot does. None outside the window or unread."""
    if banked is None or cushion_now is None or not spot_unit or spot_unit <= 0 or not 0 < left < window:
        return None
    need = cushion_now + banked * (window - left) / left
    return need / (spot_unit * math.sqrt(left / 60.0))


def decide(s: State, bid: float, entry: float, fee_bid: float, fee_entry: float, fee_of, wobble: float, now: float, left: float, cushion: float | None, spot_unit: float | None, cross_cushion: float | None = None, settle: float | None = None, bought: float | None = None, wave: object = "off") -> Read:
    """Ask the rule on one tick. `fee_bid` and `fee_entry` are the fee per contract at the bid and at the price paid, `fee_of` the fee function (for the gain at the high), `wobble` the bid's typical 10 s move (a tick when it has no reading). `cross_cushion`, when given, is the cushion the cross test reads in place of `cushion` (the watcher gives the better for the bet of the spot's 60 s and 5 s averages: the spot has crossed only when both have)."""
    cost = entry + fee_entry
    net = bid - fee_bid - cost
    z = cushion_z(cushion, spot_unit, left)
    bar_w = bar_wobbles(left, z)
    cc = cushion if cross_cushion is None else cross_cushion
    if cc is not None:
        s.best_cushion = max(s.best_cushion, cc)
    over = crossed(cc, spot_unit)
    if over and s.best_cushion > 0:
        over = -cc >= s.best_cushion - 1e-12  # CROSSED FOR A LOSS SALE ONLY WHEN THE SPOT IS AS FAR UNDER THE TARGET AS IT HAS BEEN OVER IT: see the module doc ("no loss")
    if s.first_ts <= 0:
        s.first_ts = now
    if 1.0 - bid <= TICK + 1e-9:
        if settle is not None and 0.5 * (1.0 + math.erf(settle / math.sqrt(2.0))) > bid - fee_bid:
            return Read(False, "locked", net, 0.0, 0.0, bid, settle)  # the settlement average is locked in: holding is worth more than the bid pays
        return Read(True, "top", net, 0.0, 0.0, bid, z)  # THE TOP: a tick or less left to win and the whole bid to lose
    if net > 0:  # EARLY PROFIT
        s.low = s.bounce_high = 0.0
        s.last_ahead_ts = now
        if wave != "off":  # THE WAVE EXIT, REPLAY ONLY, NOT LIVE AND NOT TO BE MADE LIVE AS IT IS: on 210 live bets (scripts/priskp_wave_exit_replay.py, studies/results/2026-11-26_priskp_wave_exit_replay.txt) it made +$47.83 against +$149.66 for the bid-fall profit side below, +$148.39 held to settlement and +$52.38 actual: a swing that has given back 0.2 typical minutes sells a winner for a cent or two. Kept so the replay runs. The profit is the swing.s. `wave` is (class, at_extreme) of the bet's side in the curve's current leg, the entry's own reading (wavefacts.leg), or None when it cannot be read
            if wave is None:
                return Read(False, "no wave", net, 0.0, 0.0, bid, z)
            cls, extreme = wave  # type: ignore[misc]
            if z is not None and z >= Z_HOLD:
                return Read(False, "cushion", net, 0.0, 0.0, bid, z)
            if cls in ("turned", "against"):
                return Read(True, "turn", net, 0.0, 0.0, bid, z)  # the swing the bet was riding has turned: what it gave is taken
            if cls == "inside" and extreme:
                return Read(True, "leg done", net, 0.0, 0.0, bid, z)  # the leg has run a usual one's length or travel: taken into its top
            return Read(False, "riding", net, 0.0, 0.0, bid, z)
        age = now - (bought if bought is not None and bought > 0 else s.first_ts)
        f = 1.0 if over else fade(age, age + left)  # `bought` is the bet's buy time (the first tick seen when it is not given). the fade is for a profit whose spot has not crossed: late in the market such a bet is left alone (user, 2026-11-26: "make sure late profit doesn't sell if it's not under the target"); with the spot on the wrong side a profit is still taken at any minute
        if s.run_high <= 0:
            if net < ARM_WOBBLES * wobble - 1e-9:
                return Read(False, "nothing", net, 0.0, 0.0, bid, z, f)  # ahead by less than two wobbles is not a profit this rule can take: see ARM_WOBBLES
            s.run_high, s.run_high_ts = bid, now
        elif bid > s.run_high + 1e-9:
            s.run_high, s.run_high_ts = bid, now
        fall = s.run_high - bid
        life = age + left
        if life > 0 and age > 0 and (net / max(1.0 - cost, 1e-9)) / (age / life) >= FAST_PACE:
            return Read(True, "fast", net, fall, 0.0, s.run_high, z, f)  # A PROFIT GAINED TOO FAST TO RISK: see FAST_PACE. Asked before the cushion hold and never faded: it is about what the bet has already been paid, not about the bid falling or stalling
        if z is not None and z >= Z_HOLD:
            return Read(False, "cushion", net, fall, 0.0, s.run_high, z, f)
        high_net = s.run_high - fee_of(s.run_high) - cost
        bar = max(wobble, min(bar_w * wobble, high_net - wobble)) / max(f, 1e-9)
        if fall >= bar - GRID:
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
    if not over:
        return Read(False, "no cross", net, 0.0, bar, s.bounce_high or s.low, z)  # NO LOSS while the spot has not crossed
    if s.bounce_high > 0 and s.bounce_high - bid >= bar - GRID:
        return Read(True, "salvage", net, s.bounce_high - bid, bar, s.bounce_high, z)
    if now - max(s.first_ts, s.last_ahead_ts) >= NO_RECOVERY_SHARE * MARKET_S:
        return Read(True, "no recovery", net, 0.0, bar, s.bounce_high or s.low, z)  # THE TIME BACKSTOP: a third of the market under cost with the spot crossed (user, 2026-11-24: "accept its a loss when it doesn't come back after the wait"; a profit expires)
    return Read(False, "no bounce" if s.bounce_high <= 0 else "under bar", net, max(0.0, s.bounce_high - bid), bar, s.bounce_high or s.low, z)  # SALVAGE: the bounce off the low has failed with the spot on the wrong side
