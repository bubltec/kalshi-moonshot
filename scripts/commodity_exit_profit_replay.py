"""Profit-first exits replayed on the last six days of the live commodity bets (2026-11-24; user: "I'm not interested in the bets from 11-09. I only care about the past 5-6 days of tape and running new logic against that"; "the concept is stop trying to win the market, and just profit"; "selling is more important").

Question: on the live commodity bets since 2026-11-18 20:00 UTC (every commodity lane except multiscale, mode trade, one per market, side and lane), does a PROFIT-FIRST exit beat what actually happened (the lane's own sale, else held to settlement)? A replayed sale only counts if it comes before the lane's own sale. Fee on both legs, sale at the side bid minus 1c, cost at the decision's ask.
Rules (every size in the bet's own units: u = the side bid's typical minute before the buy, rms 60 s change sampled every 5 s over the market's quotes before the buy, floor 0.01; w = max(1c, u x sqrt(10/60)), the bid's typical 10 s move, the step's "wobble"):
  EARLY(m): the quote-only body of the PriskP `early_take` step (watcher._sell_early_take) with its wobble scaled by m (m = 1 is the step): ARMED the first time the net after both fees is at least m x w; then sold at the first of (a) the bid m x w or more under its high since arming for 3% of the time left, no new high meanwhile, (b) no new high for 10% of the time left; never at a net loss. NOT replayed: the spot's cushion (the step holds while the spot still climbs) and the hold-value check against the spot's chance (no spot here), so this is the step with its two vetoes off.
  LOSS(K, choppy): sell the first time the bid is K x u under its high since the buy (any time), for all bets or only those whose bid path efficiency in the 5 minutes before the buy is at or under 0.11 (the earlier study's cut, found after seeing 11-09 to 11-24).
  Combinations: whichever comes first.
Reported: n, what happened, each rule's net, change, sold, beat, lost, by lane group and day of the window, and by curve shape (path efficiency of the bid before the buy, in thirds; a proxy). Settings are a grid; none was tuned on these bets; a setting picked from this table is chosen after seeing it.
Not covered: fills at the replayed price, the sale queue, the resting sale, quotes the tape did not record, the spot vetoes.

    scripts/cloud_job.sh scripts/commodity_exit_profit_replay.py
"""
import bisect
import calendar
import collections
import math
import os
import time

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

os.nice(15)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
t0 = calendar.timegm((2026, 11, 18, 20, 0, 0))
rows = st.db.execute(
    "SELECT ts,ticker,lane,side,ask,stake_usd,outcome,exit_price,exit_ts,close_time FROM decisions WHERE action='bet' AND mode='trade' AND stake_usd>0 AND ts>? AND lane LIKE 'commodity_%' AND lane<>'commodity_multiscale' AND (outcome IS NOT NULL OR exit_price IS NOT NULL) ORDER BY ts",
    (t0,),
).fetchall()
seen = {}
for r in rows:
    seen[(r["ticker"], r["lane"], r["side"], round(r["ask"], 3))] = r
print("commodity live bets settled or sold since 2026-11-18 20:00 UTC:", len(seen))


def actual(r):
    c = r["stake_usd"] / r["ask"]
    if r["exit_price"] is not None:
        return (r["exit_price"] - r["ask"]) * c - (fee(r["ask"]) + fee(max(r["exit_price"], 0.01))) * c
    return ((1 - r["ask"]) * c if r["outcome"] == r["side"] else -r["ask"] * c) - fee(r["ask"]) * c


def sidebid(side, q):
    return q["bid"] if side == "yes" else 1 - q["ask"]


def _close(r):
    """The market's close as a time: 15 minutes after the open the ticker's window names; the decision's close_time when it parses."""
    try:
        return calendar.timegm(time.strptime(str(r["close_time"])[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return r["ts"] + 450.0


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
    bets.append({"r": r, "post": [(float(x["ts"]), sidebid(r["side"], x)) for x in post], "qu": qu, "eff": eff, "act": actual(r), "close": _close(r)})
print("bets replayed:", len(bets), " skipped (no tape or too little history):", skipped)


POOLED_CUT = 0.11


def replay(b, m, K, chop_only, g=0.0, maker_fee=False, choppy_rest=False, S=0.0):
    """(net, sold) for EARLY(m) (m = 0 off) and LOSS(K) (K = 0 off; chop_only gates it to the choppy bids), whichever sale comes first."""
    r = b["r"]
    e, qu = r["ask"], b["qu"]
    c = r["stake_usd"] / e
    end = r["exit_ts"] if r["exit_ts"] else 1e18
    close = r["ts"] + 0.0
    left0 = b["close"] - r["ts"]
    w = max(0.01, qu * math.sqrt(10.0 / 60.0))
    hi, armed, hi_ts, fall_since = e, False, 0.0, 0.0
    loss_on = K and (not chop_only or b["eff"] <= POOLED_CUT)
    rest = None
    if g and (not choppy_rest or b["eff"] <= POOLED_CUT):  # a resting sell order at the price that nets g x u after the buy fee (no sale fee when maker_fee is False: the resting-sale notes treat it as fee-free)
        rest = min(0.99, e + fee(e) + g * qu)
    for t, bid in b["post"]:
        if t >= end:
            break
        left = max(b["close"] - t, 1.0)
        hi = max(hi, bid)
        sale = max(bid - 0.01, 0.01)
        net_sale = (sale - e) - fee(e) - fee(sale)
        if rest is not None and bid >= rest - 1e-9:  # a buyer at the resting price: the order fills there
            return ((rest - e) - fee(e) - (fee(rest) if maker_fee else 0.0)) * c, True
        if S and bid <= e - S * qu:  # the exit before losing: the bid is S typical minutes under the price paid (a floor on the bet, not on its high)
            return net_sale * c, True
        if loss_on and bid <= hi - K * qu:
            return net_sale * c, True
        if m:
            net = (bid - fee(bid)) - (e + fee(e))
            if not armed:
                if net >= m * w:
                    armed, hi, hi_ts = True, bid, t
                continue
            if bid > hi + 1e-9:
                hi, hi_ts, fall_since = bid, t, 0.0
            if net <= 0:
                continue
            if hi - bid >= m * w - 1e-9:
                fall_since = fall_since or t
            else:
                fall_since = 0.0
            fell = fall_since and t - fall_since >= 0.03 * left
            stalled = t - hi_ts >= 0.10 * left
            if (fell or stalled) and net_sale > 0:
                return net_sale * c, True
    return b["act"], False


RULES = [
    ("EARLY m=1 (the step)", 1, 0, False), ("EARLY m=2", 2, 0, False), ("EARLY m=0.5", 0.5, 0, False),
    ("LOSS K=4 all", 0, 4, False), ("LOSS K=4 choppy only", 0, 4, True),
    ("EARLY m=1 + LOSS K=4 all", 1, 4, False), ("EARLY m=1 + LOSS K=4 choppy", 1, 4, True),
    ("EARLY m=2 + LOSS K=4 choppy", 2, 4, True), ("EARLY m=1 + LOSS K=3 choppy", 1, 3, True),
    ("REST sell g=0.5, all", 0, 0, False, 0.5), ("REST sell g=1, all", 0, 0, False, 1.0), ("REST sell g=2, all", 0, 0, False, 2.0),
    ("STOP S=0.5 (under the price paid)", 0, 0, False, 0, False, False, 0.5), ("STOP S=1", 0, 0, False, 0, False, False, 1.0), ("STOP S=2", 0, 0, False, 0, False, False, 2.0),
    ("REST g=1 + STOP S=0.5, all", 0, 0, False, 1.0, False, False, 0.5), ("REST g=1 + STOP S=1, all", 0, 0, False, 1.0, False, False, 1.0), ("REST g=1 + STOP S=2, all", 0, 0, False, 1.0, False, False, 2.0),
    ("REST g=0.5 + STOP S=1, all", 0, 0, False, 0.5, False, False, 1.0),
    ("REST g=1 + STOP S=1, choppy only", 0, 0, False, 1.0, False, True, 1.0),
    ("REST sell g=1, choppy only", 0, 0, False, 1.0, False, True), ("REST sell g=0.5, choppy only", 0, 0, False, 0.5, False, True),
    ("REST sell g=1 (maker fee), all", 0, 0, False, 1.0, True), ("REST g=1 + EARLY m=1, all", 1, 0, False, 1.0),
]


def table(label, sub):
    if not sub:
        return
    n = len(sub)
    base = sum(b["act"] for b in sub)
    print(f"\n{label}: n={n}  what happened ${base:+.2f}  ({sum(1 for b in sub if b['act'] > 0)} won, {sum(1 for b in sub if b['act'] <= 0)} lost)")
    print("  rule                          net      change  per-bet(se)       sold  beat  lost")
    for name, m, K, chop, *rest in RULES:
        d, sold, beat, lost = [], 0, 0, 0
        for b in sub:
            v, did = replay(b, m, K, chop, *rest)
            d.append(v - b["act"])
            if did:
                sold += 1
                beat += v > b["act"] + 1e-9
                lost += v < b["act"] - 1e-9
        mean = sum(d) / n
        se = math.sqrt(sum((x - mean) ** 2 for x in d) / max(n - 1, 1) / n)
        print(f"  {name:28s} {base + sum(d):+9.2f} {sum(d):+9.2f}  {mean:+.3f}({se:.3f})  {sold:5d} {beat:5d} {lost:5d}")


import time as _time

table("ALL", bets)
for lane in sorted({b["r"]["lane"] for b in bets}):
    table(f"lane {lane}", [b for b in bets if b["r"]["lane"] == lane])
for day in sorted({_time.strftime("%m-%d", _time.gmtime(b["r"]["ts"])) for b in bets}):
    table(f"day {day} (UTC)", [b for b in bets if _time.strftime("%m-%d", _time.gmtime(b["r"]["ts"])) == day])
table("bets that lost more than half the stake (selected by outcome: every sale rule wins here)", [b for b in bets if b["act"] < -0.5 * b["r"]["stake_usd"]])
effs = sorted(b["eff"] for b in bets)
if len(effs) >= 9:
    c1, c2 = effs[len(effs) // 3], effs[2 * len(effs) // 3]
    table(f"path efficiency of the bid before the buy: low third (<= {c1:.2f})", [b for b in bets if b["eff"] <= c1])
    table(f"middle third ({c1:.2f} to {c2:.2f})", [b for b in bets if c1 < b["eff"] <= c2])
    table(f"high third (> {c2:.2f})", [b for b in bets if b["eff"] > c2])
