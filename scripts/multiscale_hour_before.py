"""The hour before the buy, on the multiscale lanes' own bets (2026-11-11, user, on KXPLATINUM15M-26NOV110700 YES bought at 06:47 while platinum had fallen all hour: "even at the hour it looks pretty rough to bet up that early").
Question: on each lane's settled live bets at or over the 0.55 price floor, held to settlement, how did the bets do by the spot's move over the 60 minutes before the buy, signed with the bet and in the market's own units (the move divided by the typical minute at the buy times sqrt(60): "typical hours")? And the same for the 15 minutes before the buy. Buckets fixed before results; curve readings only. Also crossed with the minute of the buy (before / from minute 4).
    scripts/cloud.sh run scripts/multiscale_hour_before.py
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
import json

LANES = ("crypto_multiscale", "commodity_multiscale")
LO = 0.55
B = ("against by 1+", "against 0.3 to 1", "flat (within 0.3)", "with 0.3 to 1", "with by 1+")


def bk(u):
    return B[0] if u <= -1 else B[1] if u <= -0.3 else B[2] if u < 0.3 else B[3] if u < 1 else B[4]


for lane in LANES:
    q = st.db.execute(
        "SELECT d.ts,d.ticker,d.lane,d.side,d.contracts,d.stake_usd,d.exit_price,d.factors,w.close_ts,w.strike,w.result FROM decisions d JOIN windows w ON w.ticker=d.ticker "
        "WHERE d.action='bet' AND d.mode='trade' AND d.contracts>0 AND d.stake_usd>0 AND d.lane=? AND w.result IN ('yes','no') ORDER BY d.ts",
        (lane,),
    ).fetchall()
    seen = {}
    for r in q:
        seen[(r["ticker"], r["side"])] = r
    rows = []
    for r in seen.values():
        n, cost = float(r["contracts"]), float(r["stake_usd"])
        if cost / n < LO:
            continue
        got = prepare(r)
        if not got or not got[1]:
            continue
        t, left, c, rates, bid = got[1][0]
        if rates[0] <= 0:
            continue
        ser = r["ticker"].split("-")[0]
        ix = variants.crypto_index(ser) or variants.biquote_index(ser)
        t0 = float(r["ts"])
        def at(ts_):
            x = tape.execute("SELECT value FROM index_ticks WHERE index_id=? AND ts<=? AND ts>? AND value>0 ORDER BY ts DESC LIMIT 1", (ix, ts_, ts_ - 60)).fetchone()
            return float(x[0]) if x else None
        v0, v60, v15 = at(t0), at(t0 - 3600), at(t0 - 900)
        if not v0 or not v60 or not v15:
            continue
        sign = 1.0 if r["side"] == "yes" else -1.0
        unit_min = rates[0] * math.sqrt(60.0)
        try:
            el = json.loads(r["factors"]).get("elapsed_min") if r["factors"] else None
        except (TypeError, ValueError):
            el = None
        won = 1 if r["result"] == r["side"] else 0
        rows.append({"ts": t0, "stake": cost, "held": n * won - cost, "won": won, "h": math.log(v0 / v60) * sign / (unit_min * math.sqrt(60.0)), "q": math.log(v0 / v15) * sign / (unit_min * math.sqrt(15.0)), "el": el})
    rows.sort(key=lambda x: x["ts"])
    if len(rows) < 20:
        print(f"\n=== {lane}: {len(rows)} bets, too few")
        continue
    mid = rows[len(rows) // 2]["ts"]
    print(f"\n=== {lane}: {len(rows)} settled bets at {LO} or over with an hour of index before the buy | held {sum(x['held'] for x in rows):+.2f} on {sum(x['stake'] for x in rows):.2f}")

    def table(title, key, order):
        print(f"  {title}")
        g = collections.defaultdict(list)
        for x in rows:
            k = key(x)
            if k is not None:
                g[k].append(x)
        for k in order:
            r_ = g.get(k, [])
            if not r_:
                continue
            st_ = sum(x["stake"] for x in r_)
            net = sum(x["held"] for x in r_)
            h1 = sum(x["held"] for x in r_ if x["ts"] <= mid)
            print(f"    {k:34s} n {len(r_):4d} | won {sum(x['won'] for x in r_):3d} ({sum(x['won'] for x in r_) / len(r_):.0%}) | staked {st_:7.2f} | held {net:+7.2f} ({net / st_:+.1%} per $) | halves {h1:+.2f} / {net - h1:+.2f}")

    table("the 60 minutes before the buy, in typical hours, signed with the bet", lambda x: bk(x["h"]), B)
    table("the 15 minutes before the buy, same units", lambda x: bk(x["q"]), B)
    E = ("before minute 4", "from minute 4")
    table("the hour, crossed with the minute of the buy", lambda x: None if x["el"] is None else ("hour against (0.3+), " if x["h"] <= -0.3 else "hour not against, ") + (E[0] if float(x["el"]) < 4 else E[1]), ["hour against (0.3+), " + e for e in E] + ["hour not against, " + e for e in E])
print("DONE")
