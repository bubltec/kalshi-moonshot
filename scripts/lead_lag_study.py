"""Who moves first: Kalshi's price or the spot feeds? (2026-11-13, user, from hand trading: "the kalshi price tends to lead the spot values in the app, but our external sources might be more accurate, or the perpetuals feed might be what drives Kalshi's predictions").
Question: when a 15-minute market's mid price jumps (15 cents or more within 5 s, minutes 3-14 of the window), what had each index done before, during and after the jump, in the direction of the jump?
  Indexes per market: BQ (BiQuote spot), KP (Kalshi perp reference), FH (futures feed) for commodities; the coin's own settlement-style index (RTI/BRTI) and KP for crypto. Each index's move in a window is standardized by that index's own typical one-second move (rms of 1-s log changes over the 10 minutes before the jump) times sqrt(window seconds), so the units are the index's own variance and no coin or commodity gets a rule.
  Windows around the jump start s (the second the mid was 5 s before it reached the 15-cent change): before = (s-20, s), during = (s, s+5), after = (s+5, s+20). Mean signed standardized move per window; if an index leads Kalshi it has moved before; if it lags it moves after.
  Also: per jump, which index first moved 1 standardized unit in the jump's direction (seconds from s, negative = before Kalshi's jump began).
Bets are not involved: this is market behavior from the tape since Monday 11-09 00:00 ET. First look.
    scripts/cloud_job.sh scripts/lead_lag_study.py
"""

import bisect
import datetime
import calendar
import math
import os
import re
import statistics

from zoneinfo import ZoneInfo

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store

os.nice(10)
s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
st = Store(s.db_path, s.exclude_versions)
ET = ZoneInfo("America/New_York")
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
SINCE = calendar.timegm((2026, 11, 9, 5, 0, 0))
CRYPTO = {"BTC": ("BRTI", "KP_BTC"), "ETH": ("ETHUSD_RTI", "KP_ETH"), "SOL": ("SOLUSD_RTI", "KP_SOL"), "XRP": ("XRPUSD_RTI", "KP_XRP"), "DOGE": ("DOGEUSD_RTI", "KP_DOGE")}
COMM = ("BRENT", "WTI", "GOLD", "SILVER", "NATGAS", "COPPER", "PLATINUM", "PALLADIUM")
JUMP, JUMP_SECONDS = 0.15, 5
BEFORE, AFTER = 20, 20


def indexes_for(series):
    base = re.sub(r"15M$", "", series.removeprefix("KX"))
    if base in CRYPTO:
        return "crypto", {"RTI": CRYPTO[base][0], "KP": CRYPTO[base][1]}
    if base in COMM:
        return "commodity", {"BQ": f"BQ_{base}", "KP": f"KP_{base}", "FH": f"FH_{base}"}
    return None, {}


def grid(rows, a, b):
    """Per-second last value from (ts, value) rows over [a, b); None before the first tick."""
    ts = [float(r[0]) for r in rows]
    out, last = [], None
    j = 0
    for t in range(a, b):
        while j < len(ts) and ts[j] <= t:
            last = float(rows[j][1])
            j += 1
        out.append(last)
    return out


def wins():
    return tape.execute("SELECT DISTINCT ticker FROM quotes WHERE ts>? AND ticker LIKE 'KX%15M-%'", (SINCE,)).fetchall()


stats = {}  # (group, index kind, window) -> [z]
first = {}  # (group, kind) -> [seconds relative to s of the first 1-unit move in the direction]
njump = {"crypto": 0, "commodity": 0}
tickers = [r[0] for r in wins()]
print(f"markets with quotes since Monday: {len(tickers)}")
for ticker in tickers:
    series = ticker.split("-")[0]
    group, idx = indexes_for(series)
    if not group:
        continue
    m = re.match(r"^[A-Z0-9]+-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})", ticker)
    if not m:
        continue
    try:
        close = datetime.datetime(2000 + int(m.group(1)), MONTHS.index(m.group(2)) + 1, int(m.group(3)), int(m.group(4)), int(m.group(5)), tzinfo=ET).timestamp()
    except ValueError:
        continue
    open_ = close - 900
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (ticker, open_ - 5, close)).fetchall()
    if len(q) < 60:
        continue
    a, b = int(open_) - 700, int(close) + 60
    mid = grid([(r[0], (float(r[1]) + float(r[2])) / 2.0) for r in q], int(open_), int(close))
    jumps, last_j = [], -999
    for k in range(180 + JUMP_SECONDS, 840):
        if mid[k] is None or mid[k - JUMP_SECONDS] is None:
            continue
        d = mid[k] - mid[k - JUMP_SECONDS]
        if abs(d) >= JUMP and k - last_j > 40:
            # the start of the move: the earliest second in the last 5 where the mid was still within 0.03 of its level 5 s before
            st = k - JUMP_SECONDS
            jumps.append((int(open_) + st, 1.0 if d > 0 else -1.0))
            last_j = k
    if not jumps:
        continue
    series_rows = {}
    for kind, ix in idx.items():
        rows = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts", (ix, a - 5, b)).fetchall()
        if len(rows) > 300:
            g = grid(rows, a, b)
            series_rows[kind] = (g, a)
    for sj, dirn in jumps:
        njump[group] += 1
        for kind, (g, base) in series_rows.items():
            k0 = sj - base
            if k0 < 620 or k0 + AFTER + 1 >= len(g) or any(x is None for x in g[k0 - 600 : k0 + AFTER + 1]):
                continue
            lg = [math.log(x) for x in g[k0 - 600 : k0 + AFTER + 1]]
            ch = [lg[i] - lg[i - 1] for i in range(1, 601)]
            sd = math.sqrt(sum(c * c for c in ch) / len(ch))
            if sd <= 0:
                continue
            base_i = 600  # index of second k0 in lg

            def z(x, y):
                return dirn * (lg[base_i + y] - lg[base_i + x]) / (sd * math.sqrt(y - x))

            for name, (x, y) in (("before", (-BEFORE, 0)), ("during", (0, JUMP_SECONDS)), ("after", (JUMP_SECONDS, AFTER))):
                stats.setdefault((group, kind, name), []).append(z(x, y))
            hit = None
            for off in range(-BEFORE, AFTER + 1):
                if dirn * (lg[base_i + off] - lg[base_i - BEFORE]) / (sd * math.sqrt(max(off + BEFORE, 1))) >= 1.0 and off + BEFORE >= 3:
                    hit = off
                    break
            if hit is not None:
                first.setdefault((group, kind), []).append(hit)
print(f"jumps found (15 cents in 5 s, minutes 3-14): crypto {njump['crypto']}, commodity {njump['commodity']}")
for group in ("commodity", "crypto"):
    print(f"\n=== {group}")
    for kind in sorted({k for (g, k, _) in stats if g == group}):
        cells = []
        for name in ("before", "during", "after"):
            zs = stats.get((group, kind, name), [])
            cells.append(f"{name} {statistics.fmean(zs):+.2f} (se {statistics.pstdev(zs) / math.sqrt(len(zs)):.2f}, n {len(zs)})" if len(zs) > 3 else f"{name} n/a")
        f = first.get((group, kind), [])
        fs = f"first 1-unit move in the jump's direction: median {statistics.median(f):+.0f}s from the jump's start (n {len(f)}, before it {sum(1 for x in f if x < 0)}, after it {sum(1 for x in f if x >= 0)})" if len(f) > 3 else "first move n/a"
        print(f"  {kind:4s}: " + " | ".join(cells) + f"\n        {fs}")
print("DONE")
