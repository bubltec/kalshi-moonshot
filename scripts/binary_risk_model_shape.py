"""The binary risk model's picks split by curve shape (CLAUDE.md rule 4), the follow-up to studies/results/2026-11-20-binary-risk-model-buy-score.txt.
Question (user, 2026-11-20: "Yes" to the split by curve shape): where does the lift of the price-plus-curve picks over a random point live? Same data, same hourly refit (trailing 24 h, set D price + curve), same picks (top 5% and top 10% of the hour's training sides, first reading per market side, ask not 0.45-0.55). Each pick and each reading is put in a tercile of one curve reading, side-oriented: cz the cushion (low: the spot is on the wrong side or close; high: well on the side's side), eff5 the 5-minute path efficiency (low: choppy, high: a straight path), vr the 15-minute typical minute against the hour's (low: calming, high: speeding up), amp15 the 15-minute range. Tercile cutoffs are the 33rd and 67th percentiles of ALL side readings (one set for the whole run; they use the full period, so they are a split, not a rule), chosen before seeing results. Two price groups: asks under 0.45, and over 0.55. For each cell, held to settlement: n of picks, net per $1 of the picks with its standard error, net per $1 of every reading in the cell (the random point), and the lift. A rule needs a positive net and a positive lift in each tercile it claims before it goes to shadow. No coin or commodity is named.
Run: SPOT=0 scripts/cloud_job.sh scripts/binary_risk_model_shape.py"""
import array, bisect, math, multiprocessing, os, time
from datetime import datetime, timezone
from kalshi_moonshot import config, db
from kalshi_moonshot.kalshiws import index_for_series
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
START = datetime(2026, 11, 9, 5, tzinfo=timezone.utc).timestamp()  # Monday 11-09 00:00 ET: the logic rewrite
NAMES = ["cz", "left", "z_tm", "d60", "up5", "dn5", "eff5", "amp15", "pos15", "vr", "h_move", "h_pos", "h_strike", "ry1", "ry3"]
SIDE = ["cz", "z_tm", "left", "d60", "bounce_with", "bounce_against", "eff5", "amp15", "pos15", "vr", "h_move", "h_pos", "h_strike", "ry1", "ry3", "logit_ask", "ask_off"]
SETS = {"P price alone": ["logit_ask", "ask_off"], "D price + curve": SIDE}
WINDOWS = (24,)
TOPS = (0.05, 0.10)
HORIZONS = (60.0, 120.0, 180.0, 300.0)
BANDS = ((0.0, 0.20), (0.20, 0.35), (0.35, 0.45), (0.45, 0.55), (0.55, 0.65), (0.65, 0.80), (0.80, 1.01))
EXITS = ["hold", "1m", "2m", "3m", "5m"]

allw = st.execute("SELECT close_ts, result FROM windows WHERE result IN ('yes','no') AND close_ts IS NOT NULL ORDER BY close_ts").fetchall()
ct = [float(r["close_ts"]) for r in allw]
cum = [0]
for r in allw: cum.append(cum[-1] + (1 if r["result"] == "yes" else 0))
def recent_yes(t, hours):
    lo, hi = bisect.bisect_left(ct, t - hours * 3600), bisect.bisect_left(ct, t)
    return (cum[hi] - cum[lo]) / (hi - lo) - 0.5 if hi - lo >= 20 else 0.0

def curve(T, V, cs, ca, t, strike, left_s):
    """The readings at time t from the index ticks (T times, V values, cs / ca prefix sums of squared and absolute log steps). None when the history is short or stale."""
    j = bisect.bisect_right(T, t) - 1
    if j < 0 or t - T[j] > 10: return None
    i15, i5, i60, i3600 = (bisect.bisect_left(T, t - x) for x in (900, 300, 60, 3600))
    if j - i15 < 300 or j - i3600 < 1500 or T[i15] > t - 840 or T[i3600] > t - 3300: return None
    spot = V[j]
    if spot <= 0 or strike <= 0: return None
    def unit(i):
        span = T[j] - T[i]
        return math.sqrt((cs[j] - cs[i]) / span * 60.0) if span > 0 and cs[j] > cs[i] else None
    u15, u60 = unit(i15), unit(i3600)
    if not u15 or not u60: return None
    w5, w15, wh = V[i5:j + 1], V[i15:j + 1], V[i3600:j + 1]
    path5 = ca[j] - ca[i5]
    lo15, hi15, loh, hih = min(w15), max(w15), min(wh), max(wh)
    lm = max(left_s, 1.0) / 60.0
    lg = math.log(spot / strike)
    cl = lambda x, a: max(-a, min(a, x))
    return {"cz": cl(lg / (u15 * math.sqrt(lm)), 8.0), "left": lm, "z_tm": cl(lg / u15, 30.0), "d60": cl(math.log(spot / V[i60]) / u15, 10.0) if V[i60] > 0 else 0.0,
            "up5": math.log(max(w5) / spot) / u15, "dn5": math.log(spot / min(w5)) / u15, "eff5": abs(math.log(w5[-1] / w5[0])) / path5 if path5 > 0 else 0.0,
            "amp15": math.log(hi15 / lo15) / u15, "pos15": (spot - lo15) / (hi15 - lo15) if hi15 > lo15 else 0.5, "vr": u15 / u60 - 1.0,
            "h_move": cl(math.log(spot / wh[0]) / u15, 60.0), "h_pos": (spot - loh) / (hih - loh) if hih > loh else 0.5, "h_strike": (strike - loh) / (hih - loh) if hih > loh else 0.5}

def quote_at(Q, t):
    k = bisect.bisect_right(Q[0], t) - 1
    return (Q[1][k], Q[2][k]) if k >= 0 and t - Q[0][k] <= 60 else None

t0 = time.time()
wins = st.execute("SELECT ticker, series, close_ts, strike, result FROM windows WHERE result IN ('yes','no') AND strike > 0 AND close_ts > ? ORDER BY close_ts", (START + 3600 * 2,)).fetchall()
by_index = {}
for w in wins:
    ix = index_for_series(w["series"])
    if ix: by_index.setdefault(ix, []).append(w)
rows = []  # (t, ticker, x list, bid, ask, y)
for ix, ws in by_index.items():
    lo, hi = min(float(w["close_ts"]) for w in ws) - 900 - 4000, max(float(w["close_ts"]) for w in ws) + 5
    tk = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (ix, lo, hi)).fetchall()
    T = array.array("d", (float(r[0]) for r in tk)); V = array.array("d", (float(r[1]) for r in tk)); del tk
    cs = array.array("d", [0.0]); ca = array.array("d", [0.0])
    for k in range(1, len(T)):
        d = math.log(V[k] / V[k - 1]) if V[k] > 0 and V[k - 1] > 0 else 0.0
        cs.append(cs[-1] + d * d); ca.append(ca[-1] + abs(d))
    for w in ws:
        close, opened = float(w["close_ts"]), float(w["close_ts"]) - 900.0
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (w["ticker"], opened - 60, close)).fetchall()
        if len(q) < 20: continue
        Q = ([float(r[0]) for r in q], [float(r[1]) for r in q], [float(r[2]) for r in q])
        scale = 100.0 if max(Q[2]) > 1.5 else 1.0
        y = 1.0 if w["result"] == "yes" else 0.0
        t = opened + 60.0
        while t <= close - 15.0:
            qa = quote_at(Q, t); f = curve(T, V, cs, ca, t, float(w["strike"]), close - t) if qa else None
            if f:
                f["ry1"], f["ry3"] = recent_yes(t, 1), recent_yes(t, 3)
                ex = []
                for H in HORIZONS:
                    qh = quote_at(Q, t + H) if t + H <= close - 15.0 else None
                    ex.append((qh[0] / scale, qh[1] / scale) if qh else None)  # None: the market is over by then, settlement pays
                rows.append((t, w["ticker"], [f[n] for n in NAMES], qa[0] / scale, qa[1] / scale, y, ex))
            t += 30.0
    print(f"{ix}: {len(ws)} windows, rows so far {len(rows)}, {time.time() - t0:.0f}s", flush=True)
rows.sort(key=lambda r: r[0])
print(f"{len(rows)} readings from {len(set(r[1] for r in rows))} markets, {datetime.fromtimestamp(rows[0][0], timezone.utc):%m-%d} to {datetime.fromtimestamp(rows[-1][0], timezone.utc):%m-%d}", flush=True)
for n in ("cz", "ry1"):
    k = NAMES.index(n); v = sorted(r[2][k] for r in rows); print(f"  {n}: 1st {v[len(v)//100]:+.3f} median {v[len(v)//2]:+.3f} 99th {v[-len(v)//100]:+.3f}", flush=True)


def fee_sell(x): return fee(x)
def sides_of(r):
    """Both sides of one reading: (t, ticker, side, oriented readings, ask, nets per exit)."""
    t, tk, x, b, a, y, ex = r
    f = dict(zip(NAMES, x)); out = []
    for side, ask, win in (("yes", a, y == 1.0), ("no", 1.0 - b, y == 0.0)):
        if not 0.02 <= ask <= 0.98: continue
        sg = 1.0 if side == "yes" else -1.0
        up, dn = (f["up5"], f["dn5"]) if side == "yes" else (f["dn5"], f["up5"])
        pos = (lambda v: v) if side == "yes" else (lambda v: 1.0 - v)
        o = {"cz": sg * f["cz"], "z_tm": sg * f["z_tm"], "left": f["left"], "d60": sg * f["d60"], "bounce_with": up, "bounce_against": dn, "eff5": f["eff5"], "amp15": f["amp15"], "pos15": pos(f["pos15"]), "vr": f["vr"],
             "h_move": sg * f["h_move"], "h_pos": pos(f["h_pos"]), "h_strike": pos(f["h_strike"]), "ry1": sg * f["ry1"], "ry3": sg * f["ry3"], "logit_ask": math.log(ask / (1 - ask)), "ask_off": 10.0 * (ask - 0.5) ** 2}
        pays = [1.0 if win else 0.0]
        for e in ex:
            if e is None: pays.append(1.0 if win else 0.0)
            else:
                sell = e[0] if side == "yes" else 1.0 - e[1]
                pays.append(sell - fee(sell))
        nets = [(pp - ask - fee(ask)) / ask for pp in pays]
        out.append((t, tk, side, [o[n] for n in SIDE], ask, nets, win))
    return out
S = []
for r in rows: S.extend(sides_of(r))
del rows
S.sort(key=lambda r: r[0])
print(f"{len(S)} side readings", flush=True)
def band_of(a):
    for k, (lo, hi) in enumerate(BANDS):
        if lo <= a < hi: return k
def solve(A, b):
    n = len(b); M = [A[i][:] + [b[i]] for i in range(n)]
    for i in range(n):
        p = max(range(i, n), key=lambda k: abs(M[k][i])); M[i], M[p] = M[p], M[i]
        d = M[i][i] or 1e-12
        for j in range(i, n + 1): M[i][j] /= d
        for k in range(n):
            if k != i:
                f = M[k][i]
                for j in range(i, n + 1): M[k][j] -= f * M[i][j]
    return [M[i][n] for i in range(n)]
def fit(data, idx):
    """One ridge regression per exit, on standardised readings: returns (mu, sd, weights per exit)."""
    mu = [sum(r[3][i] for r in data) / len(data) for i in idx]
    sd = [math.sqrt(sum((r[3][i] - m) ** 2 for r in data) / len(data)) or 1.0 for i, m in zip(idx, mu)]
    K = len(idx) + 1; A = [[0.0] * K for _ in range(K)]; B = [[0.0] * K for _ in EXITS]
    for r in data[::3]:
        v = [1.0] + [(r[3][i] - m) / d for i, m, d in zip(idx, mu, sd)]
        for i in range(K):
            vi = v[i]
            for j in range(i, K): A[i][j] += vi * v[j]
            for e in range(len(EXITS)): B[e][i] += vi * min(3.0, r[5][e])
    for i in range(K):
        A[i][i] += 1.0 if i else 1e-6
        for j in range(i): A[i][j] = A[j][i]
    return mu, sd, [solve(A, B[e]) for e in range(len(EXITS))]
def predict(model, idx, r):
    mu, sd, W = model
    v = [1.0] + [(r[3][i] - m) / d for i, m, d in zip(idx, mu, sd)]
    best = max(range(len(EXITS)), key=lambda e: sum(a * b for a, b in zip(W[e], v)))
    return sum(a * b for a, b in zip(W[best], v)), best

SHAPE = ["cz", "eff5", "vr", "amp15"]
SH_IDX = [SIDE.index(n) for n in SHAPE]
cuts = []
for i in SH_IDX:
    v = sorted(r[3][i] for r in S[::7])
    cuts.append((v[len(v) // 3], v[2 * len(v) // 3]))
def terc(r, k):
    x = r[3][SH_IDX[k]]; lo, hi = cuts[k]
    return 0 if x < lo else 1 if x < hi else 2
def group(a): return 0 if a < 0.45 else 1 if a >= 0.55 else None

def run(job):
    W, name = job; idx = [SIDE.index(n) for n in SETS[name]]
    pick = {top: {} for top in TOPS}
    base = {}  # (shape k, tercile, price group) -> [n, sum, sumsq] of the hold net
    nh = 0
    t = S[0][0] + 24 * 3600; t_end = S[-1][0]
    times = [r[0] for r in S]
    while t + 3600 <= t_end:
        a0, a1, b1 = (bisect.bisect_left(times, x) for x in (t - W * 3600, t, t + 3600))
        tr, te = S[a0:a1], S[a1:b1]
        t += 3600
        if len(tr) < 4000 or len(te) < 600: continue
        model = fit(tr, idx); nh += 1
        ps = sorted(predict(model, idx, r)[0] for r in tr[::4] if not 0.45 <= r[4] <= 0.55)
        cut = {top: ps[int(len(ps) * (1 - top))] for top in TOPS}
        for r in te:
            g = group(r[4])
            if g is None: continue
            h = r[5][0]
            for k in range(len(SHAPE)):
                c = base.setdefault((k, terc(r, k), g), [0, 0.0, 0.0]); c[0] += 1; c[1] += h; c[2] += h * h
            pr, _ = predict(model, idx, r)
            for top in TOPS:
                if pr >= cut[top] and (r[1], r[2]) not in pick[top]: pick[top][(r[1], r[2])] = (g, [terc(r, k) for k in range(len(SHAPE))], h)
    out = [f"=== {name}: trailing {W} h, hourly refit, {nh} test hours; held to settlement, net per $1; terciles of each reading: low / mid / high"]
    for top in TOPS:
        out.append(f"  picks = top {int(top * 100)}% of the hour's training sides:")
        for g, gl in ((0, "ask under 0.45"), (1, "ask over 0.55")):
            for k, sn in enumerate(SHAPE):
                cells = []
                for q in range(3):
                    v = [x[2] for x in pick[top].values() if x[0] == g and x[1][k] == q]
                    b = base.get((k, q, g))
                    if v and b and b[0]:
                        m = sum(v) / len(v); sd = math.sqrt(max(0.0, sum(x * x for x in v) / len(v) - m * m)); bm = b[1] / b[0]
                        cells.append(f"n {len(v):4d} {m:+.3f}+-{sd / math.sqrt(len(v)):.3f} vs {bm:+.3f} lift {m - bm:+.3f}")
                    else: cells.append("n 0")
                out.append(f"    {gl:15s} {sn:6s} " + " | ".join(cells))
    return "\n".join(out)

jobs = [(W, "D price + curve") for W in WINDOWS]
with multiprocessing.get_context("fork").Pool(1) as pool:
    for text in pool.imap(run, jobs): print(text, flush=True)
print(f"done in {time.time() - t0:.0f}s")
