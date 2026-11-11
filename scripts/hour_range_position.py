"""Where in the hour's range was the buy? (2026-11-11, user on the XRP and HYPE YES losses of 07:30: "i can see that based on the hour history. it was moving up and down, and this 15m stretch it went down further than expected")
Question: on each multiscale lane's settled live bets at or over the 0.55 price floor, held to settlement, how did the bets do by (a) the spot's position inside its own range of the 60 minutes before the buy, measured toward the bet's side (1.0 = a YES bought at the hour's high, or a NO at its low), and (b) how the hour moved: path efficiency of the hour (net move / path travelled, on 10 s steps), cut into this lane's own terciles (rule 2: the lane's own readings, no fixed number), "back and forth" the lowest third? And the two together. Buckets fixed before results; curve readings only.
    scripts/cloud.sh run scripts/hour_range_position.py
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

LANES = ("crypto_multiscale", "commodity_multiscale")
LO = 0.55
P = ("lower third of the hour's range", "middle third", "upper third, under 0.9", "top tenth (0.9 and up)")


def pk(p):
    return P[0] if p < 1 / 3 else P[1] if p < 2 / 3 else P[2] if p < 0.9 else P[3]


for lane in LANES:
    q = st.db.execute(
        "SELECT d.ts,d.ticker,d.lane,d.side,d.contracts,d.stake_usd,d.exit_price,w.close_ts,w.strike,w.result FROM decisions d JOIN windows w ON w.ticker=d.ticker "
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
        ser = r["ticker"].split("-")[0]
        ix = variants.crypto_index(ser) or variants.biquote_index(ser)
        t0 = float(r["ts"])
        tk = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts", (ix, t0 - 3600, t0)).fetchall()
        if len(tk) < 600 or float(tk[0][0]) > t0 - 3300:
            continue
        g = []
        j = 0
        last = None
        for t in range(int(t0) - 3600, int(t0) + 1, 10):
            while j < len(tk) and float(tk[j][0]) <= t:
                last = float(tk[j][1])
                j += 1
            if last:
                g.append(math.log(last))
        if len(g) < 300:
            continue
        hi, lo = max(g), min(g)
        if hi <= lo:
            continue
        pos = (g[-1] - lo) / (hi - lo)
        if r["side"] == "no":
            pos = 1.0 - pos
        path = sum(abs(b - a) for a, b in zip(g, g[1:]))
        eff = abs(g[-1] - g[0]) / path if path > 0 else 0.0
        won = 1 if r["result"] == r["side"] else 0
        rows.append({"ts": t0, "tk": r["ticker"], "stake": cost, "held": n * won - cost, "won": won, "pos": pos, "eff": eff})
    rows.sort(key=lambda x: x["ts"])
    if len(rows) < 30:
        print(f"\n=== {lane}: {len(rows)} bets, too few")
        continue
    effs = sorted(x["eff"] for x in rows)
    c1, c2 = effs[len(effs) // 3], effs[2 * len(effs) // 3]
    mid = rows[len(rows) // 2]["ts"]
    print(f"\n=== {lane}: {len(rows)} settled bets at {LO} or over with an hour of index before the buy | held {sum(x['held'] for x in rows):+.2f} on {sum(x['stake'] for x in rows):.2f} | the lane's own efficiency terciles cut at {c1:.3f} and {c2:.3f}")

    def shape(x):
        return "back and forth" if x["eff"] <= c1 else "trending" if x["eff"] >= c2 else "in between"

    def table(title, key, order):
        print(f"  {title}")
        gdict = collections.defaultdict(list)
        for x in rows:
            gdict[key(x)].append(x)
        for k in order:
            r_ = gdict.get(k, [])
            if not r_:
                continue
            st_ = sum(x["stake"] for x in r_)
            net = sum(x["held"] for x in r_)
            h1 = sum(x["held"] for x in r_ if x["ts"] <= mid)
            print(f"    {k:52s} n {len(r_):4d} | won {sum(x['won'] for x in r_):3d} ({sum(x['won'] for x in r_) / len(r_):.0%}) | staked {st_:7.2f} | held {net:+7.2f} ({net / st_:+.1%} per $) | halves {h1:+.2f} / {net - h1:+.2f}")

    table("position in the hour's range, toward the bet's side", lambda x: pk(x["pos"]), P)
    S = ("back and forth", "in between", "trending")
    table("how the hour moved", shape, S)
    table("the two together", lambda x: shape(x) + ", " + ("upper third or higher" if x["pos"] >= 2 / 3 else "lower two thirds"), [s_ + ", " + p_ for s_ in S for p_ in ("upper third or higher", "lower two thirds")])
    for x in rows:
        if x["tk"] in ("KXXRP15M-26NOV110730-30", "KXHYPE15M-26NOV110730-30"):
            print(f"    {x['tk']}: position {x['pos']:.2f}, hour efficiency {x['eff']:.3f} ({shape(x)}), held {x['held']:+.2f}")
print("DONE")
