"""Fibonacci look-backs: the curve read at 1, 2, 3, 5, 8, 13, 21, 34 and 55 minutes, direction as an angle and choppiness as the variability of the spot's distance to the strike.

Question (2026-11-07, user: "those time periods should be 1, 2, 3, 5, 8, 13, 21, 34, 55"): the record states so far read 5 and 60 minutes (and the lane's scale set was 1, 2, 5, 15, 60). With nine scales a state of directions at every scale has 3^9 cells, far too few readings each, so two readings of the same data:
A. Which PAIRS of scales carry a state that pays? For every pair of the nine scales, the state is the direction of the cushion at each (rising/flat/falling, at 45 degrees both ways, tan(angle) = the change in the cushion over the look-back in that look-back's typical move) with and without the first scale's choppiness (the standard deviation of the cushion over the look-back in typical moves, chop above the pooled 67th percentile of that scale). Walk-forward as scripts/state_record_walkforward.py: a state and price band's record is every earlier day (one reading per market side, state and band, net of the fee, held to the result, shrunk by 10, at least 15 markets), each later day buys the moments whose record clears 0.10 or 0.20. The best pairs by net per $10 ticket are listed with the two halves.
B. A ridge regression on the direction slope and variability at all nine scales together (18 readings) plus the price and spread, fit on the earlier days for the bet-to-win net, scored on each next day: the realised net of the top 5%, 10% and 20% of predicted moments.
Crypto only (the five coins with windows), minute 2 to 14, ask 0.20-0.70, spread <= 0.04; three test days. With 36 pairs x 2 shapes x 2 bars the best rows are in-sample.

    scripts/hostrun3.sh-style: docker compose exec -T bot python - < scripts/state_record_fibonacci.py
"""
import bisect
import calendar
import collections
import itertools
import math
import os

from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

os.nice(15)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
t0 = calendar.timegm((2026, 11, 2, 6, 0, 0))
CRYPTO = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M")
wins = st.db.execute("SELECT ticker,series,close_ts,strike,result FROM windows WHERE close_ts>? AND series IN (?,?,?,?,?) AND result IN ('yes','no') AND strike>0 ORDER BY series,close_ts", (t0, *CRYPTO)).fetchall()
G0 = int(t0 - 3800)
G1 = int(max(float(w["close_ts"]) for w in wins)) + 2
FIB = (1, 2, 3, 5, 8, 13, 21, 34, 55)
LOOK = {m: 60 * m for m in FIB}
STRIDE = {m: max(1, 60 * m // 60) for m in FIB}
rows = []


def grid(ix):
    r = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (ix, G0 - 60)).fetchall()
    ts = [float(x[0]) for x in r]
    vs = [float(x[1]) for x in r]
    g, j, last = [], 0, None
    for t in range(G0, G1):
        while j < len(ts) and ts[j] <= t:
            last = vs[j]
            j += 1
        g.append(math.log(last) if last else None)
    sq = [0.0]
    for a, b in itertools.pairwise(g):
        sq.append(sq[-1] + ((b - a) ** 2 if a is not None and b is not None else 0.0))
    return g, sq


def std_and_resid(xs):
    n = len(xs)
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / n
    tm = (n - 1) / 2.0
    stt = sum((i - tm) ** 2 for i in range(n))
    cov = sum((i - tm) * (x - m) for i, x in enumerate(xs))
    res = max(0.0, var - (cov * cov / stt) / n)
    return math.sqrt(var), math.sqrt(res)


for ser in CRYPTO:
    g, sq = grid(variants.crypto_index(ser))
    for w in (x for x in wins if x["series"] == ser):
        close, K = float(w["close_ts"]), math.log(float(w["strike"]))
        opened = close - 900.0
        q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (w["ticker"], opened - 20, close)).fetchall()
        if len(q) < 20:
            continue
        qt = [float(x[0]) for x in q]
        for e in range(120, 841, 30):
            at = opened + e
            i = bisect.bisect_right(qt, at) - 1
            if i < 0 or at - qt[i] > 20:
                continue
            yb, ya = float(q[i][1]), float(q[i][2])
            k = int(at) - G0
            if k - 3300 < 0 or g[k] is None:
                continue
            left = close - at
            for side, bid, ask in (("yes", yb, ya), ("no", 1 - ya, 1 - yb)):
                if not (0.20 <= ask <= 0.70) or ask - bid > 0.04:
                    continue
                sg = 1 if side == "yes" else -1
                feat = {}
                ok = True
                for name, L in LOOK.items():
                    rate = math.sqrt((sq[k] - sq[k - L]) / L)
                    if rate <= 0 or g[k - L] is None:
                        ok = False
                        break
                    unit = rate * math.sqrt(L)
                    c = (g[k] - K) * sg
                    seg = [(g[j] - K) * sg for j in range(k - L, k + 1, STRIDE[name]) if g[j] is not None]
                    if len(seg) < 8:
                        ok = False
                        break
                    sd, _rs = std_and_resid(seg)
                    feat[name] = {"z": c / (rate * math.sqrt(left)), "slope": (c - (g[k - L] - K) * sg) / unit, "var": sd / unit}
                if not ok:
                    continue
                won = w["result"] == side
                contracts = 1.0 / ask
                net = ((1 - ask) * contracts if won else -1.0) - fee(ask) * contracts
                rows.append((ser + w["ticker"], side, e, ask, int(won), net, close, feat))
print("rows", len(rows), "markets", len({r[0] for r in rows}))
P67 = {m: sorted(r[7][m]["var"] for r in rows)[int(0.67 * (len(rows) - 1))] for m in FIB}
PB = lambda a: 0 if a < 0.35 else 1 if a < 0.5 else 2
day = lambda r: int(r[6] // 86400)
days = sorted({day(r) for r in rows})
by_day = collections.defaultdict(list)
for r in rows:
    by_day[day(r)].append(r)
K_, MIN_N = 10.0, 15
mid_list = sorted(r[6] for r in rows if day(r) in days[3:])
mid = mid_list[len(mid_list) // 2] if mid_list else 0
D45 = math.tan(math.radians(45.0))
dr = lambda sl: "u" if sl >= D45 else "d" if sl <= -D45 else "f"


def run(keyf, bar):
    kidx = {id(r): keyf(r[7]) for r in rows}
    out = []
    for di, d in enumerate(days):
        if di < 3:
            continue
        seen, rec = set(), collections.defaultdict(lambda: [0, 0.0])
        for dd in days[:di]:
            for r in sorted(by_day[dd], key=lambda x: x[2]):
                kk = (r[0], r[1], kidx[id(r)], PB(r[3]))
                if kk in seen:
                    continue
                seen.add(kk)
                c = rec[(kidx[id(r)], PB(r[3]))]
                c[0] += 1
                c[1] += r[5]
        bought = set()
        for r in sorted(by_day[d], key=lambda x: x[2]):
            mk = (r[0], r[1])
            if mk in bought:
                continue
            n, tot = rec.get((kidx[id(r)], PB(r[3])), (0, 0.0))
            if n >= MIN_N and tot / (n + K_) >= bar:
                bought.add(mk)
                out.append(r)
    return out


res = []
for a, b in itertools.combinations(FIB, 2):
    for shape in ("directions", "first-scale chop + directions"):
        if shape == "directions":
            kf = lambda f, a=a, b=b: dr(f[a]["slope"]) + dr(f[b]["slope"])
        else:
            kf = lambda f, a=a, b=b: dr(f[a]["slope"]) + ("c" if f[a]["var"] >= P67[a] else "s") + dr(f[b]["slope"])
        for bar in (0.10, 0.20):
            got = run(kf, bar)
            if len(got) >= 40:
                h1 = [r[5] * 10 for r in got if r[6] <= mid]
                h2 = [r[5] * 10 for r in got if r[6] > mid]
                res.append((sum(r[5] * 10 for r in got) / len(got), a, b, shape, bar, len(got), sum(r[4] for r in got) / len(got), sum(r[3] for r in got) / len(got), sum(h1) / max(len(h1), 1), len(h1), sum(h2) / max(len(h2), 1), len(h2)))
res.sort(reverse=True)
print("\nA. best 25 scale pairs (minutes) by net per $10 ticket on the later days")
for r in res[:25]:
    print(f"{r[1]:2d}m+{r[2]:2d}m {r[3]:30s} bar {r[4]:.2f} buys {r[5]:4d} won {100 * r[6]:3.0f}% vs {r[7]:.2f} net/$10 {r[0]:+.2f} | halves {r[8]:+.2f} (n={r[9]}) / {r[10]:+.2f} (n={r[11]})")
pos = [r for r in res if r[0] > 0 and r[8] > 0 and r[10] > 0]
print(f"\npairs positive overall and in both halves: {len(pos)} of {len(res)} (a quarter would be expected by chance)")
for r in sorted(res, key=lambda x: (x[1], x[2]))[:0]:
    pass
print("\nthe old pair 5m+55m for comparison:", [f"{r[3]} bar {r[4]:.2f}: buys {r[5]} net {r[0]:+.2f}" for r in res if r[1] == 5 and r[2] == 55])

FE = []
for m in FIB:
    FE += [(m, "slope"), (m, "var")]
X = [[1.0] + [r[7][m][k] for m, k in FE] + [r[3], 0.0] for r in rows]
mu = [sum(x[j] for x in X) / len(X) for j in range(len(X[0]))]
sd = [(sum((x[j] - mu[j]) ** 2 for x in X) / len(X)) ** 0.5 or 1.0 for j in range(len(X[0]))]
X = [[1.0] + [(x[j] - mu[j]) / sd[j] for j in range(1, len(x))] for x in X]
Y = [r[5] for r in rows]
Dd = [day(r) for r in rows]


def ridge(Xs, Ys, lam=50.0):
    k = len(Xs[0])
    A = [[0.0] * k for _ in range(k)]
    b = [0.0] * k
    for x, y in zip(Xs, Ys):
        for i_ in range(k):
            b[i_] += x[i_] * y
            xi = x[i_]
            for j_ in range(i_, k):
                A[i_][j_] += xi * x[j_]
    for i_ in range(k):
        for j_ in range(i_):
            A[i_][j_] = A[j_][i_]
        if i_:
            A[i_][i_] += lam
    M = [A[i_][:] + [b[i_]] for i_ in range(k)]
    for c_ in range(k):
        p_ = max(range(c_, k), key=lambda r_: abs(M[r_][c_]))
        M[c_], M[p_] = M[p_], M[c_]
        pv = M[c_][c_] or 1e-9
        M[c_] = [v / pv for v in M[c_]]
        for r_ in range(k):
            if r_ != c_ and M[r_][c_]:
                fac = M[r_][c_]
                M[r_] = [v - fac * w_ for v, w_ in zip(M[r_], M[c_])]
    return [M[i_][k] for i_ in range(k)]


print("\nB. ridge on the slope and variability at all nine scales + price + spread: realised bet-to-win net on the test days")
picks = {0.05: [], 0.10: [], 0.20: [], 1.0: []}
for di, d in enumerate(days):
    if di < 3:
        continue
    tr = [i_ for i_ in range(len(rows)) if Dd[i_] < d]
    te = [i_ for i_ in range(len(rows)) if Dd[i_] == d]
    if len(tr) < 2000 or not te:
        continue
    w_ = ridge([X[i_] for i_ in tr], [Y[i_] for i_ in tr])
    sc = sorted(((sum(a * b for a, b in zip(w_, X[i_])), i_) for i_ in te), reverse=True)
    for frac in picks:
        top = sc[: max(1, int(frac * len(sc)))]
        picks[frac] += [(Y[i_], rows[i_][3], rows[i_][6]) for _, i_ in top]
for frac, xs in picks.items():
    if xs:
        h1 = [p for p, c, t in xs if t <= mid]
        h2 = [p for p, c, t in xs if t > mid]
        print(f"  top {frac * 100:3.0f}%: n {len(xs):5d} net/$10 {10 * sum(p for p, _c, _t in xs) / len(xs):+.2f} ROI {100 * sum(p for p, _c, _t in xs) / sum(c for _p, c, _t in xs):+5.1f}% | halves {10 * sum(h1) / max(len(h1), 1):+.2f} (n={len(h1)}) / {10 * sum(h2) / max(len(h2), 1):+.2f} (n={len(h2)})")
print("DONE")
