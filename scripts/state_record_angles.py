"""State record with the slope as an angle and choppiness as the variability of the spot's distance to the strike, not a crossing count.

Question (2026-11-07, user): "slope of 0.5 makes no sense; it could be between 30 and 70 degrees depending on the direction", "0.8 crossings doesn't make it choppy ... choppiness is just the variability of the spot's distance to the strike over a time period". The record states so far read the letters A-G (variants.scale_letter): a slope bar of 0.5 typical moves and a crossing count for the choppy letter G.
Here a state is built from: the direction of the cushion over the scale (the cushion is the spot's distance from the strike on the bet's side), read as an ANGLE: tan(angle) = the change in the cushion over the look-back in that look-back's typical move (the same slope number as before, so 0.5 is 26.6 degrees and 1.0 is 45); rising when the angle is at least UP degrees, falling when it is at most -DOWN degrees, flat between; and choppiness: the standard deviation of the cushion over the look-back (in the look-back's typical move), "chop" when it is above the 67th percentile of all readings of that scale (the market's own history would replace the pooled one in the lane), the straight-line-detrended version as a second reading. Crypto only (the five coins with windows), minute 2 to 14, ask 0.20-0.70, spread <= 0.04.
Walk-forward as scripts/state_record_walkforward.py: the record of a state and price band is every earlier day (one reading per market side, state and band, net of the fee, held to the result, shrunk by K = 10, at least 15 markets); each later day buys the moments whose record clears the bar. Grid: UP and DOWN in 30, 45, 60 and 70 degrees; five state shapes. Two halves of the test days are shown. The grid is a grid, not a choice after the fact; the best rows are in-sample on three test days.

    scripts/hostrun3.sh-style: docker compose exec -T bot python - < scripts/state_record_angles.py
"""

import bisect
import calendar
import collections
import itertools
import math
import os

from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

os.nice(15)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
t0 = calendar.timegm((2026, 11, 2, 6, 0, 0))
CRYPTO = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M")
wins = st.db.execute("SELECT ticker,series,close_ts,strike,result FROM windows WHERE close_ts>? AND series IN (?,?,?,?,?) AND result IN ('yes','no') AND strike>0 ORDER BY series,close_ts", (t0, *CRYPTO)).fetchall()
G0 = int(t0 - 4300)
G1 = int(max(float(w["close_ts"]) for w in wins)) + 2
LOOK = {"5m": 300, "60m": 3600}
STRIDE = {"5m": 5, "60m": 30}
rows = []


def grid(ix):
    r = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (ix, G0 - 60)).fetchall()
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
    return g, sq


def std_and_resid(xs):
    n = len(xs)
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / n
    tm = (n - 1) / 2.0
    stt = sum((i - tm) ** 2 for i in range(n))
    cov = sum((i - tm) * (x - m) for i, x in enumerate(xs))
    res = max(0.0, var - (cov * cov / stt) / n)
    return math.sqrt(var), math.sqrt(res)


for ser in CRYPTO:
    g, sq = grid(variants.crypto_index(ser))
    for w in (x for x in wins if x["series"] == ser):
        close, K = float(w["close_ts"]), math.log(float(w["strike"]))
        opened = close - 900.0
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
            if k - 3600 < 0 or g[k] is None:
                continue
            left = close - at
            for side, bid, ask in (("yes", yb, ya), ("no", 1 - ya, 1 - yb)):
                if not (0.20 <= ask <= 0.70) or ask - bid > 0.04:
                    continue
                sg = 1 if side == "yes" else -1
                feat = {}
                ok = True
                for name, L in LOOK.items():
                    rate = math.sqrt((sq[k] - sq[k - L]) / L)
                    if rate <= 0 or g[k - L] is None:
                        ok = False
                        break
                    unit = rate * math.sqrt(L)
                    c = (g[k] - K) * sg
                    seg = [(g[j] - K) * sg for j in range(k - L, k + 1, STRIDE[name]) if g[j] is not None]
                    if len(seg) < 10:
                        ok = False
                        break
                    sd, rs = std_and_resid(seg)
                    feat[name] = {"z": c / (rate * math.sqrt(left)), "slope": (c - (g[k - L] - K) * sg) / unit, "var": sd / unit, "res": rs / unit}
                if not ok:
                    continue
                won = w["result"] == side
                contracts = 1.0 / ask
                net = ((1 - ask) * contracts if won else -1.0) - fee(ask) * contracts
                rows.append((ser + w["ticker"], side, e, ask, int(won), net, close, feat))
print("rows", len(rows), "markets", len({r[0] for r in rows}))
P67 = {}
for name in LOOK:
    for kind in ("var", "res"):
        xs = sorted(r[7][name][kind] for r in rows)
        P67[(name, kind)] = xs[int(0.67 * (len(xs) - 1))]
print("67th percentile cutoffs of the variability (typical moves of the look-back):", {f"{a}-{b}": round(v, 3) for (a, b), v in P67.items()})
PB = lambda a: 0 if a < 0.35 else 1 if a < 0.5 else 2
day = lambda r: int(r[6] // 86400)
days = sorted({day(r) for r in rows})
by_day = collections.defaultdict(list)
for r in rows:
    by_day[day(r)].append(r)
K_, MIN_N = 10.0, 15
mid = sorted(r[6] for r in rows if day(r) in days[3:])
mid = mid[len(mid) // 2] if mid else 0


def key_fn(shape, up, down):
    tu, td = math.tan(math.radians(up)), math.tan(math.radians(down))

    def d(sl):
        return "u" if sl >= tu else "d" if sl <= -td else "f"

    def ch(name, kind):
        return "c" if P67[(name, kind)] <= 0 else None

    def f(feat):
        f5, f60 = feat["5m"], feat["60m"]
        c5 = "c" if f5["var"] >= P67[("5m", "var")] else "s"
        c60 = "c" if f60["var"] >= P67[("60m", "var")] else "s"
        r5 = "c" if f5["res"] >= P67[("5m", "res")] else "s"
        near5 = "n" if f5["z"] < 1.0 else "F"
        if shape == "dir5+dir60":
            return d(f5["slope"]) + d(f60["slope"])
        if shape == "dir5+chop5+dir60":
            return d(f5["slope"]) + c5 + d(f60["slope"])
        if shape == "dir5+dir60+chop60":
            return d(f5["slope"]) + d(f60["slope"]) + c60
        if shape == "dir5+detrended chop5+dir60":
            return d(f5["slope"]) + r5 + d(f60["slope"])
        return near5 + d(f5["slope"]) + c5 + d(f60["slope"])  # position + direction + variability at 5m, direction at 60m

    return f


def run(shape, up, down, bar):
    kf = key_fn(shape, up, down)
    keys = [kf(r[7]) for r in rows]
    kidx = {id(r): kk for r, kk in zip(rows, keys)}
    out = []
    for di, d in enumerate(days):
        if di < 3:
            continue
        seen, rec = set(), collections.defaultdict(lambda: [0, 0.0])
        for dd in days[:di]:
            for r in sorted(by_day[dd], key=lambda x: x[2]):
                kk = (r[0], r[1], kidx[id(r)], PB(r[3]))
                if kk in seen:
                    continue
                seen.add(kk)
                c = rec[(kidx[id(r)], PB(r[3]))]
                c[0] += 1
                c[1] += r[5]
        bought = set()
        for r in sorted(by_day[d], key=lambda x: x[2]):
            mk = (r[0], r[1])
            if mk in bought:
                continue
            n, tot = rec.get((kidx[id(r)], PB(r[3])), (0, 0.0))
            if n >= MIN_N and tot / (n + K_) >= bar:
                bought.add(mk)
                out.append(r)
    return out


results = []
SHAPES = ["dir5+dir60", "dir5+chop5+dir60", "dir5+dir60+chop60", "dir5+detrended chop5+dir60", "pos5+dir5+chop5+dir60"]
for shape in SHAPES:
    for up, down in itertools.product((30, 45, 60, 70), repeat=2):
        for bar in (0.10, 0.20):
            got = run(shape, up, down, bar)
            if len(got) >= 40:
                a = [r[5] * 10 for r in got if r[6] <= mid]
                b = [r[5] * 10 for r in got if r[6] > mid]
                results.append((sum(r[5] * 10 for r in got) / len(got), shape, up, down, bar, len(got), sum(r[4] for r in got) / len(got), sum(r[3] for r in got) / len(got), sum(a) / max(len(a), 1), len(a), sum(b) / max(len(b), 1), len(b)))
results.sort(reverse=True)
print("\nbest 25 of the grid by net per $10 ticket on the later days (both halves shown); the shape, UP and DOWN degrees, the bar, buys, win rate vs price paid")
for r in results[:25]:
    print(f"{r[1]:28s} up {r[2]:2d} down {r[3]:2d} bar {r[4]:.2f} buys {r[5]:4d} won {100 * r[6]:3.0f}% vs {r[7]:.2f} net/$10 {r[0]:+.2f} | halves {r[8]:+.2f} (n={r[9]}) / {r[10]:+.2f} (n={r[11]})")
print("\nthe same shapes at 45 degrees both ways and the 0.20 bar, for comparison with the letters:")
for r in results:
    if r[2] == 45 and r[3] == 45 and r[4] == 0.20:
        print(f"{r[1]:28s} buys {r[5]:4d} won {100 * r[6]:3.0f}% vs {r[7]:.2f} net/$10 {r[0]:+.2f} | halves {r[8]:+.2f} (n={r[9]}) / {r[10]:+.2f} (n={r[11]})")
print("DONE")
