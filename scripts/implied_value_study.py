"""Our feed's chance, the contract's own price, or a blend: which predicts the result? (2026-11-11, user: "why don't we calculate the value from our spot properties and generate our own curve logic from kalshi data?")
The contract price read backwards through the curve is a distance from the target built from Kalshi data alone: u = Phi^-1(mid) * sqrt(seconds left / 60) typical minutes (the volatility cancels). It follows the settlement source, which our commodity feed does not always do (studies/results/2026-11-11-feed-agreement.txt). It is the market's opinion, so the question is not whether it has an edge but whether a chance made from BOTH readings is better than the one the curve uses now.
Question: across settled 15-minute windows with a spot index (1 in STRIDE, YES side, one reading at each of 600 / 420 / 300 / 180 / 120 / 60 / 30 s left), which has the lower Brier score against the result: P_feed (curvevalue's reading: the cushion of our index over its reference, 900 s horizon rate), the contract's mid, or w * P_feed + (1 - w) * mid? Fixed w 0.75 / 0.5 / 0.25, and walk-forward: each UTC day, per feed kind and time left, the w (0 to 1 by tenths) with the best Brier on all earlier days (at least 300 readings) is used that day.
Feed kind is a property of the reading, not of the market: "settlement" where our index IS the source Kalshi settles on (the CF Benchmarks indexes), "proxy" where it is another provider's price for the same thing (BiQuote). Also: by how far the two disagree (|P_feed - mid|), the share that won against each reading's average, which is what a blend has to get right.
    scripts/cloud_job.sh scripts/implied_value_study.py STRIDE=3
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

os.nice(10)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)

GROUPS = {
    "crypto": (("crypto_multiscale", "crypto_multiscale_underdog"), 300.0, int(os.environ.get("SINCE_CRYPTO", "1794000000"))),
    "commodity_multiscale": (("commodity_multiscale",), 300.0, calendar.timegm((2026, 11, 9, 14, 0, 0))),
    "favorites": (("commodity_energy", "commodity_wti", "commodity_precious", "commodity_industrial"), 120.0, calendar.timegm((2026, 11, 3, 0, 0, 0))),
}
MEM, SEC = 120, 60
BAND = (0.05, 0.89)


def load(lanes, since):
    marks = ",".join("?" * len(lanes))
    rows = st.db.execute(
        "SELECT d.ts,d.ticker,d.lane,d.side,d.contracts,d.stake_usd,d.exit_price,w.close_ts,w.strike,w.result FROM decisions d JOIN windows w ON w.ticker=d.ticker "
        f"WHERE d.action='bet' AND d.mode='trade' AND d.contracts>0 AND d.stake_usd>0 AND d.ts>? AND d.lane IN ({marks}) AND w.result IN ('yes','no') ORDER BY d.ts",
        (since, *lanes),
    ).fetchall()
    seen = {}
    for r in rows:
        seen[(r["ticker"], r["side"])] = r
    return list(seen.values())


def prepare(r):
    """The per-second readings the exit sees from the buy to the close, plus the curve shape at the buy."""
    ser = r["ticker"].split("-")[0]
    crypto = variants.crypto_index(ser)
    ix = crypto or variants.biquote_index(ser)
    if not ix:
        return None
    close, t0 = float(r["close_ts"]), float(r["ts"])
    tk = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts", (ix, t0 - 1500, close)).fetchall()
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (r["ticker"], t0 - 5, close)).fetchall()
    if len(tk) < 1000 or len(q) < 5:
        return None
    T = [float(x[0]) for x in tk]
    V = [float(x[1]) for x in tk]
    if crypto:
        if not r["strike"] or float(r["strike"]) <= 0:
            return None
        ref = math.log(float(r["strike"]))
    else:
        opened = close - 900.0
        a = bisect.bisect_left(T, opened - 5.0)
        if a >= len(T) or T[a] > opened + 20.0:
            return None
        ref = math.log(V[a])
    base = int(T[0])
    g, j, last = [], 0, None
    for t in range(base, int(close) + 1):
        while j < len(T) and T[j] <= t:
            last = V[j]
            j += 1
        g.append(math.log(last) if last else None)
    c60 = [0.0] * len(g)
    for j in range(60, len(g)):
        c60[j] = c60[j - 1] + (((g[j] - g[j - 60]) ** 2) / 60.0 if g[j] is not None and g[j - 60] is not None else 0.0)
    sq = [0.0]
    for x, y in itertools.pairwise(g):
        sq.append(sq[-1] + ((y - x) ** 2 if x is not None and y is not None else 0.0))
    kb = int(t0) - base
    seg = [x for x in g[max(kb - 300, 0) : kb + 1] if x is not None]
    path = sum(abs(y - x) for x, y in itertools.pairwise(seg))
    eff = abs(seg[-1] - seg[0]) / path if len(seg) > 30 and path > 0 else None
    sign = 1.0 if r["side"] == "yes" else -1.0
    qt = [float(x[0]) for x in q]
    moves = collections.deque()
    reads = []
    for t in range(int(t0), int(close) - 2):
        k = t - base
        if k < 960 or k >= len(g) or g[k] is None or g[k - SEC] is None:
            continue
        moves.append((t, (g[k] - g[k - SEC]) * sign))
        while moves and t - moves[0][0] > MEM:
            moves.popleft()
        left = close - t
        if left > 900:
            continue
        rates = (math.sqrt((sq[k] - sq[k - 900]) / 900), math.sqrt((c60[k] - c60[k - 900]) / 900), math.sqrt((c60[k] - c60[k - 180]) / 180))
        if rates[0] <= 0 or rates[1] <= 0 or rates[2] < 0:
            continue
        i = bisect.bisect_right(qt, t) - 1
        if i < 0 or t - qt[i] > 20:
            continue
        yb, ya = float(q[i][1]), float(q[i][2])
        bid = yb if r["side"] == "yes" else 1.0 - ya
        reads.append((t, left, (g[k] - ref) * sign, rates, bid))
    return eff, reads


def phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


import collections
import statistics
import time as _time

STRIDE = int(os.environ.get("STRIDE", "3"))
SINCE_S = int(os.environ.get("SINCE_S", str(calendar.timegm((2026, 10, 28, 0, 0, 0)))))
LEFTS = (600, 420, 300, 180, 120, 60, 30)
nd = statistics.NormalDist()
wins = st.db.execute("SELECT ticker, close_ts, strike, result FROM windows WHERE result IN ('yes','no') AND close_ts > ? ORDER BY close_ts", (SINCE_S,)).fetchall()
S = []  # (day, kind, left, p_feed, mid, won, cushion_feed_tm, cushion_implied_tm)
used = 0
t_start = _time.time()
for k, w in enumerate(wins):
    ser = w["ticker"].split("-")[0]
    crypto = variants.crypto_index(ser)
    if not ser.endswith("15M") or not (crypto or variants.biquote_index(ser)) or k % STRIDE:
        continue
    close = float(w["close_ts"])
    r = {"ticker": w["ticker"], "close_ts": close, "ts": close - 880.0, "side": "yes", "strike": w["strike"], "contracts": 1.0, "stake_usd": 1.0, "result": w["result"]}
    got = prepare(r)
    if not got:
        continue
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (w["ticker"], close - 900, close)).fetchall()
    if len(q) < 5:
        continue
    qt = [float(x[0]) for x in q]
    by_left = {int(round(left)): (c, rates) for t, left, c, rates, bid in got[1]}
    won = 1 if w["result"] == "yes" else 0
    for L in LEFTS:
        cr = by_left.get(L)
        i = bisect.bisect_right(qt, close - L) - 1
        if cr is None or i < 0 or (close - L) - qt[i] > 20 or cr[1][1] <= 0 or cr[1][0] <= 0:
            continue
        c, rates = cr
        mid = (float(q[i][1]) + float(q[i][2])) / 2.0
        pf = phi(c / (rates[1] * math.sqrt(L)))
        S.append((int(close // 86400), "settlement" if crypto else "proxy", L, pf, mid, won, c / (rates[0] * math.sqrt(60.0)), nd.inv_cdf(min(max(mid, 0.01), 0.99)) * math.sqrt(L / 60.0)))
    used += 1
    if used % 500 == 0:
        print(f"  {used} windows, {len(S)} readings, {_time.time() - t_start:.0f}s", flush=True)
print(f"windows {used} (1 in {STRIDE}), readings {len(S)}")


def brier(rows, f):
    return sum((f(x) - x[5]) ** 2 for x in rows) / len(rows) if rows else float("nan")


for kind in ("settlement", "proxy"):
    K = [x for x in S if x[1] == kind]
    if len(K) < 500:
        print(f"\n=== feed kind {kind}: {len(K)} readings, too few")
        continue
    print(f"\n=== feed kind {kind}: {len(K)} readings | mean |feed cushion - implied cushion| {sum(abs(x[6] - x[7]) for x in K) / len(K):.2f} typical minutes | sign of the two cushions agrees on {sum(1 for x in K if (x[6] >= 0) == (x[7] >= 0)) / len(K):.1%}")
    print("  Brier score by time left (lower is better): feed | mid | blend w 0.75 | 0.5 | 0.25 | walk-forward blend (and the weights it used)")
    days = sorted({x[0] for x in K})
    for L in LEFTS:
        R = [x for x in K if x[2] == L]
        if len(R) < 200:
            continue
        oos = []
        ws = collections.Counter()
        for d in days:
            past = [x for x in R if x[0] < d]
            today = [x for x in R if x[0] == d]
            if len(past) < 300 or not today:
                continue
            best = min((i / 10.0 for i in range(11)), key=lambda wv: brier(past, lambda x: wv * x[3] + (1 - wv) * x[4]))
            ws[best] += 1
            oos += [(x, best) for x in today]
        base = [x for x, _ in oos]
        wf = sum((wv * x[3] + (1 - wv) * x[4] - x[5]) ** 2 for x, wv in oos) / len(oos) if oos else float("nan")
        print(f"    {L:3d}s left, n {len(R):5d}: {brier(R, lambda x: x[3]):.4f} | {brier(R, lambda x: x[4]):.4f} | {brier(R, lambda x: 0.75 * x[3] + 0.25 * x[4]):.4f} | {brier(R, lambda x: 0.5 * x[3] + 0.5 * x[4]):.4f} | {brier(R, lambda x: 0.25 * x[3] + 0.75 * x[4]):.4f} | walk-forward on {len(base)}: {wf:.4f} against feed {brier(base, lambda x: x[3]):.4f}, mid {brier(base, lambda x: x[4]):.4f} | weights on the feed: {dict(sorted(ws.items()))}")
    print("  where the two disagree (all times left pooled): n | share that won | average feed chance | average mid | the weight on the feed that fits the outcome")
    for lo, hi, lab in ((0.0, 0.05, "within 0.05"), (0.05, 0.10, "0.05 to 0.10"), (0.10, 0.20, "0.10 to 0.20"), (0.20, 0.30, "0.20 to 0.30"), (0.30, 2.0, "0.30 and up")):
        for sgn, slab in ((1, "feed higher"), (-1, "feed lower")):
            R = [x for x in K if lo <= abs(x[3] - x[4]) < hi and ((x[3] - x[4]) >= 0) == (sgn > 0)]
            if len(R) < 40:
                continue
            wr = sum(x[5] for x in R) / len(R)
            af = sum(x[3] for x in R) / len(R)
            am = sum(x[4] for x in R) / len(R)
            fit = (wr - am) / (af - am) if abs(af - am) > 1e-6 else float("nan")
            print(f"    {lab:13s} {slab:11s}: n {len(R):5d} | won {wr:.3f} | feed {af:.3f} | mid {am:.3f} | fitted weight on the feed {fit:+.2f}")
print("DONE")
