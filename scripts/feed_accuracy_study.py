"""Which feed predicts the settlement best? (2026-11-13, user: "yes", after the lead-lag study: "our external sources might be more accurate, or the perpetuals feed might be what drives Kalshi's predictions").
Question: on every settled 15-minute market since Monday 11-09 00:00 ET, at 600, 420, 300, 180, 120 and 60 seconds before the close, how good is each source's chance that YES pays?
  Kalshi: the mid of the quote (the price cushion).
  Each feed (BQ BiQuote spot, KP Kalshi perp, FH futures for commodities; RTI settlement-style index and KP for crypto): Phi(z), z = the index's log distance from its own value at the window open, over its typical movement for the time left (rms of its 60 s moves over the last 900 s, divided by sqrt(60), times sqrt(seconds left)). Every feed is read against its own open value, so a feed that sits above or below the others does not matter.
  blend: the plain average of the Kalshi mid and the group's main feed (BQ for commodities, RTI for crypto), to see whether the feed adds anything to Kalshi's own price.
The outcome is read from the last quote of the market (mid over 0.9 = YES, under 0.1 = NO; others skipped). Only markets where every source has a reading at that time are scored, so all sources are scored on the same markets. Scores: Brier (lower is better) and log loss; also the share of markets where the source was the closest to the outcome.
First look, no bets involved.
    scripts/cloud_job.sh scripts/feed_accuracy_study.py
"""

import calendar
import datetime
import math
import os
import re
from zoneinfo import ZoneInfo

from kalshi_moonshot import config, db

os.nice(10)
s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
ET = ZoneInfo("America/New_York")
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
SINCE = calendar.timegm((2026, 11, 9, 5, 0, 0))
CRYPTO = {"BTC": ("BRTI", "KP_BTC"), "ETH": ("ETHUSD_RTI", "KP_ETH"), "SOL": ("SOLUSD_RTI", "KP_SOL"), "XRP": ("XRPUSD_RTI", "KP_XRP"), "DOGE": ("DOGEUSD_RTI", "KP_DOGE")}
COMM = ("BRENT", "WTI", "GOLD", "SILVER", "NATGAS", "COPPER", "PLATINUM", "PALLADIUM")
LEFTS = (600, 420, 300, 180, 120, 60)


def indexes_for(series):
    base = re.sub(r"15M$", "", series.removeprefix("KX"))
    if base in CRYPTO:
        return "crypto", {"RTI": CRYPTO[base][0], "KP": CRYPTO[base][1]}, "RTI"
    if base in COMM:
        return "commodity", {"BQ": f"BQ_{base}", "KP": f"KP_{base}", "FH": f"FH_{base}"}, "BQ"
    return None, {}, None


def phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def grid(rows, a, b):
    ts = [float(r[0]) for r in rows]
    out, last, j = [], None, 0
    for t in range(a, b):
        while j < len(ts) and ts[j] <= t:
            last = (float(rows[j][1]), float(rows[j][0]))
            j += 1
        out.append(last)
    return out


def clamp(p):
    return min(max(p, 0.001), 0.999)


scores = {}  # (group, left, source) -> list of (p, y)
nm = {}
tickers = [r[0] for r in tape.execute("SELECT DISTINCT ticker FROM quotes WHERE ts>? AND ticker LIKE 'KX%15M-%'", (SINCE,)).fetchall()]
print(f"markets with quotes since Monday: {len(tickers)}")
for ticker in tickers:
    group, idx, main = indexes_for(ticker.split("-")[0])
    m = re.match(r"^[A-Z0-9]+-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})", ticker)
    if not group or not m:
        continue
    try:
        close = int(datetime.datetime(2000 + int(m.group(1)), MONTHS.index(m.group(2)) + 1, int(m.group(3)), int(m.group(4)), int(m.group(5)), tzinfo=ET).timestamp())
    except ValueError:
        continue
    open_ = close - 900
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (ticker, open_ - 5, close + 30)).fetchall()
    if len(q) < 30:
        continue
    final = [(float(r[1]) + float(r[2])) / 2.0 for r in q if float(r[0]) >= close - 20]
    if not final:
        continue
    y = 1.0 if final[-1] > 0.9 else 0.0 if final[-1] < 0.1 else None
    if y is None:
        continue
    qm = grid([(r[0], (float(r[1]) + float(r[2])) / 2.0) for r in q], open_ - 5, close + 1)
    a = open_ - 1000
    gs = {}
    for kind, ix in idx.items():
        rows = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts", (ix, a - 30, close)).fetchall()
        if len(rows) < 300:
            break
        gs[kind] = grid(rows, a, close + 1)
    if len(gs) != len(idx):
        continue
    for left in LEFTS:
        t = close - left
        preds = {}
        km = qm[t - (open_ - 5)]
        if km is None or t - km[1] > 20:
            continue
        preds["Kalshi"] = clamp(km[0])
        ok = True
        for kind, g in gs.items():
            now = g[t - a]
            op = g[open_ + 5 - a]
            if now is None or op is None or t - now[1] > 10 or abs(op[1] - open_) > 40:
                ok = False
                break
            lg = [math.log(x[0]) if x else None for x in g[t - a - 960 : t - a + 1]]
            if len(lg) < 961 or any(v is None for v in lg[0::30]):
                ok = False
                break
            inc = [(lg[k] - lg[k - 60]) ** 2 / 60.0 for k in range(60, 961) if lg[k] is not None and lg[k - 60] is not None]
            if len(inc) < 600:
                ok = False
                break
            rate = math.sqrt(sum(inc[-900:]) / len(inc[-900:]))
            if rate <= 0:
                ok = False
                break
            z = (math.log(now[0]) - math.log(op[0])) / (rate * math.sqrt(left))
            preds[kind] = clamp(phi(z))
        if not ok:
            continue
        preds["blend"] = clamp(0.5 * preds["Kalshi"] + 0.5 * preds[main])
        nm[(group, left)] = nm.get((group, left), 0) + 1
        for src, p in preds.items():
            scores.setdefault((group, left, src), []).append((p, y))
for group in ("commodity", "crypto"):
    print(f"\n=== {group}")
    for left in LEFTS:
        n = nm.get((group, left), 0)
        if n < 30:
            continue
        srcs = sorted({k[2] for k in scores if k[0] == group and k[1] == left})
        line = []
        for src in srcs:
            v = scores[(group, left, src)]
            br = sum((p - y) ** 2 for p, y in v) / len(v)
            ll = -sum(math.log(p if y else 1 - p) for p, y in v) / len(v)
            line.append(f"{src} {br:.4f}/{ll:.3f}")
        print(f"  {left:4d}s left, n={n}: Brier/logloss  " + " | ".join(line))
print("DONE")
