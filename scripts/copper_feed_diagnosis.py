"""Why does the copper curve not work? (2026-11-11, user: "why doesn't copper work?")
The Kalshi perp reference (KP_COPPER) and the external feed (BQ_COPPER) correlate +0.47 on 60 s moves over three hours where gold, oil and gas correlate +0.9 or more. Questions, over the last HOURS (default 6), with gold as the control:
 1. each feed on its own: ticks, median gap between ticks, share of ticks that repeat the previous value (stale), the smallest price step (the tick size) in basis points, and the typical 60 s move in basis points;
 2. is one feed late: the correlation of the two feeds' 60 s moves with one shifted by -120..+120 s;
 3. which feed agrees with the copper contract itself: correlation of each feed's 60 s move with the 60 s change of the contract's YES mid over the markets of the period (the contract is what settles);
 4. how much of each feed's moves are at the tick size.
One pass, read-only, nothing fitted.
    scripts/cloud.sh run scripts/copper_feed_diagnosis.py [HOURS=6]
"""
import bisect, math, os, re, statistics, time, datetime as dt
from kalshi_moonshot import config, db
s = config.reload(); tape = db.connect(s.tape_path, "tape", shared=True)
HOURS = float(os.environ.get("HOURS", "6")); now = time.time(); start = now - HOURS * 3600
def load(ix):
    rows = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (ix, start)).fetchall()
    return [float(r[0]) for r in rows], [float(r[1]) for r in rows]
def grid(ts, vs, t0, t1):
    out, j, last = [], 0, None
    for t in range(int(t0), int(t1) + 1):
        while j < len(ts) and ts[j] <= t:
            last = vs[j]; j += 1
        out.append(last)
    return out
def corr(x, y):
    n = min(len(x), len(y)); x, y = x[:n], y[:n]
    mx, my = statistics.mean(x), statistics.mean(y)
    c = sum((a - mx) * (b - my) for a, b in zip(x, y)); vx = sum((a - mx) ** 2 for a in x); vy = sum((b - my) ** 2 for b in y)
    return c / math.sqrt(vx * vy) if vx > 0 and vy > 0 else float("nan")
def moves(g, k=60):
    return [math.log(g[i] / g[i - k]) if g[i] and g[i - k] else None for i in range(k, len(g))]
print(f"window {HOURS:g} h")
print("feed          | ticks | median gap | repeats prev value | smallest step (bp) | typical 60 s move (bp, rms)")
feeds = {}
for name, ix in (("KP_COPPER", "KP_COPPER"), ("BQ_COPPER", "BQ_COPPER"), ("KP_GOLD", "KP_GOLD"), ("BQ_GOLD", "BQ_GOLD")):
    ts, vs = load(ix); feeds[name] = (ts, vs)
    if len(ts) < 100:
        print(f"{name:13s} | {len(ts)} ticks"); continue
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    rep = sum(1 for a, b in zip(vs, vs[1:]) if a == b) / (len(vs) - 1)
    steps = sorted({abs(math.log(b / a)) for a, b in zip(vs, vs[1:]) if a != b})
    g = grid(ts, vs, ts[0], ts[-1]); mv = [m for m in moves(g) if m is not None]
    rms = math.sqrt(sum(m * m for m in mv) / len(mv)) * 1e4 if mv else 0
    print(f"{name:13s} | {len(ts):5d} | {statistics.median(gaps):5.1f} s    | {rep:6.0%}             | {steps[0] * 1e4 if steps else 0:6.2f}             | {rms:6.2f}")
print("\nlag check: correlation of the two feeds' 60 s moves, KP shifted by k seconds (positive = KP later)")
for label, a, b in (("copper", "KP_COPPER", "BQ_COPPER"), ("gold  ", "KP_GOLD", "BQ_GOLD")):
    ta, va = feeds[a]; tb, vb = feeds[b]
    if len(ta) < 100 or len(tb) < 100: continue
    t0, t1 = max(ta[0], tb[0]) + 200, min(ta[-1], tb[-1]) - 200
    ga, gb = grid(ta, va, t0 - 200, t1 + 200), grid(tb, vb, t0 - 200, t1 + 200)
    ma, mb = moves(ga), moves(gb)
    out = []
    for k in (-120, -60, -30, -10, 0, 10, 30, 60, 120):
        pairs = [(ma[i + k], mb[i]) for i in range(max(0, -k), min(len(ma), len(mb)) - max(0, k)) if ma[i + k] is not None and mb[i] is not None]
        out.append(f"{k:+d}s {corr([p[0] for p in pairs], [p[1] for p in pairs]):+.2f}")
    print(f"  {label}: " + "  ".join(out))
print("\nagainst the contract itself: correlation of each feed's 60 s move with the 60 s change of KXCOPPER15M / KXGOLD15M YES mid (one-second grid, all windows of the period)")
MON = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}
for label, ser, a, b in (("copper", "KXCOPPER15M", "KP_COPPER", "BQ_COPPER"), ("gold  ", "KXGOLD15M", "KP_GOLD", "BQ_GOLD")):
    q = tape.execute("SELECT ts,ticker,bid,ask FROM quotes WHERE ticker LIKE ? AND ts>? AND bid>0 AND ask>0 ORDER BY ticker, ts", (ser + "-%", start)).fetchall()
    by = {}
    for ts, tk, bid, ask in q: by.setdefault(tk, []).append((float(ts), (float(bid) + float(ask)) / 2))
    ta, va = feeds[a]; tb, vb = feeds[b]
    xa, xb, xc = [], [], []
    for tk, rows in by.items():
        if len(rows) < 300: continue
        t0, t1 = int(rows[0][0]) + 5, int(rows[-1][0]) - 1
        rt = [r[0] for r in rows]
        ga, gb = grid(ta, va, t0, t1), grid(tb, vb, t0, t1)
        for i in range(60, len(ga), 5):
            t = t0 + i; t_ = t - 60
            j = bisect.bisect_right(rt, t) - 1; j2 = bisect.bisect_right(rt, t_) - 1
            if j < 0 or j2 < 0 or t - rt[j] > 20 or t_ - rt[j2] > 20 or not ga[i] or not ga[i - 60] or not gb[i] or not gb[i - 60]: continue
            xa.append(math.log(ga[i] / ga[i - 60])); xb.append(math.log(gb[i] / gb[i - 60])); xc.append(rows[j][1] - rows[j2][1])
    if len(xc) > 100:
        print(f"  {label}: n={len(xc)}  perp reference vs contract {corr(xa, xc):+.2f}   external feed vs contract {corr(xb, xc):+.2f}")
print("DONE")
