"""Can the market's own signals (how fast the side's bid is falling, the spread, the book speeding up, the index's last 3 minutes against us) tell when holding is worth less than selling at the bid now? Replayed from the tape. Places nothing.

Moments: every 15 s in 15-minute crypto windows, from minute 3 to 2 minutes before the close, for the side whose mid is 55c or more and whose bid is at least 50c.
Target: holding minus selling now = settlement payoff (1 or 0) minus (bid minus the sale fee). Negative = selling now would have been better.
Features (all signed toward the side): bid change over the last 10 s, 30 s and 90 s (cents); spread (cents); how much faster the book is updating (log of theta now over theta 3 thetas ago... negative = speeding up);
index move over the last 3 minutes in its own size; minutes left.
Least squares clustered by 15-minute slot; fit on the earlier half of the windows, scored on the later half. Also the plain facts: how often the bid fell 10c+ within the next 2 minutes, and what holding was worth after such a fall.
    docker compose exec -T bot python - < scripts/crash_own_signals_study.py
"""
import bisect
import math
import statistics
from collections import defaultdict

from kalshi_moonshot import config, db
from kalshi_moonshot.kalshiws import index_for_series
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
CRYPTO = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M")
GRID = (2, 4, 6, 10, 15, 20, 30, 45, 60, 90, 120, 180, 240, 360, 480, 600)
NAMES = ("bid 10s", "bid 30s", "bid 90s", "spread", "book speed", "index 3m", "min left")
ticks, sigma = {}, {}
for ix in {index_for_series(x) for x in CRYPTO}:
    r = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND value > 0 ORDER BY ts", (ix,)).fetchall()
    ticks[ix] = ([x[0] for x in r], [x[1] for x in r])
    rets, t = [], ticks[ix][0][0] + 400
    while t < ticks[ix][0][-1]:
        a, b = bisect.bisect_right(ticks[ix][0], t) - 1, bisect.bisect_right(ticks[ix][0], t - 180) - 1
        rets.append(math.log(ticks[ix][1][a] / ticks[ix][1][b]))
        t += 120
    sigma[ix] = math.sqrt(sum(x * x for x in rets) / len(rets))


def value(ix, t):
    ts, v = ticks[ix]
    i = bisect.bisect_right(ts, t) - 1
    return v[i] if i >= 0 and t - ts[i] < 30 else None


def theta_at(ts, t):
    for w in GRID:
        if bisect.bisect_right(ts, t) - bisect.bisect_right(ts, t - w) >= 8:
            return float(w)
    return None


def at(ts, vals, t):
    i = bisect.bisect_right(ts, t) - 1
    return vals[i] if i >= 0 else None


def solve(a, b):
    n = len(a)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[p] = m[p], m[c]
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                m[r] = [x - f * y for x, y in zip(m[r], m[c], strict=True)]
    return [m[i][n] / m[i][i] for i in range(n)]


def ols(rows):
    k = len(NAMES) + 1
    xtx = [[0.0] * k for _ in range(k)]
    xty = [0.0] * k
    for r in rows:
        x = [1.0] + r["x"]
        for i in range(k):
            xty[i] += x[i] * r["y"]
            for j in range(k):
                xtx[i][j] += x[i] * x[j]
    beta = solve(xtx, xty)
    inv = [solve(xtx, [1.0 if i == j else 0.0 for i in range(k)]) for j in range(k)]
    score = defaultdict(lambda: [0.0] * k)
    for r in rows:
        x = [1.0] + r["x"]
        e = r["y"] - sum(b * v for b, v in zip(beta, x, strict=True))
        for i in range(k):
            score[r["cl"]][i] += x[i] * e
    meat = [[sum(sc[i] * sc[j] for sc in score.values()) for j in range(k)] for i in range(k)]
    cov = [[sum(inv[i][a] * meat[a][b] * inv[b][j] for a in range(k) for b in range(k)) for j in range(k)] for i in range(k)]
    return beta, [math.sqrt(max(cov[i][i], 0.0)) for i in range(k)]


rows = []
for m in main.db.execute(f"SELECT ticker, series, result, close_ts FROM windows WHERE series IN ({','.join('?' * len(CRYPTO))}) AND result IN ('yes','no')", CRYPTO).fetchall():
    ix = index_for_series(m["series"])
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 ORDER BY ts", (m["ticker"],)).fetchall()
    if len(q) < 60 or ix is None:
        continue
    qts = [r[0] for r in q]
    close = m["close_ts"]
    for side in ("yes", "no"):
        bids = [r[1] if side == "yes" else 1 - r[2] for r in q]
        asks = [r[2] if side == "yes" else 1 - r[1] for r in q]
        win = 1.0 if m["result"] == side else 0.0
        sign = 1.0 if side == "yes" else -1.0
        t = close - 900 + 180
        while t < close - 120:
            i = bisect.bisect_right(qts, t) - 1
            t += 15
            if i < 0:
                continue
            b, a = bids[i], asks[i]
            if (a + b) / 2 < 0.55 or b < 0.50:
                continue
            b10, b30, b90 = (at(qts, bids, qts[i] - w) for w in (10, 30, 90))
            th = theta_at(qts, qts[i])
            earlier = theta_at(qts, qts[i] - 3 * th) if th else None
            now, then = value(ix, qts[i]), value(ix, qts[i] - 180)
            if None in (b10, b30, b90, th, earlier, now, then):
                continue
            j = bisect.bisect_right(qts, qts[i] + 120) - 1
            future_low = min(bids[i:j + 1]) if j > i else b
            x = [(b - b10) * 100, (b - b30) * 100, (b - b90) * 100, (a - b) * 100, math.log(th / earlier), sign * math.log(now / then) / sigma[ix], (close - qts[i]) / 60.0]
            held_minus_sold = win - (b - fee(b))
            rows.append({"cl": close, "x": x, "y": held_minus_sold, "crash": (b - future_low) >= 0.10, "win": win, "t": close})
print(f"{len(rows):,} moments, {len({r['cl'] for r in rows})} windows; holding minus selling now averages {100 * statistics.mean(r['y'] for r in rows):+.2f}c")
crashed = [r for r in rows if r["crash"]]
print(f"the bid fell 10c or more within the next 2 minutes after {len(crashed) / len(rows):.1%} of moments; of those, {statistics.mean(r['win'] for r in crashed):.0%} still won; holding minus selling now was {100 * statistics.mean(r['y'] for r in crashed):+.1f}c")
beta, err = ols(rows)
print("\\nall data, effect on (holding minus selling now) per unit of each signal, clustered by window")
for name, bt, e in zip(("intercept",) + NAMES, beta, err, strict=True):
    print(f"  {name:11} {bt:+.5f} +-{e:.5f}  (t = {bt / e:+.1f})")
cut = sorted({r["t"] for r in rows})[len({r["t"] for r in rows}) // 2]
train, test = [r for r in rows if r["t"] <= cut], [r for r in rows if r["t"] > cut]
bt, _ = ols(train)


def fitted(r):
    return bt[0] + sum(b * v for b, v in zip(bt[1:], r["x"], strict=True))


def line(label, g):
    if len(g) < 30:
        print(f"  {label:34} only {len(g)}")
        return
    per = defaultdict(list)
    for r in g:
        per[r["cl"]].append(r["y"])
    mm = [sum(v) / len(v) for v in per.values()]
    sd = math.sqrt(sum((x - sum(mm) / len(mm)) ** 2 for x in mm) / (len(mm) - 1) / len(mm)) if len(mm) > 1 else float("nan")
    print(f"  {label:34} n={len(g):6} ({len(per):3} windows)  holding minus selling now {100 * statistics.mean(r['y'] for r in g):+6.2f}c +-{100 * sd:.2f}")


print(f"\\nfit on the earlier {len(train):,}, scored on the later {len(test):,}: holding minus selling now (negative = selling now better)")
line("every test moment", test)
for label, c in (("forecast says sell by 2c+", -0.02), ("forecast says sell by 4c+", -0.04), ("forecast says sell by 8c+", -0.08)):
    line(label, [r for r in test if fitted(r) < c])
    line("   the rest", [r for r in test if fitted(r) >= c])
