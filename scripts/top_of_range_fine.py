"""Inside the top fifth of a side's range: is the loss at the fresh high or across the whole fifth? (2026-11-16, user: "how do we get this corrected so it is not skipping winners"; the paper lane's 166 skipped sides lost at the very top of the range, -21.6% on 64, and about broke even from 0.80 to 0.99.)

The same question on a week: every 15 seconds in every settled market of the live crypto lane's series since Monday 2026-11-09, each side in the live lane's own band (ask 0.57 to 0.82, spread 0.03 or under, 3 minutes in or later), by where its mid sits in its own range since the open (read every 5 s), held to settlement at the ask, fee in. Also by the ask inside the band, for the very top. Bands fixed before the run. No coin is named.

Run on the host: scripts/cloud.sh run scripts/top_of_range_fine.py
"""
import bisect
import datetime
from collections import defaultdict

from kalshi_moonshot import config, db
from kalshi_moonshot.lanes import lane_specs
from kalshi_moonshot.strategy import fee_per_contract as fee

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 9, 0, 0, tzinfo=ET).timestamp()
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
series = set(lane_specs(s)["crypto_multiscale"].series)
wins = [w for w in st.execute("SELECT ticker, series, close_ts, result FROM windows WHERE close_ts>? AND result IN ('yes','no') ORDER BY close_ts", (SINCE,)).fetchall() if w["series"] in series]
POS = ((0.0, 0.4, "under 0.40"), (0.4, 0.6, "0.40 to 0.60"), (0.6, 0.8, "0.60 to 0.80"), (0.8, 0.9, "0.80 to 0.90"), (0.9, 0.97, "0.90 to 0.97"), (0.97, 0.995, "0.97 to 0.99"), (0.995, 2, "at the very top"))
cell = defaultdict(lambda: [0, 0, 0.0, 0.0, set()])
top = defaultdict(lambda: [0, 0, 0.0, 0.0, set()])
for w in wins:
    close = float(w["close_ts"])
    opened = close - 900.0
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], opened - 20, close)).fetchall()
    q = [(float(x[0]), float(x[1] or 0.0), float(x[2] or 0.0)) for x in q if x[2] and float(x[2]) > 0 and not (float(x[1] or 0.0) <= 0 and float(x[2]) >= 1)]
    if len(q) < 50:
        continue
    qt = [x[0] for x in q]
    lo = hi = None
    for t in range(int(opened), int(close) - 15, 5):
        i = bisect.bisect_right(qt, t) - 1
        if i < 0:
            continue
        bid, ask = q[i][1], q[i][2]
        mid = (bid + ask) / 2
        lo, hi = (mid, mid) if lo is None else (min(lo, mid), max(hi, mid))
        if (t - int(opened)) % 15 or t - opened < 180 or hi <= lo or ask - bid > 0.03:
            continue
        for side in ("yes", "no"):
            a = ask if side == "yes" else 1 - bid
            if not (0.57 <= a <= 0.82):
                continue
            pos = (mid - lo) / (hi - lo) if side == "yes" else (hi - mid) / (hi - lo)
            won = w["result"] == side
            net = (1.0 if won else 0.0) / (a + fee(a)) - 1.0
            name = next(n for x, y, n in POS if x <= pos < y)
            for c in ((cell[name],) if pos < 0.995 else (cell[name], top["0.57 to 0.66" if a < 0.66 else "0.66 to 0.74" if a < 0.74 else "0.74 to 0.82"])):
                c[0] += 1; c[1] += won; c[2] += a; c[3] += net; c[4].add(w["ticker"])


def line(name, c):
    print(f"  {name:16s} | {c[0]:6d} ({len(c[4]):4d} markets) | won {c[1] / c[0]:4.0%} | ask {c[2] / c[0]:.3f} | held net per $ {c[3] / c[0] * 100:+6.1f}%" if c[0] else f"  {name:16s} | 0")


print(f"window: 2026-11-09 00:00 ET on; {len(wins)} settled markets. Sides in the live band, by position in their own range since the open")
for _, _, name in POS:
    line(name, cell[name])
print("\nat the very top, by the ask")
for name in ("0.57 to 0.66", "0.66 to 0.74", "0.74 to 0.82"):
    line(name, top[name])
print("DONE")
