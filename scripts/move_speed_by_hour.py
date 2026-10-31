"""Do the crypto 15-minute curves move faster in the US day than at night? Read-only.

Per BTC/ETH/SOL window the largest YES-mid move over 3 s and over 10 s (last 20 s of the window left out), grouped by the New York hour the window closed in. Also the share of windows with a 10 s move of 20c or more.
    docker compose -p moonshot-local --env-file .env.local exec -T -e DAYS=8 bot python - < scripts/move_speed_by_hour.py
"""
import bisect
import os
import re
import statistics
import time
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
DAYS = float(os.environ.get("DAYS", "8"))
NY = ZoneInfo("America/New_York")
BUCKETS = [(0, 6, "12am-6am"), (6, 9, "6am-9am"), (9, 12, "9am-12pm"), (12, 17, "12pm-5pm"), (17, 23, "5pm-11pm"), (23, 24, "11pm-12am")]
wins = main.db.execute("SELECT ticker, close_ts FROM windows WHERE result IN ('yes','no') AND close_ts > ? ORDER BY close_ts", (time.time() - DAYS * 86400,)).fetchall()
by = defaultdict(list)
for w in wins:
    m = re.match(r"^KX(BTC|ETH|SOL)15M-", w["ticker"])
    if not m:
        continue
    close = float(w["close_ts"])
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], close - 900, close - 20)).fetchall()
    if len(q) < 30:
        continue
    ts = [float(r[0]) for r in q]
    mid = {}
    for sec in range(int(close - 900), int(close - 20)):
        i = bisect.bisect_right(ts, sec + 0.999) - 1
        if i >= 0 and sec + 0.999 - ts[i] <= 15:
            mid[sec] = (float(q[i][1]) + float(q[i][2])) / 2
    secs = sorted(mid)
    m3 = max((abs(mid[t] - mid[t - 3]) for t in secs if t - 3 in mid), default=0.0)
    m10 = max((abs(mid[t] - mid[t - 10]) for t in secs if t - 10 in mid), default=0.0)
    hour = datetime.fromtimestamp(close, NY).hour
    by[next(b[2] for b in BUCKETS if b[0] <= hour < b[1])].append((m3, m10, datetime.fromtimestamp(close, NY).weekday() >= 5))
print(f"{'New York time':<11} {'windows':>7} | {'median 3s':>9} {'median 10s':>10} | {'10s >= 20c':>10} | weekend share")
for lo, hi, name in BUCKETS:
    rs = by.get(name, [])
    if rs:
        print(f"{name:<11} {len(rs):>7} | {statistics.median(r[0] for r in rs) * 100:>8.0f}c {statistics.median(r[1] for r in rs) * 100:>9.0f}c | {100 * sum(1 for r in rs if r[1] >= 0.20) / len(rs):>9.0f}% | {100 * sum(1 for r in rs if r[2]) / len(rs):.0f}%")
for label, wk in (("weekday", False), ("weekend", True)):
    rs = [r for v in by.values() for r in v if r[2] == wk]
    if rs:
        print(f"{label:<11} {len(rs):>7} | {statistics.median(r[0] for r in rs) * 100:>8.0f}c {statistics.median(r[1] for r in rs) * 100:>9.0f}c | {100 * sum(1 for r in rs if r[1] >= 0.20) / len(rs):>9.0f}%")
print("DONE")
