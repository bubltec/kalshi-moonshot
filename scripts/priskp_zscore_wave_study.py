"""Does a Gaussian z-score of the spot, applied only to WAVY markets, avoid buying into a dip without killing normal favourite trend buys? (2026-11-25; user, on the 08:30 round: "gaussian curve math", "just the ones from the past 24 hours", "we need to make sure this doesn't kill normal favorite trends buys".)
QUESTION (one): on the 131 live priskp bets of the 24 hours to 11-25 10:20 ET, as they ran (sold or settled, fee on every leg), at the moment of each buy compute from the spot's 10 s path over the 30 minutes before it: z = (spot - the window's mean) / its standard deviation, the R^2 of a straight-line fit (high = a trend, low = a wave), and the z of the spot against that line (residual z). A bet is 'stretched with the bet' when the side's z (NO: -z, YES: +z) is at least 1.0, i.e. the spot already sits in the tail of its own range on the side the bet wins. The candidate rule refuses a stretched bet in a WAVY market (R^2 under the median R^2 of these bets, the data's own cut). How many bets would it refuse, what did they net as they ran, how many winners and losers, and what does it leave untouched among the TREND bets (R^2 at or over the median), stretched or not?
HONEST LIMITS: 131 bets in 24 hours, one regime; the 30 minutes, the z bar of 1.0 and the median R^2 split were set before the run and nothing else was tried; 'as run' mixes the lane's exits with the entry; the 10 s grid is the tape's average of ticks; a lead, not a rule. A rule on it goes to shadow first.
INPUT: a directory with bets24.json and d_<index>.raw ('sec:value,...' 10 s averages), pulled from the host one query at a time.
Run on this Mac: uv run python scripts/priskp_zscore_wave_study.py <dir>"""
import bisect
import json
import math
import re
import statistics
import sys

D = sys.argv[1]
IDX = {"KXBNB15M": "BNBUSD_RTI", "KXBTC15M": "BRTI", "KXETH15M": "ETHUSD_RTI", "KXHYPE15M": "HYPEUSD_RTI", "KXSOL15M": "SOLUSD_RTI", "KXXRP15M": "XRPUSD_RTI", "KXDOGE15M": "DOGEUSD_RTI", "KXZEC15M": "ZECUSD_RTI", "KXNEAR15M": "NEARUSD_RTI"}
SPAN, ZBAR = 1800, 1.0


def fee(p):
    return 0.07 * p * (1 - p)


def load(ix):
    line = [x.strip() for x in open(f"{D}/d_{ix}.raw").read().split("\n") if re.match(r"^\d+:[\d.]+,", x.strip())][0]
    t, v = [], []
    for a, b in (p.split(":") for p in line.split(",")):
        t.append(int(a))
        v.append(float(b))
    return t, v


spot = {ix: load(ix) for ix in set(IDX.values())}


def read(ix, now):
    t, v = spot[ix]
    hi, lo = bisect.bisect_right(t, now), bisect.bisect_left(t, now - SPAN)
    w = v[lo:hi]
    if len(w) < 120:
        return None
    n = len(w)
    mu, sd = statistics.fmean(w), statistics.pstdev(w)
    if sd <= 0:
        return None
    xs = list(range(n))
    xm = (n - 1) / 2
    sxx = sum((x - xm) ** 2 for x in xs)
    b = sum((x - xm) * (y - mu) for x, y in zip(xs, w)) / sxx
    a = mu - b * xm
    res = [y - (a + b * x) for x, y in zip(xs, w)]
    r2 = 1.0 - sum(r * r for r in res) / (sd * sd * n)
    rsd = math.sqrt(sum(r * r for r in res) / n) or 1e-12
    return (w[-1] - mu) / sd, r2, res[-1] / rsd, b * 6 / sd  # z, R^2, residual z, slope in sigmas per minute


bets = json.load(open(f"{D}/bets24.json"))
rows = []
for b in bets:
    ix = IDX.get(b["ticker"].split("-")[0])
    if ix is None or b["pay"] is None:
        continue
    r = read(ix, b["ts"])
    if r is None:
        continue
    pay, n = float(b["pay"]), float(b["n"])
    cost = pay + fee(pay)
    if b["xts"]:
        got = float(b["xp"]) - fee(float(b["xp"]))
    else:
        got = 1.0 if b["out"] == b["side"] else 0.0
    z, r2, rz, slope = r
    sz = z if b["side"] == "yes" else -z  # the z on the side the bet wins
    rows.append({"tk": b["ticker"], "side": b["side"], "ts": b["ts"], "sz": sz, "r2": r2, "rz": rz, "slope": slope, "net": (got - cost) * n, "pc": got - cost, "n": n})
med = statistics.median(x["r2"] for x in rows)
print(f"{len(rows)} bets with a 30-minute spot window; median R^2 of the straight-line fit {med:.2f} (trend at or over it, wave under it)")


def line(label, g):
    if not g:
        print(f"{label:62s} n   0")
        return
    w = sum(1 for x in g if x["pc"] > 0)
    print(f"{label:62s} n {len(g):3d}  as run ${sum(x['net'] for x in g):+8.2f}  won {w:3d} lost {len(g) - w:3d}  per contract {sum(x['pc'] for x in g) / len(g):+.3f}")


line("all bets", rows)
trend = [x for x in rows if x["r2"] >= med]
wave = [x for x in rows if x["r2"] < med]
line("TREND bets (R^2 at or over the median)", trend)
line("WAVE bets (R^2 under the median)", wave)
st = lambda g: [x for x in g if x["sz"] >= ZBAR]
print(f"\nstretched with the bet (the side's z >= {ZBAR:g}):")
line("  in TREND markets (the rule leaves these alone)", st(trend))
line("  in WAVE markets (the rule REFUSES these)", st(wave))
line("  wave, not stretched (the rule leaves these alone)", [x for x in wave if x["sz"] < ZBAR])
ref = st(wave)
print(f"\nrefused: {len(ref)} of {len(rows)} bets; they netted ${sum(x['net'] for x in ref):+.2f} as run, so refusing them changes the total by ${-sum(x['net'] for x in ref):+.2f}; winners among them {sum(1 for x in ref if x['pc'] > 0)}, losers {sum(1 for x in ref if x['pc'] <= 0)}")
print("\nthe 08:30 round (bot's four NO bets):")
for x in rows:
    if "26NOV250830" in x["tk"]:
        print(f"  {x['tk'][:24]:24s} {x['side']:3s} side z {x['sz']:+.2f}  R^2 {x['r2']:.2f}  residual z {x['rz']:+.2f}  net ${x['net']:+.2f}  {'REFUSED' if x in ref else 'kept'}")
print("\nlargest refused losers and winners:")
for x in sorted(ref, key=lambda x: x["net"])[:4] + sorted(ref, key=lambda x: -x["net"])[:3]:
    print(f"  {x['tk'][:24]:24s} {x['side']:3s} side z {x['sz']:+.2f}  R^2 {x['r2']:.2f}  net ${x['net']:+.2f}")
