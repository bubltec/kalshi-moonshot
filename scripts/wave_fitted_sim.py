"""The wave trade with the thresholds read off the user's own hand-sold scalps (2026-11-11; studies/results/2026-11-11-manual-scalps.txt: paid 0.36 and sold 0.54 after 150 s, bought with about 8 minutes left, 0.7 typical minutes behind the target, the 5-minute slope -0.17 a minute and the 1-minute slope flat; sold while the 1-minute slope was still rising).
Question: taken on every window of the tape, not only the ones that worked, what does that trade pay?
ENTRY (measured toward the side bought, in typical minutes; a YES when the spot is under the target, a NO when it is over): the cushion between -2.5 and -0.2; the 5-minute slope between -0.6 and 0.0 a minute (still slightly against); the 1-minute slope between -0.15 and +0.30 (gone flat); 660 to 360 s left; the side's bid + 0.01 between 0.10 and 0.55. The first such moment in a window. These bands are fitted to 24 trades of the user's and are stated as such.
FILL: "rest" a buy rests at the bid for 30 s and fills only if the ask comes down to it (no fee); "touch" it fills at once at the bid (best case, no fee); "take" bought at the ask with the taker fee.
EXIT: a sell resting at the price paid + G fills when the bid reaches it (no fee); G 0.10 / 0.18 / 0.25. If it has not filled: sold at the bid after 150 s, or after 300 s, or held to settlement. A cushion 1.5 typical minutes further against than at the buy sells at the bid at once (give-up). Also the same entries simply held to settlement.
    scripts/cloud_job.sh scripts/wave_fitted_sim.py STRIDE=2
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

GS = (0.10, 0.18, 0.25)
AFTER = (150, 300, None)  # seconds to the time-out sale; None = hold to settlement
FILLS = ("rest", "touch", "take")
wins = st.db.execute("SELECT ticker, close_ts, strike, result FROM windows WHERE result IN ('yes','no') AND close_ts > ? ORDER BY close_ts", (SINCE_S,)).fetchall()
T = collections.defaultdict(list)  # (kind, fill, G, after) -> (ts, pnl, why)
H = collections.defaultdict(list)  # (kind, fill) -> (ts, held pnl, entry price)
sig_n = collections.Counter()
used = 0
t_start = _time.time()
for k, w in enumerate(wins):
    ser = w["ticker"].split("-")[0]
    crypto = variants.crypto_index(ser)
    ix = crypto or variants.biquote_index(ser)
    if not ser.endswith("15M") or not ix or k % STRIDE:
        continue
    close = float(w["close_ts"])
    tk = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts", (ix, close - 2720, close)).fetchall()
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (w["ticker"], close - 900, close)).fetchall()
    if len(tk) < 1200 or len(q) < 30 or float(tk[0][0]) > close - 2650:
        continue
    T_ = [float(x[0]) for x in tk]
    base = int(close) - 2700
    g = []
    j = 0
    last = None
    for t in range(base, int(close) + 1):
        while j < len(T_) and T_[j] <= t:
            last = float(tk[j][1])
            j += 1
        g.append(math.log(last) if last else None)
    o = 1800
    if any(x is None for x in g[o - 1000 :]):
        continue
    if crypto:
        if not w["strike"] or float(w["strike"]) <= 0:
            continue
        ref = math.log(float(w["strike"]))
    else:
        ref = g[o]
    qt = [float(x[0]) for x in q]
    yb, ya = [], []
    for t in range(int(close) - 900, int(close) + 1):
        i = bisect.bisect_right(qt, t) - 1
        if i < 0 or t - qt[i] > 20:
            yb.append(None); ya.append(None)
        else:
            yb.append(float(q[i][1])); ya.append(float(q[i][2]))
    seg = g[o - 900 : o + 1]
    u = math.sqrt(sum((b_ - a_) ** 2 for a_, b_ in zip(seg, seg[1:])) / 900.0) * math.sqrt(60.0)
    if u <= 0:
        continue
    pre = [0.0]
    for x in g[o - 1000 :]:
        pre.append(pre[-1] + x)
    off = o - 1000

    def a15(kk):
        a_ = kk - off
        return (pre[a_ + 1] - pre[a_ - 14]) / 15.0

    def bid_of(sec, yes):
        return None if yb[sec] is None else (yb[sec] if yes else 1.0 - ya[sec])

    def ask_of(sec, yes):
        return None if yb[sec] is None else (ya[sec] if yes else 1.0 - yb[sec])

    won_yes = w["result"] == "yes"
    kind = "settlement" if crypto else "proxy"
    used += 1
    sig = None
    for sec in range(240, 541, 5):  # 660 to 360 s left
        kk = o + sec
        raw = (a15(kk) - ref) / u
        yes = raw < 0
        sgn = 1.0 if yes else -1.0
        cush = raw * sgn  # toward the side bought: negative = behind
        s5 = (a15(kk) - a15(kk - 300)) / 5.0 / u * sgn
        s1 = (a15(kk) - a15(kk - 60)) / u * sgn
        b0 = bid_of(sec, yes)
        if b0 is None:
            continue
        if -2.5 <= cush <= -0.2 and -0.6 <= s5 <= 0.0 and -0.15 <= s1 <= 0.30 and 0.10 <= b0 + 0.01 <= 0.55:
            sig = (sec, yes, cush, b0)
            break
    if sig is None:
        continue
    sec, yes, cush0, b0 = sig
    sgn = 1.0 if yes else -1.0
    for fill in FILLS:
        sig_n[(kind, fill)] += 1
        if fill == "rest":
            fs = next((s2 for s2 in range(sec + 1, min(sec + 31, 870)) if ask_of(s2, yes) is not None and ask_of(s2, yes) <= b0 + 1e-9), None)
            price, cost = b0, b0
        elif fill == "touch":
            fs, price, cost = sec, b0, b0
        else:
            a0 = ask_of(sec, yes)
            fs, price, cost = sec, a0, a0 + fee(a0)
        if fs is None:
            continue
        hold = (1.0 if won_yes == yes else 0.0) - cost
        H[(kind, fill)].append((close - 900 + fs, hold, price))
        for G in GS:
            for after in AFTER:
                out = None
                why = "settled"
                end = 870 if after is None else min(fs + after, 870)
                for s2 in range(fs + 1, end + 1):
                    bd = bid_of(s2, yes)
                    if bd is None:
                        continue
                    if bd >= price + G:
                        out, why = price + G - cost, "target"
                        break
                    if s2 % 5 == 0 and ((a15(o + s2) - ref) / u * sgn - cush0) <= -1.5:
                        out, why = bd - fee(bd) - cost, "give-up"
                        break
                if out is None:
                    if after is None:
                        out = hold
                    else:
                        s2 = end
                        while s2 > fs and bid_of(s2, yes) is None:
                            s2 -= 1
                        bd = bid_of(s2, yes) or 0.0
                        out, why = (bd - fee(bd) if bd > 0 else 0.0) - cost, "time-out"
                T[(kind, fill, G, after)].append((close - 900 + fs, out, why))
    if used % 500 == 0:
        print(f"  {used} windows, {_time.time() - t_start:.0f}s", flush=True)
print(f"windows {used} (1 in {STRIDE})")
for kind in ("settlement", "proxy"):
    print(f"\n=== feed kind {kind} (dollars per contract)")
    for fill in FILLS:
        Hh = sorted(H.get((kind, fill), []))
        n_sig = sig_n[(kind, fill)]
        if len(Hh) < 20:
            print(f"  fill {fill}: {n_sig} signals, {len(Hh)} filled: too few")
            continue
        mid = Hh[len(Hh) // 2][0]
        tot = sum(x[1] for x in Hh)
        h1 = sum(x[1] for x in Hh if x[0] <= mid)
        print(f"  fill {fill:5s}: {n_sig} signals, {len(Hh)} filled ({len(Hh) / n_sig:.0%}) at {sum(x[2] for x in Hh) / len(Hh):.2f} avg | simply held to settlement: won {sum(1 for x in Hh if x[1] > 0) / len(Hh):.0%}, net {tot:+.2f} ({tot / len(Hh) * 100:+.1f} cents a trade), halves {h1:+.2f} / {tot - h1:+.2f} {'BOTH HALVES +' if h1 > 0 and tot - h1 > 0 else ''}")
        for G in GS:
            for after in AFTER:
                R = sorted(T.get((kind, fill, G, after), []))
                tot = sum(x[1] for x in R)
                h1 = sum(x[1] for x in R if x[0] <= mid)
                why = collections.Counter(x[2] for x in R)
                print(f"      target +{G:.2f}, then {'hold to settlement' if after is None else 'sell after ' + str(after) + ' s':18s}: target reached {why['target'] / len(R):.0%} | net {tot:+7.2f} ({tot / len(R) * 100:+.1f} cents a trade) | halves {h1:+.2f} / {tot - h1:+.2f} | {dict(why)} {'BOTH HALVES +' if h1 > 0 and tot - h1 > 0 else ''}")
print("DONE")
