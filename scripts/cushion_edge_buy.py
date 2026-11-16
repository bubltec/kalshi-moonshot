"""One number from the spot cushion and the time left, against the price, with no price gate and no time gate (2026-11-16, user: "so how do we translate that to logic without hard limits. needs to be based on the spot cushion and time remaining"; "the more time remaining the more cushion it needs").

The number: the chance the side holds, from the cushion alone: N(cushion), where the cushion is how far the market's Kalshi perp sits on the side's side of its value at the market's open, in units of what the perp typically moves in the time left (its root-mean-square 90 s move over the last 3 hours x sqrt(time left / 90)). More time left makes the same distance a smaller cushion, so it asks for more by itself. The edge is that chance less what the price needs (the ask plus its fee).
One question, same moments (CLAUDE.md rule 4): every 15 seconds from minute 1 in every settled market of the live crypto lane's series since Monday 2026-11-09, each side at ANY ask from 0.05 to 0.95 (spread 0.03 or under), held to settlement at the ask, fee in, by the edge; then by the edge and the time left, and by the edge and the ask. Bands fixed before the run. No coin is named.

Run on the host: scripts/cloud.sh run scripts/cushion_edge_buy.py
"""
import bisect
import datetime
import math
from statistics import NormalDist
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
LEFT = ((720, 841, "12 to 14 min left"), (540, 720, "9 to 12 min left"), (360, 540, "6 to 9 min left"), (180, 360, "3 to 6 min left"), (60, 180, "1 to 3 min left"), (15, 60, "under 1 min left"))
EDGE = ((-9, -0.10, "edge under -0.10"), (-0.10, -0.03, "-0.10 to -0.03"), (-0.03, 0.03, "-0.03 to +0.03"), (0.03, 0.10, "+0.03 to +0.10"), (0.10, 0.20, "+0.10 to +0.20"), (0.20, 9, "+0.20 and over"))
N = NormalDist()
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
        for t in range(int(opened) + 60, int(close) - 14, 15):
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
                if not (0.05 <= a <= 0.95):
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
                edge = N.cdf(cush) - (a + fee(a))
                ek = next(name for lo, hi, name in EDGE if lo <= edge < hi)
                add(("edge", ek), a, won, w["ticker"])
                add(("edget", lk + " | " + ek), a, won, w["ticker"])
                pb = "ask 0.05 to 0.30" if a < 0.30 else "ask 0.30 to 0.50" if a < 0.50 else "ask 0.50 to 0.70" if a < 0.70 else "ask 0.70 to 0.85" if a < 0.85 else "ask 0.85 to 0.95"
                add(("edgep", pb + " | " + ek), a, won, w["ticker"])
    del pt, pv


def show(title, kind, order=None):
    print(f"\n{title}")
    keys = [k for k in cells if k[0] == kind]
    keys.sort(key=(lambda k: order.index(k[1])) if order else (lambda k: k[1]))
    for k in keys:
        c = cells[k]
        need = c[2] / c[0] + fee(c[2] / c[0])
        print(f"  {k[1]:40s} | {c[0]:6d} ({len(c[4]):4d} markets) | won {c[1] / c[0]:4.0%} needs {need:4.0%} | ask {c[2] / c[0]:.3f} | held net per $ {c[3] / c[0] * 100:+6.1f}%")


print("window: 2026-11-09 00:00 ET on; every side at an ask of 0.05 to 0.95 from minute 1, with 3 hours of the perp")
show("1. by the edge: the cushion's chance less what the price needs", "edge", [n for _, _, n in EDGE])
show("2. by the time left and the edge", "edget")
show("3. by the ask and the edge", "edgep")
print("DONE")
