"""Where, in time and on which side, is Kalshi's price wrong after the fee? (2026-11-12, user: "this just means you're betting at the wrong time or on the wrong side")
No model, no factor, no picks. Every 30-second moment from minute 1 to 14 of every settled 15-minute market in the tape the job restores (DAYS, default 9; the days are said below), each side, with three coordinates only:
  the minute of the window (1-4, 4-7, 7-10, 10-13, 13-14.5),
  the price paid (the ask: 0.05-0.20, 0.20-0.40, 0.40-0.60, 0.60-0.80, 0.80-0.90, 0.90-0.97),
  the spot's side of the strike (the 13-minute cushion on the bet's side: with us, at or above zero, or against us, below): the curve's own reading, never the YES/NO label or the asset.
Per cell: markets, moments, how often the side won, the mean ask (a fair price would make these two equal before the fee), and net per $1 bought at that ask held to settlement with the entry fee, with its standard error taken over markets (each market's own mean in the cell, so moments 30 seconds apart do not count as independent). Also the YES-against-NO split, to see whether the label itself is priced wrong. 60 cells, so about three land past |t| = 2 by chance: a cell is only worth a rule if its sign holds on each separate day (printed), and it goes to one stated rule scored forward, never straight to a lane.
    scripts/cloud_job.sh scripts/time_side_grid.py          # MAX_MARKETS=300 for a quick look
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

MINS = ((60, 240), (240, 420), (420, 600), (600, 780), (780, 871))
ASKS = ((0.05, 0.20), (0.20, 0.40), (0.40, 0.60), (0.60, 0.80), (0.80, 0.90), (0.90, 0.97))


def netv(x):
    return x[6] / (x[5] + fee(x[5])) - 1.0


def cell(xs):
    by, byd = {}, {}
    for x in xs:
        by.setdefault(x[0], []).append(netv(x))
        byd.setdefault(x[1], {}).setdefault(x[0], []).append(netv(x))
    means = [statistics.fmean(v) for v in by.values()]
    if len(means) < 5:
        return None
    se = statistics.stdev(means) / math.sqrt(len(means))
    perday = {}
    for d, m_ in byd.items():
        vs_ = [statistics.fmean(v) for v in m_.values()]
        if len(vs_) >= 20:
            perday[d] = statistics.fmean(vs_)
    return len(means), len(xs), sum(x[6] for x in xs) / len(xs), sum(x[5] for x in xs) / len(xs), statistics.fmean(means), se, perday


rows = []
print(f"\nthe grid: net per $1 bought at the ask, held to settlement, fee in; markets / won / mean ask / net (t) / days positive of days with 20+ markets")
for side_lab, keepc in (("spot WITH us (cushion >= 0)", lambda x: x[10] >= 0), ("spot AGAINST us (cushion < 0)", lambda x: x[10] < 0)):
    print(f"\n=== {side_lab} ===")
    for (m0, m1) in MINS:
        for (a0, a1) in ASKS:
            xs = [x for x in R if m0 <= x[2] < m1 and a0 <= x[5] < a1 and keepc(x)]
            c = cell(xs)
            if c is None:
                continue
            n, nm_, won, ask, net, se, perday = c
            t = net / se if se > 0 else 0.0
            pos = sum(1 for v in perday.values() if v > 0)
            rows.append((t, side_lab, m0, a0, n, net, perday))
            print(f"  minute {m0 / 60:4.1f}-{m1 / 60:4.1f} ask {a0:.2f}-{a1:.2f} | markets {n:5d} | won {won:5.1%} vs ask {ask:.3f} | net {net:+.3f} (t {t:+.1f}) | days positive {pos}/{len(perday)}")
print(f"\n{len(rows)} cells; {sum(1 for r in rows if abs(r[0]) > 2)} past |t| = 2 (about {0.05 * len(rows):.0f} by chance); positive ones past t = 2:")
for t, lab, m0, a0, n, net, perday in sorted(rows, reverse=True)[:8]:
    print(f"  t {t:+.1f} | {lab} | minute {m0 / 60:.1f}+ ask {a0:.2f}+ | markets {n} | net {net:+.3f} | by day " + ", ".join(f"{d}:{v:+.2f}" for d, v in sorted(perday.items())))
print("\nYES against NO (the label itself), all minutes, ask 0.05-0.97")
for sd in ("yes", "no"):
    c = cell([x for x in R if x[3] == sd and 0.05 <= x[5] < 0.97])
    print(f"  {sd}: markets {c[0]} | won {c[2]:.1%} vs ask {c[3]:.3f} | net {c[4]:+.3f} (t {c[4] / c[5]:+.1f})")
print("DONE")
