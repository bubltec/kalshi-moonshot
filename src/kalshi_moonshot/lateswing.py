"""The late swing lane: a small bet on a cheap or dear side of a jumpy 15-minute market in the last two minutes, sold at a target that depends on how much time the bet had.

2026-11-20, user: "a new lane that watches our 15min markets in the last 2 mins (up to 45s remaining) for price scales in the 10-30/70-90 range (low and high not YES or NO -- could be either side). using our lookback capability we want to see what the curve has done in the past 5 mins. if it looks jumpy and has crossed the line we should take a low bet and sell it for a max amount of profit. this will likely tier at 100%, 70%, 50%, 40%, 30%, 20% and we can't bet in retrospect, so ideally we would just lower the tier as time goes on, with 100% being the best bet but we would set a sell order for those tiers based on where the price goes between 2min-45s." Answers (2026-11-20): a resting target per tier; the tier by time left alone; jumpy = crossings plus the last minute's volatility against the five-minute one.

How it is built (buy step `late_swing` in buychain.py, sell step `swing_take` in sellchain.py, lane fields late_swing_*):
  window  the buy is open from late_swing_from seconds left down to late_swing_to (120 and 45). The window is cut into one slice per tier, oldest slice first, so the tier falls as time runs out: 100% of the lane's stake at the start, 20% near 45 s. A bet cannot be placed in retrospect, so the tier is read from the time left at the buy, never later.
  price   the ask is in the lane's price range but not in the middle (late_swing_mid_lo to late_swing_mid_hi, 0.30 to 0.70): a low or a high side, YES or NO.
  jumpy   the settlement index crossed the strike at least late_swing_min_crosses times in the last five minutes (variants.snapshot_now) AND the last minute's move rate is at least the five-minute rate (vol_ratio >= 1): both are the market's own readings, in its own units.
  sale    a resting target per tier: the bet is sold once its bid, net of the fee, gives a gain of the tier's share of the room that was left at the cost (1 less the all-in cost). Tier 100% is a target at the whole room, so that bet rides to settlement; tier 20% takes a fifth of the room. The tier is read back from the time left at the buy (close less the buy time), so no extra state is kept. A bet that never reaches its target settles.

The tier shares and the target shares are the user's list used both ways: a first setting, not fitted. It runs on paper first (CLAUDE.md rule 5); the study that scores the tiers on their own bets comes after a week of them. Memory only: nothing here reads the database.
"""

from __future__ import annotations

from typing import Any

DEFAULT_TIERS = (1.0, 0.7, 0.5, 0.4, 0.3, 0.2)


def tier_index(left: float, hi: float, lo: float, tiers: tuple[float, ...] = DEFAULT_TIERS) -> int | None:
    """Which slice of the window `left` seconds to the close falls in (0 = the first, the best bet), or None outside the window. The window runs from `hi` seconds left down to `lo`, in as many equal slices as there are tiers."""
    if hi <= lo or not tiers or not lo <= left <= hi:
        return None
    return min(len(tiers) - 1, int((hi - left) / (hi - lo) * len(tiers)))


def tier_share(left: float, hi: float, lo: float, tiers: tuple[float, ...] = DEFAULT_TIERS) -> float | None:
    """The share of the lane's stake (and of the room, for the sale's target) at this time left, or None outside the window."""
    i = tier_index(left, hi, lo, tiers)
    return None if i is None else float(tiers[i])


def stake_factor(spec: Any, left: float) -> float:
    """The multiplier on the lane's stake for a buy `left` seconds before the close (1.0 for a lane that is not a late swing lane or a buy outside its window: those never reach a buy through the step)."""
    if spec.late_swing_from <= 0:
        return 1.0
    return tier_share(left, spec.late_swing_from, spec.late_swing_to, tuple(spec.late_swing_tiers)) or 1.0


def band_ok(spec: Any, ask: float) -> bool:
    """The ask is in the lane's price range and outside its middle."""
    lo, hi = spec.price
    return lo <= ask <= hi and not spec.late_swing_mid_lo < ask < spec.late_swing_mid_hi


def target_gain(share: float, basis: float) -> float:
    """The gain a contract must show, net of the fee, for the tier's sale: the share of the room (1 less the all-in cost) the buy left."""
    return share * (1.0 - basis)
