"""Commodity favorites: the whole curve exit stack, and which time-to-go window loses least while preventing total losses (2026-11-10, user: "lets keep commodity favorites with whatever loses least and prevents total loss").
Question: on each favorites lane's settled live bets, with the lane's live curve sale and the gap sale (0.35, 15 s) running together, what does the time-to-go sale do at none / 30 / 60 / 120 / 240 s (curve chance under 0.5), and with the cutoff at 0.3? Reported per lane and pooled: net against holding, total losses left, winners sold, halves. The first sale to fire sells; fills at the next second's bid less the fee; 8 s guard. Grid fixed before results; the pick is made after seeing them.
    scripts/cloud_job.sh scripts/favorites_stack_replay.py
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


SC = {"900": (1,), "900+180": (1, 2)}
LANE_SALE = {"commodity_energy": ("900", 0.20, 15), "commodity_wti": ("900", 0.15, 5), "commodity_precious": ("900", 0.25, 0), "commodity_industrial": ("900+180", 0.15, 0)}
GAP = (0.35, 15)


def decide(r, reads, sale, gap, L, cut):
    n, cost = float(r["contracts"]), float(r["stake_usd"])
    hold = (n if r["result"] == r["side"] else 0.0) - cost
    vs = gs = None
    for idx, (t, left, c, rates, bid) in enumerate(reads):
        if idx + 1 >= len(reads) or left > 900 or left < 8 or bid <= 0:
            vs = gs = None
            continue
        net = bid - fee(bid)
        root = math.sqrt(left)
        p9 = phi(c / (rates[1] * root)) if rates[1] > 0 else (1.0 if c > 0 else 0.0)
        why = None
        if sale is not None and bid >= 0.05:
            sc, m, ps = sale
            p = min(phi(c / (rates[j] * root)) if rates[j] > 0 else (1.0 if c > 0 else 0.0) for j in SC[sc])
            if net - p >= m:
                vs = vs if vs is not None else t
                if t - vs >= ps:
                    why = "curve"
            else:
                vs = None
        if gap is not None and bid >= 0.05:
            if p9 - net >= gap[0]:
                gs = gs if gs is not None else t
                if t - gs >= gap[1]:
                    why = why or "gap"
            else:
                gs = None
        if L is not None and left <= L and p9 < cut:
            why = why or "go"
        if why:
            sell = reads[idx + 1][4]
            if sell > 0:
                return n * (sell - fee(sell)) - cost, why
    return hold, None


VARIANTS = [("held, no exit", False, False, None, 0.5), ("curve sale only", True, False, None, 0.5), ("curve + gap", True, True, None, 0.5)] + [(f"curve + gap + go {L}s under {cut}", True, True, L, cut) for L in (30, 60, 120, 240) for cut in (0.5, 0.3)] + [(f"go {L}s only", False, False, L, 0.5) for L in (30, 60, 120, 240)]
since = GROUPS["favorites"][2]
pool = []
for lane, sale in LANE_SALE.items():
    bets = load((lane,), since)
    data = []
    for r in bets:
        got = prepare(r)
        if got:
            data.append((r, got[1]))
    pool += [(r, reads, sale) for r, reads in data]
    print(f"{lane}: settled {len(bets)}, replayed {len(data)}, curve sale {sale}")
print()
for label, group in [("ALL FOUR LANES", pool)] + [(lane, [x for x in pool if x[0]["lane"] == lane]) for lane in LANE_SALE]:
    if len(group) < 20:
        print(f"=== {label}: {len(group)} bets, too few")
        continue
    hold = [((float(r["contracts"]) if r["result"] == r["side"] else 0.0) - float(r["stake_usd"])) for r, _, _ in group]
    zeros = [i for i, h in enumerate(hold) if h < 0]
    mid = sorted(float(r["ts"]) for r, _, _ in group)[len(group) // 2]
    print(f"=== {label}: {len(group)} bets | held {sum(hold):+.2f} | total losses {len(zeros)} (${-sum(hold[i] for i in zeros):.2f})")
    for name, use_sale, use_gap, L, cut in VARIANTS:
        res = [decide(r, reads, sale if use_sale else None, GAP if use_gap else None, L, cut) for r, reads, sale in group]
        d = [p - h for (p, _), h in zip(res, hold, strict=True)]
        h1 = sum(x for x, (r, _, _) in zip(d, group) if float(r["ts"]) <= mid)
        h2 = sum(d) - h1
        left0 = sum(1 for i in zeros if res[i][1] is None)
        lost_left = -sum(res[i][0] for i in zeros)
        won_sold = sum(1 for i, h in enumerate(hold) if h > 0 and res[i][1] is not None)
        whys = {}
        for _, w in res:
            if w:
                whys[w] = whys.get(w, 0) + 1
        print(f"    {name:34s}: net {sum(p for p, _ in res):+8.2f} (vs holding {sum(d):+7.2f}) | total losses left {left0:2d} of {len(zeros)}, dollars lost on the losers ${lost_left:7.2f} | winners sold {won_sold:3d} | halves {h1:+.2f} / {h2:+.2f} | {whys}")
print("DONE")
