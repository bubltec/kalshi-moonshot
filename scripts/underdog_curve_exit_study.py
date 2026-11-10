"""Does a curve-value exit beat holding for underdogs? (2026-11-10, user: "yes, test it", after studies/results/2026-11-10-underdog-exit-study.txt found that flat take-profit and stop rules lose to holding.)
Question: on the same underdog-priced tape states (each side's first 0.20-0.40 ask 30 s-7.5 min into a settled 15-minute crypto market since 10-29, bought at the ask plus the buy fee, bid history of at least 25 s before the entry), is selling when the bid net of the sell fee is at least the curve's own chance that the side pays plus a margin better than holding to the result?
Curve chance (as curvevalue.py, live on commodity_multiscale): P = Phi(c / (r * sqrt(s))) with c the signed log cushion of the index over the strike, s the seconds left, and r a rate read at the horizon scale: "60s900" = rms of 60 s moves over the last 900 s divided by sqrt(60); "60s900+60s180" = the lower P of that and the same over the last 180 s (spike inside); "1s900" = the rms one-second move over 900 s (the older reading). Sale at the bid two seconds after the condition has held for the persistence; nothing in the last 8 s; held to the result is the baseline. Fee on both legs.
Grid fixed before the results (the choice is made after seeing them and is said to be so): scales as above; margin 0.00 / 0.03 / 0.06 / 0.10 / 0.15; persistence 0 / 5 s.
Subsets: ALL states and GATED (the hour's drift against the bet at or under the sample's 25th percentile, the live gate; the cutoff is from the whole sample). A variant counts only when positive against holding in both halves and in each price third it claims.

    scripts/cloud_job.sh scripts/underdog_curve_exit_study.py
"""

import array
import calendar
import itertools
import math
import os
import time

from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

os.nice(10)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
SINCE = float(os.environ.get("SINCE", calendar.timegm((2026, 10, 29, 0, 0, 0))))
NOW = float(os.environ.get("UNTIL", time.time()))
NAN = float("nan")

COINS = {"BTC": "KXBTC15M", "ETH": "KXETH15M", "SOL": "KXSOL15M", "XRP": "KXXRP15M", "DOGE": "KXDOGE15M", "BNB": "KXBNB15M", "ZEC": "KXZEC15M", "HYPE": "KXHYPE15M", "NEAR": "KXNEAR15M"}
IDX = {c: variants.crypto_index(ser) for c, ser in COINS.items()}
IDX = {c: i for c, i in IDX.items() if i}
PERP = {"BTC", "ETH", "SOL", "XRP", "DOGE"}

wins = st.db.execute(
    "SELECT ticker,close_ts,strike,result FROM windows WHERE result IN ('yes','no') AND close_ts>? AND close_ts<? AND ticker LIKE 'KX%15M-%' ORDER BY close_ts", (SINCE + 600, NOW)
).fetchall()
wins = [w for w in wins if w["ticker"].split("-")[0] in COINS.values() and w["strike"]]
T0 = int(SINCE) - 3700
T1 = int(max(float(w["close_ts"]) for w in wins)) + 5
N = T1 - T0 + 1
print(f"markets {len(wins)} | coins {sorted(IDX)} | seconds {N}", flush=True)


def load_index(ix):
    g = array.array("d", [NAN]) * N
    day = 86400
    a = T0
    while a < T1:
        b = min(a + day, T1 + 1)
        for ts, v in tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>=? AND ts<? AND value>0 ORDER BY ts", (ix, a, b)).fetchall():
            g[int(ts) - T0] = math.log(float(v))
        a = b
    last = NAN
    for k in range(N):
        if g[k] == g[k]:
            last = g[k]
        else:
            g[k] = last
    c60 = array.array("d", [0.0]) * (N + 1)
    for k in range(60, N):
        a_, b_ = g[k], g[k - 60]
        c60[k + 1] = c60[k] + (((a_ - b_) ** 2) / 60.0 if a_ == a_ and b_ == b_ else 0.0)
    sq, ab = array.array("d", [0.0]) * (N + 1), array.array("d", [0.0]) * (N + 1)
    for k in range(1, N):
        d = g[k] - g[k - 1]
        d = d if d == d else 0.0
        sq[k + 1] = sq[k] + d * d
        ab[k + 1] = ab[k] + abs(d)
    return g, sq, ab, c60


G = {c: load_index(i) for c, i in IDX.items() if c in COINS}
print("indexes loaded", flush=True)




def unit(c, k, w=900):
    sq = G[c][1]
    v = (sq[k + 1] - sq[k + 1 - w]) / w
    return math.sqrt(v) * math.sqrt(60.0) if v > 0 else None


SETS = {"1s900": ("s",), "60s900": ("a",), "60s900+60s180": ("a", "b")}
MARGINS = (0.0, 0.03, 0.06, 0.10, 0.15)
PERSIST = (0, 5)
DELAY, LAST = 2, 8
SQ60 = math.sqrt(60.0)


def phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


states = []
for wi, w in enumerate(wins):
    tk, close, strike = w["ticker"], float(w["close_ts"]), float(w["strike"])
    coin = next(c for c, ser in COINS.items() if ser == tk.split("-")[0])
    if coin not in G:
        continue
    g, sq, ab, c60 = G[coin]
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (tk, close - 900, close)).fetchall()
    taken = set()
    qt = [float(x[0]) for x in q]
    for ts, bid, ask in q:
        ts = float(ts)
        if not (close - 900 + 30 <= ts <= close - 900 + 450):
            continue
        yb, ya = float(bid), float(ask)
        if ya - yb > 0.06:
            continue
        for side, a in (("yes", ya), ("no", 1.0 - yb)):
            if side in taken or not (0.20 <= a <= 0.40):
                continue
            taken.add(side)
            k = int(ts) - T0
            if k < 3700 or k >= N - 5:
                continue
            sign = 1.0 if side == "yes" else -1.0
            open_t, end_t = int(close - 900), int(close)
            bids, j = [], 0
            for t in range(open_t, end_t + 1):
                while j + 1 < len(qt) and qt[j + 1] <= t:
                    j += 1
                bids.append((float(q[j][1]) if side == "yes" else 1.0 - float(q[j][2])) if qt[j] <= t and t - qt[j] <= 20 else None)
            e = int(ts) - open_t
            seq = [b for b in bids[: e + 1] if b is not None]
            if len(seq) < 25:
                continue
            ul = unit(coin, k, 3600)
            if not ul:
                continue
            ls = math.log(strike)
            last_i = len(bids) - 1 - LAST
            pv = {name: [None] * len(bids) for name in SETS}
            for i in range(e + 1, last_i):
                t = open_t + i
                kk = t - T0
                left = close - t
                c = (g[kk] - ls) * sign
                rs = (math.sqrt((sq[kk + 1] - sq[kk + 1 - 900]) / 900.0), math.sqrt((c60[kk + 1] - c60[kk + 1 - 900]) / 900.0), math.sqrt((c60[kk + 1] - c60[kk + 1 - 180]) / 180.0))
                root = math.sqrt(left)

                def p_of(r):
                    return phi(c / (r * root)) if r > 0 else (1.0 if c > 0 else 0.0)

                p_s, p_a, p_b = p_of(rs[0]), p_of(rs[1]), p_of(rs[2])
                pv["1s900"][i] = p_s
                pv["60s900"][i] = p_a
                pv["60s900+60s180"][i] = min(p_a, p_b)
            states.append(dict(ts=ts, ask=a, win=w["result"] == side, e=e, bids=bids, pv=pv, hour=(g[k] - g[k - 3600]) * sign / ul))
    if wi % 1000 == 0:
        print(f"  {wi} markets, {len(states)} states", flush=True)
print(f"\nstates {len(states)} | win {sum(r['win'] for r in states) / len(states):.3f} | mean ask {sum(r['ask'] for r in states) / len(states):.3f}", flush=True)

hours = sorted(r["hour"] for r in states)
hcut = hours[len(hours) // 4]
mid = sorted(r["ts"] for r in states)[len(states) // 2]
asks = sorted(r["ask"] for r in states)
a1, a2 = asks[len(asks) // 3], asks[2 * len(asks) // 3]


def third(a):
    return "low" if a < a1 else "mid" if a < a2 else "high"


def run(r, name, m, persist):
    a = r["ask"]
    cost = a + fee(a)
    bids, p = r["bids"], r["pv"][name]
    since = None
    for i in range(r["e"] + 1, len(bids) - LAST):
        b = bids[i]
        if b is None or p[i] is None or b < 0.03:
            since = None
            continue
        if b - fee(b) >= p[i] + m:
            if since is None:
                since = i
            if i - since >= persist:
                s_ = bids[i + DELAY] if i + DELAY < len(bids) else None
                if s_ is None or s_ <= 0.0:
                    continue
                got = s_ - fee(s_)
                return (got - cost) / cost, 1, 1 if got > cost else 0
        else:
            since = None
    return ((1.0 if r["win"] else 0.0) - cost) / cost, 0, 0


def report(name_, rows):
    n = len(rows)
    base = [((1.0 if r["win"] else 0.0) - (r["ask"] + fee(r["ask"]))) / (r["ask"] + fee(r["ask"])) for r in rows]
    print(f"\n=== {name_}: {n} states | held net per $ {sum(base) / n:+.4f}", flush=True)
    for pn in ("low", "mid", "high"):
        sub = [x for r, x in zip(rows, base) if third(r["ask"]) == pn]
        print(f"  held, {pn} ask third: n={len(sub)} net/$ {sum(sub) / max(len(sub), 1):+.4f}")
    out = []
    for name in SETS:
        for m in MARGINS:
            for ps in PERSIST:
                res = [run(r, name, m, ps) for r in rows]
                d = [x[0] - b for x, b in zip(res, base)]
                h1 = [x for x, r in zip(d, rows) if r["ts"] <= mid]
                h2 = [x for x, r in zip(d, rows) if r["ts"] > mid]
                by = {pn: sum(x for x, r in zip(d, rows) if third(r["ask"]) == pn) / max(sum(1 for r in rows if third(r["ask"]) == pn), 1) for pn in ("low", "mid", "high")}
                won = sum(1 for x, r in zip(res, rows) if x[1] and r["win"])
                out.append((sum(d) / n, name, m, ps, sum(x[1] for x in res), sum(x[2] for x in res), won, sum(h1) / max(len(h1), 1), sum(h2) / max(len(h2), 1), sum(x[0] for x in res) / n, by))
    out.sort(key=lambda o: -o[0])
    print("  (scales, margin, persist): change in net per $ against holding, best 12 then worst 3")
    for o in out[:12] + out[-3:]:
        print(f"    {o[1]:>14s} m {o[2]:.2f} persist {o[3]}: {o[0]:+.4f} (net {o[9]:+.4f}) sold {o[4]} ({o[5]} at a profit, {o[6]} would have won) | halves {o[7]:+.4f} / {o[8]:+.4f} | low {o[10]['low']:+.4f} mid {o[10]['mid']:+.4f} high {o[10]['high']:+.4f}")
    both = [o for o in out if o[7] > 0 and o[8] > 0]
    print(f"  positive against holding in both halves: {len(both)} of {len(out)}")


report("ALL underdog states", states)
report("GATED (hour drift against the bet, at or under the sample's 25th percentile)", [r for r in states if r["hour"] <= hcut])
print("DONE")
