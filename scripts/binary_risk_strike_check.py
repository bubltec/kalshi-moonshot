"""IS OUR CUSHION MEASURED AGAINST THE RIGHT STRIKE? (2026-11-21, found comparing winners with losers: on 384 of 2,688 replayed buys priced 0.60-0.85 the cushion put the spot on the WRONG side of the strike, and those bets still won 69%.)
On a commodity market binaryrisk reads the cushion against the perp reference's first tick at the open (open_strike). Kalshi's own strike is in windows.strike (floor_strike, from the stream).
QUESTION: on the same commodity markets, read at minutes 5, 10, 12 and 14, which strike gives a cushion that agrees with (a) the result and (b) Kalshi's own price at that moment: our open tick or Kalshi's strike? Also how far apart the two are, in the market's typical minutes.
Reported: the gap; for each strike the share of readings where the cushion's side is the side that won, the same on the readings where the two strikes DISAGREE on the side, the share where the cushion's side is the side Kalshi prices over 0.50, and the rank separation (AUC) of the YES result by the blended cushion. No price in any reading.
Run: SPOT=0 scripts/cloud_job.sh scripts/binary_risk_strike_check.py"""
import bisect, math, multiprocessing, statistics
from datetime import datetime, timezone
from kalshi_moonshot import binaryrisk as br, config, db
from kalshi_moonshot.perpref import index_for_series as perp_index

START = datetime(2026, 11, 9, 5, tzinfo=timezone.utc).timestamp()
s = config.reload()
st = db.connect(s.db_path, "main", shared=True)
jobs = {}
for w in st.execute("SELECT ticker, series, close_ts, strike, result, settled_value FROM windows WHERE result IN ('yes','no') AND close_ts > ? AND strike > 0 ORDER BY close_ts", (START + 7200,)).fetchall():
    if w["series"].startswith("KXINX"): continue
    ix = perp_index(w["series"])
    if ix: jobs.setdefault(ix, []).append((w["ticker"], float(w["close_ts"]), float(w["strike"]), w["result"] == "yes", w["settled_value"]))


def build(job):
    ix, ws = job
    tape = db.connect(config.reload().tape_path, "tape", shared=True)
    tk = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (ix, min(w[1] for w in ws) - 4900, max(w[1] for w in ws) + 5)).fetchall()
    T, V = [float(r[0]) for r in tk], [float(r[1]) for r in tk]
    out = []
    for ticker, close, kstrike, won_yes, settled in ws:
        opened = close - 900.0
        k = bisect.bisect_left(T, opened)
        if k >= len(T) or T[k] - opened > 25.0: continue
        ours = V[k]
        kc = bisect.bisect_right(T, close) - 1
        last = V[kc] if kc >= 0 and close - T[kc] < 30 else None
        for minute in (5, 10, 12, 14):
            t = opened + 60.0 * minute
            fo, fk = br.readings(T, V, t, ours, close - t), br.readings(T, V, t, kstrike, close - t)
            if fo is None or fk is None: continue
            q = tape.execute("SELECT bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts DESC LIMIT 1", (ticker, t - 10, t)).fetchone()
            mid = None
            if q:
                sc = 100.0 if float(q[1]) > 1.5 else 1.0
                mid = (float(q[0]) + float(q[1])) / (2 * sc)
            u = abs(fo["czb"] - fk["czb"])  # the gap between the strikes, in the cushion's own units at this moment
            out.append((ix, minute, fo["czb"], fk["czb"], fo["cz1"], fk["cz1"], won_yes, mid, u, math.log(ours / kstrike), None if last is None or not settled else math.log(last / float(settled)), None if not settled else math.log(float(settled) / kstrike)))
    return out


R = []
with multiprocessing.get_context("fork").Pool(4) as pool:
    for o in pool.imap_unordered(build, list(jobs.items())): R.extend(o)


def auc(pos, neg):
    xs = sorted([(v, 1) for v in pos] + [(v, 0) for v in neg]); n = len(xs); i = 0; s_ = 0.0
    while i < n:
        j = i
        while j < n and xs[j][0] == xs[i][0]: j += 1
        s_ += sum(x[1] for x in xs[i:j]) * (i + j + 1) / 2.0
        i = j
    return (s_ - len(pos) * (len(pos) + 1) / 2.0) / max(1, len(pos) * len(neg))


print(f"{len(R)} readings on {len({r[0] for r in R})} perp references")
gl = sorted(abs(r[9]) for r in R if r[1] == 10)
print("our open tick against Kalshi's strike, relative gap: median %.2e, 90th pct %.2e; in the cushion's units at minute 10: median %.2f, 90th pct %.2f" % (gl[len(gl) // 2], gl[int(len(gl) * .9)], statistics.median(r[8] for r in R if r[1] == 10), sorted(r[8] for r in R if r[1] == 10)[int(len(gl) * .9)]))
b = [r[10] for r in R if r[1] == 10 and r[10] is not None]
if b: print("the perp's last tick against Kalshi's settled value (log): median %+.2e, median size %.2e, n %d (a steady offset = the perp is not the settlement source)" % (statistics.median(b), statistics.median(abs(x) for x in b), len(b)))
for ix in sorted({r[0] for r in R}):
    g = [r for r in R if r[0] == ix and r[1] == 10]
    bb = [r[10] for r in g if r[10] is not None]
    print("  %-14s n %4d  open tick - Kalshi strike median %+.2e (size %.2e)  last tick - settled median %s" % (ix, len(g), statistics.median(r[9] for r in g), statistics.median(abs(r[9]) for r in g), "%+.2e (size %.2e)" % (statistics.median(bb), statistics.median(abs(x) for x in bb)) if bb else "n/a"))
for minute in (5, 10, 12, 14):
    g = [r for r in R if r[1] == minute]
    dis = [r for r in g if (r[2] > 0) != (r[3] > 0)]
    gm = [r for r in g if r[7] is not None and abs(r[7] - 0.5) > 0.02]
    print(f"\nminute {minute}: {len(g)} readings; the two strikes put the spot on DIFFERENT sides on {len(dis)} ({100.0 * len(dis) / max(1, len(g)):.0f}%)")
    for name, i in (("our open tick ", 2), ("Kalshi's strike", 3)):
        print("  %s: cushion's side won %4.1f%% | where they disagree %4.1f%% | cushion's side is Kalshi's favourite %4.1f%% | separation of the result %.3f" % (
            name, 100.0 * sum(1 for r in g if (r[i] > 0) == r[6]) / len(g), 100.0 * sum(1 for r in dis if (r[i] > 0) == r[6]) / max(1, len(dis)),
            100.0 * sum(1 for r in gm if (r[i] > 0) == (r[7] > 0.5)) / max(1, len(gm)), auc([r[i] for r in g if r[6]], [r[i] for r in g if not r[6]])))
