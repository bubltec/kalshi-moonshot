"""Would the curve model (the live crypto_multiscale ridge on the 18 Fibonacci-scale readings plus the ask) pick better commodity bets than the commodity lanes' own rule? (2026-11-08, user: "we need to adopt the commodity lane to the curve model first, then we apply the exits")

Question: on every settled commodity 15-minute window with a BiQuote index since 11-02, at every 30 s from minute 2 to 14.7, both sides, ask 0.20-0.97 and spread 0.06 or less, a ridge fit (curvemodel.fit, the live code) on the EARLIER days only predicts the net per $1 of buying that side at the ask, held to the result, after the fee. Scored on each next day (the first two days only train). Reference for the cushion is the index's own price at the window open (the lane's shock_ref), not Kalshi's strike.
The comparison is on the same moments the favorite lanes can buy: ask 0.76-0.92, minutes 7-14, spread 0.03 or less, first moment per market side (the lanes' own band, not their other gates). "all" buys every such first moment (the lane's rule); "model top Q" buys the first moment where the model's predicted net per $1 is in the top Q share of the training moments (Q 5%, 10%, 20%, the crypto study's shares) and above zero, inside that same band; "model, any band" is the same pick over the whole 0.20-0.92 range, minutes 2-14, as crypto_multiscale buys. Net per $1 after the fee, win rate, two halves, minutes in, curve shapes (the spot's path efficiency in the 5 minutes before the moment, in thirds of all moments), per-bet standard error.
Settings (the bands, Q) come from the lanes and the crypto study, not from these results. Training moments are every third (they overlap heavily).
Not covered: the lanes' other gates and exits, fills, the 60 s settlement average.

    scripts/cloud.sh run scripts/commodity_curve_model_walkforward.py
"""

import bisect
import calendar
import collections
import itertools
import math
import os

from kalshi_moonshot import config, curvemodel, db
from kalshi_moonshot.biquotefeed import index_for_series
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

os.nice(15)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
t0 = calendar.timegm((2026, 11, 2, 13, 0, 0))
SER = ("KXGOLD15M", "KXSILVER15M", "KXPLATINUM15M", "KXPALLADIUM15M", "KXWTI15M", "KXBRENT15M", "KXNATGAS15M", "KXCOPPER15M")
G0 = int(t0 - 3800)
G1 = calendar.timegm((2026, 11, 8, 14, 0, 0))
SQ = "(" + ",".join("?" * len(SER)) + ")"
wins = st.db.execute(f"SELECT ticker,series,close_ts,result FROM windows WHERE close_ts>? AND series IN {SQ} AND result IN ('yes','no') ORDER BY series,close_ts", (t0, *SER)).fetchall()
print("commodity windows", len(wins), flush=True)
rows, meta = [], []  # rows: (18 readings, ask, net); meta: (close, elapsed, ticker, side, bid, spread, eff, day)
for ser in SER:
    ix = index_for_series(ser)
    r = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (ix, G0 - 3600)).fetchall()
    ts, vs = [float(x[0]) for x in r], [float(x[1]) for x in r]
    g, j, last = [], 0, None
    for t in range(G0, G1):
        while j < len(ts) and ts[j] <= t:
            last = vs[j]
            j += 1
        g.append(math.log(last) if last else None)
    sq = [0.0]
    for a, b in itertools.pairwise(g):
        sq.append(sq[-1] + ((b - a) ** 2 if a is not None and b is not None else 0.0))
    print("grid built", ser, len(rows), flush=True)
    for w in (x for x in wins if x["series"] == ser):
        close = float(w["close_ts"])
        opened = close - 900.0
        a = bisect.bisect_left(ts, opened - 5.0)
        if a >= len(ts) or ts[a] > opened + 20.0:
            continue
        ref_log = math.log(vs[a])
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
            seg = [math.exp(x) for x in g[k - 300 : k + 1] if x is not None] if k >= 300 else []
            path = sum(abs(math.log(b / a_)) for a_, b in itertools.pairwise(seg)) if len(seg) > 10 else 0.0
            eff = abs(math.log(seg[-1] / seg[0])) / path if path > 0 else None
            for side, bid, ask in (("yes", yb, ya), ("no", 1 - ya, 1 - yb)):
                if not (0.20 <= ask <= 0.97) or ask - bid > 0.06:
                    continue
                read = curvemodel.features(g, sq, k, ref_log, 1 if side == "yes" else -1)
                if read is None:
                    continue
                won = w["result"] == side
                net = ((1 - ask) / ask if won else -1.0) - fee(ask) / ask
                rows.append((read, ask, net))
                meta.append((close, float(e), w["ticker"], side, bid, ask - bid, eff, int(close // 86400)))
print("moments", len(rows), "market sides", len({(m[2], m[3]) for m in meta}))
days = sorted({m[7] for m in meta})
print("UTC days", days)
by_day = collections.defaultdict(list)
for i, m in enumerate(meta):
    by_day[m[7]].append(i)
effs = sorted(m[6] for m in meta if m[6] is not None)
lo, hi = effs[len(effs) // 3], effs[2 * len(effs) // 3]
shape = lambda e: "unknown" if e is None else "choppy" if e <= lo else "straight" if e >= hi else "middle"
mid = sorted(m[0] for m in meta)[len(meta) // 2]
QS = (0.05, 0.1, 0.2)
sel = {f"model top {int(q * 100)}% (lane band)": [] for q in QS}
sel.update({f"model top {int(q * 100)}% (any band)": [] for q in QS})
for di, d in enumerate(days):
    if di < 2:
        continue
    train = [i for dd in days[:di] for i in by_day[dd]][::3]
    model = curvemodel.fit([(rows[i][0], rows[i][1], rows[i][2]) for i in train])
    if model is None:
        print("day", d, "not fitted")
        continue
    for q in QS:
        cut = model["cuts"][f"{q:g}"]
        for i in by_day[d]:
            p = curvemodel.predict(model, rows[i][0], rows[i][1])
            if p < cut or p <= 0:
                continue
            m = meta[i]
            sel[f"model top {int(q * 100)}% (any band)"].append(i)
            if 0.76 <= rows[i][1] <= 0.92 and m[5] <= 0.03 and 420 <= m[1]:
                sel[f"model top {int(q * 100)}% (lane band)"].append(i)
test_idx = [i for d in days[2:] for i in by_day[d]]
band = lambda i: 0.76 <= rows[i][1] <= 0.92 and meta[i][5] <= 0.03 and 420 <= meta[i][1]
sel["all (the lane's rule, band only)"] = [i for i in test_idx if band(i)]


def first(idx):
    seen = {}
    for i in sorted(idx, key=lambda i: meta[i][1]):
        seen.setdefault((meta[i][2], meta[i][3]), i)
    return list(seen.values())


def show(label, idx):
    if not idx:
        print(f"   {label:46s} n=0")
        return
    n = len(idx)
    net = [rows[i][2] for i in idx]
    mean = sum(net) / n
    se = (sum((x - mean) ** 2 for x in net) / max(n - 1, 1)) ** 0.5 / n**0.5
    a = [rows[i][2] for i in idx if meta[i][0] <= mid]
    b = [rows[i][2] for i in idx if meta[i][0] > mid]
    won = sum(1 for x in net if x > 0)
    print(f"   {label:46s} n={n:5d} net per $1 {mean:+.3f} (se {se:.3f}) won {100 * won / n:4.1f}% | halves {sum(a) / max(len(a), 1):+.3f} (n={len(a)}) / {sum(b) / max(len(b), 1):+.3f} (n={len(b)})")


print("\nfirst moment per market side, test days only:")
for name, idx in sel.items():
    f = first(idx)
    show(name, f)
    for lo_, hi_ in ((120, 420), (420, 660), (660, 900)):
        show(f"   {lo_ // 60}-{hi_ // 60} min in", [i for i in f if lo_ <= meta[i][1] < hi_])
    for sh in ("choppy", "middle", "straight"):
        show(f"   curve shape {sh}", [i for i in f if shape(meta[i][6]) == sh])
print("\nper test day (UTC day; 20763 = Friday 11-06), first moment per market side, lane band for 'all' and 'model (lane band)':")
for d in days[2:]:
    for name, idx in sel.items():
        if "any band" in name:
            continue
        show(f"day {d} {name}", first([i for i in idx if meta[i][7] == d]))
print("DONE")
