"""What does buying at each price pay, held to settlement? (2026-11-11, user: "betting in that 45-55 range is pointless. because its a coinflip then ... betting low can be a good way to win, during a market, and betting higher is a good way to win held to settlement")
Question: across settled 15-minute windows (1 in STRIDE), buying one contract of a side at its ask with 600 / 300 / 120 s left and holding it, what is the win rate and the net per dollar staked after the taker fee, by the price paid in bands of 0.10? Both sides of every window (YES at the YES ask, NO at 1 - YES bid). Split by feed kind (a label for the report; the bands are the reading). The two halves of the period by time. No rule is fitted; this is the table the buy bands should be read against.
    scripts/cloud.sh run scripts/price_band_hold.py STRIDE=3
"""

import bisect
import calendar
import collections
import os

from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

os.nice(10)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
STRIDE = int(os.environ.get("STRIDE", "3"))
SINCE_S = int(os.environ.get("SINCE_S", str(calendar.timegm((2026, 10, 28, 0, 0, 0)))))
LEFTS = (600, 300, 120)
wins = st.db.execute("SELECT ticker, close_ts, result FROM windows WHERE result IN ('yes','no') AND close_ts > ? ORDER BY close_ts", (SINCE_S,)).fetchall()
R = []  # (ts, kind, left, price, won)
print(f"windows listed {len(wins)}", flush=True)
for k, w in enumerate(wins):
    if k % 3000 == 0:
        print(f"  at window {k}, {len(R)} positions", flush=True)
    ser = w["ticker"].split("-")[0]
    crypto = variants.crypto_index(ser)
    if not ser.endswith("15M") or not (crypto or variants.biquote_index(ser)) or k % STRIDE:
        continue
    close = float(w["close_ts"])
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (w["ticker"], close - 900, close)).fetchall()
    if len(q) < 10:
        continue
    qt = [float(x[0]) for x in q]
    for L in LEFTS:
        i = bisect.bisect_right(qt, close - L) - 1
        if i < 0 or (close - L) - qt[i] > 20:
            continue
        yb, ya = float(q[i][1]), float(q[i][2])
        for side, price in (("yes", ya), ("no", 1.0 - yb)):
            if 0.02 <= price <= 0.98:
                R.append((close, "settlement" if crypto else "proxy", L, price, 1 if w["result"] == side else 0))
print(f"positions {len(R)} from 1 in {STRIDE} windows")
mid = sorted(x[0] for x in R)[len(R) // 2]
for kind in ("settlement", "proxy", "both"):
    for L in LEFTS:
        K = [x for x in R if (kind == "both" or x[1] == kind) and x[2] == L]
        if len(K) < 500:
            continue
        print(f"\n=== feed kind {kind}, {L} s left: {len(K)} positions")
        for b in range(10):
            lo, hi = b / 10.0, (b + 1) / 10.0
            S = [x for x in K if lo <= x[3] < hi]
            if len(S) < 30:
                continue
            cost = sum(x[3] + fee(x[3]) for x in S)
            net = sum(x[4] for x in S) - cost
            c1 = sum(x[3] + fee(x[3]) for x in S if x[0] <= mid)
            n1 = sum(x[4] for x in S if x[0] <= mid) - c1
            print(f"    paid {lo:.1f} to {hi:.1f}: n {len(S):5d} | avg price {sum(x[3] for x in S) / len(S):.3f} | won {sum(x[4] for x in S) / len(S):.3f} | net per $ staked {net / cost:+.1%} | halves {n1 / c1 if c1 else 0:+.1%} / {(net - n1) / (cost - c1) if cost > c1 else 0:+.1%}")
print("DONE")
