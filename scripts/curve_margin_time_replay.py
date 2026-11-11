"""A curve sale margin that shrinks as the close approaches (2026-11-11, user: "that number 0.15 -- it needs to scale with time left"; KXDOGE15M-26NOV110815 NO sat 0.05 to 0.09 over the curve's chance with a bid of 0.82 to 0.96 for its last 100 s, under the fixed 0.15, then flipped in ten seconds; KXZEC15M in the same window was 0.24 over and was sold for a gain).
Question: on each multiscale lane's own settled live bets, does margin(left) = near + (far - near) * sqrt(left / 900) beat the lane's fixed margin against holding? sqrt because what the remaining time can still move scales with its square root, the same scaling the curve's chance uses: far from the close a sale gives up a lot of remaining upside and needs a wide margin; near it there is little upside left and a flip costs the whole stake. Rates and persistence as each lane runs them live (crypto 900 / 5 s, commodity 900+180 / 15 s). Grid fixed before results: far 0.15 / 0.20 / 0.25 / 0.30, near 0.00 / 0.03 / 0.05 / 0.08; plus fixed 0.06, 0.10, 0.15. The pick is made after seeing them and is said to be so.
    scripts/cloud_job.sh scripts/curve_margin_time_replay.py
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


SCALES = {"900": (1,), "900+180": (1, 2)}
LANES = {"crypto_multiscale": (300.0, GROUPS["crypto"][2], "900", 5), "commodity_multiscale": (300.0, GROUPS["commodity_multiscale"][2], "900+180", 15)}


def decide(r, reads, sc, persist, far, near):
    n, cost = float(r["contracts"]), float(r["stake_usd"])
    hold = (n if r["result"] == r["side"] else 0.0) - cost
    since = None
    for idx, (t, left, c, rates, bid) in enumerate(reads):
        if left > 900 or left < 8 or bid < 0.05:
            since = None
            continue
        m = far if near is None else near + (far - near) * math.sqrt(left / 900.0)
        p = min(phi(c / (rates[j] * math.sqrt(left))) if rates[j] > 0 else (1.0 if c > 0 else 0.0) for j in SCALES[sc])
        if bid - fee(bid) - p >= m:
            since = since if since is not None else t
            if t - since >= persist and idx + 1 < len(reads):
                sell = reads[idx + 1][4]
                if sell > 0.0:
                    return n * (sell - fee(sell)) - cost, True, left
        else:
            since = None
    return hold, False, None


VARIANTS = [(f, None) for f in (0.06, 0.10, 0.15)] + [(f, nr) for f in (0.15, 0.20, 0.25, 0.30) for nr in (0.0, 0.03, 0.05, 0.08)]
for lane, (own_L, since, sc, persist) in LANES.items():
    bets = load((lane,), since)
    data = []
    for r in bets:
        got = prepare(r)
        if got and float(r["stake_usd"]) / float(r["contracts"]) >= 0.55:
            data.append((r, got[0], got[1]))
    print(f"\n=== {lane}: settled live bets at 0.55 or over, replayed {len(data)} | rates {sc}, held {persist} s")
    if len(data) < 20:
        print("  too few bets")
        continue
    effs = sorted(e for _, e, _ in data if e is not None)
    lo, hi = effs[len(effs) // 3], effs[2 * len(effs) // 3]
    mid = sorted(float(r["ts"]) for r, _, _ in data)[len(data) // 2]

    def shape(e):
        return "unknown" if e is None else "choppy" if e <= lo else "straight" if e >= hi else "middle"

    hold = [((float(r["contracts"]) if r["result"] == r["side"] else 0.0) - float(r["stake_usd"])) for r, _, _ in data]
    zeros = [i for i, h in enumerate(hold) if h < 0]
    print(f"  held to the result {sum(hold):+.2f} | staked {sum(float(r['stake_usd']) for r, _, _ in data):.2f} | total losses {len(zeros)}")
    out = []
    for far, near in VARIANTS:
        res = [decide(r, reads, sc, persist, far, near) for r, e, reads in data]
        d = [p - h for (p, _, _), h in zip(res, hold, strict=True)]
        ch = [(x, i) for i, x in enumerate(d) if abs(x) > 1e-9]
        h1 = sum(x for x, i in ch if float(data[i][0]["ts"]) <= mid)
        h2 = sum(d) - h1
        by = {}
        for x, i in ch:
            by[shape(data[i][1])] = by.get(shape(data[i][1]), 0.0) + x
        won = sum(1 for (p, f, _), h in zip(res, hold, strict=True) if f and h > 0)
        saved = sum(1 for i in zeros if res[i][1])
        late = sum(1 for _, f, lf in res if f and lf is not None and lf <= 120)
        out.append((sum(d), far, near, len(ch), saved, won, h1, h2, by, late))
    out.sort(key=lambda o: -o[0])
    for o in out:
        name = f"fixed {o[1]:.2f}" if o[2] is None else f"{o[1]:.2f} far to {o[2]:.2f} at the close"
        tag = ("BOTH HALVES + " if o[6] > 0 and o[7] > 0 else "") + ("ALL SHAPES +" if len(o[8]) >= 3 and all(x > 0 for x in o[8].values()) else "")
        sh = " ".join(f"{k} {x:+.1f}" for k, x in sorted(o[8].items()))
        print(f"    {name:30s}: vs holding {o[0]:+7.2f} on {o[3]:3d} sold ({o[9]} in the last 2 min) | total losses caught {o[4]} of {len(zeros)}, winners sold {o[5]} | halves {o[6]:+.2f} / {o[7]:+.2f} | {sh} {tag}")
print("DONE")
