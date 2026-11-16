"""A different rule for the sides in the live band: does the 3-hour view pick the ones that pay? (2026-11-16, user, after the top-of-range skip turned out to select nothing in the band: "we need a different rule for those")

One question, same moments (CLAUDE.md rule 4): every 15 seconds in every settled market of the live crypto lane's series since Monday 2026-11-09, each side in the lane's own band (ask 0.57 to 0.82, spread 0.03 or under, 3 minutes in or later), held to settlement at the ask, fee in. The band as a whole loses about 5%. Split by the reading the buy notes now carry (curverisk.spike_view, the market's Kalshi perp every 30 s):
  z         the perp's move over the last 90 s in its own typical 90 s moves of the 3 hours before, signed toward the side
  similar   how many of those 120 stretches moved as far
and by the two the day's other studies found: the share of the market the side had led, and the minutes into the market. Moments without 3 hours of the perp are left out and counted. Bands fixed before the run. No coin is named.

Run on the host: scripts/cloud.sh run scripts/band_by_spike_view.py
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
Z = ((-99, -1.5, "z under -1.5 (a real move against)"), (-1.5, -0.5, "-1.5 to -0.5"), (-0.5, 0.5, "-0.5 to +0.5 (the perp is still)"), (0.5, 1.5, "+0.5 to +1.5"), (1.5, 3, "+1.5 to +3"), (3, 99, "+3 and over (a real move for it)"))
SIM = ((0, 1, "none as large in 3 hours"), (1, 6, "1 to 5"), (6, 25, "6 to 24"), (25, 121, "25 and over (ordinary)"))
cells = defaultdict(lambda: [0, 0, 0.0, 0.0, set()])
unread = total = 0


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
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], opened - 20, close)).fetchall()
        q = [(float(x[0]), float(x[1] or 0.0), float(x[2] or 0.0)) for x in q if x[2] and float(x[2]) > 0 and not (float(x[1] or 0.0) <= 0 and float(x[2]) >= 1)]
        if len(q) < 50:
            continue
        qt = [x[0] for x in q]
        n = fav = 0
        for t in range(int(opened), int(close) - 15, 15):
            i = bisect.bisect_right(qt, t) - 1
            if i < 0:
                continue
            bid, ask = q[i][1], q[i][2]
            mid = (bid + ask) / 2
            if t - opened >= 180 and n >= 4 and ask - bid <= 0.03:
                read = None
                for side in ("yes", "no"):
                    a = ask if side == "yes" else 1 - bid
                    if not (0.57 <= a <= 0.82):
                        continue
                    total += 1
                    if read is None:
                        hist = [perp(t - 90.0 * k) for k in range(122)]
                        read = False
                        if all(h is not None for h in hist):
                            past = [hist[k] - hist[k + 1] for k in range(1, 121)]
                            rms = math.sqrt(sum(x * x for x in past) / 120)
                            if rms > 0:
                                mv = hist[0] - hist[1]
                                read = (mv / rms, sum(1 for x in past if abs(x) >= abs(mv)))
                    if not read:
                        unread += 1
                        continue
                    z = read[0] if side == "yes" else -read[0]
                    won = w["result"] == side
                    led = fav / n if side == "yes" else (n - fav) / n
                    mins = (t - opened) / 60.0
                    zk = next(name for lo, hi, name in Z if lo <= z < hi)
                    add(("z", zk), a, won, w["ticker"])
                    add(("sim", next(name for lo, hi, name in SIM if lo <= read[1] < hi)), a, won, w["ticker"])
                    add(("all", "every read moment"), a, won, w["ticker"])
                    half = "first half" if mins < 7.5 else "second half"
                    add(("zt", zk + " | " + half), a, won, w["ticker"])
                    if read[1] < 6:
                        add(("rare", ("with it" if z > 0 else "against it") + " | " + ("the side trailed (led under 20%)" if led < 0.2 else "the side led 80% and over" if led >= 0.8 else "contested")), a, won, w["ticker"])
            n += 1
            fav += mid > 0.5
    del pt, pv


def show(title, kind, order=None):
    print(f"\n{title}")
    keys = [k for k in cells if k[0] == kind]
    keys.sort(key=(lambda k: order.index(k[1])) if order else (lambda k: k[1]))
    for k in keys:
        c = cells[k]
        print(f"  {k[1]:52s} | {c[0]:6d} ({len(c[4]):4d} markets) | won {c[1] / c[0]:4.0%} | ask {c[2] / c[0]:.3f} | held net per $ {c[3] / c[0] * 100:+6.1f}%")


print(f"window: 2026-11-09 00:00 ET on; {total} band moments, {unread} without 3 hours of the perp and left out")
show("0. the band", "all")
show("1. by the last 90 s on the 3-hour view, signed toward the side", "z", [n for _, _, n in Z])
show("2. by similar moves in the last 3 hours", "sim", [n for _, _, n in SIM])
show("3. the same z, by half of the market", "zt")
show("4. a rare move (5 or fewer as large in 3 hours): with or against the side, by who had led", "rare")
print("DONE")
