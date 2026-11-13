"""Is the ladder's chance, priced from Kalshi's own perp, right on every crypto 15-minute market, not only the bets the old rules took? (2026-11-13, user: "i thought we are trying to test the ladder"; "the ladder would be completely different"; "whatever you need to change to make the ladder work".)

Question, one: on every settled 15-minute market of the five coins that have a perp reference (KXBTC, ETH, SOL, XRP, DOGE 15M), from 2026-11-11 12:00 ET (after the perp ticks have 3 hours of boundaries to learn from) to the tape's end, sampled at minutes 5, 8, 11 and 14 of the window, is the ladder's chance (commodities.py window anchor, as commodity lanes use it) calibrated against what happened, and does it carry information beyond Kalshi's own quote? And does buying the favorite (ask 0.70-0.90, spread <= 0.04, the live lane's price band) only where the ladder's edge clears 0.015 beat buying the same band without it, held to settlement, fee on the entry?

The ladder chance: P(yes) = Phi(ln(center/strike) / width); center = the perp's last tick times the offset learned from the last 3 hours of 15-minute boundaries (window_basis, at least 6); width = sqrt((k * realized_sigma)^2 + unc^2), realized_sigma from the perp's 5-minute closes of the last 6 hours (at least 20 bars) over the time left, unc the offset's own spread floored at COMMODITY_WINDOW_UNC_FLOOR, k = COMMODITY_VOL_MULT (1.25 as set). The same pieces are saved per sample, so the width can be rescored without repricing.
Width variants, said plainly:
  as set      k = COMMODITY_VOL_MULT, what the commodity lanes run
  walk-fwd    k chosen per series from that series' own samples settled in the 6 hours before the sample (grid 0.50..2.00 by 0.125, the k with the lowest log loss; the series' own last hours, rule 2; no asset name is used for the choice, the series is the market). With under 40 earlier samples, k as set.
  best fixed  the single k with the lowest log loss over ALL samples: chosen after seeing the results, printed to show the size of the miss, not a proposed setting.
Not simulated: the curve risk exit (this scores the buy, held to settlement). Dollars per $1 staked: cost = ask + fee, back = 1 if the side won else 0.
By curve shape (rule 4): terciles of the curve model's own level / slope / curvature readings at 5 and 13 minutes (curvemodel.features_curvature) of the perp against the strike, on the side bought, cut from all picks.
    scripts/cloud_job.sh scripts/ladder_calibration_all_markets.py
"""

import bisect
import calendar
import collections
import datetime as dt
import math
import os

from kalshi_moonshot import commodities, config, db
from kalshi_moonshot.curvemodel import features_curvature, live_grid
from kalshi_moonshot.kalshifeed import _closes
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
from kalshi_moonshot.watcher import perp_index

os.nice(10)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
ET = dt.timezone(dt.timedelta(hours=-5))
hm = lambda t: dt.datetime.fromtimestamp(t, ET).strftime("%m-%d %H:%M")  # noqa: E731
START = calendar.timegm((2026, 11, 11, 17, 0, 0))  # 11-11 12:00 ET
SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M")
MINUTES = (5, 8, 11, 14)
BAND = (0.70, 0.90)
MAX_SPREAD = 0.04
BAR = 0.015
K_GRID = [0.5 + 0.125 * i for i in range(13)]
N = commodities._N.cdf


class Perp:
    """One perp index's ticks, with the 5-minute and 1-minute closes built once."""

    def __init__(self, ix):
        r = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (ix, START - 8 * 3600)).fetchall()
        self.ts = [float(x[0]) for x in r]
        self.vs = [float(x[1]) for x in r]
        c5, k5 = _closes(r, 300)
        c1, k1 = _closes(r, 60)
        self.c5, self.k5, self.c1, self.k1 = c5, [int(x) for x in k5], c1, [int(x) for x in k1]

    def feed(self, t):
        i = bisect.bisect_right(self.ts, t) - 1
        if i < 0:
            return None
        a = bisect.bisect_left(self.k5, t - 6 * 3600.0)
        m = bisect.bisect_right(self.k5, t)
        closes, times = list(self.c5[a:m]), [float(x) for x in self.k5[a:m]]
        if closes and self.k5[m - 1] + 300 > t:
            closes[-1] = self.vs[i]  # the bar still open at t closes at the last tick
        return i, commodities.Series(last=self.vs[i], last_ts=self.ts[i], closes=closes, times=times)

    def minutes(self, t, i):
        a = bisect.bisect_left(self.k1, t - 4 * 3600.0)
        m = bisect.bisect_right(self.k1, t)
        out = {self.k1[j]: self.c1[j] for j in range(a, m)}
        if m and self.k1[m - 1] + 60 > t:
            out[self.k1[m - 1]] = self.vs[i]
        return out


perps = {}
samples, why = [], collections.Counter()
wins = st.db.execute("SELECT ticker,series,close_ts,strike,result FROM windows WHERE series IN (%s) AND close_ts>? AND result IN ('yes','no') AND strike>0 ORDER BY close_ts" % ",".join("'%s'" % x for x in SERIES), (START,)).fetchall()
allw = collections.defaultdict(dict)
for r in st.db.execute("SELECT series,close_ts,strike FROM windows WHERE series IN (%s) AND close_ts>? AND strike>0" % ",".join("'%s'" % x for x in SERIES), (START - 4 * 3600,)).fetchall():
    allw[r["series"]][int(float(r["close_ts"])) - 900] = float(r["strike"])
print(f"settled windows of the five series since {hm(START)} ET: {len(wins)}; last close {hm(float(wins[-1]['close_ts']))} ET")

for w in wins:
    ix = perp_index(w["series"])
    if not ix:
        why["no perp reference"] += 1
        continue
    if ix not in perps:
        perps[ix] = Perp(ix)
    pp = perps[ix]
    close, strike = float(w["close_ts"]), float(w["strike"])
    opened = close - 900.0
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (w["ticker"], opened - 30, close)).fetchall()
    qt = [float(x[0]) for x in q]
    for m in MINUTES:
        t = opened + 60.0 * m
        j = bisect.bisect_right(qt, t) - 1
        if j < 0 or t - qt[j] > 30:
            why["no quote within 30 s"] += 1
            continue
        bid, ask = float(q[j][1]), float(q[j][2])
        got = pp.feed(t)
        if got is None:
            why["no perp tick yet"] += 1
            continue
        i, feed = got
        age = t - feed.last_ts
        if age > commodities.REALTIME_STALE_SECONDS:
            why["perp tick over 120 s old"] += 1
            continue
        refs = {T: k for T, k in allw[w["series"]].items() if t - commodities.WINDOW_MAX_AGE <= T <= t}
        wb = commodities.window_basis(refs, pp.minutes(t, i), t)
        if wb is None:
            why["under 6 boundaries"] += 1
            continue
        try:
            sig = commodities.realized_sigma(feed, (close - t) / 3600.0 + age / 3600.0)
        except commodities.CommodityError:
            why["under 20 bars"] += 1
            continue
        lnd = math.log(feed.last * math.exp(wb.basis) / strike)
        samples.append(dict(t=t, series=w["series"], ticker=w["ticker"], m=m, close=close, strike=strike, yes=1 if w["result"] == "yes" else 0, bid=bid, ask=ask, lnd=lnd, sig=sig, unc=max(wb.sd, s.commodity_window_unc_floor), ix=ix, i=i, basis=wb.basis))
print(f"samples priced: {len(samples)} from {len({x['ticker'] for x in samples})} markets; not priced: {dict(why)}")
if not samples:
    raise SystemExit(0)


def chance(x, k):
    p = N(x["lnd"] / math.sqrt((k * x["sig"]) ** 2 + x["unc"] ** 2))
    return min(max(p, s.commodity_tail_floor), 1.0 - s.commodity_tail_floor)


def logloss(xs, k):
    tot = 0.0
    for x in xs:
        p = chance(x, k)
        tot -= math.log(p if x["yes"] else 1.0 - p)
    return tot / len(xs)


K0 = s.commodity_vol_mult
samples.sort(key=lambda x: x["t"])
# walk-forward k per series from that series' samples whose market had settled 6 hours or less before the sample
byser = collections.defaultdict(list)
for x in samples:
    byser[x["series"]].append(x)
for ser, xs in byser.items():
    for x in xs:
        past = [y for y in xs if x["t"] - 6 * 3600 <= y["close"] <= x["t"]]
        x["k_wf"] = K0 if len(past) < 40 else min(K_GRID, key=lambda k: logloss(past, k))
for x in samples:
    x["p0"] = chance(x, K0)
    x["pw"] = chance(x, x["k_wf"])
    x["mid"] = (x["bid"] + x["ask"]) / 2.0
kbest = min(K_GRID, key=lambda k: logloss(samples, k))
for x in samples:
    x["pb"] = chance(x, kbest)
sg = sorted(x["sig"] * 1e4 for x in samples)
un = sorted(x["unc"] * 1e4 for x in samples)
print(f"\nwidth pieces (basis points): realized sigma median {sg[len(sg) // 2]:.2f} (x k {K0}); offset spread unc median {un[len(un) // 2]:.2f}; unc share of the width squared, median {sorted((x['unc'] ** 2 / ((K0 * x['sig']) ** 2 + x['unc'] ** 2)) for x in samples)[len(samples) // 2]:.1%}")
print(f"k as set {K0}; best fixed k over all samples {kbest} (after the fact); walk-forward k median {sorted(x['k_wf'] for x in samples)[len(samples) // 2]}")


def brier(xs, key):
    return sum((x[key] - x["yes"]) ** 2 for x in xs) / len(xs)


print("\nBRIER on the yes outcome (lower is better): all samples | samples with a side in the price band")
inband = [x for x in samples if (BAND[0] <= x["ask"] <= BAND[1] or BAND[0] <= 1.0 - x["bid"] <= BAND[1])]
for label, key in (("Kalshi quote mid", "mid"), ("ladder, as set", "p0"), ("ladder, walk-forward k", "pw"), ("ladder, best fixed k (after the fact)", "pb")):
    print(f"   {label:40s} {brier(samples, key):.4f} | {brier(inband, key):.4f}")
for label, key in (("average of mid and ladder as set", None),):
    print(f"   {label:40s} {sum(((x['mid'] + x['p0']) / 2 - x['yes']) ** 2 for x in samples) / len(samples):.4f} | {sum(((x['mid'] + x['p0']) / 2 - x['yes']) ** 2 for x in inband) / len(inband):.4f}")

print("\nCALIBRATION, yes side, by the ladder chance as set (n | mean chance | yes share | mean quote mid)")
for lo, hi in ((0, 0.1), (0.1, 0.3), (0.3, 0.5), (0.5, 0.7), (0.7, 0.9), (0.9, 1.01)):
    g = [x for x in samples if lo <= x["p0"] < hi]
    if g:
        print(f"   chance {lo:.1f}-{hi:.1f}: n={len(g):5d} | chance {sum(x['p0'] for x in g) / len(g):.3f} | yes {sum(x['yes'] for x in g) / len(g):.3f} | mid {sum(x['mid'] for x in g) / len(g):.3f}")
print("by the quote mid, same table (so the two sources can be compared bin for bin): n | mean mid | yes share | mean chance")
for lo, hi in ((0, 0.1), (0.1, 0.3), (0.3, 0.5), (0.5, 0.7), (0.7, 0.9), (0.9, 1.01)):
    g = [x for x in samples if lo <= x["mid"] < hi]
    if g:
        print(f"   mid {lo:.1f}-{hi:.1f}: n={len(g):5d} | mid {sum(x['mid'] for x in g) / len(g):.3f} | yes {sum(x['yes'] for x in g) / len(g):.3f} | chance {sum(x['p0'] for x in g) / len(g):.3f}")


# ---- the buy rule on the same markets: the first sample of a market where a side is in the band
def picks(key_edge):
    out, seen = [], set()
    for x in samples:
        if x["ticker"] in seen:
            continue
        for side in ("yes", "no"):
            ask = x["ask"] if side == "yes" else 1.0 - x["bid"]
            spread = x["ask"] - x["bid"]
            if not (BAND[0] <= ask <= BAND[1]) or spread > MAX_SPREAD:
                continue
            p = x[key_edge] if side == "yes" else 1.0 - x[key_edge]
            won = x["yes"] if side == "yes" else 1 - x["yes"]
            out.append(dict(x=x, side=side, ask=ask, p=p, edge=(p - ask - fee(ask)) if key_edge else 0.0, won=won, net=(1.0 if won else 0.0) / (ask + fee(ask)) - 1.0))
            seen.add(x["ticker"])
            break
    return out


base = picks("p0")  # the band alone: every first in-band sample; the edge column is the ladder's, as set
seen_t = {b["x"]["ticker"] for b in base}
print(f"\nBUY RULES, first in-band sample of each market (ask {BAND[0]}-{BAND[1]}, spread <= {MAX_SPREAD}), held to settlement, per $1 staked after the entry fee")


def line(g):
    if not g:
        return "n=   0"
    return f"n={len(g):4d} | net {sum(b['net'] for b in g):+8.2f} on ${len(g):d} ({sum(b['net'] for b in g) / len(g):+.1%}) | won {sum(b['won'] for b in g) / len(g):.1%} vs ask {sum(b['ask'] for b in g) / len(g):.3f}"


print(f"   the band alone (no ladder)                         {line(base)}")
rules = {}
for label, key in (("ladder as set", "p0"), ("ladder walk-forward k", "pw"), ("ladder best fixed k (after the fact)", "pb")):
    pk = picks(key)
    for bar in (BAR, 0.0, -0.05):
        kept = [b for b in pk if b["edge"] >= bar]
        skipped = [b for b in pk if b["edge"] < bar]
        rules[(key, bar)] = (kept, skipped)
        print(f"   {label:38s} edge >= {bar:+.3f}  kept {line(kept)}")
        print(f"   {'':38s}                skipped {line(skipped)}  | change vs the band alone {-sum(b['net'] for b in skipped):+.2f}, beat {sum(1 for b in skipped if b['net'] < 0)}, lost to {sum(1 for b in skipped if b['net'] > 0)}")

print("\nBY CURVE SHAPE (rule 4): kept at edge >= 0.015 vs the band alone, terciles cut from all in-band picks; kept n | kept net per $ | band-alone n | band-alone net per $")
names = ("level_5m", "slope_5m", "curv_5m", "level_13m", "slope_13m", "curv_13m")
shape = {}
for b in base:
    x = b["x"]
    pp = perps[x["ix"]]
    a = bisect.bisect_left(pp.ts, x["t"] - 34 * 60 - 60)
    grid = live_grid(pp.ts[a : x["i"] + 1], pp.vs[a : x["i"] + 1], x["t"], 34 * 60)
    sh = features_curvature(grid[0], grid[1], grid[2], math.log(x["strike"]) - x["basis"], 1 if b["side"] == "yes" else -1) if grid else None
    shape[id(b)] = sh[:6] if sh else None
read = [b for b in base if shape[id(b)]]
print(f"   curve readings available for {len(read)} of {len(base)} picks")
kept_ids = {b["x"]["ticker"] for b in rules[("p0", BAR)][0]}
for n_, name in enumerate(names):
    v = sorted(shape[id(b)][n_] for b in read)
    c1, c2 = v[len(v) // 3], v[2 * len(v) // 3]
    for label, pick in (("low ", lambda z: z < c1), ("mid ", lambda z: c1 <= z < c2), ("high", lambda z: z >= c2)):
        g = [b for b in read if pick(shape[id(b)][n_])]
        kk = [b for b in g if b["x"]["ticker"] in kept_ids]
        print(f"   {name:9s} {label}: kept n={len(kk):3d} {sum(b['net'] for b in kk) / len(kk) if kk else float('nan'):+.3f} | band alone n={len(g):3d} {sum(b['net'] for b in g) / len(g) if g else float('nan'):+.3f}")

print("\nBY SERIES (a label for the market; nothing is keyed on it): the band alone vs kept as set at 0.015")
for ser in SERIES:
    g = [b for b in base if b["x"]["series"] == ser]
    kk = [b for b in rules[("p0", BAR)][0] if b["x"]["series"] == ser]
    print(f"   {ser:10s} band {line(g)}  ||  kept {line(kk)}")
print("DONE")
