"""The curve exit reading the contract's own price (2026-11-10, user: "it should be built into the curve exit logic"; KXPLATINUM15M-26NOV101830 YES at 0.92 lost $10.75: the BiQuote spot stayed above the strike while Kalshi's settlement source fell under it, the bid went 0.89 -> 0.50, and the spot-based curve exit held).
Question: on the same live bets, does adding a sale that reads the contract's own price to the curve value exit beat the value exit alone, holding, and the live stacked exits? Two candidate terms, each in the market's own units, nothing a fixed price:
  gap: sell when the curve's chance P is at least G above the bid net of the fee (the market prices the position far under what our spot says: our reading is the one in doubt), held `persist` seconds.
  drop: sell when the bid has fallen from its highest of the last 60 s by at least N of its own typical minutes (rms one-second bid change over the last 300 s times sqrt(60)), held 5 s.
Value exit as live: bid net of the fee at least P + margin, 5 s, 8 s guard; fills at the next second's bid less the fee. Variants (grid fixed before results; the choice is made after seeing them and is said to be so): G 0.15 / 0.25 / 0.35 with persist 5 / 15 s; N 1 / 2 / 3; each alone with the value exit, and the pairs.
    scripts/cloud_job.sh scripts/curve_quote_term_replay.py
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

SCALES = {"60s900": (1,), "60s900+60s180": (1, 2)}


def decide(r, reads, v, margin, sc):
    """(pnl, sold, why) with v = (G, gpersist, N); None = that term off."""
    G, gp, N = v
    n, cost = float(r["contracts"]), float(r["stake_usd"])
    hold = (n if r["result"] == r["side"] else 0.0) - cost
    vsince = gsince = dsince = None
    bids = collections.deque()  # (t, bid) of the last 300 s
    for idx, (t, left, c, rates, bid) in enumerate(reads):
        bids.append((t, bid))
        while bids and t - bids[0][0] > 300:
            bids.popleft()
        if left > 900 or left < 8 or bid < 0.05:
            vsince = gsince = dsince = None
            continue
        p = min(phi(c / (rates[j] * math.sqrt(left))) if rates[j] > 0 else (1.0 if c > 0 else 0.0) for j in SCALES[sc])
        net = bid - fee(bid)
        why = None
        if net - p >= margin:
            vsince = vsince if vsince is not None else t
            if t - vsince >= 5:
                why = "value"
        else:
            vsince = None
        if G is not None:
            if p - net >= G:
                gsince = gsince if gsince is not None else t
                if t - gsince >= gp:
                    why = why or "gap"
            else:
                gsince = None
        if N is not None and len(bids) >= 120:
            ch = [(b2 - b1) ** 2 for (_, b1), (_, b2) in zip(list(bids)[:-1], list(bids)[1:])]
            typ = math.sqrt(sum(ch) / len(ch)) * math.sqrt(60.0)
            high = max(b for tt, b in bids if t - tt <= 60)
            if typ > 0 and high - bid >= N * typ:
                dsince = dsince if dsince is not None else t
                if t - dsince >= 5:
                    why = why or "drop"
            else:
                dsince = None
        if why and idx + 1 < len(reads):
            sell = reads[idx + 1][4]
            if sell > 0.0:
                return n * (sell - fee(sell)) - cost, True, why
    return hold, False, None


CONF = {"crypto": (0.15, "60s900"), "commodity_multiscale": (0.06, "60s900+60s180"), "favorites": (0.15, "60s900")}
VARIANTS = [("value only", (None, 0, None))] + [("value + gap", (G, gp, None)) for G in (0.15, 0.25, 0.35) for gp in (5, 15)] + [("value + drop", (None, 0, N)) for N in (1.0, 2.0, 3.0)] + [("value + gap + drop", (G, gp, N)) for G in (0.25, 0.35) for gp in (5, 15) for N in (2.0, 3.0)]

for gname, (lanes, own_L, since) in GROUPS.items():
    margin, sc = CONF[gname]
    bets = load(lanes, since)
    data = []
    for r in bets:
        got = prepare(r)
        if got:
            data.append((r, got[0], got[1]))
    print(f"\n=== {gname}: lanes {lanes} | settled live bets {len(bets)} | replayed {len(data)} | value exit margin {margin}, scales {sc}")
    if len(data) < 20:
        print("  too few bets")
        continue
    effs = sorted(e for _, e, _ in data if e is not None)
    lo, hi = effs[len(effs) // 3], effs[2 * len(effs) // 3]
    mid = sorted(float(r["ts"]) for r, _, _ in data)[len(data) // 2]

    def shape(e):
        return "unknown" if e is None else "choppy" if e <= lo else "straight" if e >= hi else "middle"

    hold = [((float(r["contracts"]) if r["result"] == r["side"] else 0.0) - float(r["stake_usd"])) for r, _, _ in data]
    live = [((float(r["contracts"]) * float(r["exit_price"]) - float(r["stake_usd"])) if r["exit_price"] else h) for (r, _, _), h in zip(data, hold, strict=True)]
    zeros = [i for i, (r, _, _) in enumerate(data) if hold[i] <= -float(r["stake_usd"]) + 1e-9]
    print(f"  held to the result {sum(hold):+.2f} | live (own sales, stacked exits) {sum(live):+.2f} | whole-stake losses held {len(zeros)}")
    out = []
    for name, v in VARIANTS:
        res = [decide(r, reads, v, margin, sc) for r, e, reads in data]
        net = sum(p for p, _, _ in res)
        d = [p - h for (p, _, _), h in zip(res, hold, strict=True)]
        ch = [(x, i) for i, x in enumerate(d) if abs(x) > 1e-9]
        if not ch:
            continue
        h1 = sum(x for x, i in ch if float(data[i][0]["ts"]) <= mid)
        h2 = sum(x for x, i in ch if float(data[i][0]["ts"]) > mid)
        by = {}
        for x, i in ch:
            by[shape(data[i][1])] = by.get(shape(data[i][1]), 0.0) + x
        won = sum(1 for (p, f, _), h in zip(res, hold, strict=True) if f and h > 0)
        saved = sum(1 for (p, f, _), h in zip(res, hold, strict=True) if f and h < 0)
        fixed = sum(1 for i in zeros if res[i][1])
        whys = collections.Counter(w for _, f, w in res if f)
        out.append((net, sum(d), name, v, len(ch), saved, won, fixed, h1, h2, by, dict(whys)))
    out.sort(key=lambda o: -o[0])
    print("  all variants by net (v = G, gap persist, N):")
    for o in out:
        sh = " ".join(f"{k} {v:+.1f}" for k, v in sorted(o[10].items()))
        both = "BOTH HALVES +" if o[8] > 0 and o[9] > 0 else ""
        print(f"    {o[2]} {o[3]}: net {o[0]:+.2f} (vs holding {o[1]:+.2f}, vs live {o[0] - sum(live):+.2f}) on {o[4]} sold {o[11]} | losers saved {o[5]}, winners sold {o[6]}, zeros partial {o[7]} of {len(zeros)} | halves {o[8]:+.2f} / {o[9]:+.2f} | {sh} {both}")
print("DONE")
