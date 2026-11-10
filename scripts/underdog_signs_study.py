"""Are there signs, external to Kalshi, that an underdog is about to break out? (2026-11-10, user: "they are definitely external to kalshi but there are signs"; the signs they watch by hand: perp price and flow, the lead coin and breadth, outside markets.)
Question: among ALL underdog-priced states on the tape (not only the 41 live bets), does anything read outside Kalshi's own quote separate the sides that went on to pay from the ones that did not, enough to beat the price paid after the fee, in both halves of the period?
Population: every settled 15-minute crypto market since 10-29, each side, at the FIRST moment between 30 s and 7.5 min after the open when that side's ask is 0.20-0.40 (the lane's own band and window), spread <= 0.06. One row per market side. Label: the side settled at $1. Net per $ is the held-to-result return at that ask after the buy fee (the settlement pays no fee).
Readings (all signed with the bet, so positive = it leans the bet's way, and all in the market's own units: typical minute = rms one-second log move of the 900 s before, times sqrt(60); nothing fixed, no market named):
  own curve (internal, for reference): cushion (spot to strike), hour drift, regime ratio (180 s / 3600 s rms), path efficiency of the 5 minutes
  lead coin and breadth (from the other coins' own index ticks): lead60 / lead180 = the lead coin's 60 s / 180 s move (BTC, or ETH for the BTC market); breadth60 = mean of the other coins' 60 s moves; n_with = how many of the other coins moved at least one typical minute with the bet over 60 s; gap60 = the market's own 60 s move minus the mean of the others (a coin lagging the pack)
  perp (Hyperliquid, BTC ETH SOL XRP DOGE only): perp_lead60 / perp_lead180 = the perp's move minus the index's move over the window (the perp running ahead of the settlement index); perp_move60; flow10 / flow30 / flow60 = taker net dollars with the bet over the last 10/30/60 s divided by that perp's own normal (rms one-second net flow over 900 s times sqrt(h))
Outputs: the base rate against the implied one; each reading's quartiles (n, win rate, net per $, the two halves of the net); then pairs of top-quartile readings with at least 80 rows, positive in both halves. 14 readings and their pairs were tried, so a bin that looks good among that many is a lead, not a rule; anything kept needs a walk-forward on days it was not chosen from.

    scripts/cloud_job.sh scripts/underdog_signs_study.py
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
    sq, ab = array.array("d", [0.0]) * (N + 1), array.array("d", [0.0]) * (N + 1)
    for k in range(1, N):
        d = g[k] - g[k - 1]
        d = d if d == d else 0.0
        sq[k + 1] = sq[k] + d * d
        ab[k + 1] = ab[k] + abs(d)
    return g, sq, ab


G = {c: load_index(i) for c, i in IDX.items()}
print("indexes loaded", flush=True)


def load_perp(sym):
    px = array.array("d", [NAN]) * N
    net = array.array("d", [0.0]) * N
    for ts, p, b, sl in tape.execute("SELECT ts,px,buy_usd,sell_usd FROM fut_1s WHERE symbol=? AND ts>=? AND ts<=? ORDER BY ts", (sym, T0, T1)).fetchall():
        k = int(ts) - T0
        px[k] = math.log(float(p))
        net[k] += float(b) - float(sl)
    last = NAN
    for k in range(N):
        if px[k] == px[k]:
            last = px[k]
        else:
            px[k] = last
    cum, c2 = array.array("d", [0.0]) * (N + 1), array.array("d", [0.0]) * (N + 1)
    for k in range(N):
        cum[k + 1] = cum[k] + net[k]
        c2[k + 1] = c2[k] + net[k] * net[k]
    return px, cum, c2


P = {c: load_perp(c) for c in PERP if c in IDX}
print("perps loaded", flush=True)


def unit(c, k, w=900):
    sq = G[c][1]
    v = (sq[k + 1] - sq[k + 1 - w]) / w
    return math.sqrt(v) * math.sqrt(60.0) if v > 0 else None


def mv(c, k, w):
    g = G[c][0]
    return g[k] - g[k - w]


rows = []
for wi, w in enumerate(wins):
    tk, close, strike = w["ticker"], float(w["close_ts"]), float(w["strike"])
    coin = next(c for c, ser in COINS.items() if ser == tk.split("-")[0])
    if coin not in IDX:
        continue
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (tk, close - 900 + 30, close - 900 + 450)).fetchall()
    taken = set()
    for ts, bid, ask in q:
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
            u = unit(coin, k)
            ul = unit(coin, k, 3600)
            ush = unit(coin, k, 180)
            if not u or not ul or not ush:
                continue
            g, sq, ab = G[coin]
            f = {"cushion": (g[k] - math.log(strike)) * sign / u, "hour": mv(coin, k, 3600) * sign / ul, "ratio": ush / ul}
            path = ab[k + 1] - ab[k + 1 - 300]
            f["eff"] = abs(g[k] - g[k - 300]) / path if path > 0 else NAN
            others = [c for c in G if c != coin]
            m60 = {c: (mv(c, k, 60) * sign / u_) if (u_ := unit(c, k)) else NAN for c in others}
            m180 = {c: (mv(c, k, 180) * sign / u_) if (u_ := unit(c, k)) else NAN for c in others}
            ok = [v for v in m60.values() if v == v]
            lead = "ETH" if coin == "BTC" else "BTC"
            f["lead60"], f["lead180"] = m60.get(lead, NAN), m180.get(lead, NAN)
            f["breadth60"] = sum(ok) / len(ok) if ok else NAN
            f["n_with"] = float(sum(1 for v in ok if v >= 1.0))
            own60 = mv(coin, k, 60) * sign / u
            f["gap60"] = own60 - f["breadth60"] if ok else NAN
            if coin in P:
                px, cum, c2 = P[coin]
                for hh in (60, 180):
                    f[f"perp_lead{hh}"] = ((px[k] - px[k - hh]) - mv(coin, k, hh)) * sign / u
                f["perp_move60"] = (px[k] - px[k - 60]) * sign / u
                rms = math.sqrt((c2[k + 1] - c2[k + 1 - 900]) / 900.0)
                for hh in (10, 30, 60):
                    f[f"flow{hh}"] = sign * (cum[k + 1] - cum[k + 1 - hh]) / (rms * math.sqrt(hh)) if rms > 0 else NAN
            won = w["result"] == side
            cost = a + fee(a)
            f.update(ts=float(ts), win=1.0 if won else 0.0, ret=((1.0 if won else 0.0) - cost) / cost, ask=a)
            rows.append(f)
    if wi % 1000 == 0:
        print(f"  {wi} markets, {len(rows)} rows", flush=True)

print(f"\nunderdog-priced states: {len(rows)} | win rate {sum(r['win'] for r in rows) / len(rows):.3f} | mean ask {sum(r['ask'] for r in rows) / len(rows):.3f} | net per $ {sum(r['ret'] for r in rows) / len(rows):+.4f}")
mid = sorted(r["ts"] for r in rows)[len(rows) // 2]
FEATS = ["cushion", "hour", "ratio", "eff", "lead60", "lead180", "breadth60", "n_with", "gap60", "perp_lead60", "perp_lead180", "perp_move60", "flow10", "flow30", "flow60"]


def stats(rs):
    n = len(rs)
    if not n:
        return "n=0"
    h1 = [r["ret"] for r in rs if r["ts"] <= mid]
    h2 = [r["ret"] for r in rs if r["ts"] > mid]
    return f"n={n:5d} win {sum(r['win'] for r in rs) / n:.3f} net/$ {sum(r['ret'] for r in rs) / n:+.3f} | halves {sum(h1) / max(len(h1), 1):+.3f} (n={len(h1)}) / {sum(h2) / max(len(h2), 1):+.3f} (n={len(h2)})"


print("\nquartiles of each reading (Q1 lowest ... Q4 highest; positive = leans the bet's way):")
cuts = {}
for f in FEATS:
    xs = sorted(r[f] for r in rows if f in r and r[f] == r[f])
    if len(xs) < 200:
        continue
    cuts[f] = (xs[len(xs) // 4], xs[len(xs) // 2], xs[3 * len(xs) // 4])
    print(f"\n{f}  (cuts {cuts[f][0]:+.2f} {cuts[f][1]:+.2f} {cuts[f][2]:+.2f}, n={len(xs)})")
    c1, c2, c3 = cuts[f]
    have = [r for r in rows if f in r and r[f] == r[f]]
    for name, test in (("Q1", lambda v: v <= c1), ("Q2", lambda v: c1 < v <= c2), ("Q3", lambda v: c2 < v <= c3), ("Q4", lambda v: v > c3)):
        print(f"  {name} {stats([r for r in have if test(r[f])])}")

print("\npairs of top-quartile readings (both above their own 75th percentile), n >= 80, positive in both halves, best 12 by net per $:")
tops = [f for f in cuts]
res = []
for a, b in itertools.combinations(tops, 2):
    rs = [r for r in rows if a in r and b in r and r[a] == r[a] and r[b] == r[b] and r[a] > cuts[a][2] and r[b] > cuts[b][2]]
    if len(rs) < 80:
        continue
    h1 = [r["ret"] for r in rs if r["ts"] <= mid]
    h2 = [r["ret"] for r in rs if r["ts"] > mid]
    if not h1 or not h2 or sum(h1) <= 0 or sum(h2) <= 0:
        continue
    res.append((sum(r["ret"] for r in rs) / len(rs), a, b, rs))
res.sort(key=lambda x: -x[0])
for _, a, b, rs in res[:12]:
    print(f"  {a} & {b}: {stats(rs)}")
print(f"  ({len(list(itertools.combinations(tops, 2)))} pairs tried, {len(res)} positive in both halves)")
print("DONE")
