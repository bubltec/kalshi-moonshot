"""The price-free binary score, valued by calibration alone (user, 2026-11-20: "we need to value the risk without that" (the ask); the score is 0 to 100, 50 the midpoint, YES and NO from the same moment, NO = 100 minus YES, built from the curve, cushion, time left and the hour's place only, beside Kalshi's price and never compared with it).
Every settled 15-minute market from 11-09, a reading every 60 s from 60 s after the open to 15 s before the close (the settlement window), spot = a coin's Kalshi settlement index or a commodity's Kalshi perp reference. Features: the 14 price-free readings of binaryrisk.side_vector in the YES view (cz, z_tm, left, eff5, vr, amp15, pos15, bounce up/down, h_move, h_pos, h_strike, recent YES rate 1 h and 3 h; no ask). Ridge logistic for P(YES), refit every 6 h on the trailing 72 h, scored on the next 6 h (walk-forward), test from 3 days after the start, two halves. Reported, by market kind (a label: coin vs commodity) and pooled: calibration in ten score bins (n, mean score, the share that settled YES, per half), log loss against the base rate (a score with no skill scores 0 lift), the share of readings with a score outside 40-60, and by seconds left. No price, no profit, no bar. Run: SPOT=0 scripts/cloud_job.sh scripts/binary_score_calibration.py"""
import bisect, math, multiprocessing, time
from datetime import datetime, timezone
from kalshi_moonshot import binaryrisk as br, config, db
from kalshi_moonshot.perpref import index_for_series as pidx
from kalshi_moonshot.kalshiws import index_for_series as crypto_index

t0 = time.time()
s = config.reload()
START = datetime(2026, 11, 9, 5, tzinfo=timezone.utc).timestamp()
st = db.connect(s.db_path, "main", shared=True)
allw = st.execute("SELECT close_ts, result FROM windows WHERE result IN ('yes','no') AND close_ts IS NOT NULL").fetchall()
recent = br.Recent([(float(r["close_ts"]), 1 if r["result"] == "yes" else 0) for r in allw])
wins = st.execute("SELECT ticker, series, close_ts, strike, result FROM windows WHERE result IN ('yes','no') AND close_ts > ? ORDER BY close_ts", (START + 7200,)).fetchall()
jobs = {}
for w in wins:
    ix = crypto_index(w["series"]); kind = "coin" if ix else "commodity"
    if not ix: ix = pidx(w["series"])
    if ix: jobs.setdefault(ix, []).append((w["ticker"], float(w["close_ts"]), float(w["strike"]) if kind == "coin" and w["strike"] else None, w["result"] == "yes", kind))
KEEP = [i for i, n in enumerate(br.NAMES) if n not in ("logit_ask", "ask_off")]
NAMES = [br.NAMES[i] for i in KEEP]
NF = len(KEEP)

def build(job):
    ix, ws = job
    tape = db.connect(config.reload().tape_path, "tape", shared=True)
    tk = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (ix, min(w[1] for w in ws) - 4900, max(w[1] for w in ws) + 5)).fetchall()
    T, V = [float(r[0]) for r in tk], [float(r[1]) for r in tk]; del tk
    out = []
    for ticker, close, strike, yes, kind in ws:
        opened = close - 900.0
        if strike is None:
            k = bisect.bisect_left(T, opened)
            if k >= len(T) or T[k] - opened > 25.0: continue
            strike = V[k]
        t = opened + 60.0
        while t <= close - 15.0:
            f = br.readings(T, V, t, strike, close - t)
            if f:
                v = br.side_vector(f, "yes", 0.5, recent.yes(t, 1.0), recent.yes(t, 3.0))
                out.append((t, kind, [v[i] for i in KEEP], 1.0 if yes else 0.0, close - t, close))
            t += 60.0
    return out

with multiprocessing.get_context("fork").Pool(4) as pool:
    R = [x for part in pool.imap_unordered(build, list(jobs.items())) for x in part]
R.sort(key=lambda r: r[0])
print(f"{len(R)} readings from {len(wins)} markets, {time.time() - t0:.0f}s", flush=True)

def fitlog(rows):
    mu = [sum(r[2][i] for r in rows) / len(rows) for i in range(NF)]
    sd = [math.sqrt(sum((r[2][i] - mu[i]) ** 2 for r in rows) / len(rows)) or 1.0 for i in range(NF)]
    X = [[1.0] + [(r[2][i] - mu[i]) / sd[i] for i in range(NF)] for r in rows]
    y = [r[3] for r in rows]
    w = [0.0] * (NF + 1)
    for _ in range(6):
        A = [[0.0] * (NF + 1) for _ in range(NF + 1)]; b = [0.0] * (NF + 1)
        for x, yy in zip(X, y):
            z = max(-30.0, min(30.0, sum(a * c for a, c in zip(w, x)))); p = 1.0 / (1.0 + math.exp(-z)); wt = max(p * (1 - p), 1e-4)
            for i in range(NF + 1):
                xi = x[i] * wt
                for j in range(i, NF + 1): A[i][j] += xi * x[j]
                b[i] += x[i] * (yy - p)
        for i in range(NF + 1):
            A[i][i] += 1.0 if i else 1e-6
            b[i] -= (1.0 if i else 0.0) * w[i]
            for j in range(i): A[i][j] = A[j][i]
        w = [a + c for a, c in zip(w, br._solve(A, b))]
    return mu, sd, w
def prob(m, x):
    mu, sd, w = m
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, w[0] + sum(w[i + 1] * (x[i] - mu[i]) / sd[i] for i in range(NF))))))

TEST0 = START + 3 * 86400
tmax = R[-1][0]
sc = []  # (t, kind, score 0-100, y, left, base)
h = TEST0; last = None
while h < tmax:
    tr = [r for r in R if h - 72 * 3600 <= r[5] <= h]
    if len(tr) >= 3000:
        base = sum(r[3] for r in tr) / len(tr)
        m = last = fitlog(tr[::3])
        for r in R:
            if h <= r[0] < h + 6 * 3600: sc.append((r[0], r[1], 100.0 * prob(m, r[2]), r[3], r[4], base))
    h += 6 * 3600
print(f"{len(sc)} scored readings")
if last: print("last weights (standardised, YES view): " + " ".join(f"{n} {last[2][i + 1]:+.2f}" for i, n in enumerate(NAMES)))
mid = sorted(r[0] for r in sc)[len(sc) // 2]
def ll(g, use_base):
    e = 1e-6; tot = 0.0
    for r in g:
        p = min(1 - e, max(e, (r[5] if use_base else r[2] / 100.0)))
        tot -= math.log(p if r[3] else 1 - p)
    return tot / len(g)
def table(g, label):
    print(f"\n{label}: n {len(g)} | log loss {ll(g, False):.4f} against the base rate's {ll(g, True):.4f} (lift {ll(g, True) - ll(g, False):+.4f}) | outside 40-60: {sum(1 for r in g if not 40 <= r[2] <= 60) / len(g):.2f}")
    for half in (1, 2):
        gh = [r for r in g if (r[0] < mid) == (half == 1)]
        print(f"  half {half}: n {len(gh)}, log loss lift {ll(gh, True) - ll(gh, False):+.4f}")
        for b in range(10):
            bb = [r for r in gh if b * 10 <= r[2] < (b + 1) * 10 or (b == 9 and r[2] == 100)]
            if len(bb) >= 20: print(f"    score {b * 10:3d}-{b * 10 + 10:3d}: n {len(bb):6d} mean score {sum(r[2] for r in bb) / len(bb):5.1f}  settled YES {100 * sum(r[3] for r in bb) / len(bb):5.1f}")
table(sc, "ALL")
for kind in ("coin", "commodity"): table([r for r in sc if r[1] == kind], kind.upper() + " (a label: the fit is pooled)")
print("\nBY SECONDS LEFT (all kinds): lift and the share settled YES at score >= 80 / <= 20")
for lo, hi in ((0, 120), (120, 300), (300, 600), (600, 900)):
    g = [r for r in sc if lo <= r[4] < hi]
    if g:
        a = [r for r in g if r[2] >= 80]; b = [r for r in g if r[2] <= 20]
        print(f"  {lo:3d}-{hi:3d}s left: n {len(g)} lift {ll(g, True) - ll(g, False):+.4f} | score>=80: n {len(a)} YES {100 * sum(r[3] for r in a) / max(len(a), 1):.1f}% | score<=20: n {len(b)} YES {100 * sum(r[3] for r in b) / max(len(b), 1):.1f}%")
print(f"done in {time.time() - t0:.0f}s")
