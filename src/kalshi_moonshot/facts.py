"""The facts board: how the steps of a module talk to each other (2026-11-22; user: "could we make steps communicate with each other?", then "yes this works").

A READER computes one named fact about a bet at one moment (the spot's cushion over the target, the chance of losing the bet, the kind of wave it is). A step does not call the reader's code or another step's helper: it asks the board for the fact by name. A fact is computed the first time anything asks for it on a check and kept for the rest of that check, so two steps that need the same fact read the same value, once. A reader may ask for other facts (the chance of losing needs the spot's cushion); that is how facts build on each other.

Each step names the facts it may read (NEEDS, below). Asking for one it did not name raises, so what influences a step is always listed, and the module's step list says what each sale depends on. The sale reasons carry the facts the step read ("[facts read: ...]"), so a bet's log line shows what it was judged on. When a lane file loads, `sellchain.check` refuses a step whose needs have no reader.

VETOES (user, 2026-11-22: "this facts board ... is exactly what we need to make sure we don't let one step action when another tells it not to"): VETO_RULES say which fact, read as which value, stops which steps from selling ("the whipsaw fact is not False: the wave sales hold"). A step asks the board `cleared()` at the point where it would sell and the sale walk refuses a sale from a step the board vetoed, so a step cannot act against a fact another reading has settled, and the refusal is logged with the rule's label.

WEIGHTS (same message: "allows us to add weights that really accumulate"): any step or reader may `weigh(source, weight, why)` on the board: signed evidence for (+) or against (-) a sale on this check, and `score()` is the running total. The ledger is part of the board and of the note a sale carries; no lane uses it yet (nothing here sells on a total): the first use is for the user to choose.

Memory only: a reader reads the quote and tick memory and the bet's own state, never the database (CLAUDE.md: no database call in buy or sell logic). Sell steps first; the buy chain can adopt the same board (a `Ctx` that holds a Board, readers for the buy's facts).

To add a fact: write a reader with @reader("name") returning (value, why), and add the name to NEEDS for each step that reads it.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from typing import Any

log = logging.getLogger("moonshot")

READERS: dict[str, Callable[[Board], tuple[Any, str]]] = {}
NEEDS: dict[str, tuple[str, ...]] = {
    "wave_take": ("whipsaw", "bid_unit", "p_lose", "cushion_factor", "waves_before", "wave_now", "wave_leg"),
    "wave_salvage": ("spot",),
    "value_sale": ("p_lose", "spot", "waves_before", "wave_now", "wave_leg"),  # early_take, no_recovery and late_dip read the board through this view
    "value_hold": ("p_lose", "spot", "waves_before", "wave_now", "wave_leg"),
    "priskp": ("priskp_screen", "shift_out", "early_swings", "hour_view", "price_gap", "waves_before", "wave_now", "wave_leg"),  # the buy side (2026-11-22): the board's host is a small holder of the settings, the moment and the hours, the bet is the shot
}


VETO_RULES: list[tuple[str, Callable[[Any], bool], tuple[str, ...], str]] = []  # (fact, vetoes when this is true of its value, the steps it stops, the label the log carries)
VETO_RULES.append(("shift_out", lambda v: v is not None, ("priskp",), "shift_out_of_cushion"))  # user, 2026-11-22: no buy on the side a confirmed shift took out of its cushion, against the current target
VETO_RULES.append(("loss_while_ahead", lambda v: v is True, ("late_dip", "no_recovery", "value_sale"), "still_on_the_winning_side_of_the_target"))  # user, 2026-11-25, on BNB and DOGE 13:30 YES sold at 0.60 and 0.71 with the spot over the strike (both settled YES): "we were winning", "is it selling because it thinks its losing when its over 50c?", "just don't sell if it isn't on the other side of the target": a sale under what the bet cost needs the spot on the OTHER side of the target
VETO_RULES.append(("early_swings", lambda v: v is True, ("priskp",), "early_swings_not_cleared"))  # user, 2026-11-25: "If you're betting on a curve early you need to be calculating for additional swings and variance possibilities based on the recent history (hour)"; 2026-11-26 "Fix it": a curve trend entry WAITS while the swing-aware chance (wave_now) of ending on its side is under a bar that slides with the time left (swing_bar)
VETOED_STEPS = frozenset(step for _, _, steps, _ in VETO_RULES for step in steps)


def reader(name: str) -> Callable[[Callable[[Board], tuple[Any, str]]], Callable[[Board], tuple[Any, str]]]:
    def deco(fn: Callable[[Board], tuple[Any, str]]) -> Callable[[Board], tuple[Any, str]]:
        READERS[name] = fn
        return fn

    return deco


def check_needs(chain: tuple[str, ...] | list[str], who: str) -> None:
    """Raise ValueError when a step in the chain needs a fact no reader provides."""
    for step in chain:
        for need in NEEDS.get(step, ()):
            if need not in READERS:
                raise ValueError(f"{who}: step {step!r} needs the fact {need!r}, which has no reader")


class Board:
    """The facts of one bet on one check. `host` is the watcher (its helpers read memory), `t` the bet's state."""

    def __init__(self, host: Any, t: Any, now: float, left: float, bid: float) -> None:
        self.host, self.t, self.now, self.left, self.bid = host, t, now, left, bid
        self._v: dict[str, tuple[Any, str]] = {}
        self._read: list[str] = []
        self._weights: list[tuple[str, float, str]] = []

    def get(self, name: str) -> Any:
        if name not in self._v:
            if name not in READERS:
                raise KeyError(f"no reader for the fact {name!r}")
            try:
                self._v[name] = READERS[name](self)
            except Exception as exc:  # a reader that fails is an unknown fact, never a crash of the sale
                log.warning("fact %s unreadable on %s: %r", name, getattr(self.t, "ticker", "?"), exc)
                self._v[name] = (None, f"unreadable ({exc!r})")
        if name not in self._read:
            self._read.append(name)
        return self._v[name][0]

    def why(self, name: str) -> str:
        self.get(name)
        return self._v[name][1]

    def view(self, step: str) -> View:
        return View(self, step)

    def vetoes(self, step: str) -> str | None:
        """The label of the first veto rule that stops `step` on this check, or None. Reads the rule's fact through the board."""
        for fact, test, steps, label in VETO_RULES:
            if step in steps and test(self.get(fact)):
                return label
        return None

    def weigh(self, source: str, weight: float, why: str = "") -> None:
        """Add signed evidence (for a sale +, against -) to this check's ledger."""
        self._weights.append((source, float(weight), why))

    def score(self) -> float:
        return sum(w for _, w, _ in self._weights)

    def note(self) -> str:
        """The facts read (and the weights added) on this check, for a sale's reason."""
        out = ""
        if self._read:
            out += " [facts read: " + "; ".join(f"{n}={_show(self._v[n][0])}" for n in self._read) + "]"
        if self._weights:
            out += " [weights: " + "; ".join(f"{src} {w:+.2f}" for src, w, _ in self._weights) + f" = {self.score():+.2f}]"
        return out


class View:
    """A step's own window on the board: only the facts it named."""

    def __init__(self, board: Board, step: str) -> None:
        self.board, self.step = board, step

    def get(self, name: str) -> Any:
        if name not in NEEDS.get(self.step, ()):
            raise KeyError(f"step {self.step!r} read the fact {name!r}, which it did not name in facts.NEEDS")
        return self.board.get(name)

    def cleared(self) -> bool:
        """False when a veto rule stops this step on this check: the step must not sell (the walk refuses it if it does)."""
        return self.board.vetoes(self.step) is None

    def weigh(self, weight: float, why: str = "") -> None:
        self.board.weigh(self.step, weight, why)

    def note(self) -> str:
        return self.board.note()


def _show(v: Any) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:.3g}"
    if isinstance(v, tuple):
        return "(" + ", ".join(_show(x) for x in v) + ")"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{k}={_show(x)}" for k, x in v.items()) + "}"
    return str(v)


# ---- the readers (memory only) ----
@reader("spot")
def _spot(b: Board) -> tuple[Any, str]:
    """(cushion, typical minute) of the spot against the target, for the bet's side: the 60 s average's distance past the strike (negative = the wrong side) and the spot's own rms one-second move x sqrt(60). None when no reading is armed or the ticks are stale."""
    got = b.host._wave_spot(b.t, b.now)
    return got, "the spot's 60 s average against the target, in the spot's own typical minute" if got else "no spot reading (none armed, or stale ticks)"


@reader("p_lose")
def _p_lose(b: Board) -> tuple[Any, str]:
    """The chance the spot's average ends on the wrong side: its cushion over what a random walk moves in the time left (wavetop.p_lose). None without a spot reading."""
    from . import wavetop

    end = settle_end(b)
    if end is not None:  # inside the settlement window the trailing 60 s average and the whole time left are the wrong reading (2026-11-26, user, on ZEC 15:45 YES: "I'm sure the priskp score said NO. Which at the end of a market should matter most"; it read 0.95 for YES at 15:44:40 as the spot fell through the target, off a 1/3/5 minute blend): what settles is this window's average, part of it already banked
        ends, sd = end
        lose = (0.0 if ends > 0 else 1.0) if sd <= 0 else wavetop.norm_cdf(-ends / sd)
        return lose, f"the settlement window: it would end {ends:+.4g} for the bet if the spot stays where it is, against {sd:.4g} of random walk left in it"
    spot = b.get("spot")
    if spot is None:
        return None, "no spot reading"
    return wavetop.p_lose(spot[0], spot[1], b.left), "the cushion against a random walk's move over the time left"


def settle_end(b: Board) -> tuple[float, float] | None:
    """Inside the settlement window (the market's last SETTLE_S seconds, what Kalshi settles on): (where the window's average would end for the bet's side if the spot stays at its last 5 s level, the standard deviation a random walk still adds to that average). The first is (the banked average x seconds done + the last 5 s x seconds left) / 60; the second is (left / 60) x the 1 s move x sqrt(left / 3), the 1 s move being the typical minute / sqrt(60) (the mean of a random walk over T seconds has variance T / 3 per unit of 1 s variance). None outside the window or without both readings."""
    if not 0 < b.left < SETTLE_S:
        return None
    banked, fast = b.get("spot_settling"), b.get("spot_now")
    if banked is None or fast is None:
        return None
    ends = (banked[0] * (SETTLE_S - b.left) + fast[0] * b.left) / SETTLE_S
    sd = (b.left / SETTLE_S) * (fast[1] / math.sqrt(60.0)) * math.sqrt(b.left / 3.0)
    return ends, sd


@reader("cushion_factor")
def _cushion_factor(b: Board) -> tuple[Any, str]:
    """2 x (1 - p_lose): about 2 with the spot safely on the bet's side, about 0 on the wrong side; 1 when the lane's wave_cushion is off or there is no reading."""
    if not b.t.wave_cushion:
        return 1.0, "the lane's wave_cushion is off"
    lose = b.get("p_lose")
    return (1.0, "no spot reading") if lose is None else (2.0 * (1.0 - lose), "2 x (1 - p_lose)")


@reader("bid_unit")
def _bid_unit(b: Board) -> tuple[Any, str]:
    """The bet's own typical minute: the rms one-second change of the market's mid over the last 15 minutes x sqrt(60) (the watcher's _risk_unit)."""
    return b.host._risk_unit(b.t, b.now), "the market's own typical minute"


@reader("whipsaw")
def _whipsaw(b: Board) -> tuple[Any, str]:
    """The whipsaw variant: True when the bet's bid range since the buy, in the bid's typical minutes, is at or over the lane's wave_whipsaw_range (read on every check by the watcher's _is_whipsaw_behavior; once true it stays true); False under it or with the variant off. The staircase reads it to size its swing; nothing is held."""
    v = b.host._is_whipsaw_behavior(b.t, b.now)
    if b.t.wave_whipsaw <= 0:
        return False, "the lane's variant is off"
    return v, f"bid range {b.t.wave_range_u:.2f} typical minutes since the buy against {b.t.wave_whipsaw:.2f}"



# ---- the buy side's readers (the priskp step; memory only) ----
@reader("priskp_screen")
def _priskp_screen(b: Board) -> tuple[Any, str]:
    """The five checks for the shot's side now (priskp.screen): the note as a dict with verb buy or wait. The latest out-of-cushion shift it found is kept for the fact `shift_out`."""
    from . import priskp

    note, shift = priskp.screen(b.host, b.t)
    b.host.shift = shift
    return note, "the five checks in the market's own fifths, held for the readings the time left asks, minutes 2-14, no ask 0.45-0.55"


@reader("hour_view")
def _hour_view(b: Board) -> tuple[Any, str]:
    """The hour before now for the bet's side (hourview.py): drift, range, crossings, straightness, minutes since its best and worst. Built every five minutes in the background and read from memory; None when the market has none or it is stale. A tie-breaker for a risk that is not sure, logged only for now."""
    from . import hourview

    got = hourview.read(b.t.ticker, b.t.side, b.now)
    return got, "the hour before now, built in the background every five minutes" if got else "no hour view for this market (not built yet, or stale)"


@reader("price_gap")
def _price_gap(b: Board) -> tuple[Any, str]:
    """What the bet costs against its chance (2026-11-26, user: "it should have an influence but it shouldn't be the factor in whether we buy"; then "if you're betting on a curve early you need to be calculating for additional swings and variance possibilities based on the recent history"). The chance is the SWING-AWARE one when it can be read: `wave_now`'s mean-reverting chance (the market's own centre, width and time scale from its recent history: a spike away from the centre is expected to swing back, and the more time is left the more it can), else the plain Gaussian chance of the screen's blended cushion (a random walk, no swings). The gap is that chance minus the ask and its buy fee: signed, positive when the price is below the chance. None when no chance is read."""
    note = b.get("priskp_screen")
    gauss, ask = note.get("chance"), note.get("ask")
    if ask is None or (gauss is None and not isinstance(b.get("wave_now"), dict)):
        return None, "the chance is not read (no cushion reading yet)"
    wn = b.get("wave_now")
    swing = wn.get("chance") if isinstance(wn, dict) else None
    chance = swing if swing is not None else gauss
    if chance is None:
        return None, "the chance is not read (no cushion reading yet)"
    ask = float(ask)
    out = {"chance": float(chance), "gauss": None if gauss is None else float(gauss), "swing": None if swing is None else float(swing), "ask": ask, "gap": round(float(chance) - ask - 0.07 * ask * (1.0 - ask), 4)}
    return out, "the chance of ending on the bet's side (the swing-aware one when read, else the random-walk one) minus the ask and its buy fee"


SWING_HALF = 0.5  # the swing-aware chance an entry at the OPEN needs: more likely than not to end on its side once the hour's swings are counted (structural). The bar slides with the time left (user, 2026-11-26: "Make sure the early rule has a slide with time left"): SWING_HALF x the share of the market still to run, so 0.5 at the open, 0.33 at minute 5, 0.25 at the half, 0 at the close


def swing_bar(left: float) -> float:
    """The least swing-aware chance a curve trend entry needs with `left` seconds to the close: SWING_HALF x left / 900. The more of the market is still to run, the more swings can still come, and the more the entry has to have on its side."""
    return SWING_HALF * min(1.0, max(0.0, left / 900.0))


@reader("early_swings")
def _early_swings(b: Board) -> tuple[Any, str]:
    """True when the buy is a curve TREND ENTRY and the swing-aware chance of ending on its side (wave_now: the hour's swings and the time left, not a random walk) is under swing_bar(left), a bar that slides down as the market runs out (no cut-off minute). The veto rule turns it into a WAIT: the bet is bought once the chance clears the bar, which falls every second. False otherwise; None when the swing-aware chance is not read (nothing waits on a number that is not there). 2026-11-26, the 38 trend entries of 11:02-13:30 ET with a reading (exchange results, seen after the fact, score it forward): under the bar 11 bets -$52.38 (4 on the winning side), over it 27 bets +$23.07 (23). The first form cut off at minute 5 and missed ZEC 13:30 NO, bought at minute 5.9 with a chance of 0.16, -$13.83."""
    n = b.get("priskp_screen")
    if n.get("why") != "curve_trend_entry":
        return False, "not a curve trend entry"
    wn = b.get("wave_now")
    ch = wn.get("chance") if isinstance(wn, dict) else None
    if ch is None:
        return None, "the swing-aware chance is not read"
    bar = swing_bar(b.left)
    return bool(float(ch) < bar), f"the swing-aware chance {float(ch):.2f} against a bar of {bar:.2f} ({SWING_HALF} x {b.left:.0f} s of 900 left)"


@reader("shift_out")
def _shift_out(b: Board) -> tuple[Any, str]:
    """The latest confirmed shift (a dict) when it took the spot out of this side's cushion against this market's target; None otherwise (and None until the checks are read)."""
    b.get("priskp_screen")
    return getattr(b.host, "shift", None), "the latest confirmed shift, against this market's own target"


# ---- the wave readings (wavefacts.py; memory only; 2026-11-25, user: "those can go on the fact board for all steps to use") ----
def _wave_ctx(b: Board) -> tuple[str, float, float, str] | None:
    """(the curve's index, its open, the target on the curve's own scale, the bet's side) for the bet or the shot on this board, or None when no reading is armed. A watched bet carries them (shock_index, shock_ref); a buy's shot is read the way the wave lane's step reads it."""
    t = b.t
    side = getattr(t, "side", "") or ""
    if getattr(t, "shock_index", "") and getattr(t, "shock_ref", 0.0) > 0 and getattr(t, "close_ts", 0.0) > 0:
        return t.shock_index, t.close_ts - 900.0, float(t.shock_ref), side
    from . import binaryrisk, ticks

    series = getattr(t, "series", "") or str(getattr(t, "ticker", "")).split("-")[0]
    index = binaryrisk.curve_index(series) if series else None
    strike = ticks.strikes.get(getattr(t, "ticker", ""))
    hours = getattr(t, "hours_to_close", None)
    if not index or not strike or hours is None:
        return None
    return index, round((b.now + hours * 3600.0) / 900.0) * 900.0 - 900.0, float(strike), side


@reader("waves_before")
def _waves_before(b: Board) -> tuple[Any, str]:
    """The market's waves before its open (wavefacts.before): its typical minute, its time scale tau, how far its waves travel in 3, 10 and 15 minutes and its wave width; read once per market from the tick memory. None when no curve is read or the hour before the open is not in memory."""
    from . import wavefacts

    ctx = _wave_ctx(b)
    got = wavefacts.before(ctx[0], ctx[1]) if ctx else None
    return got, "the hour before the open on a 10 s grid: typical minute, tau, 3/10/15 minute travel, wave width" if got else "no waves before (no curve armed, or the hour before the open is not in memory)"


@reader("wave_now")
def _wave_now(b: Board) -> tuple[Any, str]:
    """The waves now for the bet's side (wavefacts.now_read): z against the 30 minute mean, the line's R^2 and slope, the run and pull-back of the last 5 minutes in typical 10 s moves, the last minute's move, the target's distance in wave widths, tau and the mean-reverting chance of the side winning. None when the last 30 minutes are not in memory."""
    from . import wavefacts

    ctx = _wave_ctx(b)
    got = wavefacts.now_read(ctx[0], b.now, ctx[2], ctx[3], b.left) if ctx and ctx[3] in ("yes", "no") else None
    return got, "the last 30 minutes on a 10 s grid, signed for the side" if got else "no waves now (no curve armed, or the last 30 minutes are not in memory)"


@reader("wave_leg")
def _wave_leg(b: Board) -> tuple[Any, str]:
    """Where the bet's side stands in the curve's current leg (wavefacts.leg): the class (inside an unturned leg, turned, against the current swing, none), the swing and its give-back in typical minutes, the 30 minute R^2 and the shadow verdict would_refuse. Memory only; None when the hour before the open or the path since it is not in memory."""
    from . import wavefacts

    ctx = _wave_ctx(b)
    got = wavefacts.leg(ctx[0], ctx[1], b.now, ctx[2], ctx[3], b.left) if ctx and ctx[3] in ("yes", "no") else None
    return got, "the wave lane's zig-zag walked back from now to the window's open" if got else "no leg reading (no curve armed, or the hour before the open or the path since it is not in memory)"


@reader("spot_now")
def _spot_now(b: Board) -> tuple[Any, str]:
    """(cushion, typical minute) of the spot against the target for the bet's side over the last 5 seconds only (wavetop.spot_read with a 5 s span): the 60 s average of the fact `spot` lags a spot that has just crossed (2026-11-25, DOGE 17:00 NO sold at 0.53 at 16:53:21, 16 s after the spot came back under the strike, while its 60 s average was still over it, and it kept falling to -0.12%)."""
    got = b.host._wave_spot(b.t, b.now, 5)
    return got, "the spot's last 5 s against the target, in the spot's own typical minute" if got else "no spot reading (none armed, or stale ticks)"


SETTLE_S = 60.0  # the settlement window: Kalshi settles these markets on the index's average over the last 60 s (docs/architecture.md section 6)


@reader("spot_settling")
def _spot_settling(b: Board) -> tuple[Any, str]:
    """(cushion, typical minute) of the spot's average SINCE THE SETTLEMENT WINDOW OPENED, for the bet's side: the part of the settlement average already banked. None outside the window, in its first second, or without a reading."""
    banked = int(SETTLE_S - b.left)
    if not 0 < b.left < SETTLE_S or banked < 1:
        return None, "not inside the settlement window"
    got = b.host._wave_spot(b.t, b.now, banked)
    return got, f"the spot's average over the {banked} s of the settlement window so far, against the target" if got else "no spot reading (none armed, or stale ticks)"


@reader("loss_while_ahead")
def _loss_while_ahead(b: Board) -> tuple[Any, str]:
    """True when a sale now would be a LOSS (the bid after its fee under the price paid with its fee) while the spot is still on the bet's own side of the target by EITHER reading: the 60 s average or the last 5 s (2026-11-25, user: "just don't sell if it isn't on the other side of the target"; "it cannot just sell blindly", after the 60 s average let a sale through 16 s after the spot had come back). A sale goes through only when both are on the other side. False when the sale is not a loss; None when no spot is read (the sale is then not vetoed). The veto rule on the board stops the loss-sale steps with it."""
    from .strategy import fee_per_contract

    spot = b.get("spot")
    fast = b.get("spot_now")
    slow_name = "60 s average"
    if 0 < b.left < SETTLE_S:  # inside the settlement window (structural: Kalshi settles on the index's average over the market's last 60 s) the trailing 60 s average is mostly prices that do NOT count; what counts is the average since the window opened. 2026-11-26, NEAR 12:45 YES: the spot went under the target as the window opened (12:44:00), late_dip wanted to sell for 48 s while the bid went 0.28 -> 0.01, and this veto held it on a 60 s average still lifted by the minute before; sold at 0.011 with 20 s left, -$14.78
        banked = b.get("spot_settling")
        if banked is not None:
            spot, slow_name = banked, f"settlement window so far ({SETTLE_S - b.left:.0f} s)"
    if spot is None and fast is None:
        return None, "no spot reading"
    loss = (b.bid - fee_per_contract(b.bid)) < (b.t.entry + fee_per_contract(b.t.entry))
    ahead = (spot is not None and spot[0] > 0) or (fast is not None and fast[0] > 0)
    told = ", ".join(f"{n} {v[0]:+.4g}" for n, v in ((slow_name, spot), ("last 5 s", fast)) if v is not None)
    if slow_name != "60 s average" and spot is not None and fast is not None:  # inside the settlement window with both read: what settles is the WHOLE window's average, so the bet is ahead only if the banked part plus the rest of the window at the spot's level now still ends on its side. 2026-11-26, ZEC 15:45 YES (bought 0.80, bid 0.99 at 15:44:15, a full loss of $15.18): the spot fell through the target with 30 s left, late_dip was held at 15:44:43 because the 43 s banked were still over it, and the market settled NO
        ends = (spot[0] * (SETTLE_S - b.left) + fast[0] * b.left) / SETTLE_S  # the same projection as settle_end
        ahead = ends > 0
        told += f"; the window would end {ends:+.4g} if the spot stays where it is"
    return bool(loss and ahead), f"the sale would be a {'loss' if loss else 'gain'}; the spot over the target for the bet's side: {told}"
