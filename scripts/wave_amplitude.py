"""How big are the waves inside a 15-minute crypto market? (2026-11-13, user: "most markets there is a wave in the 15 mins. There are always ups and downs"; the idea: take a profit on the way up and buy again on the way down.)
Question: in the five coin markets the ladder lane trades, from minute 5 to minute 14 of each window, how often does the contract's own price (the YES mid) swing, and by how much, measured against what a round trip costs?
  A swing is a move of at least h between a high and the next low or the reverse (the standard zigzag), counted at h = 5, 8, 12 and 20 cents. Per market: swings, and the range (high minus low) of the mid.
  Reported for all markets and for the markets the lane could buy at minute 5 (either side priced 0.60 to 0.90).
  Round trip cost: the spread (ask - bid, median) plus the fee on each leg (0.07 x p x (1 - p), per contract), in cents, at three prices.
This describes the price paths only. It does not test a sell rule or say whether a rebuy would win.
Markets since Monday 11-09 00:00 ET, from the tape.
    scripts/cloud_job.sh scripts/wave_amplitude.py
"""

import calendar
import datetime
import os
import re
import statistics
from zoneinfo import ZoneInfo

from kalshi_moonshot import config, db

os.nice(10)
s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
ET = ZoneInfo("America/New_York")
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
SINCE = calendar.timegm((2026, 11, 9, 5, 0, 0))
THRESHOLDS = (0.05, 0.08, 0.12, 0.20)
COINS = ("BTC", "ETH", "SOL", "XRP", "DOGE")


def zigzag(series, h):
    """Number of swings of at least h in a list of values."""
    if not series:
        return 0
    swings, hi, lo, direction = 0, series[0], series[0], 0
    for v in series[1:]:
        if direction >= 0:
            if v > hi:
                hi = v
            if direction == 0 and hi - lo >= h:
                direction, swings, lo = 1, swings + 1, hi
            elif direction == 1 and hi - v >= h:
                direction, swings, lo = -1, swings + 1, v
        if direction <= 0:
            if v < lo:
                lo = v
            if direction == 0 and hi - lo >= h:
                direction, swings, hi = -1, swings + 1, lo
            elif direction == -1 and v - lo >= h:
                direction, swings, hi = 1, swings + 1, v
        if direction == 1 and v > hi:
            hi = v
        if direction == -1 and v < lo:
            lo = v
    return swings


rows_all, rows_band, spreads = [], [], []
tickers = [r[0] for r in tape.execute("SELECT DISTINCT ticker FROM quotes WHERE ts>? AND ticker ~ ?", (SINCE, "^KX(BTC|ETH|SOL|XRP|DOGE)15M-")).fetchall()]
print(f"crypto markets with quotes since Monday: {len(tickers)}")
for ticker in tickers:
    m = re.match(r"^[A-Z0-9]+-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})", ticker)
    if not m:
        continue
    try:
        close = int(datetime.datetime(2000 + int(m.group(1)), MONTHS.index(m.group(2)) + 1, int(m.group(3)), int(m.group(4)), int(m.group(5)), tzinfo=ET).timestamp())
    except ValueError:
        continue
    open_ = close - 900
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (ticker, open_ + 300, open_ + 840)).fetchall()
    if len(q) < 120:
        continue
    mids, j, last = [], 0, None
    for t in range(open_ + 300, open_ + 840):
        while j < len(q) and q[j][0] <= t:
            last = (float(q[j][1]) + float(q[j][2])) / 2.0
            spreads.append(float(q[j][2]) - float(q[j][1]))
            j += 1
        if last is not None:
            mids.append(last)
    if len(mids) < 300:
        continue
    row = {"range": max(mids) - min(mids), **{h: zigzag(mids, h) for h in THRESHOLDS}}
    rows_all.append(row)
    if 0.60 <= mids[0] <= 0.90 or 0.60 <= 1 - mids[0] <= 0.90:
        rows_band.append(row)


def report(label, rows):
    n = len(rows)
    print(f"\n=== {label}: {n} markets")
    if not n:
        return
    print(f"  range of the mid (high - low), minutes 5-14: median {statistics.median(r['range'] for r in rows) * 100:.0f}c, quartiles {sorted(r['range'] for r in rows)[n // 4] * 100:.0f}c / {sorted(r['range'] for r in rows)[3 * n // 4] * 100:.0f}c")
    for h in THRESHOLDS:
        c = [r[h] for r in rows]
        print(f"  swings of {h * 100:.0f}c or more: median {statistics.median(c):.0f} a market | at least 1 in {100 * sum(1 for x in c if x >= 1) / n:.0f}% | at least 2 in {100 * sum(1 for x in c if x >= 2) / n:.0f}% | at least 4 in {100 * sum(1 for x in c if x >= 4) / n:.0f}%")


report("all five coins", rows_all)
report("markets the lane could buy at minute 5 (a side priced 0.60-0.90)", rows_band)
print(f"\nspread (ask - bid) over those quotes: median {statistics.median(spreads) * 100:.1f}c, 90th percentile {sorted(spreads)[int(len(spreads) * 0.9)] * 100:.1f}c")
for p in (0.60, 0.75, 0.90):
    fee = 0.07 * p * (1 - p)
    print(f"round trip at {p:.2f}: spread 1.0c + fee in and out {2 * fee * 100:.1f}c = about {(0.010 + 2 * fee) * 100:.1f}c")
print("DONE")
