"""HAND CHECK: each buy step's recorded reading on the bot's own live commodity bets, recomputed from the raw Kalshi perp ticks and Kalshi quotes of that market (own_bets_raw.json.gz, scripts/own_bets_dump.py), with the bot's own functions where a step has one.
  price       the side's ask in the newest quote at the decision
  cushion     variants.snapshot on the PERP ticks, against the perp's first tick at the open (z, crossings, variety)
  perp_worth  curvevalue.read(..., "900") on the perp ticks, against the same open tick
  price_trend the side's mid now less the newest quote 60 s back (runner.side_trend)
  spike       the side's mid rise and bid swing over 90 s (runner.side_spike; the lane's limits are printed by the step only when it says no)
Not checkable from this file: vol_regime (hours of history), center_agree (the outside spot and its learned offset), same_side, cooldown, context_score.
    PYTHONPATH=src python3 scripts/own_bets_handcheck.py [N=6]"""
import bisect, glob, gzip, json, statistics, sys
sys.argv = sys.argv[:1] + [a for a in sys.argv[1:]]
from own_bets_table import B, ET
import datetime
from kalshi_moonshot import curvevalue, variants

N = int(dict(a.split("=") for a in sys.argv[1:]).get("N", 6))
RAW = json.load(gzip.open(sorted(glob.glob("job-results/*/files/own_bets_raw.json.gz"))[-1]))
rows = []
for r, p in B:
    raw = RAW.get(r["ticker"])
    if not raw or not raw["ticks"] or not r["close"]: continue
    T, V = [x[0] for x in raw["ticks"]], [x[1] for x in raw["ticks"]]
    Q = [q for q in raw["quotes"] if q[1] > 0 and q[2] > 0]
    sc = 100.0 if max(q[2] for q in Q) > 1.5 else 1.0
    qt = [q[0] for q in Q]
    now, close, side = r["ts"], r["close"], r["side"]
    opened = close - 900.0
    i = bisect.bisect_left(T, opened - 5.0)
    ref = V[i] if i < len(T) and T[i] <= opened + 20.0 else None
    sideask = lambda q: q[2] / sc if side == "yes" else 1.0 - q[1] / sc
    sidemid = lambda q: (q[1] + q[2]) / (2 * sc) if side == "yes" else 1.0 - (q[1] + q[2]) / (2 * sc)
    sidebid = lambda q: q[1] / sc if side == "yes" else 1.0 - q[2] / sc
    j = bisect.bisect_right(qt, now) - 1
    jb = bisect.bisect_right(qt, now - 60.0) - 1
    js = bisect.bisect_right(qt, now - 90.0) - 1
    inside = [sidebid(q) for q in Q[bisect.bisect_left(qt, now - 90.0):j + 1]]
    snap = variants.snapshot(T, V, ref, side, now, close) if ref else None
    cv = curvevalue.read(T, V, now, ref, side, close - now, "900") if ref else None
    rows.append({"r": r, "p": p, "ask": sideask(Q[j]) if j >= 0 else None, "quote_age": now - qt[j] if j >= 0 else None, "z": snap and snap["z"], "crosses": snap and snap["crosses"], "variety": snap and snap.get("variety"),
                 "perp": cv and cv.p, "trend": 100 * (sidemid(Q[j]) - sidemid(Q[jb])) if j >= 0 and jb >= 0 else None, "rise90": 100 * (sidemid(Q[j]) - sidemid(Q[js])) if j >= 0 and js >= 0 else None, "swing90": 100 * (max(inside) - min(inside)) if inside else None,
                 "tick_age": now - T[bisect.bisect_right(T, now) - 1], "ref": ref, "strike": r["strike"]})
f = lambda v, fmt: (fmt % v) if v is not None else "  n/a"
def show(x):
    r, p = x["r"], x["p"]
    print("%s %s %s paid %.2f net %+.2f$ (%s)" % (datetime.datetime.fromtimestamp(r["ts"], ET).strftime("%m-%d %H:%M:%S"), r["ticker"], r["side"].upper(), r["paid"], r["net"], "won" if r["won"] else "lost"))
    print("    price       recorded ask %s | recomputed %s (quote %ss old)" % (f(p["ask"], "%.2f"), f(x["ask"], "%.2f"), f(x["quote_age"], "%.1f")))
    print("    cushion     recorded z %s, %s crossings, variety %s | on the perp: z %s, %s crossings, variety %s (perp tick %ss old; open tick %s, Kalshi's strike %s)" % (f(p["cushion_z"], "%+.2f"), f(p["crossings"], "%.0f"), p["variety"], f(x["z"], "%+.2f"), f(x["crosses"], "%.0f"), x["variety"], f(x["tick_age"], "%.1f"), x["ref"], x["strike"]))
    print("    perp_worth  recorded chance %s | recomputed %s" % (f(p["perp_chance"], "%.3f"), f(x["perp"], "%.3f")))
    print("    price_trend recorded %sc over 60 s | recomputed %sc" % (f(p["trend_c"], "%+.1f"), f(x["trend"], "%+.1f")))
    print("    spike       recorded '%s' | recomputed: rose %sc, bid swung %sc over 90 s" % (p["spike"], f(x["rise90"], "%+.1f"), f(x["swing90"], "%.1f")))
S = sorted(rows, key=lambda x: x["r"]["net"])
print(f"{len(rows)} bets with raw ticks\n\n=== {N} BIGGEST LOSERS"); [show(x) for x in S[:N]]
print(f"\n=== {N} BIGGEST WINNERS"); [show(x) for x in S[::-1][:N]]
print("\n=== ALL %d: recorded against recomputed" % len(rows))
def cmp(name, a, b, tol):
    pr = [(a(x), b(x)) for x in rows if a(x) is not None and b(x) is not None]
    d = sorted(abs(u - v) for u, v in pr)
    print("  %-12s n %3d  within %-5g: %3d (%2.0f%%)  median gap %.3f  90th pct %.3f  worst %.3f" % (name, len(pr), tol, sum(1 for e in d if e <= tol), 100.0 * sum(1 for e in d if e <= tol) / max(1, len(d)), d[len(d) // 2], d[int(len(d) * .9)], d[-1]))
cmp("ask", lambda x: x["p"]["ask"], lambda x: x["ask"], 0.011)
cmp("cushion z", lambda x: x["p"]["cushion_z"], lambda x: x["z"], 0.1)
cmp("crossings", lambda x: x["p"]["crossings"], lambda x: x["crosses"], 0)
cmp("perp chance", lambda x: x["p"]["perp_chance"], lambda x: x["perp"], 0.02)
cmp("price trend c", lambda x: x["p"]["trend_c"], lambda x: x["trend"], 1.0)
print("  variety      same letter: %d of %d" % (sum(1 for x in rows if x["variety"] == x["p"]["variety"]), sum(1 for x in rows if x["variety"])))
print("  cushion z under the lane's 0.8 when read on the perp: %d of %d; perp-side z negative: %d" % (sum(1 for x in rows if x["z"] is not None and x["z"] < 0.8), sum(1 for x in rows if x["z"] is not None), sum(1 for x in rows if x["z"] is not None and x["z"] < 0)))
