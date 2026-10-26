"""Does a wave of one-sided trading in a market warn of a crash? Kalshi's own trade flow (the tape's per-second contract counts) against the favorite. Replayed from the tape. Places nothing.

For each finished 15-minute window and each side priced 0.80-0.97 (bid) in minutes 8-14, every second: the adverse flow of the last 3 seconds = contracts traded against the side minus with it (NO minus YES contracts for a YES holder), and the trade count per second.
A trigger fires the first second the adverse flow reaches a bar (contracts per 3 seconds). At that second the question is: holding is worth the result (1 or 0); selling takes the bid minus the sale fee. Reported: how many windows fire, how often the side then lost,
and holding minus selling (cents per contract; negative = selling at the trigger was better), against the same measure at a random second of the same kind of window. Standard errors across windows.
    docker compose exec -T bot python - < scripts/flow_crash_study.py
"""
import bisect
import math
import random
import statistics

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M")
BARS = (3000, 6000, 12000, 25000)
random.seed(1)
rows = []  # (window, bar -> (hold_minus_sell, lost) at its first trigger), plus baseline samples
for m in main.db.execute(f"SELECT ticker, result, close_ts FROM windows WHERE series IN ({','.join('?' * len(SERIES))}) AND result IN ('yes','no')", SERIES).fetchall():
    close = m["close_ts"]
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts>? AND ts<=? AND bid > 0 AND ask > 0 ORDER BY ts", (m["ticker"], close - 420, close)).fetchall()
    fl = tape.execute("SELECT ts, yes_vol, no_vol, n FROM flow_1s WHERE ticker=? AND ts>? AND ts<=? ORDER BY ts", (m["ticker"], int(close - 425), int(close))).fetchall()
    if len(q) < 30 or len(fl) < 30:
        continue
    qts = [r[0] for r in q]
    fby = {int(r[0]): (r[1], r[2], r[3]) for r in fl}
    for side in ("yes", "no"):
        win = 1.0 if m["result"] == side else 0.0
        trig = {b: None for b in BARS}
        pool = []
        for sec in range(420, 0, -1):  # seconds before the close; minutes 8-14 of a 15-minute window are the last 420 s
            t = close - sec
            i = bisect.bisect_right(qts, t) - 1
            if i < 0:
                continue
            bid = q[i][1] if side == "yes" else 1 - q[i][2]
            if not (0.80 <= bid <= 0.97):
                continue
            adv = 0.0
            for k in range(3):
                f = fby.get(int(t) - k)
                if f:
                    adv += (f[1] - f[0]) if side == "yes" else (f[0] - f[1])
            hs = win - (bid - fee(bid))
            pool.append(hs)
            for b in BARS:
                if trig[b] is None and adv >= b:
                    trig[b] = (hs, 1.0 - win)
        if pool:
            rows.append((m["ticker"], trig, statistics.mean(pool), random.choice(pool)))
print(f"{len(rows)} window-sides had a 0.80-0.97 bid in the last 7 minutes")
base = statistics.mean(r[2] for r in rows)
print(f"holding minus selling at a random second of such a window: {100 * base:+.2f} c per contract on average (random draw {100 * statistics.mean(r[3] for r in rows):+.2f})")
for b in BARS:
    g = [(r[0], r[1][b]) for r in rows if r[1][b] is not None]
    if len(g) < 30:
        print(f"  adverse flow >= {b:6} per 3 s: only {len(g)} windows fire")
        continue
    hs = [x[1][0] for x in g]
    per = {}
    for t_, (h, _) in g:
        per.setdefault(t_, []).append(h)
    mm = [sum(v) / len(v) for v in per.values()]
    se = 100 * math.sqrt(sum((x - sum(mm) / len(mm)) ** 2 for x in mm) / (len(mm) - 1) / len(mm)) if len(mm) > 2 else float("nan")
    print(f"  adverse flow >= {b:6} per 3 s: {len(g):4} window-sides fire; the side then lost {100 * statistics.mean(x[1][1] for x in g):4.0f}%; holding minus selling {100 * statistics.mean(hs):+6.2f}c +-{se:.2f} per contract (negative = selling at the trigger was better)")
