"""WIDER ASK, PERP FEED (2026-11-20, user: "why are you asking 20-45c? those are obviously going to be losers more often"): asks 0.20-0.80, targets +25/+40/+70%, commodities on Kalshi's perp reference. The first run (scripts/early_swing_targets.py): net per $1 -0.134/-0.124/-0.102 at +40/+70/+100%, worse the cheaper the ask.
The early cheap-side swing: buy a cheap side in the first minutes of a 15-minute market and rest a sell at +40%, +70% or +100%; otherwise hold to settlement. How often does it hit, what does it net, and in which curve shapes?
Question (user, 2026-11-20, on KXGOLD15M-26NOV200930: "this kind of bet is what I've been trying to automate. It's so easy to find a market in the early stages and make a quick 40-100% profit"): the market's YES side was 27-29 cents at 09:15-09:17 ET and 51-58 cents by 09:19-09:20. Every market of every series the tape holds a price feed for (crypto: Kalshi's settlement index, commodity: the feed the lane reads, against its own open price), every 30 s from 60 to 240 s after the open (the early stage), both sides, asks 0.20-0.45. For each, a resting sell at ask x (1 + g) for g in 0.40, 0.70, 1.00 fills at that price when the side's bid first reaches it before 15 s left (fee on the buy and on the sale); a miss is held to settlement (no sale fee). Reported with the first reading per market side only (one bet per side per market): n, hit rate, net per $1 for each g, and the same sides held to settlement; then by third of the curve's own readings, side-oriented: cushion (cz), its distance from the strike (|cz|), path efficiency (eff5), speed-up (vr), 15-minute range (amp15), and by second after the open and by ask band. Thirds are the 33rd and 67th percentiles of every reading, fixed before the run; g and the ask range are the user's, stated in the question. No asset is named; markets are pooled and described by their curves. A reading in a third is a rule only if its net is positive in every third it claims (CLAUDE.md rule 4).
Run: SPOT=0 scripts/cloud_job.sh scripts/early_swing_targets.py"""
import bisect, math, multiprocessing, time
from datetime import datetime, timezone
from kalshi_moonshot import binaryrisk as br, config, db
from kalshi_moonshot.perpref import index_for_series as bq_index  # Kalshi's perp reference, not BiQuote
from kalshi_moonshot.kalshiws import index_for_series as crypto_index
from kalshi_moonshot.strategy import fee_per_contract as fee

t0 = time.time()
s = config.reload()
START = datetime(2026, 11, 9, 5, tzinfo=timezone.utc).timestamp()  # Monday 11-09: the logic rewrite
GS = (0.25, 0.40, 0.70)
AGES = tuple(range(60, 241, 30))
ASK = (0.20, 0.80)  # widened 2026-11-20 (user: 20-45c is the weak end); the first run's band table showed the loss shrinking as the ask rises
NI = {n: i for i, n in enumerate(br.NAMES)}
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
        for age in AGES:
            t = opened + age
            j = bisect.bisect_right(qt, t) - 1
            if j < 0 or t - qt[j] > 30.0: continue
            f = br.readings(T, V, t, strike, close - t)
            if not f: continue
            r1, r3 = recent.yes(t, 1.0), recent.yes(t, 3.0)
            end = bisect.bisect_right(qt, close - 15.0)
            for side, ask in (("yes", qa[j]), ("no", 1.0 - qb[j])):
                if not ASK[0] <= ask < ASK[1]: continue
                won = won_yes if side == "yes" else not won_yes
                hold = ((1.0 if won else 0.0) - ask - fee(ask)) / ask
                nets = []
                for g in GS:
                    target = ask * (1.0 + g)
                    if target >= 0.99:
                        nets.append(hold); continue  # cannot rest a sell at or above 0.99: nothing to take
                    hit = False
                    for m in range(j + 1, end):
                        bid_side = qb[m] if side == "yes" else 1.0 - qa[m]
                        if bid_side >= target: hit = True; break
                    nets.append((target - fee(target) - ask - fee(ask)) / ask if hit else hold)
                    if hit: nets[-1] = ("hit", nets[-1])
                out.append((ticker, side, age, ask, hold, nets, br.side_vector(f, side, ask, r1, r3)))
    return out

with multiprocessing.get_context("fork").Pool(4) as pool:
    R = [x for part in pool.imap_unordered(build, list(jobs.items())) for x in part]
print(f"{len(R)} early cheap-side readings from {len({r[0] for r in R})} markets, {time.time() - t0:.0f}s", flush=True)
first = {}
for r in sorted(R, key=lambda r: (r[0], r[1], r[2])):
    first.setdefault((r[0], r[1]), r)
P = list(first.values())
print(f"{len(P)} market sides (first early reading each)\n")
def parts(r):
    hits = [isinstance(x, tuple) for x in r[5]]
    return hits, [x[1] if isinstance(x, tuple) else x for x in r[5]]
def cell(g):
    if not g: return "n 0"
    n = len(g); hh = [parts(r) for r in g]
    hit = " ".join(f"{sum(h[0][k] for h in hh) / n:.2f}" for k in range(3))
    net = " ".join(f"{sum(h[1][k] for h in hh) / n:+.3f}" for k in range(3))
    return f"n {n:5d} hit {hit} | net per $1 at +25/+40/+70%: {net} | held {sum(r[4] for r in g) / n:+.3f}"
print("ALL (hit rate and net per $1 at +40% / +70% / +100%)\n  " + cell(P))
IDX = [("cushion (cz)", NI["cz"] if False else 2), ("path efficiency", 5), ("speed-up", 6), ("15-min range", 7)]
cuts = {}
for nm, i in IDX + [("|cushion|", -1)]:
    v = sorted((abs(r[6][2]) if i == -1 else r[6][i]) for r in P)
    cuts[nm] = (v[len(v) // 3], v[2 * len(v) // 3])
def third(r, nm, i):
    x = abs(r[6][2]) if i == -1 else r[6][i]; lo, hi = cuts[nm]
    return 0 if x < lo else 1 if x < hi else 2
print("\nBY THE CURVE (thirds of every reading, side-oriented):")
for nm, i in IDX + [("|cushion|", -1)]:
    for q, tn in enumerate(("low", "mid", "high")):
        print(f"  {nm:16s} {tn:4s}: " + cell([r for r in P if third(r, nm, i) == q]))
print("\nBY SECONDS AFTER THE OPEN (all readings of that age, one per market side):")
for age in AGES:
    seen = {}
    for r in R:
        if r[2] == age: seen.setdefault((r[0], r[1]), r)
    print(f"  {age:3d}s: " + cell(list(seen.values())))
print("\nBY ASK BAND:")
for lo, hi in ((0.20, 0.30), (0.30, 0.38), (0.38, 0.45), (0.45, 0.55), (0.55, 0.65), (0.65, 0.80)):
    print(f"  {lo:.2f}-{hi:.2f}: " + cell([r for r in P if lo <= r[3] < hi]))
print(f"done in {time.time() - t0:.0f}s")
