"""How well does Kalshi's own price say the chance, by price and by time left? (2026-11-16, user: "this has to do with time and pricing, and our curve logic is applicable as well as the ladder. it is all related, but we haven't figured out the right structure")

The base table under every buy rule, from Kalshi's quotes only: every 15 seconds in every settled market of the live crypto lane's series since Monday 2026-11-09, each side's mid (spread 0.03 or under), by the price and by the minutes left. For each cell: how often the side won less its mid (the calibration gap, in points: + = the side wins more often than its price says), and what buying it at the ask returned held to settlement, fee in. No path reading, no coin. Cells fixed before the run. Both sides of a moment are counted, so the table is its own mirror; it is printed whole so each price can be read directly.

Run on the host: scripts/cloud.sh run scripts/price_time_calibration.py
"""
import bisect
import datetime
from collections import defaultdict

from kalshi_moonshot import config, db
from kalshi_moonshot.lanes import lane_specs
from kalshi_moonshot.strategy import fee_per_contract as fee

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 9, 0, 0, tzinfo=ET).timestamp()
STEP = 15
PRICE = ((0.03, 0.10), (0.10, 0.20), (0.20, 0.30), (0.30, 0.40), (0.40, 0.50), (0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 0.90), (0.90, 0.97))
LEFT = ((720, 900, "12-15"), (540, 720, "9-12"), (360, 540, "6-9"), (180, 360, "3-6"), (60, 180, "1-3"), (15, 60, "0-1"))
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
series = set(lane_specs(s)["crypto_multiscale"].series)
wins = [w for w in st.execute("SELECT ticker, series, close_ts, result FROM windows WHERE close_ts>? AND result IN ('yes','no') ORDER BY close_ts", (SINCE,)).fetchall() if w["series"] in series]
cell = defaultdict(lambda: [0, 0, 0.0, 0.0])  # n, won, mid sum, net sum
seen = 0
for w in wins:
    close = float(w["close_ts"])
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], close - 920.0, close)).fetchall()
    q = [(float(x[0]), float(x[1] or 0.0), float(x[2] or 0.0)) for x in q if x[2] and float(x[2]) > 0 and not (float(x[1] or 0.0) <= 0 and float(x[2]) >= 1)]
    if len(q) < 50:
        continue
    seen += 1
    qt = [x[0] for x in q]
    for t in range(int(close - 900), int(close) - 15, STEP):
        i = bisect.bisect_right(qt, t) - 1
        if i < 0 or q[i][2] - q[i][1] > 0.03:
            continue
        left = close - t
        lk = next((name for a, b, name in LEFT if a <= left <= b), None)
        for side in ("yes", "no"):
            bid, ask = (q[i][1], q[i][2]) if side == "yes" else (1 - q[i][2], 1 - q[i][1])
            mid = (bid + ask) / 2
            pk = next((k for k, (a, b) in enumerate(PRICE) if a <= mid < b), None)
            if pk is None or lk is None or not (0.01 < ask < 0.99):
                continue
            won = w["result"] == side
            c = cell[(pk, lk)]
            c[0] += 1; c[1] += won; c[2] += mid; c[3] += (1.0 if won else 0.0) / (ask + fee(ask)) - 1.0
print(f"window: 2026-11-09 00:00 ET on; {len(wins)} settled markets, {seen} with quotes. Each cell: calibration gap in points (won less mid) / net per $ at the ask")
print("mid price    | " + " | ".join(f"{name:>5s} min left " for _, _, name in LEFT))
for k, (a, b) in enumerate(PRICE):
    row = []
    for _, _, name in LEFT:
        c = cell[(k, name)]
        row.append(f"{(c[1] / c[0] - c[2] / c[0]) * 100:+5.1f} / {c[3] / c[0] * 100:+6.1f}%" if c[0] >= 200 else "       n/a     ")
    print(f"{a:.2f} to {b:.2f} | " + " | ".join(row))
print("moments per cell:")
for k, (a, b) in enumerate(PRICE):
    print(f"{a:.2f} to {b:.2f} | " + " | ".join(f"{cell[(k, name)][0]:15d}" for _, _, name in LEFT))
print("DONE")
