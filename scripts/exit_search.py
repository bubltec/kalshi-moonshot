"""A wide, out-of-sample search over the curve exit's settings (2026-11-10, user: "lets continue running different variable sets until we find something").
The live bets (about 500 a group) are too few to search on: enough settings and some look good by chance. So: hypothetical positions from the whole tape, and walk-forward scoring.
Positions: every settled 15-minute window with a spot index (1 in STRIDE), both sides. Entry class "mid": the first second in minutes 1.5-14 where the side's bid + 0.02 is in 0.55-0.80; class "fav": the first second in minutes 7-14 where it is in 0.76-0.92. One contract, bought at bid + 0.02 plus the fee (the spread is assumed, the tape row here has the bid). Cushion class at entry, in typical minutes: thin under 1, medium 1-2, thick 2 or more (curve readings only, never a market name).
Settings (fixed before results), each alone against holding, fill at the next reading's bid less the fee, 8 s guard:
  curve sale: margin 0.06/0.10/0.15/0.20/0.25 x rates 900 / 900+180 x held 5/15 s
  trajectory: fall 1/1.5/2/3 typical minutes x over 60/120 s x cushion at or under 0/0.5/1, held 5 s
  gap: curve chance over the bid by 0.25/0.35/0.45 x held 5/15/30 s
  time to go: last 15/30/60/120/240 s x curve chance under 0.5 / 0.3
A. each setting always on: total against holding, two halves by time, three cushion classes. B. walk-forward: each UTC day the setting with the best total on earlier days (at least 2 days and 100 positions) is used that day; its out-of-sample total, halves and count of days won. A setting is "found" only if B is positive in both halves.
    scripts/cloud_job.sh scripts/exit_search.py STRIDE=6
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

STRIDE = int(os.environ.get("STRIDE", "6"))
SINCE_S = int(os.environ.get("SINCE_S", str(calendar.timegm((2026, 10, 28, 0, 0, 0)))))

SETTINGS = (
    [("value", (sc, m, ps)) for sc in ("900", "900+180") for m in (0.06, 0.10, 0.15, 0.20, 0.25) for ps in (5, 15)]
    + [("trajectory", (D, W, M)) for D in (1.0, 1.5, 2.0, 3.0) for W in (60, 120) for M in (0.0, 0.5, 1.0)]
    + [("gap", (G, ps)) for G in (0.25, 0.35, 0.45) for ps in (5, 15, 30)]
    + [("go", (L, cut)) for L in (15, 30, 60, 120, 240) for cut in (0.5, 0.3)]
)


def arrays(reads, i0):
    """Per reading from the entry on: (t, left, p900, pmin, tm, bid, net)."""
    out = []
    for t, left, c, rates, bid in reads[i0:]:
        root = math.sqrt(left) if left > 0 else 1.0
        p9 = phi(c / (rates[1] * root)) if rates[1] > 0 else (1.0 if c > 0 else 0.0)
        p1 = phi(c / (rates[2] * root)) if rates[2] > 0 else p9
        out.append((t, left, p9, min(p9, p1), c / (rates[0] * math.sqrt(60.0)), bid, bid - fee(bid)))
    return out


def sale(a, kind, v):
    """Index of the reading the sale fires on, or None."""
    since = None
    if kind == "trajectory":
        D, W, M = v
        hist = collections.deque()
    for i, (t, left, p9, pm, tm, bid, net) in enumerate(a):
        if kind == "trajectory":
            hist.append((t, tm))
            while hist and t - hist[0][0] > W + 1:
                hist.popleft()
        if left < 8 or bid <= 0:
            since = None
            continue
        if kind == "value":
            sc, m, ps = v
            ok = bid >= 0.05 and net - (p9 if sc == "900" else pm) >= m
        elif kind == "gap":
            G, ps = v
            ok = bid >= 0.05 and p9 - net >= G
        elif kind == "trajectory":
            ps = 5
            ok = bid >= 0.05 and t - hist[0][0] >= W - 1 and tm <= M and hist[0][1] - tm >= D
        else:
            L, cut = v
            if left <= L and p9 < cut:
                return i
            continue
        if ok:
            since = since if since is not None else t
            if t - since >= ps:
                return i
        else:
            since = None
    return None


wins = st.db.execute("SELECT ticker, close_ts, strike, result FROM windows WHERE result IN ('yes','no') AND close_ts > ? ORDER BY close_ts", (SINCE_S,)).fetchall()
pos = []  # (entry ts, class, cushion class, hold pnl, [delta per setting])
t_start = _time.time()
used = 0
for k, w in enumerate(wins):
    ser = w["ticker"].split("-")[0]
    if not ser.endswith("15M") or not (variants.crypto_index(ser) or variants.biquote_index(ser)) or k % STRIDE:
        continue
    for side in ("yes", "no"):
        r = {"ticker": w["ticker"], "close_ts": w["close_ts"], "ts": float(w["close_ts"]) - 880.0, "side": side, "strike": w["strike"], "contracts": 1.0, "stake_usd": 1.0, "result": w["result"]}
        got = prepare(r)
        if not got:
            continue
        reads = got[1]
        for cls, (lo_left, hi_left, lo_px, hi_px) in (("mid", (60, 810, 0.55, 0.80)), ("fav", (60, 480, 0.76, 0.92))):
            i0 = next((i for i, (t, left, c, rates, bid) in enumerate(reads) if lo_left <= left <= hi_left and lo_px <= bid + 0.02 <= hi_px and rates[0] > 0), None)
            if i0 is None or i0 + 2 >= len(reads):
                continue
            t, left, c, rates, bid = reads[i0]
            px = bid + 0.02
            cost = px + fee(px)
            hold = (1.0 if w["result"] == side else 0.0) - cost
            tm = c / (rates[0] * math.sqrt(60.0))
            cush = "thin" if tm < 1 else "medium" if tm < 2 else "thick"
            a = arrays(reads, i0 + 1)
            d = []
            for kind, v in SETTINGS:
                j = sale(a, kind, v)
                if j is None or j + 1 >= len(a) or a[j + 1][5] <= 0:
                    d.append(0.0)
                else:
                    sell = a[j + 1][5]
                    d.append(sell - fee(sell) - cost - hold)
            pos.append((t, cls, cush, hold, d))
    used += 1
    if used % 200 == 0:
        print(f"  {used} windows, {len(pos)} positions, {_time.time() - t_start:.0f}s", flush=True)
print(f"windows {used} (1 in {STRIDE}), positions {len(pos)}")
pos.sort(key=lambda x: x[0])
for cls in ("mid", "fav"):
    P = [x for x in pos if x[1] == cls]
    if len(P) < 200:
        print(f"\n=== {cls}: {len(P)} positions, too few")
        continue
    mid_t = P[len(P) // 2][0]
    print(f"\n=== entry class {cls}: {len(P)} positions | held to the result {sum(x[3] for x in P):+.2f} per-contract dollars | by cushion: " + ", ".join(f"{c} {sum(1 for x in P if x[2] == c)} ({sum(x[3] for x in P if x[2] == c):+.1f})" for c in ("thin", "medium", "thick")))
    rows = []
    for s_i, (kind, v) in enumerate(SETTINGS):
        tot = sum(x[4][s_i] for x in P)
        h1 = sum(x[4][s_i] for x in P if x[0] <= mid_t)
        h2 = tot - h1
        by = {c: sum(x[4][s_i] for x in P if x[2] == c) for c in ("thin", "medium", "thick")}
        sold = sum(1 for x in P if abs(x[4][s_i]) > 1e-9)
        rows.append((tot, kind, v, sold, h1, h2, by))
    rows.sort(key=lambda o: -o[0])
    print("  A. always on, best 14 and every setting positive in both halves:")
    for n_i, o in enumerate(rows):
        both = o[4] > 0 and o[5] > 0
        if n_i < 14 or both:
            tag = ("BOTH HALVES + " if both else "") + ("ALL CUSHIONS +" if all(x > 0 for x in o[6].values()) else "")
            print(f"    {o[1]} {o[2]}: {o[0]:+.2f} vs holding on {o[3]} sold | halves {o[4]:+.2f} / {o[5]:+.2f} | thin {o[6]['thin']:+.1f} medium {o[6]['medium']:+.1f} thick {o[6]['thick']:+.1f} {tag}")
    # B. walk-forward by UTC day
    days = sorted({int(x[0] // 86400) for x in P})
    by_day = {d: [x for x in P if int(x[0] // 86400) == d] for d in days}
    run = [0.0] * len(SETTINGS)
    seen_n = 0
    oos = []
    picks = collections.Counter()
    for di, d in enumerate(days):
        if di >= 2 and seen_n >= 100:
            best = max(range(len(SETTINGS)), key=lambda s_i: run[s_i])
            if run[best] > 0:
                got = sum(x[4][best] for x in by_day[d])
                oos.append((d, got, best))
                picks[best] += 1
            else:
                oos.append((d, 0.0, None))
        for x in by_day[d]:
            for s_i in range(len(SETTINGS)):
                run[s_i] += x[4][s_i]
        seen_n += len(by_day[d])
    if oos:
        half = len(oos) // 2
        tot = sum(g for _, g, _ in oos)
        print(f"  B. walk-forward (the best earlier-days setting, used the next day): {tot:+.2f} vs holding over {len(oos)} days | halves {sum(g for _, g, _ in oos[:half]):+.2f} / {sum(g for _, g, _ in oos[half:]):+.2f} | days won {sum(1 for _, g, _ in oos if g > 0)}, lost {sum(1 for _, g, _ in oos if g < 0)}, stood down {sum(1 for _, _, b in oos if b is None)}")
        print("     settings it picked: " + "; ".join(f"{SETTINGS[b][0]} {SETTINGS[b][1]} x{n}" for b, n in picks.most_common(5)))
    for cush in ("thin", "medium", "thick"):
        run = [0.0] * len(SETTINGS)
        seen_n = 0
        oos = []
        for di, d in enumerate(days):
            today = [x for x in by_day[d] if x[2] == cush]
            if di >= 2 and seen_n >= 60:
                best = max(range(len(SETTINGS)), key=lambda s_i: run[s_i])
                oos.append(sum(x[4][best] for x in today) if run[best] > 0 else 0.0)
            for x in today:
                for s_i in range(len(SETTINGS)):
                    run[s_i] += x[4][s_i]
            seen_n += len(today)
        if oos:
            half = len(oos) // 2
            print(f"     cushion {cush}: walk-forward {sum(oos):+.2f} | halves {sum(oos[:half]):+.2f} / {sum(oos[half:]):+.2f}")
print("DONE")
