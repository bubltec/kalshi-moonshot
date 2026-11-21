"""FIT SEARCH, BASE VARIANT ON THE STUDY CACHE (2026-11-21): scripts/binary_risk_fit_search.py V=base, unchanged logic, but the rows come from scripts/study_cache.py (the cache the runner built once) instead of a database restore, so it runs on the Mac in seconds. Same picks, same protocol: commodity perp (S&P out), asks 0.60-0.85, today's gate, first reading per market side held to settlement, hourly refit on the trailing 120 h with only markets closed by the refit time, top 5% / 10% bars from the training predictions in the band, net per $1 after the fee, half 1 / half 2 of the test hours (the newest 24 h held out and shown only with HOLDOUT=1).
Run: .venv/bin/python scripts/study_cache_fit_base.py job-results/<cache id> [HOLDOUT=1] [TIE=<seed>]"""
import bisect, math, multiprocessing, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import study_cache as sc
from kalshi_moonshot import binaryrisk as br

V, HOLDOUT, WIN = "base", "HOLDOUT=1" in sys.argv, 120.0
BAND, TOPS = (0.60, 0.85), (0.05, 0.10)
BANDS = ((0.60, 0.65), (0.65, 0.70), (0.70, 0.75), (0.75, 0.80), (0.80, 0.85))
NI = {n: i for i, n in enumerate(br.NAMES)}
t0 = time.time()
TIE = next((int(a[4:]) for a in sys.argv if a.startswith("TIE=")), "ticker")  # TIE=<seed> shuffles rows that share a time (see study_cache.side_rows)
S = sc.side_rows(sc.Cache(sys.argv[1]), kind="commodity", tie=TIE)
print(f"VARIANT {V}: {len(S)} gated commodity sides, {time.time() - t0:.1f}s", flush=True)
COLS = list(range(len(br.NAMES)))
def X(r): return [r[1][i] for i in COLS]

def stats(rs, idx, mu, sd):
    n = len(mu); K = n + 1
    A = [[0.0] * K for _ in range(K)]
    B = [[0.0] * K for _ in range(6)]
    for r in rs:
        x = X(r)
        v = [1.0] + [(x[i] - mu[i]) / sd[i] for i in range(n)]
        tg = [min(br.CAP, e) for e in r[2]] + [r[7]]
        for i in range(K):
            vi = v[i]
            for j in range(i, K): A[i][j] += vi * v[j]
            for e in range(6): B[e][i] += vi * tg[e]
    return A, B

def solve(A, B, lam):
    K = len(A)
    M = [[A[min(i, j)][max(i, j)] + (lam if (i == j and i) else (1e-6 if i == j else 0.0)) for j in range(K)] for i in range(K)]
    return [br._solve(M, B[e]) for e in range(6)]

def fit(tr):
    xs = [X(r) for r in tr]
    n = len(xs[0])
    mu = [sum(x[i] for x in xs) / len(xs) for i in range(n)]
    sd = [math.sqrt(sum((x[i] - mu[i]) ** 2 for x in xs) / len(xs)) or 1.0 for i in range(n)]
    sub = tr[::3]
    m = {"mu": mu, "sd": sd, "lam": br.RIDGE}
    if V in ("logit", "winp"):
        w = [0.0] * (n + 1)
        K = n + 1
        vs = [[1.0] + [(x[i] - mu[i]) / sd[i] for i in range(n)] for x in (X(r) for r in sub)]
        ys = [r[7] for r in sub]
        for _ in range(6):
            H = [[0.0] * K for _ in range(K)]; g = [0.0] * K
            for v, y in zip(vs, ys):
                p = sigm(sum(a * b for a, b in zip(w, v))); ww = max(p * (1 - p), 1e-4)
                for i in range(K):
                    g[i] += (y - p) * v[i]
                    for j in range(i, K): H[i][j] += ww * v[i] * v[j]
            for i in range(K):
                H[i][i] += br.RIDGE if i else 1e-6
                g[i] -= (br.RIDGE * w[i]) if i else 0.0
                for j in range(i): H[i][j] = H[j][i]
            d = br._solve(H, g)
            w = [a + b for a, b in zip(w, d)]
        m["w"] = w
        return m
    A, B = stats(sub, None, mu, sd)
    lam = br.RIDGE
    if V == "ridgecv":
        blocks = [[r for r in sub if hash(r[4]) % 4 == k] for k in range(4)]
        blocks = [[r for r in sub if (sum(map(ord, r[4])) % 4) == k] for k in range(4)]
        bs = [stats(b, None, mu, sd) for b in blocks]
        best = None
        for lm in (1.0, 10.0, 100.0, 1000.0, 10000.0):
            err = 0.0; cnt = 0
            for k in range(4):
                Ak = [[A[i][j] - bs[k][0][i][j] for j in range(len(A))] for i in range(len(A))]
                Bk = [[B[e][i] - bs[k][1][e][i] for i in range(len(A))] for e in range(6)]
                w = solve(Ak, Bk, lm)[0]
                for r in blocks[k]:
                    x = X(r); v = [1.0] + [(x[i] - mu[i]) / sd[i] for i in range(n)]
                    err += (min(br.CAP, r[2][0]) - sum(a * b for a, b in zip(w, v))) ** 2; cnt += 1
            err /= max(cnt, 1)
            if best is None or err < best[0]: best = (err, lm)
        lam = best[1]
    m["lam"] = lam
    m["W"] = solve(A, B, lam)
    return m

def pred(m, r):
    """(score used to rank, predicted hold net per $1, predicted best-exit net) all capped at the ceiling."""
    x = X(r); v = [1.0] + [(a - u) / d for a, u, d in zip(x, m["mu"], m["sd"])]
    ce = br.ceiling(r[3])
    if V == "winp":
        p = sigm(sum(a * b for a, b in zip(m["w"], v)))
        return p, p, p  # the score is the predicted chance to win, from the curve readings alone
    if V == "logit":
        p = sigm(sum(a * b for a, b in zip(m["w"], v)))
        net = min((p - r[3] - fee(r[3])) / r[3], ce)
        return net, net, net
    got = [sum(a * b for a, b in zip(w, v)) for w in m["W"][:5]]
    best, hold = min(max(got), ce), min(got[0], ce)
    return (best / ce if V == "ceil" else best), hold, best

times = [r[0] for r in S]
cm = [r for r in S if r[9] == "commodity"]
t_start, t_end = cm[0][0] + 24 * 3600, cm[-1][0]
t_hold = t_end - 24 * 3600
t_mid = (t_start + t_hold) / 2
inb = lambda r: br.usable(r[3]) and BAND[0] <= r[3] < BAND[1]
def band_of(a):
    for k, (lo, hi) in enumerate(BANDS):
        if lo <= a < hi: return k

def hour(t):
    hold_hour = t >= t_hold
    if hold_hour and not HOLDOUT: return None
    a0, a1, b1 = (bisect.bisect_left(times, x) for x in (t - WIN * 3600, t, t + 3600))
    tr = [r for r in S[a0:a1] if r[8] <= t]  # market closed by the refit time (no outcome from after it)
    te = [r for r in S[a1:b1] if r[9] == "commodity"]
    trc = [r for r in tr if r[9] == "commodity"]
    if len(trc) < 1500 or len(te) < 100: return None
    m = fit(tr)
    trs = [(r, pred(m, r)[0]) for r in trc[::4] if inb(r)]
    if not trs: return None
    cuts = {}
    for top in TOPS:
        if V == "band":
            for k in range(len(BANDS)):
                ps = sorted(p for r, p in trs if band_of(r[3]) == k)
                cuts[(top, k)] = ps[int(len(ps) * (1 - top))] if ps else 9e9
        else:
            ps = sorted(p for _, p in trs)
            cuts[(top, None)] = ps[int(len(ps) * (1 - top))]
    seg = 2 if hold_hour else (0 if t < t_mid else 1)
    out = []
    for r in te:
        if not inb(r): continue
        sc, ph, pb = pred(m, r)
        for top in TOPS:
            if sc >= cuts[(top, band_of(r[3]) if V == "band" else None)]:
                out.append((top, r[4], r[5], seg, r[2][0], r[1][NI["sl30"]], r[1][NI["cv30"]], ph, r[7], cuts[(top, band_of(r[3]) if V == "band" else None)]))
    return t, len(trc), len({r[4] for r in tr}), m["lam"], out

picks = {top: {} for top in TOPS}
bars = {top: [[], [], []] for top in TOPS}
sizes, lams = [], []
with multiprocessing.get_context("fork").Pool(4) as pool:
    for res in pool.imap_unordered(hour, range(int(t_start), int(t_end - 3600), 3600)):
        if not res: continue
        t, ntr, nmk, lam, out = res
        sizes.append((ntr, nmk)); lams.append(lam)
        for top, tk, sd, seg, net, sl, cv, ph, won, cut in out:
            if (tk, sd) not in picks[top]:
                picks[top][(tk, sd)] = (seg, net, sl, cv, ph, won)
                bars[top][seg].append(cut)
slv = sorted(r[1][NI["sl30"]] for r in cm[::7]); cvv = sorted(r[1][NI["cv30"]] for r in cm[::7])
c_sl = (slv[len(slv) // 3], slv[2 * len(slv) // 3]); c_cv = (cvv[len(cvv) // 3], cvv[2 * len(cvv) // 3])
def th(x, c): return 0 if x < c[0] else 1 if x < c[1] else 2
# win-rate baseline per segment: the first in-band gated reading per market side
_seen, _base = set(), {0: [], 1: [], 2: []}
for r in cm:
    if not inb(r) or (r[4], r[5]) in _seen: continue
    _seen.add((r[4], r[5]))
    _base[2 if r[0] >= t_hold else (0 if r[0] < t_mid else 1)].append(r[7])
BASE = {k: (sum(v) / len(v) if v else 0.0) for k, v in _base.items()}
def cell(g, seg=None):
    if V == "winp":
        if not g: return "n    0"
        n = len(g); w = sum(x[5] for x in g) / n; se = math.sqrt(max(w * (1 - w), 1e-9) / n)
        return "n %4d win %.2f se %.3f | universe win %.2f lift %+.3f (%+.1f se) | mean predicted win %.2f" % (n, w, se, BASE[seg], w - BASE[seg], (w - BASE[seg]) / se, sum(x[4] for x in g) / n)
    if not g: return "n    0"
    nets = [x[1] for x in g]; mm = sum(nets) / len(nets)
    se = math.sqrt(sum((x - mm) ** 2 for x in nets) / max(1, len(nets) - 1) / len(nets))
    return "n %4d net %+.3f se %.3f (%+.1f se) win %.2f pred %+.3f" % (len(g), mm, se, mm / se if se else 0.0, sum(x[5] for x in g) / len(g), sum(x[4] for x in g) / len(g))
print(f"training per hour: mean rows {sum(a for a, _ in sizes) / len(sizes):.0f} (commodity), mean distinct markets {sum(b for _, b in sizes) / len(sizes):.0f}; ridge strength mean {sum(lams) / len(lams):.1f}; hours {len(sizes)}")
for top in TOPS:
    print(f"\n=== {V} top {int(top * 100)}%")
    for seg, name in ((0, "half 1"), (1, "half 2")) + (((2, "HOLDOUT"),) if HOLDOUT else ()):
        g = [x for x in picks[top].values() if x[0] == seg]
        bar = sum(bars[top][seg]) / max(1, len(bars[top][seg]))
        print(f" {name}: bar {bar:+.3f} " + cell(g, seg))
        for lab, idx, c in (("slope", 2, c_sl), ("curv", 3, c_cv)):
            print("    by " + lab + " third: " + " | ".join(f"{('low', 'mid', 'high')[k]} " + cell([x for x in g if th(x[idx], c) == k], seg) for k in range(3)))
print(f"done in {time.time() - t0:.0f}s")
