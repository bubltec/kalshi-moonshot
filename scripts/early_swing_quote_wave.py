"""The early cheap-side swing with the QUOTE'S OWN WAVE as the signal (user, 2026-11-20: the unselected bet loses exactly what the fees say (a fair market reaches +100% from 0.37 half the time, fee alone costs 5-9 cents per $1), so "this should still fit" must come from WHICH market and WHICH moment; the hand trade buys the cheap side after its price has fallen in the first minutes and sells the rebound: 27-29c at 09:15-09:17, 51-58c by 09:19-09:20 on KXGOLD15M-26NOV200930). Spot is Kalshi's: a coin's settlement index, a commodity's perp reference (no BiQuote).
Per reading (60-240 s after the open, every 30 s, either side, ask 0.20-0.45) the side's own quote path since the open gives seven readings, all ratios, no price in cents as a rule: drop (its peak so far to the ask, over the peak), from_open (its open level to the ask, over the open level), bounce (where the ask sits between its trough and peak so far, 0 trough, 1 peak), speed30 (its last 30 s change over its range so far), trough_recent (how recent the trough is, over the time since the open), rng_rel (its range over its open level), chop (its path length over its range). They are added to the 16 spot readings of binaryrisk.py. Label: the resting sell at +100% fills (else held). Ridge logistic, refit every 6 h on the earlier 72 h of markets, test from 3 days after the start in two halves; a bet is taken the first time in a market side that the predicted hit chance is in the top 10% / 25% of the training readings'; exits as in scripts/early_swing_misses_and_signal.py. Also univariate thirds of each quote reading over all readings (readings of a market are correlated: read the sign and the gradient, not the n). Rule 4: positive in both halves and every third claimed. Windows, shares and refits are stated before the run.
Run: SPOT=0 scripts/cloud_job.sh scripts/early_swing_quote_wave.py"""
import bisect, math, multiprocessing, time
from datetime import datetime, timezone
from kalshi_moonshot import binaryrisk as br, config, db
from kalshi_moonshot.perpref import index_for_series as bq_index  # Kalshi's perp reference, not BiQuote
from kalshi_moonshot.kalshiws import index_for_series as crypto_index
from kalshi_moonshot.strategy import fee_per_contract as fee

t0 = time.time()
s = config.reload()
START = datetime(2026, 11, 9, 5, tzinfo=timezone.utc).timestamp()
AGES = tuple(range(60, 241, 30))
ASK = (0.20, 0.45)
FS = (0.25, 0.5, 0.75)
EX = ["hold", "rest+100"] + [f"timed{int(f * 100)}" for f in FS] + ["decay+100", "decay+70"]
st = db.connect(s.db_path, "main", shared=True)
allw = st.execute("SELECT close_ts, result FROM windows WHERE result IN ('yes','no') AND close_ts IS NOT NULL").fetchall()
recent = br.Recent([(float(r["close_ts"]), 1 if r["result"] == "yes" else 0) for r in allw])
wins = st.execute("SELECT ticker, series, close_ts, strike, result FROM windows WHERE result IN ('yes','no') AND close_ts > ? ORDER BY close_ts", (START + 3600 * 2,)).fetchall()
jobs = {}
for w in wins:
    ix = crypto_index(w["series"]); kind = "crypto" if ix else "commodity"
    if not ix: ix = bq_index(w["series"])
    if ix: jobs.setdefault(ix, []).append((w["ticker"], float(w["close_ts"]), float(w["strike"]) if kind == "crypto" and w["strike"] else None, w["result"] == "yes"))
print(f"{len(wins)} settled windows on {len(jobs)} price feeds", flush=True)

def net(sale, ask): return (sale - fee(sale) - ask - fee(ask)) / ask

def build(job):
    ix, ws = job
    tape = db.connect(config.reload().tape_path, "tape", shared=True)
    tk = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (ix, min(w[1] for w in ws) - 4900, max(w[1] for w in ws) + 5)).fetchall()
    T, V = [float(r[0]) for r in tk], [float(r[1]) for r in tk]; del tk
    out = []
    for ticker, close, strike, won_yes in ws:
        opened = close - 900.0
        if strike is None:
            k = bisect.bisect_left(T, opened)
            if k >= len(T) or T[k] - opened > 25.0: continue
            strike = V[k]
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (ticker, opened - 60, close)).fetchall()
        if len(q) < 20: continue
        qt, qb, qa = [float(r[0]) for r in q], [float(r[1]) for r in q], [float(r[2]) for r in q]
        scale = 100.0 if max(qa) > 1.5 else 1.0
        qb = [x / scale for x in qb]; qa = [x / scale for x in qa]
        end = bisect.bisect_right(qt, close - 15.0)
        for age in AGES:
            t = opened + age
            j = bisect.bisect_right(qt, t) - 1
            if j < 0 or t - qt[j] > 30.0: continue
            f = br.readings(T, V, t, strike, close - t)
            if not f: continue
            r1, r3 = recent.yes(t, 1.0), recent.yes(t, 3.0)
            left = close - 15.0 - t
            k0 = bisect.bisect_left(qt, opened)
            if j - k0 < 8: continue
            for side, ask in (("yes", qa[j]), ("no", 1.0 - qb[j])):
                if not ASK[0] <= ask < ASK[1]: continue
                won = won_yes if side == "yes" else not won_yes
                hold = ((1.0 if won else 0.0) - ask - fee(ask)) / ask
                if side == "yes": mid = [(qb[m] + qa[m]) / 2 for m in range(k0, j + 1)]
                else: mid = [1.0 - (qb[m] + qa[m]) / 2 for m in range(k0, j + 1)]
                pk, tr, op = max(mid), min(mid), mid[0]
                rg = pk - tr
                tj = k0 + mid.index(tr)
                k30 = bisect.bisect_left(qt, t - 30.0, k0, j + 1)
                path = sum(abs(mid[i + 1] - mid[i]) for i in range(len(mid) - 1))
                qf = [(pk - ask) / pk, (op - ask) / op, (ask - tr) / rg if rg > 0 else 0.0, (mid[-1] - mid[k30 - k0]) / max(rg, 0.01), (t - qt[tj]) / max(t - opened, 1.0), rg / op, path / rg if rg > 0 else 0.0]
                bids = [(qt[m], qb[m] if side == "yes" else 1.0 - qa[m]) for m in range(j + 1, end)]
                def rest(g, decay, until=None):
                    for tm, b in bids:
                        u = (tm - t) / left
                        if until is not None and u >= until: return None, b  # not filled; the bid at the timed exit
                        tg = ask * (1.0 + g * (1.0 - u)) if decay else ask * (1.0 + g)
                        if decay: tg = max(tg, ask + 2 * fee(ask) + 0.01)
                        if tg < 0.99 and b >= tg: return tg, None
                    return None, None
                nets = [hold]
                tg, _ = rest(1.0, False)
                nets.append(net(tg, ask) if ask * 2 < 0.99 and tg else hold)
                hit = bool(tg) and ask * 2 < 0.99
                for fr in FS:
                    tg, b = rest(1.0, False, fr)
                    if ask * 2 >= 0.99: nets.append(hold)
                    elif tg: nets.append(net(tg, ask))
                    elif b is not None: nets.append(net(b, ask) if b > 0 else -1.0 - fee(ask) / ask)
                    else: nets.append(hold)
                for g in (1.0, 0.7):
                    tg, _ = rest(g, True)
                    nets.append(net(tg, ask) if tg else hold)
                out.append((ticker, side, age, ask, close, t, hit, nets, br.side_vector(f, side, ask, r1, r3) + qf))
    return out

with multiprocessing.get_context("fork").Pool(4) as pool:
    R = [x for part in pool.imap_unordered(build, list(jobs.items())) for x in part]
print(f"{len(R)} early cheap-side readings from {len({r[0] for r in R})} markets, {time.time() - t0:.0f}s", flush=True)

R.sort(key=lambda r: r[5])
QN = ["drop", "from_open", "bounce", "speed30", "trough_recent", "rng_rel", "chop"]
NAMES = list(br.NAMES) + QN
NF = len(NAMES)
def cell(g):
    if not g: return "n 0"
    n = len(g)
    return f"n {n:5d} hit {sum(r[6] for r in g) / n:.2f} | " + " ".join(f"{EX[k]} {sum(r[7][k] for r in g) / n:+.3f}" for k in (0, 1, 2, 5))
print(f"\nALL readings: " + cell(R))
print("UNIVARIATE: thirds of each quote reading over all readings (correlated within a market)")
for qi, nm in enumerate(QN):
    v = sorted(r[8][len(br.NAMES) + qi] for r in R); lo, hi = v[len(v) // 3], v[2 * len(v) // 3]
    for q, tn in enumerate(("low", "mid", "high")):
        print(f"  {nm:14s} {tn:4s} (cuts {lo:+.2f}/{hi:+.2f}): " + cell([r for r in R if (0 if r[8][len(br.NAMES) + qi] < lo else 1 if r[8][len(br.NAMES) + qi] < hi else 2) == q]))
def fitlog(rows):
    mu = [sum(r[8][i] for r in rows) / len(rows) for i in range(NF)]
    sd = [math.sqrt(sum((r[8][i] - mu[i]) ** 2 for r in rows) / len(rows)) or 1.0 for i in range(NF)]
    X = [[1.0] + [(r[8][i] - mu[i]) / sd[i] for i in range(NF)] for r in rows]
    y = [1.0 if r[6] else 0.0 for r in rows]
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
tmax = R[-1][5]
sel = {0.10: [], 0.25: []}; scored = []; last = None; taken = {sh: set() for sh in sel}
h = TEST0
while h < tmax:
    tr = [r for r in R if h - 72 * 3600 <= r[4] <= h][::2]
    if len(tr) >= 3000:
        m = fitlog(tr); last = m
        ps = sorted(prob(m, r[8]) for r in tr)
        bars = {sh: ps[int(len(ps) * (1 - sh))] for sh in sel}
        for r in R:
            if h <= r[5] < h + 6 * 3600:
                p = prob(m, r[8]); scored.append((p, r))
                for sh in sel:
                    key = (r[0], r[1])
                    if p >= bars[sh] and key not in taken[sh]:
                        taken[sh].add(key); sel[sh].append(r)
    h += 6 * 3600
print(f"\nWALK-FORWARD: {len(scored)} scored readings; a bet is the first reading of a market side over the bar")
if last: print("  last weights: " + " ".join(f"{n} {last[2][i + 1]:+.2f}" for i, n in enumerate(NAMES)))
if scored:
    mid_t = sorted(r[5] for _, r in scored)[len(scored) // 2]
    first = {}
    for _, r in sorted(scored, key=lambda x: x[1][5]): first.setdefault((r[0], r[1]), r)
    base = list(first.values())
    H = lambda g, half: [r for r in g if (r[5] < mid_t) == (half == 1)]
    for nm, g in [("first reading, no signal", base)] + [(f"top {int(sh * 100)}% by predicted hit", sel[sh]) for sh in sel]:
        for half in (1, 2): print(f"  {nm:26s} half {half}: " + cell(H(g, half)))
    ap = sorted(scored, key=lambda x: x[0]); n = len(ap)
    print("  by predicted-hit decile (all scored readings):")
    for d in range(10): print(f"    decile {d}: " + cell([r for _, r in ap[d * n // 10:(d + 1) * n // 10]]))
    for sh in sel:
        g = sel[sh]
        if len(g) < 30: continue
        print(f"  top {int(sh * 100)}% by quote-wave third (both halves pooled):")
        for qi, nm in enumerate(QN):
            ix = len(br.NAMES) + qi
            v = sorted(r[8][ix] for r in R); lo, hi = v[len(v) // 3], v[2 * len(v) // 3]
            for q, tn in enumerate(("low", "mid", "high")):
                print(f"    {nm:14s} {tn:4s}: " + cell([r for r in g if (0 if r[8][ix] < lo else 1 if r[8][ix] < hi else 2) == q]))
print(f"done in {time.time() - t0:.0f}s")
