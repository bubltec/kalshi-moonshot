"""WHICH SIDE'S BURST CARRIES THE INFORMATION: in the last 120 s of every finished crypto 15-minute market, the first volume burst by each side's takers (the last 3 s in DOLLARS at least 5x that side's own median second of the 2 minutes before, 300+ contracts).
For each, what the side the takers bought was priced at, and what happened: FOLLOW = buy that side at its ask, FADE = buy the opposite side at its ask, held to the result, after the buy fee, per dollar. Grouped by whether the burst side was cheap (up to 15c), middle, or the favorite, and by coin.
Read-only.
    docker compose exec -T bot python - < scripts/burst_side_study.py
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
events = []
for w in main.db.execute("SELECT ticker, series, close_ts, result FROM windows WHERE series IN ('KXBTC15M','KXETH15M','KXSOL15M','KXXRP15M','KXDOGE15M') AND result IN ('yes','no')").fetchall():
    T = float(w["close_ts"])
    flow = {int(x[0]): x for x in tape.execute("SELECT ts, yes_vol, no_vol, vwap FROM flow_1s WHERE ticker=? AND ts BETWEEN ? AND ?", (w["ticker"], T - 260, T)).fetchall()}
    if len(flow) < 20:
        continue
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (w["ticker"], T - 260, T)).fetchall()
    if not q:
        continue
    qts = [x[0] for x in q]

    def dollars(sec, side, flow=flow):
        x = flow.get(sec)
        if not x or x[3] is None:
            return 0.0, 0.0
        return (x[1], x[1] * x[3]) if side == "yes" else (x[2], x[2] * (1.0 - x[3]))
    for side in ("yes", "no"):
        for sec in range(int(T) - 120, int(T) - 9):
            c3 = d3 = 0.0
            for k in range(3):
                c, d = dollars(sec - k, side)
                c3 += c
                d3 += d
            if c3 < 300:
                continue
            per = sorted(dollars(sec - 3 - k, side)[1] for k in range(120))
            base = (per[59] + per[60]) / 2
            if base < 0.5 or d3 / (3 * base + 0.01) < 5.0:
                continue
            i = bisect.bisect_right(qts, sec) - 1
            if i < 0 or sec - qts[i] > 30:
                break
            yb, ya = q[i][1], q[i][2]
            px = {"yes": ya, "no": 1.0 - yb}
            events.append({"win": T, "coin": w["series"][2:5], "side": side, "p_side": px[side], "p_other": px["no" if side == "yes" else "yes"], "left": T - sec, "won": w["result"] == side})
            break  # the first burst of this side in this market
print(len(events), "first bursts (dollars >= 5x, 300+ contracts) in the last 120 s of", len({e["win"] for e in events}), "windows")


def cluster(vals, wins):
    by = defaultdict(float)
    for v, wn in zip(vals, wins, strict=True):
        by[wn] += v
    m = statistics.mean(vals)
    se = math.sqrt(len(by)) * statistics.pstdev(list(by.values())) / len(vals) if len(by) > 1 else float("nan")
    return m, se


def line(name, g):
    if len(g) < 12:
        return
    follow = [((1.0 if e["won"] else 0.0) - e["p_side"] - fee(e["p_side"])) / (e["p_side"] + fee(e["p_side"])) for e in g]
    fade = [((0.0 if e["won"] else 1.0) - e["p_other"] - fee(e["p_other"])) / (e["p_other"] + fee(e["p_other"])) for e in g]
    wins = [e["win"] for e in g]
    fm, fs = cluster(follow, wins)
    gm, gs = cluster(fade, wins)
    print(f"  {name:28} {len(g):3} bursts; the burst side won {100 * statistics.mean(e['won'] for e in g):3.0f}% at an average price {statistics.mean(e['p_side'] for e in g):.2f};  FOLLOW {fm:+6.0%} +-{fs:.0%}   FADE {gm:+6.0%} +-{gs:.0%}")


for who, sel in (("all coins", lambda e: True), ("BTC only", lambda e: e["coin"] == "BTC"), ("not BTC", lambda e: e["coin"] != "BTC")):
    g = [e for e in events if sel(e)]
    print(f"\n{who}")
    for side in ("yes", "no"):
        for name, lo, hi in (("cheap (up to 15c)", 0.0, 0.15), ("middle (15-60c)", 0.15, 0.60), ("favorite (60c+)", 0.60, 1.01)):
            line(f"{side.upper()} takers, {name}", [e for e in g if e["side"] == side and lo <= e["p_side"] < hi])
