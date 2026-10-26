"""Kalshi's price alone: when a market's YES price has traced this shape, does it win more or less often than the price says? No coin data at all. Replayed from the tape. Places nothing.

For each finished 15-minute crypto window the YES mid is read at the end of every minute (the last quote at or before it). At decision minute m the shape of that price path so far gives a class:
price level (under 0.30, 0.30-0.70, over 0.70) x which third of the path's own range the latest mid is in x how long ago the path's high was (now, 1-2 minutes, 3 or more) x whether the last 3 minutes rose or fell.
Per coin, a table learns for each class how many log-odds the result sits above or below the mid (shrunk toward zero when thin) on the earlier windows; it is scored on the later ones: log-loss of mid + the class shift against the mid alone.
    docker compose exec -T bot python - < scripts/kalshi_curve_study.py
"""
import bisect
import math
import statistics

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
COINS = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M")


def ll(p, y):
    p = min(1 - 1e-4, max(1e-4, p))
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def logit(p):
    p = min(1 - 1e-4, max(1e-4, p))
    return math.log(p / (1 - p))


def cls_of(path):
    hi, lo, last = max(path), min(path), path[-1]
    pos = (last - lo) / (hi - lo) if hi > lo else 0.5
    since = len(path) - 1 - path.index(hi)
    level = 0 if last < 0.30 else 1 if last <= 0.70 else 2
    return level * 18 + min(int(pos * 3), 2) * 6 + (0 if since == 0 else 1 if since <= 2 else 2) * 2 + int(last > path[-4])


print("coin | windows | minute | log-loss gain per 1000 (positive = the class table beats the mid), later 40% of the windows")
for series in COINS:
    wins = main.db.execute("SELECT ticker, result, close_ts FROM windows WHERE series=? AND result IN ('yes','no') ORDER BY close_ts", (series,)).fetchall()
    data = []
    for w in wins:
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 ORDER BY ts", (w["ticker"],)).fetchall()
        if len(q) < 60:
            continue
        qts = [r[0] for r in q]
        o = w["close_ts"] - 900
        mids = []
        for k in range(1, 14):
            i = bisect.bisect_right(qts, o + 60 * k) - 1
            mids.append((q[i][1] + q[i][2]) / 2 if i >= 0 and o + 60 * k - qts[i] < 60 else None)
        data.append((w["ticker"], 1.0 if w["result"] == "yes" else 0.0, mids))
    cut = int(len(data) * 0.6)
    for m in (5, 8, 10):
        tr, te = [], []
        for n, (tk, y, mids) in enumerate(data):
            path = mids[:m]
            if None in path or len(path) < 5:
                continue
            row = (cls_of(path), path[-1], y)
            (tr if n < cut else te).append(row)
        shift = {}
        for c in {r[0] for r in tr}:
            g = [r for r in tr if r[0] == c]
            if len(g) >= 20:
                d = 0.0
                for _ in range(15):
                    pp = [1 / (1 + math.exp(-(logit(r[1]) + d))) for r in g]
                    d += sum(r[2] - p for r, p in zip(g, pp, strict=True)) / max(sum(p * (1 - p) for p in pp), 1e-9)
                shift[c] = d * len(g) / (len(g) + 100)
        if len(te) < 100:
            continue
        base = statistics.mean(ll(r[1], r[2]) for r in te)
        adj = statistics.mean(ll(1 / (1 + math.exp(-(logit(r[1]) + shift.get(r[0], 0.0)))), r[2]) for r in te)
        print(f"{series[2:5]:5} | {len(data):4} | {m:2} | {1000 * (base - adj):+6.1f}  ({len(te)} test windows, {len(shift)} classes with 20+ training windows)")
