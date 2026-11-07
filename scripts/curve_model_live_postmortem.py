"""Post-mortem of the curve model's live bets (2026-11-07, user: "5 total losses though ... are we learning anything from our losses?").

Question: on the markets that settled after the model was fit (the model has not seen them), does its top-20% pick pay what it predicted, and what did the five losses of the 16:00 ET window have in common?
A. The model lane's live bets since the fit: n, win rate against the price paid, net in dollars (fee included, resting fills pay none).
B. The forward test: every moment (30 s step, the lane's price and spread bands) of every settled window after the fit, both sides, scored by the model that was live. The pick is the live rule: predicted net at or over the model's top-20% cut and above zero. Net per $1 staked after the fee, held to the result; each reading once per market side ("first") as the bot buys, and every moment. Two halves of the period, and by curve shape: the direction of the cushion at 13 minutes (up, flat, down by the slope's angle: 26.6 degrees either way) and whether its variability at 13 minutes is over the training median.
C. Same-window clustering: per window, how many market sides the rule picks, how many of them the same direction (the cushion's sign is the side; "yes"/"no" shows the direction of the spot), and how they settled.
D. The curve readings of each live loss against the live winners.
Not covered: queue position, the bid that rests first (picks are scored at the ask), and any market the history job has not settled yet (windows lag about an hour). The live bets are few, the forward test is the larger sample.

    scripts/hostrun3.sh-style: docker compose exec -T bot python - < scripts/curve_model_live_postmortem.py
"""

import bisect
import collections
import itertools
import math
import os
import statistics
import time
from datetime import datetime, timezone

from kalshi_moonshot import config, curvemodel, db, variants
from kalshi_moonshot.kalshiws import RECORD_ONLY
from kalshi_moonshot.lanes import lane_specs
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

os.nice(15)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
model = curvemodel.load(s)
built = float(model["built_at"])
cut20 = float(model["cuts"]["0.2"])
print("model built", datetime.fromtimestamp(built, timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "rows", model["n"], "mean net", round(model["mean_net"], 3), "top-20% cut", round(cut20, 3))
NAMES = model["names"]
ix = {n: i for i, n in enumerate(NAMES)}
hm = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%H:%M")

# A. the live bets
rows = st.db.execute("SELECT ts,ticker,side,ask,contracts,stake_usd,outcome FROM decisions WHERE action='bet' AND lane='crypto_multiscale' AND mode='trade' AND ts>? ORDER BY ts", (built,)).fetchall()
print("\nA. live model bets since the fit:", len(rows))
tot = 0.0
stake = 0.0
won_n = 0
settled = 0
for r in rows:
    if r["outcome"] in ("yes", "no"):
        settled += 1
        w = r["outcome"] == r["side"]
        won_n += w
        pnl = (r["contracts"] - r["stake_usd"]) if w else -r["stake_usd"]
        tot += pnl
        stake += r["stake_usd"]
print(f"   settled {settled}, won {won_n} ({100 * won_n / max(settled, 1):.0f}%), staked ${stake:.2f}, net ${tot:+.2f} ({100 * tot / max(stake, 1e-9):+.0f}% of stake)")

# B. the forward test
rules = {}
for sp in lane_specs(s).values():
    if sp.enabled and sp.model_buy:
        for series in sp.series:
            lo = max(sp.entry_elapsed[0] * 60.0, 900.0 - sp.hours[1] * 3600.0)
            hi = min(sp.entry_elapsed[1] * 60.0, 900.0 - sp.hours[0] * 3600.0)
            rules[series] = (lo, hi, sp.price[0], sp.price[1], sp.max_spread if sp.max_spread > 0 else 1.0)
now = time.time()
g0 = int(built - 3600)
g1 = int(now) + 2
wins = st.db.execute("SELECT ticker,series,close_ts,strike,result FROM windows WHERE close_ts>? AND result IN ('yes','no') AND strike>0 ORDER BY series,close_ts", (built + 900,)).fetchall()
picks = []  # (close, ticker, side, e, ask, net, pred, readings, won)
every = 0
cur = None
for w in wins:
    ser = w["series"]
    if ser not in rules or ser in RECORD_ONLY:
        continue
    ixid = variants.crypto_index(ser)
    if not ixid:
        continue
    if ixid != cur:
        cur = ixid
        r = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (ixid, g0 - 60)).fetchall()
        ts = [float(x[0]) for x in r]
        vs = [float(x[1]) for x in r]
        g, j, last = [], 0, None
        for t in range(g0, g1):
            while j < len(ts) and ts[j] <= t:
                last = vs[j]
                j += 1
            g.append(math.log(last) if last else None)
        sq = [0.0]
        for a, b in itertools.pairwise(g):
            sq.append(sq[-1] + ((b - a) ** 2 if a is not None and b is not None else 0.0))
    lo, hi, plo, phi, cap = rules[ser]
    close = float(w["close_ts"])
    opened = close - 900.0
    sl = math.log(float(w["strike"]))
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (w["ticker"], opened - 20, close)).fetchall()
    if len(q) < 20:
        continue
    qt = [float(x[0]) for x in q]
    for e in range(max(60, int(lo) // 30 * 30), min(841, int(hi) + 1), 30):
        if e < lo:
            continue
        at = opened + e
        i = bisect.bisect_right(qt, at) - 1
        if i < 0 or at - qt[i] > 20:
            continue
        yb, ya = float(q[i][1]), float(q[i][2])
        k = int(at) - g0
        if k < 0 or k >= len(g):
            continue
        for side, bid, ask in (("yes", yb, ya), ("no", 1 - ya, 1 - yb)):
            if not (plo <= ask <= phi) or ask - bid > cap:
                continue
            read = curvemodel.features(g, sq, k, sl, 1 if side == "yes" else -1)
            if read is None:
                continue
            won = w["result"] == side
            c = 1.0 / ask
            net = ((1 - ask) * c if won else -1.0) - fee(ask) * c
            pred = curvemodel.predict(model, read, ask)
            every += 1
            picks.append((close, w["ticker"], side, e, ask, net, pred, read, won))
print(f"\nB. forward test: {len(wins)} settled windows after the fit, {every} moments scored")
mid = statistics.median(p[0] for p in picks) if picks else 0
vmed = model["mu"][ix["var_13m"]]


def shape(p):
    sl13 = p[7][ix["slope_13m"]]
    d = "up" if sl13 >= 0.5 else "down" if sl13 <= -0.5 else "flat"
    return d + ("/wide" if p[7][ix["var_13m"]] >= vmed else "/tight")


def line(label, ps):
    if not ps:
        print(f"   {label:34s} n=0")
        return
    n = len(ps)
    print(f"   {label:34s} n={n:5d} win {100 * sum(p[8] for p in ps) / n:3.0f}% vs ask {sum(p[4] for p in ps) / n:.2f} | pred {sum(p[6] for p in ps) / n:+.3f} actual {sum(p[5] for p in ps) / n:+.3f} per $1")


sel = [p for p in picks if p[6] >= cut20 and p[6] > 0]
first = {}
for p in sorted(sel, key=lambda x: x[3]):
    first.setdefault((p[1], p[2]), p)
first = list(first.values())
print("   every moment vs the live rule (top-20% cut, above zero):")
line("every moment", picks)
line("pick, every moment", sel)
line("pick, first moment per market side", first)
line("pick, first, first half", [p for p in first if p[0] <= mid])
line("pick, first, second half", [p for p in first if p[0] > mid])
print("   by predicted net (first moment per market side, any prediction over zero):")
fp = {}
for p in sorted(picks, key=lambda x: x[3]):
    if p[6] > 0:
        fp.setdefault((p[1], p[2]), p)
fp = list(fp.values())
for a, b in ((0, 0.02), (0.02, 0.05), (0.05, 0.10), (0.10, 9)):
    line(f"predicted {a:+.2f} to {b:+.2f}", [p for p in fp if a <= p[6] < b])
print("   by curve shape (pick, first moment per market side):")
for sh in sorted({shape(p) for p in first}):
    line(sh, [p for p in first if shape(p) == sh])
print("   by price paid (pick, first):")
for a, b in ((0.2, 0.35), (0.35, 0.5), (0.5, 0.71)):
    line(f"ask {a:.2f} to {b:.2f}", [p for p in first if a <= p[4] < b])

# C. clustering
print("\nC. picks per window (pick, first moment per market side): windows with 3 or more picks")
byw = collections.defaultdict(list)
for p in first:
    byw[p[0]].append(p)
for cl in sorted(byw):
    ps = byw[cl]
    if len(ps) >= 3:
        yes = sum(p[2] == "yes" for p in ps)
        print(f"   close {hm(cl)}: {len(ps)} picks, {yes} yes / {len(ps) - yes} no, won {sum(p[8] for p in ps)}, net {sum(p[5] for p in ps):+.2f} per $1 each")
sizes = collections.Counter(len(v) for v in byw.values())
print("   windows by picks:", dict(sorted(sizes.items())))
same = [max(sum(p[2] == "yes" for p in ps), sum(p[2] == "no" for p in ps)) / len(ps) for ps in byw.values() if len(ps) >= 2]
print("   share of picks that point the same way inside a window with 2+ picks:", round(sum(same) / max(len(same), 1), 2), "over", len(same), "windows")
for n_ in sorted({len(v) for v in byw.values()}):
    ws = [v for v in byw.values() if len(v) == n_]
    print(f"   windows with {n_} picks: n={len(ws)}, net per pick {sum(p[5] for v in ws for p in v) / (n_ * len(ws)):+.3f}")

# D. the live losses against the live winners
print("\nD. curve readings at each live bet (cushion slope in typical moves; var in typical moves; the live prediction is in the log)")
bytk = {}
for p in picks:
    bytk.setdefault((p[1], p[2]), []).append(p)
for r in rows:
    if r["outcome"] not in ("yes", "no"):
        continue
    cands = [p for p in bytk.get((r["ticker"], r["side"]), []) if abs((p[0] - 900 + p[3]) - r["ts"]) <= 20]
    if not cands:
        continue
    p = cands[0]
    rd = p[7]
    tag = "WON " if r["outcome"] == r["side"] else "LOST"
    print(f"   {tag} {hm(r['ts'])} {r['side']:3s} ask {r['ask']:.2f} pred {p[6]:+.3f} | slope 3m {rd[ix['slope_3m']]:+.2f} 8m {rd[ix['slope_8m']]:+.2f} 21m {rd[ix['slope_21m']]:+.2f} 55m {rd[ix['slope_55m']]:+.2f} | var 3m {rd[ix['var_3m']]:.2f} 13m {rd[ix['var_13m']]:.2f} 55m {rd[ix['var_55m']]:.2f}")
print("DONE")
