"""Scratch work for the risk exit's score: which factors say anything about a bet's worth beyond Kalshi's price? (2026-11-16, user: "i thought we had several factors that built up to a risk score for selling"; "nothing is absolute. all these factors need to build towards that risk score"; "i want the risk exit. this is where the profit and loss logic should live")

The score a sale should compare with the bid is what the bet is worth held: the chance its side wins. This fits that chance from several factors at once and asks, on days the fit has not seen, whether it knows anything Kalshi's own mid does not.
Factors, each for the YES side at a moment, each read only from data stamped at or before that moment:
  price      logit of Kalshi's mid
  cushion    the settlement index against the target (the 60 s average before the open), in the index's typical moves for the time left
  led        the share of the market so far in which YES was the favorite, centred (led - 0.5)
  place      where YES's mid sits in its own range since the open, centred (place - 0.5)
  drift      the mid's change over the last 3 minutes
  time       the share of the market gone, times the logit of the mid (does the price mean more late than early?)
Fit: logistic regression (Newton), walk-forward by day: each day is scored with a fit on the days before it (at least two). Every 30 seconds from minute 1 to 14, every settled market of the live crypto lane's series since Monday 2026-11-09.
Reported on the held-out days: the weights of the last fit, log-loss against Kalshi's mid alone, and the test that matters for a sale: split the moments by how far the score sits from the mid, and see whether the side wins more or less often than its price said.
No money is simulated and nothing is bought or sold here. No coin is named.

Run on the host: scripts/cloud.sh run scripts/risk_score_fit.py
"""
import bisect
import datetime
import math
from collections import defaultdict

from kalshi_moonshot import config, db, variants
from kalshi_moonshot.lanes import lane_specs

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 9, 0, 0, tzinfo=ET).timestamp()
NAMES = ("price", "cushion", "led", "place", "drift", "time")
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
series = sorted(set(lane_specs(s)["crypto_multiscale"].series))
lg = lambda p: math.log(p / (1 - p))  # noqa: E731
rows = []  # (day, x vector, yes won, mid)
for ser in series:
    ix = variants.crypto_index(ser)
    bt, bv, last = [], [], -1
    for r in (tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (ix, SINCE - 4000)).fetchall() if ix else []):
        b = int(float(r[0]) // 5)
        if b != last:
            bt.append((b + 1) * 5.0); bv.append(float(r[1])); last = b  # 5 s buckets stamped at their END
        else:
            bv[-1] = float(r[1])
    for w in st.execute("SELECT ticker, close_ts, result, strike FROM windows WHERE series=? AND close_ts>? AND result IN ('yes','no') AND strike>0 ORDER BY close_ts", (ser, SINCE)).fetchall():
        close, strike = float(w["close_ts"]), float(w["strike"])
        opened = close - 900.0
        j0, i60 = bisect.bisect_right(bt, opened), bisect.bisect_right(bt, opened - 3600.0)
        if j0 - i60 < 600:
            continue
        marks = [bv[bisect.bisect_right(bt, opened - 60.0 * k) - 1] for k in range(60, -1, -1)]
        mv = [math.log(marks[k + 1] / marks[k]) for k in range(60)]
        unit = math.sqrt(sum(m * m for m in mv) / 60)  # typical one-minute move of the hour before the open
        if unit <= 0:
            continue
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], opened, close)).fetchall()
        q = [(float(x[0]), float(x[1] or 0.0), float(x[2] or 0.0)) for x in q if x[2] and float(x[2]) > 0 and not (float(x[1] or 0.0) <= 0 and float(x[2]) >= 1)]
        if len(q) < 50:
            continue
        qt = [x[0] for x in q]
        day = datetime.datetime.fromtimestamp(close, ET).strftime("%m-%d")
        lo = hi = None
        n = fav = 0
        hist = {}
        for t in range(int(opened), int(close) - 59, 5):
            i = bisect.bisect_right(qt, t) - 1
            if i < 0:
                continue
            mid = (q[i][1] + q[i][2]) / 2
            lo, hi = (mid, mid) if lo is None else (min(lo, mid), max(hi, mid))
            hist[t] = mid
            if (t - int(opened)) % 30 == 0 and t - opened >= 60 and n >= 6 and hi > lo and q[i][2] - q[i][1] <= 0.03 and 0.03 < mid < 0.97:
                k = bisect.bisect_right(bt, t) - 1
                back = hist.get(t - 180)
                if k >= 0 and t - bt[k] < 30 and back is not None:
                    left = close - t
                    cush = math.log(bv[k] / strike) / (unit * math.sqrt(left / 60.0))
                    x = (lg(mid), max(-6.0, min(6.0, cush)), fav / n - 0.5, (mid - lo) / (hi - lo) - 0.5, mid - back, (t - opened) / 900.0 * lg(mid))
                    rows.append((day, x, w["result"] == "yes", mid))
            n += 1
            fav += mid > 0.5
    del bt, bv

sig = lambda z: 1 / (1 + math.exp(-max(-30.0, min(30.0, z))))  # noqa: E731
K = len(NAMES)


def fit(data):
    w = [1.0] + [0.0] * (K - 1)
    for _ in range(30):
        g = [0.0] * K
        H = [[0.0] * K for _ in range(K)]
        for _, x, won, _ in data:
            p = sig(sum(a * b for a, b in zip(w, x)))
            e, v = (1.0 if won else 0.0) - p, p * (1 - p)
            for a in range(K):
                g[a] += e * x[a]
                for b in range(a, K):
                    H[a][b] += v * x[a] * x[b]
        for a in range(K):
            H[a][a] += 1e-6
            for b in range(a):
                H[a][b] = H[b][a]
        M = [H[a][:] + [g[a]] for a in range(K)]  # solve H d = g
        for c in range(K):
            piv = max(range(c, K), key=lambda r: abs(M[r][c]))
            M[c], M[piv] = M[piv], M[c]
            for r in range(K):
                if r != c and M[c][c]:
                    f = M[r][c] / M[c][c]
                    M[r] = [u - f * v for u, v in zip(M[r], M[c])]
        d = [M[a][K] / M[a][a] if M[a][a] else 0.0 for a in range(K)]
        w = [a + b for a, b in zip(w, d)]
        if sum(abs(x) for x in d) < 1e-6:
            break
    return w


days = sorted({r[0] for r in rows})
loss = {"mid": [0.0, 0], "score": [0.0, 0]}
gap = defaultdict(lambda: [0, 0, 0.0, 0.0])  # band -> n, yes won, mid sum, score sum
BANDS = ((-9, -0.06, "score 6+ points UNDER the mid"), (-0.06, -0.03, "3 to 6 under"), (-0.03, -0.01, "1 to 3 under"), (-0.01, 0.01, "within 1 point"), (0.01, 0.03, "1 to 3 over"), (0.03, 0.06, "3 to 6 over"), (0.06, 9, "score 6+ points OVER the mid"))
w_last = None
for D in days[2:]:
    train = [r for r in rows if r[0] < D]
    if len(train) < 5000:
        continue
    w_last = fit(train)
    for _, x, won, mid in (r for r in rows if r[0] == D):
        p = min(1 - 1e-6, max(1e-6, sig(sum(a * b for a, b in zip(w_last, x)))))
        loss["mid"][0] -= math.log(mid if won else 1 - mid); loss["mid"][1] += 1
        loss["score"][0] -= math.log(p if won else 1 - p); loss["score"][1] += 1
        name = next(nm for lo_, hi_, nm in BANDS if lo_ <= p - mid < hi_)
        c = gap[name]
        c[0] += 1; c[1] += won; c[2] += mid; c[3] += p

print(f"window: 2026-11-09 00:00 ET on; {len(rows)} moments on days {', '.join(days)}; scored walk-forward on {', '.join(days[2:])}")
print("weights of the last fit (trained on every day before the last): " + ", ".join(f"{n} {v:+.3f}" for n, v in zip(NAMES, w_last or [])))
print(f"log-loss on the held-out days (lower is better): Kalshi's mid {loss['mid'][0] / loss['mid'][1]:.4f} | the score {loss['score'][0] / loss['score'][1]:.4f}")
print("\nwhere the score and Kalshi's mid disagree, who was right? (YES side, held-out days)")
for _, _, name in BANDS:
    c = gap[name]
    if c[0] < 200:
        print(f"  {name:30s} n {c[0]:6d}")
        continue
    print(f"  {name:30s} n {c[0]:6d} | Kalshi's mid said {c[2] / c[0]:5.1%} | the score said {c[3] / c[0]:5.1%} | YES actually won {c[1] / c[0]:5.1%}")
print("DONE")
