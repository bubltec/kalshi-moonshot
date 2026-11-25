"""The buy as a chain of named steps a lane puts in its own order (lane field buy_chain).

2026-11-17, user, on finding the ladder was the first thing every buy went through: "ladder should not be the entry point"; "the ladder is something that should come LAST in the chain. all it does is evaluate the worth of the bet, not what side to pick or whether it makes any sense"; "the risk score makes way more sense to go FIRST"; "you need to write new code that is modular and can fit in logic in any order".

A step is one question about one side of one market at one moment, answered yes or no with a sentence: is its price one the lane buys, does the risk score favour it, is its risk holding, is it worth more than it costs. A lane lists the steps it wants, in the order it wants them asked:

    buy_chain = ["risk_score", "price", "ladder"]

Both sides of the market start alive. Each step is asked of every side still alive, in the lane's order; a side that gets a no is out and is asked nothing more. A side that is still alive after the last step is one the lane buys. So the first step is the one that puts sides forward, and the last is the final word on the ones that got that far.

Where it runs:
  the review   (runner.lane_shots) asks the steps that need no model estimate, in order, up to the first one that does. The sides left are the lane's candidates; the estimator's own screen (the ladder's) is NOT run for a lane with a chain, so the ladder no longer decides which side is looked at.
  the decision (runner.evaluate) asks every step, in order, of both sides, with the estimate in hand. The candidate is bought when it is alive at the end, and the decision's note carries what each step said of each side.

The steps read the lane's existing settings (its price range, entry_trend_seconds, min_edge_room, buy_curve_edge_min); none has a number of its own here. A new step is one function with @step("name") and its name added to lanes.BUY_STEPS. A chain can be given a name and shared: a [module.<name>] table in strategies.toml holds `steps` (and any lane settings as its defaults), and a lane takes it with buy_module = "<name>" (lanes._with_module). Memory only: no step reads the database.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, NamedTuple

from .lanes import BUY_STEPS, LaneSpec, edge_verdict, wants_bet
from .strategy import LongShot, edge, fee_per_contract

log = logging.getLogger("moonshot")


class Check(NamedTuple):
    ok: bool
    why: str  # one short clause: what the step read and what it made of it


@dataclass
class Ctx:
    """What a step may read: the settings, the lane, the moment, and the model's chance that YES pays (None in the review, before any estimate is asked for)."""

    s: Any
    spec: LaneSpec
    p_yes: float | None = None
    now: float = field(default_factory=time.time)
    store: Any = None  # the books, at a decision only (the review has none): a step that counts the lane's open bets, or needs a window's strike the tick memory lacks
    why: str = ""  # the estimator's note on the market, at a decision only


Step = Callable[[Ctx, LongShot], Check]
STEPS: dict[str, Step] = {}
NEEDS_ESTIMATE: set[str] = set()


def step(name: str, needs_estimate: bool = False) -> Callable[[Step], Step]:
    def add(fn: Step) -> Step:
        STEPS[name] = fn
        if needs_estimate:
            NEEDS_ESTIMATE.add(name)
        return fn

    return add


@step("price")
def price_step(ctx: Ctx, shot: LongShot) -> Check:
    """The side's ask is inside the lane's price range, and the lane buys that side."""
    lo, hi = ctx.spec.price
    if shot.side not in ctx.spec.sides:
        return Check(False, f"the lane does not buy {shot.side.upper()}")
    if not lo <= shot.ask <= hi:
        return Check(False, f"ask {shot.ask:.2f} is outside the lane's {lo:g} to {hi:g}")
    return Check(True, f"ask {shot.ask:.2f}")


def _hours_left(ctx: Ctx, shot: LongShot) -> float:
    """Hours to the close as of the step's own moment (a shot's own figure is from when it was listed)."""
    from datetime import UTC, datetime

    from .strategy import _hours_until

    hours = _hours_until(shot.close_time, datetime.fromtimestamp(ctx.now, UTC)) if shot.close_time else None
    return shot.hours_to_close if hours is None else hours


@step("spread")
def spread_step(ctx: Ctx, shot: LongShot) -> Check:
    """The gap between the side's ask and its bid is at most the lane's max_spread. (A step since 2026-11-21, user: "I keep finding out about new things influencing the buys and sells that I didn't ask for": it was the lane filter's, applied before the module and listed nowhere in it.)"""
    gap = shot.ask - shot.bid
    if gap > ctx.spec.max_spread:
        return Check(False, f"spread {gap:.2f} (bid {shot.bid:.2f}, ask {shot.ask:.2f}) is over the lane's {ctx.spec.max_spread:g}")
    return Check(True, f"spread {gap:.2f}")


@step("entry_window")
def entry_window_step(ctx: Ctx, shot: LongShot) -> Check:
    """A fixed-length market is this many minutes old: inside the lane's entry_elapsed (minutes after the open). A market that is not one of the lane's fixed-length windows passes. (A step since 2026-11-21; it was the lane filter's.)"""
    spec = ctx.spec
    series = shot.series or shot.ticker.split("-")[0]
    if not (spec.timed_suffix and series.endswith(spec.timed_suffix)):
        return Check(True, "not a fixed-length window")
    elapsed = spec.timed_minutes - _hours_left(ctx, shot) * 60.0
    lo, hi = spec.entry_elapsed
    if not lo <= elapsed <= hi:
        return Check(False, f"{elapsed:.1f} min into a {spec.timed_minutes:g}-min window, outside the lane's {lo:g} to {hi:g}")
    return Check(True, f"{elapsed:.1f} min in")


@step("time_left")
def time_left_step(ctx: Ctx, shot: LongShot) -> Check:
    """The time to the close is inside the lane's `hours`. (A step since 2026-11-21; it was the lane filter's.)"""
    hours = _hours_left(ctx, shot)
    lo, hi = ctx.spec.hours
    if hours < lo:
        return Check(False, f"closes in {hours * 60:.1f} min, sooner than the lane's {lo * 60:g}")
    if hours > hi:
        return Check(False, f"closes in {hours * 60:.1f} min, further out than the lane's {hi * 60:g}")
    return Check(True, f"{hours * 60:.1f} min left")


@step("blackout")
def blackout_step(ctx: Ctx, shot: LongShot) -> Check:
    """The moment is outside the lane's blackout windows (blackout_et). (A step since 2026-11-21; it was the lane filter's.)"""
    from datetime import UTC, datetime

    from .lanes import in_blackout

    if in_blackout(ctx.spec, datetime.fromtimestamp(ctx.now, UTC), shot.series or shot.ticker.split("-")[0]):
        return Check(False, "blackout: the lane makes no new entries in this time window")
    return Check(True, "no blackout")


@step("volume")
def volume_step(ctx: Ctx, shot: LongShot) -> Check:
    """The market has traded at least the lane's min_volume. (A step since 2026-11-21; it was the lane filter's.)"""
    if shot.volume < ctx.spec.min_volume:
        return Check(False, f"volume {shot.volume:g} is under the lane's {ctx.spec.min_volume:g}")
    return Check(True, f"volume {shot.volume:g}")


@step("min_return")
def min_return_step(ctx: Ctx, shot: LongShot) -> Check:
    """A win pays at least the lane's min_return on the stake, after the fee. (A step since 2026-11-21; it was the lane filter's.)"""
    from .strategy import net_return

    pays = net_return(shot.ask)
    if ctx.spec.min_return and pays < ctx.spec.min_return:
        return Check(False, f"a win pays {pays:.0%}, under the lane's {ctx.spec.min_return:.0%}")
    return Check(True, f"a win pays {pays:.0%}")




@step("price_trend")
def price_trend_step(ctx: Ctx, shot: LongShot) -> Check:
    """The side's price has not moved against it over the lane's span (entry_trend_seconds): it gained at least entry_trend_min dollars (0 = no loss) since a quote that long ago. No quote that far back is a no."""
    from . import runner

    seconds, bar = ctx.spec.entry_trend_seconds, ctx.spec.entry_trend_min
    if seconds <= 0:
        return Check(False, "the lane sets no span (entry_trend_seconds)")
    moved = runner.side_trend(ctx.s, shot, seconds, ctx.now)
    if moved is None:
        return Check(False, f"no quote {seconds:.0f}s back")
    return Check(moved >= bar, f"price moved {100 * moved:+.1f}c over {seconds:.0f}s, needs {100 * bar:+.1f}c")


def kalshi_index(series: str, now: float) -> str | None:
    """The tape index of Kalshi's own price for the series' underlying (variants.kalshi_index): a coin's settlement index, a commodity's perp reference while it is ticking. None when only the outside feed could answer: a step that reads Kalshi's price does not read that."""
    from . import variants

    return variants.kalshi_index(series, now)


@step("perp_worth")
def perp_worth_step(ctx: Ctx, shot: LongShot) -> Check:
    """Kalshi's own price backs the side (2026-11-18, user, on the commodity lanes' gates: "these are model concepts, they shouldn't be lane options"). The reading is the side's chance from the Kalshi perp reference (variants.curve_edge_now, source "kalshi"). It needs no model estimate. No reading is a no.
    As a LEVEL (lane field perp_chance_quantile, 2026-11-21): the chance is at or over its own bar, that quantile of this series' own readings of the last day (perpchance.py; CLAUDE.md rule 2, no fixed number). The gap to the ask stays in the note with what the old rule would have said, so both are scored on the bets that follow. While the series' history is short there is no bar and the gap rule answers.
    As a GAP (perp_chance_quantile 0, the rule until 2026-11-21): the chance is no more than the lane's buy_curve_edge_min under the ask."""
    from . import perpchance, variants
    from . import ticks as ticks_mod

    floor, q = ctx.spec.buy_curve_edge_min, ctx.spec.perp_chance_quantile
    if floor <= -1.0 and q <= 0:
        return Check(False, "the lane sets no allowance (buy_curve_edge_min) and no level (perp_chance_quantile)")
    series = shot.series or shot.ticker.split("-")[0]
    if kalshi_index(series, ctx.now) is None:
        return Check(False, "Kalshi's perp reference is silent, so its chance cannot be read")
    ce = variants.curve_edge_now(series, shot.side, shot.hours_to_close * 3600.0, shot.ask, ticks_mod.strikes.get(shot.ticker) or _window_strike(ctx, shot), now=ctx.now, source="kalshi")
    if ce is None:
        return Check(False, "the perp's chance cannot be read (short history, a stale tick or no reference)")
    if q > 0:
        level, n = perpchance.bar(series, q, ctx.now)
        perpchance.note(series, shot.ticker, shot.side, ce[0], ctx.now)  # after the bar is read: a reading is judged against the ones before it
        if level is not None:
            old = "the old gap rule is off" if floor <= -1.0 else f"the old gap rule would {'pass' if ce[1] >= floor else 'refuse'} at {floor:+g}"
            ok = ce[0] >= level
            return Check(ok, f"the perp's chance {ce[0]:.3f} is {'at or over' if ok else 'under'} its own bar {level:.3f} (the {q:.0%} point of this market's {n} readings of the last day); against the ask {shot.ask:.2f} ({ce[1]:+.3f}, {old})")
        if floor <= -1.0:
            return Check(False, f"the perp's chance {ce[0]:.3f} has no bar yet ({n} readings of this market in memory) and the lane sets no gap allowance")
    short = f"; no level bar yet ({perpchance.bar(series, q, ctx.now)[1]} readings of this market in memory), so the gap rule answers" if q > 0 else ""
    if ce[1] < floor:
        return Check(False, f"the perp's chance {ce[0]:.3f} is {-ce[1]:.3f} under the ask {shot.ask:.2f}, the lane allows {-floor:g}{short}")
    return Check(True, f"the perp's chance {ce[0]:.3f} against the ask {shot.ask:.2f} ({ce[1]:+.3f}, the lane allows {floor:+g}){short}")


@step("calm")
def calm_step(ctx: Ctx, shot: LongShot) -> Check:
    """The market is inside its own recent range (CLAUDE.md rule 3; 2026-11-18, in place of the lane option max_vol_rel, a fixed 1.6 times normal per lane): no when the curve of Kalshi's own price is in the anomaly state, which is a path efficiency outside the 1st to 99th percentile of that market's own readings of the last hours, or a typical minute over three times its median (curveexit.state, the cutoffs redone hourly from the ticks in memory). No rule is assumed to hold there, so the side sits out and the note says what was read. It has no setting. A state that cannot be read yet is a no."""
    from . import curveexit

    series = shot.series or shot.ticker.split("-")[0]
    index = kalshi_index(series, ctx.now)
    if index is None:
        return Check(False, "Kalshi's perp reference is silent, so the market's state cannot be read")
    state, r, cuts = curveexit.state_now(index, ctx.now)
    if r is None or cuts is None:
        return Check(False, "the market's state cannot be read yet (under three hours of ticks in memory, or a stale one)")
    read = f"typical minute {r['unit'] / cuts['unit_med']:.2f}x its median, efficiency {r['eff']:.2f} (its own range {cuts['p01']:.2f} to {cuts['p99']:.2f})"
    if state == "anomaly":
        return Check(False, f"anomaly: {read}; no rule is assumed to hold, the side sits out")
    return Check(True, f"{state}: {read}")


def _window_strike(ctx: Ctx, shot: LongShot) -> float | None:
    """The market's target when the tick memory lacks it: the books', at a decision."""
    return ctx.store.window_strike(shot.ticker) if ctx.store is not None else None


@step("vol_regime")
def vol_regime_step(ctx: Ctx, shot: LongShot) -> Check:
    """The market's volatility over the last hours is not more than max_vol_rel times its own normal (volregime.py). A market whose regime is not known yet is a no."""
    from . import variants, volregime

    hi = ctx.spec.max_vol_rel
    if hi <= 0:
        return Check(False, "the lane sets no ceiling (max_vol_rel)")
    coin = variants.coin_of(shot.series)
    vrel = volregime.regime.rel(coin)
    if vrel is None:
        return Check(False, f"volatility regime unknown for {coin}")
    return Check(vrel <= hi, f"volatility {vrel:.2f}x its normal, the lane allows {hi:g}")


@step("cushion")
def cushion_step(ctx: Ctx, shot: LongShot) -> Check:
    """The spot's distance to the strike, in the market's own typical moves, is at least min_cushion_z (variants.snapshot_now, on Kalshi's own price). A cushion that cannot be read is a no. The note also carries how many times the spot crossed the strike in five minutes and the side's variety letter (variants.py), as readings only: the limit on crossings (max_spot_crosses) and the variety step with its skip list (skip_varieties) were removed 2026-11-21 at the user's word; no curve shape is refused."""
    from . import ticks as ticks_mod
    from . import variants

    spec = ctx.spec
    if spec.min_cushion_z <= -90:
        return Check(False, "the lane sets no cushion (min_cushion_z)")
    snap = variants.snapshot_now(ctx.s, shot.series, shot.side, shot.hours_to_close * 3600.0, now=ctx.now, strike=ticks_mod.strikes.get(shot.ticker) or _window_strike(ctx, shot))
    if snap is None:
        return Check(False, "the cushion cannot be read")
    if snap["z"] < spec.min_cushion_z:
        return Check(False, f"cushion z {snap['z']:+.2f} is under the lane's {spec.min_cushion_z:g}")
    return Check(True, f"cushion z {snap['z']:+.2f}, {int(snap['crosses'])} crossings, variety {snap.get('variety') or '?'}")


@step("spike")
def spike_step(ctx: Ctx, shot: LongShot) -> Check:
    """The side's price has not just run: over the last entry_spike_seconds it rose less than entry_spike_rise and its bid swung less than entry_spike_range (dollars). A span with no quotes is not a spike."""
    from . import runner

    seconds = ctx.spec.entry_spike_seconds
    if seconds <= 0:
        return Check(False, "the lane sets no span (entry_spike_seconds)")
    rose, swung = runner.side_spike(ctx.s, shot, seconds, ctx.now)
    if (rose is not None and rose >= ctx.spec.entry_spike_rise) or (swung is not None and swung >= ctx.spec.entry_spike_range):
        return Check(False, f"price rose {100 * (rose or 0):+.0f}c and its bid swung {100 * (swung or 0):.0f}c over {seconds:.0f}s (the lane skips {100 * ctx.spec.entry_spike_rise:.0f}c / {100 * ctx.spec.entry_spike_range:.0f}c)")
    return Check(True, f"no spike over {seconds:.0f}s")


@step("center_agree", needs_estimate=True)
def center_agree_step(ctx: Ctx, shot: LongShot) -> Check:
    """The ladder's spot and its corrected center both sit on the side's side of the strike (commodities.parse_spot_center on the estimator's note). A note with no such pair, or a market with no strike, is not held to it."""
    from . import commodities
    from . import ticks as ticks_mod

    if not ctx.spec.require_spot_center_agree:
        return Check(False, "the lane does not ask the spot and the center to agree (require_spot_center_agree)")
    pair = commodities.parse_spot_center(ctx.why)
    strike = ticks_mod.strikes.get(shot.ticker) or _window_strike(ctx, shot)
    if pair is None or not strike:
        return Check(True, "no spot and center pair to compare")
    side_yes = shot.side == "yes"
    spot_on, center_on = ((x >= strike) if side_yes else (x < strike) for x in pair)
    if not (spot_on and center_on):
        return Check(False, f"the spot {pair[0]:.6g} and the corrected center {pair[1]:.6g} do not both sit on the {shot.side.upper()} side of the {strike:.6g} strike")
    return Check(True, f"spot {pair[0]:.6g} and center {pair[1]:.6g} agree on {shot.side.upper()}")


@step("same_side", needs_estimate=True)
def same_side_step(ctx: Ctx, shot: LongShot) -> Check:
    """The lane does not already hold max_same_side bets on this side in this window (the part of the ticker after the series): a cap on how many of its bets can lose together."""
    cap = ctx.spec.max_same_side
    if cap <= 0:
        return Check(False, "the lane sets no cap (max_same_side)")
    if ctx.store is None or shot.ticker.count("-") < 2:
        return Check(True, "no bets to count")
    window = shot.ticker.split("-")[1]
    same = sum(1 for r in ctx.store.open_bets() if (r["lane"] or "moonshot") == shot.lane and r["side"] == shot.side and r["ticker"] != shot.ticker and r["ticker"].count("-") >= 2 and r["ticker"].split("-")[1] == window)
    return Check(same < cap, f"{same} of the lane's bets already {shot.side.upper()} in this window, the lane holds at most {cap}")


def _hold_value(ticker: str, shot: LongShot, now: float) -> float | None:
    """What an open bet on `ticker` (the shot's side, in the shot's own window, so the same time left) is worth HELD to the close: 1 - the chance the spot's 60 s average ends on the wrong side, from its cushion over this market's target in the spot's own typical minute (wavetop.spot_read, wavetop.p_lose: the sell side's own reading). None when the spot, its typical minute or the target is not in the tick memory. Memory only."""
    from . import binaryrisk, ticks, wavetop

    try:
        index = binaryrisk.curve_index(ticker.split("-")[0])
        strike = ticks.strikes.get(ticker)
        if not index or not strike:
            return None
        got = ticks.history.window(index, now - 620.0, now)
        if not got or not got[0]:
            return None
        read = wavetop.spot_read(got[0], got[1], now, float(strike), shot.side)
        if read is None:
            return None
        return 1.0 - wavetop.p_lose(read[0], read[1], max(shot.hours_to_close * 3600.0, 1.0))
    except Exception:  # a reading that fails is no cushion: the bid decides
        return None


@step("side_losing", needs_estimate=True)
def side_losing_step(ctx: Ctx, shot: LongShot) -> Check:
    """The lane does not buy more of a side while its open bets on that side in this window are losing at their current bids (2026-11-24, user, after the 22:15 round where four NO bets in four markets all lost together: "we need a rule where it stops buying more of the same side when the current bids are losing"). Each open bet of this lane on the same side in the same window (the part of the ticker after the series), in another market, is marked at its side's bid now: bid less the sale fee, against the price paid with its buy fee, times its contracts. When those bets stand under what they cost by more than the lane's side_losing_share (2026-11-24, user: "it needs to be losing by like a decent percentage, like 20% or something"; 0.2 in the priskp module, my reading of 'like 20%') the buy is refused; the first bet of a round (nothing open on the side) and a round whose bets are ahead go through. No number: the sign of what the lane already holds. A bet with no quote in memory yet is not counted. Reads the bet book and the in-memory quotes only, so it is asked at the decision."""
    if ctx.store is None or shot.ticker.count("-") < 2:
        return Check(True, "no bets to count")
    from . import ticks

    window = shot.ticker.split("-")[1]
    total, spent, n, unread, worst, worst_tk = 0.0, 0.0, 0, 0, 0.0, ""
    for r in ctx.store.open_bets():
        tk = r["ticker"]
        if (r["lane"] or "moonshot") != shot.lane or r["side"] != shot.side or tk == shot.ticker or tk.count("-") < 2 or tk.split("-")[1] != window:
            continue
        rows = ticks.mids.quotes(tk)
        contracts = float(r["contracts"] or 0.0)
        if not rows or contracts <= 0:
            unread += 1
            continue
        yes_bid, yes_ask = rows[-1][1], rows[-1][2]
        bid = yes_bid if shot.side == "yes" else 1.0 - yes_ask
        paid = float(r["stake_usd"] or 0.0) / contracts
        worth = bid - fee_per_contract(bid)  # what a sale at the bid pays now, after its fee
        hold = _hold_value(tk, shot, ctx.now)  # 2026-11-25, user, on a BNB bet with a minor dip and plenty of cushion: "side_losing needs to account for cushion also, add cushion to the value in some pricing sense": the bet is worth the better of selling it and holding it on the spot's cushion
        if hold is not None and hold > worth:
            worth = hold
        net_each = worth - paid - fee_per_contract(paid)
        total += net_each * contracts
        spent += (paid + fee_per_contract(paid)) * contracts
        n += 1
        try:
            entry_bid = float(r["bid"] or 0.0)  # the bet side's bid when the bet was bought (the decision's own quote)
        except (KeyError, IndexError, TypeError, ValueError):
            entry_bid = 0.0
        baseline = entry_bid - fee_per_contract(entry_bid) if 0.0 < entry_bid < 1.0 else paid + fee_per_contract(paid)  # 2026-11-25, user: "i think we need to fix that gap": a bet is losing by how far it has MOVED since it was bought (what a sale would have paid then against its worth now), so the spread and the fees paid at the buy do not count; with no bid stored the cost is the baseline as before
        under = max(0.0, (baseline - worth) / (paid + fee_per_contract(paid)))
        if under > worst:
            worst, worst_tk = under, tk
    if n == 0:
        return Check(True, "no other bet of the lane on this side in this window" + (f" with a quote ({unread} unread)" if unread else ""))
    share = ctx.spec.side_losing_share
    ok = worst <= share + 1e-12  # ANY one of them under the share blocks, not their total
    return Check(ok, f"the lane's {n} other {shot.side.upper()} bet(s) in this window stand {total:+.2f} dollars now (each valued at the better of its bid after the fee and its hold value on the spot's cushion); the worst, {worst_tk.split('-')[0] if worst_tk else 'none'}, is {worst:.0%} under where it stood when bought" + ("" if ok else f", past the {share:.0%} the lane allows: no more of the side"))


_CONFIRM_FIRST: dict[tuple[str, str], list[float]] = {}  # (ticker, side) -> [when the lane first would have bought, the spot then, the spot's typical minute then]


@step("settle_confirm", needs_estimate=True)
def settle_confirm_step(ctx: Ctx, shot: LongShot) -> Check:
    """Buy only once the spot has CONFIRMED the bet's direction, in its own units, with no clock (2026-11-24, user, on the 22:15 NO bets and ZEC 23:00 NO, all bought in a chop and lost: "if we don't see a direction yet we shouldn't bet yet"; on a fixed 2-minute wait: "what's the point in adding a fixed wait if there's no logic to it. That's just a mutation of the time lapsed"; "Let's just go live with it"; scripts/priskp_confirm_study.py, 106 bets, one day, held to settlement: 26 bets +$33.00 against the fixed wait's 23 bets +$18.98, median wait 68 s). Put it LAST in a module's list: it starts the first time every other step would have bought, and notes the spot and its typical minute (the hour before, priskp.typical_minute) then. From then on the buy goes through at the first tick where the spot has moved the bet's way (a rise for YES, a fall for NO) by at least the lane's settle_confirm_units typical minutes since that moment; until then it is a wait, not a pass: the entry thread asks again on every tick. A calm market confirms at once, a chop takes long, a reversal never confirms (the lane's own minute limit ends it). The hold is per market and side and lasts until it is bought or the market ends. Memory only (the in-memory index ticks)."""
    units = ctx.spec.settle_confirm_units
    if units <= 0:
        return Check(False, "the lane sets no confirmation (settle_confirm_units)")
    from . import binaryrisk, priskp, ticks

    index = binaryrisk.curve_index(shot.series)
    last = ticks.history.last_at_or_before(index, ctx.now) if index else None
    if last is None or ctx.now - last[0] > 30.0:
        return Check(False, "no fresh spot reading to confirm a direction with")
    spot = float(last[1])
    key = (shot.ticker, shot.side)
    got = _CONFIRM_FIRST.get(key)
    if got is None:
        win = ticks.history.window(index, ctx.now - 3600.0, ctx.now)
        unit = priskp.typical_minute(win[0], win[1], ctx.now, 3600.0) if win and win[0] else 0.0
        if unit <= 0:
            return Check(False, "the spot's typical minute cannot be read yet")
        got = _CONFIRM_FIRST[key] = [ctx.now, spot, unit]
        if len(_CONFIRM_FIRST) > 2048:
            for k in [k for k, v in _CONFIRM_FIRST.items() if ctx.now - v[0] > 3600.0]:
                del _CONFIRM_FIRST[k]
    moved = (spot - got[1]) if shot.side == "yes" else (got[1] - spot)
    need = units * got[2]
    waited = ctx.now - got[0]
    if moved >= need:
        return Check(True, f"the spot confirmed the bet's way after {waited:.0f}s: moved {moved:g} since {got[1]:g}, over the {need:g} ({units:g} typical minutes of {got[2]:g}) it needed")
    return Check(False, f"waiting for the spot to confirm: it has moved {moved:+g} the bet's way since {got[1]:g} ({waited:.0f}s ago) and needs {need:g} ({units:g} typical minutes of {got[2]:g})")


@step("wave_leg_shadow")
def wave_leg_shadow_step(ctx: Ctx, shot: LongShot) -> Check:
    """SHADOW ONLY, never says no (2026-11-25, user, after the 08:30 round where BTC, ETH, HYPE and BNB NO favourites were bought inside a dip: "why can't it walk the curve back and identify it was inside a dip?"; "yes" to a shadow build): walks the spot back from now to the window's open with the wave lane's zig-zag (wavefacts.leg) and writes into the decision's note which leg the bet's side is in (inside it, not turned; turned; against the current swing; none), the swing and its give-back in typical minutes, the 30 minute R^2 and whether the rule of scripts/priskp_leg_study.py would REFUSE (the swing runs against the bet, or the bet is inside an unturned leg in a wavy market; a trend's leg is never refused). Changes nothing the lane buys; scored later against the bets that follow."""
    from . import binaryrisk, ticks, wavefacts

    try:
        series = shot.series or shot.ticker.split("-")[0]
        index = binaryrisk.curve_index(series)
        if not index:
            return Check(True, "shadow: leg: no index is read for this series")
        opened = round((ctx.now + shot.hours_to_close * 3600.0) / 900.0) * 900.0 - 900.0
        strike = ticks.strikes.get(shot.ticker) or _window_strike(ctx, shot)
        if not strike:
            return Check(True, "shadow: leg: the market's target is not in memory yet")
        got = wavefacts.leg(index, opened, ctx.now, float(strike), shot.side, max(opened + 900.0 - ctx.now, 1.0))
        if got is None:
            return Check(True, "shadow: leg: unread (the hour before the open or the path since it is not in memory)")
        r2 = "n/a" if got["r2"] is None else f"{got['r2']:.2f}"
        told = f"{got['class']} (swing {got['swing_u']:.2f} typical minutes, gave back {got['gb_u']:.2f}, 30 minute R^2 {r2}, a trend at or over {wavefacts.R2_CUT:g})"
        return Check(True, f"shadow: would {'REFUSE' if got['would_refuse'] else 'buy'}: the {shot.side.upper()} is {told}")
    except Exception as exc:  # a shadow reading that fails must never stop a buy
        return Check(True, f"shadow: leg unreadable ({exc!r})")


@step("cooldown", needs_estimate=True)
def cooldown_step(ctx: Ctx, shot: LongShot) -> Check:
    """The lane does not buy back too soon what it just sold in this market (2026-11-19, user: "put both into one buy step, cooldown"; the rebuy wait and the reentry cooldown were lane options the cleanup took out, "should be part of buy logic"). After a sale FOR A GAIN the same side waits rebuy_opposite_after_gain_seconds (2026-11-08, HYPE 09:15: sold YES at 0.67 for a gain, bought YES again 94 s later at 0.67 and lost $2.22); the other side goes straight through the other steps, so a rebuy is usually the opposite end. After a sale at a loss, or when the lane sets no rebuy wait, the whole market (either side) waits reentry_cooldown_seconds. A lane sets either or both (in its buy module); a gain sale on a lane with a rebuy wait is not held to the reentry cooldown. Reads the bet book in memory (Store.last_exit_ts, Store.last_gain_sale), so it is asked at the decision only."""
    reentry, rebuy = ctx.spec.reentry_cooldown_seconds, ctx.spec.rebuy_opposite_after_gain_seconds
    if reentry <= 0 and rebuy <= 0:
        return Check(False, "the lane sets no wait (reentry_cooldown_seconds, rebuy_opposite_after_gain_seconds)")
    if ctx.store is None:
        return Check(True, "no books to read")
    last = ctx.store.last_exit_ts(shot.ticker, shot.lane)
    if last is None:
        return Check(True, "not sold in this lane lately")
    gain = ctx.store.last_gain_sale(shot.ticker, shot.lane) if rebuy > 0 else None
    if gain is not None and gain[1] >= last - 1e-6:  # the latest sale of this market was for a gain
        if shot.side != gain[0]:
            return Check(True, f"the lane sold {gain[0].upper()} of this market for a gain: the other side goes through")
        wait = gain[1] + rebuy - ctx.now
        if wait > 0:
            return Check(False, f"this lane sold {shot.side.upper()} of {shot.ticker} for a gain {rebuy - wait:.0f}s ago; only the other side may be bought for {wait:.0f}s more")
        return Check(True, f"this side was sold for a gain {rebuy - wait:.0f}s ago, past the {rebuy:.0f}s wait")
    if reentry > 0:
        wait = last + reentry - ctx.now
        if wait > 0:
            return Check(False, f"this lane sold {shot.ticker} {reentry - wait:.0f}s ago and waits {reentry:.0f}s before buying it again")
    return Check(True, "not sold in this lane lately")


@step("state_record", needs_estimate=True)
def state_record_step(ctx: Ctx, shot: LongShot) -> Check:
    """CLAUDE.md rule 8: the side's curve state (varieties at several scales) paid on its own record: its shrunk net per dollar staked at the buy's price band is at least record_min over at least record_min_n earlier markets (staterecord.py). No record built yet, or an unreadable state, is a no."""
    from . import staterecord

    ok, why = staterecord.admits(ctx.s, ctx.store, shot, ctx.spec, shot.ask)
    return Check(ok, why)


@step("state_record_shadow", needs_estimate=True)
def state_record_shadow_step(ctx: Ctx, shot: LongShot) -> Check:
    """SHADOW, never says no (2026-11-19, user: "why can't it be on the same market as a shadow feature?"): the state record (rule 8) is asked of every side that got through the lane's own steps, and its verdict goes in the decision's note as "would buy" or "would not buy" with the state and its recorded net per dollar. The bet is scored later by that verdict, held and as sold, on the same bets. It changes nothing the lane does."""
    from . import staterecord

    try:
        ok, why = staterecord.admits(ctx.s, ctx.store, shot, ctx.spec, shot.ask)
    except Exception as exc:  # a shadow reading that fails must never stop a buy
        return Check(True, f"shadow: state record unreadable ({exc})")
    return Check(True, f"shadow: {'would buy' if ok else 'would not buy'}: {why}")


@step("curve_model", needs_estimate=True)
def curve_model_step(ctx: Ctx, shot: LongShot) -> Check:
    """The curve model (curvemodel.py: the spot's curve at nine Fibonacci look-backs, fitted daily on the tape) predicts a net per dollar for the side that is in the top model_top share of its training moments and over zero. No fitted model, or a curve that cannot be read, is a no."""
    from . import curvemodel

    ok, why = curvemodel.admits(ctx.s, ctx.store, shot, ctx.spec, shot.ask)
    return Check(ok, why)




@step("win_chance_shadow")
def win_chance_shadow_step(ctx: Ctx, shot: LongShot) -> Check:
    """SHADOW, never says no (2026-11-21, user via parent): the price-free win-chance score (binaryrisk.win_read: a logistic fit of the side winning at settlement on the curve readings alone, read on every gated tick) goes in the decision's note with the readings it saw. No ask, edge or net is in it or in the ticks it reads; value (the score against the price) is a later, separate step. Reads memory only. It changes nothing the lane does."""
    from . import binaryrisk

    try:
        pw, why = binaryrisk.win_read(ctx.s, shot)
    except Exception as exc:  # a shadow reading that fails must never stop a buy
        return Check(True, f"shadow: win chance unreadable ({exc})")
    return Check(True, f"shadow: {why}" if pw is not None else f"shadow: no win chance ({why})")




@step("priskp")
def priskp_step(ctx: Ctx, shot: LongShot) -> Check:
    """The PriskP checks model (priskp.py; 2026-11-22, user: separate yes/no checks, each against the market's own history, no single score): the five checks read and passing with the cushion in its top fifth, held three readings in a row, inside minutes 2 to 14, never an ask of 0.45 to 0.55, and not the side a confirmed shift took out of its cushion. A take at the ask. The note is the step's answer as properties (verb, why, the failing checks, each check's fifth). Memory only."""
    from . import priskp

    try:
        ok, why = priskp.admit(ctx.s, shot, ctx.now, _hours_left(ctx, shot))
    except Exception as exc:
        return Check(False, f"priskp unreadable ({exc})")
    return Check(ok, why)




@step("wave_trend")
def wave_trend_step(ctx: Ctx, shot: LongShot) -> Check:
    """SHADOW ONLY, never says no (wavetrend.py; 2026-11-22, user: the trend "shouldn't be a hard NO"; "Ok let's try that"): writes the trend over 1, 3, 12 and 24 hours before the open, signed for this bet, and what the 24 h trend would move the bet's worth by, into the decision's note. Changes nothing the lane buys; scored later against the bets that follow."""
    from . import binaryrisk, wavetrend

    try:
        index = binaryrisk.curve_index(shot.series or shot.ticker.split("-")[0])
        if not index:
            return Check(True, "trend: no index is read for this series (shadow)")
        opened = round((ctx.now + shot.hours_to_close * 3600.0) / 900.0) * 900.0 - 900.0
        return Check(True, wavetrend.note(index, opened, ctx.now, shot.side))
    except Exception as exc:
        return Check(True, f"trend unreadable ({exc!r}) (shadow)")


@step("wave_top")
def wave_top_step(ctx: Ctx, shot: LongShot) -> Check:
    """The wave top (wavetop.py; 2026-11-21 the user's hand scalp, rebuilt 2026-11-22 on the 60-second average against the target): the average has risen (fallen) faster than the earlier rises (falls), the whole leg stayed above (below) the market's target, the raw spot has just crossed to the other side of its average and the average has not yet come off its extreme: a peak (dip) whose flip is starting. This side is NO on a peak and YES on a dip. A dip bet after a rising hour is not taken (wave_top_trend_hours). Memory only (the tick history and the target in memory)."""
    from . import ticks as ticks_mod
    from . import wavetop

    strike = ticks_mod.strikes.get(shot.ticker) or _window_strike(ctx, shot)
    ok, why = wavetop.admits(ctx.s, shot, ctx.spec, ctx.now, strike)
    return Check(ok, why)


@step("wave_flag")
def wave_flag_step(ctx: Ctx, shot: LongShot) -> Check:
    """The wave flag (waveflag.py; 2026-11-23, a candidate beside wave_top, from scripts/wave_flag_lab.py): a leg of the raw spot, confirmed when the spot comes off its extreme by max(wave_flag_turn x the swing, sigma10), is significant when its swing is at least wave_flag_z x sigma10 x sqrt(max(duration, 5) / 10) (sigma10 = the spot's rms 10 s change over the hour before the open); the flag is that confirmation when at least wave_flag_legs significant legs came in the last wave_flag_window seconds and the whole leg stayed on one side of the target. NO after a peak over the target, YES after a dip under it (the target on the curve's scale: a coin's strike, a commodity's perp value at the open, waveflag.target). The flag stays open for the entry thread's look gap (entry_min_gap_seconds) after its confirmation. Price band and entry window are the lane's own steps. Memory only (the tick history and the target in memory)."""
    from . import ticks as ticks_mod
    from . import waveflag

    strike = ticks_mod.strikes.get(shot.ticker) or _window_strike(ctx, shot)
    ok, why = waveflag.admits(ctx.s, shot, ctx.spec, ctx.now, strike)
    return Check(ok, why)


@step("wave_shape")
def wave_shape_step(ctx: Ctx, shot: LongShot) -> Check:
    """The wave shape (waveshape.py; 2026-11-24, PAPER, in place of wave_top and wave_flag: the classifier the study wrote against the user's chart labels, scripts/wave_buy_shape_classifier.py): on the broad curve of Kalshi's own spot (a median over 60 s / rho^2, 30 to 240 s), bought AT the turn (the broad extreme at most 0.3 of that window ago, at most 0.10 of the swing given back) with a base within one typical minute of the target on the extreme's side, and either a repeated swing (R: an earlier same-way swing 0.75-1.35x this one that came back, bases close, the swing at least 0.4 of the market's range since the open) or one wide swing (S: the market's extreme since the open, at least 0.3 of the hour's broad range, made at 0.6 typical minutes a minute or faster). NO at a peak over the target, YES at a trough under it. No price band. The note carries the branch, the swing, W, rho, the base's distance and the turn's age. Memory only (the tick history and the target in memory)."""
    from . import ticks as ticks_mod
    from . import waveshape

    strike = ticks_mod.strikes.get(shot.ticker) or _window_strike(ctx, shot)
    ok, why = waveshape.admits(ctx.s, shot, ctx.now, strike)
    return Check(ok, why)


@step("wave_turn")
def wave_turn_step(ctx: Ctx, shot: LongShot) -> Check:
    """The wave turn (waveturn.py; 2026-11-24, PAPER, in place of wave_shape on the wave_shape lane: the rule of the study on the user's own marked buys, scripts/wave_turn_chop.py): on Kalshi's own spot smoothed over 30 s, a swing of at least 0.5 typical minutes (u, from the hour before the open) is bought once 0.2 u has come back from its extreme (NO after a rise, YES after a fall), once per swing, NO only when the curve is not more than 0.5 u under the target and YES only when not more than 0.5 u over it; and only in a market whose pre-open hour crossed each 15-minute window's opening price at least 8 times on average (marketprofile.profile_from; an hour not in memory is no reading and no buy). The note carries the gate's reading and, as shadow, the hour's efficiency, waves so far, the last-5-min efficiency, the distances from the target, the swing and the give-back. Memory only."""
    from . import ticks as ticks_mod
    from . import waveturn

    strike = ticks_mod.strikes.get(shot.ticker) or _window_strike(ctx, shot)
    ok, why = waveturn.admits(ctx.s, shot, ctx.now, strike)
    return Check(ok, why)


@step("ladder", needs_estimate=True)
def ladder_step(ctx: Ctx, shot: LongShot) -> Check:
    """The worth of the bet: the model's chance for the side against what it costs, by the lane's own edge rule (lanes.wants_bet: min_edge_room, min_edge, max_lag) and its chance band."""
    if ctx.p_yes is None:
        return Check(False, "no estimate")
    p = ctx.p_yes if shot.side == "yes" else 1.0 - ctx.p_yes
    e = edge(p, shot.ask)
    cost = shot.ask + fee_per_contract(shot.ask)
    if not wants_bet(ctx.spec, p, shot.ask, e):
        room = (1.0 - p) + e
        needs = f", needs {ctx.spec.min_edge_room * room:+.3f}" if ctx.spec.min_edge_room is not None else ""
        return Check(False, f"worth {p:.2f} for {cost:.3f}, edge {e:+.3f}{needs}" if edge_verdict(ctx.spec, p, shot.ask, e) else f"worth {p:.2f} for {cost:.3f}")
    return Check(True, f"worth {p:.2f} for {cost:.3f}, edge {e:+.3f}")


assert set(STEPS) == set(BUY_STEPS), "buychain's steps and lanes.BUY_STEPS must name the same steps"


class Trail(NamedTuple):
    side: str
    alive: bool  # still alive after the steps that were asked
    said: tuple[tuple[str, Check], ...]  # each step asked of this side, in order, with its answer


def other_side(shot: LongShot) -> LongShot:
    """The same market's other side, its prices read off this side's (a NO ask is one less the YES bid)."""
    return replace(shot, side="no" if shot.side == "yes" else "yes", bid=round(1.0 - shot.ask, 4), ask=round(1.0 - shot.bid, 4))


def walk(ctx: Ctx, shot: LongShot, order: tuple[str, ...]) -> Trail:
    """Ask the steps of one side, in order, until one says no. A step that needs the estimate ends the walk, the side still alive, when there is none yet (the review)."""
    said: list[tuple[str, Check]] = []
    for name in order:
        if name in NEEDS_ESTIMATE and ctx.p_yes is None:
            break
        try:
            got = STEPS[name](ctx, shot)
        except Exception as exc:  # a step that cannot answer has not said yes
            log.warning("buy chain: step %s could not read %s %s: %s", name, shot.ticker, shot.side, exc)
            got = Check(False, f"could not be read ({exc})")
        said.append((name, got))
        if not got.ok:
            return Trail(shot.side, False, tuple(said))
    return Trail(shot.side, True, tuple(said))


def describe(order: tuple[str, ...], trails: list[Trail], module: str = "") -> str:
    """Both sides' walks in one line for a decision's note: the order, then for each side every step asked and what it said."""

    def one(t: Trail) -> str:
        steps = "; ".join(f"{name} {'yes' if c.ok else 'NO'} ({c.why})" for name, c in t.said)
        end = "passed every step" if t.alive and len(t.said) == len(order) else "alive so far" if t.alive else f"out at {t.said[-1][0]}"
        return f"{t.side.upper()} {end}: {steps}" if steps else f"{t.side.upper()} {end}"

    return f"[chain {module + ': ' if module else ''}{' > '.join(order)} | {' | '.join(one(t) for t in trails)}]"


def decide(ctx: Ctx, shot: LongShot) -> tuple[bool, str]:
    """The decision for one candidate (runner.evaluate): every step, in the lane's order, asked of the candidate and of the market's other side. (the candidate is alive at the end, the note)."""
    order = tuple(ctx.spec.buy_chain)
    mine = walk(ctx, shot, order)
    theirs = walk(ctx, other_side(shot), order)
    trails = [mine, theirs] if shot.side == "yes" else [theirs, mine]
    return mine.alive, describe(order, trails, ctx.spec.buy_module)


DROPPED: dict[str, list[dict[str, Any]]] = {}  # lane -> the sides its last review's steps turned down, with why (runner.review_lane writes them to screen_log)


def screen(ctx: Ctx, cands: list[LongShot]) -> list[LongShot]:
    """The review's part of the chain: each candidate side is asked the steps that need no estimate, in the lane's order, up to the first that does. The sides still alive are returned in the order they came; the others are kept in DROPPED with the step that turned each down."""
    order = tuple(ctx.spec.buy_chain)
    keep: list[LongShot] = []
    dropped: list[dict[str, Any]] = []
    for shot in cands:
        trail = walk(ctx, shot, order)
        if trail.alive:
            keep.append(shot)
        else:
            name, check = trail.said[-1]
            dropped.append({"ticker": shot.ticker, "side": shot.side, "ask": shot.ask, "reason": f"chain {' > '.join(order)}: out at {name} ({check.why})"})
    DROPPED[ctx.spec.name] = dropped
    return keep
