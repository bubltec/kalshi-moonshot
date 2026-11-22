"""The sale as a chain of named steps a lane puts in its own order (lane field sell_chain).

2026-11-18, user, on the buy having become named steps in a lane's own order (buychain.py): "we also need to build a sell module, since we're refactoring this flow". The risk exit's decision (watcher.ProfitWatcher._risk_decide) was one function that asked its rules in a fixed order. Here each rule is a step with a name, and a lane lists the steps it wants, in the order it wants them asked:

    sell_chain = ["profit_take", "trail", "long_hold"]

On every risk check the steps are asked in the lane's order. A step answers one of three things: a sale (its reason: the walk ends and the bet is sold), a hold (the walk ends and nothing is sold on this check), or nothing (the next step is asked). A step left out of a chain is not asked.

The steps (a lane with no exit module sells nothing):
  never_ahead   a bet never once in profit is sold at the lane's mark (curve_risk_never_ahead)
  under_even    a favourite whose bid has stayed under even is sold (curve_risk_under_even)
  profit_take   a gain is taken once it is over its bar and the bid has stopped rising (curve_take and its settings: the early take, the sure-win latch, the cushion hold)
  trail         a bet ahead is sold once its bid has come off its high by the market's own swing (curve_take_trail)
  long_hold     a HOLD: while the long layer says the side is likely to pay, no step after it is asked (curve_risk_confirm)
  (the `loss` step, the max loss and the line sale with their wait and early hold, was REMOVED on 2026-11-19, user: "we should remove loss AND the bid_floor concept": a bet that is behind is only sold by never_ahead, under_even, late_tight or the lane's curve exit; a chain that names `loss` is refused)
  time_to_go    in the last curve_risk_time_to_go seconds a bet whose curve chance is under curve_risk_time_to_go_chance (the spot on the wrong side of its target) is sold at the bid once that has held without a break for a wait set by the time left (a dip never shortens it)
  wave_salvage  the wave-top lane's sale (wavetop.py): only a bet that has gone under water (its bid one of its own typical minutes under the bid at the buy) is sold, once the bid is half a unit off its low, its climb has slowed to a quarter of the run's pace and the spot has stopped moving the bet's way. A bet that never dipped is held. The user's resting sale at the entry + 0.40 is recorded as "would sell" (kind "wave rest") and never acts
  late_tight    a bet whose bid is K typical minutes under its high since the buy, ahead or not, in the last share of the market, is sold (curve_risk_late_tight, _share, _live; shadow until _live). Not in the default order: put it after `trail`, so a sale by an earlier step stands
  shape         SHADOW ONLY, never sells: while a bet is behind it records the shape of the bet's own price path (flat, trend against or for, wavy; pathshape.py) since the buy and over a quarter of the time left, and whether the user's rule would sell (kind "shape" in data/risk_shadow.jsonl). Not in the default order; put it first in a chain, since a sale or a hold ends the walk
  shape_timed   the bet has been behind for a share of the time left (pathshape.timed_window) and its path over that same stretch is trend_against (pathshape.py): the bet is sold at the bid. A flat path is only recorded (one row, kind "shape_timed" in data/risk_shadow.jsonl, with the bid it would sell at). Not in the default order
  trail_timed   SHADOW ONLY, never sells or holds: the trail's condition (curverisk.trail_hit) read twice, with the exit's static typical minute and with the time-aware one (pricevol.implied_rate: the price's one-second move implied by its cushion and the time left, which rises toward the close); one row (kind "trail_timed" in data/risk_shadow.jsonl) each time the pair of verdicts changes, with both units and `would_differ`. Not in the default order

Each step reads the lane's existing settings and is off when its setting is off, as it always was; none has a number of its own here. The step bodies are methods of the watcher (ProfitWatcher._sell_<name>): they use its helpers and the bet's state. This module holds the names, the default order, the check of a lane's chain and the note a sale carries.

A lane sells only through its exit module's steps (2026-11-19, user: "we don't use anything outside modules for buying and selling"); the sale reason carries a note at its end:

    [sell chain <exit module, or 'lane'>: a > b > c | sold at <step>]

A chain can be given a name and shared: an exit module ([exit.<name>] in strategies.toml) may hold `steps`, and a lane takes it with exit_module = "<name>" (lanes._with_exit). Memory only: no step reads the database.
"""

from __future__ import annotations

SELL_STEPS = ("never_ahead", "under_even", "profit_take", "trail", "long_hold", "shape", "shape_timed", "trail_timed", "late_tight", "time_to_go", "wave_salvage")  # the steps a lane may name in its exit module; the watcher holds one method for each (_sell_<name>). `shape` and `trail_timed` are shadow only
KEEP = 900  # what Store.mark_exited keeps of a sale's reason


class _Hold:
    """A step's answer that ends the walk with no sale."""

    def __repr__(self) -> str:
        return "HOLD"


HOLD = _Hold()


def check(chain: tuple[str, ...] | list[str], who: str) -> None:
    """Raise ValueError when a chain names a step that does not exist, or a step twice. `who` names the lane or module in the message."""
    bad = [x for x in chain if x not in SELL_STEPS]
    if bad or len(set(chain)) != len(chain):
        raise ValueError(f"{who}: sell_chain names {bad or 'a step twice'}; the steps are {', '.join(SELL_STEPS)}, each at most once")


def tag(chain: tuple[str, ...], module: str, step: str) -> str:
    """The note a chain lane's sale carries: the chain's name and order, and the step that sold."""
    return f" [sell chain {module or 'lane'}: {' > '.join(chain)} | sold at {step}]"


def tagged(reason: str, chain: tuple[str, ...], module: str, step: str, keep: int = KEEP) -> str:
    """The reason with the note at its end. Code that reads a reason matches on its start, so the note is only ever added after it; when the two together are longer than the store keeps, the reason's tail is cut so the note is not lost."""
    note = tag(chain, module, step)
    if len(reason) + len(note) > keep:
        reason = reason[: max(0, keep - len(note) - 3)] + "..."
    return reason + note
