"""The user's low buy, replayed on the tape as round trips, on YES and mirrored onto NO.

User, 2026-11-17: "trending down on a YES side (low buy)"; 2026-11-11: sloping down, "wait until it starts to even out", then a resting (zero-fee) buy; wait for it to rise and sell before the peak; "cheap sides are for winning during the market". "I find it harder to buy NOs ... you could help me there."

One question on the same buys: a cheap side that has been falling and has just stopped falling is bought; is there a round trip in it?
The buy (looked for at each 30 s mark, a side bought again only after a take):
  cheap        the side costs 0.05 to 0.40 at the ask
  was falling  over the two minutes before the last one its mid fell at least one of the market's typical minutes in all (riskscore's nudge at the marks one and two minutes back, summed)
  evened out   over the last minute it did not fall (nudge at or over 0)
The same buys closed five ways, the fee on every leg: take at +10 cents; take at +20; take at +20 or out at the bid 5 minutes after the buy; never sold. And the user's own entry: a bid rested at the side's best bid for 60 s (no fee on the buy, as the bot treats a resting buy), counted as filled only when the side's ask came down to it, then the +20 take.
Fixed before the run. The user's: cheap, falling then evening out, sold on the way up, the resting buy. Mine: every number above (0.40, one typical minute, the take's cents, 5 minutes, 60 s). No loss sale on price: the rising-side run showed one is hit by ordinary wobble.
Reads data/tape_archive, built by scripts/risk_attribute_replay.py build. Nothing on the host.

    .venv/bin/python scripts/hand_low_buy_replay.py
"""
import csv
import datetime
import sys
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import risk_attribute_replay as R  # noqa: E402

from kalshi_moonshot.strategy import fee_per_contract as fee  # noqa: E402

ET = ZoneInfo("America/New_York")
LOW, HIGH, FELL, REST = 0.05, 0.40, 1.0, 60.0
N = R.NAMES
I_CUSH, I_NUDGE = N.index("cushion"), N.index("nudge")
NAMES = ["take +10", "take +20", "+20 or 5 min", "never sold", "rested, +20"]

rows = list(csv.reader(open(R.OUT)))[1:]
views = {(r[0], int(r[1])): tuple(float(v) for v in r[2:7]) for r in list(csv.reader(open(R.VIEWS)))[1:]}
rows.sort(key=lambda r: (r[0], int(r[1])))
nudge_at = {(r[0], int(r[1])): float(r[6 + I_NUDGE]) for r in rows}
quotes = R._quotes_15m()

trips = []
free_at: dict[tuple[str, int], float] = {}
for r in rows:
    tk, ts, age, y = r[0], int(r[1]), float(r[2]), int(r[3])
    bid, ask = float(r[4]), float(r[5])
    x = [float(v) for v in r[6:]]
    n1, n2 = nudge_at.get((tk, ts - 60)), nudge_at.get((tk, ts - 120))
    if not (0 < bid < ask < 1) or n1 is None or n2 is None:
        continue
    for sg in (1, -1):
        price, side_bid_now = (ask, bid) if sg > 0 else (1.0 - bid, 1.0 - ask)
        if not (LOW <= price <= HIGH) or sg * (n1 + n2) > -FELL or sg * x[I_NUDGE] < 0 or ts < free_at.get((tk, sg), 0.0):
            continue
        qt, qb, qa = quotes[tk]
        a = int(np.searchsorted(qt, ts, side="right"))
        sb = qb[a:] if sg > 0 else 1.0 - qa[a:]
        sa = qa[a:] if sg > 0 else 1.0 - qb[a:]
        times = qt[a:]
        won = (y == 1) == (sg > 0)
        cost = price + fee(price)
        held = ((1.0 if won else 0.0) - cost) / cost

        def sale(i: int, c: float) -> float:
            s = min(max(float(sb[i]), 0.01), 0.99)
            return (s - fee(s) - c) / c

        nets = {"never sold": held}
        t20 = None
        for name, take, limit in (("take +10", 0.10, None), ("take +20", 0.20, None), ("+20 or 5 min", 0.20, 300.0)):
            hit = np.flatnonzero(sb >= price + take - 1e-9)
            cands = [(int(hit[0]), "take")] if len(hit) else []
            if limit is not None:
                late = np.flatnonzero(times >= ts + limit)
                if len(late):
                    cands.append((int(late[0]), "time"))
            nets[name] = sale(min(cands)[0], cost) if cands else held
            if name == "take +20":
                t20 = int(hit[0]) if len(hit) else None
        # the resting buy: our bid at the side's best bid, filled only if the side's ask comes down to it inside REST seconds
        in_rest = times <= ts + REST
        fill = np.flatnonzero(in_rest & (sa <= side_bid_now + 1e-9))
        if len(fill) and side_bid_now > 0:
            f = int(fill[0])
            hit = np.flatnonzero(sb[f:] >= side_bid_now + 0.20 - 1e-9)
            if len(hit):
                s = min(float(sb[f + hit[0]]), 0.99)
                nets["rested, +20"] = (s - fee(s) - side_bid_now) / side_bid_now
            else:
                nets["rested, +20"] = ((1.0 if won else 0.0) - side_bid_now) / side_bid_now
        free_at[(tk, sg)] = float(times[t20]) if t20 is not None else 1e18
        p5, p15, p60, jump, eff = views.get((tk, ts), (0.5, 0.5, 0.5, 0.0, 0.0))
        trips.append(dict(side="YES" if sg > 0 else "NO", nets=nets, won=won, age=age, price=price, cush=sg * x[I_CUSH], room=[(1.0 - p) if sg > 0 else p for p in (p5, p15, p60)], tk=tk,
                          took=t20 is not None, secs=float(times[t20]) - ts if t20 is not None else None, day=datetime.datetime.fromtimestamp(ts, ET).strftime("%m-%d")))

trips = [t for t in trips if t["day"] != "11-08"]
days = sorted({t["day"] for t in trips})
print(f"window: 2026-11-09 on, ET days {', '.join(days)}; {len(trips)} buys on {len({t['tk'] for t in trips})} markets; whole market, no other gate")
print(f"the buy: a side at {LOW:.2f} to {HIGH:.2f} at the ask that fell at least {FELL:.0f} typical minute over the two minutes before the last, and did not fall in the last minute")
print("each cell: cents per dollar staked, the fee on every leg, and (days ahead of 8). 'rested, +20' counts only the buys whose resting bid filled (the share is in the last column). All settings fixed before the run\n")
tk20 = [t for t in trips if t["took"]]
print(f"reached +20 cents: {100 * len(tk20) / len(trips):.0f}% of buys; reached settlement as the winner: {100 * sum(t['won'] for t in trips) / len(trips):.0f}%; average price paid {np.mean([t['price'] for t in trips]):.2f}")
if tk20:
    cs = [t["secs"] for t in tk20]
    print(f"the +20 takes came a median of {np.median(cs):.0f} s after the buy (a quarter under {np.percentile(cs, 25):.0f} s, a quarter over {np.percentile(cs, 75):.0f} s)\n")


def line(label, sel):
    cells = []
    for nm in NAMES:
        got = [t for t in sel if nm in t["nets"]]
        if not got:
            cells.append("n/a")
            continue
        v = np.array([t["nets"][nm] for t in got])
        pd_ = defaultdict(float)
        for t in got:
            pd_[t["day"]] += t["nets"][nm]
        cells.append(f"{100 * v.mean():+7.2f} ({sum(u > 0 for u in pd_.values())})")
    filled = sum(1 for t in sel if "rested, +20" in t["nets"])
    print(f"  {label:38s} {len(sel):6d} | " + " | ".join(f"{c:>12s}" for c in cells) + f" | filled {100 * filled / len(sel):3.0f}%")


def table(title, key):
    print(title)
    print(f"  {'':38s} {'buys':>6s} | " + " | ".join(f"{n:>12s}" for n in NAMES) + " |")
    g = defaultdict(list)
    for t in trips:
        g[key(t)].append(t)
    for k in sorted(g):
        line(str(k), g[k])
    print()


zone = lambda t: "1 first 3 minutes" if t["age"] < 180 else "2 3 to 10 minutes" if t["age"] < 600 else "3 the close (10 to 15)"  # noqa: E731
third = lambda v: "1 little room (under a third)" if v < 1 / 3 else "2 middle" if v < 2 / 3 else "3 all the room (over two thirds)"  # noqa: E731
table("all", lambda t: "all")
table("by side", lambda t: t["side"])
table("by day", lambda t: t["day"])
table("by the price paid", lambda t: "1 0.05 to 0.15" if t["price"] < 0.15 else "2 0.15 to 0.25" if t["price"] < 0.25 else "3 0.25 to 0.40")
table("by the user's time zones", zone)
table("by side and time zone", lambda t: f"{t['side']:3s} {zone(t)}")
table("by side and price paid", lambda t: f"{t['side']:3s} " + ("1 0.05 to 0.15" if t["price"] < 0.15 else "2 0.15 to 0.25" if t["price"] < 0.25 else "3 0.25 to 0.40"))
for i, name in enumerate(("5-minute", "15-minute", "hour")):
    table(f"TARGET PLACEMENT, the {name} view, turned to the side bought (room: how far the index sits on the side's own side of the target, within the view)", lambda t, i=i: third(t["room"][i]))
table("by curve shape at the buy (how far the index is from the target, against the side, in typical moves)", lambda t: "1 on the line (within 0.5)" if t["cush"] > -0.5 else "2 behind 0.5 to 1.5" if t["cush"] > -1.5 else "3 behind over 1.5")
