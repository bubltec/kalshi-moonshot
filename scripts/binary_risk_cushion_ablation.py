"""CUSHION ABLATION (2026-11-20, user via parent: "something is still off. We were improving with the cushion feature and now we have reverted."). Forms fixed BEFORE the run. Everything is today's model (readings, ridge, capped prediction) and changes ONLY the cushion treatment:
 Z  last tick against the strike (czt, czlt), no gate (the cushion before commit e3725bf)
 A  three separate cushions on the 1, 3 and 5 minute averages (cz1 cz3 cz5 and their time-left twins), free weights, no gate (the e3725bf form)
 B  the same three, weights constrained non-negative (active-set refit), no gate
 C  one blended cushion (czb, czlb), no gate
 D  blended cushion plus today's gate (every cushion above 0, 180 s left; training rows cut by it)  = today's model
 E  three separate non-negative plus the gate
Same picks: commodity (perp reference, S&P out), asks 0.60-0.85, bar = top 5% / 10% of the training predictions inside the band, hourly refit on the trailing 120 h, first reading per market side held to settlement, net per $1 after the fee, the two halves of the test hours. An hour needs 1,500 training and 100 test rows after the variant's own row cut. Reported per variant and half: n, net, win rate, the mean bar, the mean PREDICTED net of the picks (against the realised net: was the improvement real or only predicted), the share of picks with any cushion below 0, and the lift over every ungated band reading of the same half. Settings were chosen before the results.
Run: SPOT=0 scripts/cloud_job.sh scripts/binary_risk_cushion_ablation.py"""
import array, bisect, math, multiprocessing, time
from datetime import datetime, timezone
from kalshi_moonshot import binaryrisk as br, config, db
from kalshi_moonshot.perpref import index_for_series as perp_index
from kalshi_moonshot.kalshiws import index_for_series as crypto_index
from kalshi_moonshot.strategy import fee_per_contract as fee

t0 = time.time()
BAND = (0.60, 0.85)
START = datetime(2026, 11, 9, 5, tzinfo=timezone.utc).timestamp()
STEP, TOPS, HORIZONS = 60.0, (0.05, 0.10), br.HORIZONS
s = config.reload()
st = db.connect(s.db_path, "main", shared=True)
wins = st.execute("SELECT ticker, series, close_ts, result FROM windows WHERE result IN ('yes','no') AND close_ts > ? ORDER BY close_ts", (START + 7200,)).fetchall()
jobs = {}
for w in wins:
    if w["series"].startswith("KXINX") or crypto_index(w["series"]): continue
    ix = perp_index(w["series"])
    if ix: jobs.setdefault(ix, []).append((w["ticker"], float(w["close_ts"]), w["result"] == "yes"))
print(f"{sum(len(v) for v in jobs.values())} commodity windows on {len(jobs)} feeds", flush=True)
NI = {n: i for i, n in enumerate(br.NAMES)}
DROP = (NI["czb"], NI["czlb"])

def build(job):
    ix, ws = job
    tape = db.connect(config.reload().tape_path, "tape", shared=True)
    lo, hi = min(w[1] for w in ws) - 4900, max(w[1] for w in ws) + 5
    tk = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (ix, lo, hi)).fetchall()
    T, V = [float(r[0]) for r in tk], [float(r[1]) for r in tk]
    del tk
    rows = []
    for ticker, close, won_yes in ws:
        opened = close - 900.0
        k = bisect.bisect_left(T, opened)
        if k >= len(T) or T[k] - opened > 25.0: continue
        strike = V[k]
        ref = []
        for hh in (24.0, 72.0):
            rr = tape.execute("SELECT value FROM index_ticks WHERE index_id=? AND ts<=? AND ts>? ORDER BY ts DESC LIMIT 1", (ix, opened - hh * 3600.0, opened - hh * 3600.0 - 1800.0)).fetchone()
            ref.append(float(rr[0]) if rr else 0.0)
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (ticker, opened - 60, close)).fetchall()
        if len(q) < 20: continue
        qt, qb, qa = [float(r[0]) for r in q], [float(r[1]) for r in q], [float(r[2]) for r in q]
        scale = 100.0 if max(qa) > 1.5 else 1.0
        def quote_at(t):
            j = bisect.bisect_right(qt, t) - 1
            return (qb[j] / scale, qa[j] / scale) if j >= 0 and t - qt[j] <= 60.0 else None
        t = opened + 60.0
        while t <= close - 15.0:
            qq = quote_at(t)
            f = br.readings(T, V, t, strike, close - t, (ref[0], ref[1])) if qq else None
            if f and qq:
                ahead = [quote_at(t + h) if t + h <= close - 15.0 else None for h in HORIZONS]
                for side, ask in (("yes", qq[1]), ("no", 1.0 - qq[0])):
                    if not 0.02 <= ask <= 0.98: continue
                    won = won_yes if side == "yes" else not won_yes
                    pays = [1.0 if won else 0.0]
                    for e in ahead:
                        if e is None: pays.append(1.0 if won else 0.0)
                        else:
                            sell = e[0] if side == "yes" else 1.0 - e[1]
                            pays.append(sell - fee(sell))
                    sg = 1.0 if side == "yes" else -1.0
                    vb = br.side_vector(f, side, ask)
                    base = [v for i, v in enumerate(vb) if i not in DROP]
                    ex = tuple(sg * f[n] for n in ("czb", "czlb", "cz1", "cz3", "cz5", "czl1", "czl3", "czl5", "czt"))
                    ex += (ex[8] * min(f["left"], 15.0) / 15.0,)
                    rows.append((t, base, ex, [(p - ask - fee(ask)) / ask for p in pays], ask, ticker, side, (ex[2], ex[3], ex[4], close - t)))
            t += STEP
    return rows

with multiprocessing.get_context("fork").Pool(4) as pool:
    S = [r for rs in pool.imap_unordered(build, list(jobs.items())) for r in rs]
S.sort(key=lambda r: r[0])
print(f"{len(S)} commodity sides, {time.time() - t0:.0f}s", flush=True)

# extras columns per variant: indexes into ex (czb 0, czlb 1, cz1 2, cz3 3, cz5 4, czl1 5, czl3 6, czl5 7, czt 8, czlt 9)
VAR = {"Z": ((8, 9), False, False), "A": ((2, 3, 4, 5, 6, 7), False, False), "B": ((2, 3, 4, 5, 6, 7), True, False),
       "C": ((0, 1), False, False), "D": ((0, 1), False, True), "E": ((2, 3, 4, 5, 6, 7), True, True)}
RIDGE, CAP = br.RIDGE, br.CAP

def vec(r, cols): return r[1] + [r[2][c] for c in cols]

def fit(rs, cols, nonneg):
    n = len(rs[0][1]) + len(cols)
    X = [vec(r, cols) for r in rs]
    mu = [sum(x[i] for x in X) / len(X) for i in range(n)]
    sd = [math.sqrt(sum((x[i] - mu[i]) ** 2 for x in X) / len(X)) or 1.0 for i in range(n)]
    K = n + 1
    A = [[0.0] * K for _ in range(K)]
    B = [[0.0] * K for _ in br.EXITS]
    for x, r in list(zip(X, rs))[::3]:
        v = [1.0] + [(x[i] - mu[i]) / sd[i] for i in range(n)]
        for i in range(K):
            for j in range(i, K): A[i][j] += v[i] * v[j]
            for e in range(len(br.EXITS)): B[e][i] += v[i] * min(CAP, r[3][e])
    for i in range(K):
        A[i][i] += RIDGE if i else 1e-6
        for j in range(i): A[i][j] = A[j][i]
    cush = set(range(1 + len(rs[0][1]), K)) if nonneg else set()
    W = []
    for e in range(len(br.EXITS)):
        off = set()
        while True:
            keep = [i for i in range(K) if i not in off]
            sol = br._solve([[A[i][j] for j in keep] for i in keep], [B[e][i] for i in keep])
            w = [0.0] * K
            for i, v in zip(keep, sol): w[i] = v
            bad = [i for i in cush if w[i] < 0 and i not in off]
            if not bad: break
            off.update(bad)
        W.append(w)
    return {"mu": mu, "sd": sd, "W": W}

def pred(m, r, cols):
    x = vec(r, cols)
    v = [1.0] + [(a - mu) / d for a, mu, d in zip(x, m["mu"], m["sd"])]
    return min(max(sum(a * b for a, b in zip(w, v)) for w in m["W"]), br.ceiling(r[4]))

times = [r[0] for r in S]
t_start, t_end = S[0][0] + 24 * 3600, S[-1][0]
t_mid = (t_start + t_end) / 2

def ok(r): return min(r[7][0], r[7][1], r[7][2]) > 0.0 and r[7][3] >= 180.0

def hour(task):
    v, t = task
    cols, nonneg, gated = VAR[v]
    a0, a1, b1 = (bisect.bisect_left(times, x) for x in (t - 120 * 3600, t, t + 3600))
    tr = [r for r in S[a0:a1] if (not gated or ok(r))]
    te = [r for r in S[a1:b1] if (not gated or ok(r))]
    if len(tr) < 1500 or len(te) < 100: return v, t, None
    m = fit(tr, cols, nonneg)
    ps = sorted(pred(m, r, cols) for r in tr[::4] if br.usable(r[4]) and BAND[0] <= r[4] < BAND[1])
    if not ps: return v, t, None
    cut = {top: ps[int(len(ps) * (1 - top))] for top in TOPS}
    out = []
    for r in te:
        if not (br.usable(r[4]) and BAND[0] <= r[4] < BAND[1]): continue
        p = pred(m, r, cols)
        for top in TOPS:
            if p >= cut[top]: out.append((top, r[5], r[6], r[3][0], p, min(r[7][:3]) < 0.0, cut[top]))
    wts = {"cz": [round(x, 3) for x in m["W"][0][1 + len(r[1]):]]}
    return v, t, (out, wts, cut)

tasks = [(v, t) for v in VAR for t in range(int(t_start), int(t_end - 3600), 3600)]
picks = {(v, top): {} for v in VAR for top in TOPS}
bars = {(v, top): [] for v in VAR for top in TOPS}
cw = {v: [] for v in VAR}
with multiprocessing.get_context("fork").Pool(4) as pool:
    for v, t, res in pool.imap_unordered(hour, tasks):
        if not res: continue
        out, wts, cut = res
        cw[v].append(wts["cz"])
        half = 0 if t < t_mid else 1
        for top, tk, sd, net, p, neg, _cut in out:
            if (tk, sd) not in picks[(v, top)]: picks[(v, top)][(tk, sd)] = (half, net, p, neg)
        for top in TOPS: bars[(v, top)].append(cut[top])
base = [[], []]
for r in S:
    if BAND[0] <= r[4] < BAND[1] and br.usable(r[4]) and r[0] >= t_start:
        base[0 if r[0] < t_mid else 1].append(r[3][0])
bl = [sum(b) / len(b) for b in base]
print(f"baseline (every ungated band reading, held): half 1 {bl[0]:+.3f} (n {len(base[0])}), half 2 {bl[1]:+.3f} (n {len(base[1])})")
def cell(g, h):
    if not g: return "n    0"
    net = sum(x[1] for x in g) / len(g)
    return "n %4d net %+.3f win %.2f lift %+.3f pred %+.3f neg-cushion %.0f%%" % (len(g), net, sum(1 for x in g if x[1] > 0) / len(g), net - bl[h], sum(x[2] for x in g) / len(g), 100 * sum(1 for x in g if x[3]) / len(g))
for top in TOPS:
    print(f"\n=== top {int(top * 100)}% (commodity, 0.60-0.85, held; net per $1 after the fee)")
    for v in VAR:
        print(f" {v}: bar {sum(bars[(v, top)]) / max(1, len(bars[(v, top)])):+.3f} " + " | ".join(f"half {h + 1}: " + cell([x for x in picks[(v, top)].values() if x[0] == h], h) for h in (0, 1)))
print("\n=== cushion weights (standardised, hold exit) averaged over the refit hours, in the order of each variant's cushion columns")
for v in VAR:
    if cw[v]:
        n = len(cw[v][0])
        print(f" {v}: " + " ".join("%+.3f" % (sum(w[i] for w in cw[v]) / len(cw[v])) for i in range(n)) + f"  ({len(cw[v])} hours)")
print(f"done in {time.time() - t0:.0f}s")
