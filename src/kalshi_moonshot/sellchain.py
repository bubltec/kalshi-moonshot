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
  wave_take     the wave-top lane's sales (wavetop.py, 2026-11-22): the staircase sells a bet that has been ahead the first time its bid falls under the last swing low (swings sized by the bid's typical minute and the time left; a small loss is accepted); from 0.80 up a bet that is ahead is sold when the chance of losing it is at least what the sale gives up over holding.
  value_sale    (lane fields value_sale_crossed: only after the spot has crossed to the wrong side of the target; value_sale_wait_share: the condition must hold without a break for this share of the time left) a favourite's sale (2026-11-23, user: "value sale looks good"; "avoiding loss"): sells when the bid after the sale fee is at least what holding is worth, 1 - p_lose (the spot's chance of ending on the wrong side, in its own typical moves over the time left), plus the lane's value_sale_premium; at a gain or a loss. A premium over zero holds the bets the spot says are strong toward settlement and sells the ones the market overpays for
  early_take    (2026-11-24, user: "early market, take early profit ... if it doesn't keep increasing just sell for a profit"; lane fields early_take_share, early_take_stall_share): in the first early_take_share of the market's 900 s, once the bid after the sale fee is over the price paid with its buy fee, follow the bid's high and sell, at a gain, the tick it stops climbing (a fall from the high of its own typical 10 s move, or no new high for early_take_stall_share of the time left). No fixed cents, no reading waited for.
  no_recovery   (2026-11-24, user: "accept its a loss when it doesn't come back after the wait ... if it never became profitable it should sell after the majority of the market is done"; lane field no_loss_share): a bet never once in profit after both fees is sold at the bid, at a loss, once the early no_loss_share of the market is over
  late_dip      (2026-11-24, user: "if you see a sharp dip on price or spot to strike, it means sell immediately under a minute left"; lane fields late_dip_share, late_dip_units, late_dip_look_seconds, late_dip_wait_share): in the last late_dip_share of the market, a bid or spot cushion down from its recent high by late_dip_units of its own typical 10 s move, held for late_dip_wait_share of the time left, is sold at any result.
  turn_sale     (2026-11-26, user, on ZEC 10:30 NO bought at 0.18 that ran to 0.32 and was sold at 0.02: "it didn't take the profit it had", "The curve followed the expected pattern and it just didn't exit"): a wave bet is sold AT A GAIN after both fees when the wave has turned back, read on the buy step's own curve (waveturn.peek): for a NO, a fall of the lane's swing size that has given back its turn size, with its low after the buy; a YES mirrors it. Where the lane would buy the other side, the bet's move is over. Never a loss sale
  value_hold    a HOLD (2026-11-23, the wave session, the user: "value hold only applies after the flip"): while the spot's cushion for the bet's side is over zero (the flip has happened, the bet is a favourite) and the bid after the sale fee is under what holding is worth plus value_sale_premium (1 - p_lose + premium; exactly when value_sale would not sell), the walk ends here with no sale, so a step after it (the staircase's lower-low sale, the give-up) is not asked. Name it before them. A bet the spot has on the wrong side is not held by it
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

SELL_STEPS = ("never_ahead", "under_even", "profit_take", "trail", "long_hold", "shape", "shape_timed", "trail_timed", "late_tight", "time_to_go", "wave_take", "wave_salvage", "value_sale", "value_hold", "early_take", "late_dip", "no_recovery", "turn_sale")  # the steps a lane may name in its exit module; the watcher holds one method for each (_sell_<name>). `shape` and `trail_timed` are shadow only
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
    from . import facts

    facts.check_needs(chain, who)  # a step's named facts must each have a reader


def tag(chain: tuple[str, ...], module: str, step: str) -> str:
    """The note a chain lane's sale carries: the chain's name and order, and the step that sold."""
    return f" [sell chain {module or 'lane'}: {' > '.join(chain)} | sold at {step}]"


def tagged(reason: str, chain: tuple[str, ...], module: str, step: str, keep: int = KEEP) -> str:
    """The reason with the note at its end. Code that reads a reason matches on its start, so the note is only ever added after it; when the two together are longer than the store keeps, the reason's tail is cut so the note is not lost."""
    note = tag(chain, module, step)
    if len(reason) + len(note) > keep:
        reason = reason[: max(0, keep - len(note) - 3)] + "..."
    return reason + note
