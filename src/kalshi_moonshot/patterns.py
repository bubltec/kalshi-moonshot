"""What your larger wins (and larger losses) have in common. Read-only; it never changes a setting.

Every finished trade from the synced account history (see trips.py) is described by a handful of plain features: what
was traded, the price paid, how far into a 15-minute window it was bought, how the price had just moved, how big the
stake was, when in the day, how long it was held and how it ended. A "larger win" is a winner in the top quarter of
winners (at least $2); a "larger loss" is a loser in the bottom quarter. For each feature value the report shows how
often larger wins happen there against how often they happen overall (the lift) and how likely that count is by chance.

With dozens of trades most patterns are luck. The report says so, and marks a value as a lead only when it holds at least
five trades, shows a lift of 1.5 or more, and a chance probability under 5%. Even then, about one comparison in twenty
passes by luck alone, so treat a lead as something to keep watching.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .store import Store
from .trips import Trip, build_trips

TZ = ZoneInfo("America/New_York")
MIN_N = 5
MIN_LIFT = 1.5
ALPHA = 0.05


def _band(x: float, edges: tuple[float, ...], labels: tuple[str, ...]) -> str:
    for edge, label in zip(edges, labels, strict=False):
        if x < edge:
            return label
    return labels[-1]


def family(series: str) -> str:
    for names, label in (
        (("BTC", "ETH", "SOL", "XRP", "DOGE"), "crypto"),
        (("GOLD", "SILVER", "COPPER", "PLATINUM", "PALLADIUM"), "metals"),
        (("WTI", "BRENT", "NATGAS"), "energy"),
    ):
        if any(name in series for name in names):
            return label
    return "other"


def _pre_move(store: Store, t: Trip) -> str:
    """How the side's price moved in the three minutes before the buy (15-minute markets with stored candles)."""
    if not t.series.endswith("15M"):
        return "n/a"
    minute = int(t.opened_ts // 60) * 60

    def price(end_ts: int) -> float | None:
        r = store.db.execute("SELECT bid_c, ask_c FROM candles WHERE ticker=? AND end_ts=?", (t.ticker, end_ts)).fetchone()
        if not r or r["bid_c"] is None or r["ask_c"] is None:
            return None
        mid = (r["bid_c"] + r["ask_c"]) / 2
        return mid if t.side == "yes" else 1 - mid

    now, before = price(minute), price(minute - 180)
    if now is None or before is None:
        return "no data"
    move = now - before
    return "rising 5c+" if move >= 0.05 else "falling 5c+" if move <= -0.05 else "flat"


def features(store: Store, t: Trip) -> dict[str, str]:
    minute = t.minute_in_window(t.opened_ts)
    local = datetime.fromtimestamp(t.opened_ts, TZ)
    return {
        "market type": family(t.series),
        "series": t.series,
        "side bought": t.side.upper(),
        "price paid": _band(t.entry_price, (0.15, 0.30, 0.50, 0.70), ("under 15c", "15-30c", "30-50c", "50-70c", "70c+")),
        "minute of the window": "n/a" if minute is None else _band(
            minute, (3, 6, 9, 12), ("minutes 0-3", "minutes 3-6", "minutes 6-9", "minutes 9-12", "minutes 12-15")),
        "price just before buying": _pre_move(store, t),
        "stake": _band(t.cost, (2, 5, 15), ("under $2", "$2-5", "$5-15", "$15+")),
        "time of day (ET)": _band(local.hour, (6, 12, 18), ("midnight-6am", "6am-noon", "noon-6pm", "6pm-midnight")),
        "held": _band(t.hold_minutes, (3, 8, 15), ("under 3 min", "3-8 min", "8-15 min", "15+ min")),
        "how it ended": t.exit_type,
    }


def binom_tail(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p): how likely at least k larger wins is by chance in a group of n trades."""
    if k <= 0:
        return 1.0
    return min(1.0, sum(math.comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k, n + 1)))


@dataclass(frozen=True)
class Line:
    feature: str
    value: str
    n: int
    hits: int  # larger wins (or larger losses) in this group
    pnl: float
    lift: float
    chance: float  # probability of at least this many hits by luck
    lead: bool


@dataclass(frozen=True)
class Report:
    n: int
    wins: int
    losses: int
    win_cut: float
    loss_cut: float
    big_wins: int
    big_losses: int
    pnl: float
    win_lines: list[Line]
    loss_lines: list[Line]


def _lines(rows: list[tuple[Trip, dict[str, str]]], hit, base: float) -> list[Line]:
    groups: dict[tuple[str, str], list[Trip]] = defaultdict(list)
    for trip, feats in rows:
        for key, value in feats.items():
            groups[(key, value)].append(trip)
    out = []
    for (key, value), g in groups.items():
        if len(g) < MIN_N or value in ("n/a", "no data"):
            continue
        hits = sum(1 for t in g if hit(t))
        rate = hits / len(g)
        lift = rate / base if base else float("nan")
        chance = binom_tail(hits, len(g), base)
        out.append(Line(key, value, len(g), hits, sum(t.pnl for t in g), lift, chance,
                        lift >= MIN_LIFT and chance < ALPHA and hits >= 2))
    out.sort(key=lambda x: (not x.lead, -x.lift, -x.n))
    return out


def analyze(store: Store, source: str | None = "manual") -> Report | None:
    done = [t for t in build_trips(store) if t.done and t.reliable and (source in (None, "all") or t.source == source)]
    wins = sorted(t.pnl for t in done if t.pnl > 0)
    losses = sorted(t.pnl for t in done if t.pnl < 0)
    if len(done) < 20 or len(wins) < 8 or len(losses) < 8:
        return None
    win_cut = max(2.0, wins[int(len(wins) * 0.75)])
    loss_cut = min(-2.0, losses[int(len(losses) * 0.25)])
    rows = [(t, features(store, t)) for t in done]
    big_w = sum(1 for t in done if t.pnl >= win_cut)
    big_l = sum(1 for t in done if t.pnl <= loss_cut)
    return Report(
        len(done), len(wins), len(losses), win_cut, loss_cut, big_w, big_l, sum(t.pnl for t in done),
        _lines(rows, lambda t: t.pnl >= win_cut, big_w / len(done)),
        _lines(rows, lambda t: t.pnl <= loss_cut, big_l / len(done)),
    )


def render(r: Report | None, source: str = "manual") -> str:
    if r is None:
        return "Not enough finished trades yet to look for patterns (needs 20, with at least 8 wins and 8 losses).\n"
    out = [
        f"# Patterns in your larger wins and losses ({source} trades)\n",
        f"{r.n} finished trades: {r.wins} won, {r.losses} lost, net {r.pnl:+,.2f}.",
        f"A larger win is +${r.win_cut:.2f} or more ({r.big_wins} trades). A larger loss is -${abs(r.loss_cut):.2f} or worse ({r.big_losses} trades).\n",
        "Lift 2.0 means larger wins are twice as common in that group as overall. 'Chance' is how likely that many larger wins "
        "would be by luck. A LEAD needs 5+ trades, lift 1.5+ and chance under 5%; one comparison in twenty passes by luck alone.\n",
    ]

    def section(title: str, lines: list[Line], label: str) -> None:
        out.append(f"## {title}\n")
        leads = [x for x in lines if x.lead]
        if not leads:
            out.append("No group stands out beyond luck yet. The strongest few (not leads), for reference:\n")
        else:
            out.append("Leads:\n")
        shown = leads or lines[:5]
        for x in shown:
            mark = "LEAD " if x.lead else "     "
            out.append(f"- {mark}{x.feature}: {x.value}: {x.hits} of {x.n} are {label} (lift {x.lift:.1f}x, chance {x.chance:.0%}), net {x.pnl:+,.2f}")
        out.append("")

    section("Larger wins", r.win_lines, "larger wins")
    section("Larger losses", r.loss_lines, "larger losses")
    return "\n".join(out) + "\n"
