"""Is there a price and a time in the window where buying pays? A scan of every price band and minute of the 15-minute windows, fee-free, with the best cells checked on later windows. Places nothing.

Moments: every 30 s from minute 1 to 13, each side with a spread of 4c or less, bought at the ask and held to the result. Return = (win - ask) / ask, no fee (a resting order pays none).
Cells: market class (crypto, FX, commodity) x ask in 5c bands x minute in 2-minute bands. A cell's score is its mean return over its standard error across windows (only cells with 150+ windows).
Honest check: windows are split in time; the best cells of the EARLIER half (by score) are scored on the LATER half. If the scan finds only noise, the later half shows about zero (or worse, since the best of many noisy cells is picked).
    docker compose exec -T bot python - < scripts/time_price_scan.py
"""
import bisect
import math
import statistics
from collections import defaultdict

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
CLASS = {**dict.fromkeys(("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M"), "crypto"),
         **dict.fromkeys(("KXEURUSD15M", "KXGBPUSD15M", "KXUSDJPY15M", "KXAUDUSD15M", "KXUSDCAD15M"), "fx"),
         **dict.fromkeys(("KXGOLD15M", "KXSILVER15M", "KXWTI15M", "KXBRENT15M", "KXCOPPER15M", "KXNATGAS15M", "KXPLATINUM15M", "KXPALLADIUM15M"), "commodity")}
rows = []
for m in main.db.execute(f"SELECT ticker, series, result, close_ts FROM windows WHERE series IN ({','.join('?' * len(CLASS))}) AND result IN ('yes','no')", tuple(CLASS)).fetchall():
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 ORDER BY ts", (m["ticker"],)).fetchall()
    if len(q) < 60:
        continue
    qts = [r[0] for r in q]
    close = m["close_ts"]
    for side in ("yes", "no"):
        asks = [r[2] if side == "yes" else 1 - r[1] for r in q]
        bids = [r[1] if side == "yes" else 1 - r[2] for r in q]
        win = 1.0 if m["result"] == side else 0.0
        t = close - 900 + 60
        while t < close - 900 + 780:
            i = bisect.bisect_right(qts, t) - 1
            t += 30
            a = asks[i] if i >= 0 else 0
            if i < 0 or not (0.05 <= a <= 0.95) or a - bids[i] > 0.04:
                continue
            minute = (qts[i] - (close - 900)) / 60
            rows.append({"cl": m["ticker"], "t": close, "cell": (CLASS[m["series"]], int(a * 20) / 20, int(minute // 2) * 2), "ret": (win - a) / a})
print(f"{len(rows):,} moments in {len({r['cl'] for r in rows})} windows")
cut = sorted({r["t"] for r in rows})[len({r['t'] for r in rows}) // 2]
early, late = [r for r in rows if r["t"] <= cut], [r for r in rows if r["t"] > cut]


def cells(rs, min_windows=150):
    g = defaultdict(lambda: defaultdict(list))
    for r in rs:
        g[r["cell"]][r["cl"]].append(r["ret"])
    out = {}
    for cell, per in g.items():
        if len(per) < min_windows:
            continue
        mm = [sum(v) / len(v) for v in per.values()]
        se = math.sqrt(sum((x - sum(mm) / len(mm)) ** 2 for x in mm) / (len(mm) - 1) / len(mm))
        out[cell] = (statistics.mean(x for v in per.values() for x in v), se, len(per), sum(len(v) for v in per.values()))
    return out


allc = cells(rows)
print(f"{len(allc)} cells with enough windows; best by score over everything (these are the ones that would tempt us):")
for cell, (mean, se, nw, n) in sorted(allc.items(), key=lambda kv: -kv[1][0] / kv[1][1])[:10]:
    print(f"  {cell[0]:9} ask {cell[1]:.2f}-{cell[1] + 0.05:.2f} minute {cell[2]:2}-{cell[2] + 2:2}  return {mean:+6.1%} +-{se * 100:.1f}  ({nw} windows, {n} moments)  score {mean / se:+.1f}")
ec = cells(early)
best = sorted(ec.items(), key=lambda kv: -kv[1][0] / kv[1][1])[:10]
print("\\nthe ten best cells of the earlier half, scored on the later half:")
lc = cells(late, 40)
pool = []
for cell, (mean, se, nw, n) in best:
    got = lc.get(cell)
    pool += [r["ret"] for r in late if r["cell"] == cell]
    print(f"  {cell[0]:9} ask {cell[1]:.2f}-{cell[1] + 0.05:.2f} minute {cell[2]:2}-{cell[2] + 2:2}  earlier {mean:+6.1%} (score {mean / se:+.1f})  later " + (f"{got[0]:+6.1%} +-{got[1] * 100:.1f} ({got[2]} windows)" if got else "too few"))
if pool:
    print(f"  pooled later-half return of those ten cells: {statistics.mean(pool):+.1%} over {len(pool)} moments (the whole later half averages {statistics.mean(r['ret'] for r in late):+.1%})")
