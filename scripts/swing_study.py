"""Trade the swings of the curve, not the settlement (2026-11-11, user: "we're not trying to predict the completed outcome, kalshi already does that, we're trying to model the curve so we can make wins out of it in multiple ways. betting in dips and peaks on the opposite side (low) when history says it will likely come back the other way").
The studies of 11-10 and 11-11 scored readings against the settled result or against holding to it. This one scores a round trip in the contract's own price.
Question: after the spot makes a fast move of D typical minutes over W seconds inside a 15-minute window, does buying the side that just got cheaper and selling it on the rebound pay, after the spread and the fees?
  Entry: the first second in minutes 1 to 12 of the window where the move over the last W s is at least D typical minutes (rms one-second move of the last 900 s times sqrt(60)) against a side and that side's ask is in 0.10-0.60. A dip (spot fell) buys YES at the YES ask; a peak (spot rose) buys NO at 1 - YES bid. One entry a window. Taker fee on the buy.
  Exit: a sell resting at the entry price + g fills when that side's bid reaches it (no fee on a resting fill, as the lanes' resting orders show; an assumption for sells); otherwise the position is sold at the bid after T minutes, or 30 s before the close if that comes first (taker fee). Compared with holding the same entry to settlement.
  State at entry, for the breakdowns (curve readings only, rule 1): the hour's path efficiency (net move over path, 10 s steps, 60 minutes before the entry) in terciles of the sample, the price paid, and the minutes left.
Grid fixed before results: D 1.0 / 1.5 / 2.0 / 3.0; W 60 / 120 s; g 0.05 / 0.10 / 0.15; T 2 / 4 / 8 minutes. Per setting: entries, share that reached the target, net per contract and in total, the two halves by time, and the same entries held to settlement. Walk-forward: each UTC day uses the setting with the best total on earlier days (at least 3 days), and stands down when that total is not positive.
    scripts/cloud_job.sh scripts/swing_study.py STRIDE=3
"""

import bisect
import calendar
import collections
import math
import os
import time as _time

from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store

os.nice(10)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
STRIDE = int(os.environ.get("STRIDE", "3"))
SINCE_S = int(os.environ.get("SINCE_S", str(calendar.timegm((2026, 10, 28, 0, 0, 0)))))
from kalshi_moonshot.strategy import fee_per_contract as fee

DS = (1.0, 1.5, 2.0, 3.0)
WS = (60, 120)
GS = (0.05, 0.10, 0.15)
TS = (2, 4, 8)
wins = st.db.execute("SELECT ticker, close_ts, strike, result FROM windows WHERE result IN ('yes','no') AND close_ts > ? ORDER BY close_ts", (SINCE_S,)).fetchall()
E = collections.defaultdict(list)  # (kind, D, W) -> entries: dict(ts, day, eff, price, left, hold, exits{(g,T): pnl}, hit{(g,T): bool})
used = 0
t_start = _time.time()
for k, w in enumerate(wins):
    ser = w["ticker"].split("-")[0]
    crypto = variants.crypto_index(ser)
    ix = crypto or variants.biquote_index(ser)
    if not ser.endswith("15M") or not ix or k % STRIDE:
        continue
    close = float(w["close_ts"])
    tk = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts", (ix, close - 4520, close)).fetchall()
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (w["ticker"], close - 900, close)).fetchall()
    if len(tk) < 2500 or len(q) < 30 or float(tk[0][0]) > close - 4450:
        continue
    T_ = [float(x[0]) for x in tk]
    base = int(close) - 4500
    g = []
    j = 0
    last = None
    for t in range(base, int(close) + 1):
        while j < len(T_) and T_[j] <= t:
            last = float(tk[j][1])
            j += 1
        g.append(math.log(last) if last else None)
    o = int(close) - 900 - base  # index of the window open
    if any(x is None for x in g[o - 3600 :]):
        continue
    # the contract per second from the open: (yes bid, yes ask) carried forward, None when stale
    qt = [float(x[0]) for x in q]
    yb, ya = [], []
    for t in range(int(close) - 900, int(close) + 1):
        i = bisect.bisect_right(qt, t) - 1
        if i < 0 or t - qt[i] > 20:
            yb.append(None); ya.append(None)
        else:
            yb.append(float(q[i][1])); ya.append(float(q[i][2]))
    sq = [0.0]
    for a_, b_ in zip(g, g[1:]):
        sq.append(sq[-1] + (b_ - a_) ** 2)
    won_yes = w["result"] == "yes"
    kind = "settlement" if crypto else "proxy"
    for D in DS:
        for W in WS:
            for sec in range(60, 721):  # minutes 1 to 12
                kk = o + sec
                u = math.sqrt((sq[kk] - sq[kk - 900]) / 900.0) * math.sqrt(60.0)
                if u <= 0 or yb[sec] is None:
                    continue
                move = (g[kk] - g[kk - W]) / u
                if abs(move) < D:
                    continue
                side_yes = move < 0  # a dip buys YES, a peak buys NO
                price = ya[sec] if side_yes else 1.0 - yb[sec]
                if not 0.10 <= price <= 0.60:
                    continue
                cost = price + fee(price)
                hold = (1.0 if won_yes == side_yes else 0.0) - cost
                gg = g[kk - 3600 : kk + 1 : 10]
                path = sum(abs(b_ - a_) for a_, b_ in zip(gg, gg[1:]))
                eff = abs(gg[-1] - gg[0]) / path if path > 0 else 0.0
                ent = {"ts": close - 900 + sec, "day": int(close // 86400), "eff": eff, "price": price, "left": 900 - sec, "hold": hold, "pnl": {}, "hit": {}}
                for G in GS:
                    for Tm in TS:
                        out = None
                        hit = False
                        end = min(sec + Tm * 60, 870)
                        for s2 in range(sec + 1, end + 1):
                            if yb[s2] is None:
                                continue
                            bid = yb[s2] if side_yes else 1.0 - ya[s2]
                            if bid >= price + G:
                                out = price + G - cost
                                hit = True
                                break
                        if out is None:
                            s2 = end
                            while s2 > sec and yb[s2] is None:
                                s2 -= 1
                            bid = (yb[s2] if side_yes else 1.0 - ya[s2]) if yb[s2] is not None else 0.0
                            out = (bid - fee(bid) if bid > 0 else 0.0) - cost
                        ent["pnl"][(G, Tm)] = out
                        ent["hit"][(G, Tm)] = hit
                E[(kind, D, W)].append(ent)
                break
    used += 1
    if used % 400 == 0:
        print(f"  {used} windows, {_time.time() - t_start:.0f}s", flush=True)
print(f"windows {used} (1 in {STRIDE})")
for kind in ("settlement", "proxy"):
    rows = []
    for D in DS:
        for W in WS:
            ents = sorted(E.get((kind, D, W), []), key=lambda x: x["ts"])
            if len(ents) < 40:
                continue
            mid = ents[len(ents) // 2]["ts"]
            hold = sum(x["hold"] for x in ents)
            for G in GS:
                for Tm in TS:
                    key = (G, Tm)
                    tot = sum(x["pnl"][key] for x in ents)
                    h1 = sum(x["pnl"][key] for x in ents if x["ts"] <= mid)
                    rows.append((tot, D, W, G, Tm, len(ents), sum(x["hit"][key] for x in ents) / len(ents), h1, tot - h1, hold, sum(x["price"] for x in ents) / len(ents)))
    if not rows:
        print(f"\n=== feed kind {kind}: too few entries")
        continue
    rows.sort(key=lambda r: -r[0] / r[5])
    print(f"\n=== feed kind {kind}: {len(rows)} settings | dollars per contract; one contract an entry")
    print("  best 14 by net per entry, then every setting positive in both halves:")
    for n_i, r in enumerate(rows):
        both = r[7] > 0 and r[8] > 0
        if n_i < 14 or both:
            print(f"    move {r[1]:.1f} over {r[2]:3d}s, target +{r[3]:.2f}, out after {r[4]} min: {r[5]:4d} entries at {r[10]:.2f} avg | target reached {r[6]:.0%} | net {r[0]:+7.2f} ({r[0] / r[5] * 100:+.1f} cents an entry) | halves {r[7]:+.2f} / {r[8]:+.2f} | held to settlement instead {r[9]:+.2f} {'BOTH HALVES +' if both else ''}")
    # walk-forward over settings
    days = sorted({x["day"] for (kd, D, W), v in E.items() if kd == kind for x in v})
    run = collections.defaultdict(float)
    oos = []
    picks = collections.Counter()
    for di, d in enumerate(days):
        if di >= 3 and run:
            best = max(run, key=lambda s_: run[s_])
            if run[best] > 0:
                D, W, G, Tm = best
                got = [x["pnl"][(G, Tm)] for x in E[(kind, D, W)] if x["day"] == d]
                oos.append((d, sum(got), len(got)))
                picks[best] += 1
            else:
                oos.append((d, 0.0, 0))
        for (kd, D, W), v in E.items():
            if kd != kind:
                continue
            for x in v:
                if x["day"] == d:
                    for (G, Tm), p in x["pnl"].items():
                        run[(D, W, G, Tm)] += p
    if oos:
        half = len(oos) // 2
        print(f"  walk-forward (the best earlier-days setting, used the next day): net {sum(x[1] for x in oos):+.2f} on {sum(x[2] for x in oos)} entries over {len(oos)} days | halves {sum(x[1] for x in oos[:half]):+.2f} / {sum(x[1] for x in oos[half:]):+.2f} | days won {sum(1 for x in oos if x[1] > 0)}, lost {sum(1 for x in oos if x[1] < 0)}, stood down {sum(1 for x in oos if x[2] == 0)}")
        print("     picked: " + "; ".join(f"move {b[0]} over {b[1]}s target +{b[2]} out {b[3]}min x{n}" for b, n in picks.most_common(4)))
    # breakdowns for the middle of the grid
    for D, W, G, Tm in ((1.5, 60, 0.10, 4), (2.0, 120, 0.10, 4)):
        ents = E.get((kind, D, W), [])
        if len(ents) < 90:
            continue
        effs = sorted(x["eff"] for x in ents)
        c1, c2 = effs[len(effs) // 3], effs[2 * len(effs) // 3]
        print(f"  breakdown, move {D} over {W}s, target +{G:.2f}, out after {Tm} min ({len(ents)} entries):")
        for lab, f in (("hour back and forth", lambda x: x["eff"] <= c1), ("hour in between", lambda x: c1 < x["eff"] < c2), ("hour trending", lambda x: x["eff"] >= c2), ("paid under 0.25", lambda x: x["price"] < 0.25), ("paid 0.25 to 0.45", lambda x: 0.25 <= x["price"] < 0.45), ("paid 0.45 to 0.60", lambda x: x["price"] >= 0.45), ("over 8 min left", lambda x: x["left"] > 480), ("4 to 8 min left", lambda x: 240 < x["left"] <= 480), ("under 4 min left", lambda x: x["left"] <= 240)):
            S = [x for x in ents if f(x)]
            if S:
                tot = sum(x["pnl"][(G, Tm)] for x in S)
                print(f"      {lab:20s} n {len(S):4d} | target reached {sum(x['hit'][(G, Tm)] for x in S) / len(S):.0%} | net {tot:+7.2f} ({tot / len(S) * 100:+.1f} cents an entry) | held instead {sum(x['hold'] for x in S):+.2f}")
print("DONE")
