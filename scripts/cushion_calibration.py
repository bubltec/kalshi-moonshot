"""Calibrating the cushion's chance against results, so the edge it needs shrinks toward zero (2026-11-16, user: "lets add 'The cleaner fix is to calibrate the chance against results so the margin shrinks toward zero' first").

The raw number (scripts/cushion_edge_buy.py): N(cushion), the cushion being the perp's distance from its level at the market's open in typical moves for the time left. It runs too rosy: an edge of +0.03 to +0.10 over the price still lost, and only +0.20 and over paid.
Two calibrations, each fitted by the time left (five bands) and each WALK-FORWARD: a day is scored only with a fit made on the days before it (at least two), so nothing is scored on data it was fitted on:
  A  cushion only      P(side holds) = N(b x cushion): one scale b per time band (b under 1 = the coin moves further than its 3-hour typical move says)
  B  price + cushion   P = logistic(w1 x logit(Kalshi's mid) + w2 x cushion): Kalshi's own prediction, moved by what the cushion adds to it (the user: "kalshi already does the prediction, we just augment it")
Scored on the held-out days: every 15 seconds from minute 1, every side at an ask of 0.05 to 0.95 (spread 0.03 or under), held to settlement at the ask, fee in, by the calibrated chance less what the price needs. Plus the fitted numbers and each model's log-loss against Kalshi's mid. No coin is named. Bands fixed before the run.

Run on the host: scripts/cloud.sh run scripts/cushion_calibration.py
"""
import bisect
import datetime
import math
from collections import defaultdict
from statistics import NormalDist

from kalshi_moonshot import config, db
from kalshi_moonshot.lanes import lane_specs
from kalshi_moonshot.strategy import fee_per_contract as fee
from kalshi_moonshot.watcher import perp_index

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 9, 0, 0, tzinfo=ET).timestamp()
N = NormalDist()
LEFT = ((720, 841, "12 to 14 min left"), (540, 720, "9 to 12 min left"), (360, 540, "6 to 9 min left"), (180, 360, "3 to 6 min left"), (60, 180, "1 to 3 min left"))
EDGE = ((-9, -0.10, "edge under -0.10"), (-0.10, -0.03, "-0.10 to -0.03"), (-0.03, 0.03, "-0.03 to +0.03"), (0.03, 0.06, "+0.03 to +0.06"), (0.06, 0.10, "+0.06 to +0.10"), (0.10, 9, "+0.10 and over"))
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
series = sorted(set(lane_specs(s)["crypto_multiscale"].series))
rows = []  # (day, time band index, yes mid, yes ask, no ask, cushion for yes, yes won, ticker)
for ser in series:
    kp = perp_index(ser)
    pr = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (kp, SINCE - 4 * 3600)).fetchall() if kp else []
    pt, pv, last = [], [], -1
    for r in pr:
        b = int(float(r[0]) // 30)
        if b != last:
            pt.append(b * 30.0); pv.append(math.log(float(r[1]))); last = b
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
        day = datetime.datetime.fromtimestamp(close, ET).strftime("%m-%d")
        for t in range(int(opened) + 60, int(close) - 59, 15):
            i = bisect.bisect_right(qt, t) - 1
            if i < 0 or q[i][2] - q[i][1] > 0.03:
                continue
            left = close - t
            lk = next((k for k, (lo, hi, _) in enumerate(LEFT) if lo <= left < hi), None)
            if lk is None:
                continue
            hist = [perp(t - 90.0 * k) for k in range(122)]
            if not all(h is not None for h in hist):
                continue
            past = [hist[k] - hist[k + 1] for k in range(1, 121)]
            rms = math.sqrt(sum(x * x for x in past) / 120)
            if rms <= 0:
                continue
            mid = (q[i][1] + q[i][2]) / 2
            rows.append((day, lk, min(0.995, max(0.005, mid)), q[i][2], 1 - q[i][1], (hist[0] - ref) / (rms * math.sqrt(left / 90.0)), w["result"] == "yes", w["ticker"]))
    del pt, pv

days = sorted({r[0] for r in rows})
lg = lambda p: math.log(p / (1 - p))  # noqa: E731
sig = lambda x: 1 / (1 + math.exp(-max(-30.0, min(30.0, x))))  # noqa: E731


def fit_a(data):
    """b in P = N(b x cushion), by a search over the log-likelihood."""
    def ll(b):
        tot = 0.0
        for _, _, _, _, _, c, won, _ in data:
            p = min(1 - 1e-6, max(1e-6, N.cdf(b * c)))
            tot += math.log(p if won else 1 - p)
        return tot
    lo, hi = 0.05, 2.0
    for _ in range(40):
        a, b = lo + (hi - lo) / 3, hi - (hi - lo) / 3
        if ll(a) < ll(b):
            lo = a
        else:
            hi = b
    return (lo + hi) / 2


def fit_b(data):
    """(w1, w2) in P = logistic(w1 x logit(mid) + w2 x cushion), by Newton's method."""
    w1, w2 = 1.0, 0.0
    for _ in range(25):
        g1 = g2 = h11 = h12 = h22 = 0.0
        for _, _, mid, _, _, c, won, _ in data:
            x1 = lg(mid)
            p = sig(w1 * x1 + w2 * c)
            e, v = (1.0 if won else 0.0) - p, p * (1 - p)
            g1 += e * x1; g2 += e * c
            h11 += v * x1 * x1; h12 += v * x1 * c; h22 += v * c * c
        det = h11 * h22 - h12 * h12
        if det <= 1e-12:
            break
        d1, d2 = (h22 * g1 - h12 * g2) / det, (h11 * g2 - h12 * g1) / det
        w1, w2 = w1 + d1, w2 + d2
        if abs(d1) + abs(d2) < 1e-6:
            break
    return w1, w2


cells = defaultdict(lambda: [0, 0, 0.0, 0.0, set()])
loss = defaultdict(lambda: [0.0, 0])
fits = {}
for D in days[2:]:
    train = [r for r in rows if r[0] < D]
    test = [r for r in rows if r[0] == D]
    for lk in range(len(LEFT)):
        tr = [r for r in train if r[1] == lk]
        if len(tr) < 2000:
            continue
        b, (w1, w2) = fit_a(tr), fit_b(tr)
        fits[(D, lk)] = (b, w1, w2, len(tr))
        for _, _, mid, ya, na, c, won, tk in (r for r in test if r[1] == lk):
            pa, pb = N.cdf(b * c), sig(w1 * lg(mid) + w2 * c)
            for name, p in (("A", pa), ("B", pb), ("K", mid)):
                p = min(1 - 1e-6, max(1e-6, p))
                loss[name][0] -= math.log(p if won else 1 - p); loss[name][1] += 1
            for side, a, w_ in (("yes", ya, won), ("no", na, not won)):
                if not (0.05 <= a <= 0.95):
                    continue
                net = (1.0 if w_ else 0.0) / (a + fee(a)) - 1.0
                for name, p in (("A", pa), ("B", pb)):
                    ps = p if side == "yes" else 1 - p
                    e = ps - (a + fee(a))
                    ek = next(n for lo, hi, n in EDGE if lo <= e < hi)
                    for key in ((name, ek), (name + "t", LEFT[lk][2] + " | " + ek), (name + "p", ("ask under 0.40" if a < 0.40 else "ask 0.40 to 0.70" if a < 0.70 else "ask 0.70 and over") + " | " + ek)):
                        if key[0].endswith(("t", "p")) and e < 0.03:
                            continue
                        cl = cells[key]
                        cl[0] += 1; cl[1] += w_; cl[2] += a; cl[3] += net; cl[4].add(tk)


def show(title, kind, order=None):
    print(f"\n{title}")
    keys = [k for k in cells if k[0] == kind]
    keys.sort(key=(lambda k: order.index(k[1])) if order else (lambda k: k[1]))
    for k in keys:
        c = cells[k]
        print(f"  {k[1]:40s} | {c[0]:6d} ({len(c[4]):4d} markets) | won {c[1] / c[0]:4.0%} | ask {c[2] / c[0]:.3f} | held net per $ {c[3] / c[0] * 100:+6.1f}%")


E = [n for _, _, n in EDGE]
print(f"window: 2026-11-09 00:00 ET on; {len(rows)} moments with a cushion reading on days {', '.join(days)}; scored walk-forward on {', '.join(days[2:])}")
print("the fits used for the last scored day (trained on every day before it): time band | b of model A | w1, w2 of model B | training moments")
for lk, (_, _, name) in enumerate(LEFT):
    f = fits.get((days[-1], lk))
    if f:
        print(f"  {name:18s} | b {f[0]:.2f} | w1 {f[1]:.2f}, w2 {f[2]:+.3f} | {f[3]}")
print("log-loss on the held-out days (lower is better): " + ", ".join(f"{n} {loss[k][0] / loss[k][1]:.4f}" for k, n in (("K", "Kalshi's mid"), ("A", "A cushion only"), ("B", "B price + cushion")) if loss[k][1]))
show("A. cushion only, calibrated: by the edge over the price", "A", E)
show("B. price + cushion: by the edge over the price", "B", E)
show("B. edges of +0.03 and over, by the time left", "Bt")
show("B. edges of +0.03 and over, by the ask", "Bp")
show("A. edges of +0.03 and over, by the ask", "Ap")
print("DONE")
