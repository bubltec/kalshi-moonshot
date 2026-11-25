"""A resting buy is re-judged on every quote while it waits (2026-11-24, user: "Does it keep calculating each tick while it's waiting for the buy? It needs to so it's evaluating logic and updating or removing the buy", then "Yes").

Until this, a resting buy only followed a falling ask down (`buy_rest_below` under it, never up, at most three moves) and was withdrawn only under the lane's price floor: the lane's buy module was asked once, at the decision, and an order whose setup had gone stayed on the book until it filled or its time ran out. Now, on a lane that sets `buy_rest_follow`, each streamed quote on the order's market asks the lane's buy module again, for that market and that side, at the side's ask as the quote shows it, from memory only (the tick store, the bet book, the watchlist; `memguard` catches a table read or a Kalshi GET inside it, as it does in every decision). One verdict per quote:

  keep    the module still buys and the price it wants is the order's price (within one tick of Kalshi's price grid): nothing is sent, the order keeps its place in the queue.
  move    the module still buys and wants a different price (lower or higher, toward the ask): the order is amended in place (runner._rest_follow; paper records the move).
  cancel  a step of the module now says no, or the ask fell under the lane's price floor: the order is withdrawn. The verdict names the step and carries the step's own words, as the decision's note does ("out at <step> (<why>)").

The price a step wants comes from a PRICER, chosen by the lane's settings (pricer_for): `rest_below` (`buy_rest_below` under the ask, or under the lower of the ask and its recent average with `buy_rest_avg_seconds`, restprice.py) or `rest_at_bid` (the side's bid, lane field `buy_rest_at_bid`). Both are put on Kalshi's tick grid the way the order is sent (kalshi.book_price) and kept under the ask (a post-only buy at the ask is refused). A later step that sets its own price (a slope-based price, a separate design) is added to PRICERS and named in pricer_for; nothing else here changes.

The live order thread (runner._rest_wait) and the paper judge (runner._paper_rest_wait, then paperfill.settle) ask the same function, so paper and live agree.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any, NamedTuple

from . import memguard, restprice, steptime
from .kalshi import book_price


class Verdict(NamedTuple):
    action: str  # "keep", "move" or "cancel"
    price: float  # where the order should sit (the order's own price for keep and cancel)
    step: str  # the step that said no (cancel), or the pricer that set the price (keep, move)
    why: str  # the step's own words (cancel: "out at <step> (<why>)"), or the reading that moved it
    ask: float | None  # the side's ask the verdict read


def tick(price: float) -> float:
    """One step of Kalshi's price grid at this price: the step kalshi.snap_price (so kalshi.book_price) rounds to, a tenth of a cent under 10c and over 90c, a cent between. Used only to say whether two prices are a whole step apart."""
    p = round(price, 6)
    return 0.01 if 0.10 - 1e-9 <= p <= 0.90 + 1e-9 else 0.001


def on_grid(price: float, side: str, ask: float | None = None) -> float:
    """A buy of `side` at its own price, put on Kalshi's grid the way the order is sent (kalshi.book_price: a YES buy is a bid on the YES book and rounds down; a NO buy is an ask at 1 - price and rounds up, so its NO price rounds down): never worse than meant. With `ask` given it is kept at least one step under that ask (a post-only buy at the ask is refused)."""

    def snap(p: float) -> float:
        p = min(0.999, max(0.001, p))
        return round(book_price(p, "bid") if side == "yes" else 1.0 - book_price(1.0 - p, "ask"), 4)

    out = snap(price)
    if ask is not None and out >= ask - 1e-9:
        out = snap(ask - tick(ask))
        if out >= ask - 1e-9:
            out = snap(ask - tick(ask) - 1e-6)
    return out


def differs(a: float, b: float) -> bool:
    """Whether two prices are at least one tick apart (a move costs the order its place in the queue, so a smaller change is not made)."""
    return abs(a - b) >= tick(min(a, b)) - 1e-9


def rest_below(spec: Any, side: str, ask: float, bid: float | None, rows: Sequence[Sequence[float]], now: float) -> float | None:
    """The `rest_below` pricer: buy_rest_below under the ask, or under the lower of the ask and its recent average (restprice.base_ask), on the grid and under the ask."""
    return on_grid(restprice.base_ask(spec, side, ask, rows, now) - spec.buy_rest_below, side, ask)


def rest_at_bid(spec: Any, side: str, ask: float, bid: float | None, rows: Sequence[Sequence[float]], now: float) -> float | None:
    """The `rest_at_bid` pricer (lane field buy_rest_at_bid): the order sits on the side's bid, on the grid and under the ask. No bid, no price (the order is kept where it is)."""
    if bid is None or bid <= 0:
        return None
    return on_grid(bid, side, ask)


PRICERS: dict[str, Callable[[Any, str, float, float | None, Sequence[Sequence[float]], float], float | None]] = {"rest_below": rest_below, "rest_at_bid": rest_at_bid}


def pricer_for(spec: Any) -> str:
    """The pricer the lane's settings name: on the bid with buy_rest_at_bid, else under the ask. A later price step (a slope-based price) goes into PRICERS and is named here."""
    return "rest_at_bid" if getattr(spec, "buy_rest_at_bid", False) else "rest_below"


def want_price(spec: Any, side: str, ask: float, rows: Sequence[Sequence[float]], now: float, bid: float | None = None) -> float | None:
    """Where the order should sit at this quote, by the lane's pricer (None when it cannot say)."""
    return PRICERS[pricer_for(spec)](spec, side, ask, bid, rows, now)


def _clean(text: str) -> str:
    """A step's words with no square brackets, so the note's own tags (runner._TAG) stay whole."""
    return text.replace("[", "(").replace("]", ")")


def recheck(s: Any, spec: Any, shot: Any, price: float, bid: float | None, ask: float | None, now: float | None = None, *,
            p_yes: float | None = None, store: Any = None, why: str = "", rows: Sequence[Sequence[float]] = (), chain: bool = True) -> Verdict:
    """The verdict on one resting buy at one quote. `shot` is the market and side as decided; `bid` and `ask` are the side's own prices now. `p_yes` and `why` are the decision's estimate and the estimator's note (a step that needs the estimate reads the one the decision had: the estimator is not asked again per quote). `rows` are the quote memory's (time, YES bid, YES ask) rows for the average-ask start. `chain` False asks only the price (a lane with no module).
    Memory only, timed against the 60 ms budget (steptime kind "buy rest recheck")."""
    from . import buychain

    now = time.time() if now is None else now
    started = time.perf_counter()
    try:
        with memguard.decision("buy"):
            if ask is None or ask <= 0:
                return Verdict("keep", price, pricer_for(spec), "the book shows no ask", None)
            left = _hours_left(shot, now)
            fresh = replace(shot, ask=round(ask, 4), bid=round(bid, 4) if bid is not None and bid > 0 else shot.bid, hours_to_close=left)
            order = tuple(getattr(spec, "buy_chain", ()) or ())
            if chain and order:
                trail = buychain.walk(buychain.Ctx(s, spec, p_yes, now=now, store=store, why=why, resting=True), fresh, order)
                if not trail.alive:
                    name, check = trail.said[-1]
                    return Verdict("cancel", price, name, f"out at {name} ({_clean(check.why)})", ask)
            if ask < spec.price[0] - 1e-9:
                return Verdict("cancel", price, "price floor", f"the {shot.side} ask {ask:.3f} is under the lane's {spec.price[0]:g} floor", ask)
            name = pricer_for(spec)
            want = want_price(spec, shot.side, ask, rows, now, bid=bid)
            if want is None or not differs(want, price):
                return Verdict("keep", price, name, f"ask {ask:.3f}, the order at {price:.3f} is where the price step ({name}) wants it", ask)
            return Verdict("move", want, name, f"ask {ask:.3f}, bid {bid or 0:.3f}: the price step ({name}) wants {want:.3f} (was {price:.3f})", ask)
    finally:
        steptime.tick_record("buy rest recheck", (time.perf_counter() - started) * 1000.0)


def _hours_left(shot: Any, now: float) -> float:
    from datetime import UTC, datetime

    from .strategy import _hours_until

    hours = _hours_until(shot.close_time, datetime.fromtimestamp(now, UTC)) if getattr(shot, "close_time", None) else None
    return shot.hours_to_close if hours is None else max(0.0, hours)


Judge = Callable[[float, "float | None", "float | None", float], Verdict]


def judge_for(s: Any, spec: Any, shot: Any, *, p_yes: float | None = None, store: Any = None, why: str = "") -> Judge:
    """The per-quote judge of one resting buy: (order price, side bid, side ask, now) -> Verdict, with the decision's context bound. Reads the quote memory for the average-ask start only when the lane sets buy_rest_avg_seconds."""
    from . import ticks as ticks_mod

    def judge(price: float, bid: float | None, ask: float | None, now: float) -> Verdict:
        rows = ticks_mod.mids.quotes(shot.ticker) if getattr(spec, "buy_rest_avg_seconds", 0) > 0 else ()
        return recheck(s, spec, shot, price, bid, ask, now, p_yes=p_yes, store=store, why=why, rows=rows)

    return judge


# ---- the paper trail: what the per-quote judge did to a paper resting buy, written on the decision and read by paperfill.settle ----
TRAIL = "[rest-follow"
_TRAIL_RE = re.compile(r"\[rest-follow: moves ([0-9.@ ]+?)(?:; withdrawn@([0-9.]+) (.*?))?\]")


def trail_mark(moves: Sequence[tuple[float, float]], started: float, withdrawn: tuple[float, str] | None) -> str:
    """The trail as one tag: every place the order sat ("price@seconds after it was placed") and, when it was withdrawn, when and why."""
    mv = " ".join(f"{p:.4f}@{max(0.0, t - started):.2f}" for t, p in moves)
    out = f"{TRAIL}: moves {mv}"
    if withdrawn is not None:
        out += f"; withdrawn@{max(0.0, withdrawn[0] - started):.2f} {_clean(withdrawn[1])}"
    return out + "]"


def parse_trail(reason: str, started: float) -> tuple[list[tuple[float, float]], tuple[float, str] | None] | None:
    """(moves as (time, price), (withdrawn time, why) or None) from a decision's note, or None when it has no trail."""
    m = _TRAIL_RE.search(reason or "")
    if not m:
        return None
    moves = []
    for part in m.group(1).split():
        p, _, t = part.partition("@")
        moves.append((started + float(t), float(p)))
    if not moves:
        return None
    withdrawn = (started + float(m.group(2)), m.group(3)) if m.group(2) else None
    return moves, withdrawn
