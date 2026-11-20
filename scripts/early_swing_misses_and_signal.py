"""The early cheap-side swing, part 2 (follow-up to scripts/early_swing_targets.py, which found hit 67/55/47% at +40/+70/+100% but net -0.13/-0.12/-0.10 per $1, every curve third negative).
Question 1: do the misses cost less under a TIME-SCALED sale? Same bets (first early reading per market side, ask 0.20-0.45, 60-240 s after the open, either side, fee on both legs). Exits, all scaled to the time left at the buy (u = share of that time elapsed), none a loss line and none a fixed number of seconds:
  hold: settlement. rest: resting sell at ask x 2 (+100%), else held. timed f: the +100% rest, and if not filled by u = f (f = 0.25, 0.5, 0.75) sell at the bid then. decay g: a resting sell whose gain falls linearly from g to 0 as u goes 0 to 1 (target = ask x (1 + g(1-u)), never under ask + 2 fees + 1c), else held; g = 1.0 and 0.7.
Question 2: does a curve-only signal pick the sides that hit? Ridge logistic on the 16 side-view readings of binaryrisk.py (label: the +100% sell fills), refit every 6 hours on the earlier 72 hours of markets (closed before the refit), scored on the next hours' first-reading bets; a bet is taken when its predicted hit chance is in the top 10% / 25% of the training readings'. Test hours from 3 days after the start, split into two halves (first half chooses what to look at, second confirms). Reported: hit rate and net per $1 under each exit, per half, and by predicted-hit decile. A reading is a rule only if the net is positive in both halves and in every third of each curve reading it claims (CLAUDE.md rule 4). Slice shares, f, g and the 72 h / 6 h windows are stated here before the run.
Run: SPOT=0 scripts/cloud_job.sh scripts/early_swing_misses_and_signal.py"""
import bisect, math, multiprocessing, time
from datetime import datetime, timezone
from kalshi_moonshot import binaryrisk as br, config, db
from kalshi_moonshot.biquotefeed import index_for_series as bq_index
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
            for side, ask in (("yes", qa[j]), ("no", 1.0 - qb[j])):
                if not ASK[0] <= ask < ASK[1]: continue
                won = won_yes if side == "yes" else not won_yes
                hold = ((1.0 if won else 0.0) - ask - fee(ask)) / ask
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
                out.append((ticker, side, age, ask, close, t, hit, nets, br.side_vector(f, side, ask, r1, r3)))
    return out

with multiprocessing.get_context("fork").Pool(4) as pool:
    R = [x for part in pool.imap_unordered(build, list(jobs.items())) for x in part]
print(f"{len(R)} early cheap-side readings from {len({r[0] for r in R})} markets, {time.time() - t0:.0f}s", flush=True)
R.sort(key=lambda r: r[5])
first = {}
for r in sorted(R, key=lambda r: (r[0], r[1], r[2])): first.setdefault((r[0], r[1]), r)
P = sorted(first.values(), key=lambda r: r[5])

def cell(g):
    if not g: return "n 0"
    n = len(g)
    return f"n {n:5d} hit {sum(r[6] for r in g) / n:.2f} | net per $1: " + " ".join(f"{EX[k]} {sum(r[7][k] for r in g) / n:+.3f}" for k in range(len(EX)))
print(f"\nQUESTION 1: ALL {len(P)} market sides, exits scaled to the time left\n  " + cell(P))
for lo, hi in ((0.20, 0.30), (0.30, 0.38), (0.38, 0.45)):
    print(f"  ask {lo:.2f}-{hi:.2f}: " + cell([r for r in P if lo <= r[3] < hi]))

# QUESTION 2
NF = len(br.NAMES)
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
        d = br._solve(A, b)
        w = [a + c for a, c in zip(w, d)]
    return mu, sd, w
def prob(m, x):
    mu, sd, w = m
    z = w[0] + sum(w[i + 1] * (x[i] - mu[i]) / sd[i] for i in range(NF))
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))
TEST0 = START + 3 * 86400
tmax = P[-1][5]
sel = {0.10: [], 0.25: []}; allp = []
h = TEST0; last = None
while h < tmax:
    tr = [r for r in R if h - 72 * 3600 <= r[4] <= h][::2]
    if len(tr) >= 3000:
        m = fitlog(tr); last = m
        ps = sorted(prob(m, r[8]) for r in tr)
        bars = {sh: ps[int(len(ps) * (1 - sh))] for sh in sel}
        for r in P:
            if h <= r[5] < h + 6 * 3600:
                p = prob(m, r[8]); allp.append((p, r))
                for sh in sel:
                    if p >= bars[sh]: sel[sh].append(r)
    h += 6 * 3600
print(f"\nQUESTION 2: walk-forward logistic on the +100% hit, {len(allp)} scored bets (test from 3 days after the start)")
if last: print("  last model weights (standardised, side view): " + " ".join(f"{n} {last[2][i + 1]:+.2f}" for i, n in enumerate(br.NAMES)))
if allp:
    mid = sorted(r[5] for _, r in allp)[len(allp) // 2]
    H = lambda g, half: [r for r in g if (r[5] < mid) == (half == 1)]
    base = [r for _, r in allp]
    for nm, g in [("ALL scored bets", base)] + [(f"top {int(sh * 100)}% by predicted hit", sel[sh]) for sh in sel]:
        for half in (1, 2): print(f"  {nm:28s} half {half}: " + cell(H(g, half)))
    print("  by predicted-hit decile (all test bets, both halves; 0 = lowest):")
    ap = sorted(allp, key=lambda x: x[0]); n = len(ap)
    for d in range(10): print(f"    decile {d}: " + cell([r for _, r in ap[d * n // 10:(d + 1) * n // 10]]))
    IDX = [("cushion (cz)", 2), ("path efficiency", 5), ("speed-up", 6), ("15-min range", 7)]
    for sh in sel:
        g = sel[sh]
        if len(g) < 30: continue
        print(f"  top {int(sh * 100)}% by curve third (cut at the thirds of the scored bets), both halves pooled:")
        for nm, i in IDX:
            v = sorted(r[8][i] for r in base); lo, hi = v[len(v) // 3], v[2 * len(v) // 3]
            for q, tn in enumerate(("low", "mid", "high")):
                print(f"    {nm:16s} {tn:4s}: " + cell([r for r in g if (0 if r[8][i] < lo else 1 if r[8][i] < hi else 2) == q]))
print(f"done in {time.time() - t0:.0f}s")
