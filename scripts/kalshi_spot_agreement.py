"""Is the Kalshi-first spot (the perp reference price) giving the same curve readings as the external feed? (2026-11-11, user: "Is the new Kalshi curve logic working?")
Question: over the last SINCE_HOURS (default 3) for each market with both a Kalshi perp reference (KP_*) and an external BiQuote feed (BQ_*), on a one-second grid (forward-filled): (1) do their 60-second moves agree (correlation of the
60 s log moves, each in its own typical minutes); (2) when the wave rule is read every minute from each feed (swing >= 2 typical minutes over 3 minutes, bet side in the lower half, low >= 60 s ago, back >= 0.25), how often do
the two feeds give the same answer, for the YES side and the NO side; (3) how stale is each feed (median seconds between ticks). Read-only; one pass; no setting is fitted.
    scripts/cloud.sh run scripts/kalshi_spot_agreement.py [HOURS=3]
"""
import bisect, math, os, statistics, time
from kalshi_moonshot import config, db, variants
s = config.reload(); tape = db.connect(s.tape_path, "tape", shared=True)
HOURS = float(os.environ.get("HOURS", "3")); now = time.time(); start = now - HOURS * 3600
PAIRS = [("S&P 500", "KP_SPX", "BQ_SPX"), ("gold", "KP_GOLD", "BQ_GOLD"), ("silver", "KP_SILVER", "BQ_SILVER"), ("platinum", "KP_PLATINUM", "BQ_PLATINUM"), ("WTI", "KP_WTI", "BQ_WTI"),
         ("Brent", "KP_BRENT", "BQ_BRENT"), ("copper", "KP_COPPER", "BQ_COPPER"), ("natgas", "KP_NATGAS", "BQ_NATGAS")]
def load(ix):
    rows = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (ix, start - 400)).fetchall()
    return [float(r[0]) for r in rows], [float(r[1]) for r in rows]
def grid(ts, vs, t0, t1):
    out, j, last = [], 0, None
    for t in range(int(t0), int(t1) + 1):
        while j < len(ts) and ts[j] <= t:
            last = vs[j]; j += 1
        out.append(last)
    return out
def wave_ok(ts, vs, t, side):
    f = variants.wave_features(ts, vs, side, t)
    if f is None: return None
    return f["amp"] >= 2.0 and f["pos"] <= 0.5 and f["since_low"] >= 60 and f["back"] >= 0.25
print(f"window {HOURS:g} h ending {time.strftime('%H:%M', time.localtime(now))} local; per pair: ticks median gap | correlation of 60 s moves | wave rule read each minute: same answer YES / NO (n) | passes by KP vs by BQ")
for name, kp, bq in PAIRS:
    a, b = load(kp), load(bq)
    if len(a[0]) < 600 or len(b[0]) < 600:
        print(f"{name:9s} too few ticks ({len(a[0])} / {len(b[0])})"); continue
    gap = lambda t: statistics.median([y - x for x, y in zip(t, t[1:])]) if len(t) > 1 else 0.0
    t0, t1 = max(a[0][0], b[0][0], start), min(a[0][-1], b[0][-1], now)
    ga, gb = grid(a[0], a[1], t0, t1), grid(b[0], b[1], t0, t1)
    ra = [math.log(ga[i] / ga[i - 60]) for i in range(60, len(ga)) if ga[i] and ga[i - 60] and gb[i] and gb[i - 60]]
    rb = [math.log(gb[i] / gb[i - 60]) for i in range(60, len(ga)) if ga[i] and ga[i - 60] and gb[i] and gb[i - 60]]
    ma, mb = statistics.mean(ra), statistics.mean(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb)); va = sum((x - ma) ** 2 for x in ra); vb = sum((y - mb) ** 2 for y in rb)
    corr = cov / math.sqrt(va * vb) if va > 0 and vb > 0 else float("nan")
    res = {"yes": [0, 0, 0, 0], "no": [0, 0, 0, 0]}
    for t in range(int(t0) + 400, int(t1), 60):
        for side in ("yes", "no"):
            x, y = wave_ok(a[0], a[1], t, side), wave_ok(b[0], b[1], t, side)
            if x is None or y is None: continue
            r = res[side]; r[0] += 1; r[1] += (x == y); r[2] += bool(x); r[3] += bool(y)
    f = lambda side: f"{res[side][1] / res[side][0]:.0%} ({res[side][0]})" if res[side][0] else "n/a"
    print(f"{name:9s} | KP {gap(a[0]):.1f} s, BQ {gap(b[0]):.1f} s | corr {corr:+.2f} | same {f('yes')} / {f('no')} | passes KP {res['yes'][2] + res['no'][2]} BQ {res['yes'][3] + res['no'][3]}")
print("DONE")
