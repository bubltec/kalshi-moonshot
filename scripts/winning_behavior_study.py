"""What does a market that gets to 99c look like, compared with one that does not? 15-minute crypto windows replayed from the tape. Places nothing.

Moments: every 30 s from minute 3 to 2 minutes before the close, for each side whose ask is 70-92c with a spread of 8c or less.
Outcomes: win (the side settles at $1); reach99 (its bid touches 99c before the close); edge = win - ask, the part the price did not already say.
Features, all signed toward the side and measured at the moment:
  ask, spread (c), minutes elapsed
  rise30, rise90   ask change over the last 30 s / 90 s (c)
  dip90            how far the bid dipped under its starting level in the last 90 s (c)
  held             minutes since the ask first reached 70c in this window
  imbalance        (bid size - ask size) / (bid size + ask size)
  updates60        book updates in the last 60 s
  dist             how far the index is on the side's side of the strike, in units of its usual move to the close (log distance / (per-minute volatility x sqrt(minutes left)))
  s03 ... s60120   the six index slices (forecast.py)
Least squares of each outcome on the standardized features, clustered by 15-minute window; then fit on the earlier half and bucket the later half by the fitted chance.
    docker compose exec -T bot python - < scripts/winning_behavior_study.py
"""
import bisect
import math
import statistics
from collections import defaultdict

from kalshi_moonshot import config, db, forecast
from kalshi_moonshot.kalshiws import index_for_series
from kalshi_moonshot.store import Store

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
CRYPTO = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M")
EDGES = forecast.EDGES
NAMES = ["ask", "spread", "elapsed", "rise30", "rise90", "dip90", "held", "imbalance", "updates60", "dist"] + [f"s{lo}-{hi}" for lo, hi in zip(EDGES, EDGES[1:], strict=False)]
ticks, sigma, volmin = {}, {}, {}
for ix in {index_for_series(x) for x in CRYPTO}:
    r = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND value > 0 ORDER BY ts", (ix,)).fetchall()
    ticks[ix] = ([x[0] for x in r], [x[1] for x in r])
    ts_, vs = ticks[ix]
    sigma[ix] = {}
    for w in EDGES[1:]:
        rets, t = [], ts_[0] + w * 60 + 60
        while t < ts_[-1]:
            a, b = bisect.bisect_right(ts_, t) - 1, bisect.bisect_right(ts_, t - w * 60) - 1
            rets.append(math.log(vs[a] / vs[b]))
            t += 120
        sigma[ix][w] = math.sqrt(sum(x * x for x in rets) / len(rets))
    volmin[ix] = sigma[ix][3] / math.sqrt(3)


def val(ix, t):
    ts_, vs = ticks[ix]
    i = bisect.bisect_right(ts_, t) - 1
    return vs[i] if i >= 0 and t - ts_[i] < 30 else None


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


def ols(rows, key, mean, sd):
    k = len(NAMES) + 1
    xtx = [[0.0] * k for _ in range(k)]
    xty = [0.0] * k
    xs = []
    for r in rows:
        x = [1.0] + [(v - m) / d for v, m, d in zip(r["x"], mean, sd, strict=True)]
        xs.append(x)
        for i in range(k):
            xty[i] += x[i] * r[key]
            for j in range(k):
                xtx[i][j] += x[i] * x[j]
    beta = solve(xtx, xty)
    inv = [solve(xtx, [1.0 if i == j else 0.0 for i in range(k)]) for j in range(k)]
    score = defaultdict(lambda: [0.0] * k)
    for r, x in zip(rows, xs, strict=True):
        e = r[key] - sum(b * v for b, v in zip(beta, x, strict=True))
        for i in range(k):
            score[r["cl"]][i] += x[i] * e
    meat = [[sum(sc[i] * sc[j] for sc in score.values()) for j in range(k)] for i in range(k)]
    cov = [[sum(inv[i][a] * meat[a][b] * inv[b][j] for a in range(k) for b in range(k)) for j in range(k)] for i in range(k)]
    return beta, [math.sqrt(max(cov[i][i], 0.0)) for i in range(k)]


rows = []
for m in main.db.execute(f"SELECT ticker, series, result, close_ts, strike FROM windows WHERE series IN ({','.join('?' * len(CRYPTO))}) AND result IN ('yes','no') AND strike IS NOT NULL", CRYPTO).fetchall():
    ix = index_for_series(m["series"])
    q = tape.execute("SELECT ts, bid, ask, bid_size, ask_size FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 ORDER BY ts", (m["ticker"],)).fetchall()
    if len(q) < 60 or ix is None:
        continue
    qts = [r[0] for r in q]
    close = m["close_ts"]
    for side in ("yes", "no"):
        bids = [r[1] if side == "yes" else 1 - r[2] for r in q]
        asks = [r[2] if side == "yes" else 1 - r[1] for r in q]
        bsz = [r[3] if side == "yes" else r[4] for r in q]
        asz = [r[4] if side == "yes" else r[3] for r in q]
        win = 1.0 if m["result"] == side else 0.0
        sign = 1.0 if side == "yes" else -1.0
        first70 = next((qts[k] for k, a in enumerate(asks) if a >= 0.70), None)
        t = close - 900 + 180
        while t < close - 120:
            i = bisect.bisect_right(qts, t) - 1
            t += 30
            if i < 0 or first70 is None or qts[i] < first70:
                continue
            a, b = asks[i], bids[i]
            if not (0.70 <= a <= 0.92) or a - b > 0.08:
                continue
            i30, i90 = bisect.bisect_right(qts, qts[i] - 30) - 1, bisect.bisect_right(qts, qts[i] - 90) - 1
            if i30 < 0 or i90 < 0:
                continue
            window90 = bids[i90:i + 1]
            now_v = val(ix, qts[i])
            pts = [val(ix, qts[i] - mnt * 60) for mnt in EDGES]
            if now_v is None or None in pts:
                continue
            left = max(0.5, (close - qts[i]) / 60)
            tot = (bsz[i] or 0) + (asz[i] or 0)
            sl = [sign * math.log(pts[n] / pts[n + 1]) / (sigma[ix][EDGES[n + 1]] * math.sqrt((EDGES[n + 1] - EDGES[n]) / EDGES[n + 1])) for n in range(len(EDGES) - 1)]
            x = [a, (a - b) * 100, (qts[i] - (close - 900)) / 60, (a - asks[i30]) * 100, (a - asks[i90]) * 100, max(0.0, window90[0] - min(window90)) * 100, (qts[i] - first70) / 60,
                 ((bsz[i] or 0) - (asz[i] or 0)) / tot if tot else 0.0, float(i - bisect.bisect_right(qts, qts[i] - 60) + 1), sign * math.log(now_v / m["strike"]) / (volmin[ix] * math.sqrt(left))] + sl
            reach = 1.0 if max(bids[i:]) >= 0.99 else 0.0
            rows.append({"cl": close, "x": x, "win": win, "reach": reach, "edge": win - a, "t": close})
print(f"{len(rows):,} moments, {len({r['cl'] for r in rows})} windows; won {statistics.mean(r['win'] for r in rows):.1%}, bid touched 99c {statistics.mean(r['reach'] for r in rows):.1%}, average ask {statistics.mean(r['x'][0] for r in rows):.3f}")
mean = [statistics.mean(r["x"][k] for r in rows) for k in range(len(NAMES))]
sd = [statistics.pstdev([r["x"][k] for r in rows]) or 1.0 for k in range(len(NAMES))]
for key, label in (("win", "chance of winning"), ("edge", "edge beyond the price (win - ask)")):
    beta, err = ols(rows, key, mean, sd)
    print(f"\\n{label}: effect of one standard deviation of each feature, clustered by window")
    ranked = sorted(range(len(NAMES)), key=lambda k: -abs(beta[k + 1] / err[k + 1]))
    for k in ranked:
        t_ = beta[k + 1] / err[k + 1]
        print(f"  {NAMES[k]:10} {100 * beta[k + 1]:+6.2f} points  (t = {t_:+5.1f}){'  <--' if abs(t_) >= 2 else ''}   typical {mean[k]:.2f} (sd {sd[k]:.2f})")
cuts = sorted({r["t"] for r in rows})
cut = cuts[len(cuts) // 2]
train, test = [r for r in rows if r["t"] <= cut], [r for r in rows if r["t"] > cut]
beta, _ = ols(train, "win", mean, sd)
for r in test:
    r["f"] = beta[0] + sum(b * (v - m) / d for b, v, m, d in zip(beta[1:], r["x"], mean, sd, strict=True))
test.sort(key=lambda r: r["f"])
print(f"\\nfit on the earlier {len(train):,} moments; the later {len(test):,} in thirds by fitted chance of winning (lowest to highest)")
for n, part in enumerate((test[:len(test) // 3], test[len(test) // 3:2 * len(test) // 3], test[2 * len(test) // 3:])):
    per = defaultdict(list)
    for r in part:
        per[r["cl"]].append(r["edge"])
    mm = [sum(v) / len(v) for v in per.values()]
    sdv = math.sqrt(sum((x - sum(mm) / len(mm)) ** 2 for x in mm) / (len(mm) - 1) / len(mm)) * 100
    print(f"  third {n + 1}: average ask {statistics.mean(r['x'][0] for r in part):.3f}, won {statistics.mean(r['win'] for r in part):5.1%}, touched 99c {statistics.mean(r['reach'] for r in part):5.1%}, edge beyond the price {100 * statistics.mean(r['edge'] for r in part):+5.1f} points +-{sdv:.1f}")
