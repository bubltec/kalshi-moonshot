"""Where should the risk exit sell? (2026-11-13, user: "if the bid was .50 already we are already high risk. what was our risk level? then we can set a value where we sell or buy").
Risk reading q = the chance the side pays, the worse of the price cushion (the bid net of the sell fee) and the spot cushion (the curve's chance at the 900 s rate; the spot counts only half of the way down when it is the worse one). Sell when q is under the line L, after a wait of min(15 s * (1 - (L - q) / L), 0.25 * seconds left), for a loss at any depth or a profit of at least 1% of the cost; and sell at once when the result reaches -max_loss of the cost (bid at least 0.05). No history factor here (the replay has 25 minutes of ticks before a buy).
Bets: the live trade-mode bets of crypto_multiscale and the commodity favorites lanes since Monday 11-09 00:00 ET, against holding to settlement, fee on the sale. First look, lines chosen before the run.
Also printed: q at the first reading after the buy (the risk level we enter at), and for the lines the share of winners and losers whose q ever went under them.
    scripts/cloud_job.sh scripts/risk_line_replay.py
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


def qread(c, rates, left, bid, w=0.5):
    spot = phi(c / (rates[1] * math.sqrt(left))) if rates[1] > 0 else (1.0 if c > 0 else 0.0)
    net = bid - fee(bid)
    q = net if net <= spot else net - w * (net - spot)
    return q, net


def decide(r, reads, v):
    """(pnl, sold). v = (L, max_loss); L 0 = only the max loss."""
    L, ML = v
    n, cost = float(r["contracts"]), float(r["stake_usd"])
    hold = (n if r["result"] == r["side"] else 0.0) - cost
    basis = cost / n
    since = None
    for idx, (t, left, c, rates, bid) in enumerate(reads):
        if idx + 1 >= len(reads) or bid <= 0 or left < 8 or rates[1] <= 0:
            continue
        q, net = qread(c, rates, left, bid)
        pnl = (net - basis) / basis
        stop = pnl <= -ML and bid >= 0.05
        ok = L > 0 and q < L and (pnl >= 0.01 or pnl < 0.0)
        if not (stop or ok):
            since = None
            continue
        since = since if since is not None else t
        wait = 0.0 if stop else max(0.0, min(15.0 * (1.0 - min(1.0, max(0.0, (L - q) / L))), 0.25 * left))
        if t - since >= wait:
            sell = reads[idx + 1][4]
            if sell > 0:
                return n * (sell - fee(sell)) - cost, True
    return hold, False


LINES = (0.0, 0.5, 0.6, 0.7, 0.75, 0.8)
VARIANTS = [(L, ML) for ML in (0.3, 0.5) for L in LINES]
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
    q0 = sorted(qread(reads[0][2], reads[0][3], reads[0][1], reads[0][4])[0] for _, _, reads in data if reads and reads[0][3][1] > 0 and reads[0][4] > 0)
    print(f"  q at the first reading after the buy (risk = 1 - q): quartiles {q0[len(q0) // 4]:.3f} / {q0[len(q0) // 2]:.3f} / {q0[3 * len(q0) // 4]:.3f}")
    for L in LINES[1:]:
        def under(i):
            return any(rates[1] > 0 and left >= 8 and bid > 0 and qread(c, rates, left, bid)[0] < L for t, left, c, rates, bid in data[i][2])
        print(f"  q ever under {L:.2f}: losers {sum(1 for i in zeros if under(i))} of {len(zeros)}, winners {sum(1 for i in wins if under(i))} of {len(wins)}")
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
        out.append((sum(d), v, len(ch), won, lost_sold, len(zeros), h1, h2, by, sum(d[i] for i in zeros)))
    out.sort(key=lambda o: -o[0])
    print("  (line, max loss) by net against holding (saved = recovered on the whole-stake losers):")
    for o in out:
        sh = " ".join(f"{k} {x:+.1f}" for k, x in sorted(o[8].items()))
        both = "BOTH HALVES +" if o[6] > 0 and o[7] > 0 else ""
        print(f"    {o[1]}: vs holding {o[0]:+.2f} on {o[2]} sold | winners sold {o[3]} of {len(wins)}, losers sold {o[4]} of {o[5]} (saved {o[9]:+.2f}) | halves {o[6]:+.2f} / {o[7]:+.2f} | {sh} {both}")
print("DONE")
