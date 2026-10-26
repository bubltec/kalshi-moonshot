"""Does the settlement index's move over several look-back windows (3, 7, 15, 30, 60, 120 minutes) forecast a 15-minute crypto favorite beyond its price? Replayed from the tape. Places nothing.

Every 30 s from 5 minutes into a window to 2 minutes before it closes, for the side the market leans toward (mid 55c or more, ask up to 92c, spread 8c or less):
  z_w     the index's log move over the last w minutes, signed toward that side, divided by the usual size of a w-minute move for that coin (so each window is comparable)
  y       win (1/0) minus ask minus the buy fee: the profit per contract of buying at the ask and holding
Least squares of y on the six z_w (clustered by 15-minute slot, since the coins move together). A coefficient says how much that window moves the edge per unit of its own normal size, with the other windows held fixed.
Out of sample: fit on the earlier half of the windows, then buy only the test moments where the fitted edge is above zero (and above 1c); compare with buying every test moment.
    docker compose exec -T bot python - < scripts/multiwindow_forecast_study.py
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
WINDOWS = (3, 7, 15, 30, 60, 120)
ticks = {}
sigma = {}
for ix in {index_for_series(x) for x in CRYPTO}:
    r = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND value > 0 ORDER BY ts", (ix,)).fetchall()
    ticks[ix] = ([x[0] for x in r], [x[1] for x in r])
    t0, t1 = ticks[ix][0][0], ticks[ix][0][-1]
    sigma[ix] = {}
    for w in WINDOWS:
        rets, t = [], t0 + 7300
        while t < t1:
            a, b = bisect.bisect_right(ticks[ix][0], t) - 1, bisect.bisect_right(ticks[ix][0], t - w * 60) - 1
            rets.append(math.log(ticks[ix][1][a] / ticks[ix][1][b]))
            t += 120
        sigma[ix][w] = math.sqrt(sum(x * x for x in rets) / len(rets))


def value(ix, t):
    ts, v = ticks[ix]
    i = bisect.bisect_right(ts, t) - 1
    return v[i] if i >= 0 and t - ts[i] < 30 else None


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


def inverse(a):
    n = len(a)
    cols = [solve(a, [1.0 if i == j else 0.0 for i in range(n)]) for j in range(n)]
    return [[cols[j][i] for j in range(n)] for i in range(n)]


def ols(rows):
    k = len(WINDOWS) + 1
    xtx = [[0.0] * k for _ in range(k)]
    xty = [0.0] * k
    for r in rows:
        x = [1.0] + r["z"]
        for i in range(k):
            xty[i] += x[i] * r["y"]
            for j in range(k):
                xtx[i][j] += x[i] * x[j]
    beta = solve(xtx, xty)
    inv = inverse(xtx)
    meat = [[0.0] * k for _ in range(k)]
    score = defaultdict(lambda: [0.0] * k)
    for r in rows:
        x = [1.0] + r["z"]
        e = r["y"] - sum(b * v for b, v in zip(beta, x, strict=True))
        for i in range(k):
            score[r["cl"]][i] += x[i] * e
    for sc in score.values():
        for i in range(k):
            for j in range(k):
                meat[i][j] += sc[i] * sc[j]
    cov = [[sum(inv[i][a] * meat[a][b] * inv[b][j] for a in range(k) for b in range(k)) for j in range(k)] for i in range(k)]
    return beta, [math.sqrt(max(cov[i][i], 0.0)) for i in range(k)]


rows = []
for m in main.db.execute(f"SELECT ticker, series, result, close_ts FROM windows WHERE series IN ({','.join('?' * len(CRYPTO))}) AND result IN ('yes','no')", CRYPTO).fetchall():
    ix = index_for_series(m["series"])
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 ORDER BY ts", (m["ticker"],)).fetchall()
    if len(q) < 60 or ix is None:
        continue
    qts = [r[0] for r in q]
    close, t = m["close_ts"], m["close_ts"] - 900 + 300
    while t < close - 120:
        i = bisect.bisect_right(qts, t) - 1
        t += 30
        if i < 0:
            continue
        by, ay = q[i][1], q[i][2]
        side = "yes" if (by + ay) / 2 >= 0.5 else "no"
        a, b = (ay, by) if side == "yes" else (1 - by, 1 - ay)
        if not (0.55 <= (a + b) / 2 and a <= 0.92 and a - b <= 0.08):
            continue
        now = value(ix, qts[i])
        sign = 1.0 if side == "yes" else -1.0
        zs = []
        for w in WINDOWS:
            then = value(ix, qts[i] - w * 60)
            if now is None or then is None:
                break
            zs.append(sign * math.log(now / then) / sigma[ix][w])
        if len(zs) < len(WINDOWS):
            continue
        win = (m["result"] == side)
        rows.append({"cl": m["close_ts"], "z": zs, "y": (1.0 if win else 0.0) - a - fee(a), "ret": ((1.0 if win else 0.0) - a - fee(a)) / (a + fee(a)), "t": m["close_ts"]})
print(f"{len(rows):,} moments, {len({r['cl'] for r in rows})} slots; average edge after fee {statistics.mean(r['y'] for r in rows):+.4f} per contract")
beta, err = ols(rows)
print("\nall data: edge per contract by window (own-size units), clustered by slot")
for name, bt, e in zip(("intercept",) + tuple(f"{w:>3} min" for w in WINDOWS), beta, err, strict=True):
    print(f"  {name:9} {bt:+.4f} +-{e:.4f}  (t = {bt / e:+.1f})")
cut = sorted({r["t"] for r in rows})[len(set(r["t"] for r in rows)) // 2]
train, test = [r for r in rows if r["t"] <= cut], [r for r in rows if r["t"] > cut]
bt, _ = ols(train)
print(f"\nfit on the earlier {len(train):,} moments, tested on the later {len(test):,}")
print("  weights found on the earlier half:", " ".join(f"{w}m {v:+.3f}" for w, v in zip(WINDOWS, bt[1:], strict=True)))


def line(label, g):
    if len(g) < 30:
        print(f"  {label:34} only {len(g)}")
        return
    per = defaultdict(list)
    for r in g:
        per[r["cl"]].append(r["ret"])
    mm = [sum(v) / len(v) for v in per.values()]
    sd = math.sqrt(sum((x - sum(mm) / len(mm)) ** 2 for x in mm) / (len(mm) - 1) / len(mm)) if len(mm) > 1 else float("nan")
    print(f"  {label:34} n={len(g):5} ({len(per):3} slots)  return {statistics.mean(r['ret'] for r in g):+6.1%} +-{100 * sd:.1f}")


def fitted(r):
    return bt[0] + sum(b * v for b, v in zip(bt[1:], r["z"], strict=True))


line("every test moment", test)
line("fitted edge > 0", [r for r in test if fitted(r) > 0])
line("fitted edge > 1c", [r for r in test if fitted(r) > 0.01])
line("fitted edge > 2c", [r for r in test if fitted(r) > 0.02])
line("fitted edge <= 0", [r for r in test if fitted(r) <= 0])
