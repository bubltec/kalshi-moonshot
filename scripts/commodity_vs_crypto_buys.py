"""What is different about the crypto buys from the commodity buys? (2026-11-13, user: "they are working great on commodities, so whats missing from crypto?"; "you only need to look at from yesterday. we had them misconfigured and weren't running for several days".)
Question, one: on the live trade-mode 15-minute bets placed since 2026-11-12 00:00 ET (nothing earlier: the lanes were misconfigured), do the commodity-lane bets and the crypto-lane bets differ in what they paid, how often they won against the price, and in how Kalshi's own quote path was behaving at the buy?
The groups are the lanes' own names (a label for the report, nothing is keyed on it). Lanes named 'manual' are excluded and counted.
Per bet, read from the market's own quote mid (tape quotes, one-second grid) at the buy, in the market's own units (the typical minute: the rms one-second move of the mid over the last 5 minutes times sqrt(60)):
  unit      the typical-minute move of the mid, in cents
  cushion   the side's mid minus 0.5, over the typical 5-minute move (how far from even, in the market's own variance)
  slope     the side's mid change over the last 5 minutes, over the typical 5-minute move
  eff       path efficiency of the side's mid over the last 5 minutes (|net| / path; 1 = straight, 0 = back and forth)
  cross     times the mid crossed 0.5 in the last 10 minutes
  jump      the biggest one-minute move of the last 10 minutes in typical minutes (a high one is a jump, not a drift)
Also: the minute of the window, the ask, the spread, and the win share against the mean ask (a price that is right has them equal).
Dollars: as booked (cost = ask + fee; a sale at the booked exit less the 0.02 slip and the taker fee, a resting-ask fill at its own price; unsold = settlement) and held to settlement. Fee on every leg.
    scripts/cloud_job.sh scripts/commodity_vs_crypto_buys.py
"""

import bisect
import calendar
import datetime as dt
import itertools
import math
import os
import collections

from kalshi_moonshot import config, db
from kalshi_moonshot.runner import EXIT_SLIP
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

os.nice(10)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
ET = dt.timezone(dt.timedelta(hours=-5))
hm = lambda t: dt.datetime.fromtimestamp(t, ET).strftime("%m-%d %H:%M")  # noqa: E731
SINCE = calendar.timegm((2026, 11, 12, 5, 0, 0))  # 11-12 00:00 ET


def dollars(r, held=False):
    ask, stake = float(r["ask"]), float(r["stake_usd"])
    cost = ask + fee(ask)
    if r["exit_price"] is not None and not held:
        px = float(r["exit_price"])
        back = px if str(r["exit_reason"] or "").startswith("curve exit (resting ask)") else px - EXIT_SLIP - fee(px)
    else:
        back = 1.0 if r["result"] == r["side"] else 0.0
    return stake * (back / cost - 1.0)


rows = st.db.execute(
    "SELECT d.ts,d.ticker,d.lane,d.side,d.ask,d.bid,d.stake_usd,d.exit_price,d.exit_reason,w.close_ts,w.result FROM decisions d JOIN windows w ON w.ticker=d.ticker "
    "WHERE d.action='bet' AND d.mode='trade' AND d.stake_usd>0 AND d.ask>0 AND d.ts>? AND d.ticker LIKE '%15M-%' AND w.result IN ('yes','no') ORDER BY d.ts",
    (SINCE,),
).fetchall()
manual = [r for r in rows if r["lane"] == "manual"]
rows = [r for r in rows if r["lane"] != "manual"]
print(f"live trade-mode 15-minute bets since 11-12 00:00 ET, settled: {len(rows)} (manual excluded: {len(manual)}); first {hm(float(rows[0]['ts']))}, last {hm(float(rows[-1]['ts']))} ET")
CRYPTO = ("crypto_",)
group = lambda lane: "crypto" if lane.startswith(CRYPTO) else "commodity"  # noqa: E731
GROUPS = ("commodity", "crypto")

feats = []
for b in rows:
    close, t = float(b["close_ts"]), float(b["ts"])
    opened = close - 900.0
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (b["ticker"], opened - 20, close)).fetchall()
    if len(q) < 20 or t - opened < 300:
        feats.append((b, None))
        continue
    qt = [float(x[0]) for x in q]
    sign = 1.0 if b["side"] == "yes" else -1.0
    mid, j, last = [], 0, None
    for sec in range(int(opened) - 20, int(t) + 1):
        while j < len(qt) and qt[j] <= sec:
            last = (float(q[j][1]) + float(q[j][2])) / 2.0
            j += 1
        mid.append(last)
    k = len(mid) - 1
    if k < 600 or mid[k] is None or any(x is None for x in mid[k - 600 :]):
        feats.append((b, None))
        continue
    side = [0.5 + sign * (x - 0.5) if x is not None else None for x in mid]
    steps = [b_ - a for a, b_ in itertools.pairwise(side[k - 300 : k + 1])]
    rate = math.sqrt(sum(x * x for x in steps) / len(steps))
    if rate <= 0:
        feats.append((b, None))
        continue
    unit_min = rate * math.sqrt(60.0)
    unit5 = rate * math.sqrt(300.0)
    path = sum(abs(x) for x in steps)
    seg = side[k - 600 : k + 1]
    cross = sum(1 for a, c in itertools.pairwise(seg) if (a - 0.5) * (c - 0.5) < 0)
    mins = [side[k - 60 * i] - side[k - 60 * (i + 1)] for i in range(10)]
    jump = max(abs(m) for m in mins) / unit_min
    feats.append((b, dict(unit=100 * unit_min, cushion=(side[k] - 0.5) / unit5, slope=(side[k] - side[k - 300]) / unit5, eff=abs(side[k] - side[k - 300]) / path if path > 0 else 0.0, cross=cross, jump=jump, minute=(t - opened) / 60.0, spread=float(q[bisect.bisect_right(qt, t) - 1][2]) - float(q[bisect.bisect_right(qt, t) - 1][1]))))


def mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else float("nan")


def summary(g):
    stake = sum(float(b["stake_usd"]) for b, _ in g)
    booked = sum(dollars(b) for b, _ in g)
    held = sum(dollars(b, True) for b, _ in g)
    won = mean(1.0 if b["result"] == b["side"] else 0.0 for b, _ in g)
    ask = mean(float(b["ask"]) for b, _ in g)
    return f"n={len(g):4d} | booked {booked:+8.2f} ({booked / stake:+.1%}) | held {held:+8.2f} ({held / stake:+.1%}) | won {won:.1%} vs ask {ask:.3f} (gap {won - ask:+.3f})"


print("\nBY GROUP, all bets")
for gname in GROUPS:
    g = [(b, f) for b, f in feats if group(b["lane"]) == gname]
    print(f"   {gname:10s} {summary(g)}")
print("\nBY LANE")
for lane in sorted({b["lane"] for b, _ in feats}):
    g = [(b, f) for b, f in feats if b["lane"] == lane]
    print(f"   {lane:28s} {summary(g)}")

read = [(b, f) for b, f in feats if f]
print(f"\nquote path read at the buy on {len(read)} of {len(feats)} bets (the rest were bought before minute 5 or have a gap)")
print("\nCURVE AT THE BUY: group means (median in brackets)")
med = lambda xs: sorted(xs)[len(xs) // 2] if xs else float("nan")  # noqa: E731
for name in ("unit", "cushion", "slope", "eff", "cross", "jump", "minute", "spread"):
    out = []
    for gname in GROUPS:
        v = [f[name] for b, f in read if group(b["lane"]) == gname]
        out.append(f"{gname} {mean(v):.3f} [{med(v):.3f}]")
    print(f"   {name:8s} " + " | ".join(out))

print("\nBY CURVE SHAPE: the same terciles for both groups together (cut from all bets), booked / held per group")
for name in ("unit", "cushion", "slope", "eff", "cross", "jump", "minute"):
    v = sorted(f[name] for _, f in read)
    c1, c2 = v[len(v) // 3], v[2 * len(v) // 3]
    for label, pick in (("low ", lambda x: x < c1), ("mid ", lambda x: c1 <= x < c2), ("high", lambda x: x >= c2)):
        parts = []
        for gname in GROUPS:
            g = [(b, f) for b, f in read if group(b["lane"]) == gname and pick(f[name])]
            parts.append(f"{gname} n={len(g):3d} {sum(dollars(b) for b, _ in g):+7.2f}/{sum(dollars(b, True) for b, _ in g):+7.2f} won-ask {(mean(1.0 if b['result'] == b['side'] else 0.0 for b, _ in g) - mean(float(b['ask']) for b, _ in g)) if g else float('nan'):+.3f}")
        print(f"   {name:8s} {label} ({c1 if label == 'low ' else c2 if label == 'high' else c1:.2f}..): " + " | ".join(parts))

print("\nBY PRICE BAND (ask)")
for lo, hi in ((0.0, 0.5), (0.5, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.0)):
    parts = []
    for gname in GROUPS:
        g = [(b, f) for b, f in feats if group(b["lane"]) == gname and lo <= float(b["ask"]) < hi]
        parts.append(f"{gname} " + (summary(g) if g else "n=0"))
    print(f"   ask {lo:.1f}-{hi:.1f}: " + " || ".join(parts))

print("\nBY MINUTE OF THE WINDOW AT THE BUY")
for lo, hi in ((0, 5), (5, 8), (8, 11), (11, 15)):
    parts = []
    for gname in GROUPS:
        g = [(b, f) for b, f in feats if group(b["lane"]) == gname and lo <= (float(b["ts"]) - (float(b["close_ts"]) - 900)) / 60 < hi]
        parts.append(f"{gname} " + (summary(g) if g else "n=0"))
    print(f"   minute {lo}-{hi}: " + " || ".join(parts))

print("\nHOW THE LOSERS LOST (as booked, losers only): sold early or held to zero")
for gname in GROUPS:
    g = [(b, f) for b, f in feats if group(b["lane"]) == gname and dollars(b) < 0]
    sold = [x for x in g if x[0]["exit_price"] is not None]
    print(f"   {gname:10s} losers n={len(g)}: sold early {len(sold)} ({sum(dollars(b) for b, _ in sold):+.2f}), held to settlement {len(g) - len(sold)} ({sum(dollars(b) for b, _ in g if b['exit_price'] is None):+.2f})")
print("DONE")
