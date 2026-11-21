"""RANKING / PRICE-BIAS CHECK (2026-11-20, user via parent: "the bars (+0.112 / +0.073 per $1) are way too high; that is what you would expect from low buys"). Net per $1 scales with 1/ask, so a ranking by predicted net per $1 may be a ranking by a cheaper ask. Forms fixed BEFORE the run, on today's gated blended model (the gate, the blended cushion, today's windows), commodity perp reference, S&P out, asks 0.60-0.85, hourly refit on the trailing 120 h, first reading per market side held to settlement, the two halves of the test hours:
 R0 rank by predicted best-exit net per $1, one bar (today)                 R2 the same score, a bar inside each ask band (0.60-0.65 ... 0.80-0.85)
 R1 rank by predicted net / ceiling (1-ask-fee)/ask, one bar               R3 that ratio, a bar inside each ask band
 R4 predicted win chance minus the ask, one bar                            R5 that, a bar inside each ask band
The win chance is a ridge fit of the held result (1 or 0) on the same readings (linear probability, cut to 0.01-0.99). A bar is the top 5% / 10% of the training scores of the same group (the band, or the whole 0.60-0.85 range). FIX=1 also drops from each training set the rows of markets not yet closed at the refit time (the outcome and the exits are from after it); FIX=0 is the earlier form, for comparison with earlier numbers. Reported: (1) the ask distribution of the R0 picks against all gated sides in the band (mean, quartiles, share under 0.70); (2) per variant and half: n, net, win rate, the ask mix (mean, share under 0.70); (3) calibration: the picks' mean PREDICTED hold net per $1 against realised, and predicted win chance against realised win rate, by ask band for R0 and overall for each variant. Settings chosen before the results.
Run: SPOT=0 scripts/cloud_job.sh scripts/binary_risk_price_bias.py FIX=1   (and FIX=0)"""
import bisect, math, multiprocessing, os, time
from datetime import datetime, timezone
from kalshi_moonshot import binaryrisk as br, config, db
from kalshi_moonshot.perpref import index_for_series as perp_index
from kalshi_moonshot.kalshiws import index_for_series as crypto_index
from kalshi_moonshot.strategy import fee_per_contract as fee

FIX = os.environ.get("FIX", "1") == "1"
cfg = dict(look=(15, 30, 60), u_min=15, uh_min=60, avgs=(1, 3, 5))
LOOK, AVGS = cfg["look"], cfg["avgs"]
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
NEED = 60 * 60.0 + 900.0 + 300.0

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
                    rows.append((t, vector(f, side, ask), [(p - ask - fee(ask)) / ask for p in pays], ask, ticker, side, close - t, 1.0 if won else 0.0, close))
            t += STEP
    return rows

with multiprocessing.get_context("fork").Pool(4) as pool:
    S = [r for rs in pool.imap_unordered(build, list(jobs.items())) for r in rs]
S.sort(key=lambda r: r[0])
print(f"{len(S)} gated sides, {time.time() - t0:.0f}s", flush=True)


BANDS = ((0.60, 0.65), (0.65, 0.70), (0.70, 0.75), (0.75, 0.80), (0.80, 0.85))
def band_of(a):
    for k, (lo, hi) in enumerate(BANDS):
        if lo <= a < hi: return k
def fit(rs):
    n = len(rs[0][1])
    mu = [sum(r[1][i] for r in rs) / len(rs) for i in range(n)]
    sd = [math.sqrt(sum((r[1][i] - mu[i]) ** 2 for r in rs) / len(rs)) or 1.0 for i in range(n)]
    K = n + 1
    A = [[0.0] * K for _ in range(K)]
    NT = len(br.EXITS) + 1  # the five exits' nets, then the win (1 or 0)
    B = [[0.0] * K for _ in range(NT)]
    for r in rs[::3]:
        v = [1.0] + [(r[1][i] - mu[i]) / sd[i] for i in range(n)]
        tg = [min(br.CAP, x) for x in r[2]] + [r[7]]
        for i in range(K):
            for j in range(i, K): A[i][j] += v[i] * v[j]
            for e in range(NT): B[e][i] += v[i] * tg[e]
    for i in range(K):
        A[i][i] += br.RIDGE if i else 1e-6
        for j in range(i): A[i][j] = A[j][i]
    return mu, sd, [br._solve(A, B[e]) for e in range(NT)]

def scores(m, r):
    """(best-exit predicted net capped, hold net predicted capped, predicted win chance)."""
    mu, sd, W = m
    v = [1.0] + [(a - u) / d for a, u, d in zip(r[1], mu, sd)]
    got = [sum(a * b for a, b in zip(w, v)) for w in W]
    ce = br.ceiling(r[3])
    return min(max(got[:5]), ce), min(got[0], ce), min(0.99, max(0.01, got[5]))

def score_of(var, sc, r):
    if var in ("R0", "R2"): return sc[0]
    if var in ("R1", "R3"): return sc[0] / br.ceiling(r[3])
    return sc[2] - r[3]
VARS = ("R0", "R1", "R2", "R3", "R4", "R5")
PERBAND = ("R2", "R3", "R5")
times = [r[0] for r in S]
t_start, t_end = S[0][0] + 24 * 3600, S[-1][0]
t_mid = (t_start + t_end) / 2
def hour(t):
    a0, a1, b1 = (bisect.bisect_left(times, x) for x in (t - 120 * 3600, t, t + 3600))
    tr, te = S[a0:a1], S[a1:b1]
    if FIX: tr = [r for r in tr if r[8] <= t]
    if len(tr) < 1500 or len(te) < 100: return None
    m = fit(tr)
    inb = lambda r: br.usable(r[3]) and BAND[0] <= r[3] < BAND[1]
    trs = [(r, scores(m, r)) for r in tr[::4] if inb(r)]
    if not trs: return None
    cuts = {}
    for var in VARS:
        if var in PERBAND:
            for k in range(len(BANDS)):
                ps = sorted(score_of(var, sc, r) for r, sc in trs if band_of(r[3]) == k)
                for top in TOPS: cuts[(var, top, k)] = ps[int(len(ps) * (1 - top))] if ps else 9e9
        else:
            ps = sorted(score_of(var, sc, r) for r, sc in trs)
            for top in TOPS: cuts[(var, top, None)] = ps[int(len(ps) * (1 - top))]
    half = 0 if t < t_mid else 1
    out, allrows = [], []
    for r in te:
        if not inb(r): continue
        sc = scores(m, r)
        allrows.append((r[4], r[5], half, r[3]))
        for var in VARS:
            for top in TOPS:
                if score_of(var, sc, r) >= cuts[(var, top, band_of(r[3]) if var in PERBAND else None)]:
                    out.append((var, top, r[4], r[5], half, r[2][0], r[3], r[7], sc[1], sc[2]))
    return out, allrows

picks = {(v, top): {} for v in VARS for top in TOPS}
every = {}
with multiprocessing.get_context("fork").Pool(4) as pool:
    for res in pool.imap_unordered(hour, range(int(t_start), int(t_end - 3600), 3600)):
        if not res: continue
        out, allrows = res
        for tk, sd, half, ask in allrows: every.setdefault((tk, sd), (half, ask))
        for var, top, tk, sd, half, net, ask, won, ph, pw in out:
            picks[(var, top)].setdefault((tk, sd), (half, net, ask, won, ph, pw))
def q(xs, p):
    xs = sorted(xs); return xs[min(len(xs) - 1, int(p * len(xs)))]
def mix(g):
    a = [x[2] if len(x) > 3 else x[1] for x in g]
    return "ask mean %.3f q25 %.3f med %.3f q75 %.3f share<0.70 %.0f%%" % (sum(a) / len(a), q(a, .25), q(a, .5), q(a, .75), 100 * sum(1 for x in a if x < 0.70) / len(a)) if a else "-"
print(f"\n=== (1) ASK DISTRIBUTION  FIX={int(FIX)}")
for h in (0, 1):
    print(f" half {h + 1} all gated band sides (first reading): n {sum(1 for v in every.values() if v[0] == h)} " + mix([(None, v[1]) for v in every.values() if v[0] == h]))
    for top in TOPS:
        g = [x for x in picks[("R0", top)].values() if x[0] == h]
        print(f" half {h + 1} R0 top {int(top * 100)}% picks: n {len(g)} " + mix(g))
def cell(g):
    if not g: return "n    0"
    nets = [x[1] for x in g]; m_ = sum(nets) / len(nets)
    se = math.sqrt(sum((x - m_) ** 2 for x in nets) / max(1, len(nets) - 1) / len(nets))
    return "n %4d net %+.3f (se %.3f) win %.2f | pred hold %+.3f | pred win %.2f | ask %.3f <0.70 %.0f%%" % (len(g), m_, se, sum(x[3] for x in g) / len(g), sum(x[4] for x in g) / len(g), sum(x[5] for x in g) / len(g), sum(x[2] for x in g) / len(g), 100 * sum(1 for x in g if x[2] < 0.70) / len(g))
print(f"\n=== (2)+(3) RANKING VARIANTS (commodity gated, held; net per $1 after the fee; pred hold = predicted hold net per $1, pred win = predicted win chance)")
for top in TOPS:
    print(f"-- top {int(top * 100)}%")
    for var in VARS:
        for h in (0, 1):
            print(f" {var} half {h + 1}: " + cell([x for x in picks[(var, top)].values() if x[0] == h]))
print("\n=== R0 by ask band: predicted hold net vs realised")
for top in TOPS:
    for k, (lo, hi) in enumerate(BANDS):
        for h in (0, 1):
            g = [x for x in picks[("R0", top)].values() if x[0] == h and band_of(x[2]) == k]
            print(f" top {int(top * 100)}% {lo:.2f}-{hi:.2f} half {h + 1}: " + cell(g))
print(f"done in {time.time() - t0:.0f}s")
