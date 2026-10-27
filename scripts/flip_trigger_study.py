"""THE TRIGGER THE COLLAPSES POINT TO: collapse_study.py found that the best single separator of the 38 real collapses from ordinary moments is the ABSOLUTE dollars the takers against the favorite bought in the last 3 s (AUC 0.94), not their multiple of normal.
Here that is tested as a trigger: in every finished crypto window, the first second in the last 120 s (10+ s left) where a favorite priced 90c+ has at least $X bought against it in 3 s. Buy the opposite side at its ask, hold to the result, after the buy fee, per dollar. Also how often the favorite then lost.
Read-only.
    docker compose exec -T bot python - < scripts/flip_trigger_study.py
"""
import bisect
import math
import statistics
from collections import defaultdict

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
t0 = tape.execute("SELECT MIN(ts) FROM fut_1s").fetchone()[0]
LEVELS = (10, 25, 50, 100, 200, 400, 800, 1600)
first = defaultdict(dict)  # (window, coin, level) -> event
n_windows = 0
for w in main.db.execute("SELECT ticker, series, close_ts, result FROM windows WHERE series IN ('KXBTC15M','KXETH15M','KXSOL15M','KXXRP15M','KXDOGE15M') AND result IN ('yes','no')").fetchall():
    T = float(w["close_ts"])
    if T - 400 < t0:
        continue
    flow = {int(x[0]): x for x in tape.execute("SELECT ts, yes_vol, no_vol, vwap FROM flow_1s WHERE ticker=? AND ts BETWEEN ? AND ?", (w["ticker"], T - 260, T)).fetchall()}
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (w["ticker"], T - 260, T)).fetchall()
    if len(flow) < 30 or len(q) < 40:
        continue
    n_windows += 1
    qts = [x[0] for x in q]

    def dollars(sec, taker, flow=flow):
        x = flow.get(sec)
        if not x or x[3] is None:
            return 0.0
        return x[1] * x[3] if taker == "yes" else x[2] * (1.0 - x[3])
    seen = set()
    for sec in range(int(T) - 120, int(T) - 9):
        i = bisect.bisect_right(qts, sec) - 1
        if i < 0 or sec - qts[i] > 20:
            continue
        yb, ya = q[i][1], q[i][2]
        mid = (yb + ya) / 2
        fav = "yes" if mid >= 0.5 else "no"
        if max(mid, 1 - mid) < 0.90:
            continue
        against = "no" if fav == "yes" else "yes"
        d3 = sum(dollars(sec - k, against) for k in range(3))
        ask = (1.0 - yb) if fav == "yes" else ya
        for lv in LEVELS:
            if d3 >= lv and lv not in seen:
                seen.add(lv)
                first[(T, w["series"][2:5], lv)] = {"win": T, "coin": w["series"][2:5], "ask": ask, "fav_lost": w["result"] == against, "left": T - sec}
print(n_windows, "windows")


def show(coin_sel, label):
    print(f"\n{label}: the first second in the last 120 s with at least $X bought against a 90c+ favorite in 3 s")
    print(f"  {'$X':>6} {'signals':>8} {'favorite lost':>14} {'avg opposite price':>19} {'buy the opposite':>20}")
    for lv in LEVELS:
        ev = [e for (T, c, l), e in first.items() if l == lv and coin_sel(c)]
        if len(ev) < 8:
            print(f"  {lv:>6} {len(ev):>8} (too few)")
            continue
        ret = [((1.0 if e["fav_lost"] else 0.0) - e["ask"] - fee(e["ask"])) / (e["ask"] + fee(e["ask"])) for e in ev]
        by = defaultdict(float)
        for e, r in zip(ev, ret, strict=True):
            by[e["win"]] += r
        m = statistics.mean(ret)
        se = math.sqrt(len(by)) * statistics.pstdev(list(by.values())) / len(ret) if len(by) > 1 else float("nan")
        print(f"  {lv:>6} {len(ev):>8} {100 * statistics.mean(e['fav_lost'] for e in ev):>13.0f}% {statistics.mean(e['ask'] for e in ev):>19.3f} {m:>+13.0%} +-{se:.0%}")


show(lambda c: True, "all coins")
show(lambda c: c == "BTC", "BTC")
show(lambda c: c != "BTC", "not BTC")
