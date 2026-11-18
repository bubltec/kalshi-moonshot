"""The buy as a chain of named steps a lane puts in its own order (lane field buy_chain).

2026-11-17, user, on finding the ladder was the first thing every buy went through: "ladder should not be the entry point"; "the ladder is something that should come LAST in the chain. all it does is evaluate the worth of the bet, not what side to pick or whether it makes any sense"; "the risk score makes way more sense to go FIRST"; "you need to write new code that is modular and can fit in logic in any order".

A step is one question about one side of one market at one moment, answered yes or no with a sentence: is its price one the lane buys, does the risk score favour it, is its risk holding, is it worth more than it costs. A lane lists the steps it wants, in the order it wants them asked:

    buy_chain = ["risk_score", "price", "ladder"]

Both sides of the market start alive. Each step is asked of every side still alive, in the lane's order; a side that gets a no is out and is asked nothing more. A side that is still alive after the last step is one the lane buys. So the first step is the one that puts sides forward, and the last is the final word on the ones that got that far.

Where it runs:
  the review   (runner.lane_shots) asks the steps that need no model estimate, in order, up to the first one that does. The sides left are the lane's candidates; the estimator's own screen (the ladder's) is NOT run for a lane with a chain, so the ladder no longer decides which side is looked at.
  the decision (runner.evaluate) asks every step, in order, of both sides, with the estimate in hand. The candidate is bought when it is alive at the end, and the decision's note carries what each step said of each side.

The steps read the lane's existing settings (its price range, risk_score_buy, entry_risk_trend, entry_trend_seconds, min_edge_room); none has a number of its own here. A new step is one function with @step("name") and its name added to lanes.BUY_STEPS. Memory only: no step reads the database.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, NamedTuple

from .lanes import BUY_STEPS, LaneSpec, chance_ok, edge_verdict, no_buy, wants_bet
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
    """The side's ask is inside the lane's price range and outside its no-buy range, and the lane buys that side."""
    lo, hi = ctx.spec.price
    if shot.side not in ctx.spec.sides:
        return Check(False, f"the lane does not buy {shot.side.upper()}")
    if not lo <= shot.ask <= hi:
        return Check(False, f"ask {shot.ask:.2f} is outside the lane's {lo:g} to {hi:g}")
    if no_buy(ctx.spec, shot.ask):
        return Check(False, f"ask {shot.ask:.2f} is in the lane's no-buy range {ctx.spec.price_skip[0]:g} to {ctx.spec.price_skip[1]:g}")
    return Check(True, f"ask {shot.ask:.2f}")


@step("risk_score")
def risk_score_step(ctx: Ctx, shot: LongShot) -> Check:
    """The risk score favours the side: its risk (riskscore.py) is at or under the lane's level (risk_score_buy) now, 30 s ago and a minute ago."""
    from . import riskscore

    level = ctx.spec.risk_score_buy
    if level <= 0:
        return Check(False, "the lane sets no risk level (risk_score_buy)")
    rs = riskscore.live(shot.series or shot.ticker.split("-")[0], shot.ticker, shot.side, ctx.now + shot.hours_to_close * 3600.0, ctx.now, ctx.s.data_dir)
    if rs is None:
        return Check(False, "no risk score yet (a market under a minute old, a price at the edge or a stale index)")
    risks = ", ".join(f"{100 * (1 - c):.0f}%" for c in rs[0])
    if not riskscore.low_risk(rs[0], level):
        return Check(False, f"risk {risks} (now, 30 s ago, a minute ago), over the lane's {100 * level:.0f}%")
    return Check(True, f"risk {risks}, at or under {100 * level:.0f}% and held")


@step("risk_trend")
def risk_trend_step(ctx: Ctx, shot: LongShot) -> Check:
    """The side's risk, as the exit reads it, has held or improved over the lane's look back (entry_risk_trend, a share of the time left)."""
    from . import runner

    if ctx.spec.entry_risk_trend <= 0:
        return Check(False, "the lane sets no look back (entry_risk_trend)")
    got = runner.risk_trend_read(shot, ctx.spec, ctx.now, keep=False)
    if got is None:
        return Check(False, "no risk trend yet (a young market, or its quotes or long layer are missing)")
    return Check(got[2] == "holding", f"risk trend {got[0]:+.1f} over {got[1]:.0f}s, {got[2]}")


@step("price_trend")
def price_trend_step(ctx: Ctx, shot: LongShot) -> Check:
    """The side's price has gained over the lane's span (entry_trend_seconds) by more than entry_trend_z of the market's own typical moves: the check every lane ran before the chain."""
    from . import runner

    seconds, bar = ctx.spec.entry_trend_seconds, ctx.spec.entry_trend_z or 0.0
    if seconds <= 0:
        return Check(False, "the lane sets no span (entry_trend_seconds)")
    z = runner.side_trend_z(ctx.s, shot, seconds, ctx.now)
    if z is None:
        return Check(False, f"no quote {seconds:.0f}s back")
    return Check(z > bar, f"price trend {z:+.1f} over {seconds:.0f}s, needs over {bar:+.1f}")


@step("ladder", needs_estimate=True)
def ladder_step(ctx: Ctx, shot: LongShot) -> Check:
    """The worth of the bet: the model's chance for the side against what it costs, by the lane's own edge rule (lanes.wants_bet: min_edge_room, min_edge, max_lag) and its chance band."""
    if ctx.p_yes is None:
        return Check(False, "no estimate")
    p = ctx.p_yes if shot.side == "yes" else 1.0 - ctx.p_yes
    e = edge(p, shot.ask)
    cost = shot.ask + fee_per_contract(shot.ask)
    if not chance_ok(ctx.spec, p):
        return Check(False, f"worth {p:.2f} is outside the lane's chance band {ctx.spec.min_chance:g} to {ctx.spec.max_chance:g}")
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


def describe(order: tuple[str, ...], trails: list[Trail]) -> str:
    """Both sides' walks in one line for a decision's note: the order, then for each side every step asked and what it said."""

    def one(t: Trail) -> str:
        steps = "; ".join(f"{name} {'yes' if c.ok else 'NO'} ({c.why})" for name, c in t.said)
        end = "passed every step" if t.alive and len(t.said) == len(order) else "alive so far" if t.alive else f"out at {t.said[-1][0]}"
        return f"{t.side.upper()} {end}: {steps}" if steps else f"{t.side.upper()} {end}"

    return f"[chain {' > '.join(order)} | {' | '.join(one(t) for t in trails)}]"


def decide(ctx: Ctx, shot: LongShot) -> tuple[bool, str]:
    """The decision for one candidate (runner.evaluate): every step, in the lane's order, asked of the candidate and of the market's other side. (the candidate is alive at the end, the note)."""
    order = tuple(ctx.spec.buy_chain)
    mine = walk(ctx, shot, order)
    theirs = walk(ctx, other_side(shot), order)
    trails = [mine, theirs] if shot.side == "yes" else [theirs, mine]
    return mine.alive, describe(order, trails)


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
