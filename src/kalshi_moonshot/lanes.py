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
import re
import tomllib
from dataclasses import dataclass, fields, replace
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import overrides
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
SHAPE = "llm_shape"  # the language model reads the shapes of similar past 15-minute windows (shape.py)


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
    max_lag: float | None = None  # ... or, when set, when the estimate is no more than this under the ask
    usd_per_trade: float | None = None  # optional dollar ceiling per bet; None/0 = sized by balance
    fixed_stake: float = 0.0  # dollars per bet, ignoring Kelly (0 = size by the odds and the balance)
    always: bool = False  # bet whenever the filters pass, with no minimum edge from a model
    min_age_minutes: float = 0.0  # skip a market until it has been open this long (books settle down)
    gate_from: tuple[str, ...] = ()  # lanes whose finished bets decide which minutes of a 15-minute window this lane may enter (see gate.py)
    gate_since: str = ""  # a date (YYYY-MM-DD, New York time): only bets placed on or after it count toward the gate (a reset)
    gate_by: str = "band"  # "band": one record per minute band; "series": each market (series) keeps its own record, shrunk toward the lane's pooled band
    gate_pool: float = 30.0  # with gate_by="series": how many pooled bets' worth of prior a market's own record is blended with
    gate_min_n: int = 30  # finished bets a minute band needs before it can open
    gate_z: float = 1.0  # standard errors subtracted from a band's mean return before it must beat zero
    grid: bool = False  # buy only where the learned grid (grid.py) has a cell that pays; the lane's own exit settings manage the position
    knn: bool = False  # decide by the nearest-neighbour model of past entries (knn.py) instead of hard buckets
    knn_k: int = 300  # how many nearest past entries the estimate uses
    knn_min_n: float = 25.0  # comparable windows (after weighting) needed before it may buy
    knn_z: float = 3.0  # standard errors the estimate's mean must clear zero by
    knn_rule: str = "trail"  # the exit the past entries are scored under: "trail" (the live crash exit) or "hold"
    grid_rule: str = "trail"  # how the grid replays each entry: "trail" (the live peak-crash exit) or "hold" (to settlement, for a lane with no exit)
    grid_min_n: int = 30  # finished replays a cell needs before it can decide
    grid_z: float = 3.0  # standard errors a cell's mean must clear zero by (strict on purpose: of ~300 cells, some clear 2 by luck alone)
    scale: bool = False  # stake and exposure cap grow with the lane's own record (scale.py); the base stake until the record clears the bar
    scale_from: tuple[str, ...] = ()  # lanes whose finished bets count as the record (default: this lane)
    scale_since: str = ""  # only count bets opened on or after this New York date (YYYY-MM-DD)
    scale_min_n: int = 40  # finished bets needed before the stake can grow
    scale_z: float = 2.0  # standard errors the average return must clear zero by (one test per lane, not a search)
    scale_kelly: float = 0.25  # share of the Kelly stake the lower bound justifies
    scale_old_weight: float = 0.5  # how much the previous version's bets count, per version back (a new version is usually an improvement, so its record starts partly from the old one's, and fades from it as its own builds)
    scale_max_mult: float = 3.0  # most the stake and exposure cap may grow to, as a multiple of the base
    shares_markets: bool = False  # main-scan lane that also takes markets another lane took (for A/B controls); never consumes them
    min_chance: float = 0.0  # bet only when the estimator's chance that the contract pays is in [min_chance, max_chance]
    max_chance: float = 1.0  # (the estimate before calibration; for a ladder lane this is a distance to the strike)
    unstable_gain: float = 0.0  # sell when the position is up at least this fraction (after fees) AND the bid is swinging (see unstable_range); 0 = off
    unstable_range: float = 0.0  # ... the bid's high-low over unstable_seconds, in dollars
    unstable_seconds: float = 20.0
    hover_dip: float = 0.0  # sell at the first profit (after fees) once the bid has dipped to this price or lower since entry, i.e. hovered near the strike; 0 = off
    entry_checks_exit: bool = False  # 2026-11-08 (user, XRP 09:30 and 7 more: bought with the spot already behind the strike and sold 0-5 s later by the cross exit; "lets do 1"): skip a buy when this lane's own spot exits (shock_units, cross_units) would sell it at this moment, so the buy and the sale read the same state (variants.spot_exit_now)
    rebuy_opposite_after_gain_seconds: float = 0.0  # 2026-11-08 (user, HYPE 09:15: the value exit sold YES at 0.67 for a gain and the lane bought YES again 94 s later at 0.67 and lost $2.22; "a 60-ish sell is great but we might want to rebuy if confidence continues to increase"): after this lane sells one side of a market for a gain (the sale after its fee over the price paid after its fee), it may not buy that side again for this many seconds; the opposite side goes through the lane's usual gate at once, and its own gain sale starts the same wait on its side. After the wait the side is open again. A sale at a loss sets no limit (0 = off). A fixed number, set by the user, not read from the curve
    reentry_cooldown_seconds: float = 0.0  # after this lane sells a market, wait this long before it may buy that market again (0 = no wait)
    buy_rest_seconds: float = 0.0  # buy with a post-only RESTING order instead of an immediate one: it waits this many seconds at buy_rest_below under the ask and is dropped if unfilled (no fallback). A resting order that fills pays no taker fee; replay says it mostly fills on dips (0 = buy at the ask)
    buy_rest_below: float = 0.01  # how far under the ask the resting buy sits, in dollars
    paper_fill_model: bool = False  # a PAPER bet of this taker lane is judged on the tape after the fact (paperfill.settle_taker, fillmodel.py): refilled at the ask the tape shows within the lane's limit, or turned into a pass when the ask was over it. Calibrated on the ordinary lanes (0.4-0.9c price error); not for burst lanes
    buy_rest_then_take: bool = False  # when the resting buy fills nothing, buy at the fresh ask instead of dropping the bet (the way buy_retries re-prices: the lane's price band, buy_retry_chase and the risk gate still apply). Fills pay no fee; the runners a resting bid misses are still bought
    buy_rest_top_up: bool = False  # 2026-11-06: when the resting buy fills only part of the order (0.42 of 11.36 palladium, 4.73 of 11.76 Brent), buy the rest at the fresh ask under the same guards as buy_rest_then_take (price band, buy_retry_chase, risk gate). Needs buy_rest_then_take. A partial fill pays no fee on its part; the rest pays the taker fee
    curve_shadow: bool = False  # 2026-11-06: log, never act, the curve-read sell-and-flip on this lane's open bets in their last five minutes (curveexit.py): the bounce off the best spot level in typical minutes, the path-efficiency state against the market's own cutoffs, the sale and the other-side buy it would make, scored when the market settles
    confidence_shadow: bool = False  # 2026-11-06: log, never act, the curve-read chance this lane's open crypto bets settle their way, at 300, 180 and 90 s left, against the market price (confidence.py); refit daily from the tape; scored when the market settles
    accumulate_shadow: bool = False  # 2026-11-07 (user: add to a winning bet late): log, never act, the late add-on on this lane's open bets (accumulator.py): the first moment the held side's ask is 0.85-0.99 with 5-120 s left, with the cushion's z for the seconds left and its 13 and 55 minute slopes. Scored per variant; studies/results/2026-11-07-accumulator.txt
    max_open: int = 0  # at most this many bets of this lane open at once, across all its markets (0 = no limit); the next candidate waits until one closes
    max_same_side: int = 0  # at most this many of this lane's open bets on the same side of the same 15-minute window (BTC, ETH and DOGE YES together is one bet on one move); 0 = no limit. 2026-11-02 replay, 30 days of crypto_favorite: the 3rd same-side bet in a window won 13 of 22 for -$46.6, the 1st and 2nd won 82-86%
    same_side_lanes: tuple[str, ...] = ()  # lanes whose open bets count toward max_same_side together with this lane's own (empty = this lane alone), so two lanes on one coin family share one cap
    shock_bp: float = 0.0  # shock exit: in the last shock_last seconds, sell when the spot has moved this many basis points against the bet over the last shock_seconds and is within shock_margin_bp of the strike (0 = off). Commodities read BiQuote against its price at the window open, crypto reads the settlement index against the strike. 2026-11-03 replay of 259 held commodity bets over 12 days: 60 s, 8 bp, 5 bp +$44; crypto gaps in seconds, so it sold only for a few cents there
    shock_seconds: float = 60.0
    shock_margin_bp: float = 5.0
    shock_source: str = "index"  # 2026-11-09 (user: the shock exit for the treasury lane, modular for any lane): what the shock exit reads. "index" = the market's spot index against its strike or open price (quoteshock.py not used); "quote" = the contract's own mid (the held side's mid as the spot, 0.50 as the strike, the typical minute of the mid), for a market with no spot feed. shock_units and shock_margin_units apply to either; cross_units, the trend hold and the breadth shadow are index-only
    shock_units: float = 0.0  # 2026-11-06: the shock exit in the market's own variance. Sell when the spot has moved this many typical minutes against the bet over shock_seconds (a typical minute: the rms 1 s move of the last 15 minutes times sqrt(60), read live from the market's ticks). Replaces shock_bp when set; if the typical minute cannot be read the exit does not fire (no assumption)
    shock_margin_units: float = 0.0  # ...and the cushion in typical minutes (0 = use shock_margin_bp)
    cross_units: float = -1.0  # 2026-11-07 (user: SOL 09:30 had no exit): also sell, in the last shock_last seconds, when the cushion is at or under this many typical minutes from the strike (0 = the spot is on the wrong side of it) and the bid is in the shock band, whatever the move was. -1 = off. studies/results/2026-11-07-strike-cross-replay.txt
    shock_last: float = 120.0
    value_exit: float = 0.0  # 2026-11-08 (user: ETH 07:30 YES bought at 0.31 was bid 0.63 seven seconds later and settled 0; "why isn't this built into our exit rule?"): sell a 15-minute crypto bet while the bid, after the sell fee, is this many dollars over the curve's own chance that the side pays (confidence.py: the same curve, time-left and price model the confidence shadow reads) and the bid is inside value_exit_bid. 0 = off. studies/results/2026-11-07-hold-value-exit.txt: margin 0.02 sold 809 times, +$30.0 against holding, halves +0.020/+0.054 per contract (bid 0.30-0.70 both halves positive, over 0.70 about zero)
    value_exit_bid: tuple[float, float] = (0.30, 0.70)  # the bid band the value exit sells into: the study's band where it paid in both halves (chosen after seeing the results)
    value_exit_gain_x: float = 0.0  # 2026-11-08 (user: the profit sale sells for a garbage profit, 0.50 bought and sold at 0.54 fourteen seconds later; it should be loose until an extreme profit): the extreme-profit mode of the value exit. Sell only when the bid is at least this multiple of the price the buy filled at (2.0: 20 to 40, 30 to 60, 40 to 80); the curve-chance test is then not used. 0 = off
    value_exit_high: float = 0.0  # ...or when the bid is at or over this price (0.95) with more than value_exit_high_left seconds left (a near-certain win taken early, a lock rather than a wait for the close). 0 = off
    value_exit_high_left: float = 420.0  # seconds that must be left for the value_exit_high sale (7 minutes)
    value_exit_min_gain: float = 0.0  # ...and in this mode never for less than this many dollars per contract after both fees (the sale after its fee over the price paid after its fee): no garbage profit. 0 = any gain over the fees
    late_drop_units: float = 0.0  # 2026-11-10 (user: "30 s, 1 unit sounds good, implement and push"): in the last late_drop_seconds the held bid is sold when it has fallen from its highest of the last 60 s by this many of its own typical minutes (rms one-second bid change over up to 300 s, times sqrt(60)). 0 = off. studies/results/2026-11-10-zero-loss.txt: on 716 favorite bets 30 s / 1 unit turned 54 of 90 whole-stake losses into partial ones for -$1.80 net held-to-result (43 winners sold); crypto_multiscale +$3.8 (settings chosen after seeing the results)
    late_drop_seconds: float = 30.0  # lane late_drop_seconds: the window before the close in which late_drop_units applies
    hour_drift_q: float = -1.0  # 2026-11-10 (user: live now): buy only when the hour's drift signed with the bet (in the typical minute of that hour) is at or under this quantile of the same index's own readings over its last hours (variants.hour_drift_from); -1 = off. The bet leaning against the hour's move is the one that paid: studies/results/2026-11-10-underdog-walkforward.txt, 8 test days, q 0.25 +7.1% a dollar (se 3.3) against -7.0% for every underdog state, both halves. No data = no buy
    perp_lead_buy: bool = False  # 2026-11-10 (user: live, buy on either rule): with hour_drift_q set, a market the hour-drift gate refuses is still bought when the perp-lead rule passes (variants.perp_rule_from: the Hyperliquid perp running ahead of the index, the cushion near the strike, a choppy path, each against this market's own recent readings). BTC, ETH, SOL, XRP, DOGE only. studies/results/2026-11-10-underdog-walkforward.txt: +8.1% a dollar on 217 tape states (se 8.4), both halves; inside its standard error, so each buy's note says which rule passed
    curve_exit: float = -1.0  # 2026-11-10 (user: enable on commodity_multiscale; curvevalue.py): the curve exit's margin, -1 = off. Sell when the bid after the sell fee is at least the curve's own chance that the side pays (the pessimistic one over the 900 s and 180 s horizon-scale rates) plus this margin, held curve_exit_persist seconds. Replaces the shock, late-drop and value exits on a lane that sets it
    curve_exit_scales: str = "900+180"  # which horizon-scale rates the curve exit reads: "900+180" the lower chance of the 900 s and 180 s rates, "900" the 900 s rate alone
    curve_exit_persist: float = 5.0  # seconds the whole condition must hold before the sale
    curve_exit_last: float = 900.0  # act only in the last this-many seconds before the close
    curve_drop_units: float = -1.0  # 2026-11-10 (user: the curve exit needs a shock term that reads the cushion's trajectory; five crypto YES bets lost together while the bid stayed under the curve's chance all the way down): the curve exit's trajectory sale, -1 = off, needs curve_exit on. Also sell when the cushion has fallen this many typical minutes over the last curve_drop_seconds and now sits at or under curve_drop_margin_units, held curve_drop_persist seconds. In the lane's own variance units. scripts/curve_trajectory_replay.py, studies/results/2026-11-10-curve-trajectory.txt
    curve_drop_seconds: float = 120.0
    curve_drop_margin_units: float = 0.5
    curve_drop_persist: float = 5.0
    curve_gap: float = -1.0  # 2026-11-10 (user: reading the contract's own price "should be built into the curve exit logic"; KXPLATINUM15M-26NOV101830 YES at 0.92 lost $10.75 with the BiQuote spot above the strike while the settlement source fell under it and the bid went 0.89 -> 0.50): the curve exit's market-disagrees sale, -1 = off, needs curve_exit on. Also sell when the curve's chance is at least this far ABOVE the bid net of the fee for curve_gap_persist seconds: the market prices the position far under what our spot says, so our reading is the one in doubt. scripts/curve_quote_term_replay.py, studies/results/2026-11-10-curve-quote-term.txt
    curve_gap_persist: float = 15.0
    curve_behind_last: float = -1.0  # 2026-11-10 (user: "we just need to make sure we don't have those 0.00 exits ... why isn't it considering its time to go if time is running out"): the curve exit's time-to-go sale, -1 = off, needs curve_exit on. In the last this-many seconds (down to 8 s before the close), sell a position whose curve chance is under one half, at whatever the bid is. It turns a 0.00 result into a small recovery; it is not a profit rule. scripts/zero_exit_study.py, studies/results/2026-11-10-zero-exit.txt
    shock_persist: float = 0.0  # 2026-11-09 (user: "improve the exit", live as a fix): the shock exit sells only once its whole condition (the move, the cushion, the bid band, the last-seconds window) has held for this many consecutive seconds, so a spike that reverses at once is not sold. 0 = sell on the first reading. studies/results/2026-11-09-shock-exit-improve.txt: on 511 favorite-lane bets 20 s with 1.5 typical minutes, cushion 0.3, last 180 s added +$22 over the current rule, positive in both halves and all three curve shapes (settings chosen after seeing the results); on crypto no variant held up
    shock_memory: float = 0.0  # seconds a shock is remembered: the worst shock_seconds move seen in this many seconds counts, not only the latest one (0 = only the latest). The shock can come just before the last-seconds window and fade (Brent, 2026-11-03: -12 bp at 125 s left, -8 by 120); with 120 s of memory the replay on 324 held commodity bets gave +$38 against +$15 for the latest move alone, and nothing on crypto
    wrong_side_bp: float = 0.0  # wrong-side exit: sell once the spot has sat this many basis points on the wrong side of its window-open price (the strike for crypto) for wrong_side_seconds straight (0 = off). Only for a position whose variety at the buy is in wrong_side_varieties (variants.py). 2026-11-03 replay of 76 held commodity bets read as G (oscillating): 5 bp for 15 s sold 15, 14 of them losers, +$20; the near varieties lost money on it and crypto gained nothing
    wrong_side_seconds: float = 15.0
    wrong_side_varieties: tuple[str, ...] = ()  # empty = any variety
    wrong_side_last: float = 0.0  # only in the last this-many seconds before the close, and the wrong-side clock starts inside them (0 = the whole window)
    wrong_side_series: tuple[str, ...] = ()  # only these series (empty = the lane's own). 2026-11-03 replay of 63 held WTI bets: 1 bp for 10 s in the last 120 s sold 11, 10 of them total losses, +$19 against holding; other commodities +$21 at best, mostly near zero
    shock_bid_max: float = 0.0  # the shock exit sells only a bid at or under this (0 = 0.89, the built-in ceiling). Lower it to leave a position alone while the market still prices it as a likely winner: 2026-11-04 a silver NO was sold at 0.89 with 36 s left and won
    shock_log_only: bool = False  # the shock watch runs and writes shock_log (a would-fire row is fired=2) but never sells, so a lane can collect the data without trading on it. Not part of the lane version
    shock_off_series: tuple[str, ...] = ()  # series the shock exit does not act on, e.g. KXBRENT15M: its spot makes sharp 8 bp moves that reverse, so a 2026-11-03 replay sold 8 times for 3 losers saved and 5 winners cut, -$17.9 vs holding at every cushion
    skip_varieties: tuple[str, ...] = ()  # varieties (variants.py letters) this lane does not buy: "G" on either side, "A-yes" on one side. Read at the decision from BiQuote against its window-open price, so commodities only; an unreadable one is not skipped. 2026-11-03 walk-forward on the lanes' live bets: the cells that paid in training were the same in three folds and left out G and A-yes, which then lost $49, $19 and $9 in the test period
    max_crossings: int = -1  # buy only when the market's mid crossed 50c at most this many times in the last crossings_seconds (a yield or price crossing its target), read from the quotes held in memory (ticks.MidHistory). 0 = a calm chart only; -1 = off. History too short to say = no buy. 2026-11-04: 88 treasury favorites with no crossing won 93%, those with two or more 74-83%
    crossings_seconds: float = 300.0
    skip_varieties_auto: bool = False  # also skip the varieties the in-memory ledger (variants.Ledger) shows losing right now for the asset class of the series: the last auto_hours of settled favorites, blended with the older ledger as a prior. The ledger is refreshed every few minutes by the watcher and read without a query; crypto reads its settlement index against the window strike
    auto_vol_gate: bool = False  # the volatility levels of this lane follow the recorded favorites: min_vol_rel / max_vol_rel are only the starting values, and every 12 hours the class level tuned from the last 7 days replaces them (volregime.tune_thresholds)
    disagree_stake_factor: float = 0.0  # with require_spot_center_agree: 0 = skip a bet where the spot and the corrected center disagree; above 0 = play it at this share of the stake. 2026-11-04: those 94 bets won 79% but at 0.78-0.90 prices they lose a little every band, so a quarter stake keeps the winners and cuts the loss (+$40.8 against +$54.4 for skipping)
    require_spot_center_agree: bool = False  # buy only when the raw spot AND the offset-corrected center are both on the bet's side of the strike. 2026-11-04, 672 live commodity bets: both on our side 88% won, +$0.24 a bet; where they disagreed 94 bets netted -$54 (industrial -$52 on 59, precious -$11 on 25; energy +$8 on 10, so energy leaves it off). Unreadable = no change
    min_vol_rel: float = 0.0  # buy only when the coin's volatility over the last 6 h is at least this many times its 72 h normal (volregime.py). 0 = off. 1.3 is the wild regime: favorites priced 0.82+ lose to their underdog there. No reading = no buy
    max_vol_rel: float = 0.0  # buy only when it is at most this (a calm regime: below 0.7 favorites won 87%). 0 = off. No reading = no buy
    skip_other_varieties: tuple[str, ...] = ()  # skip when the OTHER side (the favorite, for an underdog buy) is one of these varieties at the buy. "B" = far from the strike and flat: 2026-11-04 study of wild-regime underdogs, a far-and-flat favorite saw 1 underdog win in 42 (none on 5 of 6 days, none in the first half), the rest 30 in 157. Unreadable = no buy
    max_cushion_z: float = 99.0  # buy only when the bet's side has a cushion of at most this many typical moves (variants snapshot z, the bet's side: negative = the side is behind the strike). -1 = the other side leads by one or more. 99 = off. Unreadable = no buy
    min_cushion_z: float = -99.0  # 2026-11-05 (user): buy only when the bet's side has a cushion of at least this many typical moves left (variants.snapshot z). -99 = off. Commodity states since 11-02 (17,900 moments): cushion under 0.5 lost -$0.4 to -$1.45 a $10 bet in both halves, 1.0-1.5 made +$0.24, 1.5+ made +$0.26 to +$0.53 (studies/results/2026-11-05-entry-state-map.txt)
    ms_rule: tuple[str, ...] = ()  # 2026-11-05 (user): buy only when the variety at each named scale is one of the letters given: ("60m:D", "5m:F") = the 60-minute variety is D and the 5-minute variety is F (scales 1m 2m 5m 15m 60m). studies/results/2026-11-05-multiscale-varieties.txt. Unreadable = no buy
    record_buy: bool = False  # 2026-11-07 (user: the main strategy): buy any curve state whose own recorded result pays. The state is the 5-minute and 60-minute varieties read at the buy; the record (staterecord.py) is every settled market of the trailing days at this lane's entry window and price band, rebuilt daily. Needs a record of at least record_min per dollar staked behind at least record_min_n earlier markets
    record_min: float = 0.20  # the shrunk record, net per dollar staked after the fee, a state needs to be bought (studies/results/2026-11-07-state-record-walkforward.txt: 0.20 +3.39 per $10 ticket, 0.10 +2.01, 0.05 +0.66, 0.00 -0.67; chosen after the results)
    record_min_n: int = 15  # earlier markets a state needs at the buy's price band
    model_buy: bool = False  # 2026-11-07 (user: go live): buy a market side when the curve model (curvemodel.py: slope and variability of the spot's distance to the strike at 1, 2, 3, 5, 8, 13, 21, 34 and 55 minutes, fit daily on the trailing days) predicts a net per dollar in the top model_top share of its training moments and above zero. studies/results/2026-11-07-state-record-fibonacci.txt
    model_top: float = 0.20  # the share of training moments the lane buys: 0.05, 0.10, 0.20, 0.30 or 0.50 (the walk-forward: top 5% +1.53 per $10 ticket, 10% +1.06, 20% +0.71, every moment -0.34)
    model_category: str = ""  # 2026-11-09 (user: the model on an underdog lane): which fitted curve model this lane reads. "" is the shared one (curve_model.json); a name gives the lane's group its own model file (curve_model_<name>.json), fitted from the training moments of that group's lanes only, with its own buy bar. A lane that buys a different price range or window must have its own group: the shared bar is a quantile of the shared training moments
    model_features: str = "fibonacci"  # 2026-11-08 (user: switch to the curvature model): which readings the curve model fits and buys on. "fibonacci": slope and variability of the cushion at 1, 2, 3, 5, 8, 13, 21, 34, 55 minutes (18 readings). "curvature": a parabola over the last 5, 13 and 34 minutes, its level, slope and curvature (9 readings). studies/results/2026-11-08-curve-simplify.txt: at the best 5% the curvature set netted +0.008 +/- 0.024 per $1 against -0.044 +/- 0.024 for the Fibonacci one (both within noise). One daily refit fits the kind the model_buy lanes ask for; until it has, a lane that wants a different kind passes
    timing_gate: bool = False  # 2026-11-08 (user: the problem is when you buy it): sit out the moments (two-minute buckets of the 15-minute window) in which this lane's own settled bets of the last 7 days have a negative shrunk net per dollar staked over at least 8 bets (timerecord.py). Every model_buy lane logs the would-skip in shadow whether or not this is on; the live switch is the user's
    hot_gate: bool = False  # 2026-11-08 (user: go ahead after studies/results/2026-11-08-regime-sitout.txt): sit out while 5 or more of the nine crypto indexes run at 1.5x or more of their own normal volatility (hotcheck.py). Every model_buy lane logs the would-skip in shadow (hot_shadow.jsonl) whether or not this is on; the live switch is the user's
    min_model_edge: float = -99.0  # 2026-11-05 (user: it bought YES at 0.87 when its own model said 0.70): buy only when the model's chance the contract pays minus the ask is at least this (-0.05 = the model may sit up to 5 cents under the price). -99 = off. 697 live commodity bets since 11-02: model 5+ cents under the price won 84-85% and lost 3.8% / 1.0% of the stake (403 bets, -$60); within 5 cents or above won 89% and made +3.5% to +5.0% (294 bets, +$99)
    trend_down_max: float = 0.0  # 2026-11-05 (user: bank on a steady market-wide downtrend): when below 0, buy only the NO side and only while the five coins' average 60-minute move (variants.broad_trend_now) is at or under this percent (-1.0 = down 1% or more). 0 = off. Unreadable = no buy
    trend_up_min: float = 0.0  # the same for an uptrend: when above 0, buy only the YES side while that move is at or over this percent. 0 = off
    wave_amp_min: float = 0.0  # 2026-11-05 (user: apply the wave to the underdog): buy only when the bet side's price path over the last 3 minutes ranged at least this many typical minutes (variants.wave_features amp). 0 = off. 2 = the wild underdog setting: 19.2% won, +$3.85 a $10 bet on 292 setups against 12.7% and -$0.66 under it
    wave_pos_max: float = 1.0  # and only when the buy sits at or below this fraction of that range (0 = at the bet side's low swing). 1 = off. 0.5 = the lower half: 19.1% won against 13.4% in the upper half (studies/results/2026-11-05-swing-shape-wave.txt)
    pretrend_min: float = -99.0  # 2026-11-06 (user: the jumpy stretch is read by the trend before it): buy only when the bet side's spot drift over the pretrend_seconds ending pretrend_gap seconds before the buy is at least this many typical minutes (the coin's own 15-minute rms move per minute, from the in-memory ticks). -99 = off. 613 live crypto bets since 10-28: trend with us +$64.62 (+2.6%, n=314), the rest -$162.52 (studies/results/2026-11-06-pretrend.txt)
    shock_trend_hold: float = -99.0  # 2026-11-06 (user: when a market is spiky, read the hour line before the spike): the shock exit does not sell while the bet side's spot drift over the 420 s ending 90 s before now is at least this many typical minutes (the trend is with us). -99 = off. Commodity shock sales made with the trend sold $6.32 under holding (33 sales); with no trend they saved $10.22 (15)
    stake_price_full: float = 0.0  # 2026-11-06 (user: lower prices lose more, so scale the stake with the price): at or above this ask the stake is full; below it the stake falls in a straight line to stake_price_min_factor at the lane's lowest price. 0 = off (a flat stake at every price)
    stake_price_min_factor: float = 0.25
    pretrend_seconds: float = 420.0
    pretrend_gap: float = 90.0
    max_spot_crosses: int = 0  # buy only when the spot crossed the strike fewer than this many times in the last five minutes (4 = the oscillating variety G). 0 = off. Commodity states: 4+ crossings lost $0.59 a bet in both halves
    skip_markets_auto: bool = False  # also skip a market (gold, palladium, ETH...) or a market's price band that the ledger shows losing over auto_market_hours, judged against its asset class (variants.Ledger.market_skip). Varieties pool every market of a class; the markets differ
    auto_market_hours: float = 48.0  # the stretch a market is judged over: its quality moves slowly, so this is longer than auto_hours
    auto_market_prior: float = 30.0  # bets of the class mean a market's record is blended with
    auto_market_min_loss: float = 0.30  # dollars lost per $10 favorite (after the blend) before a whole market is skipped
    auto_market_min_n: int = 20  # favorites of the market needed in the stretch before it can be skipped
    auto_hours: float = 6.0  # the recent stretch the auto skip judges, in hours: the markets shift in 3-6 hour periods
    auto_prior_weight: float = 15.0  # how many bets of the older record the recent stretch is blended with (higher = slower to change its mind)
    auto_min_loss: float = 0.10  # dollars lost per $10 bet (after the blend) before a variety is skipped
    auto_min_n: int = 5  # recent favorites of the variety needed before it can be skipped
    choppy_floor: float = 0.0  # a choppy market: the favorites (the side with an ask in 0.78-0.92 from minute 7) of choppy_series settled in the last choppy_hours have won less often than this (regime.py). 0 = no choppy rules. While choppy the next three settings apply
    choppy_hours: float = 12.0
    choppy_series: tuple[str, ...] = ()  # whose favorites are counted; empty = this lane's own series. The crypto lanes count all five coins together: one coin's 2 hours alone is too few windows
    choppy_left_max: float = 0.0  # while choppy, buy only with this many seconds left or fewer (0 = no limit)
    choppy_price_min: float = 0.0  # while choppy, the ask has to be at least this
    choppy_skip: bool = False  # while choppy, buy nothing. 2026-11-03 replay, five coins over 9 days: skipping choppy windows beat buying in the last 300 s at 82c+ (+$253 against +$185)
    min_dwell_seconds: float = 0.0  # buy a side only after its price has been inside the lane's band this long (the tape's quotes). Buying the moment it crosses 70c lost 28% on 85 bets; waiting 30 s+ about 4% (0 = buy at once)
    entry_trend_seconds: float = 0.0  # buy only when the side's price (mid of the tape's bid and ask, in its own terms) has not fallen over the last this many seconds by more than entry_trend_min (0 = no check). A market that is falling when we would buy lost 16-18% over 7 days; one flat or rising lost 0-5%
    entry_trend_min: float = 0.0  # the least the side's price must have changed over entry_trend_seconds, in dollars (0.0 = not falling; 0.01 = up a cent)
    entry_spike_seconds: float = 0.0  # no buy on a spike (2026-11-02): the side's price (mid) has risen by entry_spike_rise or more over the last this many seconds, or its bid has swung by entry_spike_range or more inside them (0 = off). 242 settled live commodity bets in dollars: rise over 90 s of 30c+ -$44.5 on $149 (-30%), swing of 30c+ -$34.9 on $248, the rest +$73 on $1,289
    entry_spike_rise: float = 0.30  # dollars, the rise that counts as a spike
    entry_spike_range: float = 0.30  # dollars, the bid swing that counts as a spike
    entry_bounce_rise: float = 0.0  # no buy on a bounce (2026-11-02): the side's ask has risen by this much (dollars) over the last 30 s AND its bid had dipped by entry_bounce_dip or more from where it stood 90 s ago (0 = off). 170 settled live crypto bets in dollars: rise 10c+ with a dip 10c+ lost $39.8 on $218 (-18%), negative in both halves of the time
    entry_bounce_dip: float = 0.10  # dollars, the dip that makes the rise a bounce
    entry_spot_ticks: int = 0  # buy only when the UNDERLYING's last this many price changes (the spot feed in the tape: an up-and-down market's YES is up, NO is down) all moved the way the bet goes, up for YES and down for NO (0 = off). Judged by the coin, not by the contract's own quote, which trails it. No spot price in the last 30 s means no opinion
    entry_spot_seconds: float = 0.0  # the same idea by time: the underlying's net move over the last this many seconds must be in the bet's direction (0 = off)
    entry_trend_z: float | None = None  # the same check in the market's own units: the move over entry_trend_seconds divided by that market's typical move over the same span (the standard deviation of its changes over the previous 2 minutes) must be MORE than this (0 = the side must be gaining; negative allows a fall of that many typical moves). Replaces entry_trend_min when set; None = off
    stop_loss_at: float = 0.0  # this lane's own stop, as a fraction under the price paid (0 = the global STOP_LOSS_DROP)
    stop_confirm_seconds: float = 0.0  # the LONGEST the stop waits for a dipped bid to recover before selling (0 = at once); the actual wait is calculated from the time left — strategy.stop_wait, fitted in scripts/stop_wait_study.py
    stop_guard: bool = False  # True = the model's opinion may hold the stop back; the default is a hard stop that sells whatever the model thinks it is worth
    stop_model: bool = False  # a 15-minute lane's stop first asks the model about the window's shape and holds only when it values the position above the sale (STOP_MODEL_SECONDS wait; no answer = sell)
    settlement_guard: float = 0.0  # OFF: 377 live sales, 88% would have won, -8.3c vs holding. Same job as the burst exit, worse signal. The watcher still has the code; 0.10 would turn it back on
    burst_exit: bool = False  # watch held 15-minute bets for a volume burst against the holder in the last 120 s and SELL the favorite (watcher.py: that is the peak-crash exit); also log the other side as a paper bet in burst_flip. A silent book cannot 5x itself (BURST_MIN_NORMAL)
    burst_ratio: float = 0.0  # the burst EXIT as a multiple of the market's own normal: sell when the dollars bought against the holder in 3 s are at least this many times this market's normal 3 s (the median second of the 120 s before, both sides, times 3). A bar that grows in a busy market and shrinks in a quiet one. 0 = off (burst_dollars, the flat bar, applies). When set it replaces burst_dollars
    burst_min_dollars: float = 50.0  # ...and at least this many dollars, so a dead market's ordinary print is not a crash
    burst_dollars: float = 0.0  # the burst EXIT of a lane with burst_exit, in dollars: sell the held 90c+ favorite in the last 120 s when at least this many dollars are bought against it in 3 s (0 = the older multiple-of-normal rule). Per family from burst_exit_study: crypto 400, currencies 50, metals/energy 400
    burst_dollars_btc: float = 0.0  # the same for BTC: a bar of its own, or -1 to keep the older rule (it beat holding by 5.3c on BTC; the dollar bar did not)
    burst_favorite: float = 0.90  # the burst EXIT of a lane with burst_exit and burst_dollars sells the held side only while its bid is at least this (a favorite)
    drop_cents: float = 0.0  # a held favorite whose bid falls this many dollars within drop_seconds, in the burst window, is SOLD whatever the taker flow (0 = off). A book that thins gives a fall with almost no taker dollars, which the burst exit cannot see (KXCOPPER15M-26OCT301000: 0.85 -> 0.55 in four seconds, then 0). scripts/drop_exit_study.py: 0.25 in 3 s sold better than holding by about 10c a contract; 0.10-0.15 did not beat holding
    bounce_fall: float = 0.0  # sell into the first bounce (0 = off): once the held bid has fallen this far under the price paid and then bounces bounce_up off its low, sell, any time up to burst_min_left before the close (scripts/exit_tradeoff.py: ~half the losing dollars cut at about zero net cost, at the price of selling most positions)
    bounce_up: float = 0.04  # ...how far the bid must bounce off its low for the sale
    drop_seconds: float = 3.0  # ...measured against the highest bid of the last this many seconds
    drop_was: float = 0.76  # ...and only when the bid had been at least this in the last 60 s, so it is a favorite that is falling and not a long shot sliding
    drop_confirm: float = 0.0  # a fall only ARMS the exit; it sells when the bid is still drop_stay under the fall's high this many seconds later (0 = sell at once). A spike bounces back inside seconds, a flip does not (scripts/drop_flip_vs_spike_study.py: waiting 4-6 s lifted selling over holding from about 0 to +0.05 a contract on both halves of the data)
    drop_stay: float = 0.0  # ...how far under the high the bid must still be at the check (0 = drop_cents)
    drop_window: float = 120.0  # the exit watches the last this many seconds before the close (burst_min_left is the other end). Falls before the last three minutes did not pay in the replay
    burst_share: float = 0.0  # the burst exit also needs this share of the last 3 s of dollars to have been bought AGAINST the held side (0 = the dollar bar alone). A burst where the held side is being bought as hard is two-way trading or a pump of the side we hold, not a crash (scripts/burst_floor_study.py)
    burst_spot_edge: float = 0.0  # a burst sells only when spot's own chance that the held side pays is NOT more than this many points over what the sale gets (the bid less its fee); over that the dip is a wick in the price and it holds (0 = off, order flow alone decides). commodity: a one-second spike in the reference price crashed a 0.96 bid to 0.43 and sold a bet that went on to win
    burst_window: float = 120.0  # ...in the last this many seconds. Past that, holding still paid (burst_flip record: last 120 s sold +6 to +8c vs holding -5 to -8c; at 2-5 min holding was still ahead)
    burst_min_left: float = 10.0  # ...and not in the last this many (a sale sent later lands after the close)
    stop_from_seconds: float = 0.0  # the stop-loss acts only once this many seconds remain before the close (0 = whenever the hour rule allows). Replay of 6,299 favorites bets: holding through early dips until 3 minutes left helped commodity +1.2 and fx +1.1 points per dollar (within the error), cost crypto 1.1
    stop_loss: bool = True  # False = no stop-loss for this lane (a $1 ticket's loss is already capped; its spread alone can look like a 30% drop)
    take_profit_x: float = 0.0  # sell when the bid reaches this multiple of the price paid (0 = off); watched live on the socket
    take_profit_at: float = 0.0  # sell when the bid reaches this price (0 = off)
    cash_before_minute: float = 0.0  # a 15-minute buy opened before this minute of the window is cashed out early (0 = off; strategy.early_cash)
    cash_leave_minute: float = 9.0  # that buy is sold once the window reaches this minute, while the bid is still above the price paid
    cash_up_minute: float = 0.0  # once the window reaches this minute, sell if the bid is above the price paid (0 = off; strategy.cash_if_up)
    cash_drop: float = 0.0  # ...and sell when the bid is this fraction under the price paid (0.30 = down 30%). 0 = off. A smaller dip is left to the other exit
    cash_left_seconds: float = 0.0  # with this many seconds left, sell a hover that is still under the price paid (0 = off; strategy.cash_if_losing). A bid still above the price paid is held
    cash_at: float = 0.95  # or sooner, the moment the bid prints this
    cash_out_at: float = 0.0  # sell the moment the bid is at or under this price (0 = off): a cash-out line in dollars, so a bet that has collapsed returns what is left of it. Not a fraction of the price paid
    strike_through_sale: float = 0.0  # percent of the strike (0.003 = 0.003%). A cross this far through starts a watch: a return to the winning side is held to the close, a stay of 45s through sells at any time, and in the last strike_through_seconds a turn back down or a pause of half that window also sells. 0 = off
    strike_through_seconds: float = 60.0  # the faster bounce sale only looks this close to the close. A 45s stay through sells with more time left. 0 = off
    cross_take: float = 0.0  # 2026-11-05 (user): sell a gain once the bid is this many dollars over the price paid AND the index has crossed the strike cross_min times in this window; hold it otherwise (0 = off). 121 crypto gains the user sold early: with no crossing holding paid +7.3c a contract more, with 1-2 crossings selling paid +8.2c more
    cross_min: int = 1  # crossings of the strike the cross_take sale needs
    strike_through_rearm: bool = False  # after the index comes back to the winning side the watch starts over (a second cross gets its own stay clock) instead of holding the bet to the close. 2026-11-02 replay on the 5 Hz index, 200 live crypto_favorite bets: +$100.9 re-arming against +$84.3 holding after a return
    watch_at: float = 0.0  # a favorite is watched once the bid prints this (0 = off; the moonshot lanes leave it off). A downtrend that stays there is sold after watch_ride_seconds, scaled by the time left (strategy.watch_trend). A hover or a spike is held
    watch_patient_left: float = 420.0  # watch_ride_seconds is the wait when at least this many seconds remain; less time left shortens the wait in proportion
    watch_ride_seconds: float = 0.0  # how long a visit under watch_at may last, at watch_patient_left, before a downtrend is abandoned (0 = a downtrend sells as soon as the last 3 seconds say so)
    watch_loss: float = 0.0  # hard stop: sell when the bid has lost this fraction of the price paid (0.75 keeps about a quarter). 0 = off. Checked the whole 15-minute window, ahead of the 45% line
    stop_cross_gap: float = 0.0  # a Polymarket lane: do not sell on a Polymarket bid that is more than this many dollars under the Kalshi bid for the same side and market (0 = off). Polymarket prints a bid of exactly 0.15 on markets Kalshi has at 0.75: the stops sold healthy bets into it
    watch_loss_seconds: float = 3.0  # once that line prints, this many seconds before a sale. A print back above the line restarts it
    watch_loss_lift: float = 0.0  # a bounce this far off the low of that visit is held (0 = any price still under the line can sell once the seconds have passed)
    scalp_at: float = 0.0  # once the bid prints this price, follow the high (0 = off). A climb keeps riding (strategy.scalp_hit)
    scalp_up: float = 0.0  # or once the bid is this far above the price paid (0 = off). Whichever arm is set and reached first
    scalp_dip: float = 0.03  # sell when the bid is this far under that high...
    scalp_dip_hard: float = 0.0  # a fall this far under the high sells at once, without waiting scalp_dip_seconds: in a sharp reversal the bid keeps falling while the wait runs (0 = off). 6c was best in scripts/scalp_dip_study.py
    scalp_dip_seconds: float = 3.0  # ...and has stayed that far under it for this many seconds (0 = on the first print). A dip that recovers sooner is noise, not a reversal
    scalp_plateau_seconds: float = 0.0  # once armed, sell when the bid has made no new high for this many seconds (a plateau) and is still at or over the arm (0 = off)
    rest_sell: bool = False  # rest the take-profit sell on the book from the moment a live buy fills (post-only, so no taker fee) instead of having the watcher cross the book when the bid gets there; needs take_profit_x or take_profit_at. A stop or crash exit cancels it first and sells what is left
    trail_activate: float = 0.0  # peak-crash exit, when trail_progress is 0: once the bid has been this fraction above the price paid (0.10 = up 10%) ...
    trail_progress: float | None = None  # arm the crash exit once the bid has covered this share of the way from the price paid to $1.00; None = the global CRASH_PROGRESS, 0 = use trail_activate (a gain on the price paid) instead
    trail_spot_sigma: float | None = None  # the crash exit waits while spot is still this many sigma on the winning side of the strike; None = the global CRASH_SPOT_SIGMA, 0 = off
    trail_giveback: float | None = None  # the crash exit sells once this share of the gain (peak over price paid) is gone; None = the global CRASH_GIVEBACK, 0 = the fixed trail_drop instead
    trail_drop: float = 0.0  # ... sell when it falls this many dollars from its peak (0 = off; watched live on the socket only)
    trail_fast_drop: float = 0.0  # ... but sell at once, with no wait, when the bid is this far (dollars) below the peak: a gap down is not noise (0 = off)
    trail_confirm_seconds: float = 10.0  # ... and has stayed that far down this long (a dip of a second or two is noise)
    trail_hold_seconds: float = 3.0  # a bid counts toward the peak only once it has held this long (one thin-book tick is not a peak)
    spike_cents: float = 0.0  # sell, while in profit, when the bid has risen this much (dollars) within spike_seconds (0 = off)
    spike_seconds: float = 30.0
    spike_min_gain: float = 0.10  # a spike sells only when the bid is also this fraction above the price paid (0.10 = up 10%)
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
    entry_socket: bool = False  # two-level lanes: also re-review a watched event the moment a streamed quote puts a side in the price band (entrywatch.py)
    entry_approach: float = 0.0  # arm a side when its price is within this many dollars UNDER the lane's floor and gaining (entrywatch.py). The watch then rests a buy AT the floor; it is not a take of the ask (0 = off)
    entry_arm_seconds: float = 30.0  # the resting floor bid is canceled after this many seconds if it has not filled. An arm that never got its bid out is dropped then too, and may be armed again
    entry_move: float = 0.0  # a socket move trigger: when YES moves this many dollars within entry_move_seconds either way, rest a buy on the side that FELL (a YES fall buys YES, a YES rise buys NO). Needs entry_socket; the taker path is off for the lane (0 = off)
    entry_move_seconds: float = 3.0  # the span the move is measured over
    entry_move_below: float = 0.02  # the move buy rests this far under the fallen side's ask (post-only, no taker fee if it fills) for entry_arm_seconds
    late_steps: int = 0  # a late-window trend lane (entrywatch.py): the settlement index over minutes 9-12 is sampled in this many equal steps (odd, so no split vote); 0 = off
    late_agree: int = 0  # steps that must agree for a trend (0 = a bare majority). Fewer than this is "mixed": the lane passes
    late_min_z: float = 0.8  # the index's distance from the strike, in units of the coin's usual move over the time left (sigma over minutes 9-12 times the square root of seconds left), on the trend's side
    late_from_minute: float = 12.0  # the buy is the first time in this part of the window that the index sets a new high of the whole window (up trend, above the strike) or a new low (down trend, below it)
    late_to_minute: float = 14.0
    entry_min_gap_seconds: float = 2.0  # ... but not the same event more often than this
    buy_max_move: float = 0.0  # skip a buy when the order book's ask on the side has FALLEN more than this many dollars under the ask the decision was made at (0 = off). A job can wait many seconds behind the shared request budget between deciding and ordering; a Gold NO decided at 0.86 was bought at 0.24 eight seconds before the close, after the side had collapsed
    buy_retries: int = 0  # when an immediate buy fills nothing, read the book again and send it once more, up to this many times, right away (0 = no retry). The next signal on the market comes a median 108 s later and about 3.7c dearer
    buy_retry_chase: float = 0.0  # how many dollars over the first limit a retry may pay (0 = never above it). The lane's price band and the risk gate still apply
    buy_slip: float = 0.0  # the buy order is limited this many dollars above the ask the bot read: it still fills at the best ask, and pays up to the allowance only if the book has moved (0 = limit at the ask)
    buy_tick_ahead: bool = False  # 2026-11-08 (user: "just put the buy order at whatever the difference between the last 2 ticks is ahead of the current price"; the buys skipped with "$0.00 offered at 0.42" because the ask had run to 0.44): build the buy limit from the newest ask the quote stream shows, plus the size of the last tick up when the ask is rising (the last two distinct ask values), never under that ask, and never over the decision ask + buy_slip + buy_retry_chase. A falling ask is bought at its new, lower level. 0 = the old limit (the decision ask + buy_slip)
    watch_max: int = 40
    priority: int = 0  # queue priority of this lane's jobs; higher runs first
    trust: float = 1.0  # prior weight on the estimator before our record has any say (0 to 1)
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
        # Not scanned (disabled): worker.do_flip feeds it the opposite side of a market a stop-loss just sold, and the language model decides whether buying that side is worth it.
        "stop_flip": LaneSpec(
            "stop_flip", SHAPE, "stop_flip", (0.05, 0.95), (0.0, 1.5), 0.10, 0.0, min_edge=0.05, stop_loss=False, sides=("yes", "no"), enabled=False,
        ),
    }
    return lanes


def _overlay(base: LaneSpec | None, name: str, row: dict[str, Any]) -> LaneSpec:
    if "settlement_flip" in row:  # the burst exit's old name
        if "burst_exit" in row:
            raise ValueError(f"lane {name!r}: set burst_exit or its old name settlement_flip, not both")
        row = {("burst_exit" if k == "settlement_flip" else k): v for k, v in row.items()}
    known = {f.name for f in fields(LaneSpec)}
    unknown = set(row) - known
    if unknown:
        raise ValueError(f"lane {name!r}: unknown field(s) {sorted(unknown)}")
    row = dict(row)
    for key in ("price", "hours", "entry_elapsed", "value_exit_bid"):
        if key in row:
            row[key] = _pair(row[key])
    for key in ("series", "sides", "gate_from", "scale_from", "blackout_et", "choppy_series", "same_side_lanes", "wrong_side_varieties", "wrong_side_series", "skip_varieties", "skip_other_varieties", "shock_off_series", "ms_rule"):
        if key in row:
            row[key] = tuple(row[key])
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


def lane_specs_from(s: Settings, strategies_toml: str | None, overrides_json: str | None = None) -> dict[str, LaneSpec]:
    lanes = builtin_lanes(s)
    if strategies_toml:
        rows = tomllib.loads(strategies_toml).get("lane") or {}
        for name in rows:
            lanes[name] = _overlay(lanes.get(name), name, _inherited(rows, name))
    lanes = overrides.apply(lanes, overrides.parse(overrides_json))  # the admin page's changes, already checked against overrides.TUNABLES
    for spec in lanes.values():
        if tiered(spec) and not (spec.series or spec.needs_instrument):
            raise ValueError(
                f"lane {spec.name!r}: review_minutes needs `series` or `needs_instrument`, "
                "because a fast pass can only fetch what it can name"
            )
        if spec.late_steps and (spec.late_steps < 3 or spec.late_steps % 2 == 0 or spec.late_agree > spec.late_steps or not spec.entry_socket):
            raise ValueError(f"lane {spec.name!r}: late_steps must be an odd number of 3 or more, late_agree at most that, and entry_socket on")
        if spec.entry_socket and not tiered(spec):
            raise ValueError(f"lane {spec.name!r}: entry_socket needs review_minutes, because it re-runs that lane's review")
    if not s.paper_lanes and s.live_lanes is not None:
        # The live host runs only what is live. A switched-off lane keeps its spec, so a bet it left open is still stopped and sold.
        lanes = {name: spec if name in s.live_lanes or name in s.paper_named else replace(spec, enabled=False) for name, spec in lanes.items()}
    return lanes


@lru_cache(maxsize=8)
def _cached(s: Settings, text: str | None, changes: str | None) -> dict[str, LaneSpec]:
    return lane_specs_from(s, text, changes)


def strategies_path(data_dir: Path) -> Path:
    return data_dir / "strategies.toml"


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


def spec_for(s: Settings, name: str) -> LaneSpec:
    lanes = lane_specs(s)
    if name not in lanes:
        raise KeyError(f"unknown lane {name!r} (known: {', '.join(sorted(lanes))})")
    return lanes[name]


def stop_drop_for(spec: LaneSpec | None, s: Any) -> float:
    """How far under the price paid this lane's stop sits."""
    return spec.stop_loss_at if spec is not None and spec.stop_loss_at else s.stop_loss_drop


# Settings that only say how the bot goes about watching a lane, or how its record is read: changing one is not a change to what the lane does.
_NOT_LOGIC = frozenset({
    "name", "label", "enabled", "shortlist", "scope_minutes", "review_minutes", "priority", "watch_max", "cooldown_minutes", "shares_markets",
    "entry_socket", "entry_min_gap_seconds", "buy_rest_top_up", "curve_shadow", "confidence_shadow", "accumulate_shadow", "paper_fill_model", "buy_slip", "entry_move", "entry_move_seconds", "entry_move_below", "cash_out_at", "late_steps", "late_agree", "late_min_z", "late_from_minute", "late_to_minute", "scalp_plateau_seconds", "buy_max_move", "buy_retries", "buy_retry_chase", "buy_rest_then_take",
    "scale", "scale_from", "scale_since", "scale_min_n", "scale_z", "scale_kelly", "scale_max_mult", "scale_old_weight", "gate_since",
    "blackout_et", "max_same_side", "same_side_lanes", "shock_bp", "shock_source", "shock_units", "shock_margin_units", "cross_units", "shock_bid_max", "shock_seconds", "shock_margin_bp", "shock_last", "value_exit", "value_exit_bid", "value_exit_gain_x", "value_exit_high", "value_exit_high_left", "value_exit_min_gain", "entry_checks_exit", "rebuy_opposite_after_gain_seconds", "shock_memory", "shock_persist", "hour_drift_q", "perp_lead_buy", "curve_exit", "curve_exit_scales", "curve_exit_persist", "curve_exit_last", "curve_drop_units", "curve_drop_seconds", "curve_drop_margin_units", "curve_drop_persist", "curve_gap", "curve_gap_persist", "curve_behind_last", "late_drop_units", "late_drop_seconds", "shock_off_series", "shock_log_only", "wrong_side_bp", "wrong_side_seconds", "wrong_side_varieties", "wrong_side_last", "wrong_side_series", "skip_varieties", "choppy_floor", "choppy_hours", "choppy_series", "choppy_left_max", "choppy_price_min", "choppy_skip", "bounce_fall", "bounce_up", "burst_buy_pct", "burst_ratio", "burst_min_dollars", "drop_cents", "drop_seconds", "drop_was", "drop_confirm", "drop_stay", "drop_window", "drop_buy", "drop_buy_max", "hunt_contracts", "hunt_contracts_by", "hunt_sell_over", "hunt_buy_under", "strike_through_sale", "strike_through_seconds", "cross_take", "cross_min", "wave_amp_min", "wave_pos_max", "pretrend_min", "pretrend_seconds", "pretrend_gap", "shock_trend_hold", "stake_price_full", "stake_price_min_factor",  # reported by settings_of only when a lane sets one, so a lane without a blackout keeps the version it had
})


def settings_of(spec: LaneSpec, s: Any) -> dict[str, str]:
    """Everything that decides what this lane buys, how much, and how it gets out, including the global rules that reach it."""
    out = {f.name: repr(getattr(spec, f.name)) for f in fields(spec) if f.name not in _NOT_LOGIC}
    out["settlement_flip"] = out.pop("burst_exit")  # the old name, so no lane's version changes with the rename
    out["effective_stop"] = repr(stop_drop_for(spec, s) if spec.stop_loss else None)
    out["crash_spot_sigma"] = repr(spec.trail_spot_sigma if spec.trail_spot_sigma is not None else s.crash_spot_sigma)
    out["crash_giveback"] = repr(spec.trail_giveback if spec.trail_giveback is not None else s.crash_giveback)
    progress = spec.trail_progress if spec.trail_progress is not None else s.crash_progress
    out["crash_progress"] = repr(progress)
    if spec.trail_drop > 0 and progress > 0:
        out["trail_activate"] = repr(0.1)  # arming follows the progress rule, so a declared trail_activate is unused; held at the value every lane carried so existing versions stay continuous
    out["stops_on_15m"] = repr(s.stop_loss_fast)
    out["kelly_fraction"] = repr(None if spec.fixed_stake else s.kelly_fraction)
    if spec.blackout_et:
        out["blackout_et"] = repr(spec.blackout_et)
    if spec.max_same_side:
        out["max_same_side"] = repr(spec.max_same_side)
        if spec.same_side_lanes:
            out["same_side_lanes"] = repr(spec.same_side_lanes)
    if spec.shock_bp and not spec.shock_log_only:
        out["shock_bp"], out["shock_seconds"], out["shock_margin_bp"], out["shock_last"] = repr(spec.shock_bp), repr(spec.shock_seconds), repr(spec.shock_margin_bp), repr(spec.shock_last)
        if spec.shock_memory:
            out["shock_memory"] = repr(spec.shock_memory)
    if spec.shock_off_series:
        out["shock_off_series"] = repr(spec.shock_off_series)
    if spec.skip_varieties:
        out["skip_varieties"] = repr(spec.skip_varieties)
    if spec.wrong_side_bp:
        out["wrong_side_bp"], out["wrong_side_seconds"], out["wrong_side_varieties"] = repr(spec.wrong_side_bp), repr(spec.wrong_side_seconds), repr(spec.wrong_side_varieties)
    if spec.choppy_floor:
        out["choppy_floor"], out["choppy_hours"], out["choppy_series"] = repr(spec.choppy_floor), repr(spec.choppy_hours), repr(spec.choppy_series)
        out["choppy_left_max"], out["choppy_price_min"], out["choppy_skip"] = repr(spec.choppy_left_max), repr(spec.choppy_price_min), repr(spec.choppy_skip)
    if spec.drop_cents:
        out["drop_cents"], out["drop_seconds"], out["drop_was"] = repr(spec.drop_cents), repr(spec.drop_seconds), repr(spec.drop_was)
        if spec.drop_confirm or spec.drop_stay or spec.drop_window != 120.0:
            out["drop_confirm"], out["drop_stay"], out["drop_window"] = repr(spec.drop_confirm), repr(spec.drop_stay), repr(spec.drop_window)
    if spec.entry_move:
        out["entry_move"], out["entry_move_seconds"], out["entry_move_below"] = repr(spec.entry_move), repr(spec.entry_move_seconds), repr(spec.entry_move_below)
    if spec.cash_out_at:
        out["cash_out_at"] = repr(spec.cash_out_at)
    if spec.late_steps:
        out["late_steps"], out["late_agree"], out["late_min_z"] = repr(spec.late_steps), repr(spec.late_agree), repr(spec.late_min_z)
        out["late_from_minute"], out["late_to_minute"] = repr(spec.late_from_minute), repr(spec.late_to_minute)
    if spec.scalp_plateau_seconds:
        out["scalp_plateau_seconds"] = repr(spec.scalp_plateau_seconds)
    if spec.bounce_fall:
        out["bounce_fall"], out["bounce_up"] = repr(spec.bounce_fall), repr(spec.bounce_up)
    if spec.burst_ratio:
        out["burst_ratio"] = repr(spec.burst_ratio)
        out["burst_min_dollars"] = repr(spec.burst_min_dollars)
    if spec.strike_through_sale > 0:
        out["strike_through_sale"] = repr(spec.strike_through_sale)
        out["strike_through_seconds"] = repr(spec.strike_through_seconds)
        if spec.strike_through_rearm:
            out["strike_through_rearm"] = "true"
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


def takes_profit(spec: LaneSpec) -> bool:
    return spec.cash_out_at > 0 or spec.settlement_guard > 0 or spec.take_profit_x > 0 or spec.take_profit_at > 0 or spec.unstable_gain > 0 or spec.hover_dip > 0 or spec.trail_drop > 0 or spec.spike_cents > 0 or spec.scalp_at > 0 or spec.scalp_up > 0 or spec.cash_up_minute > 0 or spec.cash_drop > 0 or spec.cash_left_seconds > 0 or spec.drop_cents > 0 or spec.bounce_fall > 0 or spec.strike_through_sale > 0 or spec.shock_bp > 0 or spec.shock_units > 0 or spec.cross_units >= 0 or spec.wrong_side_bp > 0 or spec.cross_take > 0 or spec.value_exit > 0 or spec.curve_exit >= 0


def chance_ok(spec: LaneSpec, p: float) -> bool:
    return spec.min_chance - 1e-9 <= p <= spec.max_chance + 1e-9


def wants_bet(spec: LaneSpec, p: float, ask: float, edge: float) -> bool:
    """The lane's bet rule, from its parameters. An edge bar and a lag bar can both be set."""
    if spec.always:
        return True
    if spec.max_lag is not None and ask - p > spec.max_lag + 1e-9:
        return False
    if spec.min_edge is not None and edge < spec.min_edge:
        return False
    return spec.max_lag is not None or spec.min_edge is not None
