"""The user's hand method, replayed on the tape as round trips, on YES and mirrored onto NO.

User, 2026-11-17: "trending up on a YES side (usually try to catch around 60-75 and cash out after 20c or higher profit) -- can do this multiple times in a single market. I find it harder to buy NOs ... you could help me there"; "target placement in 5, 15, hour views"; "time left, i already gave you these time zones" (the first 3 minutes, 3 to 10, the close).

One question on the same bets: a side whose price has been rising over the last minute and costs 0.60 to 0.75 at the ask is bought; it is sold the first time its bid is 20 cents or more over the price paid, and otherwise held to settlement. Against the same bets simply held. The fee is on every leg. A market can be bought again after a cash-out.
Fixed before the run, from the user's words: the 0.60 to 0.75 band, the 20 cents. Mine, said so: "rising" is the side's mid up over 0.5 of the market's typical minutes in the last minute (riskscore's nudge; the cut the hand-buy profile used); a buy is looked for at each 30 s mark.
Reads data/tape_archive (replay_readings.csv, replay_views.csv, the quotes), built by scripts/risk_attribute_replay.py build. Nothing on the host.

    .venv/bin/python scripts/hand_method_replay.py
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
LOW, HIGH, TAKE, RISING = 0.60, 0.75, 0.20, 0.5
N = R.NAMES
I_CUSH, I_NUDGE = N.index("cushion"), N.index("nudge")

rows = list(csv.reader(open(R.OUT)))[1:]
views = {(r[0], int(r[1])): (float(r[2]), float(r[3]), float(r[4])) for r in list(csv.reader(open(R.VIEWS)))[1:]}
rows.sort(key=lambda r: (r[0], int(r[1])))
quotes = R._quotes_15m()

trips = []
free_at: dict[tuple[str, int], float] = {}
for r in rows:
    tk, ts, age, y = r[0], int(r[1]), float(r[2]), int(r[3])
    bid, ask = float(r[4]), float(r[5])
    x = [float(v) for v in r[6:]]
    if not (0 < bid < ask < 1):
        continue
    for sg in (1, -1):
        price = ask if sg > 0 else 1.0 - bid
        if not (LOW <= price <= HIGH) or sg * x[I_NUDGE] <= RISING or ts < free_at.get((tk, sg), 0.0):
            continue
        qt, qb, qa = quotes[tk]
        a = int(np.searchsorted(qt, ts, side="right"))
        side_bid = qb[a:] if sg > 0 else 1.0 - qa[a:]
        hit = np.flatnonzero(side_bid >= price + TAKE - 1e-9)
        won = (y == 1) == (sg > 0)
        cost = price + fee(price)
        held = ((1.0 if won else 0.0) - cost) / cost
        if len(hit):
            s = float(side_bid[hit[0]])
            traded = (s - fee(min(s, 0.99)) - cost) / cost
            free_at[(tk, sg)] = float(qt[a + hit[0]])
            secs = float(qt[a + hit[0]]) - ts
        else:
            traded, secs = held, None
            free_at[(tk, sg)] = 1e18  # held to the end: no second buy of this side
        p5, p15, p60 = views.get((tk, ts), (0.5, 0.5, 0.5))
        # the views turned to the side bought: 1 = the target at the far edge of the view from where the side needs the index (all the room), 0 = the index has to cross the whole view
        room = [(1.0 - p) if sg > 0 else p for p in (p5, p15, p60)]
        trips.append(dict(side="YES" if sg > 0 else "NO", traded=traded, held=held, cashed=bool(len(hit)), won=won, age=age, price=price, secs=secs, cush=sg * x[I_CUSH], room=room,
                          day=datetime.datetime.fromtimestamp(ts, ET).strftime("%m-%d"), tk=tk))

trips = [t for t in trips if t["day"] != "11-08"]  # a part day
days = sorted({t["day"] for t in trips})
print(f"window: 2026-11-09 on, ET days {', '.join(days)}; {len(trips)} round trips on {len({t['tk'] for t in trips})} markets; whole market, no other gate")
print(f"the buy: a side at {LOW:.2f} to {HIGH:.2f} at the ask whose price rose over {RISING} typical minutes in the last minute. the sale: the first bid {TAKE:.2f} or more over the price paid, else held to settlement")
print("per dollar staked, the fee on every leg; 'if held' is the same buys never sold. Dollar totals are on $5 a buy\n")


def line(label, sel):
    if not sel:
        return
    tr, he = np.array([t["traded"] for t in sel]), np.array([t["held"] for t in sel])
    pd_ = defaultdict(float)
    for t in sel:
        pd_[t["day"]] += t["traded"]
    cashed = [t for t in sel if t["cashed"]]
    lost = sum(1 for t in sel if not t["cashed"] and not t["won"])
    print(f"  {label:36s} trips {len(sel):5d} | cashed out {100 * len(cashed) / len(sel):3.0f}% | held and won {sum(1 for t in sel if not t['cashed'] and t['won']):5d} | held and lost {lost:5d} ({100 * lost / len(sel):2.0f}%) | as traded {tr.mean():+.4f} (${5 * tr.sum():+8.2f}) | if held {he.mean():+.4f} (${5 * he.sum():+8.2f}) | days ahead {sum(v > 0 for v in pd_.values())} of {len(pd_)}")


def table(title, key, order=None):
    print(title)
    g = defaultdict(list)
    for t in trips:
        g[key(t)].append(t)
    for k in order or sorted(g):
        line(str(k), g.get(k, []))
    print()


table("all", lambda t: "all")
table("by side", lambda t: t["side"])
table("by day", lambda t: t["day"])
zone = lambda t: "1 first 3 minutes" if t["age"] < 180 else "2 3 to 10 minutes" if t["age"] < 600 else "3 the close (10 to 15)"  # noqa: E731
table("by the user's time zones", zone)
table("by side and time zone", lambda t: f"{t['side']:3s} {zone(t)}")
third = lambda v: "1 little room (under a third)" if v < 1 / 3 else "2 middle" if v < 2 / 3 else "3 all the room (over two thirds)"  # noqa: E731
for i, name in enumerate(("5-minute", "15-minute", "hour")):
    table(f"by the target's place in the {name} view, turned to the side bought (room = how far the index sits on the side's own side of the target, within the view)", lambda t, i=i: third(t["room"][i]))
table("by curve shape at the buy (the cushion zone)", lambda t: "1 behind or on the line (under 0.5)" if t["cush"] < 0.5 else "2 ahead 0.5 to 1.5" if t["cush"] < 1.5 else "3 ahead over 1.5")
table("by side and day", lambda t: f"{t['side']:3s} {t['day']}")
table("by side and the 15-minute view", lambda t: f"{t['side']:3s} {third(t['room'][1])}")
table("by side and the hour view", lambda t: f"{t['side']:3s} {third(t['room'][2])}")
cs = [t["secs"] for t in trips if t["cashed"]]
print(f"the cash-outs took a median of {np.median(cs):.0f} s (a quarter under {np.percentile(cs, 25):.0f} s, a quarter over {np.percentile(cs, 75):.0f} s)")
again = defaultdict(int)
for t in trips:
    again[(t["tk"], t["side"])] += 1
print(f"markets bought more than once on the same side: {sum(1 for v in again.values() if v > 1)} of {len(again)}")
