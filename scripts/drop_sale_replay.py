"""A sale on a fast fall of the cushion, with every time limit scaled to the time left (2026-11-13, user: "our time limits need to scale against time left ... at 1min you can't use the same wait, and it's better to take a profit or small loss than a full one").
Trigger case: KXBRENT15M-26NOV130545 NO bought at 0.84 lost the whole $10.11; the NO bid went 0.87 -> 0.52 in 3 s and 0.25 in 10 s with 2 minutes left, always under the curve's own chance, so the margin sale (sells only when the bid is OVER the chance) never acted and the held-wait had no time.
Question: on the same live bets, what does a sale on a fall do against holding to settlement, when the look-back and the wait are shares of the time left instead of fixed seconds?
  rule: the cushion (typical minutes over the target, on the bet's side) fell U or more from its highest reading in the last W seconds, and the curve's chance is under pmax; held for P seconds; sold at the next bid. Scaled: W = wshare * seconds left, P = pshare * seconds left. Fixed (comparison): W 120 s, P 5 or 15 s.
Bets: the live trade-mode bets of crypto_multiscale and the commodity favorites lanes since Monday 11-09 00:00 ET. Fee on the sale (the buy fee is in the stake). Settings were chosen before the run, but this is the first look: say so.
    scripts/cloud_job.sh scripts/drop_sale_replay.py
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
    """(pnl, sold). v = (U, W share or fixed W seconds, P share or fixed P seconds, pmax, scaled)."""
    U, w, p_, pmax, scaled = v
    n, cost = float(r["contracts"]), float(r["stake_usd"])
    hold = (n if r["result"] == r["side"] else 0.0) - cost
    hist = collections.deque()
    since = None
    for idx, (t, left, c, rates, bid) in enumerate(reads):
        if idx + 1 >= len(reads) or bid <= 0 or left < 8 or rates[1] <= 0:
            continue
        tm = c / (rates[1] * math.sqrt(60.0))
        win = w * left if scaled else w
        hist.append((t, tm))
        while hist and t - hist[0][0] > win:
            hist.popleft()
        top = max(x for _, x in hist)
        chance = phi(c / (rates[1] * math.sqrt(left)))
        if top - tm >= U and chance < pmax:
            since = since if since is not None else t
            if t - since >= (p_ * left if scaled else p_):
                sell = reads[idx + 1][4]
                if sell > 0:
                    return n * (sell - fee(sell)) - cost, True
        else:
            since = None
    return hold, False


VARIANTS = [(U, 0.25, ps, pm, True) for U in (1.0, 1.5) for ps in (0.0, 0.1, 0.25) for pm in (0.5, 1.01)] + [(U, 120.0, pf, pm, False) for U in (1.0, 1.5) for pf in (5.0, 15.0) for pm in (0.5, 1.01)]
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
        out.append((sum(d), v, len(ch), won, lost_sold, len(zeros), h1, h2, by))
    out.sort(key=lambda o: -o[0])
    print("  rules (U typical minutes, W, P, chance under, scaled), by net against holding:")
    for o in out:
        sh = " ".join(f"{k} {x:+.1f}" for k, x in sorted(o[8].items()))
        both = "BOTH HALVES +" if o[6] > 0 and o[7] > 0 else ""
        print(f"    {o[1]}: vs holding {o[0]:+.2f} on {o[2]} sold | winners sold {o[3]} of {len(wins)}, losers sold {o[4]} of {o[5]} | halves {o[6]:+.2f} / {o[7]:+.2f} | {sh} {both}")
print("DONE")
