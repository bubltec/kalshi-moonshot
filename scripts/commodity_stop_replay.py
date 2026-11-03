"""Stop-loss search for the live commodity lanes. Places nothing.

Two questions on every settled live commodity bet of the last DAYS days that no lane exit already sold:
 1. Calibration: when the held bid is B with T seconds left, how often does the bet win? A stop only pays if the bid is HIGHER than the true win rate there.
 2. Rules: sell at the held bid LAT seconds after the held bid has stayed <= LINE for STAY seconds (while at least MINLEFT seconds remain), less the taker fee. Printed as net dollars vs holding,
    with bets sold, "saved" (sold bets that settled as a loss) and "cost" (sold bets that would have won). Bets a lane already exited keep their recorded result.
Prints the grid overall, then the best few rules by lane.

    scripts/cloud.sh run scripts/commodity_stop_replay.py [DAYS=7] [LAT=1]
"""
import collections
import itertools
import os
import time

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
st = Store(s.db_path, s.exclude_versions)
DAYS = float(os.environ.get("DAYS", "7"))
LAT = float(os.environ.get("LAT", "1"))

rows = st.db.execute(
    "SELECT d.ts, d.lane, d.ticker, d.side, d.ask, d.stake_usd, d.exit_price, w.close_ts, w.result FROM decisions d JOIN windows w ON w.ticker = d.ticker "
    "WHERE d.action='bet' AND d.mode='trade' AND d.lane LIKE 'commodity%' AND d.ts > ? AND w.result IN ('yes','no') AND d.stake_usd > 0 ORDER BY d.ts",
    (time.time() - DAYS * 86400,),
).fetchall()

bets = []
for r in rows:
    if r["exit_price"] is not None:
        continue  # a lane exit already sold it; those keep their recorded result and are left out of the comparison
    close = float(r["close_ts"])
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (r["ticker"], r["ts"], close + 1)).fetchall()
    if len(q) < 5:
        continue
    held = [(float(t), float(b if r["side"] == "yes" else 1.0 - a)) for t, b, a in q]
    held = [(t, v) for t, v in held if 0 <= v <= 1]
    if len(held) < 5:
        continue
    bets.append({"lane": r["lane"], "ask": float(r["ask"]), "n": r["stake_usd"] / float(r["ask"]), "won": r["result"] == r["side"], "close": close, "ts": float(r["ts"]), "h": held})
print(f"{len(bets)} unexited settled live commodity bets in {DAYS:g} days (of {len(rows)} total)")


def hold_pnl(b):
    return ((1.0 if b["won"] else 0.0) - b["ask"] - fee(b["ask"])) * b["n"]


# 1. calibration: first sample under each (bid bucket, time-left bucket); did that bet win?
print("\ncalibration: held bid bucket x seconds left -> n, win rate, mean bid (a stop pays only where win rate < bid - fees)")
cal = collections.defaultdict(list)
for b in bets:
    seen = set()
    for t, v in b["h"]:
        left = b["close"] - t
        lb = next((x for x in (30, 60, 120, 240, 420, 900) if left <= x), None)
        bb = min(int(v * 10), 9)
        if lb is None or (lb, bb) in seen:
            continue
        seen.add((lb, bb))
        cal[(lb, bb)].append((b["won"], v))
print("left<=s  bid:  " + "  ".join(f"{i / 10:.1f}-{(i + 1) / 10:.1f}" for i in range(0, 9)))
for lb in (30, 60, 120, 240, 420, 900):
    cells = []
    for bb in range(0, 9):
        c = cal.get((lb, bb), [])
        cells.append(f"{sum(w for w, _ in c) / len(c):.2f}/{len(c):<3d}" if len(c) >= 8 else "  .    ")
    print(f"{lb:>6}   " + "  ".join(cells))


# 2. rule grid
def run(b, line, stay, minleft):
    """Return (pnl, sold) for one bet under the rule."""
    h = b["h"]
    since = None
    for i, (t, v) in enumerate(h):
        if b["close"] - t < minleft:
            break
        if v <= line:
            since = t if since is None else since
            if t - since >= stay:
                sell_t = t + LAT
                px = next((x for tt, x in h[i:] if tt >= sell_t), h[-1][1])
                px = max(px - 0.0, 0.0)
                return (px - b["ask"] - fee(px)) * b["n"], True
        else:
            since = None
    return hold_pnl(b), False


base = sum(hold_pnl(b) for b in bets)
print(f"\nhold all: {base:+.1f} on {sum(b['n'] * b['ask'] for b in bets):.0f} staked; full losses {sum(not b['won'] for b in bets)}")
res = []
for line, stay, minleft in itertools.product((0.65, 0.55, 0.45, 0.35, 0.25, 0.15), (0, 3, 10), (0, 30, 120)):
    tot, sold, saved, cost = 0.0, 0, 0, 0
    per = collections.defaultdict(float)
    for b in bets:
        p, sd = run(b, line, stay, minleft)
        tot += p
        per[b["lane"]] += p - hold_pnl(b)
        if sd:
            sold += 1
            saved += not b["won"]
            cost += b["won"]
    res.append((tot - base, line, stay, minleft, sold, saved, cost, dict(per)))
res.sort(reverse=True)
print("\nrule: bid <= LINE for STAY s with >= MINLEFT s left  |  vs hold, sold, saved (lost anyway), cost (would have won)")
for d, line, stay, ml, sold, saved, cost, per in res[:12]:
    print(f"line {line:.2f} stay {stay:>2} minleft {ml:>3}: {d:+7.1f}  sold {sold:>3} saved {saved:>3} cost {cost:>3}")
print("worst:", f"line {res[-1][1]:.2f} stay {res[-1][2]} minleft {res[-1][3]}: {res[-1][0]:+.1f}")
print("\nbest rule by lane (vs hold):")
for lane in sorted({b["lane"] for b in bets}):
    best = max(res, key=lambda r: r[7].get(lane, 0.0))
    print(f"{lane:<22} line {best[1]:.2f} stay {best[2]:>2} minleft {best[3]:>3}: {best[7].get(lane, 0.0):+6.1f}")
