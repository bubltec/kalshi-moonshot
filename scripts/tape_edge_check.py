"""Does the curve chance against the price predict the result of a mid-priced buy across the whole tape? (2026-11-11, the independent check on studies/results/2026-11-11-multiscale-buy.txt, whose sample is the lanes' own bets over six days.)
Positions: every settled 15-minute window with a spot index (1 in STRIDE), both sides; the first second in minutes 1.5-14 where the side's bid + 0.02 is in 0.55-0.80; one contract at bid + 0.02 plus the fee (the spread is assumed), held to settlement. These are NOT the lanes' picks (the lanes buy on the curve model); the question is only whether edge = curve chance (900 s horizon rate) - price paid sorts results. Buckets fixed before results. Each bucket: positions, won, net held per contract-dollar and per dollar staked, the two halves by time. Then two fixed filters (edge >= -0.05, edge >= 0): kept against all, and on how many UTC days the kept set beat buying all per dollar staked. Also by cushion and by minutes elapsed, the two other readings that split the lanes' own bets.
    scripts/cloud_job.sh scripts/tape_edge_check.py STRIDE=4
"""

import bisect
import calendar
import collections
import itertools
import math
import os

from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

os.nice(10)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)

GROUPS = {
    "crypto": (("crypto_multiscale", "crypto_multiscale_underdog"), 300.0, int(os.environ.get("SINCE_CRYPTO", "1794000000"))),
    "commodity_multiscale": (("commodity_multiscale",), 300.0, calendar.timegm((2026, 11, 9, 14, 0, 0))),
    "favorites": (("commodity_energy", "commodity_wti", "commodity_precious", "commodity_industrial"), 120.0, calendar.timegm((2026, 11, 3, 0, 0, 0))),
}
MEM, SEC = 120, 60
BAND = (0.05, 0.89)


def load(lanes, since):
    marks = ",".join("?" * len(lanes))
    rows = st.db.execute(
        "SELECT d.ts,d.ticker,d.lane,d.side,d.contracts,d.stake_usd,d.exit_price,w.close_ts,w.strike,w.result FROM decisions d JOIN windows w ON w.ticker=d.ticker "
        f"WHERE d.action='bet' AND d.mode='trade' AND d.contracts>0 AND d.stake_usd>0 AND d.ts>? AND d.lane IN ({marks}) AND w.result IN ('yes','no') ORDER BY d.ts",
        (since, *lanes),
    ).fetchall()
    seen = {}
    for r in rows:
        seen[(r["ticker"], r["side"])] = r
    return list(seen.values())


def prepare(r):
    """The per-second readings the exit sees from the buy to the close, plus the curve shape at the buy."""
    ser = r["ticker"].split("-")[0]
    crypto = variants.crypto_index(ser)
    ix = crypto or variants.biquote_index(ser)
    if not ix:
        return None
    close, t0 = float(r["close_ts"]), float(r["ts"])
    tk = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts", (ix, t0 - 1500, close)).fetchall()
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (r["ticker"], t0 - 5, close)).fetchall()
    if len(tk) < 1000 or len(q) < 5:
        return None
    T = [float(x[0]) for x in tk]
    V = [float(x[1]) for x in tk]
    if crypto:
        if not r["strike"] or float(r["strike"]) <= 0:
            return None
        ref = math.log(float(r["strike"]))
    else:
        opened = close - 900.0
        a = bisect.bisect_left(T, opened - 5.0)
        if a >= len(T) or T[a] > opened + 20.0:
            return None
        ref = math.log(V[a])
    base = int(T[0])
    g, j, last = [], 0, None
    for t in range(base, int(close) + 1):
        while j < len(T) and T[j] <= t:
            last = V[j]
            j += 1
        g.append(math.log(last) if last else None)
    c60 = [0.0] * len(g)
    for j in range(60, len(g)):
        c60[j] = c60[j - 1] + (((g[j] - g[j - 60]) ** 2) / 60.0 if g[j] is not None and g[j - 60] is not None else 0.0)
    sq = [0.0]
    for x, y in itertools.pairwise(g):
        sq.append(sq[-1] + ((y - x) ** 2 if x is not None and y is not None else 0.0))
    kb = int(t0) - base
    seg = [x for x in g[max(kb - 300, 0) : kb + 1] if x is not None]
    path = sum(abs(y - x) for x, y in itertools.pairwise(seg))
    eff = abs(seg[-1] - seg[0]) / path if len(seg) > 30 and path > 0 else None
    sign = 1.0 if r["side"] == "yes" else -1.0
    qt = [float(x[0]) for x in q]
    moves = collections.deque()
    reads = []
    for t in range(int(t0), int(close) - 2):
        k = t - base
        if k < 960 or k >= len(g) or g[k] is None or g[k - SEC] is None:
            continue
        moves.append((t, (g[k] - g[k - SEC]) * sign))
        while moves and t - moves[0][0] > MEM:
            moves.popleft()
        left = close - t
        if left > 900:
            continue
        rates = (math.sqrt((sq[k] - sq[k - 900]) / 900), math.sqrt((c60[k] - c60[k - 900]) / 900), math.sqrt((c60[k] - c60[k - 180]) / 180))
        if rates[0] <= 0 or rates[1] <= 0 or rates[2] < 0:
            continue
        i = bisect.bisect_right(qt, t) - 1
        if i < 0 or t - qt[i] > 20:
            continue
        yb, ya = float(q[i][1]), float(q[i][2])
        bid = yb if r["side"] == "yes" else 1.0 - ya
        reads.append((t, left, (g[k] - ref) * sign, rates, bid))
    return eff, reads


def phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


import collections
import time as _time

STRIDE = int(os.environ.get("STRIDE", "4"))
SINCE_S = int(os.environ.get("SINCE_S", str(calendar.timegm((2026, 10, 28, 0, 0, 0)))))
wins = st.db.execute("SELECT ticker, close_ts, strike, result FROM windows WHERE result IN ('yes','no') AND close_ts > ? ORDER BY close_ts", (SINCE_S,)).fetchall()
pos = []
used = 0
t_start = _time.time()
for k, w in enumerate(wins):
    ser = w["ticker"].split("-")[0]
    if not ser.endswith("15M") or not (variants.crypto_index(ser) or variants.biquote_index(ser)) or k % STRIDE:
        continue
    for side in ("yes", "no"):
        r = {"ticker": w["ticker"], "close_ts": w["close_ts"], "ts": float(w["close_ts"]) - 880.0, "side": side, "strike": w["strike"], "contracts": 1.0, "stake_usd": 1.0, "result": w["result"]}
        got = prepare(r)
        if not got:
            continue
        i0 = next((i for i, (t, left, c, rates, bid) in enumerate(got[1]) if 60 <= left <= 810 and 0.55 <= bid + 0.02 <= 0.80 and rates[0] > 0 and rates[1] > 0), None)
        if i0 is None:
            continue
        t, left, c, rates, bid = got[1][i0]
        px = bid + 0.02
        cost = px + fee(px)
        won = 1 if w["result"] == side else 0
        pos.append({"ts": t, "day": int(t // 86400), "stake": cost, "held": won - cost, "won": won, "edge": phi(c / (rates[1] * math.sqrt(left))) - cost, "cush": c / (rates[0] * math.sqrt(60.0)), "elapsed": (900 - left) / 60.0})
    used += 1
    if used % 400 == 0:
        print(f"  {used} windows, {len(pos)} positions, {_time.time() - t_start:.0f}s", flush=True)
pos.sort(key=lambda x: x["ts"])
mid = pos[len(pos) // 2]["ts"]
print(f"windows {used} (1 in {STRIDE}), positions {len(pos)} | held {sum(x['held'] for x in pos):+.2f} on {sum(x['stake'] for x in pos):.2f} staked ({sum(x['held'] for x in pos) / sum(x['stake'] for x in pos):+.1%} per $)")


def table(title, key, order):
    print(f"  {title}")
    g = collections.defaultdict(list)
    for x in pos:
        g[key(x)].append(x)
    for k in order:
        r = g.get(k, [])
        if not r:
            continue
        st_ = sum(x["stake"] for x in r)
        net = sum(x["held"] for x in r)
        h1 = sum(x["held"] for x in r if x["ts"] <= mid)
        s1 = sum(x["stake"] for x in r if x["ts"] <= mid)
        print(f"    {k:18s} n {len(r):5d} | won {sum(x['won'] for x in r) / len(r):.0%} | held {net:+8.2f} ({net / st_:+.1%} per $) | halves {h1 / s1 if s1 else 0:+.1%} / {(net - h1) / (st_ - s1) if st_ > s1 else 0:+.1%}")


def b(x, cuts, labels):
    for c, lab in zip(cuts, labels):
        if x < c:
            return lab
    return labels[-1]


E = ("under -0.10", "-0.10 to -0.05", "-0.05 to 0", "0 to +0.05", "+0.05 to +0.10", "+0.10 and up")
table("curve chance minus price paid", lambda x: b(x["edge"], (-0.10, -0.05, 0.0, 0.05, 0.10), E), E)
C = ("behind (under 0)", "0 to 0.5", "0.5 to 1", "1 to 2", "2 and up")
table("cushion at the buy, typical minutes", lambda x: b(x["cush"], (0.0, 0.5, 1.0, 2.0), C), C)
M = ("under 4", "4 to 8", "8 to 11", "11 and up")
table("minutes elapsed at the buy", lambda x: b(x["elapsed"], (4, 8, 11), M), M)
days = sorted({x["day"] for x in pos})
for name, keep in (("edge >= -0.05", lambda x: x["edge"] >= -0.05), ("edge >= 0", lambda x: x["edge"] >= 0.0), ("edge >= +0.05", lambda x: x["edge"] >= 0.05)):
    K = [x for x in pos if keep(x)]
    beat = lost = 0
    for d in days:
        A = [x for x in pos if x["day"] == d]
        Kd = [x for x in A if keep(x)]
        if Kd and len(Kd) < len(A):
            a = sum(x["held"] for x in A) / sum(x["stake"] for x in A)
            kk = sum(x["held"] for x in Kd) / sum(x["stake"] for x in Kd)
            beat += kk > a
            lost += kk < a
    st_ = sum(x["stake"] for x in K)
    net = sum(x["held"] for x in K)
    h1 = sum(x["held"] for x in K if x["ts"] <= mid)
    s1 = sum(x["stake"] for x in K if x["ts"] <= mid)
    print(f"  filter {name}: kept {len(K)} of {len(pos)} | held {net:+.2f} ({net / st_:+.1%} per $) | halves {h1 / s1:+.1%} / {(net - h1) / (st_ - s1):+.1%} | days the kept set beat buying all per $: {beat}, lost: {lost} of {len(days)}")
print("DONE")
