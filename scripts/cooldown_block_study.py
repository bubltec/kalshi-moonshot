"""How much of each 15-minute market is the live lane's review switched off by the cooldown, and by whose decision?

runner.review_lane drops a market "decided on within the lane's cooldown": store.decided_since(ticker, now - minutes),
minutes = min(cooldown_minutes, max(0.5, minutes left / 5)). decided_since reads every lane's decisions on the ticker,
so a paper lane's reading of a market also cools it for the live lane. This counts, second by second over each market
the lane decided on, whether the lane was cooling, and whether its own decision or only another lane's was the cause.
It also lists the lane's buys that came within a few seconds of a cooling period ending (a buy the cooldown may have
delayed). It cannot say what the lane would have decided in a skipped second: those readings were never taken.

Run on the host: LANE=crypto_multiscale python scripts/cooldown_block_study.py
"""
import bisect
import datetime
import os
from collections import defaultdict

from kalshi_moonshot import config, db

LANE = os.environ.get("LANE", "crypto_multiscale")
COOL = float(os.environ.get("COOLDOWN_MINUTES", "3.0"))
SINCE = datetime.datetime(2026, 11, 9, 5, 0, tzinfo=datetime.UTC).timestamp()  # Monday 2026-11-09 00:00 ET: the logic was rewritten then
WINDOW = 900.0

s = config.load()
st = db.connect(s.db_path, "main", shared=True)
rows = st.execute(
    "SELECT ticker, ts, lane, action, close_time, stake_usd FROM decisions WHERE ts>=? AND ticker LIKE ? ORDER BY ts",
    (SINCE, "%15M-%"),
).fetchall()
by: dict[str, list] = defaultdict(list)
for ticker, ts, lane, action, close_time, stake in rows:
    try:
        close = datetime.datetime.fromisoformat(str(close_time).replace("Z", "+00:00")).timestamp()
    except ValueError:
        continue
    by[ticker].append((float(ts), lane, action, close, float(stake or 0.0)))


def cap(left: float) -> float:
    """The cooldown in seconds with `left` seconds to the close, as runner.review_lane computes it."""
    return 60.0 * min(COOL, max(0.5, left / 60.0 / 5.0))


tot = defaultdict(float)  # by third of the market: seconds free, cooled by own decision, cooled by others only
markets = 0
buys = []
lanes_seen = defaultdict(int)
for ticker, ds in by.items():
    own = [d[0] for d in ds if d[1] == LANE]
    if not own:
        continue
    markets += 1
    close = ds[0][3]
    other = [d[0] for d in ds if d[1] != LANE]
    for d in ds:
        if d[1] != LANE:
            lanes_seen[d[1]] += 1
    start = close - WINDOW
    for k in range(int(WINDOW)):
        t = start + k + 0.5
        c = cap(close - t)
        i = bisect.bisect_right(own, t)
        mine = i > 0 and own[i - 1] >= t - c
        j = bisect.bisect_right(other, t)
        theirs = j > 0 and other[j - 1] >= t - c
        third = min(2, int(k // (WINDOW / 3)))
        tot[(third, "own" if mine else "others" if theirs else "free")] += 1
    allts = sorted(d[0] for d in ds)
    for ts, lane, action, _, stake in ds:
        if lane != LANE or action != "bet" or stake <= 0:
            continue
        i = bisect.bisect_left(allts, ts)
        prev = allts[i - 1] if i > 0 else None
        if prev is None:
            buys.append((ticker, ts, None, None, None))
            continue
        c = cap(close - ts)
        prev_lane = next(d[1] for d in ds if d[0] == prev)
        buys.append((ticker, ts, ts - prev, c, prev_lane))

print(f"lane {LANE}, decisions since Mon 2026-11-09 00:00 ET, 15-minute markets the lane decided on: {markets}")
print("other lanes deciding on the same markets:", dict(sorted(lanes_seen.items(), key=lambda kv: -kv[1])[:8]))
names = ("first 5 min", "middle 5 min", "last 5 min")
for third in range(3):
    n = sum(tot[(third, k)] for k in ("own", "others", "free")) or 1.0
    print(f"  {names[third]:12s}: free {tot[(third,'free')]/n:5.1%}   cooling on its own decision {tot[(third,'own')]/n:5.1%}   cooling ONLY because another lane decided {tot[(third,'others')]/n:5.1%}")
n = sum(tot.values()) or 1.0
print(f"  {'whole market':12s}: free {sum(v for (t,k),v in tot.items() if k=='free')/n:5.1%}   own {sum(v for (t,k),v in tot.items() if k=='own')/n:5.1%}   others only {sum(v for (t,k),v in tot.items() if k=='others')/n:5.1%}")
with_prev = [b for b in buys if b[2] is not None]
edge = [b for b in with_prev if b[2] >= b[3] - 1.0 and b[2] <= b[3] + 6.0]
inside = [b for b in with_prev if b[2] < b[3] - 1.0]
print(f"buys: {len(buys)}; with an earlier decision on the market: {len(with_prev)}")
print(f"  bought within 6 s of a cooling period ending (the cooldown may have delayed the buy): {len(edge)}, of which the earlier decision was another lane's: {sum(1 for b in edge if b[4] != LANE)}")
print(f"  bought INSIDE a cooling period (the cooldown did not apply to that buy path): {len(inside)}")
for b in edge[:12]:
    print("   ", b[0], datetime.datetime.fromtimestamp(b[1], datetime.UTC).strftime("%m-%d %H:%M:%S UTC"), f"gap {b[2]:.0f}s cap {b[3]:.0f}s prev lane {b[4]}")
print("DONE")
