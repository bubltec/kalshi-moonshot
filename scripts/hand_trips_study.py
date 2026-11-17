"""The user's own round trips, from the account's fills, described: how they were entered, how they were closed, and what each kind made.

User, 2026-11-17: "Yes, rebuild them" (the real hand round trips, to learn the scalp and the sale timing; "the middle of the market is the best time to scalp"; "we have to have a sale because total losses are devastating").
Reads data/tape_archive/trips.csv (scripts/hand_trips_dump.py, run on the host: trips.py's rebuild from raw fills, real prices and fees) and the replay's readings and views for the state at the buy. A trip is flat to a position and back to flat or to settlement, so several buys of one market before it is flat again are one trip.
A description of these trips. Nothing here is a tested rule.

    .venv/bin/python scripts/hand_trips_study.py
"""
import bisect
import csv
import datetime
import sys
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import risk_attribute_replay as R  # noqa: E402

ET = ZoneInfo("America/New_York")
N = R.NAMES
by = defaultdict(list)
for r in list(csv.reader(open(R.OUT)))[1:]:
    by[r[0]].append((int(r[1]), [float(v) for v in r[6:]]))
views = {(r[0], int(r[1])): tuple(float(v) for v in r[2:7]) for r in list(csv.reader(open(R.VIEWS)))[1:]}

trips = []
for r in csv.reader(open("data/tape_archive/trips.csv")):
    if len(r) < 16 or r[2] not in ("manual", "mixed") or "15M-" not in r[1]:
        continue
    tk, side, t0, t1, ep, xp, n, cost, pnl, fees, how, result, close, reliable = r[1], r[3], float(r[4]), float(r[5]), float(r[6]), float(r[7]), float(r[8]), float(r[9]), float(r[10]), float(r[11]), r[12], r[13], float(r[14]), r[15]
    if cost <= 0 or not close:
        continue
    sg = 1 if side == "yes" else -1
    d = dict(tk=tk, side=side, age=t0 - (close - 900.0), out_age=t1 - (close - 900.0), ep=ep, xp=xp, cost=cost, pnl=pnl, roi=pnl / cost, how=how, hold=t1 - t0, move=xp - ep,
             day=datetime.datetime.fromtimestamp(t0, ET).strftime("%m-%d"), room=None, nudge=None, cush=None, source=r[2])
    got = by.get(tk)
    if got:
        i = max(0, bisect.bisect_right([g[0] for g in got], t0) - 1)
        if abs(got[i][0] - t0) <= 75:
            x = got[i][1]
            d["nudge"], d["cush"] = sg * x[N.index("nudge")], sg * x[N.index("cushion")]
            v = views.get((tk, got[i][0]))
            if v:
                d["room"] = [(1.0 - p) if sg > 0 else p for p in v[:3]]
    trips.append(d)

days = sorted({t["day"] for t in trips})
print(f"window: the user's own round trips on 15-minute markets, opened {days[0]} to {days[-1]} ET, from the account's fills: {len(trips)} trips, ${sum(t['cost'] for t in trips):.0f} put in, ${sum(t['pnl'] for t in trips):+.2f} after every fee")
print("'$1 a trip' counts every trip the same, so a large stake cannot carry a row. Prices and fees are the real fills'.\n")


def line(label, sel):
    if not sel:
        return
    won = sum(1 for t in sel if t["pnl"] > 0)
    tot = sum(1 for t in sel if t["roi"] <= -0.90)
    print(f"  {label:34s} trips {len(sel):3d} | ahead {won:3d} ({100 * won / len(sel):3.0f}%) | total losses {tot:2d} | avg paid {np.mean([t['ep'] for t in sel]):.2f} | put in ${sum(t['cost'] for t in sel):6.0f} | made ${sum(t['pnl'] for t in sel):+8.2f} | $1 a trip {sum(t['roi'] for t in sel):+7.2f} | median hold {np.median([t['hold'] for t in sel]) / 60:4.1f} min")


def table(title, key):
    print(title)
    g = defaultdict(list)
    for t in trips:
        k = key(t)
        if k is not None:
            g[k].append(t)
    for k in sorted(g):
        line(str(k), g[k])
    print()


zone = lambda a: "1 first 3 minutes" if a < 180 else "2 3 to 10 minutes" if a < 600 else "3 the close (10 to 15)"  # noqa: E731
band = lambda p: "1 under 0.20" if p < 0.20 else "2 0.20 to 0.40" if p < 0.40 else "3 0.40 to 0.60" if p < 0.60 else "4 0.60 to 0.75" if p < 0.75 else "5 0.75 and over"  # noqa: E731
table("all", lambda t: "all")
table("how the trip was closed", lambda t: {"cash-out": "1 sold before the close", "settlement": "2 held to settlement", "mixed": "3 part sold, part held"}[t["how"]])
table("by the time zone of the buy", lambda t: zone(t["age"]))
table("sold before the close, by the time zone of the buy", lambda t: zone(t["age"]) if t["how"] == "cash-out" else None)
table("held to settlement, by the time zone of the buy", lambda t: zone(t["age"]) if t["how"] == "settlement" else None)
table("by the price paid", lambda t: band(t["ep"]))
table("sold before the close, by the price paid", lambda t: band(t["ep"]) if t["how"] == "cash-out" else None)
table("by day", lambda t: t["day"])
table("by stake", lambda t: "1 under $5" if t["cost"] < 5 else "2 $5 to $20" if t["cost"] < 20 else "3 $20 and over")
table("the side's price over the minute before the buy (the nudge, in typical minutes), where the tape has it", lambda t: None if t["nudge"] is None else "1 falling (under -0.5)" if t["nudge"] < -0.5 else "2 flat" if t["nudge"] < 0.5 else "3 rising (over 0.5)")
table("target placement in the hour view, turned to the side bought, where the tape has it", lambda t: None if t["room"] is None else "1 little room" if t["room"][2] < 1 / 3 else "2 middle" if t["room"][2] < 2 / 3 else "3 all the room")
table("target placement in the 15-minute view", lambda t: None if t["room"] is None else "1 little room" if t["room"][1] < 1 / 3 else "2 middle" if t["room"][1] < 2 / 3 else "3 all the room")

sold = [t for t in trips if t["how"] == "cash-out"]
up, down = [t for t in sold if t["move"] > 0], [t for t in sold if t["move"] <= 0]
print(f"THE SALES: {len(sold)} trips sold before the close")
if up:
    print(f"  sold higher than bought: {len(up)} trips, a median {100 * np.median([t['move'] for t in up]):+.0f} cents a contract (a quarter under {100 * np.percentile([t['move'] for t in up], 25):+.0f}, a quarter over {100 * np.percentile([t['move'] for t in up], 75):+.0f}), held a median {np.median([t['hold'] for t in up]) / 60:.1f} min, made ${sum(t['pnl'] for t in up):+.2f}")
if down:
    print(f"  sold lower than bought:  {len(down)} trips, a median {100 * np.median([t['move'] for t in down]):+.0f} cents a contract (a quarter under {100 * np.percentile([t['move'] for t in down], 25):+.0f}, a quarter over {100 * np.percentile([t['move'] for t in down], 75):+.0f}), held a median {np.median([t['hold'] for t in down]) / 60:.1f} min, made ${sum(t['pnl'] for t in down):+.2f}")
print(f"  sales made with this long left in the market: median {np.median([900 - t['out_age'] for t in sold]) / 60:.1f} min")
print("\nevery trip sold before the close (buy minute | side | paid -> sold | cents | held | stake | made):")
for t in sorted(sold, key=lambda t: (t["day"], t["age"])):
    print(f"  {t['day']} {t['tk'].split('-')[0]:14s} min {t['age'] / 60:4.1f} | {t['side']:3s} | {t['ep']:.2f} -> {t['xp']:.2f} | {100 * t['move']:+4.0f}c | {t['hold'] / 60:4.1f} min | ${t['cost']:6.2f} | ${t['pnl']:+7.2f}")
