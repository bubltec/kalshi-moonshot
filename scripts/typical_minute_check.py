"""Is quoteshock.typical_minute (sqrt(60) x the rms one-second change of the mid) the size of a real one-minute move? (2026-11-15, user: "double check the code for invalid equations")

The take's late cap, the slope tests, the cushion's flat hold and the cap-free squeeze all read a market's "typical minute" as sqrt(60) times the root-mean-square one-second change of its quote mid. That is a minute's move only if one-second changes are independent. Quotes jump in whole cents and bounce between bid and ask, so one-second changes can be negatively correlated and the scaled figure over-states a real minute; a trending quote does the opposite. This compares, on the same tape, the typical minute with the standard deviation of REAL one-minute changes of the same mid (non-overlapping, over the same 15 minutes) for 15-minute crypto markets of the last 12 hours: per market the ratio typical minute / real minute, and the median, quartiles and extremes. Ratio 1 = the scaling is right; over 1 = the typical minute is too big, so every bar built on it is too loose.

Run on the host: python scripts/typical_minute_check.py
"""
import bisect
import math
import re
import statistics
import time

from kalshi_moonshot import config, db, quoteshock

s = config.load()
tape = db.connect(s.tape_path, "tape", shared=True)
now = time.time()
tickers = [r[0] for r in tape.execute("SELECT DISTINCT ticker FROM quotes WHERE ts>? AND ticker LIKE '%15M-26NOV%'", (now - 12 * 3600,)).fetchall()]
ratios = []
for tk in tickers:
    m = re.match(r"KX[A-Z]+15M-26NOV(\d\d)(\d\d)(\d\d)-", tk)
    if not m:
        continue
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? ORDER BY ts", (tk,)).fetchall()
    if len(q) < 400:
        continue
    rows = [(float(r[0]), (float(r[1]) + float(r[2])) / 2.0) for r in q]
    ts = [r[0] for r in rows]
    for frac in (0.55, 0.75, 0.95):  # three reading times inside the market's life
        t_end = ts[0] + (ts[-1] - ts[0]) * frac
        part = [r for r in rows if r[0] <= t_end]
        unit = quoteshock.typical_minute(part, t_end, 600.0)
        if unit is None:
            continue
        marks = []
        for k in range(11):
            i = bisect.bisect_right(ts, t_end - 60.0 * (10 - k)) - 1
            if i < 0:
                break
            marks.append(rows[i][1])
        if len(marks) < 11:
            continue
        ch = [marks[k + 1] - marks[k] for k in range(10)]
        real = math.sqrt(sum(c * c for c in ch) / len(ch))
        if real > 1e-6:
            ratios.append(unit / real)
ratios.sort()
n = len(ratios)
print(f"readings {n} from {len(tickers)} markets (10 one-minute moves each)")
if n:
    print(f"typical minute / real one-minute rms: median {statistics.median(ratios):.2f}, quartiles {ratios[n // 4]:.2f} to {ratios[3 * n // 4]:.2f}, 5th {ratios[n // 20]:.2f}, 95th {ratios[19 * n // 20]:.2f}, share over 1.25: {sum(1 for r in ratios if r > 1.25) / n:.0%}, share under 0.8: {sum(1 for r in ratios if r < 0.8) / n:.0%}")
print("DONE")
