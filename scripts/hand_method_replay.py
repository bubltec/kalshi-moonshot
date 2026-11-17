"""The user's hand method, replayed on the tape as round trips, on YES and mirrored onto NO.

User, 2026-11-17: "trending up on a YES side (usually try to catch around 60-75 and cash out after 20c or higher profit) -- can do this multiple times in a single market. I find it harder to buy NOs ... you could help me there"; "target placement in 5, 15, hour views"; "spot history ... to see if the spot is likely to perform another jump, dive, or stable trend/waves"; "time left, i already gave you these time zones" (the first 3 minutes, 3 to 10, the close).
On the loss side, the same day: "we should have a tighter loss structure. we don't want to waste time waiting for losers. especially on mid-market buys, we either catch a profit or try to cash out with as little loss as possible."

One question on the same buys: a side whose price has been rising over the last minute and costs 0.60 to 0.75 at the ask is bought. The same buys are then closed six ways and compared (the fee on every leg):
  take only        sold the first time the bid is 20 cents over the price paid, else held to settlement
  -20 / -10 / -5   the take, or a loss sale the first time the bid is that many cents under the price paid, whichever comes first
  -10, 5 min       the take, the 10 cent loss sale, or, if neither has happened 5 minutes after the buy, sold at the bid then
  3 min / 5 min    the take, or, if it has not come that long after the buy, sold at the bid then; no loss sale on price (added after the first run showed a price stop is hit by ordinary wobble: 83% of buys at 5 cents)
  never sold       held to settlement
Fixed before the run. The user's: the 0.60 to 0.75 band, the 20 cent take, "tighter" and "do not wait". Mine: "rising" is the side's mid up over 0.5 of the market's typical minutes in the last minute; the loss sale's cents (20 as the take's mirror, then 10 and 5); the 5 minutes (three quarters of the cash-outs came inside 385 s on the first run). A buy is looked for at each 30 s mark; a side is bought again only after a cash-out at the take.
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
EXITS = [("take only", None, None), ("-20", 0.20, None), ("-10", 0.10, None), ("-5", 0.05, None), ("-10, 5 min", 0.10, 300.0), ("3 min", None, 180.0), ("5 min", None, 300.0)]
N = R.NAMES
I_CUSH, I_NUDGE = N.index("cushion"), N.index("nudge")

rows = list(csv.reader(open(R.OUT)))[1:]
vrows = list(csv.reader(open(R.VIEWS)))[1:]
views = {(r[0], int(r[1])): tuple(float(v) for v in r[2:7]) for r in vrows}
J_LO, J_HI = np.quantile([float(r[5]) for r in vrows], [1 / 3, 2 / 3])  # thirds of the whole tape, not tuned
E_LO, E_HI = np.quantile([abs(float(r[6])) for r in vrows], [1 / 3, 2 / 3])
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
        times = qt[a:]
        won = (y == 1) == (sg > 0)
        cost = price + fee(price)
        held = ((1.0 if won else 0.0) - cost) / cost
        hit = np.flatnonzero(side_bid >= price + TAKE - 1e-9)
        first_take = int(hit[0]) if len(hit) else None

        def sale(i: int) -> float:
            s = min(max(float(side_bid[i]), 0.01), 0.99)
            return (s - fee(s) - cost) / cost

        nets, how = {}, {}
        for name, stop, limit in EXITS:
            cands = []
            if first_take is not None:
                cands.append((first_take, "take"))
            if stop is not None:
                cut = np.flatnonzero(side_bid <= price - stop + 1e-9)
                if len(cut):
                    cands.append((int(cut[0]), "loss sale"))
            if limit is not None:
                late = np.flatnonzero(times >= ts + limit)
                if len(late):
                    cands.append((int(late[0]), "time"))
            if cands:
                i, why = min(cands)
                nets[name], how[name] = sale(i), why
            else:
                nets[name], how[name] = held, "held"
        free_at[(tk, sg)] = float(times[first_take]) if first_take is not None else 1e18
        p5, p15, p60, jump, eff = views.get((tk, ts), (0.5, 0.5, 0.5, 0.0, 0.0))
        room = [(1.0 - p) if sg > 0 else p for p in (p5, p15, p60)]  # 1 = the index sits at the far edge of the view on the side's own side of the target
        feats = [v if j == 8 else sg * v for j, v in enumerate(x)] + room + [jump, sg * eff, age / 900.0, price]  # the fifteen readings turned to the side bought, the three views, the two spot-history measures, the time gone, the price
        trips.append(dict(feats=feats, side="YES" if sg > 0 else "NO", nets=nets, how=how, held=held, won=won, age=age, price=price, cush=sg * x[I_CUSH], room=room, tk=tk,
                          secs=float(times[first_take]) - ts if first_take is not None else None,
                          day=datetime.datetime.fromtimestamp(ts, ET).strftime("%m-%d"),
                          shape="3 a straight run (top third)" if abs(eff) >= E_HI else "1 waves (bottom third)" if abs(eff) < E_LO else "2 between",
                          way="toward the side" if sg * eff > 0 else "away from the side",
                          jumpy="3 jumps in the hour (top third)" if jump >= J_HI else "1 a calm hour (bottom third)" if jump < J_LO else "2 between"))

trips = [t for t in trips if t["day"] != "11-08"]  # a part day
days = sorted({t["day"] for t in trips})
print(f"window: 2026-11-09 on, ET days {', '.join(days)}; {len(trips)} buys on {len({t['tk'] for t in trips})} markets; whole market, no other gate")
print(f"the buy: a side at {LOW:.2f} to {HIGH:.2f} at the ask whose price rose over {RISING} typical minutes in the last minute")
print("each cell: cents per dollar staked, the fee on every leg, and (days ahead of 8). The same buys under each exit; see the top of scripts/hand_method_replay.py for the exits. All settings fixed before the run\n")
names = [e[0] for e in EXITS]


def line(label, sel):
    if not sel:
        return
    cells = []
    for nm in names:
        v = np.array([t["nets"][nm] for t in sel])
        pd_ = defaultdict(float)
        for t in sel:
            pd_[t["day"]] += t["nets"][nm]
        cells.append(f"{100 * v.mean():+6.2f} ({sum(u > 0 for u in pd_.values())})")
    he = np.array([t["held"] for t in sel])
    print(f"  {label:38s} {len(sel):6d} | " + " | ".join(f"{c:>11s}" for c in cells) + f" | {100 * he.mean():+6.2f}")


def table(title, key):
    print(title)
    print(f"  {'':38s} {'buys':>6s} | " + " | ".join(f"{n:>11s}" for n in names) + " | never sold")
    g = defaultdict(list)
    for t in trips:
        g[key(t)].append(t)
    for k in sorted(g):
        line(str(k), g[k])
    print()


print("how each exit's buys ended (share of buys)")
for nm in names:
    c = defaultdict(int)
    for t in trips:
        c[t["how"][nm]] += 1
    v = np.array([t["nets"][nm] for t in trips])
    by = {k: np.mean([t["nets"][nm] for t in trips if t["how"][nm] == k]) for k in c}
    print(f"  {nm:12s} " + ", ".join(f"{k} {100 * c[k] / len(trips):.0f}% (avg {100 * by[k]:+.0f}c per $)" for k in ("take", "loss sale", "time", "held") if k in c) + f" | all {100 * v.mean():+.2f}c per $, ${5 * v.sum():+.0f} on $5 a buy")
print()
zone = lambda t: "1 first 3 minutes" if t["age"] < 180 else "2 3 to 10 minutes" if t["age"] < 600 else "3 the close (10 to 15)"  # noqa: E731
third = lambda v: "1 little room (under a third)" if v < 1 / 3 else "2 middle" if v < 2 / 3 else "3 all the room (over two thirds)"  # noqa: E731
table("all", lambda t: "all")
table("by side", lambda t: t["side"])
table("by day", lambda t: t["day"])
table("by the user's time zones", zone)
table("by side and time zone", lambda t: f"{t['side']:3s} {zone(t)}")
for i, name in enumerate(("5-minute", "15-minute", "hour")):
    table(f"TARGET PLACEMENT, the {name} view, turned to the side bought (room: how far the index sits on the side's own side of the target, within the view)", lambda t, i=i: third(t["room"][i]))
table("by side and the hour view", lambda t: f"{t['side']:3s} {third(t['room'][2])}")
table("SPOT HISTORY: the index's last 15 minutes, waves or a straight run (net move over the path travelled; thirds of the tape)", lambda t: t["shape"])
table("the same, with which way it pointed", lambda t: f"{t['shape'][:2]}{t['way']:19s}{t['shape'][1:]}")
table("SPOT HISTORY: jumps or dives in the last hour (the largest one-minute move, in typical minutes; thirds of the tape)", lambda t: t["jumpy"])
table("by side and jumps in the hour", lambda t: f"{t['side']:3s} {t['jumpy']}")
table("by curve shape at the buy (the cushion zone)", lambda t: "1 behind or on the line (under 0.5)" if t["cush"] < 0.5 else "2 ahead 0.5 to 1.5" if t["cush"] < 1.5 else "3 ahead over 1.5")
table("NO side, all the room in the hour view, by time zone", lambda t: zone(t) if (t["side"] == "NO" and t["room"][2] >= 2 / 3) else "(everything else)")
cs = [t["secs"] for t in trips if t["secs"] is not None]
print(f"the takes came a median of {np.median(cs):.0f} s after the buy (a quarter under {np.percentile(cs, 25):.0f} s, a quarter over {np.percentile(cs, 75):.0f} s)")


# THE SCORE AS A SELECTOR (user, 2026-11-17: "the thing is we don't want to take every one. this is what i wanted the risk score to do").
# Not "will the side win" against Kalshi's price: among the moments that fit the pattern, which ones reach the take before the loss sale. Fit on the pattern's own earlier buys (every day before), scored on the next day; the buys are then put in fifths by the score, the cuts taken from the earlier days.
print("\nTHE SCORE AS A SELECTOR: among the pattern's buys, which reach +20 before the loss sale at -20. Walk-forward: fit on the days before, scored on the next day; fifths by score, the cuts from the earlier days")
SEL = "-20"
F = np.array([t["feats"] for t in trips])
mu, sd = F.mean(0), F.std(0) + 1e-9
Z = np.c_[np.ones(len(F)), (F - mu) / sd]
yv = np.array([1.0 if t["how"][SEL] == "take" else 0.0 for t in trips])
netv = np.array([t["nets"][SEL] for t in trips])
dayv = np.array([t["day"] for t in trips])
buckets = defaultdict(list)
for k in range(2, len(days)):
    tr, te = np.isin(dayv, days[:k]), dayv == days[k]
    w = R._fit(Z[tr], yv[tr], ridge=1.0)
    cuts = np.quantile(Z[tr] @ w, [0.2, 0.4, 0.6, 0.8])
    for i in np.flatnonzero(te):
        buckets[int(np.searchsorted(cuts, Z[i] @ w))].append((days[k], yv[i], netv[i]))
print(f"  {'fifth by score':26s} {'buys':>6s} | reached the take | cents per $ | days ahead of {len(days) - 2}")
for b in range(5):
    got = buckets[b]
    pd_ = defaultdict(float)
    for d, _, v in got:
        pd_[d] += v
    print(f"  {['1 lowest', '2', '3', '4', '5 highest'][b]:26s} {len(got):6d} | {100 * np.mean([g[1] for g in got]):15.0f}% | {100 * np.mean([g[2] for g in got]):+11.2f} | {sum(v > 0 for v in pd_.values())}")
allg = [g for b in buckets.values() for g in b]
print(f"  {'all (the same days)':26s} {len(allg):6d} | {100 * np.mean([g[1] for g in allg]):15.0f}% | {100 * np.mean([g[2] for g in allg]):+11.2f} |")
names_f = list(N) + ["room 5 min", "room 15 min", "room hour", "jumps in the hour", "run toward the side", "time gone", "price paid"]
w_all = R._fit(Z, yv, ridge=1.0)
order = np.argsort(-np.abs(w_all[1:]))
print("  what the fit leans on (all days, standardized weights, largest first): " + ", ".join(f"{names_f[j]} {w_all[1 + j]:+.2f}" for j in order[:8]))
