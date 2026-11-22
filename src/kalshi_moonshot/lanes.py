"""Lanes as data. A lane is a set of parameters plus the name of the estimator that rates it.

Nothing about a lane is written into the code paths: the filter, the trend rule, the bet rule and
the order size all read a LaneSpec. The defaults come from the same environment settings as
before. A `strategies.toml` in DATA_DIR can change a lane's fields or add a whole new lane:

    [lane.metals_long]
    estimator = "ladder"
    price = [0.02, 0.30]
    hours = [1, 48]
    min_edge = 0.10
    series = ["KXGOLDD", "KXSILVERD"]

The file is read by lane_specs(), so editing it applies on the next scan.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import tomllib
from dataclasses import dataclass, fields, replace
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import overrides, sellchain

log = logging.getLogger("moonshot")
from .config import Settings

# Paper twin of the commodity lane. It reads Polymarket and never places an order, even if named in LIVE_LANES.
POLY_LANE = "commodity_poly"
# Every Polymarket lane. Paper unless that lane is named in LIVE_LANES. An empty list does not turn them on.
POLY_LANES = frozenset({POLY_LANE, "crypto_poly", "crypto_poly_5m"})

LLM = "llm"
LADDER = "ladder"
MARKET = "market"
CURVE = "curve"  # the class of the move so far in the window (curve.py) gives the chance
FORECAST = "forecast"  # the index's recent moves forecast the side's price (forecast.py)


@dataclass(frozen=True)
class LaneSpec:
    name: str
    estimator: str  # key in the estimator registry (estimators.py)
    label: str  # used in reject messages, e.g. "ask outside {label} range"
    price: tuple[float, float]  # ask range, dollars
    hours: tuple[float, float]  # hours to close range
    max_spread: float
    min_volume: float
    min_edge: float | None = None  # bet when the calibrated edge reaches this ...
    min_edge_room: float | None = None  # 2026-11-13 (user: a flat edge bar "makes it less likely to buy a 'sure' bet"): the edge bar scaled to the room left. Bet when the edge is at least this share of what lies between the all-in cost (ask + fee) and certainty: edge / (1 - ask - fee) >= this. 0.3 = our chance of losing is at least 30% smaller than the price implies: a chance of about 0.935 at an ask of 0.90, just over 0.80 at an ask of 0.70. A flat min_edge asks for the same cents at every price and so can never be met near the top of the band. Both may be set: then both must pass. None = off
    max_lag: float | None = None  # ... or, when set, when the estimate is no more than this under the ask
    usd_per_trade: float | None = None  # optional dollar ceiling per bet; None/0 = sized by balance
    fixed_stake: float = 0.0  # dollars per bet, ignoring Kelly (0 = size by the odds and the balance)
    needs_model_edge: bool = True  # the model must see an edge over the price before the lane buys (min_edge, min_edge_room, max_lag). False = the lane buys whatever passes its other gates, with no minimum edge from a model. Until 2026-11-16 this was `always` with the opposite sense (user: "lets re-work that concept. because 'always' means nothing"); a lane file may still say `always`. On the live crypto lane's 157 bets with the screen off (11-15 16:31 on), the 19 it would have passed made +1.10 and the 138 it would have dropped -175.35, though the model's edge does not sort the bets: the screen mostly buys an eighth as often (studies/results/2026-11-16-edge-screen-record.txt)
    min_age_minutes: float = 0.0  # skip a market until it has been open this long (books settle down)
    shares_markets: bool = False  # main-scan lane that also takes markets another lane took (for A/B controls); never consumes them
    reentry_cooldown_seconds: float = 0.0  # after this lane exits a market, wait this long before it may buy that market again (0 = no wait)
    rebuy_opposite_after_gain_seconds: float = 0.0  # 2026-11-08 (user, HYPE 09:15: the value exit sold YES at 0.67 for a gain and the lane bought YES again 94 s later at 0.67 and lost $2.22; "a 60-ish sell is great but we might want to rebuy if confidence continues to increase"); put back 2026-11-19 (user: "Put it back", after the cleanup took it out): after this lane exits one side of a market for a gain (the sale after its fee over the price paid after its fee), it may not buy that SAME side again for this many seconds; the opposite side goes through the lane's usual buy chain at once, and its own gain sale starts the same wait on its side. After the wait the side is open again. A sale at a loss sets no limit (0 = off). Read by the buy step `cooldown` (buychain.py; name it in a buy module's steps and set this in the module, with reentry_cooldown_seconds), from the bet book in memory (Store.last_gain_sale), not the database. A fixed number, set by the user, not read from the curve
    buy_rest_seconds: float = 0.0  # buy with a post-only RESTING order instead of an immediate one: it waits this many seconds at buy_rest_below under the ask and is dropped if unfilled (no fallback). A resting order that fills pays no taker fee; replay says it mostly fills on dips (0 = buy at the ask)
    stake_vol_shadow: bool = False  # 2026-11-18 SHADOW (user: "and maybe we could scale the bet by lane volatility too"; "Yes, shadow it"): every live bet writes one row (kind "stake_vol" in data/risk_shadow.jsonl) with the market's own typical minute at the buy (quoteshock.typical_minute: sqrt(60) times the rms one-second move of its mid over the last 15 minutes), the stake and the price. Changes no stake. scripts/stake_vol_shadow_score.py scores a stake of the lane's stake times (median typical minute of the earlier rows over this one, limited to 0.5 to 2) against the flat stake. Evidence: studies/results/2026-11-18-commodity-stake-by-volatility.txt (4,584 commodity favourites, held: flat -2.74 per $100, scaled -2.19; the wildest third of markets lost 4.65, the quietest 0.54). Retire under rule 6 if it scores negative
    buy_redecide_shadow: bool = False  # 2026-11-18 SHADOW (user, on the commodity buys: "if it doesn't fill 1-2c under in a few seconds, shouldn't another tick have gone by with another choice?"; "Yes, shadow option"): when a LIVE resting buy rests its time and fills nothing, run the lane's whole buy decision once more on the market as it is now, without ordering and without logging a decision, and record what it would have done (kind "redecide" in data/risk_shadow.jsonl: the price it would have rested at, or the gate that said no) beside what the live lane did (took the ask, or bought nothing). Changes nothing the lane does. Scored forward by scripts/redecide_shadow_score.py against the live lane's own result; retire under rule 6 if it scores negative
    buy_rest_below: float = 0.01  # how far under the ask the resting buy sits, in dollars
    paper_fill_model: bool = False  # a PAPER bet of this taker lane is judged on the tape after the fact (paperfill.settle_taker, fillmodel.py): refilled at the ask the tape shows within the lane's limit, or turned into a pass when the ask was over it. Calibrated on the ordinary lanes (0.4-0.9c price error); not for burst lanes
    buy_rest_then_take: bool = False  # when the resting buy fills nothing, buy at the fresh ask instead of dropping the bet (the way buy_retries re-prices: the lane's price band, buy_retry_chase and the risk gate still apply). Fills pay no fee; the runners a resting bid misses are still bought
    buy_rest_follow: bool = False  # 2026-11-19 (user: "a change in price re-evaluates a resting buy order and may update it"): while a resting buy waits, each streamed quote on its market asks again where the order should sit. When the ask has fallen so that resting `buy_rest_below` under it is lower than the order's price, the order is cancelled and placed again at that lower price for what is left unfilled (at most 3 times); when the ask has fallen under the lane's price floor, the order is withdrawn. A risen ask never raises the price. Off = the order stays where it was placed (the way it worked); the live switch is the user's
    buy_rest_top_up: bool = False  # 2026-11-06: when the resting buy fills only part of the order (0.42 of 11.36 palladium, 4.73 of 11.76 Brent), buy the rest at the fresh ask under the same guards as buy_rest_then_take (price band, buy_retry_chase, risk gate). Needs buy_rest_then_take. A partial fill pays no fee on its part; the rest pays the taker fee
    max_open: int = 0  # at most this many bets of this lane open at once, across all its markets (0 = no limit); the next candidate waits until one closes
    max_same_side: int = 0  # at most this many of this lane's open bets on the same side of the same 15-minute window (BTC, ETH and DOGE YES together is one bet on one move); 0 = no limit. 2026-11-02 replay, 30 days of crypto_favorite: the 3rd same-side bet in a window won 13 of 22 for -$46.6, the 1st and 2nd won 82-86%
    buy_curve_edge_min: float = -9.0  # 2026-11-11 (user: "within 0.05 and not flat looks good lets implement that now"): buy only when the curve's chance that the side pays (curvevalue.read, 900 s rate) minus the ask is at least this (-0.05 = the chance may sit up to 5 cents under the price); -9 = off. No reading = no buy. studies/results/2026-11-11-multiscale-buy-filters.txt: on 308 crypto_multiscale bets, with the flat-trend skip, 212 kept made +$56.43 (+10.8% per $) against +$29.79 buying all, better per dollar on each of the 5 days it changed; six days of bets and one bad day (11-08) carries most of it; a cutoff picked day by day lost, so this one is fixed, not tuned. The study measured the price paid; the gate reads the ask, so it is slightly stricter.
    perp_chance_quantile: float = 0.0  # 2026-11-21: the perp_worth step gates on the perp's chance as a LEVEL: at or over this quantile (0.5 = the median) of the series' own readings of the last day (perpchance.py), in place of its gap to the ask (buy_curve_edge_min, which then only answers while the history is short and stays in the note). 0 = the gap rule. A lead from 132 live commodity bets with 28 behind, to be scored on the bets that follow
    curve_exit_scales: str = "900+180"  # which horizon-scale rates the curve exit reads: "900+180" the lower chance of the 900 s and 180 s rates, "900" the 900 s rate alone
    curve_risk_live: bool = False  # false: log "curve risk shadow: would sell" and sell nothing; true: the sale is made
    curve_risk_spot_weight: float = 0.5  # how far a spot alarm counts when it is the worse cushion (1 = as much as the price, 0 = not at all); the price is trusted more
    curve_risk_history: bool = True  # widen the spot cushion by the hour's jumpiness (curverisk.history_factor), read from the market's Kalshi perp reference (the long-horizon layer)
    curve_take: bool = False  # 2026-11-14 (user: "why not just use the bet?"; "earlier in the market is a better favor to sell for profit, since we can always buy back"): the profit take. A bet exits for a gain once the gain is at least the share of its own room (1 less its all-in cost) that the market's time has used up, and more than the fee to buy it back (curverisk.take_profit); and not while the bid is still sloping up (curve_take_slope_share). Off by default. Shadow first: set on a paper lane, scored on its round trips against holding. Not fitted
    curve_take_slope_share: float = 0.08  # the look back for the profit take's slope, as a share of the TIME LEFT (2026-11-14, user, after two live sales fired on a pause of a few seconds in a climb: "I think the 10s look back depends on how much time is left. It needs to be based on the time left"): 58 s with 12 minutes left, 24 s with five, 5 s with one. The bet is not sold while the bid has risen over that look back by more than the market's own noise for it (typical minute x sqrt(look back / 60)) and sits within that noise of its high (curverisk.still_rising). Replaces the fixed 10 s (curve_take_slope_seconds). 0.08 is the user's number ("Let's bump the look back to 8%"); it was 0.05 for an hour, unpushed
    curve_take_cushion: bool = False  # 2026-11-14 (user: "we just need a cushion check before we decide to sell (in the profit flow)"): the profit take also asks the spot's cushion. The curve's chance that the side pays, from the spot's distance to the strike over the time left in the market's own variance (the hour's jumps widen it, as in the risk reading), is held against what the sale pays net of its fee; when the cushion's chance is the larger the take does not sell (curverisk.cushion_holds). With no spot reading the take goes as before. Off by default: scored on the bets that follow, studies/results/2026-11-14-profit-take-score.txt
    curve_take_sure: float = -1.0  # 2026-11-15 night (user: "if it stops these sure wins from selling, sure. I thought we had this fixed"): the profit take never exits at a bid at or over this, at any time left (0.95); -1 = off. The cushion check only held a take when the spot's chance backed the price; a thin cushion at 0.99 still sold, and at a bid of 0.95 and over 0 of 12 takes saved a bet (studies/results/2026-11-15-late-take-check.txt). Chosen after seeing that table
    curve_take_sure_late: float = -1.0  # the same hold, but only in the last fifth of the market (curverisk.LATE_SHARE), from this bid up (0.90); -1 = off. At 0.90 to 0.95 in the last 180 s 0 of 8 takes saved a bet (sale less hold -18.45)
    curve_take_sure_slide: float = -1.0  # the late sure hold with no cut-off: the bid that counts as a sure win slides with the time left (curverisk.sure_bar), from curve_take_sure_late at the close up to 1.00 when this share of the market is still to run. -1 = off (curve_take_sure_late holds from its bid inside the last fifth, as it always did). 0.333 = nothing is sure with five minutes or more left of fifteen; 0.93 with 90 s left; 0.90 at the close. 2026-11-17, user: "we want the sure win, but it should be time based"; "5 mins left? that is too much time"; "do not use hard limits"
    curve_take_early_bid: float = -1.0  # 2026-11-15 night (user: "if it ever hits 95 and we haven't passed 6 mins we should take and let the rebuy kick in"): in the first curve_take_early_seconds of the market the profit take exits at once at a bid at or over this (0.95), whatever the slope or the cushion check say: an early 0.95 is not sure, there is time to turn, so cash it and let the re-entry buy back. -1 = off. The sure hold (curve_take_sure) applies only after this window
    curve_take_trail: float = 0.0  # 2026-11-16 (user, on KXXRP15M-26NOV161745 NO bought at 0.60, up to 0.80 with five minutes left and sold at 0.25: "we should be tracking every market that we buy with a trail"; "Yes, build the trail"): the profit take's trail. A bet that is ahead by more than the fee to buy back is sold once its bid is this many typical minutes of its own market (quoteshock.typical_minute) or more under the highest bid since the buy, whatever the take's bar says (curverisk.trail_hit). 0 = off. The sure hold applies; the cushion check does not (the replay had none). With no typical minute it does not fire. studies/results/2026-11-16-take-hold-trail-replay.txt: at 1.0 on 200 live crypto_multiscale bets since 11-14, +$68.34 against what happened, sold first on 32 (beat 13, lost to 19), positive where the side was in the top fifth of its range at the buy (+$45.08) and lower (+$23.26); the buy-back after a sale was not replayed
    curve_take_trail_live: bool = False  # the trail exits; off = it logs "would sell" once a bet and records it in data/risk_shadow.jsonl (kind "trail"), and exits nothing
    curve_take_trail_timed: bool = False  # 2026-11-18 (user: "as time expires the volatility rate increases"; "so having a static volatility number is pointless"; "put it on commodity also, replace the trail there", then "Yes, live"): the trail reads the TIME-AWARE typical minute (pricevol.implied_rate: the price's one-second move implied by its own cushion and the time left, which rises toward the close) in place of the exit's static 15-minute one. With too little history for it the trail falls back to the static minute. Evidence: studies/results/2026-11-18-price-vol-estimators.txt (held out, 151,485 marks: the time-aware estimate is closer than the static one in every split but 1.5 to 3 min left). Retire it under rule 6 if its forward score (scripts/trail_timed_score.py) turns negative
    curve_risk_late_tight: float = 0.0  # 2026-11-19 (user, on KXPLATINUM15M-26NOV190200 NO bought 0.90 and sold 0.25 and KXCOPPER15M-26NOV190230 YES sold 0.29, both lost in the last minute or two: "i dont think high volatility means we don't buy, but i think it means we tighten our selling"; "this is a lead and we should note it"; "build the new step as a module"): the sell step late_tight. In the last curve_risk_late_tight_share of the market a bet is sold at the bid once its bid is this many typical minutes of its own market (the exit's static one) under its highest bid since the buy, AHEAD OR NOT (the trail needs the bet ahead). 0 = off. It sells only where curve_risk_late_tight_live and the lane's risk exit are live, else it logs one would-sell a bet and records it (data/risk_shadow.jsonl, kind "late tight"). Evidence (a lead, settings chosen after seeing it): studies/results/2026-11-19-commodity-tight-sell-last150.txt, 715 live commodity bets, K 3 in the last 150 s: +$6.60 against what happened (beat 92, lost 50), total-loss bets (ending under 0.10) 65 to 15, all losses -$92.58 to -$78.72, gains +$78.60 to +$71.34
    curve_risk_late_tight_share: float = 1.0 / 6.0  # the share of the market's 900 s at the end of which late_tight applies (a sixth is 150 s, the study's window; a share, so it is not fixed seconds)
    curve_risk_late_tight_live: bool = False  # late_tight sells; off = it logs and records a would-sell and sells nothing
    curve_risk_never_ahead: float = 0.0  # 2026-11-17 LIVE where set (user: "Ok so 'never profitable and 90s left.' Let's add that to the live lanes"): seconds left at which the risk exit exits a bet that was never once in profit (its bid less the sell fee over its cost with the buy fee) since the buy; 0 = off. One look a bet, at the mark; only bets bought 30 s or more before it and watched the whole time. It exits when the lane's risk exit is live (curve_risk_live), else it logs a would-sell and records it (data/risk_shadow.jsonl, kind "never ahead"). The long layer does not hold it. Evidence: studies/results/2026-11-17-time-in-profit.txt (6 bets at 90 s, 2 won if held, selling +$12.84 against what happened; small count, scored forward and retired under rule 6 if it turns negative)
    curve_risk_time_to_go: float = 0.0  # the time_to_go sell step (2026-11-17 sale, put back as a step 2026-11-19, user: "Yes, build it"): in the last this many seconds a bet whose curve chance (the spot against its target, curvevalue.py) is under curve_risk_time_to_go_chance is sold at the bid once that has held, without a break, for a wait set by the time left (time based: a dip never shortens it). 0 = off
    curve_risk_time_to_go_chance: float = 0.5  # the curve's chance that the side pays under which the step sells: the spot is on the wrong side of its target
    curve_risk_time_to_go_wait_share: float = 0.25  # the wait is at least this share of the time left, and at least the market's own median recovery time of a fast fall
    curve_risk_under_even: float = 0.0  # 2026-11-17 (user, on platinum 09:30, Brent 10:00 and copper 10:15, each bought near 0.90 and sold at 0.06 or settled at zero: "just had 3 zeros in the last hour. we need to fix this"): the risk exit exits a bet bought as the favorite once its bid has been under even (0.50) without a break for this share of the time left; 0 = off. It exits where curve_risk_under_even_live and the lane's risk exit are both live, else it logs one would-sell a bet and records it (data/risk_shadow.jsonl, kind "under even"). The long layer does not hold it. Evidence in curverisk.under_even
    curve_risk_under_even_live: bool = False
    curve_take_early_seconds: float = 360.0  # the first six minutes of the market
    curve_risk_long_scales: tuple[int, ...] = ()  # 2026-11-15 (user: the long layer should look further, but keep the current hour's context: 15, 30, 60, 120 and 180 minutes): the minutes the long layer (curve_risk_confirm) reads its movement over, together as a geometric mean of each scale's rate (curverisk.multiscale_rate); 0 = the market so far (its open to now) and a negative number reaches back that many minutes past the open, every window ending now: [0, -15, -75, -255] is the user's design of 2026-11-15 night (the market so far, then each next segment 15, 60 and 180 minutes longer). Empty = the last hour alone, as it was. studies/results/2026-11-15-multiscale-hour-check.txt
    curve_risk_min_profit: float = 0.01  # the profit asked for before a sale at high risk, as a share of the cost, scaled by the risk (this * q): shrinks toward nothing as q falls. A loss is cut at any depth once the risk is under the line
    curve_risk_confirm: float = -1.0  # 2026-11-13 (user, after a live sale of a bet that dipped briefly, never crossed the strike and won: "the long layer showed it likely staying positive"): a risk sale (the max loss and the line) also needs the long layer's chance (the Kalshi perp's cushion over its open, against its own last hour of movement: curverisk.long_chance) to be under this. -1 = price alone. studies/results/2026-11-13-risk-hour-replay.txt: 0.4 cut the cost against holding from -$31 to -$13 on crypto and from -$83 to -$58 on the commodity favorites (first look; still negative against holding)
    late_swing_from: float = 0.0  # 2026-11-20 (user: the last two minutes of the 15-minute markets, a low bet on a jumpy curve, tiered by the time left): the buy step late_swing opens this many seconds before the close (120) and the sell step swing_take uses the same window; 0 = off. See lateswing.py
    late_swing_to: float = 45.0  # ... and shuts this many seconds before the close
    late_swing_tiers: tuple[float, ...] = (1.0, 0.7, 0.5, 0.4, 0.3, 0.2)  # the window cut into one slice per tier, the first slice first: the share of the lane's stake bet, and the share of the room the sale's target asks for. The user's list; a first setting, not fitted
    late_swing_mid_lo: float = 0.30  # the ask must not sit between these two: a low side (the lane's price floor to this) or a high one (the other number to the price ceiling), YES or NO
    late_swing_mid_hi: float = 0.70
    late_swing_min_crosses: int = 2  # the settlement index crossed the strike at least this many times in the last five minutes, and the last minute moved at least as fast as the five minutes (vol_ratio >= 1)
    skip_varieties: tuple[str, ...] = ()  # varieties (variants.py letters) this lane does not buy: "G" on either side, "A-yes" on one side. Read at the decision from BiQuote against its window-open price, so commodities only; an unreadable one is not skipped. 2026-11-03 walk-forward on the lanes' live bets: the cells that paid in training were the same in three folds and left out G and A-yes, which then lost $49, $19 and $9 in the test period
    require_spot_center_agree: bool = False  # buy only when the raw spot AND the offset-corrected center are both on the bet's side of the strike. 2026-11-04, 672 live commodity bets: both on our side 88% won, +$0.24 a bet; where they disagreed 94 bets netted -$54 (industrial -$52 on 59, precious -$11 on 25; energy +$8 on 10, so energy leaves it off). Unreadable = no change
    max_vol_rel: float = 0.0  # buy only when it is at most this (a calm regime: below 0.7 favorites won 87%). 0 = off. No reading = no buy
    min_cushion_z: float = -99.0  # 2026-11-05 (user): buy only when the bet's side has a cushion of at least this many typical moves left (variants.snapshot z). -99 = off. Commodity states since 11-02 (17,900 moments): cushion under 0.5 lost -$0.4 to -$1.45 a $10 bet in both halves, 1.0-1.5 made +$0.24, 1.5+ made +$0.26 to +$0.53 (studies/results/2026-11-05-entry-state-map.txt)
    buy_module: str = ""  # 2026-11-17 (user: "we should be able to have multiple modules. And assign a lane to a module"): the name of a module in strategies.toml ([module.<name>]: `steps`, a `label`, and any lane settings as its defaults). The lane's buy_chain is the module's steps. Empty = no module
    exit_module: str = ""  # 2026-11-18 (user: "we need a sell module which covers profit and loss"; "you mentioned it above, exit module ... do that"): the name of an exit module in strategies.toml ([exit.<name>], with a `profit` and a `loss` part); its exit settings are this lane's unless the lane writes its own (_with_exit). The name itself decides nothing
    sell_chain: tuple[str, ...] = ()  # 2026-11-18 (user: "we also need to build a sell module, since we're refactoring this flow"): the risk exit's sale as named steps in the lane's own order (sellchain.py), e.g. ["profit_take", "trail", "long_hold", "loss"]. On every risk check the steps are asked in order: a step sells, holds (nothing is sold on that check) or passes to the next. A step left out is not asked; each step still reads its own settings (curve_take, curve_risk_sell, ...). A lane on an exit module that holds `steps` takes them as its chain. Empty = the default order (never_ahead, under_even, profit_take, trail, long_hold, loss), which is what every lane did before the chain, with its sale reasons unchanged; a lane with a chain gets the chain and the step that sold at the end of its sale reason
    buy_chain: tuple[str, ...] = ()  # 2026-11-17 (user: "ladder should not be the entry point"; "the risk score makes way more sense to go FIRST"; "you need to write new code that is modular and can fit in logic in any order"): the buy as named steps in the lane's own order (buychain.py), e.g. ["risk_score", "price", "ladder"]. Both sides start alive; each step is asked of the sides still alive, a no puts a side out, and a side alive after the last step is bought. With a chain set the estimator's own screen is not run in the review (the first step puts the sides forward) and the steps replace this lane's edge rule, no-buy check, risk score rule and trend checks in the decision; the steps read those same settings. Empty = the lane decides as it always has
    entry_risk_trend: float = 0.0  # 2026-11-17 SHADOW until entry_risk_trend_live (user, on the entry trend check and its fixed 2-minute noise window: "it shouldn't even care about the entry trend by itself. its the risk factor"; "i want to build something that replaces it"; of the two risk numbers, "Exit's risk factor"): above 0, the buy reads the direction of the side's risk as the exit reads it (curverisk.risk: the worse of Kalshi's bid net of the fee and the long layer, the price counted in full) over a look back of this share of the TIME LEFT, in the market's own typical move for that span (the moves before the look back). In shadow every decision's note carries the reading, what it would do and the old price trend beside it, and nothing is gated. A first setting, not fitted. 0 = off
    spot_source: str = ""  # 2026-11-11 (user: "a new curve analysis based on kalshi's data only"): "kalshi" = this lane reads the spot for its curve readings (wave, curve edge, the curve exit's chance) from Kalshi's own prices: a coin's settlement index, else Kalshi's perp reference price for the underlying while it is ticking, the external feed only as a fallback (variants.spot_index). "" = as before (settlement index, else BiQuote). Does not reach the curve model's own features (model_buy), the hot-market gate or the hour-drift gate, which still read the older index
    max_spot_crosses: int = 0  # buy only when the spot crossed the strike fewer than this many times in the last five minutes (4 = the oscillating variety G). 0 = off. Commodity states: 4+ crossings lost $0.59 a bet in both halves
    entry_trend_seconds: float = 0.0  # buy only when the side's price (mid of the tape's bid and ask, in its own terms) has not fallen over the last this many seconds by more than entry_trend_min (0 = no check). A market that is falling when we would buy lost 16-18% over 7 days; one flat or rising lost 0-5%
    entry_trend_min: float = 0.0  # the least the side's price must have changed over entry_trend_seconds, in dollars (0.0 = not falling; 0.01 = up a cent)
    entry_spike_seconds: float = 0.0  # no buy on a spike (2026-11-02): the side's price (mid) has risen by entry_spike_rise or more over the last this many seconds, or its bid has swung by entry_spike_range or more inside them (0 = off). 242 settled live commodity bets in dollars: rise over 90 s of 30c+ -$44.5 on $149 (-30%), swing of 30c+ -$34.9 on $248, the rest +$73 on $1,289
    entry_spike_view: bool = False  # 2026-11-16 SHADOW (user: "Yes, log it"): the note of every decision that reaches the buy gates carries how big the last 90 s were on the 3-hour view of the market's Kalshi perp, as [spike view z +1.2, 34 of 120 as large] (curverisk.spike_view; z signed toward the bet side). It gates nothing and is scored forward against the bets that follow. studies/results/2026-11-16-spike-failures-history.txt
    model_top: float = 0.20  # the curve_model step: buy a side whose predicted net per dollar is in the top this share of the training moments (curvemodel.py) ...
    model_category: str = ""  # ... on the model fitted for this category of lanes ("" is the shared one) ...
    model_features: str = "fibonacci"  # ... which reads these curve readings
    record_min: float = 0.20  # the state_record step: the state's recorded net per dollar staked (shrunk toward zero) must be at least this ...
    record_min_n: int = 15  # ... and be read over at least this many earlier markets at the buy's price band (staterecord.py)
    risk_score_buy: float = 0.0  # 2026-11-16 PAPER (user: "all these factors need to build towards that risk score"; "I thought you were putting our new logic up as a lane"): above 0, the lane buys a side only while its risk by riskscore.py (Kalshi's price moved by the curve's readings) is at or under this now and at two looks back that scale with the time left (a thirtieth and a fifteenth of it). No price or minute band is needed beside it. 0 = off. Not shown to pay: the number equalled Kalshi's mid held out
    curve_risk_score: bool = False  # 2026-11-16 PAPER: the risk exit's chance is riskscore.py's number in place of the bid net of its fee, so the line (curve_risk_sell) and its wait act on a risk that is high and has stayed high; an unreadable score falls back to the bid
    entry_scenario_view: bool = False  # 2026-11-16 SHADOW (user: "build a structured buy scenario every 15 mins ... based on the previous hour of ... markets"; "we are building our scenario"; "Yes, build it"): every decision that reaches the buy gates notes the market's scenario (scenario.py): the target's place in the index's range over the 15, 30 and 60 minutes before the open, the drift across each, the cushion now in typical remaining moves, where the spot sits in each range now, and the last four markets' finishing margin and flips. It gates nothing: the labels are scored on the markets that follow before any becomes a rule
    entry_spike_rise: float = 0.30  # dollars, the rise that counts as a spike
    entry_spike_range: float = 0.30  # dollars, the bid swing that counts as a spike
    min_return: float = 0.0  # a win must pay at least this much on the stake, after the fee
    max_bet_pct: float = 0.02  # one bet, share of equity (see sizing.py)
    max_exposure_pct: float = 0.25  # all open bets in the lane, share of equity
    shortlist: int = 10
    numeric: str = "skip"  # "skip" | "allow" markets that resolve on a live number
    trend: str = "none"  # "none" | "rising" (needs history) | "not_falling" (no history is fine)
    one_per: str = "none"  # "none" | "mutex" (one side per exclusive event) | "event" (best edge only)
    series: tuple[str, ...] = ()  # restrict to these series; empty = any
    needs_instrument: bool = False  # series must be a priced commodity (instruments.toml)
    lookback_hours: int = 6  # candle window for the trend rule
    min_trend: float = 0.0  # ask gain over the window a 'rising' lane needs, dollars
    max_drop: float = 0.02  # ask fall (window or last candle) a 'not_falling' lane tolerates
    scope_minutes: float | None = None  # with review_minutes: slow pass that builds the watchlist
    review_minutes: float | None = None  # fast pass over the watchlist; None = the main scan only
    cooldown_minutes: float = 0.0  # skip a market decided on this recently
    buy_max_move: float = 0.0  # skip a buy when the order book's ask on the side has FALLEN more than this many dollars under the ask the decision was made at (0 = off). A job can wait many seconds behind the shared request budget between deciding and ordering; a Gold NO decided at 0.86 was bought at 0.24 eight seconds before the close, after the side had collapsed
    buy_retries: int = 0  # when an immediate buy fills nothing, read the book again and send it once more, up to this many times, right away (0 = no retry). The next signal on the market comes a median 108 s later and about 3.7c dearer
    buy_retry_chase: float = 0.0  # how many dollars over the first limit a retry may pay (0 = never above it). The lane's price band and the risk gate still apply
    buy_slip: float = 0.0  # the buy order is limited this many dollars above the ask the bot read: it still fills at the best ask, and pays up to the allowance only if the book has moved (0 = limit at the ask)
    buy_tick_ahead: bool = False  # 2026-11-08 (user: "just put the buy order at whatever the difference between the last 2 ticks is ahead of the current price"; the buys skipped with "$0.00 offered at 0.42" because the ask had run to 0.44): build the buy limit from the newest ask the quote stream shows, plus the size of the last tick up when the ask is rising (the last two distinct ask values), never under that ask, and never over the decision ask + buy_slip + buy_retry_chase. A falling ask is bought at its new, lower level. 0 = the old limit (the decision ask + buy_slip)
    watch_max: int = 40
    priority: int = 0  # queue priority of this lane's jobs; higher runs first
    trust: float = 1.0  # prior weight on the estimator before our record has any say (0 to 1)
    record_adjust: bool = True  # 2026-11-13 (user: "I want to try the ladder out"): false = the lane bets on its estimator's own chance. The record adjustment (calibration.py) is still computed and written in the note, but not applied. Why: on crypto_ladder_perp the adjustment's weight on the ladder fell from 0.71 to 0.25 in one evening (skill -0.04 over 61 settled), which turned ladder chances of 0.91 and 0.94 at asks of 0.80 and 0.79 into passes, so the lane could not show what the ladder alone does
    sides: tuple[str, ...] = ("yes",)  # contracts the lane may buy: "yes", "no" or both
    timed_suffix: str = ""  # series ending in this are fixed-length windows with an entry window
    timed_minutes: float = 15.0  # length of such a window
    entry_elapsed: tuple[float, float] = (0.0, 1e9)  # bet only this many minutes after it opens
    blackout_et: tuple[str, ...] = ()  # no new entries inside these New York clock windows, each "HH:MM-HH:MM" (a window may cross midnight), or "SERIES@HH:MM-HH:MM" to bar just that series (e.g. "KXNATGAS15M@17:00-18:00", the CME daily break on a thin feed). Open bets are still managed and sold
    shadow_suffix: str = ""  # series ending in this are only measured (see shadow.py), never bet
    enabled: bool = True


def _pair(v: Any) -> tuple[float, float]:
    lo, hi = v
    return float(lo), float(hi)


def builtin_lanes(s: Settings) -> dict[str, LaneSpec]:
    """The three starting lanes, parameterized entirely from settings."""
    lanes = {
        "moonshot": LaneSpec(
            "moonshot", LLM, "long-shot", (s.min_price, s.max_price),
            (s.min_hours_to_close, s.max_hours_to_close), s.max_spread, s.min_volume,
            min_edge=s.min_edge, shortlist=s.shortlist, trend="not_falling", one_per="mutex",
            max_bet_pct=s.moonshot_bet_pct, max_exposure_pct=s.moonshot_exposure_pct,
            lookback_hours=s.steady_lookback_hours, max_drop=s.moonshot_max_drop,
            sides=s.moonshot_sides,
        ),
        "steady": LaneSpec(
            "steady", LLM, "steady", (s.steady_min_price, s.steady_max_price),
            (s.steady_min_hours_to_close, s.steady_max_hours_to_close), s.steady_max_spread,
            s.steady_min_volume, max_lag=s.steady_max_lag, usd_per_trade=s.steady_usd_per_trade or None,
            shortlist=s.steady_shortlist, trend="rising", one_per="mutex", enabled=s.steady_enabled,
            max_bet_pct=s.steady_bet_pct, max_exposure_pct=s.steady_exposure_pct,
            lookback_hours=s.steady_lookback_hours, min_trend=s.steady_min_trend,
            sides=s.steady_sides,
        ),
        "commodity": LaneSpec(
            "commodity", LADDER, "commodity", (s.commodity_min_price, s.commodity_max_price),
            (s.commodity_min_hours_to_close, s.commodity_max_hours_to_close),
            s.commodity_max_spread, s.commodity_min_volume, min_edge=s.commodity_min_edge,
            usd_per_trade=s.commodity_usd_per_trade or None, shortlist=s.commodity_shortlist,
            numeric="allow", one_per="event", needs_instrument=True, enabled=s.commodities_enabled,
            sides=("yes", "no"),
            scope_minutes=s.commodity_scope_minutes, review_minutes=s.commodity_review_minutes,
            cooldown_minutes=s.commodity_cooldown_minutes, watch_max=s.commodity_watch_max,
            priority=10, min_return=s.commodity_min_return, min_age_minutes=s.commodity_min_age_minutes, shadow_suffix="15M", timed_suffix="15M",
            entry_elapsed=(s.commodity_fast_from_min, s.commodity_fast_to_min),
            max_bet_pct=s.commodity_bet_pct, max_exposure_pct=s.commodity_exposure_pct,
        ),
    }
    return lanes


def _overlay(base: LaneSpec | None, name: str, row: dict[str, Any]) -> LaneSpec:
    if "always" in row:  # the old name of needs_model_edge, with the opposite sense
        if "needs_model_edge" in row:
            raise ValueError(f"lane {name!r}: set needs_model_edge or its old name always, not both")
        row = {k: v for k, v in row.items() if k != "always"} | {"needs_model_edge": not row["always"]}
    known = {f.name for f in fields(LaneSpec)}
    unknown = set(row) - known
    if unknown:
        raise ValueError(f"lane {name!r}: unknown field(s) {sorted(unknown)}")
    row = dict(row)
    for key in ("price", "hours", "entry_elapsed"):
        if key in row:
            row[key] = _pair(row[key])
    for key in ("series", "sides", "blackout_et", "skip_varieties", "buy_chain", "sell_chain", "late_swing_tiers"):
        if key in row:
            row[key] = tuple(row[key])
    if any(x in RETIRED_STEPS for x in row.get("buy_chain", ())):
        log.warning("lane %s: its buy steps name %s, which no longer exist; read without them", name, ", ".join(x for x in row["buy_chain"] if x in RETIRED_STEPS))
        row["buy_chain"] = tuple(x for x in row["buy_chain"] if x not in RETIRED_STEPS)
    bad = [x for x in row.get("buy_chain", ()) if x not in BUY_STEPS]
    if bad or len(set(row.get("buy_chain", ()))) != len(row.get("buy_chain", ())):
        raise ValueError(f"lane {name!r}: buy_chain names {bad or 'a step twice'}; the steps are {', '.join(BUY_STEPS)}, each at most once")
    sellchain.check(row.get("sell_chain", ()), f"lane {name!r}")
    for window in row.get("blackout_et", ()):
        try:
            parse_window(window)
        except ValueError as exc:
            raise ValueError(f"lane {name!r}: {exc}") from exc
    if bad := set(row.get("sides", ())) - {"yes", "no"}:
        raise ValueError(f"lane {name!r}: sides must be \"yes\" and/or \"no\", got {sorted(bad)}")
    if base is None:
        missing = {"estimator", "price", "hours"} - set(row)
        if missing:
            raise ValueError(f"lane {name!r}: new lanes need {sorted(missing)}")
        row.setdefault("label", name)
        row.setdefault("max_spread", 0.05)
        row.setdefault("min_volume", 0.0)
        return LaneSpec(name=name, **row)
    return replace(base, **row)


BUY_STEPS = ("price", "risk_score", "risk_trend", "price_trend", "perp_worth", "calm", "variety", "vol_regime", "cushion", "spike", "center_agree", "same_side", "cooldown", "state_record", "state_record_shadow", "curve_model", "binary_risk_shadow", "win_chance_shadow", "win_value_shadow", "binary_risk", "swing_risk", "late_swing", "ladder")  # the steps a lane may name in buy_chain; buychain.py holds one function for each
RETIRED_STEPS = ("context_score",)  # steps that no longer exist: a lane file that still names one loads without it and says so in the log, so a config written for an older image does not stop the bot. context_score (shadow, never said no) was removed 2026-11-21: on the bot's own 132 live commodity bets its percentile did not separate the bets that ended ahead from the ones behind (80%, 77%, 80% ahead by thirds)


def _inherited(rows: dict[str, Any], name: str, _seen: tuple[str, ...] = ()) -> dict[str, Any]:
    """A lane's settings with `inherit = "other_lane"` resolved: the other lane's settings from strategies.toml, then this lane's own on top.
    Lets a paper twin say only how it differs. The chain may be several lanes long; a loop is an error."""
    row = dict(rows[name])
    parent = row.pop("inherit", None)
    if parent is None:
        return row
    if parent in _seen or parent == name:
        raise ValueError(f"lane {name!r}: inherit loops back through {' -> '.join((*_seen, name, parent))}")
    if parent not in rows:
        raise ValueError(f"lane {name!r}: inherit = {parent!r}, which is not a lane in strategies.toml")
    return {**_inherited(rows, parent, (*_seen, name)), **row}


def _with_module(row: dict[str, Any], name: str, modules: dict[str, Any]) -> dict[str, Any]:
    """A lane's settings with `buy_module = "name"` resolved (2026-11-17, user: "we should be able to have multiple modules. And assign a lane to a module"). A module is a named buy chain in strategies.toml:

        [module.risk_first]
        label = "the risk score picks the side, the ladder checks the worth last"
        steps = ["risk_score", "price", "ladder"]
        risk_score_buy = 0.25        # optional: any lane setting, as the module's default

    The lane's buy_chain becomes the module's steps, and every other setting the module writes is the lane's unless the lane writes its own. Many lanes may name one module; a lane names at most one, and does not also write buy_chain."""
    module = row.get("buy_module")
    if not module:
        return row
    if module not in modules:
        raise ValueError(f"lane {name!r}: buy_module = {module!r}, which is not a module in strategies.toml (it has: {', '.join(sorted(modules)) or 'none'})")
    if "buy_chain" in row:
        raise ValueError(f"lane {name!r}: it names buy_module {module!r} and writes buy_chain too; the module's steps are the chain")
    mod = dict(modules[module])
    steps = mod.pop("steps", None)
    mod.pop("label", None)
    if not steps:
        raise ValueError(f"module {module!r}: it needs `steps`, the buy's steps in order (of {', '.join(BUY_STEPS)})")
    if "buy_module" in mod or "inherit" in mod or "buy_chain" in mod:
        raise ValueError(f"module {module!r}: a module holds `steps`, a `label` and lane settings; it does not name a module, a chain or a lane to inherit")
    return {**mod, **row, "buy_chain": list(steps)}


EXIT_PARTS = ("profit", "loss")
_EXIT_PREFIXES = ("curve_",)  # the lane settings that are about selling
_PROFIT_PREFIXES = ("curve_take",)  # the ones that sell a gain
_PROFIT_FIELDS = ("curve_risk_min_profit",)  # the least gain the risk exit's profit take exits


def exit_part(field_name: str) -> str | None:
    """Which part of an exit module a lane setting belongs in: "profit" (it exits a gain), "loss" (it exits a loss, or holds one), or None when it is not an exit setting. Settings both parts read (how the exit reads the bet: its scales, its waits, whether it is live) are "loss" by this rule and may also sit at the module's top level."""
    if field_name not in LaneSpec.__dataclass_fields__ or not field_name.startswith(_EXIT_PREFIXES):
        return None
    return "profit" if field_name.startswith(_PROFIT_PREFIXES) or field_name in _PROFIT_FIELDS else "loss"


def _with_exit(row: dict[str, Any], name: str, exits: dict[str, Any]) -> dict[str, Any]:
    """A lane's settings with `exit_module = "name"` resolved (2026-11-18, user: "we need a sell module which covers profit and loss"; "you mentioned it above, exit module ... do that"). An exit module is a named exit in strategies.toml, in two parts:

        [exit.risk_exit]
        label = "the risk exit: a gain is taken when the curve says so, a loss is sold under the line"
        curve_risk_live = true           # at the top: what both parts read (how the exit reads the bet)

        [exit.risk_exit.profit]
        curve_take = true                # the settings that sell a gain

        [exit.risk_exit.loss]
        curve_risk_sell = 0.10           # the settings that sell a loss, or hold one

    A module holds `steps` at the top: the sale's steps in the order they are asked (sellchain.py), which become the lane's sell_chain; a lane on a module does not also write sell_chain.

    Every setting the module writes is the lane's unless the lane writes its own. A module must have both parts (either may be empty: the part is then the defaults), holds exit settings only, each in the part it belongs to (exit_part), and writes none twice. Many lanes may name one module; a lane names at most one."""
    module = row.get("exit_module")
    if not module:
        return row
    if module not in exits:
        raise ValueError(f"lane {name!r}: exit_module = {module!r}, which is not an exit module in strategies.toml (it has: {', '.join(sorted(exits)) or 'none'})")
    mod = dict(exits[module])
    mod.pop("label", None)
    steps = mod.pop("steps", None)  # the sale's steps in the module's order (sellchain.py)
    if not steps:
        raise ValueError(f"exit module {module!r}: it needs `steps`, the sale's steps in order (of {', '.join(sellchain.SELL_STEPS)})")
    if "sell_chain" in row:
        raise ValueError(f"lane {name!r}: it names exit_module {module!r}, which has `steps`, and writes sell_chain too; the module's steps are the chain")
    sellchain.check(steps, f"exit module {module!r}")
    flat: dict[str, Any] = {}
    for part in EXIT_PARTS:
        got = mod.pop(part, None)
        if not isinstance(got, dict):
            raise ValueError(f"exit module {module!r}: it needs a [exit.{module}.{part}] part (an exit module covers profit and loss; an empty part is the defaults)")
        for k, v in got.items():
            belongs = exit_part(k)
            if belongs is None:
                raise ValueError(f"exit module {module!r}: {k!r} in its {part} part is not an exit setting of a lane")
            if belongs != part:
                raise ValueError(f"exit module {module!r}: {k!r} is a {belongs} setting, written in the {part} part")
            flat[k] = v
    for k, v in mod.items():
        if exit_part(k) is None:
            raise ValueError(f"exit module {module!r}: {k!r} is not an exit setting of a lane (an exit module holds a `label`, a `profit` part, a `loss` part and exit settings)")
        if k in flat:
            raise ValueError(f"exit module {module!r}: {k!r} is written twice (at the top and in a part)")
        flat[k] = v
    return {**flat, **row, "sell_chain": list(steps)}


def lane_specs_from(s: Settings, strategies_toml: str | None, overrides_json: str | None = None) -> dict[str, LaneSpec]:
    lanes = builtin_lanes(s)
    if strategies_toml:
        doc = tomllib.loads(strategies_toml)
        rows, modules, exits = doc.get("lane") or {}, doc.get("module") or {}, doc.get("exit") or {}
        for name in rows:
            lanes[name] = _overlay(lanes.get(name), name, _with_module(_with_exit(_inherited(rows, name), name, exits), name, modules))
    lanes = overrides.apply(lanes, overrides.parse(overrides_json))  # the admin page's changes, already checked against overrides.TUNABLES
    for spec in lanes.values():
        if tiered(spec) and not (spec.series or spec.needs_instrument):
            raise ValueError(
                f"lane {spec.name!r}: review_minutes needs `series` or `needs_instrument`, "
                "because a fast pass can only fetch what it can name"
            )
    if not s.paper_lanes and s.live_lanes is not None:
        # The live host runs only what is live. A switched-off lane keeps its spec, so a bet it left open is still stopped and sold.
        lanes = {name: spec if name in s.live_lanes or name in s.paper_named else replace(spec, enabled=False) for name, spec in lanes.items()}
    return lanes


@lru_cache(maxsize=8)
def _cached(s: Settings, text: str | None, changes: str | None) -> dict[str, LaneSpec]:
    return lane_specs_from(s, text, changes)


def strategies_path(data_dir: Path) -> Path:
    return data_dir / "strategies.toml"


def recorder_lanes(s: Settings) -> list[tuple[str, tuple[str, ...]]]:
    """What the tape recorder needs of the lane file, and nothing else: (lane, its series) for every lane that is on and two-level (2026-11-18, user: "it should only parse what it needs to record. so the updates to it are less often").

    The recorder runs in its own container on an image a bot deploy does not replace. It used to load every lane in full, so a lane file that named a setting its older image did not know was refused, the recorder followed no markets and the tape stopped (2026-11-16, and again 2026-11-18 08:16 to 09:00 ET on `exit_module`). Here only three settings are read from each lane, raw from the file: `enabled`, `review_minutes` (set = a two-level lane) and `series`, through `inherit`, the lane's buy module's defaults and the built-in lanes, with the admin page's `enabled` on top. Every other setting, table and value in the file is ignored, known to this image or not."""
    path = strategies_path(s.data_dir)
    return _recorder_lanes(s, path.read_text() if path.is_file() else None, overrides.load_text(s.data_dir))


@lru_cache(maxsize=4)
def _recorder_lanes(s: Settings, text: str | None, changes: str | None) -> list[tuple[str, tuple[str, ...]]]:
    doc = tomllib.loads(text) if text else {}
    rows, modules = doc.get("lane") or {}, doc.get("module") or {}
    base = builtin_lanes(s)
    try:
        page = (json.loads(changes or "{}").get("lanes") or {})
    except (ValueError, AttributeError):
        page = {}
    out = []
    for name in sorted(set(base) | set(rows)):
        try:
            row = _inherited(rows, name) if name in rows else {}
        except ValueError:
            row = dict(rows[name])  # an inherit this image cannot follow: the lane's own lines
        mod = modules.get(row.get("buy_module") or "") or {}
        mod = mod if isinstance(mod, dict) else {}

        def val(key: str, default: Any, row: dict[str, Any] = row, mod: dict[str, Any] = mod, name: str = name) -> Any:
            return row[key] if key in row else mod[key] if key in mod else getattr(base[name], key) if name in base else default

        enabled = (page.get(name) or {}).get("enabled", val("enabled", True)) if isinstance(page.get(name), dict) else val("enabled", True)
        series = val("series", ())
        if enabled is True and val("review_minutes", None) is not None:
            out.append((name, tuple(str(x) for x in series) if isinstance(series, list | tuple) else ()))
    return out


def lane_specs(s: Settings) -> dict[str, LaneSpec]:
    """Lanes for these settings, with DATA_DIR/strategies.toml applied. Cached per file content."""
    path = strategies_path(s.data_dir)
    text = path.read_text() if path.is_file() else None
    return _cached(s, text, overrides.load_text(s.data_dir))


def enabled_lanes(s: Settings) -> list[LaneSpec]:
    return [lane for lane in lane_specs(s).values() if lane.enabled]


def unknown_live_lanes(s: Settings) -> list[str]:
    """Names in LIVE_LANES that are not lanes. A typo there silently trades nothing, so it is reported."""
    return sorted((s.live_lanes or frozenset()) - set(lane_specs(s)))


def tiered(spec: LaneSpec) -> bool:
    """True for a two-level lane: scoped slowly into a watchlist, reviewed quickly from it."""
    return spec.review_minutes is not None


def main_lanes(s: Settings) -> list[LaneSpec]:
    """Enabled lanes served by the all-markets scan."""
    return [lane for lane in enabled_lanes(s) if not tiered(lane)]


def tiered_lanes(s: Settings) -> list[LaneSpec]:
    return [lane for lane in enabled_lanes(s) if tiered(lane)]


LIVE_BOOST = 1000  # a live lane's jobs are claimed before any paper lane's, whatever the lanes' own priority numbers say


def job_priority(s: Settings, spec: LaneSpec) -> int:
    """The priority a lane's jobs are queued at (2026-11-12, user: "we only have 2 live lanes ... they should run on different queues"): the lane's own number, plus LIVE_BOOST when the lane places real orders. Before this a paper lane at 48 and the treasury lane at 50 were claimed ahead of the live lanes at 46 and 47."""
    return spec.priority + (LIVE_BOOST if s.live_lanes is not None and s.is_live(spec.name) else 0)


def spec_for(s: Settings, name: str) -> LaneSpec:
    lanes = lane_specs(s)
    if name not in lanes:
        raise KeyError(f"unknown lane {name!r} (known: {', '.join(sorted(lanes))})")
    return lanes[name]


# Settings that only say how the bot goes about watching a lane, or how its record is read: changing one is not a change to what the lane does.
_NOT_LOGIC = frozenset({
    "name", "label", "enabled", "shortlist", "scope_minutes", "review_minutes", "priority", "watch_max", "cooldown_minutes", "shares_markets",
    "buy_redecide_shadow", "stake_vol_shadow", "record_adjust", "paper_fill_model", "buy_slip", "buy_max_move", "buy_retries", "buy_retry_chase",
    "buy_module", "exit_module", "entry_spike_view", "entry_scenario_view",
})


def settings_of(spec: LaneSpec, s: Any) -> dict[str, str]:
    """Everything that decides what this lane buys, how much, and how it gets out, including the global rules that reach it (2026-11-19: rebuilt when the rules outside the buy and sell modules were removed, so every lane has a new version from that day)."""
    out = {f.name: repr(getattr(spec, f.name)) for f in fields(spec) if f.name not in _NOT_LOGIC}
    out["buy_chain"] = repr(tuple(x for x in spec.buy_chain if x not in ("state_record_shadow", "binary_risk_shadow", "win_chance_shadow", "win_value_shadow")))  # a shadow step that never says no changes nothing the lane does: not a new version
    out["kelly_fraction"] = repr(None if spec.fixed_stake else s.kelly_fraction)
    chain = tuple(x for x in spec.sell_chain if not (x == "late_tight" and not (spec.curve_risk_late_tight > 0 and spec.curve_risk_late_tight_live)))  # late_tight in shadow changes nothing the lane does, so it is not part of the version
    out["sell_chain"] = repr(chain)
    if not (spec.curve_risk_late_tight > 0 and spec.curve_risk_late_tight_live):  # in shadow it changes nothing the lane does
        out.pop("curve_risk_late_tight", None), out.pop("curve_risk_late_tight_share", None), out.pop("curve_risk_late_tight_live", None)
    if not (spec.curve_risk_under_even > 0 and spec.curve_risk_under_even_live):
        out.pop("curve_risk_under_even", None), out.pop("curve_risk_under_even_live", None)
    if not spec.rebuy_opposite_after_gain_seconds:  # off, the lane buys what it always did: no lane that leaves it off gets a new version
        out.pop("rebuy_opposite_after_gain_seconds", None)
    return out


ET = ZoneInfo("America/New_York")
_WINDOW = re.compile(r"^(?:([A-Z0-9]+)@)?(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$")


def parse_window(text: str) -> tuple[str, int, int]:
    """"[SERIES@]HH:MM-HH:MM" as (series, start, end), the times in minutes after midnight. The series is "" when the window applies to every series. A window of no length is an error."""
    m = _WINDOW.match(text.strip())
    if not m:
        raise ValueError(f"blackout window {text!r} is not [SERIES@]HH:MM-HH:MM")
    a, b = int(m.group(2)) * 60 + int(m.group(3)), int(m.group(4)) * 60 + int(m.group(5))
    if not (0 <= a < 1440 and 0 <= b < 1440) or a == b:
        raise ValueError(f"blackout window {text!r} is not a time range")
    return m.group(1) or "", a, b


def in_blackout(spec: LaneSpec, now: datetime, series: str = "") -> bool:
    """Whether `now` is inside one of the lane's blackout windows for this series, read on the New York clock (so it follows daylight saving).
    A window with no series bars every series; one that names a series bars only that one (and, with no series given, is not counted)."""
    if not spec.blackout_et:
        return False
    local = now.astimezone(ET)
    minute = local.hour * 60 + local.minute
    for text in spec.blackout_et:
        only, a, b = parse_window(text)
        if only and only != series:
            continue
        if (a <= minute < b) if a < b else (minute >= a or minute < b):
            return True
    return False


def version_of(spec: LaneSpec, s: Any) -> str:
    """A short fingerprint of the lane's logic. Any change to it is a new version of the lane."""
    return hashlib.sha1(json.dumps(settings_of(spec, s), sort_keys=True).encode()).hexdigest()[:8]




def wants_bet(spec: LaneSpec, p: float, ask: float, edge: float) -> bool:
    """The lane's bet rule, from its parameters. An edge bar and a lag bar can both be set."""
    if not spec.needs_model_edge:
        return True
    if spec.max_lag is not None and ask - p > spec.max_lag + 1e-9:
        return False
    if spec.min_edge is not None and edge < spec.min_edge:
        return False
    if spec.min_edge_room is not None and scaled_edge(p, edge) < spec.min_edge_room - 1e-12:
        return False
    return spec.max_lag is not None or spec.min_edge is not None or spec.min_edge_room is not None


def edge_verdict(spec: LaneSpec, p: float, ask: float, edge: float) -> str | None:
    """Why wants_bet turned the buy down, as one sentence, or None when it did not. The same checks in the same order (2026-11-17, user: "are we flagging why we turn things down?": the edge screen was the one rule that passed in silence, so a pass at it read as a bare "price band")."""
    if wants_bet(spec, p, ask, edge):
        return None
    if spec.max_lag is not None and ask - p > spec.max_lag + 1e-9:
        return f"edge screen: the model's chance {p:.2f} is {ask - p:.2f} under the price {ask:.2f}; the lane allows {spec.max_lag:.2f}."
    if spec.min_edge is not None and edge < spec.min_edge:
        return f"edge screen: the edge {edge:+.3f} (the model's chance {p:.2f} less the price {ask:.2f} and its fee) is under the lane's {spec.min_edge:+.3f}."
    if spec.min_edge_room is not None:
        room = (1.0 - p) + edge
        if edge < 0:
            return f"edge screen: the model's chance {p:.2f} is under what the side costs ({ask:.2f} and its fee): the edge is {edge:+.3f} and the lane needs +{spec.min_edge_room * room:.3f} ({spec.min_edge_room:.0%} of the room to a sure win)."
        return f"edge screen: the edge {edge:+.3f} (the model's chance {p:.2f} over the price {ask:.2f} and its fee) is under the +{spec.min_edge_room * room:.3f} the lane needs ({spec.min_edge_room:.0%} of the room to a sure win)."
    return "edge screen: the lane needs an edge from its model and has no bar set to measure one."






def scaled_edge(p: float, edge: float) -> float:
    """The edge as a share of the room between the all-in cost and certainty (lane field min_edge_room). The edge is p - ask - fee, so that room, 1 - ask - fee, is (1 - p) + edge. No room left (a cost of $1 or more) is no edge."""
    room = (1.0 - p) + edge
    return edge / room if room > 1e-9 else -1.0
