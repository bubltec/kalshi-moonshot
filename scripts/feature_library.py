"""Which curve features say something about what the contract's price does next? (2026-11-12, user: "not even curve predictions, just curve features ... curves identify different features (dozens, maybe hundreds)", to understand time frames and cushion)
No model, no bar, no picks. Every 30-second moment of every settled 15-minute market since Monday 2026-11-09 (the series the model lanes trade; crypto and commodity), each side, minute 1 on, with every feature in kalshi_moonshot/curvefeatures.py (about 100: level, slope, bend, speed, range, efficiency, drawdown, crossings, bounce at ten time frames up to 3 hours, the contract's own recent price move, taker volume imbalance and spikes). Each in the curve's own variance units; nothing keyed on an asset.
The question each feature answers is the 11-11 one, timing and cushion, not the settled result: buy the side at its ask now and sell it into the bid H minutes later (H = 1, 2, 3, the sale at the bid less the 0.02 exit slip and the taker fee, the buy with its fee), net per $1 spent. Same moments give the sale side: the change of the bid over the next H minutes (cents), which is what selling now saves or gives away. Moments whose exit time falls after the window's end or has no quote within 20 s are left out of that horizon.
Per feature: terciles of the moments; the high-minus-low spread of the mean round-trip net (H=2), its standard error taken over MARKETS (each market's own mean in the bucket, so overlapping moments do not count as independent), the same spread inside each tercile of the 13-minute level (the curve shape) and inside each tercile of the ask (a feature that only repeats Kalshi's price loses its spread there); the spread must keep its sign in all three of each before anyone calls it anything, and the sale-side bid change. Ranked by |t|. With about 100 features, about five land past |t| = 2 by chance alone: the count past 2 is printed against that. This is a screen, not a rule; anything it finds goes to one stated rule scored forward.
Two sets: ALL (ask 0.05-0.95) and BAND (ask 0.70-0.90, minutes 7-15, the live lane's own).
    scripts/cloud_job.sh scripts/feature_library.py          # MAX_MARKETS=300 for a quick look
"""
import bisect, datetime as dt, itertools, math, os, statistics, time
from kalshi_moonshot import config, db, variants
from kalshi_moonshot.curvefeatures import moment_features
from kalshi_moonshot.curvemodel import MOMENT_STEP
from kalshi_moonshot.kalshiws import RECORD_ONLY
from kalshi_moonshot.lanes import lane_specs
from kalshi_moonshot.runner import EXIT_SLIP
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
ET = dt.timezone(dt.timedelta(hours=-5))
MON = dt.datetime(2026, 11, 9, tzinfo=ET).timestamp()
HORIZ = (60, 120, 180)
MAXM = int(os.environ.get("MAX_MARKETS", "0"))
hm = lambda t: dt.datetime.fromtimestamp(t, ET).strftime("%m-%d %H:%M")  # noqa: E731
series = {sr for sp in lane_specs(s).values() if sp.enabled and sp.model_buy for sr in sp.series}
store = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
wins = store.db.execute("SELECT ticker,series,close_ts,strike,result FROM windows WHERE close_ts>? AND result IN ('yes','no') AND strike>0 ORDER BY series,close_ts", (MON,)).fetchall()
wins = [w for w in wins if w["series"] in series and w["series"] not in RECORD_ONLY]
if MAXM:
    wins = wins[-MAXM:]
now = time.time()
g0 = int(MON - 3 * 3600 - 300)
g1 = int(now) + 2
M = []  # (market, side, e, features, {target: value})
cur_ix, g, sq, ts, vs = None, [], [0.0], [], []
for w in wins:
    ser = w["series"]
    crypto = variants.crypto_index(ser)
    ix = crypto or variants.biquote_index(ser)
    if not ix:
        continue
    if ix != cur_ix:
        cur_ix = ix
        r = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (ix, g0 - 60)).fetchall()
        ts, vs = [float(x[0]) for x in r], [float(x[1]) for x in r]
        g, j, last = [], 0, None
        for t in range(g0, g1):
            while j < len(ts) and ts[j] <= t:
                last = vs[j]
                j += 1
            g.append(math.log(last) if last else None)
        sq = [0.0]
        for a, b in itertools.pairwise(g):
            sq.append(sq[-1] + ((b - a) ** 2 if a is not None and b is not None else 0.0))
    close = float(w["close_ts"])
    opened = close - 900.0
    if crypto:
        strike_log = math.log(float(w["strike"]))
    else:
        a = bisect.bisect_left(ts, opened - 5.0)
        if a >= len(ts) or ts[a] > opened + 20.0:
            continue
        strike_log = math.log(vs[a])
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (w["ticker"], opened - 400, close)).fetchall()
    if len(q) < 20:
        continue
    qt = [float(x[0]) for x in q]
    fl = tape.execute("SELECT ts,yes_vol,no_vol FROM flow_1s WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], int(opened - 700), int(close))).fetchall()
    ft = [float(x[0]) for x in fl]
    cy, cn = [0.0], [0.0]
    for x in fl:
        cy.append(cy[-1] + float(x[1]))
        cn.append(cn[-1] + float(x[2]))

    def quote_at(t):
        i = bisect.bisect_right(qt, t) - 1
        if i < 0 or t - qt[i] > 20:
            return None
        return float(q[i][1]), float(q[i][2])

    def vol(t0, t1):  # (yes, no) contracts taken in (t0, t1]
        a, b = bisect.bisect_right(ft, t0), bisect.bisect_right(ft, t1)
        return cy[b] - cy[a], cn[b] - cn[a]

    for e in range(60, 841, MOMENT_STEP):
        at = opened + e
        k = int(at) - g0
        if k < 0 or k >= len(g):
            continue
        cq = quote_at(at)
        if cq is None:
            continue
        yb, ya = cq
        for side, bid, ask in (("yes", yb, ya), ("no", 1 - ya, 1 - yb)):
            sign = 1 if side == "yes" else -1
            flow = {}
            for sec in (30, 60, 180):
                mine, other = vol(at - sec, at)[(0 if side == "yes" else 1)], vol(at - sec, at)[(1 if side == "yes" else 0)]
                flow[f"flowimb_{sec}s"] = (mine - other) / (mine + other) if mine + other > 0 else 0.0
                pm, po = vol(at - sec - 600, at - sec)
                base = (pm + po) / (600.0 / sec)
                flow[f"flowspike_{sec}s"] = (mine + other) / base if base > 0 else 0.0
            quote = {"ask": ask, "spread": ask - bid, "left_min": (900.0 - e) / 60.0}
            for mn in (1, 2, 3):
                pq = quote_at(at - 60 * mn)
                if pq is not None:
                    quote[f"mom_{mn}m"] = (bid - (pq[0] if side == "yes" else 1 - pq[1])) * 100.0
            f = moment_features(g, sq, k, strike_log, sign, flow, quote)
            if "level_5m" not in f:
                continue
            tg = {}
            for H in HORIZ:
                if e + H > 870:
                    continue
                xq = quote_at(at + H)
                if xq is None:
                    continue
                xb = xq[0] if side == "yes" else 1 - xq[1]
                if xb <= 0:
                    continue
                tg[f"rt{H // 60}"] = (xb - EXIT_SLIP - fee(xb)) / (ask + fee(ask)) - 1.0
                tg[f"db{H // 60}"] = (xb - bid) * 100.0
            if tg:
                M.append((w["ticker"], side, e, f, tg, ask))
print(f"{len(M)} moments from {len({m[0] for m in M})} markets since Monday 11-09; the tape ends {hm(max(float(w['close_ts']) for w in wins))} ET; features per moment up to {max(len(m[3]) for m in M)}")
SETS = {"ALL (ask 0.05-0.95)": lambda m: 0.05 <= m[5] <= 0.95, "BAND (ask 0.70-0.90, minutes 7-15)": lambda m: 0.70 <= m[5] <= 0.90 and m[2] >= 420}


def cell(xs, key):
    """(markets, moments, mean of key over markets' own means, se over markets)."""
    by = {}
    for m in xs:
        if key in m[4]:
            by.setdefault(m[0], []).append(m[4][key])
    means = [sum(v) / len(v) for v in by.values()]
    if len(means) < 2:
        return len(means), 0, float("nan"), float("nan")
    return len(means), sum(len(v) for v in by.values()), statistics.fmean(means), statistics.stdev(means) / math.sqrt(len(means))


def terciles(S, name):
    v = sorted(m[3][name] for m in S if name in m[3])
    if len(v) < 150:
        return None
    c1, c2 = v[len(v) // 3], v[2 * len(v) // 3]
    lo = [m for m in S if name in m[3] and m[3][name] < c1]
    mid = [m for m in S if name in m[3] and c1 <= m[3][name] < c2]
    hi = [m for m in S if name in m[3] and m[3][name] >= c2]
    return (c1, c2), lo, mid, hi


def spread(lo, hi, key):
    a, b = cell(lo, key), cell(hi, key)
    d = b[2] - a[2]
    se = math.sqrt(a[3] ** 2 + b[3] ** 2)
    return d, se, (d / se if se > 0 else float("nan"))


for sname, keep in SETS.items():
    S = [m for m in M if keep(m)]
    names = sorted({k for m in S for k in m[3]} - {"ask"})
    print(f"\n=== {sname}: {len(S)} moments, {len({m[0] for m in S})} markets ===")
    for key in ("rt2",):
        print(f"all moments, round trip H=2: mean net per $1 {cell(S, 'rt2')[2]:+.4f}  (the baseline every feature is a change from); H=1 {cell(S, 'rt1')[2]:+.4f}, H=3 {cell(S, 'rt3')[2]:+.4f}")
    shape = terciles(S, "level_13m")
    av = sorted(m[5] for m in S)
    ac1, ac2 = av[len(av) // 3], av[2 * len(av) // 3]  # the price terciles: a feature that only repeats the ask must lose its spread inside one of these
    abands = [[m for m in S if m[5] < ac1], [m for m in S if ac1 <= m[5] < ac2], [m for m in S if m[5] >= ac2]]
    print(f"ask terciles cut at {ac1:.2f} and {ac2:.2f}")
    rows = []
    for nm in names:
        t = terciles(S, nm)
        if t is None:
            continue
        _, lo, mid, hi = t
        d, se, tt = spread(lo, hi, "rt2")
        parts = []
        if shape:
            for lab, sh in zip(("low", "mid", "high"), shape[1:]):
                ids = {id(m) for m in sh}
                l2, h2 = [m for m in lo if id(m) in ids], [m for m in hi if id(m) in ids]
                if len(l2) > 40 and len(h2) > 40:
                    parts.append(spread(l2, h2, "rt2")[0])
                else:
                    parts.append(float("nan"))
        aparts = []
        for band in abands:
            ids = {id(m) for m in band}
            l2, h2 = [m for m in lo if id(m) in ids], [m for m in hi if id(m) in ids]
            aparts.append(spread(l2, h2, "rt2")[0] if len(l2) > 40 and len(h2) > 40 else float("nan"))
        asame = all(p == p for p in aparts) and (all(p > 0 for p in aparts) or all(p < 0 for p in aparts)) and (d > 0) == (aparts[0] > 0)
        same = all(p == p for p in parts) and (all(p > 0 for p in parts) or all(p < 0 for p in parts)) and (d > 0) == (parts[0] > 0) if parts else False
        dd = spread(lo, hi, "db2")
        rows.append((abs(tt) if tt == tt else 0, nm, d, se, tt, parts, same, dd, t, aparts, asame))
    rows.sort(reverse=True)
    n_sig = sum(1 for r in rows if r[0] > 2)
    print(f"{len(rows)} features screened; {n_sig} past |t| = 2 (about {0.05 * len(rows):.0f} expected by chance)")
    print("feature        | spread of round-trip net per $1, high minus low tercile (t) | inside level_13m low/mid/high | same sign in all three | inside ask terciles low/mid/high | same sign in all three | spread of the bid change, cents (t)")
    for _, nm, d, se, tt, parts, same, dd, t, aparts, asame in rows[:30]:
        print(f" {nm:13s} | {d:+.4f} ({tt:+.1f}) | {' / '.join(f'{p:+.3f}' for p in parts)} | {'yes' if same else 'no '} | {' / '.join(f'{p:+.3f}' for p in aparts)} | {'yes' if asame else 'no '} | {dd[0]:+.2f} ({dd[2]:+.1f})")
    print("\nthe ten strongest, bucket by bucket (cuts; markets, moments, mean round-trip net per $1 at H=1/2/3, bid change at H=2 in cents)")
    for _, nm, d, se, tt, parts, same, dd, t, aparts, asame in rows[:10]:
        (c1, c2), lo, mid, hi = t
        print(f" {nm} (cuts {c1:+.2f}, {c2:+.2f})")
        for lab, xs in (("low", lo), ("mid", mid), ("high", hi)):
            a = cell(xs, "rt2")
            print(f"   {lab:5s} markets {a[0]:4d} moments {a[1]:6d} | net/$1 {cell(xs, 'rt1')[2]:+.4f} {a[2]:+.4f} {cell(xs, 'rt3')[2]:+.4f} | bid change {cell(xs, 'db2')[2]:+.2f}")
print("DONE")
