"""Pairs trading on the underlyings: does the OLS residual of one asset on its partner predict anything about the 15-minute contracts? Replayed from the records.

The underlying's price comes from the windows table: each 15-minute market's strike is the reference price at its open, so a series of strikes is a series of
the underlying every 15 minutes since the bot began recording (the tape's own feed prices only go back about a day, too little for a fit). For each of the
PAIRS most correlated pairs of one family (correlation of 15-minute log returns), Y on X is fitted by OLS on the previous WINDOW slots,

    log Y = a + b log X + e,        z = (the current e) / (the window's residual standard deviation)

so z > 0 means Y is rich against X at this slot's open. The fit at each slot uses only what came before it, and the coefficients are fitted on the first 60% of
the slots and judged on the last 40%. Two tests:

 1. REVERSION. The change of the residual over the next slot, in basis points, against z. A negative slope is reversion; "oos R2" is how much of the later
    changes the earlier slope explains.
 2. THE CONTRACTS. For each settled slot, 7 minutes before the close (where the lane buys): the outcome of each leg's market minus its YES mid ("edge", cents per
    contract, before fees), against a signed z that is positive when the residual says the YES side is underpriced (Y rich: Y's YES should do worse than priced,
    X's YES better). A real signal needs an edge of several cents at large |z|; a fee is about 1-2 cents.

Analysis only: this places nothing.

    scripts/cloud.sh run scripts/asset_pairs_study.py FAMILY=metals|coins|fx [DAYS=5] [WINDOW=48] [PAIRS=4]
"""
import math
import os
import re
import time

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)

FAMILY = os.environ.get("FAMILY", "metals")
DAYS = float(os.environ.get("DAYS", "5"))
WINDOW = int(os.environ.get("WINDOW", "48"))
PAIRS = int(os.environ.get("PAIRS", "4"))
METALS = ("GOLD", "SILVER", "PLATINUM", "PALLADIUM", "COPPER")
COINS = ("BTC", "ETH", "SOL", "XRP", "DOGE")
ENERGY = {"WTI", "BRENT", "NATGAS"}


def name_of(series):
    m = re.match(r"^KX([A-Z]+)15M$", series)
    return m.group(1) if m else None


since = time.time() - DAYS * 86400
wrows = main.db.execute("SELECT ticker, series, close_ts, strike, result FROM windows WHERE result IN ('yes','no') AND close_ts >= ? AND strike > 0 ORDER BY close_ts", (since,)).fetchall()
allnames = sorted({name_of(w["series"]) for w in wrows if name_of(w["series"])})
if FAMILY == "metals":
    names = [n for n in allnames if n in METALS]
elif FAMILY == "coins":
    names = [n for n in allnames if n in COINS]
else:
    names = [n for n in allnames if len(n) == 6 and n not in METALS + COINS and n not in ENERGY]

price: dict[str, dict[int, float]] = {n: {} for n in names}  # slot index (the open time // 900) -> log of the strike
slot: dict[tuple[str, int], tuple[str, str]] = {}  # (name, slot) -> ticker, result
for w in wrows:
    nm = name_of(w["series"])
    if nm in price:
        k = int(w["close_ts"]) // 900 - 1
        price[nm][k] = math.log(float(w["strike"]))
        slot[(nm, k)] = (w["ticker"], w["result"])
names = [n for n in names if len(price[n]) > WINDOW + 60]
if len(names) < 2:
    print(f"{FAMILY}: fewer than two underlyings with enough slots ({names})")
    raise SystemExit


def returns(n):
    p = price[n]
    return {k: p[k] - p[k - 1] for k in p if k - 1 in p}


def corr(a, b):
    ra, rb = returns(a), returns(b)
    ks = [k for k in ra if k in rb]
    if len(ks) < 100:
        return 0.0
    ma, mb = sum(ra[k] for k in ks) / len(ks), sum(rb[k] for k in ks) / len(ks)
    cab = sum((ra[k] - ma) * (rb[k] - mb) for k in ks)
    caa = sum((ra[k] - ma) ** 2 for k in ks)
    cbb = sum((rb[k] - mb) ** 2 for k in ks)
    return cab / math.sqrt(caa * cbb) if caa > 0 and cbb > 0 else 0.0


cands = sorted(((corr(a, b), a, b) for i, a in enumerate(names) for b in names[i + 1:]), reverse=True)[:PAIRS]


def rolling(y_name, x_name):
    """{slot: (z, e, a, b)}, each from the WINDOW slots before it only."""
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
    """(slope, t, n, intercept) of y on x for (x, y) pairs."""
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


def yes_mid(ticker, t0):
    got = tape.execute("SELECT bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 AND ts BETWEEN ? AND ? ORDER BY ts DESC LIMIT 1", (ticker, t0 - 10, t0)).fetchone()
    return None if not got else (float(got[0]) + float(got[1])) / 2.0


def fmt(x, w, d=1):
    return f"{'-':>{w}}" if x is None else f"{x:>+{w}.{d}f}"


print(f"{FAMILY}: {len(names)} underlyings, {WINDOW}-slot rolling OLS on 15-minute reference prices, last {DAYS:g} days; top pairs by return correlation")
print(f"{'pair (Y~X)':<16} {'r':>5} | next-slot reversion: {'n':>4} {'slope bp/z':>10} {'t':>5} {'oos R2':>7} {'z>2':>9} {'z<-2':>9} | contracts at -7m: {'n':>4} {'c per z':>8} {'t':>5} {'s>2 edge':>11} {'s<-2 edge':>11}", flush=True)
for r, a, b in cands:
    fit_ = rolling(a, b)
    ks = sorted(fit_)
    obs = []  # (slot, z, change of the residual over the next slot, bp)
    for k in ks:
        if k + 1 in price[a] and k + 1 in price[b]:
            _z, e, al, be = fit_[k]
            obs.append((k, fit_[k][0], (price[a][k + 1] - al - be * price[b][k + 1] - e) * 1e4))
    pairs_all = [(z, d) for _k, z, d in obs]
    cut = obs[int(len(obs) * 0.6)][0] if obs else 0
    train = [(z, d) for k, z, d in obs if k < cut]
    test = [(z, d) for k, z, d in obs if k >= cut]
    sl, full = slope(train), slope(pairs_all)
    oos = hi_m = lo_m = None
    hi = [d for z, d in pairs_all if z > 2]
    lo = [d for z, d in pairs_all if z < -2]
    if sl and len(test) > 20:
        ymean = sum(d for _z, d in test) / len(test)
        sst = sum((d - ymean) ** 2 for _z, d in test)
        sse = sum((d - (sl[3] + sl[0] * z)) ** 2 for z, d in test)
        oos = (1 - sse / sst) * 100
    hi_m = sum(hi) / len(hi) if hi else None
    lo_m = sum(lo) / len(lo) if lo else None
    t1 = (f"{len(obs):>4} {fmt(full[0] if full else None, 10, 2)} {fmt(full[1] if full else None, 5)} {fmt(oos, 6)}% "
          f"{fmt(hi_m, 6)}({len(hi):>2}) {fmt(lo_m, 6)}({len(lo):>2})")
    rows2 = []
    for (nm, k), (ticker, result) in slot.items():
        if nm != a or k not in fit_ or (b, k) not in slot:
            continue
        zz = fit_[k][0]
        close = (k + 1) * 900
        for tk, res, sign in ((ticker, result, -zz), (slot[(b, k)][0], slot[(b, k)][1], zz)):
            mid = yes_mid(tk, close - 420)
            if mid is not None and 0.05 <= mid <= 0.95:
                rows2.append((sign, ((1.0 if res == "yes" else 0.0) - mid) * 100.0))
    sl2 = slope(rows2)
    if sl2:
        up = [e for sgn, e in rows2 if sgn > 2]
        dn = [e for sgn, e in rows2 if sgn < -2]
        t2 = (f"{sl2[2]:>4} {fmt(sl2[0], 8, 2)} {fmt(sl2[1], 5)} {fmt(sum(up) / len(up) if up else None, 6)}({len(up):>2}) {fmt(sum(dn) / len(dn) if dn else None, 6)}({len(dn):>2})")
    else:
        t2 = f"{'-':>4}"
    print(f"{a + '~' + b:<16} {r:>5.2f} | {'':>20}{t1} | {'':>17}{t2}")
