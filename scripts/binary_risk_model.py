"""The binary risk model as a BUY-OR-NOT score, built fresh from the tape (2026-11-20).
Question (user, 2026-11-20: "we can't beat Kalshi's price and should not try to. We are trying to build a score that indicates we want to buy or not at any point on the curve in any market we have the history for"; earlier: no old code; the 45-55 range is never a bet; low prices are always risky but can have good conditions; "the goal here is profit, in any time frame from any direction"): at every 30 s of every settled crypto 15-minute market since Monday 11-09, for each side (YES and NO), the score says how much profit buying that side now is expected to make, read from the settlement index's own curve in the market's own typical minutes and from the price paid (the value), never compared with Kalshi's chance.
Readings, turned to the side's view (a NO side flips the sign of the direction readings and swaps the bounce and range places): the cushion in the typical moves left (cz), the cushion in typical minutes (z_tm), minutes left, the 60 s move (d60), the bounce room on the side's way and against it over 5 minutes, the 5-minute path efficiency (eff5), the 15-minute range (amp15) and the spot's place in it (pos15), the 15-minute typical minute against the hour's (vr), the hour's move and the spot's and strike's places in it (h_move, h_pos, h_strike), the recent share of settled windows that resolved YES over 1 h and 3 h (ry1, ry3; from windows closed before the reading; flipped for NO), and the price: logit(ask) and its distance from 0.5 squared.
Profit is judged for every exit a hand trader has, either side: held to settlement, and sold at the bid 1, 2, 3 and 5 minutes later (at settlement if the market is over by then), fee on the buy and on an early sale: net per $1 staked. One ridge regression per exit (standardised readings, ridge 1.0, a constant; the target cut at +3 per $1 so one long shot cannot lead the fit, set before the run), refit every hour on the trailing W hours (W = 12 and 24) and scoring the next hour. The BUY score of a side is its best predicted net over the five exits, and the 1-100 number is its percentile among the training sides. An ask of 0.45-0.55 is never a bet (the user's rule, set not fitted): its score is forced to 0, and what such sides actually net is printed to show why. Two sets: P the price alone (what the price already tells), D the price plus every curve reading; the curve earns its place only where D beats P.
Out of sample, per test hour, a side is a buy at its first reading where its score is in the top 5% or top 10% of that hour's training sides (percent set before the run), one buy per market side, ask not in 0.45-0.55. Reported by the side's ask band (under 0.20, 0.20-0.35, 0.35-0.45, 0.55-0.65, 0.65-0.80, over 0.80): n, win rate, and net per $1 under each of the five exits for the picks beside the same exits for every reading in that band (the baseline a random point has). A score that works shows the picks above the baseline in each band, in both W. No coin or commodity is named in any reading; the series are picked by whether the tape holds their settlement index.
Run: SPOT=0 scripts/cloud_job.sh scripts/binary_risk_model.py"""
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
WINDOWS = (12, 24)
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

def run(job):
    W, name = job; idx = [SIDE.index(n) for n in SETS[name]]
    pick = {top: {} for top in TOPS}; base = [[0, 0.0, [0.0] * 5] for _ in BANDS]; nh = 0
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
            k = band_of(r[4]); b = base[k]; b[0] += 1
            for e in range(5): b[2][e] += r[5][e]
            if 0.45 <= r[4] <= 0.55: continue
            pr, _ = predict(model, idx, r)
            for top in TOPS:
                if pr >= cut[top] and (r[1], r[2]) not in pick[top]: pick[top][(r[1], r[2])] = (k, r[6], r[5])
    out = [f"=== {name}: trailing {W} h, hourly refit, {nh} test hours"]
    for top in TOPS:
        out.append(f"  buys = top {int(top * 100)}% of the hour's training sides by score, first reading per market side, ask not 0.45-0.55; net per $1 for exit hold / 1m / 2m / 3m / 5m (picks | every reading in the band):")
        for k, (lo_, hi_) in enumerate(BANDS):
            g = [v for v in pick[top].values() if v[0] == k]
            b = base[k]
            tag = "  NEVER A BET (user rule); every reading shown" if (lo_, hi_) == (0.45, 0.55) else ""
            if b[0] and (g or tag):
                sel = (f"n {len(g):5d} win {sum(1 for _, w, _ in g if w) / len(g):.2f} " + "/".join(f"{sum(n[e] for _, _, n in g) / len(g):+.2f}" for e in range(5))) if g else "no picks"
                out.append(f"    ask {lo_:.2f}-{min(hi_, 1.0):.2f}: {sel} | all n {b[0]} " + "/".join(f"{b[2][e] / b[0]:+.2f}" for e in range(5)) + tag)
    return "\n".join(out)

jobs = [(W, name) for W in WINDOWS for name in SETS]
with multiprocessing.get_context("fork").Pool(min(4, len(jobs))) as pool:
    for text in pool.imap(run, jobs): print(text, flush=True)
print(f"done in {time.time() - t0:.0f}s")
