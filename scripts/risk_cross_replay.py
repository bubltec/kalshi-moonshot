"""Sell only when the spot has really crossed the strike? (2026-11-13, user, on the live ETH and platinum sales that went on to win: "sometimes kalshis price will drop or rise but if the spot hasn't crossed the strike yet I would be hesitant to jump").
The risk sale as live: the bid net of the fee has lost 0.5 x q of the cost (q = that net bid), sold at once, or q under 0.5 after the scaled wait. NEW gate, two ways of saying "the spot has crossed":
  tick: the chance from the spot's latest tick against the strike, over the hour's own typical movement for the time left, is under X.
  avg60: the same with the spot's average over the last 60 s in place of the latest tick (the coins settle on a 60 s average, so a dip of a few seconds under the strike has not moved what the market settles on).
X 0.5 = on the wrong side of the strike at all; lower X = across it by a real margin, in the market's own variance. None = price alone.
Bets: live trade-mode bets of crypto_multiscale and the commodity favorites since Monday 11-09 00:00 ET with an hour of ticks before the buy, against holding to settlement, fee on the sale; also by seconds left at the sale. First look, X chosen before the run.
    scripts/cloud_job.sh scripts/risk_cross_replay.py
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

MONDAY = calendar.timegm((2026, 11, 9, 5, 0, 0))  # 11-09 00:00 ET: the logic was rewritten that Monday, nothing earlier counts
GROUPS = {
    "crypto": (("crypto_multiscale",), 300.0, MONDAY),
    "commodity favorites": (("commodity_energy", "commodity_wti", "commodity_precious", "commodity_industrial", "commodity_natgas", "commodity_multiscale"), 120.0, MONDAY),
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
    tk = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts", (ix, t0 - 3700, close)).fetchall()
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
        if k < 3660 or k >= len(g) or g[k] is None or g[k - SEC] is None:
            continue
        moves.append((t, (g[k] - g[k - SEC]) * sign))
        while moves and t - moves[0][0] > MEM:
            moves.popleft()
        left = close - t
        if left > 900:
            continue
        rates = (math.sqrt((sq[k] - sq[k - 900]) / 900), math.sqrt((c60[k] - c60[k - 900]) / 900), math.sqrt((c60[k] - c60[k - 180]) / 180), math.sqrt((c60[k] - c60[k - 3600]) / 3600) if k >= 3600 else 0.0)
        if rates[0] <= 0 or rates[1] <= 0 or rates[2] < 0:
            continue
        i = bisect.bisect_right(qt, t) - 1
        if i < 0 or t - qt[i] > 20:
            continue
        yb, ya = float(q[i][1]), float(q[i][2])
        bid = yb if r["side"] == "yes" else 1.0 - ya
        seg60 = [x for x in g[k - 59 : k + 1] if x is not None]
        reads.append((t, left, (g[k] - ref) * sign, rates, bid, ((sum(seg60) / len(seg60)) - ref) * sign if seg60 else (g[k] - ref) * sign))
    # the market's own last hour before the buy: 1-minute moves, a jump is one over 3 robust sigmas of that hour (1.4826 * median absolute move); A counts the jumps against the side bought in the last 30 minutes
    rets = []
    for j in range(kb, kb - 3600, -60):
        if j - 60 >= 0 and j < len(g) and g[j] is not None and g[j - 60] is not None:
            rets.append((g[j] - g[j - 60]) * sign)
    hist = None
    if len(rets) >= 40:
        mad = sorted(abs(x) for x in rets)[len(rets) // 2] * 1.4826
        if mad > 0:
            hist = (sum(1 for x in rets if abs(x) > 3 * mad), sum(1 for x in rets[:30] if x < -3 * mad))
    return eff, reads, hist


def phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def entry_price(r):
    return float(r["stake_usd"]) / float(r["contracts"])


def chance_of(c, rates, left):
    r = rates[3]
    return phi(c / (r * math.sqrt(left))) if r > 0 else (1.0 if c > 0 else 0.0)


def decide(r, reads, v):
    """(pnl, sold, left at the sale). v = (which cushion, X) or None for price alone."""
    n, cost = float(r["contracts"]), float(r["stake_usd"])
    hold = (n if r["result"] == r["side"] else 0.0) - cost
    basis = cost / n
    since = None
    for idx, (t, left, c, rates, bid, c60) in enumerate(reads):
        if idx + 1 >= len(reads) or bid <= 0 or left < 8 or rates[1] <= 0 or rates[3] <= 0:
            continue
        net = bid - fee(bid)
        q = min(max(net, 0.0), 1.0)
        pnl = (net - basis) / basis
        agree = v is None or chance_of(c60 if v[0] == "avg60" else c, rates, left) < v[1]
        stop = agree and pnl <= -0.5 * q and bid >= 0.05
        ok = agree and q < 0.5 and (pnl >= 0.01 * q or pnl < 0.0)
        if not (stop or ok):
            since = None
            continue
        since = since if since is not None else t
        wait = 0.0 if stop else max(0.0, min(15.0 * (1.0 - min(1.0, max(0.0, (0.5 - q) / 0.5))), 0.25 * left))
        if t - since >= wait:
            sell = reads[idx + 1][4]
            if sell > 0:
                return n * (sell - fee(sell)) - cost, True, left
    return hold, False, None


VARIANTS = [None] + [(w, x) for w in ("tick", "avg60") for x in (0.5, 0.4, 0.3, 0.2, 0.1)]
for gname, (lanes, _, since) in GROUPS.items():
    bets = load(lanes, since)
    data = []
    for r in bets:
        got = prepare(r)
        if got:
            data.append((r, got[0], got[1]))
    print(f"\n=== {gname}: lanes {lanes} | settled live bets {len(bets)} | with an hour of ticks {len(data)}")
    if len(data) < 20:
        print("  too few bets")
        continue
    hold = [((float(r["contracts"]) if r["result"] == r["side"] else 0.0) - float(r["stake_usd"])) for r, _, _ in data]
    zeros = [i for i in range(len(data)) if hold[i] < 0]
    wins = [i for i in range(len(data)) if hold[i] > 0]
    print(f"  held to the result {sum(hold):+.2f} | lost the whole stake {len(zeros)} (${-sum(hold[i] for i in zeros):.2f}) | won {len(wins)} (${sum(hold[i] for i in wins):.2f})")
    for v in VARIANTS:
        res = [decide(r, reads, v) for r, e, reads in data]
        d = [p - h for (p, _, _), h in zip(res, hold, strict=True)]
        won = sum(1 for i in wins if res[i][1])
        lost_sold = sum(1 for i in zeros if res[i][1])
        saved = sum(d[i] for i in zeros)
        buckets = {"<120s": 0.0, "120-240s": 0.0, ">240s": 0.0}
        counts = {"<120s": 0, "120-240s": 0, ">240s": 0}
        for i, (p, sold, lf) in enumerate(res):
            if sold:
                k = "<120s" if lf < 120 else "120-240s" if lf < 240 else ">240s"
                buckets[k] += d[i]
                counts[k] += 1
        bs = " ".join(f"{k} {buckets[k]:+.1f} ({counts[k]})" for k in buckets)
        print(f"    {v}: vs holding {sum(d):+.2f} on {sum(1 for x in res if x[1])} sold | winners sold {won} of {len(wins)}, losers sold {lost_sold} of {len(zeros)} (saved {saved:+.2f}) | {bs}")
print("DONE")
