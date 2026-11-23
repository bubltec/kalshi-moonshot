"""A slope and curvature factor on Kalshi's own price, from the last 180 minutes of Kalshi's own quotes in 5-minute segments. (2026-11-12, user: "looking at the previous 180mins chunked into 5 min segments we should analyze the curve slopes to make an additional factor applied to Kalshi's own prediction in both sides"; then, on review: "review history on the curvature in 5 minute increments (of the past 3 hours), weighted and reduced to influence the decision on whether to buy on either side of a tick".)
Rewritten 2026-11-12 after that review. The first version (commit 1753dce, result 2026-11-12-slope-factor.txt) read the external spot index, not Kalshi; its block means cancelled to three net returns, so the 5-minute segments never reached the fit; it had no curvature; its log-loss comparison clamped the two sides differently; it counted each moment twice (yes and no rows are mirrors); its ridge was on raw inputs; its "3 days" and "9 days" fits ran on whatever days existed. Its numbers are not comparable with this script's.
The factor, stated before any result. A 15-minute market only has 15 minutes of quotes, so the 180 minutes are stitched from the same series' markets: each market is cut into its three 5-minute thirds, and at a moment the 36 segments are the thirds this market has completed plus the thirds of the markets before it, newest first. Nothing crosses a market's edge, so a new strike never shows up as a move. In each segment the yes mid is read at its start, middle and end: slope = end - start, curvature = end - 2 x middle + start (positive = bending up). Both are divided by the window's own typical 5-minute move (the root-mean-square slope of its segments), so nothing is in cents or by asset. Weighted and reduced: segment j (0 = newest) has weight 36 - j, a straight decline to the oldest with no setting to choose, and the two weighted means S (slope) and C (curvature) are the whole reading. The adjusted chance of yes is logistic(logit(Kalshi's yes mid) + b_s S + b_c C); the no side is one minus it, which is the same rule with the signs flipped. b is fitted by logistic regression with the offset fixed at Kalshi's mid, on yes rows only, each market weighted as one observation, the inputs scaled to unit size and a ridge of 50 toward zero (the curve model's own); the share of the fit the ridge holds is printed, so "it can only tune" is a number and not a claim.
Gates taken out 2026-11-12 (user: "we had too many options firing that probably blocks most segments ... re-run the same test in slope_factor, just without any of the normal arguments like price or time impacting our buying window"; the gated run scored 2,232 of 14,436 markets, result 2026-11-12-slope-factor-kalshi-curve.txt): a point of the history is the last tick before it in its market however old, a reading needs one segment and not 30 of 36, and the buy has no price band and no minute window. Left out, and counted: a moment with no segment at all or a flat window, and a moment with no two-sided quote in the last 20 seconds (there is no ask to buy at). How many segments the readings had is printed. Anomalies (rule 3) are their own state: a window whose typical move is over three times the series' median over the last 24 hours, or whose S or C is outside that history's 1st/99th percentile, is counted and is in neither the fit nor the test.
The comparison: b fitted on the 1, 3 or 9 days before the test day (only when every one of those days is on the tape; otherwise no fit), against Kalshi's mid alone, on that test day only. Log-loss with one clamp for both, so b = 0 scores exactly zero. The buy test: the first moment per market (either side, minutes 1-14) where the adjusted chance is over the ask plus its fee by 0.02 is a buy of $5 held to settlement, with the fee as charged (rounded up to the cent per order), against not buying. Any ask, any moment. By curve shape: terciles of the factor's own two readings on the side bought. Standard errors over markets. The window is the DAYS (default 12, so that the 9-day fit has test days; on a shorter tape it prints "no fit") before the last settled market on the tape; no bot logic is in the question, so Monday's cutoff does not apply. One stated factor; nothing is swept after seeing the results.
    scripts/cloud_job.sh scripts/slope_factor.py          # MAX_MARKETS=3000 for a quick look (the most recent markets, all series)
"""

import bisect
import collections
import datetime as dt
import math
import os
import statistics
from zoneinfo import ZoneInfo

from kalshi_moonshot import config, db
from kalshi_moonshot.kalshiws import RECORD_ONLY
from kalshi_moonshot.lanes import lane_specs
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
from kalshi_moonshot.strategy import order_fee

s = config.reload()
ET = ZoneInfo("America/New_York")
DAYS = float(os.environ.get("DAYS", "12"))
MAXM = int(os.environ.get("MAX_MARKETS", "0"))
EDGE, STAKE, LAM = 0.02, 5.0, 50.0
NSEG, MIN_SEG = 36, 1  # 36 five-minute segments = 180 minutes; a reading needs one of them
STALE_NOW = 20.0  # seconds the two-sided quote may be old at the decision
CLAMP = 0.005  # one clamp on the mid, for Kalshi alone and for the adjusted chance alike
MIN_HIST = 30  # earlier readings a series needs before a reading can be called an anomaly
hm = lambda t: dt.datetime.fromtimestamp(t, ET).strftime("%m-%d %H:%M")  # noqa: E731
lg = lambda p: math.log(p / (1 - p))  # noqa: E731
pm = lambda mid: min(1 - CLAMP, max(CLAMP, mid))  # noqa: E731

series = {sr for sp in lane_specs(s).values() if sp.enabled and sp.model_buy for sr in sp.series}
store = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
allw = store.db.execute("SELECT ticker,series,close_ts,result FROM windows WHERE result IN ('yes','no') ORDER BY close_ts").fetchall()
allw = [w for w in allw if w["series"] in series and w["series"] not in RECORD_ONLY]
END = float(allw[-1]["close_ts"])
wins = [w for w in allw if float(w["close_ts"]) > END - DAYS * 86400]
if MAXM:
    wins = wins[-MAXM:]
wins.sort(key=lambda w: (w["series"], float(w["close_ts"])))
date0 = dt.datetime.fromtimestamp(min(float(w["close_ts"]) for w in wins), ET).date()


def thirds(qt, mids, opened):
    """The market's three 5-minute segments, oldest first: (slope, curvature) of the yes mid, or None where a point has no quote."""

    def at(o):
        t = opened + o
        i = bisect.bisect_right(qt, t) - 1
        if i >= 0:
            return mids[i]  # the last tick before the point, however old
        return mids[0] if o == 0 and qt and qt[0] < opened + 150 else None  # a market's first tick stands for its open

    p = [at(o) for o in range(0, 901, 150)]
    return [None if None in (p[2 * j], p[2 * j + 1], p[2 * j + 2]) else (p[2 * j + 2] - p[2 * j], p[2 * j + 2] - 2 * p[2 * j + 1] + p[2 * j]) for j in range(3)]


def reading(segs):
    """(typical 5-minute move, S, C) from the segments, newest first; None with no segment or a flat window."""
    have = [(j, x) for j, x in enumerate(segs) if x is not None]
    if len(have) < MIN_SEG:
        return None
    u = math.sqrt(sum(x[0] ** 2 for _, x in have) / len(have))
    if u <= 0:
        return None
    wsum = sum(NSEG - j for j, _ in have)
    return u, sum((NSEG - j) * x[0] for j, x in have) / wsum / u, sum((NSEG - j) * x[1] for j, x in have) / wsum / u


def q(xs, f):
    xs = sorted(xs)
    return xs[int(f * (len(xs) - 1))]


R = []  # (ticker, day, seconds in, yes bid, yes ask, yes won, S, C): the yes side; the no side is its mirror
skipped = collections.Counter()
nseg = collections.Counter()  # segments present -> readings (anomalies included)
cur, hist, past = None, {}, []
for w in wins:
    if w["series"] != cur:
        cur, hist, past = w["series"], {}, []  # hist: close -> thirds; past: (close, u, S, C) at each earlier market's open
    close = float(w["close_ts"])
    opened = close - 900.0
    rows = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], opened - 20, close)).fetchall()
    rows = [(float(x[0]), float(x[1] or 0.0), float(x[2] or 0.0)) for x in rows]
    book = [x for x in rows if x[2] > 0 and not (x[1] <= 0 and x[2] >= 1)]  # an empty book (0 / 1) is not a price
    th = thirds([x[0] for x in book], [(x[1] + x[2]) / 2 for x in book], opened)
    key = round(close)
    recent = [x for x in past if x[0] > close - 86400]
    trade = [x for x in rows if x[1] > 0 and 0 < x[2] < 1]  # both sides can be bought
    tt = [x[0] for x in trade]
    day = (dt.datetime.fromtimestamp(close, ET).date() - date0).days
    for e in range(0, 841, 30):
        segs = th[: e // 300][::-1]  # only the thirds this market has finished by now
        for back in range(1, 13):
            segs += (hist.get(key - 900 * back) or [None] * 3)[::-1]
        rd = reading(segs[:NSEG])
        if e == 0:
            if rd:
                past.append((close, *rd))
            continue
        if e < 60:
            continue
        i = bisect.bisect_right(tt, opened + e) - 1
        if i < 0 or opened + e - tt[i] > STALE_NOW:
            skipped["no price at the moment"] += 1
            continue
        if rd is None:
            skipped["no segment or a flat window"] += 1
            continue
        u, S, C = rd
        nseg[sum(1 for x in segs[:NSEG] if x is not None)] += 1
        if len(recent) >= MIN_HIST and (u > 3 * statistics.median(x[1] for x in recent) or not q([x[2] for x in recent], 0.01) <= S <= q([x[2] for x in recent], 0.99) or not q([x[3] for x in recent], 0.01) <= C <= q([x[3] for x in recent], 0.99)):
            skipped["anomaly (rule 3)"] += 1
            continue
        R.append((w["ticker"], day, e, trade[i][1], trade[i][2], 1.0 if w["result"] == "yes" else 0.0, S, C))
    hist[key] = th

print(f"{len(R)} moments from {len({x[0] for x in R})} markets of {len(wins)} on the tape from {hm(min(float(w['close_ts']) for w in wins))} to {hm(END)} ET (day 0 = {date0:%m-%d})")
print("left out: " + ", ".join(f"{k} {v}" for k, v in skipped.most_common()))
print("segments in a reading: " + ", ".join(f"{a}-{b}: {sum(v for k, v in nseg.items() if a <= k <= b)}" for a, b in ((1, 11), (12, 23), (24, 29), (30, 35), (36, 36))))
mid = lambda x: pm((x[3] + x[4]) / 2)  # noqa: E731


def fit(rows):
    """b (per unit of the scaled input), the scales, and the share of the fit the ridge holds."""
    per = collections.Counter(x[0] for x in rows)
    sc = [math.sqrt(sum(x[6 + i] ** 2 for x in rows) / len(rows)) or 1.0 for i in range(2)]
    b, H = [0.0, 0.0], [[0.0, 0.0], [0.0, 0.0]]
    for _ in range(12):
        gr, H = [0.0, 0.0], [[0.0, 0.0], [0.0, 0.0]]
        for x in rows:
            wt = 1.0 / per[x[0]]
            f = (x[6] / sc[0], x[7] / sc[1])
            p = 1 / (1 + math.exp(-(lg(mid(x)) + b[0] * f[0] + b[1] * f[1])))
            for i in range(2):
                gr[i] += wt * (x[5] - p) * f[i]
                for j in range(2):
                    H[i][j] += wt * p * (1 - p) * f[i] * f[j]
        g0, g1 = gr[0] - LAM * b[0], gr[1] - LAM * b[1]
        a, bb, c, d = H[0][0] + LAM, H[0][1], H[1][0], H[1][1] + LAM
        det = a * d - bb * c
        b = [b[0] + (d * g0 - bb * g1) / det, b[1] + (a * g1 - c * g0) / det]
    return b, sc, LAM / (LAM + (H[0][0] + H[1][1]) / 2)


def adj(x, m):
    b, sc = m[0], m[1]
    return 1 / (1 + math.exp(-(lg(mid(x)) + b[0] * x[6] / sc[0] + b[1] * x[7] / sc[1])))


def ll(x, p):
    return -(math.log(p) if x[5] else math.log(1 - p))


def first_buys(test, m, keep):
    out = {}
    for x in test:  # rows of a market are in time order
        if x[0] in out:
            continue
        p = adj(x, m)
        best = None
        for sign, ch, ask, won in ((1, p, x[4], x[5]), (-1, 1 - p, 1 - x[3], 1 - x[5])):
            edge = ch - (ask + fee(ask))
            if keep(ask, x[2]) and edge >= EDGE and (best is None or edge > best[0]):
                best = (edge, x[0], ask, won, sign * x[6], sign * x[7])
        if best:
            out[x[0]] = best[1:]
    return out


SETS = (("ANY", lambda ask, e: 0 < ask < 1),)  # no price band, no minute window
FITS = {"1 day": 1, "3 days": 3, "9 days": 9}
per_day = collections.Counter(x[1] for x in R)
full = {d for d, n in per_day.items() if n >= 500}
tot = {k: [] for k in FITS}
tested = {k: [] for k in FITS}
buys = {k: {"ANY": []} for k in FITS}
print("\nper test day: log-loss gain over Kalshi's mid (positive = better than Kalshi alone, x1000), b per one typical size of (slope, curvature), and the ridge's share of the fit")
for td in sorted(full):
    test = [x for x in R if x[1] == td]
    out = [f"day {td} n={len(test):6d}"]
    for name, nd in FITS.items():
        if not set(range(td - nd, td)) <= full:
            out.append(f"{name}: no fit (not {nd} full days before)")
            continue
        m = fit([x for x in R if td - nd <= x[1] < td])
        gain = [ll(x, mid(x)) - ll(x, adj(x, m)) for x in test]
        tot[name] += [(x[0], gn) for x, gn in zip(test, gain, strict=True)]
        tested[name].append(td)
        out.append(f"{name}: {1000 * sum(gain) / len(gain):+.2f} b=({m[0][0]:+.3f},{m[0][1]:+.3f}) ridge {m[2]:.0%}")
        for sname, keep in SETS:
            buys[name][sname] += first_buys(test, m, keep).values()
    print("  " + " | ".join(out))
zero = ([0.0, 0.0], [1.0, 1.0])
assert all(abs(ll(x, mid(x)) - ll(x, adj(x, zero))) < 1e-9 for x in R[:5000]), "b = 0 must score the same as Kalshi alone"
print("\nlog-loss gain over Kalshi's mid, each fit pooled over its own test days (x1000; standard error over markets)")
for name, xs in tot.items():
    by = collections.defaultdict(list)
    for tk, gn in xs:
        by[tk].append(gn)
    means = [statistics.fmean(v) for v in by.values()]
    if len(means) < 2:
        print(f"  fitted on the last {name:7s}: no test day")
        continue
    print(f"  fitted on the last {name:7s}: {1000 * statistics.fmean(means):+.3f} +- {1000 * statistics.stdev(means) / math.sqrt(len(means)):.3f}  ({len(means)} markets, test days {tested[name]})")


def dollars(x):  # (ticker, ask, won, S, C) -> (profit, cost) of a $5 buy held to settlement, the fee as charged
    n = max(1, int(STAKE // x[1]))
    cost = n * x[1] + order_fee(x[1], n)
    return n * x[2] - cost, cost


def line(label, xs):
    if not xs:
        return f"    {label:22s} n=0"
    d = [dollars(x) for x in xs]
    v = [p / c for p, c in d]
    se = statistics.stdev(v) / math.sqrt(len(v)) if len(v) > 1 else float("nan")
    return f"    {label:22s} n={len(xs):4d} | net per $1 {sum(v) / len(v):+.3f} (se {se:.3f}) | dollars {sum(p for p, _ in d):+8.2f} | beat not buying {sum(1 for p, _ in d if p > 0)}, lost to it {sum(1 for p, _ in d if p <= 0)} | mean ask {sum(x[1] for x in xs) / len(xs):.2f}"


print(f"\nthe buy test: first moment per market where the adjusted chance is over the ask plus fee by {EDGE}, ${STAKE:g} held to settlement, against not buying")
for sname, _ in SETS:
    print(f"  set {sname}")
    for name in FITS:
        xs = buys[name][sname]
        print(line(name, xs))
        if len(xs) > 90:
            for what, ix in (("slope", 3), ("curvature", 4)):
                c1, c2 = q([x[ix] for x in xs], 1 / 3), q([x[ix] for x in xs], 2 / 3)
                for lab, f_ in (("low", lambda x, ix=ix, c1=c1: x[ix] < c1), ("mid", lambda x, ix=ix, c1=c1, c2=c2: c1 <= x[ix] < c2), ("high", lambda x, ix=ix, c2=c2: x[ix] >= c2)):
                    print(line(f"    {what} {lab}", [x for x in xs if f_(x)]))
print("DONE")
