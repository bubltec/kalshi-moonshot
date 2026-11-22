"""RISK AS CHECKS: the scoreboard (2026-11-21, user: the plan is P(risk(P)); "risk should be calculating a lot of things that tell us yes/no so I don't think a single score makes sense anymore"). No blended score, no fit, no comparison with Kalshi's odds, nothing held to settlement as the test.
QUESTION: does each check, read alone, separate good buys from bad on the SAME bets?
THE BETS: commodity perp markets (S&P out, Kalshi perp reference only), each market side's FIRST reading with a fresh quote (age <= 10 s) and an ask in the lane range 0.60-0.85 (the user's example), bought at the ask; and separately the first such reading in the LAST 3 MINUTES (minutes 12-15). Readings every 30 s from 1 minute after the open to 20 s before the close; no cushion gate and no time gate.
THE CHECKS (each in the side's view, each in the market's own units):
  cushion   the blended 1/3/5-minute spot against the strike, in typical minutes x sqrt(minutes left) (binaryrisk czb)
  trend     the 1-minute average against the 3-minute one, in typical moves (tr13)
  history   the share of the last 30 minutes the spot spent on the side's winning side of the strike
  volatility the 15-minute typical minute over the hour's (vr; not signed)
  price     Kalshi's own price curve (the inner P): the side's mid now against 3 minutes ago, in the PRICE's own typical minute (the RMS 5-second mid change of the last 5 minutes x sqrt(12)) x sqrt(3)
  pricedip  how far the side's mid sits under its high of the last 5 minutes, in the price's own typical minutes
STATE of a check at a bet (CLAUDE.md rules 2 and 3): from that market series' own readings of the previous 24 hours (both sides, every reading; 100 needed): ANOMALY beyond its 1st/99th percentile; YES in its top third; NO in its bottom third; else MID; UNREAD when the history is short.
WHAT THE BUY GOT (from the side's bid every 5 s after the buy to 15 s before the close): how often the bid later went 5c and 10c over the price paid, how far it fell before its peak, how often it never got 1c over the price paid, and the share that won. Halves by close time.
Run: SPOT=0 scripts/cloud_job.sh scripts/binary_risk_checks.py"""
import bisect, math, multiprocessing, time
from datetime import datetime, timezone
from kalshi_moonshot import binaryrisk as br, config, db
from kalshi_moonshot.perpref import index_for_series as perp_index

t0 = time.time()
START = datetime(2026, 11, 9, 5, tzinfo=timezone.utc).timestamp()
LO, HI = 0.60, 0.85
CHECKS = ("cushion", "trend", "history", "volatility", "price", "pricedip")
s = config.reload()
st = db.connect(s.db_path, "main", shared=True)
wins = st.execute("SELECT ticker, series, close_ts, result FROM windows WHERE result IN ('yes','no') AND close_ts > ? ORDER BY close_ts", (START + 7200,)).fetchall()
jobs = {}
for w in wins:
    if w["series"].startswith("KXINX"): continue
    ix = perp_index(w["series"])
    if ix: jobs.setdefault(ix, []).append((w["ticker"], w["series"], float(w["close_ts"]), w["result"] == "yes"))

def build(job):
    ix, ws = job
    tape = db.connect(config.reload().tape_path, "tape", shared=True)
    tk = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (ix, min(w[2] for w in ws) - 4900, max(w[2] for w in ws) + 5)).fetchall()
    T, Vv = [float(r[0]) for r in tk], [float(r[1]) for r in tk]
    del tk
    hist, bets = [], []
    for ticker, series, close, won_yes in ws:
        opened = close - 900.0
        k = bisect.bisect_left(T, opened)
        if k >= len(T) or T[k] - opened > 25.0: continue
        strike = Vv[k]
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (ticker, opened - 60, close)).fetchall()
        if len(q) < 20: continue
        qt, qb, qa = [float(r[0]) for r in q], [float(r[1]) for r in q], [float(r[2]) for r in q]
        scale = 100.0 if max(qa) > 1.5 else 1.0
        cs = [0]
        i0 = bisect.bisect_left(T, opened - 1900.0); i1 = bisect.bisect_right(T, close)
        for v in Vv[i0:i1]: cs.append(cs[-1] + (1 if v > strike else 0))
        def mid_at(x):
            j = bisect.bisect_right(qt, x) - 1
            return None if j < 0 else (qb[j] + qa[j]) / (2.0 * scale)
        done = {}
        t = opened + 60.0
        while t <= close - 20.0:
            j = bisect.bisect_right(qt, t) - 1
            f = br.readings(T, Vv, t, strike, close - t) if j >= 0 else None
            if f is not None:
                age = t - qt[j]
                yb, ya = qb[j] / scale, qa[j] / scale
                je = bisect.bisect_right(T, t) - 1
                ia = max(i0, bisect.bisect_left(T, t - 1800.0))
                share = (cs[je + 1 - i0] - cs[ia - i0]) / max(1, je + 1 - ia)
                # Kalshi's own price curve: the YES mid on a 5-second grid over the last 5 minutes
                grid = [mid_at(t - 5.0 * g) for g in range(60, -1, -1)]
                grid = [x for x in grid if x is not None]
                pt = pd = None
                if len(grid) >= 25:
                    ch = [b - a for a, b in zip(grid, grid[1:])]
                    pu = math.sqrt(sum(c * c for c in ch) / len(ch) * 12.0)
                    m3 = mid_at(t - 180.0)
                    if pu > 0 and m3 is not None:
                        pt = (grid[-1] - m3) / (pu * math.sqrt(3.0))
                        pd = ((max(grid) - grid[-1]) / pu, (grid[-1] - min(grid)) / pu)  # YES under its high; NO under its high = YES over its low
                el = (t - opened) / 60.0
                for side, ask in (("yes", ya), ("no", 1.0 - yb)):
                    sg = 1.0 if side == "yes" else -1.0
                    rd = {"cushion": sg * f["czb"], "trend": sg * f["tr13"], "history": share if side == "yes" else 1.0 - share, "volatility": f["vr"],
                          "price": None if pt is None else sg * pt, "pricedip": None if pd is None else -(pd[0] if side == "yes" else pd[1])}  # pricedip is negated so that HIGH = near its recent high (not dipping)
                    hist.append((t, series, rd))
                    if age > 10.0 or not LO <= ask <= HI: continue
                    for tag in ("any", "late"):
                        if (side, tag) in done or (tag == "late" and el < 12.0): continue
                        j0, j1 = bisect.bisect_right(qt, t), bisect.bisect_right(qt, close - 15.0)
                        bids = [(qb[x] / scale if side == "yes" else 1.0 - qa[x] / scale) for x in range(j0, j1)]
                        if not bids: continue
                        kp = max(range(len(bids)), key=bids.__getitem__)
                        won = won_yes if side == "yes" else not won_yes
                        done[(side, tag)] = 1
                        bets.append((t, close, series, tag, ask, bids[kp] - ask, ask - min(bids[:kp + 1]), 1.0 if won else 0.0, el, rd))
            t += 30.0
    return hist, bets

H, B = {}, []
with multiprocessing.get_context("fork").Pool(4) as pool:
    for hist, bets in pool.imap_unordered(build, list(jobs.items())):
        for t, series, rd in hist: H.setdefault(series, []).append((t, rd))
        B.extend(bets)
for v in H.values(): v.sort(key=lambda x: x[0])
HT = {k: [x[0] for x in v] for k, v in H.items()}
B.sort(key=lambda b: b[1])
print(f"{sum(len(v) for v in H.values())} readings on {len(H)} series; {sum(1 for b in B if b[3] == 'any')} bets (first in range), {sum(1 for b in B if b[3] == 'late')} bets in the last 3 minutes; {time.time() - t0:.0f}s", flush=True)

def state(b, name):
    v = b[9][name]
    if v is None: return "unread"
    ts = HT[b[2]]
    lo, hi = bisect.bisect_left(ts, b[0] - 86400.0), bisect.bisect_left(ts, b[0])
    own = sorted(x[1][name] for x in H[b[2]][lo:hi] if x[1][name] is not None)
    if len(own) < 100: return "unread"
    q = lambda p: own[min(len(own) - 1, int(len(own) * p))]
    if v < q(0.01) or v > q(0.99): return "anomaly"
    return "yes" if v >= q(0.6667) else "no" if v <= q(0.3333) else "mid"

def line(g):
    n = len(g)
    if n < 20: return "n %4d (too few)" % n
    sh = lambda k: 100.0 * sum(1 for x in g if x[5] >= k - 1e-9) / n
    return "n %4d paid %.2f at minute %4.1f | bid later >=+5c %2.0f%% >=+10c %2.0f%% | fell %4.1fc first | never 1c over %2.0f%% | won %2.0f%%" % (n, sum(x[4] for x in g) / n, sum(x[8] for x in g) / n, sh(0.05), sh(0.10), 100.0 * sum(x[6] for x in g) / n, 100.0 * sum(1 for x in g if x[5] < 0.01) / n, 100.0 * sum(x[7] for x in g) / n)

for tag, title in (("any", "THE FIRST READING IN RANGE (any time in the market)"), ("late", "THE LAST 3 MINUTES (minutes 12-15)")):
    U = [b for b in B if b[3] == tag]
    mid = len(U) // 2
    halves = (U[:mid], U[mid:])
    print(f"\n######## {title}: {len(U)} bets")
    print("  ALL          : h1 " + line(halves[0]) + "\n                 h2 " + line(halves[1]))
    for name in CHECKS:
        print(f"\n  --- check: {name}")
        sts = [[state(b, name) for b in h] for h in halves]
        for stt in ("yes", "mid", "no", "anomaly", "unread"):
            g = [[b for b, x in zip(h, ss) if x == stt] for h, ss in zip(halves, sts)]
            print(f"  {stt:8s}: h1 {line(g[0])}\n            h2 {line(g[1])}")
    # all four spot checks at once, and with the price check
    print("\n  --- together: how many of cushion, trend, history say YES (0-3), then with the price curve not NO")
    for seg, h in enumerate(halves):
        cnt = [sum(1 for nm in ("cushion", "trend", "history") if state(b, nm) == "yes") for b in h]
        for c in range(4):
            print(f"  h{seg + 1} {c} of 3 yes: " + line([b for b, x in zip(h, cnt) if x == c]))
        print(f"  h{seg + 1} 2+ of 3 yes and price not NO: " + line([b for b, x in zip(h, cnt) if x >= 2 and state(b, 'price') in ('yes', 'mid')]))
        print(f"  h{seg + 1} 2+ of 3 yes and price NO    : " + line([b for b, x in zip(h, cnt) if x >= 2 and state(b, 'price') == 'no']))
print(f"\ndone in {time.time() - t0:.0f}s")
