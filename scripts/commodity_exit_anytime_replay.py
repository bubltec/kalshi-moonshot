"""An any-time loss sale and a profit take, replayed on the live commodity bets (2026-11-24; user, on KXGOLD15M-26NOV242030 NO bought 0.88, sold 0.19: "it doesn't try to profit, and it doesn't sell to avoid loss").

Question: on the live commodity bets since 2026-11-09 (every commodity lane except multiscale, mode trade, one per market, side and lane), would a sale rule that works from the first second have beaten what actually happened (the lane's own sale, else held to settlement)? A replayed sale only counts if it comes before the lane's own sale.
Rules (every size is in the market's own typical minutes, qu = the root-mean-square 60 s change of the side's bid, sampled every 5 s over the hour before the buy, floor 0.01; no fixed cents):
  LOSS(K, W): after W seconds from the buy, sell at the first quote whose side bid is K typical minutes or more under its high since the buy (the bid at the buy counts as the high).
  TAKE(G): sell at the first quote whose side bid, after a 1c slip and both fees, is G typical minutes or more over the price paid.
  Both: whichever comes first.
Sale price: side bid minus 1c, fee at the price paid and at the sale. Cost at the decision's ask, contracts = stake / ask. Bets with fewer than 20 pre-buy changes are skipped and counted.
Reported: n, net dollars of what happened, net of each rule, the change, how many bets sold, how many the rule beat and lost to what happened, and the result by curve shape, where the shape is the PATH EFFICIENCY of the side's bid in the 5 minutes before the buy in thirds (a proxy: curvemodel.py's level, slope and curvature readings are not computed here), and by the bets that lost more than half their stake. Settings are a grid; nothing was tuned to the gold bet, and any setting picked from this table after seeing it is a setting chosen after seeing the results.
Not covered: fills at the replayed price, the sale queue, the resting sale, quotes the tape did not record.

    scripts/cloud_job.sh scripts/commodity_exit_anytime_replay.py
"""

import bisect
import calendar
import collections
import math
import os

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

os.nice(15)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
t0 = calendar.timegm((2026, 11, 9, 0, 0, 0))
rows = st.db.execute(
    "SELECT ts,ticker,lane,side,ask,stake_usd,outcome,exit_price,exit_ts FROM decisions WHERE action='bet' AND mode='trade' AND stake_usd>0 AND ts>? AND lane LIKE 'commodity_%' AND lane<>'commodity_multiscale' AND (outcome IS NOT NULL OR exit_price IS NOT NULL) ORDER BY ts",
    (t0,),
).fetchall()
seen = {}
for r in rows:
    seen[(r["ticker"], r["lane"], r["side"], round(r["ask"], 3))] = r
print("commodity live bets settled or sold since 2026-11-09:", len(seen))


def actual(r):
    c = r["stake_usd"] / r["ask"]
    if r["exit_price"] is not None:
        return (r["exit_price"] - r["ask"]) * c - (fee(r["ask"]) + fee(max(r["exit_price"], 0.01))) * c
    return ((1 - r["ask"]) * c if r["outcome"] == r["side"] else -r["ask"] * c) - fee(r["ask"]) * c


def sidebid(side, q):
    return q["bid"] if side == "yes" else 1 - q["ask"]


bets = []
skipped = 0
for r in seen.values():
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts>=? AND ts<=? AND bid>0 AND ask>0 ORDER BY ts", (r["ticker"], r["ts"] - 3600, r["ts"] + 960)).fetchall()
    pre = [x for x in q if x["ts"] <= r["ts"]]
    post = [x for x in q if x["ts"] > r["ts"]]
    if len(pre) < 20 or len(post) < 3:
        skipped += 1
        continue
    pts = [float(x["ts"]) for x in pre]
    sb = [sidebid(r["side"], x) for x in pre]

    def at(t, pts=pts, sb=sb):
        i = bisect.bisect_right(pts, t) - 1
        return sb[i] if i >= 0 else None

    ch = []
    t = pts[0] + 60
    while t <= r["ts"]:
        a, b = at(t), at(t - 60)
        if a is not None and b is not None:
            ch.append(a - b)
        t += 5
    if len(ch) < 20:
        skipped += 1
        continue
    qu = max(math.sqrt(sum(c * c for c in ch) / len(ch)), 0.01)
    five = [at(t) for t in range(int(r["ts"]) - 300, int(r["ts"]) + 1, 5) if at(t) is not None]
    path = sum(abs(b - a) for a, b in zip(five, five[1:]))
    eff = abs(five[-1] - five[0]) / path if len(five) > 2 and path > 0 else 0.0
    bets.append({"r": r, "post": [(float(x["ts"]), sidebid(r["side"], x)) for x in post], "qu": qu, "eff": eff, "act": actual(r)})
print("bets replayed:", len(bets), " skipped (no tape or too little history):", skipped)


def replay(b, K, W, G):
    r = b["r"]
    e, qu = r["ask"], b["qu"]
    c = r["stake_usd"] / e
    end = r["exit_ts"] if r["exit_ts"] else 1e18
    hi = e
    for t, bid in b["post"]:
        if t >= end:
            break
        hi = max(hi, bid)
        sale = max(bid - 0.01, 0.01)
        if K and t - r["ts"] >= W and bid <= hi - K * qu:
            return (sale - e) * c - (fee(e) + fee(sale)) * c, True
        if G and ((sale - e) - fee(e) - fee(sale)) >= G * qu:
            return (sale - e) * c - (fee(e) + fee(sale)) * c, True
    return b["act"], False


def table(label, sub):
    if not sub:
        return
    n = len(sub)
    base = sum(b["act"] for b in sub)
    print(f"\n{label}: n={n}  what happened ${base:+.2f}")
    print("  rule                          net      change  per-bet(se)       sold  beat  lost")
    for name, K, W, G in [
        ("LOSS K=2 W=0", 2, 0, 0), ("LOSS K=3 W=0", 3, 0, 0), ("LOSS K=4 W=0", 4, 0, 0), ("LOSS K=6 W=0", 6, 0, 0),
        ("LOSS K=3 W=30", 3, 30, 0), ("LOSS K=4 W=30", 4, 30, 0), ("LOSS K=6 W=30", 6, 30, 0),
        ("TAKE G=0.5", 0, 0, 0.5), ("TAKE G=1", 0, 0, 1), ("TAKE G=2", 0, 0, 2),
        ("LOSS K=4 W=0 + TAKE G=1", 4, 0, 1), ("LOSS K=6 W=0 + TAKE G=1", 6, 0, 1),
    ]:
        d, sold, beat, lost = [], 0, 0, 0
        for b in sub:
            v, did = replay(b, K, W, G)
            d.append(v - b["act"])
            if did:
                sold += 1
                beat += v > b["act"] + 1e-9
                lost += v < b["act"] - 1e-9
        m = sum(d) / n
        se = math.sqrt(sum((x - m) ** 2 for x in d) / max(n - 1, 1) / n)
        print(f"  {name:28s} {base + sum(d):+9.2f} {sum(d):+9.2f}  {m:+.3f}({se:.3f})  {sold:5d} {beat:5d} {lost:5d}")


table("ALL", bets)
table("lost more than half the stake", [b for b in bets if b["act"] < -0.5 * b["r"]["stake_usd"]])
effs = sorted(b["eff"] for b in bets)
if len(effs) >= 9:
    c1, c2 = effs[len(effs) // 3], effs[2 * len(effs) // 3]
    table(f"path efficiency of the bid, 5 min before the buy: low third (<= {c1:.2f})", [b for b in bets if b["eff"] <= c1])
    table(f"middle third ({c1:.2f} to {c2:.2f})", [b for b in bets if c1 < b["eff"] <= c2])
    table(f"high third (> {c2:.2f})", [b for b in bets if b["eff"] > c2])
by = collections.Counter(b["r"]["lane"] for b in bets)
print("\nbets by lane:", dict(by))
