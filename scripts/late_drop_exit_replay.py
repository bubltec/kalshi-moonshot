"""Does selling a held bid that drops late in the window beat holding? (2026-11-10, user: "if it's in the 90s in the last 30s and drops below 90, just sell... we can make it curve logic")

Question: on the same live bets, if the held-side bid falls in the last W seconds from where it stood just before, does selling at that bid (less the fee) beat holding to the result?
Population: live bets of all lanes, one row per market and side, settled windows. The counterfactual is holding to the result (real sales are not replayed, same as studies/results/2026-11-09-shock-exit-improve.txt). Groups by the lane's own buy rule, never by market: "crypto" (crypto_multiscale + underdog), "commodity_multiscale", "favorites" (commodity favorite lanes).
The rule (curve version): at second t with W or fewer seconds left, let H be the highest held bid over the 60 s before t and U the bid's own typical minute (rms one-second bid change over the 300 s before t, times sqrt(60)). Sell when H is at least HI (the bid was "high") and H - bid >= K * U. Sale price is the bid 2 s later (order latency) less the fee; a bid under 0.05 is not sold into.
Variants fixed before the results: W 30 / 45 / 60; K 1 / 2 / 3 / 4; HI 0.75 (any strong bid) / 0.85 / 0.90. The user's literal rule is also run: in the last 30 s, bid was >= 0.90 and is now < 0.90 (a fixed number, the user's, shown for comparison; rule 2 of CLAUDE.md allows it only as structural).
Each row: bets changed, net change against holding, losers saved / winners sold, two halves of the period (median ts), curve shapes (terciles of the spot's path efficiency over the 5 minutes before the buy). A variant counts only when the change is positive in both halves and in each shape it claims.

    scripts/cloud_job.sh scripts/late_drop_exit_replay.py
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


for gname, (lanes, since) in GROUPS.items():
    data = []
    bets = load(lanes, since)
    for r in bets:
        got = prepare(r)
        if got:
            data.append((r, *got))
    print(f"\n=== {gname}: lanes {lanes} | settled live bets {len(bets)} | replayed {len(data)}")
    if len(data) < 20:
        print("  too few bets")
        continue
    lo, hi = shapes([e for _, e, _, _ in data])
    mid = sorted(float(r["ts"]) for r, *_ in data)[len(data) // 2]
    hold = [((float(r["contracts"]) if r["result"] == r["side"] else 0.0) - float(r["stake_usd"])) for r, *_ in data]
    print(f"  held to the result {sum(hold):+.2f} | shape cutoffs {lo:.3f} {hi:.3f}")

    def shape(e):
        return "unknown" if e is None else "choppy" if e <= lo else "straight" if e >= hi else "middle"

    out = []
    grid = [(W, K, HI, False) for W in (30, 45, 60) for K in (1, 2, 3, 4) for HI in (0.75, 0.85, 0.90)] + [(30, 0, 0.90, True)]
    for W, K, HI, lit in grid:
        res = [decide(r, base, bids, W, K, HI, lit) for r, e, base, bids in data]
        d = [p - h for (p, _), h in zip(res, hold, strict=True)]
        changed = [(x, i) for i, x in enumerate(d) if abs(x) > 1e-9]
        if not changed:
            continue
        h1 = sum(x for x, i in changed if float(data[i][0]["ts"]) <= mid)
        h2 = sum(x for x, i in changed if float(data[i][0]["ts"]) > mid)
        by = {}
        for x, i in changed:
            by[shape(data[i][1])] = by.get(shape(data[i][1]), 0.0) + x
        saved = sum(1 for (p, f), h in zip(res, hold, strict=True) if f and h < 0)
        wins = sum(1 for (p, f), h in zip(res, hold, strict=True) if f and h > 0)
        out.append((sum(d), (W, K, HI, "user-literal" if lit else ""), len(changed), saved, wins, h1, h2, by))
    out.sort(key=lambda o: -o[0])
    for o in out[:14]:
        sh = " ".join(f"{k} {v:+.1f}" for k, v in sorted(o[7].items()))
        print(f"    {o[1]}: vs holding {o[0]:+.2f} on {o[2]} bets sold | losers saved {o[3]}, winners sold {o[4]} | halves {o[5]:+.2f} / {o[6]:+.2f} | {sh}")
    lit = [o for o in out if o[1][3]]
    for o in lit:
        sh = " ".join(f"{k} {v:+.1f}" for k, v in sorted(o[7].items()))
        print(f"  USER LITERAL: vs holding {o[0]:+.2f} on {o[2]} sold | losers saved {o[3]}, winners sold {o[4]} | halves {o[5]:+.2f} / {o[6]:+.2f} | {sh}")
    both = [o for o in out if o[5] > 0 and o[6] > 0]
    print(f"  positive in both halves: {len(both)} of {len(out)} variants")
print("DONE")
