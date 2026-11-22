"""WHICH FEED DID THE CUSHION AND VARIETY STEPS READ? (2026-11-21.) On the 132 live commodity bets with step notes, the recorded cushion z, crossings and variety are recomputed with the bot's own variants.snapshot on (a) the outside BiQuote ticks and (b) Kalshi's perp reference, each against its own first tick at the open, at the second the steps read (the second in the 40 before the row's stamp where the ask, price trend and perp chance match the note best).
Then: what the lane's 0.8 cushion bar, its under-4-crossings limit and skip_varieties (G-no, A-yes) would have done on those bets with the PERP readings: the bets refused, their dollars, ahead and behind. Reads job-results/*/files/own_bets*.json.gz.
    PYTHONPATH=src:scripts python3 scripts/own_bets_cushion_feed.py"""
import bisect, glob, gzip, json, sys
sys.argv = sys.argv[:1]
from own_bets_table import B
from kalshi_moonshot import curvevalue, variants

load = lambda n: json.load(gzip.open(sorted(glob.glob(f"job-results/*/files/{n}.json.gz"))[-1]))
RAW, BQ = load("own_bets_raw"), load("own_bets_biquote")


def snap(ticks, r, now):
    T, V = [x[0] for x in ticks], [x[1] for x in ticks]
    opened = r["close"] - 900.0
    i = bisect.bisect_left(T, opened - 5.0)
    if not T or i >= len(T) or T[i] > opened + 20.0: return None
    return variants.snapshot(T, V, V[i], r["side"], now, r["close"])


def read_second(r, p):
    raw = RAW[r["ticker"]]
    T, V = [x[0] for x in raw["ticks"]], [x[1] for x in raw["ticks"]]
    Q = [q for q in raw["quotes"] if q[1] > 0 and q[2] > 0]
    sc = 100.0 if max(q[2] for q in Q) > 1.5 else 1.0
    qt = [q[0] for q in Q]
    mid = lambda q: (q[1] + q[2]) / (2 * sc) if r["side"] == "yes" else 1 - (q[1] + q[2]) / (2 * sc)
    ask = lambda q: q[2] / sc if r["side"] == "yes" else 1 - q[1] / sc
    i = bisect.bisect_left(T, r["close"] - 905.0)
    ref = V[i]
    best = None
    for d in range(41):
        now = r["ts"] - d
        j, jb = bisect.bisect_right(qt, now) - 1, bisect.bisect_right(qt, now - 60) - 1
        cv = curvevalue.read(T, V, now, ref, r["side"], r["close"] - now, "900")
        if j < 0 or jb < 0 or cv is None: continue
        err = abs(ask(Q[j]) - p["ask"]) / 0.01 + abs(100 * (mid(Q[j]) - mid(Q[jb])) - p["trend_c"]) + abs(cv.p - p["perp_chance"]) / 0.01
        if best is None or err < best[0]: best = (err, now)
    return best[1] if best else r["ts"]


rows = []
for r, p in B:
    if r["ticker"] not in RAW or r["ticker"] not in BQ: continue
    now = read_second(r, p)
    rows.append((r, p, snap(BQ[r["ticker"]]["ticks"], r, now), snap(RAW[r["ticker"]]["ticks"], r, now)))
print(f"{len(rows)} bets; BiQuote ticks for {sum(1 for x in rows if x[2])}, perp ticks for {sum(1 for x in rows if x[3])}")
for name, k in (("the outside BiQuote feed", 2), ("Kalshi's perp reference ", 3)):
    g = [x for x in rows if x[k]]
    d = sorted(abs(x[k]["z"] - x[1]["cushion_z"]) for x in g)
    print("  recorded against %s: z within 0.1 on %3d of %d (median gap %.3f, 90th pct %.3f); same crossings %3d; same variety letter %3d" % (name, sum(1 for e in d if e <= 0.1), len(g), d[len(d) // 2], d[int(len(d) * .9)],
          sum(1 for x in g if x[k]["crosses"] == x[1]["crossings"]), sum(1 for x in g if x[k].get("variety") == x[1]["variety"])))
tot = lambda g: "n %3d, %+7.2f$, ahead %d, behind %d" % (len(g), sum(x[0]["net"] for x in g), sum(1 for x in g if x[0]["net"] > 0), sum(1 for x in g if x[0]["net"] <= 0))
P = [x for x in rows if x[3]]
print("\nWITH THE PERP READINGS, on the same bets (what the lane's own settings would have refused):")
print("  all bets                                  : " + tot(rows))
print("  no perp reading (cushion step = no buy)   : " + tot([x for x in rows if not x[3]]))
low = [x for x in P if x[3]["z"] < 0.8]
cr = [x for x in P if x[3]["z"] >= 0.8 and x[3]["crosses"] >= 4]
sk = [x for x in P if x[3]["z"] >= 0.8 and x[3]["crosses"] < 4 and variants.skipped(("G-no", "A-yes"), x[3].get("variety"), x[0]["side"])]
print("  cushion z under 0.8 on the perp           : " + tot(low))
print("  4 or more crossings on the perp           : " + tot(cr))
print("  a skipped variety (G-no, A-yes) on the perp: " + tot(sk))
out = {id(x) for x in low + cr + sk} | {id(x) for x in rows if not x[3]}
print("  REFUSED in all                            : " + tot([x for x in rows if id(x) in out]))
print("  STILL BOUGHT                              : " + tot([x for x in rows if id(x) not in out]))
vg = [x for x in P if x[3]["z"] >= 0.8 and variants.skipped(("G-no", "A-yes"), x[3].get("variety"), x[0]["side"])]
o2 = {id(x) for x in low + vg}
print("\nWITHOUT THE CROSSINGS LIMIT (removed 2026-11-21, user: \"We don't want any of that\"), perp readings:")
print("  cushion z under 0.8 on the perp           : " + tot(low))
print("  a skipped variety (G-no, A-yes) on the perp: " + tot(vg) + "   (G-no " + tot([x for x in vg if x[3].get("variety") == "G"]) + ")")
print("  REFUSED in all                            : " + tot([x for x in rows if id(x) in o2]))
print("  STILL BOUGHT                              : " + tot([x for x in rows if id(x) not in o2]))
print("  with the variety skip gone too, only the cushion refuses: bought " + tot([x for x in rows if id(x) not in {id(y) for y in low}]))
print("  (a refused bet leaves room for a later buy in the same market, which this does not count)")
for x in sorted([x for x in rows if id(x) in out], key=lambda x: x[0]["net"])[:12]:
    print("    %-30s %-3s paid %.2f net %+6.2f | recorded z %+.2f %s, on the perp z %s crossings %s variety %s" % (x[0]["ticker"], x[0]["side"], x[0]["paid"], x[0]["net"], x[1]["cushion_z"], x[1]["variety"], "%+.2f" % x[3]["z"] if x[3] else "n/a", x[3]["crosses"] if x[3] else "n/a", x[3].get("variety") if x[3] else "n/a"))
