"""Whole-stake losses (2026-11-10, user: "$0 losses are devastating at $100 bets"). Two questions on the same live bets as studies/results/2026-11-10-late-drop-exit.txt (same groups, dates, sale at the bid 2 s later less the fee; shared code copied from scripts/late_drop_exit_replay.py).

1. Exit rules ranked by zeros, not by net dollars. For each late-drop variant (W seconds left, K bid-units, HI high): how many held-to-result whole-stake losses it turns into partial losses, the dollars of whole losses left, winners sold and the net change. A rule that removes zeros at a small net cost is shown even when its net is negative.
2. Where the zeros come from, by curve state: bets binned by buy-price band, the spot's 5-minute path efficiency tercile (group's own cutoffs) and minutes left at the buy. Whole-loss rate, net per dollar staked and n, in two halves. A cell is flagged when its whole-loss rate is at least twice the group's own in both halves with n >= 15: a candidate for a smaller stake. Nothing is sized here and nothing is live.

    scripts/cloud_job.sh scripts/zero_loss_study.py
"""

import bisect
import calendar
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
    "crypto": (("crypto_multiscale", "crypto_multiscale_underdog"), int(os.environ.get("SINCE_CRYPTO", "1794000000"))),
    "commodity_multiscale": (("commodity_multiscale",), calendar.timegm((2026, 11, 9, 14, 0, 0))),
    "favorites": (("commodity_energy", "commodity_wti", "commodity_precious", "commodity_industrial"), calendar.timegm((2026, 11, 3, 0, 0, 0))),
}
DELAY = 2


def load(lanes, since):
    marks = ",".join("?" * len(lanes))
    rows = st.db.execute(
        "SELECT d.ts,d.ticker,d.lane,d.side,d.contracts,d.stake_usd,w.close_ts,w.result FROM decisions d JOIN windows w ON w.ticker=d.ticker "
        f"WHERE d.action='bet' AND d.mode='trade' AND d.contracts>0 AND d.stake_usd>0 AND d.ts>? AND d.lane IN ({marks}) AND w.result IN ('yes','no') ORDER BY d.ts",
        (since, *lanes),
    ).fetchall()
    seen = {}
    for r in rows:
        seen[(r["ticker"], r["side"])] = r
    return list(seen.values())


def prepare(r):
    """(path efficiency before the buy, per-second held bid from the buy to the close or None)."""
    ser = r["ticker"].split("-")[0]
    ix = variants.crypto_index(ser) or variants.biquote_index(ser)
    close, t0 = float(r["close_ts"]), float(r["ts"])
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (r["ticker"], t0 - 400, close)).fetchall()
    if len(q) < 10:
        return None
    eff = None
    if ix:
        tk = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts", (ix, t0 - 300, t0)).fetchall()
        if len(tk) > 30:
            seg = [math.log(float(x[1])) for x in tk]
            path = sum(abs(b - a) for a, b in itertools.pairwise(seg))
            eff = abs(seg[-1] - seg[0]) / path if path > 0 else None
    qt = [float(x[0]) for x in q]
    bids = []
    for t in range(int(t0) - 400, int(close) + 1):
        i = bisect.bisect_right(qt, t) - 1
        if i < 0 or t - qt[i] > 20:
            bids.append(None)
            continue
        yb, ya = float(q[i][1]), float(q[i][2])
        bids.append(yb if r["side"] == "yes" else 1.0 - ya)
    return eff, int(t0) - 400, bids


def decide(r, base, bids, W, K, HI, literal=False):
    """(pnl, sold) of the bet under the late-drop rule."""
    n, cost = float(r["contracts"]), float(r["stake_usd"])
    won = r["result"] == r["side"]
    hold = (n if won else 0.0) - cost
    close = int(float(r["close_ts"]))
    for t in range(max(close - W, int(float(r["ts"])) + 5), close - DELAY):
        k = t - base
        now = bids[k]
        if now is None:
            continue
        win = [b for b in bids[k - 60 : k] if b is not None]
        if len(win) < 20:
            continue
        H = max(win)
        if literal:
            fire = H >= 0.90 and now < 0.90 and close - t <= 30
        else:
            ch = [(b - a) ** 2 for a, b in itertools.pairwise(bids[k - 300 : k + 1]) if a is not None and b is not None]
            if len(ch) < 100:
                continue
            unit = math.sqrt(sum(ch) / len(ch) * 60.0)
            fire = H >= HI and unit > 0 and (H - now) >= K * unit
        if not fire:
            continue
        sell = bids[k + DELAY]
        if sell is None or sell < 0.05:
            continue
        return n * (sell - fee(sell)) - cost, True
    return hold, False


def shapes(effs):
    e = sorted(x for x in effs if x is not None)
    return (e[len(e) // 3], e[2 * len(e) // 3]) if len(e) >= 9 else (0.0, 1.0)



def cell_key(r, e, lo, hi):
    price = float(r["stake_usd"]) / float(r["contracts"])
    band = "<.55" if price < 0.55 else "<.70" if price < 0.70 else "<.80" if price < 0.80 else "<.90" if price < 0.90 else ">=.90"
    shp = "unknown" if e is None else "choppy" if e <= lo else "straight" if e >= hi else "middle"
    left = float(r["close_ts"]) - float(r["ts"])
    lb = "last 3m" if left < 180 else "3-6m" if left < 360 else "6-10m" if left < 600 else "10m+"
    return f"{band} {shp} {lb}"


for gname, (lanes, since) in GROUPS.items():
    data = []
    bets = load(lanes, since)
    for r in bets:
        got = prepare(r)
        if got:
            data.append((r, *got))
    print(f"\n=== {gname}: settled live bets {len(bets)} | replayed {len(data)}")
    if len(data) < 20:
        print("  too few bets")
        continue
    lo, hi = shapes([e for _, e, _, _ in data])
    mid = sorted(float(r["ts"]) for r, *_ in data)[len(data) // 2]
    stake = [float(r["stake_usd"]) for r, *_ in data]
    hold = [((float(r["contracts"]) if r["result"] == r["side"] else 0.0) - c) for (r, *_), c in zip(data, stake, strict=True)]
    zeros = [i for i, h in enumerate(hold) if abs(h + stake[i]) < 1e-9]
    print(f"  held: net {sum(hold):+.2f}, whole-stake losses {len(zeros)} of {len(data)} (${sum(stake[i] for i in zeros):.2f} staked on them)")
    print("  1. exit rules by zeros (W, K, HI): zeros turned partial | whole losses left | winners sold | net vs holding | worst loss left")
    rows = []
    for W in (30, 45, 60):
        for K in (1, 2, 3):
            for HI in (0.0, 0.75, 0.85):
                res = [decide(r, base, bids, W, K, HI, False) for r, e, base, bids in data]
                left0 = [i for i in zeros if abs(res[i][0] + stake[i]) < 1e-9]
                fixed = len(zeros) - len(left0)
                wins = sum(1 for (p, f), h in zip(res, hold, strict=True) if f and h > 0)
                rows.append((fixed, sum(p - h for (p, _), h in zip(res, hold, strict=True)), (W, K, HI), len(left0), wins, min(p for p, _ in res)))
    rows.sort(key=lambda x: (-x[0], -x[1]))
    for fixed, d, v, left0, wins, worst in rows[:10]:
        print(f"    {v}: {fixed} of {len(zeros)} | {left0} left | {wins} winners sold | {d:+.2f} | worst bet {worst:+.2f}")
    print("  2. whole-loss rate by curve state (price band, shape, time left): flagged cells")
    g_rate = len(zeros) / len(data)
    cells = {}
    for i, (r, e, base, bids) in enumerate(data):
        k = cell_key(r, e, lo, hi)
        c = cells.setdefault(k, {"n": [0, 0], "z": [0, 0], "net": [0.0, 0.0], "stk": [0.0, 0.0]})
        h = 0 if float(r["ts"]) <= mid else 1
        c["n"][h] += 1
        c["z"][h] += 1 if i in set(zeros) else 0
        c["net"][h] += hold[i]
        c["stk"][h] += stake[i]
    flagged = 0
    for k, c in sorted(cells.items(), key=lambda kv: -sum(kv[1]["z"])):
        n = sum(c["n"])
        if n < 15 or min(c["n"]) < 5:
            continue
        r1, r2 = c["z"][0] / c["n"][0], c["z"][1] / c["n"][1]
        if r1 >= 2 * g_rate and r2 >= 2 * g_rate:
            flagged += 1
            npd = sum(c["net"]) / sum(c["stk"])
            print(f"    {k}: n {n}, whole losses {sum(c['z'])} ({sum(c['z']) / n:.0%}; group {g_rate:.0%}), halves {r1:.0%} / {r2:.0%}, net per $ {npd:+.3f}")
    print(f"  flagged cells: {flagged}")
print("DONE")
