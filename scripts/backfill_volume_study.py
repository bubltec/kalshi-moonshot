"""Does recent volume say which favorites fail? OLS of a favorite's edge on volume features, on the weeks of crypto markets saved by scripts/kalshi_bulk_pull.py.

The saved candles carry total volume per minute and the last trade price but not which side the takers were on, so the flow imbalance of volume_flow_study.py cannot be
rebuilt here; the volume features are:
    burst   log(1 + contracts in the last minute) minus log(1 + the market's own average per minute before the last 3 minutes)
    level   log(1 + contracts traded since the open): how active the market is
    spread  best ask minus best bid at that moment, in cents
    mom2    the favorite's mid now minus 2 minutes ago, in cents, signed toward the favorite
    tilt    the last trade price minus the mid, in cents, signed toward the favorite (a print near the ask means buying pressure)
Every favorite (the side priced FAV_LO to FAV_HI) 7 minutes before its close gets the EDGE = 100 x (1 if it won else 0) - its price, cents, before fees. OLS of the edge
on the five features, fitted on the first 60% of the markets by close time and scored on the last 40%; then mean edge by burst and tilt bucket. Analysis only.

    python - [DIR=/data/backfill_crypto] [FAV_LO=0.75] [FAV_HI=0.95] < scripts/backfill_volume_study.py          (inside the bot container)
"""
import glob
import json
import math
import os

DIR = os.environ.get("DIR", "/data/backfill_crypto")
FAV_LO = float(os.environ.get("FAV_LO", "0.75"))
FAV_HI = float(os.environ.get("FAV_HI", "0.95"))

rows = []  # (close_ts, edge, [burst, level, spread, mom2, tilt])
for path in sorted(glob.glob(os.path.join(DIR, "*.jsonl"))):
    for line in open(path):
        m = json.loads(line)
        close = int(m["close_ts"])
        t0 = close - 420
        by = {c["t"]: c for c in m["candles"]}
        c0, c2 = by.get(t0), by.get(t0 - 120)
        if not c0 or not c2 or None in (c0.get("bc"), c0.get("ac"), c2.get("bc"), c2.get("ac")):
            continue
        bid, ask = float(c0["bc"]), float(c0["ac"])
        mid = (bid + ask) / 2.0
        mid2 = (float(c2["bc"]) + float(c2["ac"])) / 2.0
        sgn = 1.0 if mid >= 0.5 else -1.0
        fav = mid if sgn > 0 else 1.0 - mid
        if not FAV_LO <= fav <= FAV_HI:
            continue
        vols = {t: float(c.get("v") or 0.0) for t, c in by.items() if t <= t0}
        v_last = vols.get(t0, 0.0)
        earlier = [v for t, v in vols.items() if t <= t0 - 180]
        base = sum(earlier) / max(len(earlier), 1)
        px = c0.get("px")
        tilt = 0.0 if px is None else sgn * (float(px) - mid) * 100.0
        feats = [math.log1p(v_last) - math.log1p(base), math.log1p(sum(vols.values())), (ask - bid) * 100.0, sgn * (mid - mid2) * 100.0, tilt]
        won = (m["result"] == "yes") == (sgn > 0)
        rows.append((close, ((1.0 if won else 0.0) - fav) * 100.0, feats))

rows.sort(key=lambda r: r[0])
n = len(rows)
if n < 200:
    print(f"only {n} favorites")
    raise SystemExit
cut = int(n * 0.6)
train, test = rows[:cut], rows[cut:]


def solve(a, b):
    k = len(a)
    mt = [row[:] + [b[i]] + [1.0 if i == j else 0.0 for j in range(k)] for i, row in enumerate(a)]
    for c in range(k):
        p = max(range(c, k), key=lambda r: abs(mt[r][c]))
        if abs(mt[p][c]) < 1e-12:
            return None, None
        mt[c], mt[p] = mt[p], mt[c]
        piv = mt[c][c]
        mt[c] = [v / piv for v in mt[c]]
        for r in range(k):
            if r != c:
                f = mt[r][c]
                mt[r] = [v - f * u for v, u in zip(mt[r], mt[c], strict=True)]
    return [mt[i][k] for i in range(k)], [[mt[i][k + 1 + j] for j in range(k)] for i in range(k)]


def ols(rs):
    x = [[1.0] + r[2] for r in rs]
    y = [r[1] for r in rs]
    k = len(x[0])
    xtx = [[sum(r[i] * r[j] for r in x) for j in range(k)] for i in range(k)]
    xty = [sum(r[i] * yy for r, yy in zip(x, y, strict=True)) for i in range(k)]
    beta, inv = solve(xtx, xty)
    resid = [yy - sum(b * v for b, v in zip(beta, r, strict=True)) for r, yy in zip(x, y, strict=True)]
    s2 = sum(e * e for e in resid) / (len(y) - k)
    se = [math.sqrt(max(s2 * inv[i][i], 0.0)) for i in range(k)]
    return beta, [b / e if e > 0 else 0.0 for b, e in zip(beta, se, strict=True)]


fit, full = ols(train), ols(rows)
xt = [[1.0] + r[2] for r in test]
yt = [r[1] for r in test]
ym = sum(yt) / len(yt)
sse = sum((yy - sum(b * v for b, v in zip(fit[0], r, strict=True))) ** 2 for r, yy in zip(xt, yt, strict=True))
sst = sum((yy - ym) ** 2 for yy in yt)
print(f"{n} favorites priced {FAV_LO:g}-{FAV_HI:g} at 7 minutes left; edge cents: mean {sum(r[1] for r in rows) / n:+.2f}, win rate {sum(1 for r in rows if r[1] > 0) / n:.0%}")
print(f"{'':<8} {'coef c/unit':>12} {'t':>6}   (all markets; train {len(train)}, test {len(test)}; out-of-sample R2 of the model fitted on train: {(1 - sse / sst) * 100:+.2f}%)")
for nm, b, t in zip(["const", "burst", "level", "spread", "mom2", "tilt"], full[0], full[1], strict=True):
    print(f"{nm:<8} {b:>+12.3f} {t:>+6.1f}")


def table(idx, label, edges):
    print(f"\n{label:<18} {'n':>5} {'win%':>5} {'edge c':>8} | {'train':>5} {'edge c':>8} | {'test':>5} {'edge c':>8}")
    for lo, hi, name in edges:
        def pick(rs):
            return [r for r in rs if lo < r[2][idx] <= hi]
        g, gt, gs = pick(rows), pick(train), pick(test)
        if not g:
            continue
        mean = lambda rs: sum(r[1] for r in rs) / len(rs) if rs else float("nan")  # noqa: E731
        print(f"{name:<18} {len(g):>5} {sum(1 for r in g if r[1] > 0) / len(g):>5.0%} {mean(g):>+8.2f} | {len(gt):>5} {mean(gt):>+8.2f} | {len(gs):>5} {mean(gs):>+8.2f}")


table(0, "burst (last minute)", ((-99, -1.0, "<= -1 (quiet)"), (-1.0, 0.0, "-1 .. 0"), (0.0, 1.0, "0 .. 1"), (1.0, 2.0, "1 .. 2"), (2.0, 99, "> 2 (surge)")))
table(4, "tilt (c, toward fav)", ((-99, -1.0, "<= -1 (against)"), (-1.0, 0.0, "-1 .. 0"), (0.0, 0.001, "0"), (0.001, 1.0, "0 .. 1"), (1.0, 99, "> 1 (toward)")))
