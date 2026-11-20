"""The early cheap-side swing PER PRICE FEED (user, 2026-11-20: the pooled result is negative, but "this should still fit to a commodity lane, maybe not all of them because they are branched into categories for a reason"). Same bets and exits as scripts/early_swing_misses_and_signal.py (first early reading per market side, ask 0.20-0.45, 60-240 s after the open, fee both legs; resting sell at +100%, else held; also timed and decaying exits), reported per price feed as a LABEL only (CLAUDE.md rule 1: no rule keys on it), with each feed's typical curve measures (mean 15-min range, path efficiency, speed-up, |cushion|, and the share of readings with a typical minute under each feed's own median) so the shape that separates the feeds can be named. Each feed is split in two halves by time; a feed is worth a shape study only if positive in both. Run: SPOT=0 scripts/cloud_job.sh scripts/early_swing_by_feed.py"""
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
                out.append((ticker, side, age, ask, close, t, hit, nets, br.side_vector(f, side, ask, r1, r3), ix))
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
    return f"n {n:5d} hit {sum(r[6] for r in g) / n:.2f} | " + " ".join(f"{EX[k]} {sum(r[7][k] for r in g) / n:+.3f}" for k in (0, 1, 2, 3, 4, 5))
mean = lambda g, i: sum(r[8][i] for r in g) / len(g)
by = {}
for r in P: by.setdefault(r[9], []).append(r)
print(f"{len(P)} market sides on {len(by)} feeds; exits: " + ", ".join(EX[:6]))
for ix, g in sorted(by.items(), key=lambda kv: -sum(r[7][1] for r in kv[1]) / len(kv[1])):
    if len(g) < 30: continue
    mid = g[len(g) // 2][5]
    print(f"\n{ix}: " + cell(g) + f"\n   curve: amp15 {mean(g, 7):.1f} eff5 {mean(g, 5):.2f} vr {mean(g, 6):+.2f} |cz| {sum(abs(r[8][2]) for r in g) / len(g):.2f} ask {sum(r[3] for r in g) / len(g):.2f}")
    print("   half 1: " + cell([r for r in g if r[5] < mid]) + "\n   half 2: " + cell([r for r in g if r[5] >= mid]))
print(f"done in {time.time() - t0:.0f}s")
