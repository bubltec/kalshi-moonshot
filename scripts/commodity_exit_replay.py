"""The value exit and the strike-cross exit replayed on the live commodity bets (2026-11-08, user: "commodity favorite lanes have been my winners ... i do think all this new curve based logic is what we will want to adopt").

Question: on the live commodity bets (every commodity lane, mode trade, one per market side), would selling by the value exit (the bid after the fee is over a curve chance that the side pays by a margin, bid 0.30-0.70, never a loss after both fees) or the strike-cross exit (in the last 300 s the spot is on the wrong side of the window-open price, bid 0.05-0.89) have beaten what actually happened (the lane's own exit, else held to the result)? A replayed sale only counts if it comes before the lane's own sale.
Reference: the index's own price at the window open (the lane's shock_ref for BiQuote commodities), as the live exits read it, not Kalshi's strike.
Chance: a logistic on [1, z, 13 m slope, 55 m slope, logit(bid)] (z = cushion over what the seconds left can move at the 13 minute one-second rate), fit on every commodity window moment (both sides, every 30 s, minute 2 to 14.7, bid 0.20-0.97, spread 0.06 or less) of the EARLIER days only, so a bet's day is never in its own fit. The first two days only train. This is a curve-only model fit on commodities, not confidence.py's model (which is crypto-trained and cannot be run here), so it says whether the idea holds on commodities, not whether the live model does.
Sale: bid - 0.01, fees on both legs (fee at the price paid and at the sale). Cost at the decision row's ask, contracts = stake / ask. Margins 0, 0.02, 0.05 are a grid; 0.02 and the bid band are the crypto values, not chosen here.
Reported as in the crypto replays: n, change in dollars against what happened, sold, beat/lost, two halves, curve shapes (path efficiency of the spot in the 5 minutes before the buy, in thirds). Per-bet standard error is given.
Not covered: fills at the replayed price, a sale queue, the lane's resting orders, the 60 s settlement average.

    scripts/cloud.sh run scripts/commodity_exit_replay.py
"""

import bisect
import calendar
import collections
import itertools
import math
import os

from kalshi_moonshot import config, db
from kalshi_moonshot.biquotefeed import index_for_series
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

os.nice(15)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
t0 = calendar.timegm((2026, 11, 2, 13, 0, 0))
SER = ("KXGOLD15M", "KXSILVER15M", "KXPLATINUM15M", "KXPALLADIUM15M", "KXWTI15M", "KXBRENT15M", "KXNATGAS15M", "KXCOPPER15M")
LOOK13, LOOK55 = 13 * 60, 55 * 60
G0 = int(t0 - 4300)
G1 = int(calendar.timegm((2026, 11, 8, 14, 0, 0)))
SQ = "(" + ",".join("?" * len(SER)) + ")"
wins = st.db.execute(f"SELECT ticker,series,close_ts,result FROM windows WHERE close_ts>? AND series IN {SQ} AND result IN ('yes','no') ORDER BY series,close_ts", (t0, *SER)).fetchall()
print("commodity windows", len(wins))


def build_grid(ix):
    r = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (ix, G0 - 3600)).fetchall()
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
    return g, sq, ts, vs


def feat(g, sq, k, ref_log, sg, left, bid):
    """[1, z, s13, s55, logit(bid)] at grid index k, or None."""
    if k - LOOK55 < 0 or k >= len(g) or g[k] is None or g[k - LOOK13] is None or g[k - LOOK55] is None or left <= 1:
        return None
    r13 = math.sqrt((sq[k] - sq[k - LOOK13]) / LOOK13)
    r55 = math.sqrt((sq[k] - sq[k - LOOK55]) / LOOK55)
    if r13 <= 0 or r55 <= 0 or not 0 < bid < 1:
        return None
    z = (g[k] - ref_log) * sg / (r13 * math.sqrt(left))
    s13 = (g[k] - g[k - LOOK13]) * sg / (r13 * math.sqrt(LOOK13))
    s55 = (g[k] - g[k - LOOK55]) * sg / (r55 * math.sqrt(LOOK55))
    c = lambda v: max(-6.0, min(6.0, v))
    return [1.0, c(z), c(s13), c(s55), math.log(bid / (1 - bid))], r13


GR = {}
REF = {}
moments = []  # (day, x, won)
for ser in SER:
    ix = index_for_series(ser)
    if not ix:
        continue
    GR[ser] = build_grid(ix)
    g, sq, ts, vs = GR[ser]
    for w in (x for x in wins if x["series"] == ser):
        close = float(w["close_ts"])
        opened = close - 900.0
        a = bisect.bisect_left(ts, opened - 5.0)
        if a >= len(ts) or ts[a] > opened + 20.0:
            continue
        ref = vs[a]
        REF[w["ticker"]] = math.log(ref)
        q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (w["ticker"], opened - 20, close)).fetchall()
        if len(q) < 20:
            continue
        qt = [float(x[0]) for x in q]
        for e in range(120, 881, 30):
            at = opened + e
            i = bisect.bisect_right(qt, at) - 1
            if i < 0 or at - qt[i] > 20:
                continue
            yb, ya = float(q[i][1]), float(q[i][2])
            for side, bid, ask in (("yes", yb, ya), ("no", 1 - ya, 1 - yb)):
                if not (0.20 <= bid <= 0.97) or ask - bid > 0.06:
                    continue
                f = feat(g, sq, int(at) - G0, REF[w["ticker"]], 1 if side == "yes" else -1, close - at, bid)
                if f is None:
                    continue
                moments.append((int(close // 86400), f[0], int(w["result"] == side)))
days = sorted({m[0] for m in moments})
print("moments", len(moments), "UTC days", days)


def fit(rs, iters=8, lam=1.0):
    n = len(rs[0][1])
    w = [0.0] * n
    for _ in range(iters):
        grad = [0.0] * n
        hess = [[lam if i == j else 0.0 for j in range(n)] for i in range(n)]
        for _, x, y in rs:
            p = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, sum(a * b for a, b in zip(w, x, strict=False))))))
            d, wt = y - p, p * (1 - p)
            for i in range(n):
                grad[i] += d * x[i]
                for j in range(n):
                    hess[i][j] += wt * x[i] * x[j]
        for i in range(n):
            grad[i] -= lam * w[i]
        a = [row[:] + [grad[i]] for i, row in enumerate(hess)]
        for c in range(n):
            piv = max(range(c, n), key=lambda r_: abs(a[r_][c]))
            a[c], a[piv] = a[piv], a[c]
            for r_ in range(n):
                if r_ != c:
                    f = a[r_][c] / a[c][c]
                    for cc in range(c, n + 1):
                        a[r_][cc] -= f * a[c][cc]
        w = [w[i] + a[i][n] / a[i][i] for i in range(n)]
    return w


W = {}
for di, d in enumerate(days):
    if di >= 2:
        W[d] = fit([m for m in moments if m[0] < d])
print("days with a walk-forward model:", len(W), "of", len(days))

LANES = ("commodity", "commodity_energy", "commodity_industrial", "commodity_precious", "commodity_multiscale", "commodity_wti", "commodity_natgas")
rows = st.db.execute(
    f"SELECT ts,ticker,lane,side,ask,stake_usd,outcome,exit_price,exit_ts FROM decisions WHERE action='bet' AND mode='trade' AND stake_usd>0 AND ask>0 AND ts>? AND lane IN ({','.join('?' * len(LANES))}) ORDER BY ts",
    (t0, *LANES),
).fetchall()
seen = {}
for r in rows:
    seen.setdefault((r["ticker"], r["side"]), r)
print("live commodity bets", len(seen))


def actual(r):
    e, c = r["ask"], r["stake_usd"] / r["ask"]
    if r["exit_price"]:
        x = float(r["exit_price"])
        return (x - e) * c - (fee(e) + fee(x)) * c
    return ((1 - e) * c if r["outcome"] == r["side"] else -e * c) - fee(e) * c


def sale(r, bid):
    e, c = r["ask"], r["stake_usd"] / r["ask"]
    sp = max(bid - 0.01, 0.01)
    return (sp - e) * c - (fee(e) + fee(sp)) * c


data = []
for r in seen.values():
    ser = r["ticker"].split("-")[0]
    if ser not in GR or r["outcome"] not in ("yes", "no") or r["ticker"] not in REF:
        continue
    wd = W.get(int(float(r["ts"]) // 86400))
    if wd is None:
        continue
    g, sq, ts, vs = GR[ser]
    w = st.db.execute("SELECT close_ts FROM windows WHERE ticker=?", (r["ticker"],)).fetchone()
    if not w:
        continue
    close = float(w["close_ts"])
    sg = 1 if r["side"] == "yes" else -1
    ref_log = REF[r["ticker"]]
    k0 = int(r["ts"]) - G0
    seg = [math.exp(x) for x in g[k0 - 300 : k0 + 1] if x is not None]
    path = sum(abs(math.log(b / a)) for a, b in itertools.pairwise(seg))
    eff = abs(math.log(seg[-1] / seg[0])) / path if len(seg) > 10 and path > 0 else None
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts>=? AND ts<=? AND bid>0 AND ask>0 ORDER BY ts", (r["ticker"], r["ts"], close)).fetchall()
    own = float(r["exit_ts"]) if r["exit_price"] and r["exit_ts"] else 1e18
    reads = []  # (time, left, bid, p, margin_bp)
    for x in q:
        now = float(x["ts"])
        left = close - now
        if left <= 3 or now >= own:
            continue
        bid = float(x["bid"]) if r["side"] == "yes" else 1.0 - float(x["ask"])
        f = feat(g, sq, int(now) - G0, ref_log, sg, left, bid)
        if f is None:
            continue
        p = 1.0 / (1.0 + math.exp(-sum(a * b for a, b in zip(wd, f[0], strict=False))))
        reads.append((now, left, bid, p, (g[int(now) - G0] - ref_log) * sg * 1e4))
    data.append((r, eff, reads, actual(r)))
print("bets replayed", len(data), "what happened: net $%.2f" % sum(a for *_, a in data))
effs = sorted(e for _, e, _, _ in data if e is not None)
lo, hi = effs[len(effs) // 3], effs[2 * len(effs) // 3]
mid = sorted(r["ts"] for r, *_ in data)[len(data) // 2]
shape = lambda e: "unknown" if e is None else "choppy" if e <= lo else "straight" if e >= hi else "middle"


def fire_value(r, reads, margin):
    e = r["ask"]
    for now, left, bid, p, _ in reads:
        if 0.30 <= bid <= 0.70 and bid - fee(bid) - p >= margin and bid - fee(bid) > e + fee(e):
            return now, bid
    return None


def fire_cross(r, reads):
    for now, left, bid, p, mg in reads:
        if left <= 300 and mg <= 0.0 and 0.05 <= bid <= 0.89:
            return now, bid
    return None


def run(label, fire):
    tot = [0, 0.0, 0, 0, 0.0]
    parts = {"first half": [0.0, 0, 0], "second half": [0.0, 0, 0]}
    shp = {}
    deltas = []
    for r, e, reads, a in data:
        f = fire(r, reads)
        p = sale(r, f[1]) if f else a
        d = p - a
        deltas.append(d)
        tot[0] += 1
        tot[1] += d
        tot[2] += bool(f)
        tot[3] += d > 1e-9
        tot[4] += d < -1e-9
        for key, tab in (("first half" if r["ts"] <= mid else "second half", parts), (shape(e), shp)):
            c = tab.setdefault(key, [0.0, 0, 0])
            c[0] += d
            c[1] += d > 1e-9
            c[2] += d < -1e-9
    n = len(deltas)
    mean = sum(deltas) / n
    se = (sum((x - mean) ** 2 for x in deltas) / max(n - 1, 1)) ** 0.5 / n**0.5
    ph = " | ".join(f"{k} {v[0]:+.2f} (beat {v[1]}, lost {v[2]})" for k, v in parts.items())
    ps = " | ".join(f"{k} {v[0]:+.2f} (beat {v[1]}, lost {v[2]})" for k, v in sorted(shp.items()))
    print(f"{label:34s} bets {n} sold {tot[2]:3d} change {tot[1]:+7.2f} (per bet {mean:+.3f}, se {se:.3f}) beat {tot[3]} lost {tot[4]}")
    print(f"      halves: {ph}")
    print(f"      shapes: {ps}")


print("path-efficiency cutoffs:", round(lo, 3), round(hi, 3), "| halves split at", mid)
for m in (0.0, 0.02, 0.05):
    run(f"value exit, margin {m:.2f}", lambda r, reads, m=m: fire_value(r, reads, m))
run("strike-cross exit, last 300 s", fire_cross)


def both(r, reads):
    a, b = fire_value(r, reads, 0.02), fire_cross(r, reads)
    return min((x for x in (a, b) if x), default=None, key=lambda x: x[0])


run("value 0.02 or strike-cross (first)", both)
print("DONE")
