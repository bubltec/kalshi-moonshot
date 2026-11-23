"""Cutting the loss when the market disagrees or the bet is behind, waits scaled to the time left (2026-11-13, user, on KXBRENT15M-26NOV130545 NO 0.84, -$10.11: "the gap on the Brent one can't be saved ... We could have cut our losses though").
Question: on the same live bets (crypto_multiscale and the commodity favorites lanes, trade mode, since Monday 11-09 00:00 ET), what do two loss-cutting sales do against holding to settlement?
  gap: the curve's chance is at least G over the bid net of the sell fee, held P seconds; P = pshare * seconds left (pshare 0 = at once).
  go: the curve's chance is under C with at most Lmax seconds left, held W seconds; W = min(15 s * (1 - typical minutes under the target / 1.5), 0.25 * seconds left), as the live time-to-go wait is.
Sold at the next bid, fee on the sale. Settings were chosen before the run; first look, so the result is exploratory.
    scripts/cloud_job.sh scripts/gap_go_replay.py
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


def entry_price(r):
    return float(r["stake_usd"]) / float(r["contracts"])


def decide(r, reads, v):
    """(pnl, sold). v = ("gap", G, pshare) or ("go", C, Lmax) or ("both", G, pshare, C, Lmax)."""
    n, cost = float(r["contracts"]), float(r["stake_usd"])
    hold = (n if r["result"] == r["side"] else 0.0) - cost
    gap_on = v[0] in ("gap", "both")
    go_on = v[0] in ("go", "both")
    if v[0] == "gap":
        G, ps = v[1], v[2]
    elif v[0] == "both":
        G, ps, C, Lmax = v[1], v[2], v[3], v[4]
    if v[0] == "go":
        C, Lmax = v[1], v[2]
    gap_since = go_since = None
    for idx, (t, left, c, rates, bid) in enumerate(reads):
        if idx + 1 >= len(reads) or bid <= 0 or left < 8 or rates[1] <= 0:
            continue
        chance = phi(c / (rates[1] * math.sqrt(left)))
        net = bid - fee(bid)
        hit = False
        if gap_on:
            if chance - net >= G:
                gap_since = gap_since if gap_since is not None else t
                if t - gap_since >= ps * left:
                    hit = True
            else:
                gap_since = None
        if go_on:
            if chance < C and left <= Lmax:
                tm = c / (rates[1] * math.sqrt(60.0))
                wait = min(15.0 * (1.0 - min(1.0, max(0.0, -tm) / 1.5)), 0.25 * left)
                go_since = go_since if go_since is not None else t
                if t - go_since >= wait:
                    hit = True
            else:
                go_since = None
        if hit:
            sell = reads[idx + 1][4]
            if sell > 0:
                return n * (sell - fee(sell)) - cost, True
    return hold, False


VARIANTS = (
    [("gap", G, ps) for G in (0.10, 0.15, 0.20) for ps in (0.0, 0.1, 0.25)]
    + [("go", C, L) for C in (0.5, 0.3) for L in (120, 240)]
    + [("both", G, ps, 0.5, 240) for G in (0.10, 0.15) for ps in (0.0, 0.1)]
)
for gname, (lanes, _, since) in GROUPS.items():
    bets = load(lanes, since)
    data = []
    for r in bets:
        got = prepare(r)
        if got:
            data.append((r, got[0], got[1]))
    print(f"\n=== {gname}: lanes {lanes} | settled live bets {len(bets)} | replayed {len(data)}")
    if len(data) < 20:
        print("  too few bets")
        continue
    effs = sorted(e for _, e, _ in data if e is not None)
    lo, hi = effs[len(effs) // 3], effs[2 * len(effs) // 3]
    mid = sorted(float(r["ts"]) for r, _, _ in data)[len(data) // 2]

    def shape(e):
        return "unknown" if e is None else "choppy" if e <= lo else "straight" if e >= hi else "middle"

    hold = [((float(r["contracts"]) if r["result"] == r["side"] else 0.0) - float(r["stake_usd"])) for r, _, _ in data]
    zeros = [i for i in range(len(data)) if hold[i] < 0]
    wins = [i for i in range(len(data)) if hold[i] > 0]
    print(f"  held to the result {sum(hold):+.2f} | staked {sum(float(r['stake_usd']) for r, _, _ in data):.2f} | lost the whole stake {len(zeros)} (${-sum(hold[i] for i in zeros):.2f}) | won {len(wins)} (${sum(hold[i] for i in wins):.2f})")
    out = []
    for v in VARIANTS:
        res = [decide(r, reads, v) for r, e, reads in data]
        d = [p - h for (p, _), h in zip(res, hold, strict=True)]
        ch = [(x, i) for i, x in enumerate(d) if abs(x) > 1e-9]
        h1 = sum(x for x, i in ch if float(data[i][0]["ts"]) <= mid)
        h2 = sum(x for x, i in ch if float(data[i][0]["ts"]) > mid)
        by = {}
        for x, i in ch:
            by[shape(data[i][1])] = by.get(shape(data[i][1]), 0.0) + x
        won = sum(1 for i in wins if res[i][1])
        lost_sold = sum(1 for i in zeros if res[i][1])
        saved = sum(d[i] for i in zeros)
        out.append((sum(d), v, len(ch), won, lost_sold, len(zeros), h1, h2, by, saved))
    out.sort(key=lambda o: -o[0])
    print("  rules, by net against holding (saved = recovered on the whole-stake losers):")
    for o in out:
        sh = " ".join(f"{k} {x:+.1f}" for k, x in sorted(o[8].items()))
        both = "BOTH HALVES +" if o[6] > 0 and o[7] > 0 else ""
        print(f"    {o[1]}: vs holding {o[0]:+.2f} on {o[2]} sold | winners sold {o[3]} of {len(wins)}, losers sold {o[4]} of {o[5]} (saved {o[9]:+.2f}) | halves {o[6]:+.2f} / {o[7]:+.2f} | {sh} {both}")
print("DONE")
