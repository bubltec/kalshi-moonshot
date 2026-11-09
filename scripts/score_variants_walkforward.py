"""Does any better score pick bets that pay after the fee? (2026-11-09, user: "a lower price indicates a lower probability of a win, but what we CAN calculate is whether a spot is at a dip or a peak, and what probability, based on the history, that it will move the opposite direction"; "there's additional variables we're not thinking about that should impact the score".)
Question: on the same moments as the live model lanes (every settled window of the model_buy lanes' series since 11-02, both sides, every 30 s inside the entry window, price and spread bands), fit each variant on the EARLIER days only and score each next day (the first three days only train): buy the first moment per market side whose predicted net per $1 is at or over the training top-15% cut and above zero, held to the result, net per $1 after the fee. Compared on the same picks rule, with the live model's readings as the base:
  feature sets   base: the curvature readings + the ask (the live model)
                 +dist: the distance of the ask from 0.50 and the ask squared (the ask is only a straight line in base)
                 +dip: where the spot sits in its recent range at the 5, 13 and 34 minute windows (position 0-1, time since the high and since the low as a share of the window, the window's range as a share of the 34 minute range), side-adjusted: a dip or a peak
                 +market: the contract's own market (the ask's move over 30 and 90 s, spread, quote age, quote updates in 60 s, and the taker imbalance and volume over 60 s from flow_1s)
                 all: all of them
  targets        net: ridge on the net per $1 (the live model)
                 edge: ridge on (won - ask), the win chance over the price paid; the pick ranks (edge - fee) / ask, so the 1/ask swing of the net does not drive the choice
Reported per variant: picks, net per $1, standard error, win rate, two halves of the test days; then, for the three best and the base, by the price paid and by family (labels for this report only: crypto, energy, metals, copper).
Settings: the live lanes' (top 15%, bands, curvature windows) and the grid above, fixed before the results. Training moments every second row (they overlap heavily).
Not covered: the lanes' other gates, exits, fills.
    scripts/cloud_job.sh scripts/score_variants_walkforward.py
"""
import bisect
import collections
import math
import multiprocessing
import os
import time

from kalshi_moonshot import config, curvemodel, db, variants
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
curvemodel.TRAIN_DAYS = (time.time() - 1_793_000_000) / 86400.0 + 0.5
meta = []
rows = curvemodel.build_rows(s, meta=meta, kind="curvature")
N = len(curvemodel.KINDS["curvature"])
print("rows", len(rows), flush=True)
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)

# ---- the extra readings, per row, from the tape ------------------------------------------------------------------
by_ticker = collections.defaultdict(list)
for i, m in enumerate(meta):
    by_ticker[m[2]].append(i)
tick_cache = {}


def ticks_of(ix, lo, hi):
    got = tick_cache.get(ix)
    if got is None or got[2] > lo or got[3] < hi:
        r = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND ts<=? AND value>0 ORDER BY ts", (ix, lo, hi)).fetchall()
        got = ([float(x[0]) for x in r], [float(x[1]) for x in r], lo, hi)
        tick_cache[ix] = got
    return got[0], got[1]


def path(ts, vs, at, window_s, sign, ref):
    out = []
    for j in range(25):
        t = at - window_s + j * window_s / 24.0
        i = bisect.bisect_right(ts, t) - 1
        if i < 0:
            return None
        out.append(math.log(vs[i] / ref) * sign)
    return out


def dip_features(p):
    lo, hi = min(p), max(p)
    span = hi - lo
    if span <= 0:
        return None
    n = len(p) - 1
    return [(p[-1] - lo) / span, (n - p.index(hi)) / n, (n - p.index(lo)) / n, span]


EXTRA = {}  # row -> (dist, dip, market)
t0 = time.time()
for ticker, idxs in sorted(by_ticker.items()):
    series = ticker.split("-")[0]
    ix = variants.crypto_index(series) or variants.biquote_index(series)
    close = meta[idxs[0]][0]
    opened = close - 900.0
    ts, vs = ticks_of(ix, opened - 2400.0 - 300.0, close)
    if not ts:
        continue
    if variants.crypto_index(series):
        w = st.db.execute("SELECT strike FROM windows WHERE ticker=?", (ticker,)).fetchone()
        ref = float(w["strike"]) if w and w["strike"] else 0.0
    else:
        a = bisect.bisect_left(ts, opened - 5.0)
        ref = vs[a] if a < len(ts) and ts[a] <= opened + 20.0 else 0.0
    if ref <= 0:
        continue
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (ticker, opened - 120, close)).fetchall()
    qt = [float(x[0]) for x in q]
    f = tape.execute("SELECT ts,yes_vol,no_vol FROM flow_1s WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (ticker, opened - 120, close)).fetchall()
    ft = [float(x[0]) for x in f]
    for i in idxs:
        at = opened + meta[i][1]
        sign = 1 if meta[i][3] == "yes" else -1
        ask = rows[i][1]
        feats = []
        for W in (300.0, 780.0, 2040.0):
            p = path(ts, vs, at, W, sign, ref)
            d = dip_features(p) if p else None
            if d is None:
                feats = None
                break
            feats.append(d)
        if feats is None:
            continue
        r34 = feats[2][3]
        dip = []
        for d in feats:
            dip += [d[0], d[1], d[2], d[3] / r34]
        k = bisect.bisect_right(qt, at) - 1
        if k < 0:
            continue

        def held_ask(j):
            return float(q[j][2]) if sign == 1 else 1.0 - float(q[j][1])

        j30 = bisect.bisect_right(qt, at - 30.0) - 1
        j90 = bisect.bisect_right(qt, at - 90.0) - 1
        if j30 < 0 or j90 < 0:
            continue
        spread = float(q[k][2]) - float(q[k][1])
        upd = k - (bisect.bisect_right(qt, at - 60.0) - 1)
        lo_f, hi_f = bisect.bisect_right(ft, at - 60.0), bisect.bisect_right(ft, at)
        yv = sum(float(x[1]) for x in f[lo_f:hi_f])
        nv = sum(float(x[2]) for x in f[lo_f:hi_f])
        tot = yv + nv
        market = [held_ask(k) - held_ask(j30), held_ask(k) - held_ask(j90), spread, at - qt[k], float(upd), (yv - nv) / tot * sign if tot > 0 else 0.0, math.log1p(tot)]
        EXTRA[i] = ([abs(ask - 0.5), (ask - 0.5) ** 2], dip, market)
print("extra readings for", len(EXTRA), "rows in", round(time.time() - t0), "s", flush=True)

idx_all = sorted(EXTRA)
SETS = {"base": lambda i: [], "+dist": lambda i: EXTRA[i][0], "+dip": lambda i: EXTRA[i][1], "+market": lambda i: EXTRA[i][2], "all": lambda i: EXTRA[i][0] + EXTRA[i][1] + EXTRA[i][2]}
day = {i: int(meta[i][0] // 86400) for i in idx_all}
days = sorted(set(day.values()))
by_day = collections.defaultdict(list)
for i in idx_all:
    by_day[day[i]].append(i)
RIDGE = curvemodel.RIDGE


def xrow(i, fs):
    return [*rows[i][0][:N], rows[i][1], *SETS[fs](i)]


def run(combo):
    fs, target = combo
    picked = []
    for di, d in enumerate(days):
        if di < 3:
            continue
        train = [i for dd in days[:di] for i in by_day[dd]][::2]
        if len(train) < 3000:
            continue
        xs = [xrow(i, fs) for i in train]
        n, k = len(xs), len(xs[0])
        mu = [sum(x[j] for x in xs) / n for j in range(k)]
        sd = [(sum((x[j] - mu[j]) ** 2 for x in xs) / n) ** 0.5 or 1.0 for j in range(k)]
        z = [[1.0] + [(x[j] - mu[j]) / sd[j] for j in range(k)] for x in xs]
        ys = [rows[i][2] for i in train] if target == "net" else [(1.0 if rows[i][2] > -1.0 + 1e-9 and rows[i][2] * rows[i][1] > -1e9 and _won(i) else 0.0) - rows[i][1] for i in train]
        w = curvemodel._ridge(z, ys, RIDGE)

        def score(i, mu=mu, sd=sd, w=w, fs=fs, target=target):
            x = xrow(i, fs)
            p = w[0] + sum(w[j + 1] * (x[j] - mu[j]) / sd[j] for j in range(len(x)))
            return p if target == "net" else (p - fee(rows[i][1])) / rows[i][1]

        tr = sorted(score(i) for i in train)
        cut = tr[int((1.0 - 0.15) * (len(tr) - 1))]
        picked += [i for i in by_day[d] if (sc := score(i)) >= cut and sc > 0]
    return combo, picked


def _won(i):
    ask = rows[i][1]
    return rows[i][2] > 0 or (rows[i][2] > -fee(ask) / ask - 1.0 + 1e-9 and rows[i][2] > -0.9999)  # a win has a net over -1 (a loss is exactly -1 - fee/ask)


mid = sorted(meta[i][0] for i in idx_all)[len(idx_all) // 2]


def first(idx):
    seen = {}
    for i in sorted(idx, key=lambda i: meta[i][1]):
        seen.setdefault((meta[i][2], meta[i][3]), i)
    return list(seen.values())


def stats(idx):
    if not idx:
        return None
    n = len(idx)
    net = [rows[i][2] for i in idx]
    mean = sum(net) / n
    se = (sum((x - mean) ** 2 for x in net) / max(n - 1, 1)) ** 0.5 / n**0.5
    a = [rows[i][2] for i in idx if meta[i][0] <= mid]
    b = [rows[i][2] for i in idx if meta[i][0] > mid]
    won = sum(1 for i in idx if rows[i][2] > -fee(rows[i][1]) / rows[i][1] - 1.0 + 1e-9)
    return n, mean, se, 100 * won / n, sum(a) / max(len(a), 1), len(a), sum(b) / max(len(b), 1), len(b)


COMBOS = [(fs, t) for t in ("net", "edge") for fs in SETS]
print("fitting", len(COMBOS), "variants on", os.cpu_count(), "cores", flush=True)
t0 = time.time()
with multiprocessing.get_context("fork").Pool(min(4, os.cpu_count() or 1)) as pool:
    results = dict(pool.map(run, COMBOS))
print("fitted in", round(time.time() - t0), "s", flush=True)

print("\nfirst moment per market side, test days only (net per $1 after the fee, held to the result):")
print(f"   {'variant':16s} {'picks':>6s} {'net/$1':>8s} {'se':>6s} {'won%':>6s}   halves (n)")
ranked = []
for combo in COMBOS:
    f = first(results[combo])
    st_ = stats(f)
    if st_ is None:
        print(f"   {combo[0]:9s} {combo[1]:6s} no picks")
        continue
    ranked.append((st_[1], combo))
    print(f"   {combo[0]:9s} {combo[1]:6s} {st_[0]:6d} {st_[1]:+8.3f} {st_[2]:6.3f} {st_[3]:6.1f}   {st_[4]:+.3f} ({st_[5]}) / {st_[6]:+.3f} ({st_[7]})")
ENERGY, METALS = ("KXWTI15M", "KXBRENT15M", "KXNATGAS15M"), ("KXGOLD15M", "KXSILVER15M", "KXPLATINUM15M", "KXPALLADIUM15M")


def family(ticker):
    ser = ticker.split("-")[0]
    return "energy" if ser in ENERGY else "metals" if ser in METALS else "copper" if ser == "KXCOPPER15M" else "crypto"


show = [("base", "net")] + [c for _, c in sorted(ranked, reverse=True)[:3] if c != ("base", "net")]
for combo in show:
    f = first(results[combo])
    print(f"\n{combo[0]} / {combo[1]}: by the price paid and by family")
    for lo, hi in ((0.35, 0.45), (0.45, 0.55), (0.55, 0.65), (0.65, 0.751)):
        sub = [i for i in f if lo <= rows[i][1] < hi]
        r = stats(sub)
        print(f"   ask {lo:.2f}-{min(hi, 0.75):.2f}  " + (f"n={r[0]:5d} net/$1 {r[1]:+.3f} won {r[3]:4.1f}% halves {r[4]:+.3f} / {r[6]:+.3f}" if r else "n=0"))
    for fam in ("crypto", "energy", "metals", "copper"):
        sub = [i for i in f if family(meta[i][2]) == fam]
        r = stats(sub)
        print(f"   {fam:10s}    " + (f"n={r[0]:5d} net/$1 {r[1]:+.3f} won {r[3]:4.1f}% halves {r[4]:+.3f} / {r[6]:+.3f}" if r else "n=0"))
print("DONE")
