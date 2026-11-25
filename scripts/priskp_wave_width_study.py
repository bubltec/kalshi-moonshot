"""Is the target inside the wave? (2026-11-25; user, on BTC and ETH 08:30 NO, which a z-score of the spot missed: "the range on the curve needs to be flexible because even though the shapes are similar, the distance is not".)
QUESTION (one): on the 131 live priskp bets of the 24 hours to 11-25 10:20 ET, at the moment of the buy, how far is the spot from the market's target on the bet's winning side, measured in WAVE WIDTHS (the standard deviation of the spot's 10 s path over the 30 minutes before the buy: each market's own range, so a shape of any size reads the same)? Also the Gaussian chance of ending on the bet's side from that distance, the time left and the spot's typical minute (Phi(distance / (typical minute x sqrt(minutes left)))), against the price paid. Bets by distance bucket: n, lost, as-run net; and by chance-minus-price bucket.
HONEST LIMITS: 131 bets, one day, one regime; the window of 30 minutes and the bucket edges (0.5 and 1.0 wave widths; the chance gaps -0.10 and 0) were fixed before the run, nothing else tried; 'as run' mixes the lane's exits with the entry; a Gaussian chance assumes no mean reversion, which a wave has. A lead only.
INPUT: bets24.json, targets24.json, d_<index>.raw in the directory given (pulled one query at a time).
Run on this Mac: uv run python scripts/priskp_wave_width_study.py <dir>"""
import bisect
import json
import math
import re
import statistics
import sys

D = sys.argv[1]
IDX = {"KXBNB15M": "BNBUSD_RTI", "KXBTC15M": "BRTI", "KXETH15M": "ETHUSD_RTI", "KXHYPE15M": "HYPEUSD_RTI", "KXSOL15M": "SOLUSD_RTI", "KXXRP15M": "XRPUSD_RTI", "KXDOGE15M": "DOGEUSD_RTI", "KXZEC15M": "ZECUSD_RTI", "KXNEAR15M": "NEARUSD_RTI"}


def fee(p):
    return 0.07 * p * (1 - p)


def phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def load(ix):
    line = [x.strip() for x in open(f"{D}/d_{ix}.raw").read().split("\n") if re.match(r"^\d+:[\d.]+,", x.strip())][0]
    t, v = [], []
    for a, b in (p.split(":") for p in line.split(",")):
        t.append(int(a))
        v.append(float(b))
    return t, v


spot = {ix: load(ix) for ix in set(IDX.values())}
targets = json.load(open(f"{D}/targets24.json"))
bets = json.load(open(f"{D}/bets24.json"))
rows = []
for b in bets:
    ix = IDX.get(b["ticker"].split("-")[0])
    K = (targets.get(b["ticker"]) or [None])[0]
    if ix is None or not K:
        continue
    t, v = spot[ix]
    hi = bisect.bisect_right(t, b["ts"])
    w = v[bisect.bisect_left(t, b["ts"] - 1800):hi]
    hr = v[bisect.bisect_left(t, b["ts"] - 3600):hi]
    if len(w) < 120 or len(hr) < 240:
        continue
    sd = statistics.pstdev(w)
    d10 = [y - x for x, y in zip(hr, hr[1:])]
    unit = math.sqrt(sum(x * x for x in d10) / len(d10) * 6.0)  # the spot's typical minute
    spotv = w[-1]
    dist = (K - spotv) if b["side"] == "no" else (spotv - K)  # the cushion on the winning side
    left_min = max((math.floor(b["ts"] / 900) * 900 + 900 - b["ts"]) / 60.0, 0.5)
    chance = phi(dist / (unit * math.sqrt(left_min))) if unit > 0 else 0.5
    # the wave as a mean-reverting Gaussian (Ornstein-Uhlenbeck) fitted on the 30 minutes before the buy: its centre, width and its own time scale tau from the lag-1 autocorrelation of the 10 s path
    mu = statistics.fmean(w)
    xc = [x - mu for x in w]
    den = sum(x * x for x in xc)
    phi1 = sum(a * c for a, c in zip(xc, xc[1:])) / den if den > 0 else 0.0
    tau = -10.0 / math.log(phi1) if 0.0 < phi1 < 0.9999 else float("inf")  # seconds; inf = no reversion seen (a trend)
    T = left_min * 60.0
    if tau == float("inf"):
        ou_chance = chance  # a random walk: the plain Gaussian chance
    else:
        mean_T = mu + (spotv - mu) * math.exp(-T / tau)
        sd_T = sd * math.sqrt(max(1.0 - math.exp(-2 * T / tau), 1e-9))
        z_end = ((K - mean_T) / sd_T) if b["side"] == "no" else ((mean_T - K) / sd_T)
        ou_chance = phi(z_end)
    pay, n = float(b["pay"]), float(b["n"])
    cost = pay + fee(pay)
    got = (float(b["xp"]) - fee(float(b["xp"]))) if b["xts"] else (1.0 if b["out"] == b["side"] else 0.0)
    rows.append({"tk": b["ticker"], "side": b["side"], "widths": dist / sd if sd > 0 else 0.0, "chance": chance, "pay": pay, "gap": chance - pay, "tau": tau, "ou": ou_chance, "ougap": ou_chance - pay, "net": (got - cost) * n, "pc": got - cost})


def line(label, g):
    if not g:
        print(f"{label:52s} n   0")
        return
    lost = sum(1 for x in g if x["pc"] <= 0)
    print(f"{label:52s} n {len(g):3d}  as run ${sum(x['net'] for x in g):+8.2f}  lost {lost:3d} ({100 * lost / len(g):.0f}%)  per contract {sum(x['pc'] for x in g) / len(g):+.3f}")


print(f"{len(rows)} bets")
line("all", rows)
print("\nthe target's distance from the spot on the bet's side, in wave widths (30 min standard deviation):")
line("  under 0.5 wave widths (the target is inside the wave)", [x for x in rows if x["widths"] < 0.5])
line("  0.5 to 1.0", [x for x in rows if 0.5 <= x["widths"] < 1.0])
line("  1.0 or more", [x for x in rows if x["widths"] >= 1.0])
print("\nGaussian chance of ending on the bet's side minus the price paid:")
line("  chance 0.10 or more under the price (overpaying)", [x for x in rows if x["gap"] < -0.10])
line("  within 0.10 under the price", [x for x in rows if -0.10 <= x["gap"] < 0])
line("  chance at or over the price", [x for x in rows if x["gap"] >= 0])
print("\nthe 08:30 round:")
for x in rows:
    if "26NOV250830" in x["tk"]:
        print(f"  {x['tk'][:24]:24s} {x['side']:3s} target {x['widths']:+.2f} wave widths away, Gaussian chance {x['chance']:.2f} vs paid {x['pay']:.2f}, net ${x['net']:+.2f}")

print("\nthe wave's own time scale tau (minutes) across these bets: " + ", ".join(f"{q:.1f}" for q in statistics.quantiles([x["tau"] / 60.0 for x in rows if x["tau"] != float("inf")], n=4)) + f" (quartile cuts); {sum(1 for x in rows if x['tau'] == float('inf'))} bets show no reversion")
print("\nthe mean-reverting Gaussian chance (the wave's own centre, width and tau) of ending on the bet's side, minus the price paid:")
line("  chance 0.10 or more under the price (overpaying)", [x for x in rows if x["ougap"] < -0.10])
line("  within 0.10 under the price", [x for x in rows if -0.10 <= x["ougap"] < 0])
line("  chance at or over the price", [x for x in rows if x["ougap"] >= 0])
print("\nthe 08:30 round, with the wave's own time scale:")
for x in rows:
    if "26NOV250830" in x["tk"]:
        print(f"  {x['tk'][:24]:24s} {x['side']:3s} tau {x['tau'] / 60.0:.1f} min, mean-reverting chance {x['ou']:.2f} vs paid {x['pay']:.2f}, net ${x['net']:+.2f}")
