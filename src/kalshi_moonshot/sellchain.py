"""The sale as a chain of named steps a lane puts in its own order (lane field sell_chain).

2026-11-18, user, on the buy having become named steps in a lane's own order (buychain.py): "we also need to build a sell module, since we're refactoring this flow". The risk exit's decision (watcher.ProfitWatcher._risk_decide) was one function that asked its rules in a fixed order. Here each rule is a step with a name, and a lane lists the steps it wants, in the order it wants them asked:

    sell_chain = ["profit_take", "trail", "long_hold", "loss"]

On every risk check the steps are asked in the lane's order. A step answers one of three things: a sale (its reason: the walk ends and the bet is sold), a hold (the walk ends and nothing is sold on this check), or nothing (the next step is asked). A step left out of a chain is not asked.

The steps:
  never_ahead   a bet never once in profit is sold at the lane's mark (curve_risk_never_ahead)
  under_even    a favourite whose bid has stayed under even is sold (curve_risk_under_even)
  profit_take   a gain is taken once it is over its bar and the bid has stopped rising (curve_take and its settings: the early take, the sure-win latch, the cushion hold)
  trail         a bet ahead is sold once its bid has come off its high by the market's own swing (curve_take_trail)
  long_hold     a HOLD: while the long layer says the side is likely to pay, no step after it is asked (curve_risk_confirm)
  loss          the max loss, the line, their wait and the early hold together (curve_risk_max_loss, curve_risk_sell, ...): they share one wait and one latch
  shape         SHADOW ONLY, never sells: while a bet is behind it records the shape of the bet's own price path (flat, trend against or for, wavy; pathshape.py) since the buy and over a quarter of the time left, and whether the user's rule would sell (kind "shape" in data/risk_shadow.jsonl). Not in the default order; put it first in a chain, since a sale or a hold ends the walk
  shape_timed   SHADOW ONLY, never sells or holds: the bet has been behind for a share of the time left (pathshape.timed_window) and its path over that same stretch is flat or trending against (pathshape.py); one row (kind "shape_timed" in data/risk_shadow.jsonl) the first time both hold, with the bid it would sell at. Not in the default order
  trail_timed   SHADOW ONLY, never sells or holds: the trail's condition (curverisk.trail_hit) read twice, with the exit's static typical minute and with the time-aware one (pricevol.implied_rate: the price's one-second move implied by its cushion and the time left, which rises toward the close); one row (kind "trail_timed" in data/risk_shadow.jsonl) each time the pair of verdicts changes, with both units and `would_differ`. Not in the default order

Each step reads the lane's existing settings and is off when its setting is off, as it always was; none has a number of its own here. The step bodies are methods of the watcher (ProfitWatcher._sell_<name>): they use its helpers and the bet's state. This module holds the names, the default order, the check of a lane's chain and the note a sale carries.

A lane with NO chain is asked the default order, which is the order the risk exit has always used, and its sale reasons are unchanged. Only a lane that names a chain gets the note at the end of its sale reason:

    [sell chain <exit module, or 'lane'>: a > b > c | sold at <step>]

A chain can be given a name and shared: an exit module ([exit.<name>] in strategies.toml) may hold `steps`, and a lane takes it with exit_module = "<name>" (lanes._with_exit). Memory only: no step reads the database.
"""

from __future__ import annotations

DEFAULT = ("never_ahead", "under_even", "profit_take", "trail", "long_hold", "loss")  # the order a lane with no chain is asked: the risk exit's order before the chain existed
SELL_STEPS = (*DEFAULT, "shape", "shape_timed", "trail_timed")  # the steps a lane may name in sell_chain; the watcher holds one method for each (_sell_<name>). `shape` and `trail_timed` are shadow only and asked only where a chain names them
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


def order_of(chain: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    """The steps to ask: the lane's chain, or the default order when it has none."""
    return tuple(chain) if chain else DEFAULT


def tag(chain: tuple[str, ...], module: str, step: str) -> str:
    """The note a chain lane's sale carries: the chain's name and order, and the step that sold."""
    return f" [sell chain {module or 'lane'}: {' > '.join(chain)} | sold at {step}]"


def tagged(reason: str, chain: tuple[str, ...], module: str, step: str, keep: int = KEEP) -> str:
    """The reason with the note at its end. Code that reads a reason matches on its start, so the note is only ever added after it; when the two together are longer than the store keeps, the reason's tail is cut so the note is not lost."""
    note = tag(chain, module, step)
    if len(reason) + len(note) > keep:
        reason = reason[: max(0, keep - len(note) - 3)] + "..."
    return reason + note
