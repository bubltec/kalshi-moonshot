"""THE ORIGINAL IDEA, TESTED AS DESCRIBED: a 95-99% favorite, a burst of selling against it in a 3-second window in the last two minutes, buy the cheap opposite side. In every finished crypto 15-minute window on the tape, the first second in the last 120 s (and separately the last 45 s) where
the favorite is priced 95c+ and the takers going AGAINST it bought at least 5x their own normal in dollars (300+ contracts) in 3 s. At that moment: what the opposite side costs, and what happened (held to the result, after the buy fee, per dollar).
Then the same with the EXTERNAL tape: the Hyperliquid perpetual's taker flow for the coin in the same 3 s, against the favorite's direction (a YES favorite is hurt by perp SELLS, a NO favorite by perp BUYS), at least 3x and 5x its own normal 3 s. Also the external burst alone.
Read-only.
    docker compose exec -T bot python - < scripts/flip_signal_study.py
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
events = []
for w in main.db.execute("SELECT ticker, series, close_ts, result FROM windows WHERE series IN ('KXBTC15M','KXETH15M','KXSOL15M','KXXRP15M','KXDOGE15M') AND result IN ('yes','no')").fetchall():
    T = float(w["close_ts"])
    if T - 300 < t0:
        continue
    coin = w["series"][2:].replace("15M", "")
    coin = {"DOG": "DOGE"}.get(coin, coin)
    flow = {int(x[0]): x for x in tape.execute("SELECT ts, yes_vol, no_vol, vwap FROM flow_1s WHERE ticker=? AND ts BETWEEN ? AND ?", (w["ticker"], T - 260, T)).fetchall()}
    fut = {int(x[0]): x for x in tape.execute("SELECT ts, buy_usd, sell_usd FROM fut_1s WHERE symbol=? AND ts BETWEEN ? AND ?", (coin, T - 260, T)).fetchall()}
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (w["ticker"], T - 260, T)).fetchall()
    if len(flow) < 20 or not q or len(fut) < 20:
        continue
    qts = [x[0] for x in q]

    def kal(sec, against, flow=flow):  # (contracts, dollars) the takers against the favorite bought this second
        x = flow.get(sec)
        if not x or x[3] is None:
            return 0.0, 0.0
        return (x[1], x[1] * x[3]) if against == "yes" else (x[2], x[2] * (1.0 - x[3]))

    def ext(sec, against, fut=fut):  # perp dollars against the favorite: a YES favorite is hurt by sells, a NO favorite by buys
        x = fut.get(sec)
        if not x:
            return 0.0
        return x[2] if against == "yes_fav" else x[1]

    found = {}
    for sec in range(int(T) - 120, int(T) - 9):
        i = bisect.bisect_right(qts, sec) - 1
        if i < 0 or sec - qts[i] > 30:
            continue
        yb, ya = q[i][1], q[i][2]
        mid = (yb + ya) / 2
        fav = "yes" if mid >= 0.5 else "no"
        pf = mid if fav == "yes" else 1 - mid
        if pf < 0.95:
            continue
        against = "no" if fav == "yes" else "yes"  # the takers against the favorite buy the other side
        c3 = d3 = 0.0
        for k in range(3):
            c, d = kal(sec - k, against)
            c3 += c
            d3 += d
        per = sorted(kal(sec - 3 - k, against)[1] for k in range(120))
        base = (per[59] + per[60]) / 2
        kal_ratio = d3 / (3 * base + 0.01) if base >= 0.1 and c3 >= 300 else 0.0
        e3 = sum(ext(sec - k, "yes_fav" if fav == "yes" else "no_fav") for k in range(3))
        eper = sorted(ext(sec - 3 - k, "yes_fav" if fav == "yes" else "no_fav") for k in range(120))
        ebase = (eper[59] + eper[60]) / 2
        ext_ratio = e3 / (3 * ebase + 1.0) if ebase > 0 else 0.0
        other_ask = (1.0 - yb) if fav == "yes" else ya
        row = {"win": T, "coin": coin, "left": T - sec, "kal": kal_ratio, "ext": ext_ratio, "ask": other_ask, "other_won": w["result"] == against}
        for key, cond in (("kalshi burst", kal_ratio >= 5.0), ("external burst alone", ext_ratio >= 5.0 and kal_ratio < 5.0), ("both (ext >= 3)", kal_ratio >= 5.0 and ext_ratio >= 3.0), ("both (ext >= 5)", kal_ratio >= 5.0 and ext_ratio >= 5.0)):
            if cond and key not in found:
                found[key] = row
    for key, row in found.items():
        events.append((key, row))
print(len(events), "signals in", len({r["win"] for _, r in events}), "windows (the Hyperliquid tape starts", int(t0), ")")


def show(title, rows):
    if len(rows) < 8:
        print(f"  {title:44} {len(rows):3} signals (too few)")
        return
    ret = [((1.0 if r["other_won"] else 0.0) - r["ask"] - fee(r["ask"])) / (r["ask"] + fee(r["ask"])) for r in rows]
    by = defaultdict(float)
    for r, x in zip(rows, ret, strict=True):
        by[r["win"]] += x
    m = statistics.mean(ret)
    se = math.sqrt(len(by)) * statistics.pstdev(list(by.values())) / len(ret) if len(by) > 1 else float("nan")
    print(f"  {title:44} {len(rows):3} signals, the opposite side won {100 * statistics.mean(r['other_won'] for r in rows):3.0f}% at an average price {statistics.mean(r['ask'] for r in rows):.3f}; buy it: {m:+6.0%} +-{se:.0%}")


for label, sel in (("all coins", lambda r: True), ("BTC", lambda r: r["coin"] == "BTC"), ("not BTC", lambda r: r["coin"] != "BTC")):
    print(f"\n{label}, favorite 95c+, in the last 120 s")
    for key in ("kalshi burst", "external burst alone", "both (ext >= 3)", "both (ext >= 5)"):
        show(key, [r for k, r in events if k == key and sel(r)])
    print(f"{label}, in the last 45 s")
    for key in ("kalshi burst", "both (ext >= 3)"):
        show(key, [r for k, r in events if k == key and sel(r) and r["left"] <= 45])
