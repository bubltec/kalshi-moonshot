"""Does recent volume and order flow say which favorites fail? OLS of a favorite's edge on flow features, judged out of sample. Replayed from the tape.

Every settled 15-minute market on the tape is looked at 7 minutes before its close (where the commodity lane buys). When its favorite side (the side priced
FAV_LO to FAV_HI) is there, the EDGE is what the market did not price: 100 x (1 if the favorite won else 0) - its price, in cents, before fees. The features,
all signed so that positive means "toward the favorite":

    flow60, flow180   taker imbalance (YES-taker contracts minus NO-taker contracts, over their sum; for a NO favorite the sign flips) over the last 60 / 180 s
    burst             log(1 + contracts in the last 60 s) minus log(1 + the market's own average per-minute contracts from its open to 60 s before)
    book              best-bid size minus best-ask size over their sum, toward the favorite (a NO favorite's support is the YES ask size)

OLS of EDGE on the four features on the first 60% of the slots (by close time), judged on the last 40%: the coefficients (cents per unit), their t-stats and
the out-of-sample R2, then the mean edge by flow60 bucket. An edge of several cents in a bucket, stable out of sample, is a filter worth testing; a fee is
about 1-2 cents. Analysis only: this places nothing.

    scripts/cloud.sh run scripts/volume_flow_study.py [DAYS=3] [FAV_LO=0.75] [FAV_HI=0.95]
"""
import math
import os
import time

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)

DAYS = float(os.environ.get("DAYS", "3"))
FAV_LO = float(os.environ.get("FAV_LO", "0.75"))
FAV_HI = float(os.environ.get("FAV_HI", "0.95"))

since = time.time() - DAYS * 86400
wrows = main.db.execute("SELECT ticker, series, close_ts, result FROM windows WHERE result IN ('yes','no') AND close_ts >= ? ORDER BY close_ts", (since,)).fetchall()

rows = []  # (close_ts, edge in cents, [flow60, flow180, burst, book], series)
for w in wrows:
    close = float(w["close_ts"])
    t0 = close - 420
    q = tape.execute("SELECT bid, ask, bid_size, ask_size FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 AND ts BETWEEN ? AND ? ORDER BY ts DESC LIMIT 1", (w["ticker"], t0 - 10, t0)).fetchone()
    if not q:
        continue
    bid, ask, bsz, asz = float(q[0]), float(q[1]), float(q[2] or 0), float(q[3] or 0)
    mid = (bid + ask) / 2.0
    sgn = 1.0 if mid >= 0.5 else -1.0
    fav = mid if sgn > 0 else 1.0 - mid
    if not FAV_LO <= fav <= FAV_HI:
        continue
    flow = tape.execute("SELECT ts, yes_vol, no_vol FROM flow_1s WHERE ticker=? AND ts > ? AND ts <= ? ORDER BY ts", (w["ticker"], int(close - 900), int(t0))).fetchall()

    def imb(seconds):
        y = sum(float(r[1]) for r in flow if r[0] > t0 - seconds)
        n = sum(float(r[2]) for r in flow if r[0] > t0 - seconds)
        return 0.0 if y + n <= 0 else sgn * (y - n) / (y + n)

    v60 = sum(float(r[1]) + float(r[2]) for r in flow if r[0] > t0 - 60)
    earlier = [float(r[1]) + float(r[2]) for r in flow if r[0] <= t0 - 60]
    span = max(t0 - 60 - (close - 900), 60.0)
    base = sum(earlier) / (span / 60.0)
    burst = math.log1p(v60) - math.log1p(base)
    book = 0.0 if bsz + asz <= 0 else sgn * (bsz - asz) / (bsz + asz)
    won = (w["result"] == "yes") == (sgn > 0)
    rows.append((close, ((1.0 if won else 0.0) - fav) * 100.0, [imb(60), imb(180), burst, book], w["series"]))

rows.sort(key=lambda r: r[0])
n = len(rows)
if n < 100:
    print(f"only {n} favorites in {FAV_LO:g}-{FAV_HI:g} with a quote and flow")
    raise SystemExit
cut = int(n * 0.6)
train, test = rows[:cut], rows[cut:]


def design(rs):
    return [[1.0] + r[2] for r in rs], [r[1] for r in rs]


def solve(a, b):
    """Gauss-Jordan on A x = b (small, dense). Returns x and the inverse of A (for standard errors)."""
    k = len(a)
    m = [row[:] + [b[i]] + [1.0 if i == j else 0.0 for j in range(k)] for i, row in enumerate(a)]
    for c in range(k):
        p = max(range(c, k), key=lambda r: abs(m[r][c]))
        if abs(m[p][c]) < 1e-12:
            return None, None
        m[c], m[p] = m[p], m[c]
        piv = m[c][c]
        m[c] = [v / piv for v in m[c]]
        for r in range(k):
            if r != c:
                f = m[r][c]
                m[r] = [v - f * u for v, u in zip(m[r], m[c], strict=True)]
    return [m[i][k] for i in range(k)], [[m[i][k + 1 + j] for j in range(k)] for i in range(k)]


def ols(rs):
    x, y = design(rs)
    k = len(x[0])
    xtx = [[sum(r[i] * r[j] for r in x) for j in range(k)] for i in range(k)]
    xty = [sum(r[i] * yy for r, yy in zip(x, y, strict=True)) for i in range(k)]
    beta, inv = solve(xtx, xty)
    if beta is None:
        return None
    resid = [yy - sum(bb * v for bb, v in zip(beta, r, strict=True)) for r, yy in zip(x, y, strict=True)]
    s2 = sum(e * e for e in resid) / (len(y) - k)
    se = [math.sqrt(max(s2 * inv[i][i], 0.0)) for i in range(k)]
    return beta, [b / e if e > 0 else 0.0 for b, e in zip(beta, se, strict=True)]


fit = ols(train)
full = ols(rows)
print(f"{n} favorites priced {FAV_LO:g}-{FAV_HI:g} at 7 minutes left, last {DAYS:g} days; edge = outcome minus price, cents; mean {sum(r[1] for r in rows) / n:+.2f}c, win rate {sum(1 for r in rows if r[1] > 0) / n:.0%}")
names = ["intercept", "flow60", "flow180", "burst", "book"]
if fit and full:
    xt, yt = design(test)
    ym = sum(yt) / len(yt)
    sse = sum((yy - sum(b * v for b, v in zip(fit[0], r, strict=True))) ** 2 for r, yy in zip(xt, yt, strict=True))
    sst = sum((yy - ym) ** 2 for yy in yt)
    print(f"{'':<10} {'coef c/unit':>12} {'t':>6}   (all slots; train {len(train)}, test {len(test)}; out-of-sample R2 of the four-feature model fitted on train: {(1 - sse / sst) * 100:+.1f}%)")
    for nm, b, t in zip(names, full[0], full[1], strict=True):
        print(f"{nm:<10} {b:>+12.2f} {t:>+6.1f}")
print(f"\n{'flow60 bucket':<16} {'n':>5} {'avg price':>9} {'win%':>5} {'edge c':>8} | {'train':>5} {'edge c':>8} | {'test':>5} {'edge c':>8}")
for lo, hi, label in ((-9, -0.5, "<= -0.5 (against)"), (-0.5, -0.1, "-0.5 .. -0.1"), (-0.1, 0.1, "about zero / none"), (0.1, 0.5, "0.1 .. 0.5"), (0.5, 9, ">= 0.5 (toward)")):
    def grp(rs):
        return [r for r in rs if lo < r[2][0] <= hi] if lo > -9 else [r for r in rs if r[2][0] <= hi]
    g, gt, gs = grp(rows), grp(train), grp(test)
    if not g:
        continue
    price = sum((r[1] / 100.0 - (1.0 if r[1] > 0 else 0.0)) * -1 for r in g) / len(g)  # price = win - edge/100, averaged
    mean = lambda rs: sum(r[1] for r in rs) / len(rs) if rs else float("nan")  # noqa: E731
    print(f"{label:<16} {len(g):>5} {price:>9.2f} {sum(1 for r in g if r[1] > 0) / len(g):>5.0%} {mean(g):>+8.2f} | {len(gt):>5} {mean(gt):>+8.2f} | {len(gs):>5} {mean(gs):>+8.2f}")
