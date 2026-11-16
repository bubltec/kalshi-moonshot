"""Buying a flat market late on its cushion (2026-11-16, user, on the spike rule buying only after the coin has moved: "that's not right. it should matter what kind of cushion it has. it is fine to buy flat at a high prediction (> 70) and less than 3 mins left i think").

One question, same moments (CLAUDE.md rule 4): every 15 seconds in every settled market of the live crypto lane's series since Monday 2026-11-09, each side with an ask of 0.70 to 0.97 (spread 0.03 or under), held to settlement at the ask, fee in. By the time left, by whether the perp is flat (the spike view's z between -0.5 and +0.5), and by the cushion read from the market's Kalshi perp, independent of Kalshi's price:
  cushion   how far the perp sits on the side's side of its value at the market's open, in units of what it typically moves in the time left (its root-mean-square 90 s move over the last 3 hours, scaled by sqrt(time left / 90)). 2 = the side is two typical remaining moves clear.
Moments without 3 hours of the perp or a perp reading at the open are left out. Bands fixed before the run. No coin is named.

Run on the host: scripts/cloud.sh run scripts/late_cushion_buy.py
"""
import bisect
import datetime
import math
from collections import defaultdict

from kalshi_moonshot import config, db
from kalshi_moonshot.lanes import lane_specs
from kalshi_moonshot.strategy import fee_per_contract as fee
from kalshi_moonshot.watcher import perp_index

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 9, 0, 0, tzinfo=ET).timestamp()
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
series = sorted(set(lane_specs(s)["crypto_multiscale"].series))
LEFT = ((540, 721, "9 to 12 min left"), (360, 540, "6 to 9 min left"), (180, 360, "3 to 6 min left"), (60, 180, "1 to 3 min left"), (15, 60, "under 1 min left"))
CUSH = ((-99, 0.5, "cushion under 0.5"), (0.5, 1.0, "0.5 to 1"), (1.0, 2.0, "1 to 2"), (2.0, 3.0, "2 to 3"), (3.0, 999, "3 and over"))
cells = defaultdict(lambda: [0, 0, 0.0, 0.0, set()])


def add(key, a, won, tk):
    c = cells[key]
    c[0] += 1; c[1] += won; c[2] += a; c[3] += (1.0 if won else 0.0) / (a + fee(a)) - 1.0; c[4].add(tk)


for ser in series:
    kp = perp_index(ser)
    pr = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (kp, SINCE - 4 * 3600)).fetchall() if kp else []
    pt, pv, last = [], [], -1
    for r in pr:
        b = int(float(r[0]) // 15)  # 15 s buckets stamped at their END and holding their last tick: at a moment on the 15 s grid this is the last tick at or before it. Until 2026-11-16 16:20 these were 30 s buckets stamped at their START, so a reading could hold a tick from up to 30 s AFTER the moment: a look-ahead that made every result here too good
        if b != last:
            pt.append((b + 1) * 15.0); pv.append(math.log(float(r[1]))); last = b
        else:
            pv[-1] = math.log(float(r[1]))
    del pr

    def perp(t):
        i = bisect.bisect_right(pt, t) - 1
        return pv[i] if i >= 0 and t - pt[i] < 120 else None

    for w in st.execute("SELECT ticker, close_ts, result FROM windows WHERE series=? AND close_ts>? AND result IN ('yes','no') ORDER BY close_ts", (ser, SINCE)).fetchall():
        close = float(w["close_ts"])
        opened = close - 900.0
        ref = perp(opened + 30)
        if ref is None:
            continue
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], opened, close)).fetchall()
        q = [(float(x[0]), float(x[1] or 0.0), float(x[2] or 0.0)) for x in q if x[2] and float(x[2]) > 0 and not (float(x[1] or 0.0) <= 0 and float(x[2]) >= 1)]
        if len(q) < 50:
            continue
        qt = [x[0] for x in q]
        for t in range(int(opened) + 180, int(close) - 14, 15):
            i = bisect.bisect_right(qt, t) - 1
            if i < 0 or q[i][2] - q[i][1] > 0.03:
                continue
            left = close - t
            lk = next((name for lo, hi, name in LEFT if lo <= left < hi), None)
            if lk is None:
                continue
            read = None
            for side in ("yes", "no"):
                a = q[i][2] if side == "yes" else 1 - q[i][1]
                if not (0.70 <= a <= 0.97):
                    continue
                if read is None:
                    hist = [perp(t - 90.0 * k) for k in range(122)]
                    read = False
                    if all(h is not None for h in hist):
                        past = [hist[k] - hist[k + 1] for k in range(1, 121)]
                        rms = math.sqrt(sum(x * x for x in past) / 120)
                        if rms > 0:
                            read = ((hist[0] - hist[1]) / rms, (hist[0] - ref) / (rms * math.sqrt(left / 90.0)))
                if not read:
                    continue
                sign = 1.0 if side == "yes" else -1.0
                z, cush = sign * read[0], sign * read[1]
                won = w["result"] == side
                flat = abs(z) < 0.5
                ck = next(name for lo, hi, name in CUSH if lo <= cush < hi)
                add(("left", lk), a, won, w["ticker"])
                if flat:
                    add(("flat", lk), a, won, w["ticker"])
                    add(("flatc", lk + " | " + ck), a, won, w["ticker"])
                if left < 180:
                    add(("latec", ck), a, won, w["ticker"])
                    add(("latep", ("ask 0.70 to 0.80" if a < 0.80 else "ask 0.80 to 0.90" if a < 0.90 else "ask 0.90 to 0.97") + " | " + ck), a, won, w["ticker"])
    del pt, pv


def show(title, kind, order=None):
    print(f"\n{title}")
    keys = [k for k in cells if k[0] == kind]
    keys.sort(key=(lambda k: order.index(k[1])) if order else (lambda k: k[1]))
    for k in keys:
        c = cells[k]
        need = c[2] / c[0] + fee(c[2] / c[0])
        print(f"  {k[1]:40s} | {c[0]:6d} ({len(c[4]):4d} markets) | won {c[1] / c[0]:4.0%} needs {need:4.0%} | ask {c[2] / c[0]:.3f} | held net per $ {c[3] / c[0] * 100:+6.1f}%")


L = [n for _, _, n in LEFT]
print("window: 2026-11-09 00:00 ET on; sides with an ask of 0.70 to 0.97 and 3 hours of the perp")
show("1. every such side, by the time left", "left", L)
show("2. the same with the perp flat (z between -0.5 and +0.5)", "flat", L)
show("3. under 3 minutes left (flat or not), by the cushion", "latec", [n for _, _, n in CUSH])
show("4. under 3 minutes left, by the ask and the cushion", "latep")
show("5. the perp flat, by the time left and the cushion", "flatc")
print("DONE")
