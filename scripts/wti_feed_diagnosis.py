"""Which recorded oil feed tracks the WTI contract best? (2026-11-11, user: "Wtis market is not very accurate I wonder if we could find a better number externally or on perpetuals socket")
Candidates found in the tape (every index_id with WTI or CL in its name, discovered below, not picked by hand): KP_WTI (Kalshi perp reference), BQ_WTI (BiQuote CLUSDT perpetual), BQ_WTI_MT5 (broker USOIL), FH_WTI (Finnhub OANDA), and any Hyperliquid or other WTI index the tape holds.
Same question as scripts/copper_feed_diagnosis.py, on the oil contract (KXWTI15M): over the last HOURS of the tape (the end is the newest tick, because a job runs on a backup up to a day old), for each feed
 1. ticks, median gap, share of ticks repeating the previous value, share of 60 s windows with no move at all (a frozen feed);
 2. correlation of the feed's 60 s log move with the 60 s change of the contract's YES mid (one-second grid, every 5 s), overall, inside the CME break (17:00-18:00 ET) and outside it.
One pass, read-only, nothing fitted, nothing chosen after the results.
    scripts/cloud_job.sh scripts/wti_feed_diagnosis.py HOURS=24
"""
import bisect, datetime as dt, math, os, statistics
from kalshi_moonshot import config, db
s = config.reload(); tape = db.connect(s.tape_path, "tape", shared=True)
HOURS = float(os.environ.get("HOURS", "24"))
ET = dt.timezone(dt.timedelta(hours=-5))
names = [r[0] for r in tape.execute("SELECT DISTINCT index_id FROM index_ticks WHERE index_id LIKE '%WTI%' OR index_id LIKE '%CL%' OR index_id LIKE '%OIL%'").fetchall()]
end = max(float(tape.execute("SELECT max(ts) FROM index_ticks WHERE index_id=?", (n,)).fetchone()[0] or 0) for n in names)
start = end - HOURS * 3600
print(f"window {HOURS:g} h ending {dt.datetime.fromtimestamp(end, ET):%m-%d %H:%M} ET; candidate indexes in the tape: {', '.join(sorted(names))}")
def load(ix):
    rows = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND ts<=? AND value>0 ORDER BY ts", (ix, start, end)).fetchall()
    return [float(r[0]) for r in rows], [float(r[1]) for r in rows]
def grid(ts, vs, t0, t1):
    out, j, last = [], 0, None
    for t in range(int(t0), int(t1) + 1):
        while j < len(ts) and ts[j] <= t:
            last = vs[j]; j += 1
        out.append(last)
    return out
def corr(x, y):
    n = min(len(x), len(y))
    if n < 30: return float("nan")
    x, y = x[:n], y[:n]
    mx, my = statistics.mean(x), statistics.mean(y)
    c = sum((a - mx) * (b - my) for a, b in zip(x, y)); vx = sum((a - mx) ** 2 for a in x); vy = sum((b - my) ** 2 for b in y)
    return c / math.sqrt(vx * vy) if vx > 0 and vy > 0 else float("nan")
feeds = {}
print("\nfeed          | ticks | median gap | repeats prev | 60 s windows with no move")
for ix in sorted(names):
    ts, vs = load(ix); feeds[ix] = (ts, vs)
    if len(ts) < 100:
        print(f"{ix:13s} | {len(ts)} ticks"); continue
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    rep = sum(1 for a, b in zip(vs, vs[1:]) if a == b) / (len(vs) - 1)
    g = grid(ts, vs, ts[0], ts[-1])
    flat = sum(1 for i in range(60, len(g), 5) if g[i] == g[i - 60]) / max(1, len(range(60, len(g), 5)))
    print(f"{ix:13s} | {len(ts):5d} | {statistics.median(gaps):5.1f} s    | {rep:5.0%}        | {flat:5.0%}")
q = tape.execute("SELECT ts,ticker,bid,ask FROM quotes WHERE ticker LIKE 'KXWTI%' AND ts>? AND ts<=? AND bid>0 AND ask>0 ORDER BY ticker, ts", (start, end)).fetchall()
by = {}
for ts, tk, bid, ask in q: by.setdefault(tk, []).append((float(ts), (float(bid) + float(ask)) / 2))
print(f"\nWTI contract quotes: {len(by)} markets, {len(q)} rows")
print("correlation of each feed's 60 s move with the 60 s change of the contract's YES mid (n = samples every 5 s)")
print("feed          | all             | outside the break | inside 17:00-18:00 ET")
for ix, (ts, vs) in feeds.items():
    if len(ts) < 100: continue
    X = {"all": ([], []), "out": ([], []), "in": ([], [])}
    for tk, rows in by.items():
        if len(rows) < 300: continue
        t0, t1 = int(rows[0][0]) + 5, int(rows[-1][0]) - 1
        rt = [r[0] for r in rows]; g = grid(ts, vs, t0, t1)
        for i in range(60, len(g), 5):
            t = t0 + i; t_ = t - 60
            j = bisect.bisect_right(rt, t) - 1; j2 = bisect.bisect_right(rt, t_) - 1
            if j < 0 or j2 < 0 or t - rt[j] > 20 or t_ - rt[j2] > 20 or not g[i] or not g[i - 60]: continue
            mv, dc = math.log(g[i] / g[i - 60]), rows[j][1] - rows[j2][1]
            k = "in" if dt.datetime.fromtimestamp(t, ET).hour == 17 else "out"
            for key in ("all", k): X[key][0].append(mv); X[key][1].append(dc)
    cell = lambda k: f"{corr(*X[k]):+.2f} (n={len(X[k][0])})"  # noqa: E731
    print(f"{ix:13s} | {cell('all'):15s} | {cell('out'):17s} | {cell('in')}")
print("DONE")
