"""A slope factor on Kalshi's own price, from the last 180 minutes in 5-minute segments. (2026-11-12, user: "we cannot expect the last 9 days to be relevant ... get rid of that regression and only use it for minor tuning on a daily basis. looking at the previous 180mins chunked into 5 min segments we should analyze the curve slopes to make an additional factor applied to Kalshi's own prediction in both sides")
The factor, stated before any result: for a side at a moment, the 180 minutes before it are cut into 36 five-minute segments; segment j's slope is the change of the bet side's cushion over it (spot against the strike, sign by side), in that 180-minute window's own typical 5-minute move (so nothing is in dollars or by asset). Three summaries: r (the mean slope of the last 3 segments, 15 minutes), m (segments 4-12, minutes 15-60) and l (segments 13-36, minutes 60-180). The adjusted chance is logistic(logit(Kalshi's mid) + b_r r + b_m m + b_l l): Kalshi's price is the base, the factor is a shift in log-odds, and the other side's slopes flip sign so one rule serves both sides. The three b are fitted by logistic regression with the offset fixed at Kalshi's price and a hard ridge toward zero (penalty 50 per fit, the curve model's own), so the fit can only tune, never replace.
How much past the fit should use is what the user doubted, so it is the comparison: the b fitted on the 1, 3 or 9 days before the test day (each test day is scored on that day only), against no fit at all (the factor off = Kalshi's mid). Per test day: log-loss of each against Kalshi's mid, the fitted b, and the buy test: on the same test days, the first moment per market (either side, minutes 1-14) where the adjusted chance exceeds the ask plus its fee by 0.02 is a buy, held to settlement with the entry fee, per $1. Two price sets: ALL (ask 0.05-0.95) and BAND (0.70-0.90, minutes 7-15, the live lane's own). By curve shape: terciles of the spot's 13-minute level at the buy (the model's own reading). Standard errors over markets. The window is the tape the job restores (DAYS, default 9 before its end); no bot logic is in the question, so Monday's cutoff does not apply, and the days are said below. One stated factor; nothing is swept after seeing the results.
    scripts/cloud_job.sh scripts/slope_factor.py          # MAX_MARKETS=300 for a quick look
"""



import bisect, datetime as dt, itertools, math, os, statistics, time
from kalshi_moonshot import config, db, variants
from kalshi_moonshot.curvefeatures import moment_features, _unit
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
SELL_CAP = 0.90  # a bid above this is the winner being sold into settlement, the hindsight trade again; the waves are what is left
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



DAYS = float(os.environ.get("DAYS", "9"))
EDGE, STAKE, LAM = 0.02, 5.0, 50.0
wins = store.db.execute("SELECT ticker,series,close_ts,strike,result FROM windows WHERE close_ts>? AND result IN ('yes','no') AND strike>0 ORDER BY series,close_ts", (time.time() - DAYS * 86400,)).fetchall()
wins = [w for w in wins if w["series"] in series and w["series"] not in RECORD_ONLY]
if MAXM:
    wins = wins[-MAXM:]
t_first = min(float(w["close_ts"]) for w in wins) - 900
g0 = int(t_first - 3 * 3600 - 400)
DAY0 = dt.datetime.fromtimestamp(t_first, ET).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
cur_ix, g, sq, ts, vs = None, [], [0.0], [], []
R = []  # (ticker, day, e, side, mid, ask, win, r, m, l, level13)
for w in wins:
    ser = w["series"]
    crypto = variants.crypto_index(ser)
    ix = crypto or variants.biquote_index(ser)
    if not ix:
        continue
    if ix != cur_ix:
        cur_ix = ix
        r_ = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (ix, g0 - 60)).fetchall()
        ts, vs = [float(x[0]) for x in r_], [float(x[1]) for x in r_]
        g, j, last = [], 0, None
        for t in range(g0, int(time.time()) + 2):
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
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (w["ticker"], opened - 20, close)).fetchall()
    if len(q) < 20:
        continue
    qt = [float(x[0]) for x in q]
    day = int((close - DAY0) // 86400)
    for e in range(60, 841, 30):
        at = opened + e
        i = bisect.bisect_right(qt, at) - 1
        if i < 0 or at - qt[i] > 20:
            continue
        yb, ya = float(q[i][1]), float(q[i][2])
        k = int(at) - g0
        if k - 10800 < 0 or k >= len(g):
            continue
        u180 = _unit(sq, k, 10800)
        if u180 is None:
            continue
        u5 = u180 / math.sqrt(10800) * math.sqrt(300)
        pts = [g[k - 300 * jj] for jj in range(37)]
        if any(p is None for p in pts):
            continue
        f = moment_features(g, sq, k, strike_log, 1)
        for side, bid, ask in (("yes", yb, ya), ("no", 1 - ya, 1 - yb)):
            sign = 1 if side == "yes" else -1
            seg = [(pts[jj] - pts[jj + 1]) * sign / u5 for jj in range(36)]  # seg[0] = the most recent 5 minutes
            rr, mm, ll = sum(seg[0:3]) / 3, sum(seg[3:12]) / 9, sum(seg[12:36]) / 24
            lvl = f.get("level_13m")
            if lvl is None:
                continue
            R.append((w["ticker"], day, e, side, (bid + ask) / 2, ask, 1.0 if w["result"] == side else 0.0, rr, mm, ll, lvl * sign))
days = sorted({x[1] for x in R})
print(f"{len(R)} side-moments from {len({x[0] for x in R})} markets, days {days} (day 0 = {dt.datetime.fromtimestamp(DAY0, ET):%m-%d} ET); the tape ends {hm(max(float(w['close_ts']) for w in wins))} ET")
lg = lambda p: math.log(p / (1 - p))  # noqa: E731


def fit(rows):
    b = [0.0, 0.0, 0.0]
    for _ in range(12):
        gr, H = [0.0] * 3, [[0.0] * 3 for _ in range(3)]
        for x in rows:
            p0 = min(0.98, max(0.02, x[4]))
            z = lg(p0) + sum(b[i] * x[7 + i] for i in range(3))
            p = 1 / (1 + math.exp(-z))
            for i in range(3):
                gr[i] += (x[6] - p) * x[7 + i]
                for jx in range(3):
                    H[i][jx] += p * (1 - p) * x[7 + i] * x[7 + jx]
        for i in range(3):
            gr[i] -= LAM * b[i]
            H[i][i] += LAM
        # solve 3x3 by Gauss
        A = [H[i][:] + [gr[i]] for i in range(3)]
        for c in range(3):
            pv = max(range(c, 3), key=lambda r_: abs(A[r_][c]))
            A[c], A[pv] = A[pv], A[c]
            for r_ in range(3):
                if r_ != c:
                    fct = A[r_][c] / A[c][c]
                    A[r_] = [A[r_][i] - fct * A[c][i] for i in range(4)]
        step = [A[i][3] / A[i][i] for i in range(3)]
        b = [b[i] + step[i] for i in range(3)]
    return b


def adj(x, b):
    p0 = min(0.98, max(0.02, x[4]))
    return 1 / (1 + math.exp(-(lg(p0) + sum(b[i] * x[7 + i] for i in range(3)))))


def ll(x, p):
    p = min(0.999, max(0.001, p))
    return -(math.log(p) if x[6] else math.log(1 - p))


FITS = {"1 day": 1, "3 days": 3, "9 days": 9}
tot = {k: [] for k in FITS}
buys = {k: {"ALL": {}, "BAND": {}} for k in [*FITS, "Kalshi alone (factor off)"]}
print("\nper test day: log-loss gain over Kalshi's mid (positive = better than Kalshi alone, x1000), and the fitted b (recent 15 min, 15-60, 60-180)")
for td in days:
    test = [x for x in R if x[1] == td]
    if len(test) < 500:
        continue
    base = [ll(x, x[4]) for x in test]
    out = [f"day {td} n={len(test):6d}"]
    for name, nd in FITS.items():
        train = [x for x in R if td - nd <= x[1] < td]
        if len(train) < 3000:
            out.append(f"{name}: no fit (train {len(train)})")
            continue
        b = fit(train)
        gain = [(base[i] - ll(x, adj(x, b))) for i, x in enumerate(test)]
        tot[name] += [(x[0], gain[i]) for i, x in enumerate(test)]
        out.append(f"{name}: {1000 * sum(gain) / len(gain):+.2f} b=({b[0]:+.3f},{b[1]:+.3f},{b[2]:+.3f})")
        for sname, keep in (("ALL", lambda x: 0.05 <= x[5] <= 0.95), ("BAND", lambda x: 0.70 <= x[5] <= 0.90 and x[2] >= 420)):
            first = {}
            for x in test:
                if not keep(x):
                    continue
                if adj(x, b) - (x[5] + fee(x[5])) >= EDGE and (x[0] not in first or x[2] < first[x[0]][2]):
                    first[x[0]] = x
            buys[name][sname].update({(td, k_): v for k_, v in first.items()})
    first = {}
    for sname, keep in (("ALL", lambda x: 0.05 <= x[5] <= 0.95), ("BAND", lambda x: 0.70 <= x[5] <= 0.90 and x[2] >= 420)):
        first = {}
        for x in test:
            if keep(x) and x[4] - (x[5] + fee(x[5])) >= EDGE and (x[0] not in first or x[2] < first[x[0]][2]):
                first[x[0]] = x
        buys["Kalshi alone (factor off)"][sname].update({(td, k_): v for k_, v in first.items()})
    print("  " + " | ".join(out))
print("\nlog-loss gain over Kalshi's mid, all test days pooled (x1000; standard error over markets)")
for name, xs in tot.items():
    if not xs:
        continue
    by = {}
    for tk, gn in xs:
        by.setdefault(tk, []).append(gn)
    means = [statistics.fmean(v) for v in by.values()]
    print(f"  fitted on the last {name:7s}: {1000 * statistics.fmean(means):+.3f} +- {1000 * statistics.stdev(means) / math.sqrt(len(means)):.3f}  ({len(means)} markets)")


def net(x):
    cost = x[5] + fee(x[5])
    return x[6] / cost - 1.0


def line(label, xs):
    if not xs:
        return f"    {label:28s} n=0"
    v = [net(x) for x in xs]
    by = {}
    for x in xs:
        by.setdefault(x[0], []).append(net(x))
    se = statistics.stdev(v) / math.sqrt(len(v)) if len(v) > 1 else float("nan")
    return f"    {label:28s} n={len(xs):4d} | net per $1 {sum(v) / len(v):+.3f} (se {se:.3f}) | dollars at ${STAKE:g} {STAKE * sum(v):+8.2f} | won {sum(1 for x in xs if x[6] > 0) / len(xs):.0%} | mean ask {sum(x[5] for x in xs) / len(xs):.2f}"


print("\nthe buy test: first moment per market where the chance (adjusted, or Kalshi's mid alone) is over the ask plus fee by 0.02, held to settlement")
for sname in ("ALL", "BAND"):
    print(f"  set {sname}")
    for name in buys:
        xs = list(buys[name][sname].values())
        print(line(name, xs))
        if name != "Kalshi alone (factor off)" and len(xs) > 90:
            lv = sorted(x[10] for x in xs)
            c1, c2 = lv[len(lv) // 3], lv[2 * len(lv) // 3]
            for lab, f_ in (("level low", lambda x: x[10] < c1), ("level mid", lambda x: c1 <= x[10] < c2), ("level high", lambda x: x[10] >= c2)):
                print(line("    " + lab, [x for x in xs if f_(x)]))
print("DONE")
