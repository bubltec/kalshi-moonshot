"""The asset-price pairs test of asset_pairs_study.py, on the weeks of crypto markets that scripts/kalshi_bulk_pull.py saved.

Each saved market has its strike (the coin's reference price at the open), its result and Kalshi's own 1-minute bid and ask candles, so a series of strikes is a
series of the coin every 15 minutes for the whole period, and the contract's YES mid 7 minutes before the close is the candle that ends then. For each of the PAIRS
most correlated pairs of coins (correlation of 15-minute log returns), Y on X is fitted by OLS on the previous WINDOW slots,

    log Y = a + b log X + e,        z = (the current e) / (the window's residual standard deviation)

with only past slots in each fit, and the slope judged out of sample (fitted on the first 60% of the slots, scored on the last 40%).
 1. REVERSION: the change of the residual over the next slot (bp) against z. A negative slope is reversion.
 2. THE CONTRACTS: the outcome of each leg's market minus its YES mid at 7 minutes left (edge, cents, before fees) against a signed z, positive when the residual says
    the YES side is underpriced (Y rich: Y's YES should do worse than priced, X's YES better). A fee is about 1-2 cents.
Analysis only: this places nothing.

    python - [DIR=/data/backfill_crypto] [WINDOW=48] [PAIRS=6] < scripts/backfill_pairs_study.py          (inside the bot container)
"""
import glob
import json
import math
import os
import re

DIR = os.environ.get("DIR", "/data/backfill_crypto")
WINDOW = int(os.environ.get("WINDOW", "48"))
PAIRS = int(os.environ.get("PAIRS", "6"))

price: dict[str, dict[int, float]] = {}
mkt: dict[tuple[str, int], tuple[float | None, str]] = {}  # (name, slot) -> (YES mid at 7 minutes left, result)
for path in sorted(glob.glob(os.path.join(DIR, "*.jsonl"))):
    for line in open(path):
        m = json.loads(line)
        nm = re.match(r"^KX([A-Z]+)15M$", m["series"]).group(1)
        k = int(m["close_ts"]) // 900 - 1
        if not m.get("strike") or float(m["strike"]) <= 0:
            continue
        price.setdefault(nm, {})[k] = math.log(float(m["strike"]))
        t0 = int(m["close_ts"]) - 420
        mid = None
        for c in m["candles"]:
            if c["t"] == t0 and c.get("bc") is not None and c.get("ac") is not None:
                mid = (float(c["bc"]) + float(c["ac"])) / 2.0
        mkt[(nm, k)] = (mid, m["result"])
names = sorted(price)
print(f"{len(names)} coins, {sum(len(v) for v in price.values())} market-slots, {min(min(v) for v in price.values())}..{max(max(v) for v in price.values())} (slot index)")


def returns(n):
    p = price[n]
    return {k: p[k] - p[k - 1] for k in p if k - 1 in p}


def corr(a, b):
    ra, rb = returns(a), returns(b)
    ks = [k for k in ra if k in rb]
    ma, mb = sum(ra[k] for k in ks) / len(ks), sum(rb[k] for k in ks) / len(ks)
    cab = sum((ra[k] - ma) * (rb[k] - mb) for k in ks)
    caa = sum((ra[k] - ma) ** 2 for k in ks)
    cbb = sum((rb[k] - mb) ** 2 for k in ks)
    return cab / math.sqrt(caa * cbb)


cands = sorted(((corr(a, b), a, b) for i, a in enumerate(names) for b in names[i + 1:]), reverse=True)[:PAIRS]


def rolling(y_name, x_name):
    py, px = price[y_name], price[x_name]
    ks = sorted(k for k in py if k in px)
    have = set(ks)
    out = {}
    for k in ks:
        w = [j for j in range(k - WINDOW, k) if j in have]
        if len(w) < WINDOW * 0.9:
            continue
        n = len(w)
        mx = sum(px[j] for j in w) / n
        my = sum(py[j] for j in w) / n
        sxx = sum((px[j] - mx) ** 2 for j in w)
        sxy = sum((px[j] - mx) * (py[j] - my) for j in w)
        syy = sum((py[j] - my) ** 2 for j in w)
        if sxx <= 0:
            continue
        b = sxy / sxx
        a = my - b * mx
        sd = math.sqrt(max(syy - b * sxy, 0.0) / (n - 2))
        if sd <= 0:
            continue
        e = py[k] - a - b * px[k]
        out[k] = (e / sd, e, a, b)
    return out


def slope(pairs):
    n = len(pairs)
    if n < 30:
        return None
    mx = sum(p[0] for p in pairs) / n
    my = sum(p[1] for p in pairs) / n
    sxx = sum((p[0] - mx) ** 2 for p in pairs)
    sxy = sum((p[0] - mx) * (p[1] - my) for p in pairs)
    syy = sum((p[1] - my) ** 2 for p in pairs)
    if sxx <= 0 or syy <= 0:
        return None
    b = sxy / sxx
    se = math.sqrt((syy - b * sxy) / (n - 2) / sxx)
    return b, b / se if se > 0 else 0.0, n, my - b * mx


def fmt(x, w, d=1):
    return f"{'-':>{w}}" if x is None else f"{x:>+{w}.{d}f}"


print(f"{'pair (Y~X)':<12} {'r':>5} | next-slot reversion: {'n':>5} {'slope bp/z':>10} {'t':>5} {'oos R2':>7} {'z>2':>10} {'z<-2':>10} | contracts at -7m: {'n':>5} {'c per z':>8} {'t':>5} {'s>2 edge':>12} {'s<-2 edge':>12}", flush=True)
for r, a, b in cands:
    fit_ = rolling(a, b)
    ks = sorted(fit_)
    obs = []
    for k in ks:
        if k + 1 in price[a] and k + 1 in price[b]:
            _z, e, al, be = fit_[k]
            obs.append((k, fit_[k][0], (price[a][k + 1] - al - be * price[b][k + 1] - e) * 1e4))
    pairs_all = [(z, d) for _k, z, d in obs]
    cut = obs[int(len(obs) * 0.6)][0]
    train = [(z, d) for k, z, d in obs if k < cut]
    test = [(z, d) for k, z, d in obs if k >= cut]
    sl, full = slope(train), slope(pairs_all)
    oos = None
    if sl and len(test) > 20:
        ym = sum(d for _z, d in test) / len(test)
        sst = sum((d - ym) ** 2 for _z, d in test)
        sse = sum((d - (sl[3] + sl[0] * z)) ** 2 for z, d in test)
        oos = (1 - sse / sst) * 100
    hi = [d for z, d in pairs_all if z > 2]
    lo = [d for z, d in pairs_all if z < -2]
    t1 = f"{len(obs):>5} {fmt(full[0], 10, 2)} {fmt(full[1], 5)} {fmt(oos, 6)}% {fmt(sum(hi) / len(hi) if hi else None, 6)}({len(hi):>3}) {fmt(sum(lo) / len(lo) if lo else None, 6)}({len(lo):>3})"
    rows2 = []
    for k in ks:
        if (a, k) not in mkt or (b, k) not in mkt:
            continue
        zz = fit_[k][0]
        for key, sign in (((a, k), -zz), ((b, k), zz)):
            mid, res = mkt[key]
            if mid is not None and 0.05 <= mid <= 0.95:
                rows2.append((sign, ((1.0 if res == "yes" else 0.0) - mid) * 100.0))
    s2 = slope(rows2)
    up = [e for sg, e in rows2 if sg > 2]
    dn = [e for sg, e in rows2 if sg < -2]
    t2 = f"{s2[2]:>5} {fmt(s2[0], 8, 2)} {fmt(s2[1], 5)} {fmt(sum(up) / len(up) if up else None, 6)}({len(up):>3}) {fmt(sum(dn) / len(dn) if dn else None, 6)}({len(dn):>3})" if s2 else "-"
    print(f"{a + '~' + b:<12} {r:>5.2f} | {'':>20}{t1} | {'':>17}{t2}")
