"""WINDOW STUDY (2026-11-20, user via parent: "I look at the 15 minute and 60 minute look back, and only about the last 30 minutes ... the time frames matter, so shuffle them"). The sets are fixed BEFORE the run, one family at a time, everything else as today:
 (1) lookback windows of the position, move and wave-shape readings: look=15,30,60 (today) | 15,60 | 15,30 | 30 | 5,15,30 | 10,30,60
 (2) the unit window u (the typical minute from the last u minutes of one-second moves): u=5 | 10 | 15 (today) | 30; and the hour in vr (uh): 30 | 60 (today) | 120 minutes
 (3) the cushion averages: avgs=1,3,5 (today) | 2,5,10 | 1,5,15 | 3,10,30 (the trend readings use the first three in order)
 then one combined run of the best set of each family (named after the results, so chosen after seeing them).
One config per run (CFG=<name>, or LOOK=, U=, UH=, AVGS= for the combination). The model is today's: the blended cushion, the gate (every cushion above 0 and 180 s left), ridge fit per exit, hourly refit on the trailing 120 h, asks 0.60-0.85, bar = top 5%/10% of the training predictions in the band, first reading per market side held to settlement, commodity perp reference, S&P out, two halves of the test hours. Eligibility of a reading is the same for every set (60 minutes of history needed, as today). Every pick is printed (PICK lines) so sets can be compared with a standard error; the summary gives n, net per $1 after the fee, win rate, the mean bar, and the result by time left at the pick (3-6, 6-10, 10-15 minutes).
Run: SPOT=0 scripts/cloud_job.sh scripts/binary_risk_window_study.py CFG=<name>"""
import bisect, math, multiprocessing, os, time
from datetime import datetime, timezone
from kalshi_moonshot import binaryrisk as br, config, db
from kalshi_moonshot.perpref import index_for_series as perp_index
from kalshi_moonshot.kalshiws import index_for_series as crypto_index
from kalshi_moonshot.strategy import fee_per_contract as fee

CFGS = {
    "base": dict(look=(15, 30, 60), u_min=15, uh_min=60, avgs=(1, 3, 5)),
    "look_15_60": dict(look=(15, 60)), "look_15_30": dict(look=(15, 30)), "look_30": dict(look=(30,)), "look_5_15_30": dict(look=(5, 15, 30)), "look_10_30_60": dict(look=(10, 30, 60)),
    "u5": dict(u_min=5), "u10": dict(u_min=10), "u30": dict(u_min=30),
    "uh30": dict(uh_min=30), "uh120": dict(uh_min=120),
    "avg_2_5_10": dict(avgs=(2, 5, 10)), "avg_1_5_15": dict(avgs=(1, 5, 15)), "avg_3_10_30": dict(avgs=(3, 10, 30)),
}
name = os.environ.get("CFG", "base")
cfg = dict(CFGS["base"])
if name == "combo":
    ints = lambda k: tuple(int(x) for x in os.environ[k].split(","))
    cfg.update(look=ints("LOOK"), u_min=int(os.environ["U"]), uh_min=int(os.environ["UH"]), avgs=ints("AVGS"))
else:
    cfg.update(CFGS[name])
LOOK, AVGS = cfg["look"], cfg["avgs"]
print(f"CONFIG {name}: {cfg}", flush=True)
t0 = time.time()
BAND, TOPS, STEP, HORIZONS = (0.60, 0.85), (0.05, 0.10), 60.0, br.HORIZONS
START = datetime(2026, 11, 9, 5, tzinfo=timezone.utc).timestamp()
s = config.reload()
st = db.connect(s.db_path, "main", shared=True)
wins = st.execute("SELECT ticker, series, close_ts, result FROM windows WHERE result IN ('yes','no') AND close_ts > ? ORDER BY close_ts", (START + 7200,)).fetchall()
jobs = {}
for w in wins:
    if w["series"].startswith("KXINX") or crypto_index(w["series"]): continue
    ix = perp_index(w["series"])
    if ix: jobs.setdefault(ix, []).append((w["ticker"], float(w["close_ts"]), w["result"] == "yes"))
NEED = max(cfg["uh_min"], 60, max(LOOK), max(AVGS), cfg["u_min"]) * 60.0 + 900.0 + 300.0

def vector(f, side, ask):
    ask = min(0.98, max(0.02, ask))
    yes = side == "yes"
    sg = 1.0 if yes else -1.0
    v = [math.log(ask / (1.0 - ask)), 10.0 * (ask - 0.5) ** 2, sg * f["czb"], sg * f["tr13"], sg * f["tr35"], f["left"], sg * f["czlb"], f["vr"], f["amp15"]]
    for m in LOOK:
        v += [f[f"pos{m}"] if yes else 1.0 - f[f"pos{m}"], sg * f[f"mv{m}"], sg * f[f"sl{m}"], sg * f[f"cv{m}"], f[f"ft{m}"]]
    return v + [sg * f["dr24"], sg * f["dr72"]]

def build(job):
    ix, ws = job
    tape = db.connect(config.reload().tape_path, "tape", shared=True)
    tk = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (ix, min(w[1] for w in ws) - NEED - 900, max(w[1] for w in ws) + 5)).fetchall()
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
            f = br.readings(T, V, t, strike, close - t, (ref[0], ref[1]), cfg) if qq else None
            if f and qq:
                ahead = [quote_at(t + h) if t + h <= close - 15.0 else None for h in HORIZONS]
                for side, ask in (("yes", qq[1]), ("no", 1.0 - qq[0])):
                    if not 0.02 <= ask <= 0.98: continue
                    sg = 1.0 if side == "yes" else -1.0
                    if min(sg * f[f"cz{m}"] for m in AVGS) <= 0.0 or close - t < 180.0: continue  # today's gate
                    won = won_yes if side == "yes" else not won_yes
                    pays = [1.0 if won else 0.0]
                    for e in ahead:
                        if e is None: pays.append(1.0 if won else 0.0)
                        else:
                            sell = e[0] if side == "yes" else 1.0 - e[1]
                            pays.append(sell - fee(sell))
                    rows.append((t, vector(f, side, ask), [(p - ask - fee(ask)) / ask for p in pays], ask, ticker, side, close - t))
            t += STEP
    return rows

with multiprocessing.get_context("fork").Pool(4) as pool:
    S = [r for rs in pool.imap_unordered(build, list(jobs.items())) for r in rs]
S.sort(key=lambda r: r[0])
print(f"{len(S)} gated sides, {time.time() - t0:.0f}s", flush=True)

def fit(rs):
    n = len(rs[0][1])
    mu = [sum(r[1][i] for r in rs) / len(rs) for i in range(n)]
    sd = [math.sqrt(sum((r[1][i] - mu[i]) ** 2 for r in rs) / len(rs)) or 1.0 for i in range(n)]
    K = n + 1
    A = [[0.0] * K for _ in range(K)]
    B = [[0.0] * K for _ in br.EXITS]
    for r in rs[::3]:
        v = [1.0] + [(r[1][i] - mu[i]) / sd[i] for i in range(n)]
        for i in range(K):
            for j in range(i, K): A[i][j] += v[i] * v[j]
            for e in range(len(br.EXITS)): B[e][i] += v[i] * min(br.CAP, r[2][e])
    for i in range(K):
        A[i][i] += br.RIDGE if i else 1e-6
        for j in range(i): A[i][j] = A[j][i]
    return mu, sd, [br._solve(A, B[e]) for e in range(len(br.EXITS))]

def pred(m, r):
    mu, sd, W = m
    v = [1.0] + [(a - u) / d for a, u, d in zip(r[1], mu, sd)]
    return min(max(sum(a * b for a, b in zip(w, v)) for w in W), br.ceiling(r[3]))

times = [r[0] for r in S]
t_start, t_end = S[0][0] + 24 * 3600, S[-1][0]
t_mid = (t_start + t_end) / 2
def hour(t):
    a0, a1, b1 = (bisect.bisect_left(times, x) for x in (t - 120 * 3600, t, t + 3600))
    tr, te = S[a0:a1], S[a1:b1]
    if len(tr) < 1500 or len(te) < 100: return None
    m = fit(tr)
    ps = sorted(pred(m, r) for r in tr[::4] if br.usable(r[3]) and BAND[0] <= r[3] < BAND[1])
    if not ps: return None
    cut = {top: ps[int(len(ps) * (1 - top))] for top in TOPS}
    out = []
    for r in te:
        if not (br.usable(r[3]) and BAND[0] <= r[3] < BAND[1]): continue
        p = pred(m, r)
        for top in TOPS:
            if p >= cut[top]: out.append((top, r[4], r[5], r[2][0], r[6], 0 if t < t_mid else 1))
    return t, cut, out

picks = {top: {} for top in TOPS}
bars = {top: [] for top in TOPS}
with multiprocessing.get_context("fork").Pool(4) as pool:
    for res in pool.imap_unordered(hour, range(int(t_start), int(t_end - 3600), 3600)):
        if not res: continue
        t, cut, out = res
        for top in TOPS: bars[top].append(cut[top])
        for top, tk, sd, net, left, half in out:
            picks[top].setdefault((tk, sd), (half, net, left))
for top in TOPS:
    for (tk, sd), (half, net, left) in picks[top].items():
        print(f"PICK {name} {int(top * 100)} {half + 1} {net:.4f} {left:.0f} {tk} {sd}")
def cell(g):
    if not g: return "n    0"
    nets = [x[1] for x in g]
    m = sum(nets) / len(nets)
    se = math.sqrt(sum((x - m) ** 2 for x in nets) / max(1, len(nets) - 1) / len(nets))
    return "n %4d net %+.3f (se %.3f) win %.2f" % (len(g), m, se, sum(1 for x in nets if x > 0) / len(nets))
for top in TOPS:
    bar = sum(bars[top]) / max(1, len(bars[top]))
    print(f"\n=== {name} top {int(top * 100)}%: bar {bar:+.3f}")
    for h in (0, 1):
        g = [x for x in picks[top].values() if x[0] == h]
        print(f" half {h + 1}: " + cell(g))
    for lo, hi in ((180, 360), (360, 600), (600, 901)):
        print(f" left {lo // 60}-{hi // 60 if hi < 901 else 15} min: " + " | ".join(f"half {h + 1} " + cell([x for x in picks[top].values() if x[0] == h and lo <= x[2] < hi]) for h in (0, 1)))
print(f"done in {time.time() - t0:.0f}s")
