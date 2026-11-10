"""Is the shock exit selling a stale shock? (2026-11-10, user: "it must be assuming the drop from the original price is a shock and exited ... there's still 5 mins left and we are probably losing a lot from that"; KXZEC15M-26NOV101330: sold at 0.39 at 13:25:00 on "spot moved 22.8 bp against us in 60s (now +7.2)", it settled YES.)
Live record (shock_log joined to decisions, crypto lanes, 269 sales with a result): sold against holding -$25.37; sales made while the latest 60 s move was already with the bet -$11.6 (62), easing -$12.2 (47), still falling and later in the window +$10.7 (112), still falling at the window opening -$12.3 (48); 128 of the 269 sales fire at the first second of the 300 s window, -$28.4.
Question: on the same live crypto bets (13 days), does the live rule do better when the shock memory is shorter or off, or when a sale waits for the latest 60 s move to still be against the bet?
Live rule (the baseline): in the last 300 s, the worst 60 s move against the bet over the last 120 s is at least 2.0 typical minutes (rms one-second log move over 900 s times sqrt(60)), the cushion over the strike at most 0.6 typical minutes, the held-side bid in [0.05, 0.89]. Sale at the bid less the fee; held to the result is the counterfactual.
Variants (grid fixed before the results; the choice is made after seeing them and is said to be so): memory 0 / 60 / 120 s (the worst move counted over that look-back; 0 = the latest move only); recovery filter off / on (no sale while the latest 60 s move is zero or with the bet); recovery filter "half" (no sale while the latest move is under half the remembered worst); persistence 0 / 5 s. Per row: net, change against holding and against the live rule, bets changed, losers saved / winners sold, two halves, curve shapes. A variant counts only when positive against the live rule in both halves and in each shape it claims.

    scripts/cloud_job.sh scripts/shock_memory_replay.py
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
}
MEM, SEC = 120, 60
BAND = (0.05, 0.89)


def load(lanes, since):
    marks = ",".join("?" * len(lanes))
    rows = st.db.execute(
        "SELECT d.ts,d.ticker,d.lane,d.side,d.contracts,d.stake_usd,w.close_ts,w.strike,w.result FROM decisions d JOIN windows w ON w.ticker=d.ticker "
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
    for t in range(int(t0), int(close) - 5):
        k = t - base
        if k < 960 or k >= len(g) or g[k] is None or g[k - SEC] is None:
            continue
        moves.append((t, (g[k] - g[k - SEC]) * sign))
        while moves and t - moves[0][0] > MEM:
            moves.popleft()
        left = close - t
        if left > 330:
            continue
        rate = math.sqrt((sq[k] - sq[k - 900]) / 900)
        if rate <= 0:
            continue
        unit = rate * math.sqrt(60.0)
        i = bisect.bisect_right(qt, t) - 1
        if i < 0 or t - qt[i] > 20:
            continue
        yb, ya = float(q[i][1]), float(q[i][2])
        bid = yb if r["side"] == "yes" else 1.0 - ya
        latest = moves[-1][1]
        w60 = min(m for tt, m in moves if t - tt <= 60)
        w120 = min(m for _, m in moves)
        reads.append((t, left, (latest / unit, w60 / unit, w120 / unit), (g[k] - ref) * sign / unit, bid))
    return eff, reads


def decide(r, reads, v):
    """(pnl, sold) under v = (memory, recovery filter, persist); size 2.0, cushion 0.6, last 300 s as live."""
    mem, filt, persist = v
    n, cost = float(r["contracts"]), float(r["stake_usd"])
    hold = (n if r["result"] == r["side"] else 0.0) - cost
    since = None
    for t, left, (latest, w60, w120), mu, bid in reads:
        worst = latest if mem == 0 else w60 if mem == 60 else w120
        ok = left <= 300.0 and BAND[0] <= bid <= BAND[1] and mu <= 0.6 and worst <= -2.0
        if ok and filt == "on" and latest > -1e-9:
            ok = False
        if ok and filt == "half" and latest > 0.5 * worst:
            ok = False
        if not ok:
            since = None
            continue
        if since is None:
            since = t
        if t - since >= persist:
            return n * (bid - fee(bid)) - cost, True
    return hold, False


VARIANTS = [(m, f, p) for m in (0, 60, 120) for f in ("off", "on", "half") for p in (0, 5)]

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
    times = sorted(float(r["ts"]) for r, _, _ in data)
    mid = times[len(times) // 2]

    def shape(e):
        return "unknown" if e is None else "choppy" if e <= lo else "straight" if e >= hi else "middle"

    hold = [((float(r["contracts"]) if r["result"] == r["side"] else 0.0) - float(r["stake_usd"])) for r, _, _ in data]
    print(f"  held to the result {sum(hold):+.2f} | shape cutoffs (path efficiency) {lo:.3f} {hi:.3f}")
    live = (120, "off", 0)
    cur = [decide(r, reads, live) for r, _, reads in data]
    print(f"  live rule {live}: net {sum(p for p, _ in cur):+.2f}, sold {sum(f for _, f in cur)}, against holding {sum(p for p, _ in cur) - sum(hold):+.2f}")
    out = []
    for v in VARIANTS:
        res = [decide(r, reads, v) for r, e, reads in data]
        d = [p - c[0] for (p, _), c in zip(res, cur, strict=True)]
        changed = [(x, i) for i, x in enumerate(d) if abs(x) > 1e-9]
        if not changed:
            continue
        h1 = sum(x for x, i in changed if float(data[i][0]["ts"]) <= mid)
        h2 = sum(x for x, i in changed if float(data[i][0]["ts"]) > mid)
        by = {}
        for x, i in changed:
            by[shape(data[i][1])] = by.get(shape(data[i][1]), 0.0) + x
        saved = sum(1 for (p, f), h in zip(res, hold, strict=True) if f and h < 0)
        sold_w = sum(1 for (p, f), h in zip(res, hold, strict=True) if f and h > 0)
        out.append((sum(d), v, sum(p for p, _ in res), len(changed), saved, sold_w, sum(f for _, f in res), h1, h2, by))
    out.sort(key=lambda o: -o[0])
    print("  variants (memory s, recovery filter, persist s) against the live rule:")
    for o in out:
        sh = " ".join(f"{k} {v:+.1f}" for k, v in sorted(o[9].items()))
        print(f"    {o[1]}: net {o[2]:+.2f} | vs live {o[0]:+.2f} on {o[3]} changed, {o[6]} sold | losers saved {o[4]}, winners sold {o[5]} | halves {o[7]:+.2f} / {o[8]:+.2f} | {sh}")
    print(f"  positive against live in both halves: {sum(1 for o in out if o[7] > 0 and o[8] > 0)} of {len(out)}")
print("DONE")
