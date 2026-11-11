"""A time-to-go sale for the underdog lane (2026-11-10, user: yes, test it). An underdog bet starts behind the strike, so "curve chance under one half" would sell every one; candidates that read the chance against where it stood at the buy, or against a low floor.
Question: on underdog-style positions, does selling late when the curve's chance has fallen under a share of its value at the buy (or under a floor) beat holding, and how many total losses does it leave?
Two samples: (1) hypothetical positions from the tape (1 in STRIDE windows, both sides): the first second in minutes 0.5-7.5 where the side's bid + 0.02 is in 0.20-0.40, one contract at bid + 0.02 plus the fee; (2) the lane's own settled live bets (41 at the time of writing: thin).
Sales, alone, fill at the next reading's bid less the fee, 8 s guard: rel(L, f): in the last L s the chance is under f times the chance at the buy; floor(L, cut): in the last L s the chance is under cut; trajectory(D, W, M) as live elsewhere; curve sale(margin, held). L 60/120/240/480; f 0.5/0.75; cut 0.10/0.20; grid fixed before results. Tape sample: always-on totals with halves, and the walk-forward pick by UTC day.
    scripts/cloud_job.sh scripts/underdog_exit_search.py STRIDE=4
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
import time as _time

STRIDE = int(os.environ.get("STRIDE", "4"))
SINCE_S = int(os.environ.get("SINCE_S", str(calendar.timegm((2026, 10, 28, 0, 0, 0)))))
SETTINGS = (
    [("rel", (L, f)) for L in (60, 120, 240, 480) for f in (0.5, 0.75)]
    + [("floor", (L, cut)) for L in (60, 120, 240, 480) for cut in (0.10, 0.20)]
    + [("trajectory", (D, W, M)) for D in (1.0, 1.5, 2.0) for W in (60, 120) for M in (0.0, -0.5)]
    + [("value", (m, ps)) for m in (0.10, 0.15, 0.20) for ps in (5, 15)]
)


def arrays(reads, i0):
    out = []
    for t, left, c, rates, bid in reads[i0:]:
        root = math.sqrt(left) if left > 0 else 1.0
        p9 = phi(c / (rates[1] * root)) if rates[1] > 0 else (1.0 if c > 0 else 0.0)
        p1 = phi(c / (rates[2] * root)) if rates[2] > 0 else p9
        out.append((t, left, p9, min(p9, p1), c / (rates[0] * math.sqrt(60.0)), bid, bid - fee(bid)))
    return out


def sale(a, kind, v, p0):
    since = None
    hist = collections.deque()
    for i, (t, left, p9, pm, tm, bid, net) in enumerate(a):
        if kind == "trajectory":
            hist.append((t, tm))
            while hist and t - hist[0][0] > v[1] + 1:
                hist.popleft()
        if left < 8 or bid <= 0:
            since = None
            continue
        if kind == "rel":
            if left <= v[0] and p9 < v[1] * p0:
                return i
            continue
        if kind == "floor":
            if left <= v[0] and p9 < v[1]:
                return i
            continue
        if kind == "value":
            ok, ps = bid >= 0.05 and net - pm >= v[0], v[1]
        else:
            ok, ps = bid >= 0.05 and t - hist[0][0] >= v[1] - 1 and tm <= v[2] and hist[0][1] - tm >= v[0], 5
        if ok:
            since = since if since is not None else t
            if t - since >= ps:
                return i
        else:
            since = None
    return None


def deltas(a, cost, hold, p0, n=1.0):
    d = []
    for kind, v in SETTINGS:
        j = sale(a, kind, v, p0)
        if j is None or j + 1 >= len(a) or a[j + 1][5] <= 0:
            d.append(0.0)
        else:
            sell = a[j + 1][5]
            d.append(n * (sell - fee(sell)) - cost - hold)
    return d


def report(P, label, wf=True):
    if len(P) < 20:
        print(f"\n=== {label}: {len(P)} positions, too few")
        return
    P.sort(key=lambda x: x[0])
    mid_t = P[len(P) // 2][0]
    zeros = [x for x in P if x[1] < 0]
    print(f"\n=== {label}: {len(P)} positions | held {sum(x[1] for x in P):+.2f} | total losses {len(zeros)} | won {sum(1 for x in P if x[1] > 0)}")
    rows = []
    for s_i, (kind, v) in enumerate(SETTINGS):
        tot = sum(x[2][s_i] for x in P)
        h1 = sum(x[2][s_i] for x in P if x[0] <= mid_t)
        left0 = sum(1 for x in zeros if abs(x[2][s_i]) < 1e-9)
        wsold = sum(1 for x in P if x[1] > 0 and abs(x[2][s_i]) > 1e-9)
        rows.append((tot, kind, v, h1, tot - h1, left0, wsold))
    rows.sort(key=lambda o: -o[0])
    for o in rows:
        both = "BOTH HALVES +" if o[3] > 0 and o[4] > 0 else ""
        print(f"    {o[1]} {o[2]}: {o[0]:+.2f} vs holding | halves {o[3]:+.2f} / {o[4]:+.2f} | total losses left {o[5]} of {len(zeros)}, winners sold {o[6]} {both}")
    if not wf:
        return
    days = sorted({int(x[0] // 86400) for x in P})
    run = [0.0] * len(SETTINGS)
    seen = 0
    oos = []
    picks = collections.Counter()
    for di, d in enumerate(days):
        today = [x for x in P if int(x[0] // 86400) == d]
        if di >= 2 and seen >= 100:
            best = max(range(len(SETTINGS)), key=lambda s_i: run[s_i])
            oos.append(sum(x[2][best] for x in today) if run[best] > 0 else 0.0)
            if run[best] > 0:
                picks[best] += 1
        for x in today:
            for s_i in range(len(SETTINGS)):
                run[s_i] += x[2][s_i]
        seen += len(today)
    if oos:
        half = len(oos) // 2
        print(f"  walk-forward (best earlier-days setting used the next day): {sum(oos):+.2f} vs holding over {len(oos)} days | halves {sum(oos[:half]):+.2f} / {sum(oos[half:]):+.2f} | days won {sum(1 for g in oos if g > 0)}, lost {sum(1 for g in oos if g < 0)}")
        print("     picked: " + "; ".join(f"{SETTINGS[b][0]} {SETTINGS[b][1]} x{n}" for b, n in picks.most_common(5)))


# 1. the lane's own live bets
live = []
for r in load(("crypto_multiscale_underdog",), GROUPS["crypto"][2]):
    got = prepare(r)
    if not got or len(got[1]) < 3:
        continue
    reads = got[1]
    n, cost = float(r["contracts"]), float(r["stake_usd"])
    hold = (n if r["result"] == r["side"] else 0.0) - cost
    a = arrays(reads, 0)
    live.append((float(r["ts"]), hold, deltas(a, cost, hold, a[0][2], n)))
report(live, "the underdog lane's live bets (dollars)", wf=False)

# 2. the tape
wins = st.db.execute("SELECT ticker, close_ts, strike, result FROM windows WHERE result IN ('yes','no') AND close_ts > ? ORDER BY close_ts", (SINCE_S,)).fetchall()
pos = []
used = 0
t_start = _time.time()
for k, w in enumerate(wins):
    ser = w["ticker"].split("-")[0]
    if not ser.endswith("15M") or not variants.crypto_index(ser) and not variants.biquote_index(ser) or k % STRIDE:
        continue
    for side in ("yes", "no"):
        r = {"ticker": w["ticker"], "close_ts": w["close_ts"], "ts": float(w["close_ts"]) - 880.0, "side": side, "strike": w["strike"], "contracts": 1.0, "stake_usd": 1.0, "result": w["result"]}
        got = prepare(r)
        if not got:
            continue
        reads = got[1]
        i0 = next((i for i, (t, left, c, rates, bid) in enumerate(reads) if 450 <= left <= 870 and 0.20 <= bid + 0.02 <= 0.40 and rates[0] > 0), None)
        if i0 is None or i0 + 2 >= len(reads):
            continue
        px = reads[i0][4] + 0.02
        cost = px + fee(px)
        hold = (1.0 if w["result"] == side else 0.0) - cost
        a = arrays(reads, i0)
        pos.append((reads[i0][0], hold, deltas(a[1:], cost, hold, a[0][2])))
    used += 1
    if used % 300 == 0:
        print(f"  {used} windows, {len(pos)} positions, {_time.time() - t_start:.0f}s", flush=True)
print(f"windows {used} (1 in {STRIDE}), underdog-style positions {len(pos)}")
report(pos, "underdog-style positions from the tape (dollars per contract)")
print("DONE")
