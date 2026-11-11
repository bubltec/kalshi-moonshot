"""No more 0.00 results? (2026-11-10, user: "the ones that exited on 0 (never exited) should have exited when it hit a positive point (if it did) otherwise it should have just sold before the end")
Question: on the same live bets, what do the user's two rules do against holding to settlement?
  profit: sell the first time the bid net of the sell fee is at least the entry price (stake / contracts, buy fee included) plus g, held 5 s.
  salvage: with L seconds left, a position still not sold whose bid net of the fee is under its entry price (or, variant, whose curve chance is under one half) is sold at the bid.
First the facts on the whole-stake losses: how many ever showed a profit of g after the buy, and what their bid was with L seconds left. Then each rule alone and the pairs: net against holding, bets sold, winners sold, zeros left, two halves, curve shapes. g 0.00 / 0.03 / 0.05 / 0.10; L 30 / 60 / 120 / 240. Grid fixed before results. Fill at the next second's bid less the fee; 8 s guard for the profit sale, the salvage sale may act down to 5 s.
    scripts/cloud_job.sh scripts/zero_exit_study.py
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


def entry_price(r):
    return float(r["stake_usd"]) / float(r["contracts"])


def decide(r, reads, v):
    """(pnl, sold, why). v = (g or None, L or None, cond) with cond "under" (bid net under entry) or "behind" (curve chance under 0.5)."""
    g, L, cond = v
    n, cost = float(r["contracts"]), float(r["stake_usd"])
    hold = (n if r["result"] == r["side"] else 0.0) - cost
    ep = entry_price(r)
    since = None
    for idx, (t, left, c, rates, bid) in enumerate(reads):
        if idx + 1 >= len(reads) or bid <= 0:
            continue
        net = bid - fee(bid)
        if g is not None and left >= 8:
            if net >= ep + g:
                since = since if since is not None else t
                if t - since >= 5:
                    sell = reads[idx + 1][4]
                    return n * (sell - fee(sell)) - cost, True, "profit"
            else:
                since = None
        if L is not None and 5 <= left <= L:
            p = phi(c / (rates[1] * math.sqrt(left))) if rates[1] > 0 else (1.0 if c > 0 else 0.0)
            if (cond == "under" and net < ep) or (cond == "behind" and p < 0.5):
                sell = reads[idx + 1][4]
                if sell > 0:
                    return n * (sell - fee(sell)) - cost, True, "salvage"
    return hold, False, None


VARIANTS = [(g, None, "") for g in (0.0, 0.03, 0.05, 0.10)] + [(None, L, c) for L in (30, 60, 120, 240) for c in ("under", "behind")] + [(g, L, c) for g in (0.03, 0.05, 0.10) for L in (30, 60, 120) for c in ("under", "behind")]
for gname, (lanes, own_L, since) in GROUPS.items():
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
    for g in (0.0, 0.03, 0.05, 0.10):
        def ever(i):
            r, _, reads = data[i]
            ep = entry_price(r)
            run = 0
            for t, left, c, rates, bid in reads:
                run = run + 1 if (bid - fee(bid) >= ep + g and left >= 8) else 0
                if run >= 5:
                    return True
            return False
        print(f"  ever showed a profit of {g:.2f} a contract for 5 s after the buy: {sum(1 for i in zeros if ever(i))} of {len(zeros)} losers, {sum(1 for i in wins if ever(i))} of {len(wins)} winners")
    for L in (30, 60, 120, 240):
        bl = []
        for i in zeros:
            reads = data[i][2]
            b = next((bid for t, left, c, rates, bid in reads if left <= L), None)
            if b is not None:
                bl.append(b)
        bw = []
        for i in wins:
            reads = data[i][2]
            b = next((bid for t, left, c, rates, bid in reads if left <= L), None)
            if b is not None:
                bw.append(b)
        if bl and bw:
            bl.sort(); bw.sort()
            print(f"  bid with {L}s left: losers median {bl[len(bl)//2]:.2f} (quartiles {bl[len(bl)//4]:.2f}-{bl[3*len(bl)//4]:.2f}); winners median {bw[len(bw)//2]:.2f} (quartiles {bw[len(bw)//4]:.2f}-{bw[3*len(bw)//4]:.2f})")
    out = []
    for v in VARIANTS:
        res = [decide(r, reads, v) for r, e, reads in data]
        d = [p - h for (p, _, _), h in zip(res, hold, strict=True)]
        ch = [(x, i) for i, x in enumerate(d) if abs(x) > 1e-9]
        h1 = sum(x for x, i in ch if float(data[i][0]["ts"]) <= mid)
        h2 = sum(x for x, i in ch if float(data[i][0]["ts"]) > mid)
        by = {}
        for x, i in ch:
            by[shape(data[i][1])] = by.get(shape(data[i][1]), 0.0) + x
        won = sum(1 for i in wins if res[i][1])
        left0 = sum(1 for i in zeros if not res[i][1])
        out.append((sum(d), v, len(ch), won, left0, h1, h2, by))
    out.sort(key=lambda o: -o[0])
    print("  rules (profit g, salvage L, salvage condition), by net against holding:")
    for o in out:
        sh = " ".join(f"{k} {x:+.1f}" for k, x in sorted(o[7].items()))
        both = "BOTH HALVES +" if o[5] > 0 and o[6] > 0 else ""
        print(f"    {o[1]}: vs holding {o[0]:+.2f} on {o[2]} sold | winners sold {o[3]} of {len(wins)}, zeros left {o[4]} of {len(zeros)} | halves {o[5]:+.2f} / {o[6]:+.2f} | {sh} {both}")
print("DONE")
