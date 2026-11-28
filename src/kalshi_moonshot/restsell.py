"""A gain-side wave sale rests at the ask instead of hitting the bid (lane field wave_rest_share; 2026-11-23, user: "make sure our exit is fee-less if possible").

When the exit step wave_take (the staircase's lower low or the spike trail), wave_salvage (its rebound sale, not the
give-up) or value_sale decides a sale that is a GAIN after both fees at the side's ask, the bet is not sold at the bid
at once. A post-only sell rests at the ask instead: an order that rests first is charged no trading fee (KalshiClient.
sell_resting), so the sale saves the exit fee (about 1.5c a contract) and half the spread. On every tick of the market
(the socket drives it, no timer):

  fills   PAPER: the market's bid reaches the order's price (the other side crosses it, the mirror of a resting buy that
          fills when the ask comes down to it). The sale is booked at the order's price with the reason starting
          manualrest.REST_PREFIX, so no taker fee and no slip is counted. LIVE: the account's fill on the socket.
  moves   the side's ask moved: the order is amended to the new ask in place (a sale may move either way).
  ends    the window is over, or the order's price would no longer be a gain after both fees: the order is withdrawn
          and the bid is taken, exactly as today (the step's own reason, with a note of the rest).
  stops   another exit decides (the give-up, a loss sale, any sale that would not itself rest): the order is withdrawn
          and that sale takes the bid.

The window is never fixed seconds: wave_rest_share x the seconds left to the close when the rest began (and never past
the last CURVE_MIN_LEFT seconds, where an order would land after the close). wave_rest_share = 0 (the default) is OFF.

"A gain after both fees" is judged as the codebase judges every gain (price - fee(price) > entry + fee(entry)), even
though a maker sale pays no fee: a price that would be a loss sold as a taker is never rested, and the booked result of
a resting fill is better than the test by the fee it does not pay.

This module is the state and its rules, in memory only (no database, no Kalshi call): watcher.py runs it on the tick
and places, amends and cancels the live order on its sell thread.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .manualrest import REST_PREFIX
from .strategy import fee_per_contract

REST_STEPS = ("wave take", "wave salvage", "value sale", "turn sale", "early take")  # the reasons of the gain-side sales that may rest; "wave give-up" (salvage's safety exit) and every other reason stay immediate taker sales
KEEP = 900  # what Store.mark_exited keeps of a reason (sellchain.KEEP)
MOVE = 0.0005  # a smaller change of the ask is no move (prices are on Kalshi's grid: a tenth of a cent at the finest)


@dataclass
class RestSell:
    reason: str  # the step's sale reason, as it decided it (its reading and its chain tag): kept so the sale can be traced
    price: float  # the order's price now, in the side's own terms (the side's ask)
    first: float  # the ask it first rested at
    started: float
    window: float  # seconds the rest may last: share x the seconds left when it began
    deadline: float
    left0: float  # seconds to the close when it began
    share: float
    live: bool = False
    moves: int = 0
    done: str = ""  # "" while resting; "filled" or "ended" once the sale has been handed to the sell thread
    order_id: str = ""  # LIVE: the exchange's order, set by the sell thread once placed
    placed: float = 0.0  # LIVE: the price the exchange's order sits at
    count: float = 0.0  # LIVE: the contracts the sale is for (the bet's whole position when the order was placed)
    filled: float = 0.0  # LIVE: contracts the socket's fills reported, over every order id this sale used
    base: float = 0.0  # LIVE: contracts filled on earlier orders of this sale (a cancel-and-place fallback starts a new order for the rest)
    ids: list = field(default_factory=list)  # LIVE: every order id this sale used
    pending: bool = False  # LIVE: a place or an amend is waiting on the sell thread (moves coalesce: it reads the latest price)
    premium: float = 0.0  # 2026-11-28 (user: "When profit exit, always put it a few cents above"): the order rests this far ABOVE the side's ask (lane field wave_rest_premium x the bid's own typical 10 s move); 0 = at the ask


def side_ask(side: str, yes_bid: float, yes_ask: float) -> float:
    """The ask of the side held, from a YES-book quote: YES's ask, or 1 - YES's bid for NO."""
    return float(yes_ask) if side == "yes" else 1.0 - float(yes_bid)


def is_gain(price: float, entry: float) -> bool:
    """A sale at `price` is a gain after both fees (the sale's and the buy's), as every gain is judged in this code."""
    return price - fee_per_contract(price) > entry + fee_per_contract(entry) + 1e-9


def eligible(reason: str) -> bool:
    """A sale of the kind that rests (a gain-side wave sale)."""
    return reason.startswith(REST_STEPS)


def target_price(ask: float, premium: float) -> float:
    """Where the order rests: the ask plus the premium, never at or over 0.99 (a sale at the top of the grid is no sale)."""
    return min(ask + max(premium, 0.0), 0.99)


def start(share: float, reason: str, ask: float, entry: float, now: float, close_ts: float, min_left: float, live: bool = False, premium: float = 0.0) -> RestSell | None:
    """A new resting sale for a step's sale, or None when it does not rest (the switch is off, the sale is not a gain-side
    wave sale, there is no ask, the ask is not a gain after both fees, or there is no window left)."""
    if share <= 0 or not eligible(reason) or not (0.0 < ask < 1.0) or entry <= 0 or not is_gain(ask, entry):
        return None
    left = close_ts - now
    deadline = min(now + share * left, close_ts - min_left)
    if left <= 0 or deadline <= now:
        return None
    price = target_price(ask, premium)
    return RestSell(reason=reason, price=price, first=price, started=now, window=deadline - now, deadline=deadline, left0=left, share=share, live=live, premium=premium)


def keeps(r: RestSell, reason: str, entry: float) -> bool:
    """While a sale rests, a gain-side sale the steps decide again is the same sale: the rest goes on. Anything else stops it."""
    return not r.done and eligible(reason) and is_gain(r.price, entry)


def tick(r: RestSell, bid: float, ask: float, entry: float, now: float) -> tuple[str, str]:
    """One tick of the market on a resting sale: ("filled", why) a paper fill, ("ended", why) the bid is to be taken,
    ("moved", note) the order follows the ask, ("", "") nothing. A live order fills by the account's fills, not here.
    Memory only."""
    if r.done:
        return "", ""
    if not r.live and bid > 0 and bid >= r.price - 1e-9:
        r.done = "filled"
        return "filled", filled_reason(r, now)
    if now >= r.deadline:
        r.done = "ended"
        return "ended", ended_reason(r, now, bid, "the window ended")
    if 0.0 < ask < 1.0 and abs(target_price(ask, r.premium) - r.price) >= MOVE:
        if not is_gain(ask, entry):
            r.done = "ended"
            return "ended", ended_reason(r, now, bid, f"the ask fell to {ask:.3f}, no longer a gain after both fees")
        was, want = r.price, target_price(ask, r.premium)
        r.price, r.moves = want, r.moves + 1
        return "moved", f"the ask moved to {ask:.3f}: the resting sale amended from {was:.3f} to {want:.3f} ({r.premium:.3f} above the ask; {r.moves} moves, {now - r.started:.0f}s of its {r.window:.0f}s window)"
    return "", ""


def _what(r: RestSell, now: float) -> str:
    return f"rested a sale at the ask {r.price:.3f} (first {r.first:.3f}, {r.moves} moves) for {now - r.started:.0f}s of its {r.window:.0f}s window ({r.share:g} x {r.left0:.0f}s left)"


def _fit(head: str, body: str, tail: str = "") -> str:
    """head + body + tail within KEEP characters: the body is cut in its middle, so its start and its end (the chain's tag) stay."""
    room = KEEP - len(head) - len(tail)
    if len(body) > room:
        keep_end = min(len(body), max(0, room // 2))
        body = body[: max(0, room - keep_end - 3)] + "..." + body[len(body) - keep_end:]
    return head + body + tail


def filled_reason(r: RestSell, now: float) -> str:
    """The sale reason of a resting fill. It starts with manualrest.REST_PREFIX and 'filled at <price>', the convention the paper exit (runner.exit_position) and the reports read as a fill at the order's own price with no taker fee."""
    return _fit(f"{REST_PREFIX} filled at {r.price:.4f}: {_what(r, now)}; the bid came up to it, no taker fee; the sale that rested: ", r.reason)


def ended_reason(r: RestSell, now: float, bid: float, why: str) -> str:
    """The step's own reason (its start is what the sale path reads), with a note that it rested and why the bid is taken."""
    return _fit("", r.reason, f" [{_what(r, now)}; {why}: the bid {bid:.2f} taken]")


def stopped_reason(r: RestSell, now: float, other: str) -> str:
    """Another exit's sale while a sale rested: its own reason first, then a note that the resting sale was withdrawn."""
    return _fit("", other, f" [the resting sale ({_what(r, now)}; it rested for: {r.reason[:120]}) was withdrawn: this exit takes the bid]")
