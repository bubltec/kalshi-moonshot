"""FIT SEARCH (2026-11-20, user via parent: "the problem is probably the fit, not the inputs ... one change per run, forms stated before the run"). One variant per run (V=<name>), each ONE change from today's gated blended model (V=base): commodity perp, S&P out, asks 0.60-0.85, today's gate, first reading per market side held to settlement, hourly refit, top 5% / 10% bars from the training predictions in the band, net per $1 after the fee. Common to every run, including base: a training row is used only if its market had closed by the refit time (the leakage fix, audit 2026-11-20).
VARIANTS: base | ridgecv (ridge strength chosen by 4-fold cross-validation in MARKET blocks of the hold-net error, grid 1,10,100,1000,10000) | fewer (9 readings: logit ask, blended cushion, trend 1-3, time left, position 15 and 30, move 15 and 30, 24 h drift) | logit (logistic ridge fit on the held win/lose, net derived = (p - ask - fee)/ask, ranked on that) | win240 (trailing 240 h) | winall (every earlier hour) | pool (train on crypto and commodity, bars and scoring on commodity) | band (a bar inside each ask band 0.60-0.65 ... 0.80-0.85) | ceil (rank by predicted net / ceiling).
PROTOCOL (set before any run): the test hours are cut into the NEWEST 24 hours (the HOLDOUT, printed only when HOLDOUT=1, to be scored once for the single chosen set) and the rest, split in halves: half 1 selects, half 2 confirms. Every run is a counted variant (studies/results/2026-11-20-variant-log.txt). A result counts only if positive on half 2 and the holdout in the same sign after the count. Reported per half: n, net per $1, its standard error and net/se, win rate, the mean bar, the mean PREDICTED hold net against realised, the net by third of the wave slope sl30 and curvature cv30 of the pick (rule 4), and the training size (distinct markets, rows).
Run: SPOT=0 scripts/cloud_job.sh scripts/binary_risk_fit_search.py V=<name> [HOLDOUT=1]"""
import bisect, math, multiprocessing, os, time
from datetime import datetime, timezone
from kalshi_moonshot import binaryrisk as br, config, db
from kalshi_moonshot.perpref import index_for_series as perp_index
from kalshi_moonshot.kalshiws import index_for_series as crypto_index
from kalshi_moonshot.strategy import fee_per_contract as fee

V = os.environ.get("V", "base")
HOLDOUT = os.environ.get("HOLDOUT") == "1"
MINLEFT = float(os.environ.get("MINLEFT", "180"))  # the model's gate drops the last 3 minutes; MINLEFT=20 lifts it for a study (user, 2026-11-21: the last 3 minutes are among the best times to buy when the conditions are right)
POOL = V == "pool"
WIN = {"win240": 240.0, "winall": 1e6}.get(V, 120.0)
t0 = time.time()
BAND, TOPS, STEP, HORIZONS = (0.60, 0.85), (0.05, 0.10), 60.0, br.HORIZONS
BANDS = ((0.60, 0.65), (0.65, 0.70), (0.70, 0.75), (0.75, 0.80), (0.80, 0.85))
START = datetime(2026, 11, 9, 5, tzinfo=timezone.utc).timestamp()
NI = {n: i for i, n in enumerate(br.NAMES)}
FEWER = [NI[n] for n in ("logit_ask", "czb", "tr13", "left", "pos15", "pos30", "mv15", "mv30", "dr24")]
s = config.reload()
st = db.connect(s.db_path, "main", shared=True)
wins = st.execute("SELECT ticker, series, close_ts, strike, result FROM windows WHERE result IN ('yes','no') AND close_ts > ? ORDER BY close_ts", (START + 7200,)).fetchall()
jobs = {}
for w in wins:
    if w["series"].startswith("KXINX"): continue
    ix = crypto_index(w["series"])
    kind = "crypto" if ix else "commodity"
    if not ix: ix = perp_index(w["series"])
    if not ix or (kind == "crypto" and (not POOL or not w["strike"])): continue
    jobs.setdefault(ix, []).append((w["ticker"], float(w["close_ts"]), float(w["strike"]) if kind == "crypto" else None, w["result"] == "yes", kind))

def build(job):
    ix, ws = job
    tape = db.connect(config.reload().tape_path, "tape", shared=True)
    tk = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (ix, min(w[1] for w in ws) - 4900, max(w[1] for w in ws) + 5)).fetchall()
    T, Vv = [float(r[0]) for r in tk], [float(r[1]) for r in tk]
    del tk
    rows = []
    for ticker, close, strike, won_yes, kind in ws:
        opened = close - 900.0
        if strike is None:
            k = bisect.bisect_left(T, opened)
            if k >= len(T) or T[k] - opened > 25.0: continue
            strike = Vv[k]
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
            f = br.readings(T, Vv, t, strike, close - t, (ref[0], ref[1])) if qq else None
            if f and qq:
                ahead = [quote_at(t + h) if t + h <= close - 15.0 else None for h in HORIZONS]
                for side, ask in (("yes", qq[1]), ("no", 1.0 - qq[0])):
                    if not 0.02 <= ask <= 0.98: continue
                    sg = 1.0 if side == "yes" else -1.0
                    if min(sg * f["cz1"], sg * f["cz3"], sg * f["cz5"]) <= 0.0 or close - t < MINLEFT: continue
                    won = won_yes if side == "yes" else not won_yes
                    pays = [1.0 if won else 0.0]
                    for e in ahead:
                        if e is None: pays.append(1.0 if won else 0.0)
                        else:
                            sell = e[0] if side == "yes" else 1.0 - e[1]
                            pays.append(sell - fee(sell))
                    j0, j1, j6 = bisect.bisect_right(qt, t), bisect.bisect_right(qt, close - 15.0), bisect.bisect_right(qt, t + 60.0)
                    if side == "yes": bids, asks = [x / scale for x in qb[j0:j1]], [x / scale for x in qa[j0:j1]]
                    else: bids, asks = [1.0 - x / scale for x in qa[j0:j1]], [1.0 - x / scale for x in qb[j0:j1]]
                    if bids:
                        kp = max(range(len(bids)), key=bids.__getitem__)
                        path = (bids[kp], min(bids[:kp + 1]), min(asks[:max(1, j6 - j0)]), min(asks), min(bids), t - qt[j0 - 1] if j0 else 99.0)  # the side's later peak bid, its lowest bid before that peak, its lowest ask in the next 60 s, its lowest ask and lowest bid to the close
                    else: path = None  # (the last element of path is the quote's age at the reading, seconds)
                    rows.append((t, br.side_vector(f, side, ask), [(p - ask - fee(ask)) / ask for p in pays], ask, ticker, side, close - t, 1.0 if won else 0.0, close, kind, path))
            t += STEP
    return rows

with multiprocessing.get_context("fork").Pool(4) as pool:
    S = [r for rs in pool.imap_unordered(build, list(jobs.items())) for r in rs]
S.sort(key=lambda r: r[0])
print(f"VARIANT {V}: {len(S)} gated sides ({sum(1 for r in S if r[9] == 'commodity')} commodity), {time.time() - t0:.0f}s", flush=True)

COLS = [NI["czb"]] if V == "cushonly" else FEWER if V == "fewer" else (list(range(2, len(br.NAMES))) if V in ("winp", "winp0", "value", "table") else list(range(len(br.NAMES))))  # winp: the curve readings only, no ask readings (logit_ask, ask_off)
def X(r): return [r[1][i] for i in COLS]
def sigm(z): return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))

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
    if V in ("logit", "winp", "winp0", "value", "table", "cushonly"):
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
    if V in ("winp", "winp0", "value", "table", "cushonly"):
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
inb = (lambda r: True) if V in ("winp0", "value", "table") else (lambda r: br.usable(r[3]) and BAND[0] <= r[3] < BAND[1])  # winp0: the risk score is evaluated on EVERY gated tick, no ask band
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
                out.append((top, r[4], r[5], seg, r[2][0], r[1][NI["sl30"]], r[1][NI["cv30"]], ph, r[7], cuts[(top, band_of(r[3]) if V == "band" else None)], r[3]))
    return t, len(trc), len({r[4] for r in tr}), m["lam"], out


if V == "value":
    # THE BUY-SIDE VALUE CHECK (2026-11-21, user via parent: "build the buy-side value check now ... win chance minus ask minus the fee, against a bar read from that market's own recent history (rule 2, no fixed number), never an ask 0.45-0.55"). The win-chance score is the price-free one (winp0); this is a SEPARATE step after it. A reading is bought when edge = win chance - ask - fee(ask) is positive AND at least the QUANTILE of that market series' own edge readings over the previous 24 hours (needs 100 of them), never at an ask of 0.45-0.55. The three quantiles (0.80, 0.90, 0.95) are the settings tried (counted: 3 variants); the price-blind comparison is the score's own top 10% (the winp0 pick set). One first reading per market side, held to settlement, net per $1 after the buy fee.
    QS, OWN_H, OWN_MIN = (0.80, 0.90, 0.95), 24 * 3600.0, 100  # first run (3 h, 200 readings) found no picks: a series has only ~8 gated readings an hour, so the history is the last 24 h with 100 needed (changed after seeing n=0, before seeing any value result)
    def sname(r): return r[4].split("-")[0]
    def hour_value(t):
        hold_hour = t >= t_hold
        if hold_hour and not HOLDOUT: return None
        a0, a1, b1 = (bisect.bisect_left(times, x) for x in (t - WIN * 3600, t, t + 3600))
        tr = [r for r in S[a0:a1] if r[8] <= t]
        te = [r for r in S[a1:b1] if r[9] == "commodity"]
        trc = [r for r in tr if r[9] == "commodity"]
        if len(trc) < 1500 or len(te) < 100: return None
        m = fit(tr)
        ps = sorted(pred(m, r)[0] for r in trc[::4])
        cut10 = ps[int(len(ps) * 0.90)]
        h0 = bisect.bisect_left(times, t - OWN_H)
        hs = {}
        for r in S[h0:b1]:
            if r[9] != "commodity": continue
            hs.setdefault(sname(r), ([], []))
            hs[sname(r)][0].append(r[0]); hs[sname(r)][1].append(pred(m, r)[0] - r[3] - fee(r[3]))
        seg = 2 if hold_hour else (0 if t < t_mid else 1)
        out, blind, band = [], [], {}
        for r in te:
            a = r[3]
            p = pred(m, r)[0]
            e = p - a - fee(a)
            bk = 0 if a < 0.40 else 1 if a < 0.60 else 2 if a < 0.85 else 3
            b = band.setdefault(bk, [0, 0, 0.0, 0.0, 0.0])
            b[0] += 1; b[1] += 1 if e > 0 else 0; b[2] += e; b[3] += r[7]; b[4] += r[2][0]
            row = (r[4], r[5], seg, r[2][0], r[1][NI["sl30"]], r[1][NI["cv30"]], p, r[7], a, e)
            if p >= cut10: blind.append(row)
            if br.DEAD[0] <= a <= br.DEAD[1] or e <= 0: continue
            ts, es = hs[sname(r)]
            lo, hi = bisect.bisect_left(ts, r[0] - OWN_H), bisect.bisect_left(ts, r[0])
            if hi - lo < OWN_MIN: continue
            own = sorted(es[lo:hi])
            for q in QS:
                bar = own[min(len(own) - 1, int(len(own) * q))]
                if e >= bar: out.append((q, row + (bar,)))
        return seg, out, blind, band
    vp = {q: {} for q in QS}; blindp = {}; bandagg = {0: {}, 1: {}, 2: {}}
    with multiprocessing.get_context("fork").Pool(4) as pool:
        for res in pool.imap(hour_value, range(int(t_start), int(t_end - 3600), 3600)):
            if not res: continue
            seg, out, blind, band = res
            for q, row in out:
                vp[q].setdefault((row[0], row[1]), row)
            for row in blind:
                blindp.setdefault((row[0], row[1]), row)
            for bk, b in band.items():
                a = bandagg[seg].setdefault(bk, [0, 0, 0.0, 0.0, 0.0])
                for i in range(5): a[i] += b[i]
    def mse(xs):
        n = len(xs); mm = sum(xs) / n if n else 0.0
        return mm, math.sqrt(sum((x - mm) ** 2 for x in xs) / max(1, n - 1) / max(1, n))
    slv = sorted(r[1][NI["sl30"]] for r in cm[::7]); cvv = sorted(r[1][NI["cv30"]] for r in cm[::7])
    c_sl = (slv[len(slv) // 3], slv[2 * len(slv) // 3]); c_cv = (cvv[len(cvv) // 3], cvv[2 * len(cvv) // 3])
    th = lambda x, c: 0 if x < c[0] else 1 if x < c[1] else 2
    _seen, _un = set(), {0: [], 1: [], 2: []}
    for r in cm:
        if (r[4], r[5]) in _seen: continue
        _seen.add((r[4], r[5])); _un[2 if r[0] >= t_hold else (0 if r[0] < t_mid else 1)].append(r[2][0])
    def line(g):
        if not g: return "n    0"
        nets = [x[3] for x in g]; mm, se = mse(nets)
        wn, _ = mse([x[7] for x in g]); ak, _ = mse([x[8] for x in g]); wa, wse = mse([x[7] - x[8] for x in g])
        return "n %4d ask %.3f win %.2f | win-ask %+.3f (%+.1f se) | net/$1 %+.3f se %.3f (%+.1f se) | total net on $1 stakes %+.1f | profitable %d lose %d" % (len(g), ak, wn, wa, wa / wse if wse else 0.0, mm, se, mm / se if se else 0.0, sum(nets), sum(1 for n_ in nets if n_ > 0), sum(1 for n_ in nets if n_ <= 0))
    print("\n######## WHERE THE SCORE EVER FINDS VALUE (every gated reading of the test hours; edge = win chance - ask - fee)")
    for seg, name in ((0, "half 1"), (1, "half 2")) + (((2, "HOLDOUT"),) if HOLDOUT else ()):
        for bk, lab in ((0, "ask <0.40"), (1, "0.40-0.60"), (2, "0.60-0.85"), (3, "0.85+")):
            b = bandagg[seg].get(bk)
            if b and b[0]: print(f" {name} {lab}: n {b[0]} edge>0 in {b[1] / b[0]:.2f} | mean edge {b[2] / b[0]:+.3f} | realised win {b[3] / b[0]:.2f} | held net/$1 {b[4] / b[0]:+.3f}")
    print("\n######## VALUE BUY (own-history bar) vs the score's price-blind top 10% vs the universe")
    for seg, name in ((0, "half 1"), (1, "half 2")) + (((2, "HOLDOUT"),) if HOLDOUT else ()):
        um, use = mse(_un[seg])
        print(f"\n {name} UNIVERSE (first reading per market side): n {len(_un[seg])} net/$1 {um:+.3f} se {use:.3f}")
        print("   price-blind top 10% by win chance: " + line([x for x in blindp.values() if x[2] == seg]))
        for q in QS:
            g = [x for x in vp[q].values() if x[2] == seg]
            print(f"   value buy, bar = own top {int((1 - q) * 100)}%: " + line(g))
            for lab, idx, c in (("slope", 4, c_sl), ("curv", 5, c_cv)):
                print("      by " + lab + " third: " + " | ".join(f"{('low', 'mid', 'high')[k]} " + line([x for x in g if th(x[idx], c) == k]) for k in range(3)))
    print(f"done in {time.time() - t0:.0f}s")
    raise SystemExit(0)


if V == "table":
    # SCORE x PRICE TABLE (2026-11-21, user: the risk score "affects the price, it doesn't compare to it"; the form is not known: "this is what I need your help figuring out"). No formula is assumed and nothing is compared with Kalshi's odds or held to settlement. Each gated reading gets the price-free win-chance score (winp0, hourly refit), read as its PLACE in that market series' own scores of the previous 24 hours (100 needed; fifths), and its price band. One reading per market side per cell (the first). In each cell: what the side's price did NEXT, in cents: how far the bid later rose above the ask paid, how far the ask fell under it (so where a resting buy would have filled, in the next 60 s and by the close), and how far the bid fell before its peak.
    OWN_H, OWN_MIN = 24 * 3600.0, 100
    PB = ((0.0, 0.40), (0.40, 0.60), (0.60, 0.75), (0.75, 0.85), (0.85, 0.93), (0.93, 1.01))
    def sname(r): return r[4].split("-")[0]
    def hour_table(t):
        if t >= t_hold: return None  # the newest 24 h stay out of this table
        a0, a1, b1 = (bisect.bisect_left(times, x) for x in (t - WIN * 3600, t, t + 3600))
        tr = [r for r in S[a0:a1] if r[8] <= t]
        te = [r for r in S[a1:b1] if r[9] == "commodity"]
        if len(tr) < 1500 or len(te) < 100: return None
        m = fit(tr)
        h0 = bisect.bisect_left(times, t - OWN_H)
        hs = {}
        for r in S[h0:b1]:
            hs.setdefault(sname(r), ([], []))
            hs[sname(r)][0].append(r[0]); hs[sname(r)][1].append(pred(m, r)[0])
        seg = 0 if t < t_mid else 1
        out = []
        for r in te:
            if r[10] is None: continue
            ts, ps = hs[sname(r)]
            lo, hi = bisect.bisect_left(ts, r[0] - OWN_H), bisect.bisect_left(ts, r[0])
            if hi - lo < OWN_MIN: continue
            own = sorted(ps[lo:hi])
            sc = pred(m, r)[0]
            qb_ = min(4, int(5 * bisect.bisect_left(own, sc) / len(own)))
            pb_ = next(k for k, (a, b) in enumerate(PB) if a <= r[3] < b)
            out.append((seg, r[4], r[5], qb_, pb_, r[3], r[10], r[7], sc, (900.0 - r[6]) / 60.0, r[1][NI["czb"]]))
        return out
    cells, tcells = {}, {}
    with multiprocessing.get_context("fork").Pool(4) as pool:
        for res in pool.imap(hour_table, range(int(t_start), int(t_end - 3600), 3600)):
            for seg, tk, sd, qb_, pb_, ask, path, won, sc, el, czb in res or ():
                if path[5] > 10.0: continue  # a stale quote overstates the dip that follows
                cells.setdefault((qb_, pb_, tk, sd), (seg, ask, path, won, sc))
                tb_ = 0 if el < 5.0 else 1 if el < 10.0 else 2 if el < 12.0 else 3
                tcells.setdefault((tb_, pb_, tk, sd), (seg, ask, path, won, sc, czb, qb_))
    def cl(g):
        n = len(g)
        if n < 15: return "n %4d (too few)" % n
        c = lambda xs: 100.0 * sum(xs) / n
        rise = [x[2][0] - x[1] for x in g]; dip60 = [x[1] - x[2][2] for x in g]; dipall = [x[1] - x[2][3] for x in g]; dd = [x[1] - x[2][1] for x in g]
        sh = lambda xs, k: 100.0 * sum(1 for v in xs if v >= k - 1e-9) / n
        return "n %4d score %.2f ask %.2f | bid later rose %+5.1fc over the ask (>=+5c %2.0f%% >=+10c %2.0f%%) | ask fell in 60 s: >=2c %2.0f%% >=5c %2.0f%%; by the close %4.1fc (>=5c %2.0f%%) | bid fell %4.1fc before its peak | won %2.0f%%" % (n, sum(x[4] for x in g) / n, sum(x[1] for x in g) / n, c(rise) , sh(rise, 0.05), sh(rise, 0.10), sh(dip60, 0.02), sh(dip60, 0.05), c(dipall), sh(dipall, 0.05), c(dd), c([x[3] for x in g]))
    print(f"\n######## SCORE (place in the market's own last 24 h, fifths: 1 lowest .. 5 highest) x PRICE BAND: what the price did next; {len(cells)} first readings")
    for pb_, (a, b) in enumerate(PB):
        print(f"\n--- ask {a:.2f}-{min(b, 1.0):.2f}")
        for qb_ in range(5):
            for seg, name in ((0, "h1"), (1, "h2")):
                g = [v for (q, p_, _, _), v in cells.items() if q == qb_ and p_ == pb_ and v[0] == seg]
                print(f"  score fifth {qb_ + 1} {name}: " + cl(g))
    # by TIME IN THE MARKET (minutes since the open), fresh quotes only (age <= 10 s); cells here are one first reading per market side per (time band, price band)
    TB = ((1.0, 5.0), (5.0, 10.0), (10.0, 12.0), (12.0, 15.0))
    print("\n######## BY TIME IN THE MARKET x PRICE BAND (quote age <= 10 s; score pooled)")
    for tb_, (a, b) in enumerate(TB):
        print(f"\n--- minutes {a:.0f}-{b:.0f}")
        for pb_, (pa, pz) in enumerate(PB):
            for seg, name in ((0, "h1"), (1, "h2")):
                g = [v for (k0, k1, _, _), v in tcells.items() if k0 == tb_ and k1 == pb_ and v[0] == seg]
                print(f"  ask {pa:.2f}-{min(pz, 1.0):.2f} {name}: " + cl(g))
    print("\n######## THE LAST 3 MINUTES (12-15) ONLY: PRICE BAND x SCORE FIFTH, then x CUSHION THIRD (the side's blended cushion, thirds cut on half 1)")
    late = [v for (k0, _, _, _), v in tcells.items() if k0 == 3]
    cz = sorted(v[5] for v in late if v[0] == 0)
    c3 = (cz[len(cz) // 3], cz[2 * len(cz) // 3]) if len(cz) > 3 else (0.0, 0.0)
    print(f"cushion cut points {c3[0]:+.2f} / {c3[1]:+.2f}")
    for pb_, (pa, pz) in enumerate(PB):
        for q5 in range(5):
            for seg, name in ((0, "h1"), (1, "h2")):
                g = [v for (k0, k1, _, _), v in tcells.items() if k0 == 3 and k1 == pb_ and v[0] == seg and v[6] == q5]
                if len(g) >= 15: print(f"  ask {pa:.2f}-{min(pz, 1.0):.2f} score fifth {q5 + 1} {name}: " + cl(g))
        for c_ in range(3):
            for seg, name in ((0, "h1"), (1, "h2")):
                g = [v for (k0, k1, _, _), v in tcells.items() if k0 == 3 and k1 == pb_ and v[0] == seg and (0 if v[5] <= c3[0] else 1 if v[5] <= c3[1] else 2) == c_]
                if len(g) >= 15: print(f"  ask {pa:.2f}-{min(pz, 1.0):.2f} cushion {('low', 'mid', 'high')[c_]} {name}: " + cl(g))
    print(f"done in {time.time() - t0:.0f}s")
    raise SystemExit(0)

picks = {top: {} for top in TOPS}
bars = {top: [[], [], []] for top in TOPS}
sizes, lams = [], []
with multiprocessing.get_context("fork").Pool(4) as pool:
    for res in pool.imap(hour, range(int(t_start), int(t_end - 3600), 3600)):
        if not res: continue
        t, ntr, nmk, lam, out = res
        sizes.append((ntr, nmk)); lams.append(lam)
        for top, tk, sd, seg, net, sl, cv, ph, won, cut, ask in out:
            if (tk, sd) not in picks[top]:
                picks[top][(tk, sd)] = (seg, net, sl, cv, ph, won, ask)
                bars[top][seg].append(cut)
slv = sorted(r[1][NI["sl30"]] for r in cm[::7]); cvv = sorted(r[1][NI["cv30"]] for r in cm[::7])
c_sl = (slv[len(slv) // 3], slv[2 * len(slv) // 3]); c_cv = (cvv[len(cvv) // 3], cvv[2 * len(cvv) // 3])
def th(x, c): return 0 if x < c[0] else 1 if x < c[1] else 2
# win-rate baseline per segment: the first in-band gated reading per market side
_seen, _base, _bax, _bnet = set(), {0: [], 1: [], 2: []}, {0: [], 1: [], 2: []}, {0: [], 1: [], 2: []}
for r in cm:
    if not inb(r) or (r[4], r[5]) in _seen: continue
    _seen.add((r[4], r[5]))
    _sg = 2 if r[0] >= t_hold else (0 if r[0] < t_mid else 1)
    _base[_sg].append(r[7]); _bax[_sg].append(r[3]); _bnet[_sg].append(r[2][0])
BASE = {k: (sum(v) / len(v) if v else 0.0) for k, v in _base.items()}
def cell(g, seg=None):
    if V in ("winp", "winp0", "cushonly"):
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
if V in ("winp", "winp0", "cushonly"):
    # VALUE CHECK: a SEPARATE step after the risk score (user, 2026-11-21: the price test comes after we evaluate risk, never inside the score or the universe). Per pick set: mean ask paid, win minus mean ask, net per $1 after the fee (buy fee; held to settlement), ask mix against the universe's.
    def ms(xs):
        n = len(xs); m = sum(xs) / n if n else 0.0
        se = math.sqrt(sum((x - m) ** 2 for x in xs) / max(1, n - 1) / max(1, n))
        return m, se
    def mix(asks):
        e = ((0.0, 0.40), (0.40, 0.60), (0.60, 0.85), (0.85, 1.01))
        return " ".join(f"{lo:.2f}-{min(hi, 1.0):.2f}:{sum(1 for a in asks if lo <= a < hi) / max(1, len(asks)):.2f}" for lo, hi in e)
    print("\n######## VALUE CHECK (after the risk score; the ask is NOT in the score)")
    for seg, name in ((0, "half 1"), (1, "half 2")):  # never the holdout: it is scored for the win chance only
        ua, un = _bax[seg], _bnet[seg]
        mn, sn = ms(un)
        print(f" {name} UNIVERSE: n {len(ua)} mean ask {sum(ua) / max(1, len(ua)):.3f} net/$1 {mn:+.3f} se {sn:.3f} ({mn / sn if sn else 0:+.1f} se) mix {mix(ua)}")
        for top in TOPS:
            g = [x for x in picks[top].values() if x[0] == seg]
            if not g: continue
            ga = [x[6] for x in g]
            wm, ws = ms([x[5] - x[6] for x in g])
            nm, ns = ms([x[1] for x in g])
            print(f"   top {int(top * 100):2d}%: n {len(g)} mean ask {sum(ga) / len(ga):.3f} | win minus ask {wm:+.3f} se {ws:.3f} ({wm / ws if ws else 0:+.1f} se) | net/$1 {nm:+.3f} se {ns:.3f} ({nm / ns if ns else 0:+.1f} se) | mix {mix(ga)}")
print(f"done in {time.time() - t0:.0f}s")
