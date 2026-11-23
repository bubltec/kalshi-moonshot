"""Look-alike stretches of Kalshi's own ticks: when the latest run of ticks looks like earlier runs in the last 3 hours, did the curve go the way it went after them? (2026-11-12, user: "the segments shouldn't matter. we should look at the bare tick values for comparison and not care about price, time frame, etc"; compare with "earlier stretches of ticks"; "a blank canvas to test ideas on, not compounded on solutions that don't work".)
A blank canvas: nothing here is fitted, and nothing comes from slope_factor.py or the curve model. Stated before any result.
A tick is one change of a market's yes mid (Kalshi's quotes only). A series' ticks are laid end to end in the order they happened; the step from one market's last quote to the next market's first is not a tick, so a new strike is never read as a move. No price level, no clock time and no market enters the comparison: a stretch is its last 20 tick changes divided by their own root-mean-square size, so only its shape is left.
At a moment (every 60 seconds, minutes 1-14 of a market) the latest stretch is set against every earlier stretch that began inside the last 3 hours and whose next 20 ticks have already happened, by correlation. A stretch that matches upside down counts as a match for the other side, with what followed it turned over (the no side's ticks are the yes side's with the sign changed). The look-alikes are the closest 5% (by the size of the correlation; at least 100 earlier stretches are needed). The reading F is what followed them: each look-alike's move over its next 20 ticks in its own tick size, weighted in a straight line from the closest down. F > 0 points at yes, F < 0 at no.
Sat out and counted: a moment with no tick yet in its market, with a quote over 20 seconds old, or with under 100 earlier stretches. Anomaly (rule 3): the stretch's tick size is over three times the series' median over the last 24 hours, or outside that history's 1st/99th percentile; it is counted and never scored.
Three questions, all on the same moments. (1) Direction: how often does the sign of F match the sign of the next 20 ticks, and how large is that move on F's side, in tick sizes. (2) The buy: the first moment per market where |F| is at or over the 80th percentile of that series' own |F| over the last 24 hours (30 earlier readings needed), buy the side F points at for $5, ask 0.05-0.95; held to settlement (fee on the buy), and as a round trip sold at the bid 20 ticks later (fee on both legs; held to settlement if the market ends first). (3) Against what: the other side bought at the same moment, so F's direction is the only thing tested. By curve shape: terciles of the reading's own size and of the latest stretch's own drift toward the side bought. The settings (20 ticks, 5%, 80th percentile) were chosen before the first run and are not swept. Window: DAYS (default 9) before the last settled market on the tape; no bot logic is in the question, so Monday's cutoff does not apply.
    scripts/cloud_job.sh scripts/tick_lookalike.py
"""

import bisect
import collections
import datetime as dt
import heapq
import math
import os
import statistics
from zoneinfo import ZoneInfo

from kalshi_moonshot import config, db
from kalshi_moonshot.kalshiws import RECORD_ONLY
from kalshi_moonshot.lanes import lane_specs
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import order_fee

s = config.reload()
ET = ZoneInfo("America/New_York")
DAYS = float(os.environ.get("DAYS", "9"))
L = H = 20  # ticks in a stretch, and ticks after it
NEAR, MIN_CAND = 0.05, 100  # the closest share that count as look-alikes; earlier stretches needed
LOOK, STALE = 10800.0, 20.0  # seconds of history; seconds a quote may be old at the moment
TOP, MIN_HIST, STAKE = 0.80, 30, 5.0
hm = lambda t: dt.datetime.fromtimestamp(t, ET).strftime("%m-%d %H:%M")  # noqa: E731

series = {sr for sp in lane_specs(s).values() if sp.enabled and sp.model_buy for sr in sp.series}
store = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
allw = store.db.execute("SELECT ticker,series,close_ts,result FROM windows WHERE result IN ('yes','no') ORDER BY close_ts").fetchall()
allw = [w for w in allw if w["series"] in series and w["series"] not in RECORD_ONLY]
END = float(allw[-1]["close_ts"])
by_series = collections.defaultdict(list)
for w in allw:
    if float(w["close_ts"]) > END - DAYS * 86400:
        by_series[w["series"]].append(w)

skipped = collections.Counter()
READ = []  # (F, the next 20 ticks' move in tick sizes or None)
BUYS = []  # (ticker, |F|, drift toward the side, (ask, won, exit bid or None) of F's side, the same of the other side)
n_markets = 0
for ser in sorted(by_series):
    ws = by_series[ser]
    n_markets += len(ws)
    T, D, B, A, M, raws = [], [], [], [], [], []
    for mi, w in enumerate(ws):
        close = float(w["close_ts"])
        rows = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], close - 920, close)).fetchall()
        raw, prev = [], None
        for x in rows:
            ts, b, a = float(x[0]), float(x[1] or 0.0), float(x[2] or 0.0)
            if a <= 0 or (b <= 0 and a >= 1):  # an empty book (0 / 1) is not a price
                continue
            raw.append(ts)
            m = (b + a) / 2
            if prev is not None and abs(m - prev) > 1e-9:
                T.append(ts)
                D.append(m - prev)
                B.append(b)
                A.append(a)
                M.append(mi)
            prev = m
        raws.append(raw)
    if len(D) < L + H + MIN_CAND:
        skipped["series with too few ticks (markets)"] += len(ws)
        continue
    P, sq = [], [0.0]  # the stitched path after each tick; running sum of squared ticks
    for x in D:
        P.append((P[-1] if P else 0.0) + x)
        sq.append(sq[-1] + x * x)
    rms = [math.sqrt((sq[r + L] - sq[r]) / L) for r in range(len(D) - L + 1)]  # row r = ticks r .. r+L-1
    hist_t, hist_u, hist_f, h0 = [], [], [], 0  # this series' earlier readings, for its own cutoffs
    for mi, w in enumerate(ws):
        close = float(w["close_ts"])
        opened = close - 900.0
        bought = False
        for e in range(60, 841, 60):
            at = opened + e
            i0 = bisect.bisect_right(T, at) - 1
            if i0 < L - 1 or M[i0] != mi:
                skipped["no tick yet in this market"] += 1
                continue
            j = bisect.bisect_right(raws[mi], at) - 1
            if j < 0 or at - raws[mi][j] > STALE:
                skipped["quote over 20 seconds old"] += 1
                continue
            lo, hi = bisect.bisect_left(T, at - LOOK), i0 - H - L + 1  # rows whose stretch began in the window and whose next H ticks are done
            if hi - lo + 1 < MIN_CAND:
                skipped["under 100 earlier stretches"] += 1
                continue
            r0 = i0 - L + 1
            u = float(rms[r0])
            while h0 < len(hist_t) and hist_t[h0] <= at - 86400:
                h0 += 1
            us = sorted(hist_u[h0:])
            hist_t.append(at)
            hist_u.append(u)
            if len(us) >= MIN_HIST and (u > 3 * us[len(us) // 2] or not us[int(0.01 * (len(us) - 1))] <= u <= us[int(0.99 * (len(us) - 1))]):
                hist_f.append(None)
                skipped["anomaly (rule 3)"] += 1
                continue
            qv = D[r0 : r0 + L]
            c = [math.sumprod(D[r : r + L], qv) / rms[r] for r in range(lo, hi + 1)]  # each earlier stretch's correlation with the latest, up to a constant
            k = max(5, int(NEAR * len(c)))
            near = heapq.nlargest(k, range(len(c)), key=lambda ix: abs(c[ix]))  # closest first
            F = sum((k - rank) * math.copysign(1.0, c[ix]) * (P[lo + ix + L - 1 + H] - P[lo + ix + L - 1]) / rms[lo + ix] for rank, ix in enumerate(near)) / (k * (k + 1) / 2)
            fs = [abs(x) for x in hist_f[h0:] if x is not None]
            hist_f.append(F)
            nxt = (P[i0 + H] - P[i0]) / u if i0 + H < len(D) else None
            READ.append((F, nxt))
            if bought or F == 0 or len(fs) < MIN_HIST or abs(F) < sorted(fs)[int(TOP * (len(fs) - 1))]:
                continue
            yb, ya = B[i0], A[i0]
            if not (yb > 0 and ya < 1):
                continue
            sign = 1 if F > 0 else -1
            ask = ya if sign > 0 else 1 - yb
            if not 0.05 <= ask <= 0.95:
                continue
            won = 1.0 if (w["result"] == "yes") == (sign > 0) else 0.0
            out = (B[i0 + H], A[i0 + H]) if i0 + H < len(D) and M[i0 + H] == mi else None  # the book 20 ticks later, if this market is still open
            mine = (ask, won, None if out is None else (out[0] if sign > 0 else 1 - out[1]))
            other = (1 - yb if sign > 0 else ya, 1 - won, None if out is None else (1 - out[1] if sign > 0 else out[0]))
            BUYS.append((w["ticker"], abs(F), sign * sum(qv) / (u * math.sqrt(L)), mine, other))
            bought = True

print(f"{len(READ)} readings and {len(BUYS)} buys from {n_markets} markets on the tape from {hm(END - DAYS * 86400)} to {hm(END)} ET")
print("sat out: " + ", ".join(f"{k} {v}" for k, v in skipped.most_common()))


def direction(label, xs):
    xs = [(f, n) for f, n in xs if n is not None and f != 0 and n != 0]
    if not xs:
        return f"  {label:34s} n=0"
    hit = sum(1 for f, n in xs if (f > 0) == (n > 0))
    mv = [n if f > 0 else -n for f, n in xs]
    return f"  {label:34s} n={len(xs):6d} | same sign {hit / len(xs):.1%} | next 20 ticks on F's side {statistics.fmean(mv):+.3f} tick sizes (se {statistics.stdev(mv) / math.sqrt(len(mv)):.3f})"


print("\n(1) direction: the sign of F against the sign of the next 20 ticks (readings overlap, so the standard error is too small)")
print(direction("every reading", READ))
cut = sorted(abs(f) for f, _ in READ)[int(TOP * (len(READ) - 1))] if READ else 0.0
print(direction("largest fifth of |F| (pooled)", [x for x in READ if abs(x[0]) >= cut]))


def settle(b):  # (ask, won, exit bid) -> dollars of a $5 buy held to settlement
    n = max(1, int(STAKE // b[0]))
    return n * b[1] - n * b[0] - order_fee(b[0], n)


def trip(b):  # sold at the bid 20 ticks later, a fee on each leg; held if the market ended first or there is no bid
    if b[2] is None or b[2] <= 0:
        return settle(b)
    n = max(1, int(STAKE // b[0]))
    return n * b[2] - order_fee(b[2], n) - n * b[0] - order_fee(b[0], n)


def line(label, xs):
    if not xs:
        return f"    {label:20s} n=0"
    out = f"    {label:20s} n={len(xs):5d}"
    for name, fn in (("settlement", settle), ("round trip", trip)):
        mine, other = [fn(x[3]) for x in xs], [fn(x[4]) for x in xs]
        diff = [a - b for a, b in zip(mine, other, strict=True)]
        se = statistics.stdev(mine) / math.sqrt(len(mine)) if len(mine) > 1 else float("nan")
        out += f" | {name}: F's side {sum(mine):+9.2f} (per bet {statistics.fmean(mine):+.3f}, se {se:.3f}), other side {sum(other):+9.2f}, beat it {sum(1 for x in diff if x > 0)}, lost to it {sum(1 for x in diff if x < 0)}"
    return out


print(f"\n(2) the buy: first moment per market with |F| at or over its series' own 80th percentile, ${STAKE:g} on F's side, against the other side at the same moment (dollars, fees in)")
print(line("all buys", BUYS))
if len(BUYS) > 90:
    for what, ix in (("|F|", 1), ("drift to the side", 2)):
        v = sorted(x[ix] for x in BUYS)
        c1, c2 = v[len(v) // 3], v[2 * len(v) // 3]
        print(line(f"{what} low", [x for x in BUYS if x[ix] < c1]))
        print(line(f"{what} mid", [x for x in BUYS if c1 <= x[ix] < c2]))
        print(line(f"{what} high", [x for x in BUYS if x[ix] >= c2]))
print("DONE")
