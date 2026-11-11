"""A self-scoring switch for the curve exit's sales (2026-11-10, user: "if we know those halves numbers are so polarizing, why don't we make the curve acknowledge that").
Question: each sale's result against holding flips sign between the two halves of the period. If a sale acts only while its own recent would-have score is positive, does it beat the always-on sale and holding, in both halves?
Each sale at its live setting, alone, on the lane group's settled live bets in time order. Its shadow result (what it would have made against holding) is computed on every bet whether or not it acted. The switch: act on a bet only when the sum of the shadow results of the bets bought in the trailing H hours (and already closed when this bet was bought) is above zero; with no history in the window the sale stands down ("off") or acts ("on"). H 12 / 24 / 48 / 96. Grid fixed before results.
Sales: value (bid net of fee >= P + margin, held), trajectory (cushion fell D typical minutes over W s to M or under), gap (P - bid net >= G, held), go (P under one half in the last L s). Fills at the next second's bid less the fee.
    scripts/cloud_job.sh scripts/exit_switch_study.py
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

SC = {"900": (1,), "900+180": (1, 2)}


def run_sale(r, reads, kind, a):
    """What the sale makes against holding on this bet (0.0 when it does not sell)."""
    n, cost = float(r["contracts"]), float(r["stake_usd"])
    hold = (n if r["result"] == r["side"] else 0.0) - cost
    since = None
    hist = collections.deque()
    for idx, (t, left, c, rates, bid) in enumerate(reads):
        if idx + 1 >= len(reads) or left > 900 or left < 8 or bid <= 0:
            since = None
            continue
        net = bid - fee(bid)
        fire = False
        if kind == "value":
            sc, m, ps = a
            p = min(phi(c / (rates[j] * math.sqrt(left))) if rates[j] > 0 else (1.0 if c > 0 else 0.0) for j in SC[sc])
            ok = bid >= 0.05 and net - p >= m
            since = (since if since is not None else t) if ok else None
            fire = ok and t - since >= ps
        elif kind == "gap":
            G, ps = a
            p = phi(c / (rates[1] * math.sqrt(left))) if rates[1] > 0 else (1.0 if c > 0 else 0.0)
            ok = bid >= 0.05 and p - net >= G
            since = (since if since is not None else t) if ok else None
            fire = ok and t - since >= ps
        elif kind == "trajectory":
            D, W, M, ps = a
            tm = c / (rates[0] * math.sqrt(60.0))
            hist.append((t, tm))
            while hist and t - hist[0][0] > W + 1:
                hist.popleft()
            ok = bid >= 0.05 and t - hist[0][0] >= W - 1 and tm <= M and hist[0][1] - tm >= D
            since = (since if since is not None else t) if ok else None
            fire = ok and t - since >= ps
        elif kind == "go":
            (L,) = a
            p = phi(c / (rates[1] * math.sqrt(left))) if rates[1] > 0 else (1.0 if c > 0 else 0.0)
            fire = left <= L and p < 0.5
        if fire:
            sell = reads[idx + 1][4]
            if sell > 0:
                return n * (sell - fee(sell)) - cost - hold
    return 0.0


SALES = {
    "crypto": [("value", ("900", 0.15, 5)), ("trajectory", (1.5, 60, 0.5, 0)), ("go", (240,))],
    "commodity_multiscale": [("value", ("900+180", 0.06, 15)), ("trajectory", (2.0, 120, 0.5, 5)), ("go", (240,))],
    "favorites": [("value", ("900", 0.15, 5)), ("gap", (0.35, 15)), ("go", (30,)), ("go", (120,))],
}
for gname, (lanes, own_L, since) in GROUPS.items():
    bets = load(lanes, since)
    data = []
    for r in bets:
        got = prepare(r)
        if got:
            data.append((r, got[1]))
    data.sort(key=lambda x: float(x[0]["ts"]))
    print(f"\n=== {gname}: lanes {lanes} | settled live bets {len(bets)} | replayed {len(data)}")
    if len(data) < 20:
        print("  too few bets")
        continue
    ts = [float(r["ts"]) for r, _ in data]
    cl = [float(r["close_ts"]) for r, _ in data]
    mid = sorted(ts)[len(ts) // 2]
    for kind, a in SALES[gname]:
        d = [run_sale(r, reads, kind, a) for r, reads in data]
        h1 = sum(x for x, t in zip(d, ts) if t <= mid)
        h2 = sum(x for x, t in zip(d, ts) if t > mid)
        print(f"  {kind} {a}: always on {sum(d):+.2f} vs holding on {sum(1 for x in d if abs(x) > 1e-9)} sold | halves {h1:+.2f} / {h2:+.2f}")
        for H in (12, 24, 48, 96):
            for default in ("off", "on"):
                tot = g1 = g2 = 0.0
                acted = 0
                for i in range(len(data)):
                    past = [d[j] for j in range(i) if ts[i] - ts[j] <= H * 3600 and cl[j] <= ts[i]]
                    live = (sum(past) > 0) if past else (default == "on")
                    if live and abs(d[i]) > 1e-9:
                        acted += 1
                        tot += d[i]
                        if ts[i] <= mid:
                            g1 += d[i]
                        else:
                            g2 += d[i]
                both = "BOTH HALVES +" if g1 > 0 and g2 > 0 else ""
                print(f"      switch H {H:2d}h, no-history {default:3s}: {tot:+.2f} vs holding on {acted} sold | halves {g1:+.2f} / {g2:+.2f} {both}")
print("DONE")
