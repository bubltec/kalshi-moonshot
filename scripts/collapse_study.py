"""WHAT COMES BEFORE A COLLAPSE: start from the real ones. In every finished crypto 15-minute window on the tape, a COLLAPSE is a favorite priced 90c+ whose mid falls to 50c or under within 30 s, inside the last 120 s (t0 = the last second it was still 90c+, before the fall). A CONTROL is any second of
the last 120 s (every 5 s) where a favorite is 90c+ and does NOT fall under 50c in the next 30 s. For each, only what was known at t0: dollars the takers AGAINST the favorite bought in the last 3 s and how many times their own normal that is (median second of the 2 minutes before), the
share of the market's dollars that were against, the market's total contracts in 3 s against its own normal, the favorite's price change over the last 10 s, the spread, the Hyperliquid perp flow against the favorite in 3 s against its own normal, and the seconds left.
Then how well each separates collapses from controls (AUC: 0.5 = nothing, 1.0 = perfect), and how the collapses ended (did the favorite lose).
Read-only.
    docker compose exec -T bot python - < scripts/collapse_study.py
"""
import bisect
import statistics

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
hl0 = tape.execute("SELECT MIN(ts) FROM fut_1s").fetchone()[0]
rows = []  # (kind, coin, features, lost)
for w in main.db.execute("SELECT ticker, series, close_ts, result FROM windows WHERE series IN ('KXBTC15M','KXETH15M','KXSOL15M','KXXRP15M','KXDOGE15M') AND result IN ('yes','no')").fetchall():
    T = float(w["close_ts"])
    if T - 400 < hl0:
        continue
    coin = {"DOG": "DOGE"}.get(w["series"][2:5], w["series"][2:5])
    flow = {int(x[0]): x for x in tape.execute("SELECT ts, yes_vol, no_vol, vwap FROM flow_1s WHERE ticker=? AND ts BETWEEN ? AND ?", (w["ticker"], T - 330, T)).fetchall()}
    fut = {int(x[0]): x for x in tape.execute("SELECT ts, buy_usd, sell_usd FROM fut_1s WHERE symbol=? AND ts BETWEEN ? AND ?", (coin, T - 330, T)).fetchall()}
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (w["ticker"], T - 330, T)).fetchall()
    if len(flow) < 30 or len(q) < 40 or len(fut) < 30:
        continue
    qts = [x[0] for x in q]

    def mid(sec, q=q, qts=qts):
        i = bisect.bisect_right(qts, sec) - 1
        return None if i < 0 or sec - qts[i] > 20 else ((q[i][1] + q[i][2]) / 2, q[i][2] - q[i][1])

    def fav_price(sec):
        m = mid(sec)
        if m is None:
            return None, None, None
        return (max(m[0], 1 - m[0]), "yes" if m[0] >= 0.5 else "no", m[1])

    def dollars(sec, taker, flow=flow):
        x = flow.get(sec)
        if not x or x[3] is None:
            return 0.0
        return x[1] * x[3] if taker == "yes" else x[2] * (1.0 - x[3])

    def contracts(sec, flow=flow):
        x = flow.get(sec)
        return (x[1] + x[2]) if x else 0.0

    def features(t0, fav):
        against = "no" if fav == "yes" else "yes"
        d3 = sum(dollars(t0 - k, against) for k in range(3))
        w3 = sum(dollars(t0 - k, fav) for k in range(3))
        per = sorted(dollars(t0 - 3 - k, against) for k in range(120))
        base = (per[59] + per[60]) / 2
        c3 = sum(contracts(t0 - k) for k in range(3))
        cper = sorted(contracts(t0 - 3 - k) for k in range(120))
        cbase = (cper[59] + cper[60]) / 2
        p_now, _f, spread = fav_price(t0)
        p_prev, _f2, _s2 = fav_price(t0 - 10)
        x = sum((fut.get(t0 - k) or (0, 0, 0))[2 if fav == "yes" else 1] for k in range(3))
        eper = sorted((fut.get(t0 - 3 - k) or (0, 0, 0))[2 if fav == "yes" else 1] for k in range(120))
        ebase = (eper[59] + eper[60]) / 2
        return {
            "against $ in 3 s": d3, "against / its normal (dollars)": d3 / (3 * base + 0.01), "against share of dollars": d3 / (d3 + w3) if d3 + w3 > 0 else 0.0,
            "all contracts / their normal": c3 / (3 * cbase + 1.0), "favorite price change, 10 s": (p_now - p_prev) if p_prev is not None else 0.0,
            "spread": spread or 0.0, "perp flow against / normal": x / (3 * ebase + 1.0), "seconds left": T - t0,
        }

    # collapses: the first time a favorite that was 90c+ within the last 30 s is at 50c or under
    t_end = int(T) - 10
    found = None
    for sec in range(int(T) - 120, t_end):
        m_now = mid(sec)
        if m_now is None:
            continue
        for back in range(1, 31):
            mb = mid(sec - back)
            if mb is None:
                continue
            if mb[0] >= 0.90 and m_now[0] <= 0.50:
                found = (sec - back, "yes")
                break
            if mb[0] <= 0.10 and m_now[0] >= 0.50:
                found = (sec - back, "no")
                break
        if found:
            break
    if found:
        t0, side = found
        # t0 is the most recent second it was still 90c+ before the fall
        while mid(t0 + 1) is not None and ((mid(t0 + 1)[0] if side == "yes" else 1 - mid(t0 + 1)[0]) >= 0.90) and t0 + 1 < T - 10:
            t0 += 1
        rows.append(("collapse", coin, features(t0, side), w["result"] != side))
    for sec in range(int(T) - 120, int(T) - 40, 5):
        p, fav, _sp = fav_price(sec)
        if p is None or p < 0.90:
            continue
        if found and abs(sec - found[0]) <= 40:
            continue
        ok = all((lambda m: m is None or (m[0] if fav == "yes" else 1 - m[0]) > 0.50)(mid(sec + k)) for k in range(1, 31))
        if ok:
            rows.append(("control", coin, features(sec, fav), w["result"] != fav))
col = [r for r in rows if r[0] == "collapse"]
ctl = [r for r in rows if r[0] == "control"]
print(f"{len(col)} collapses (90c+ to 50c or under within 30 s, last 120 s) in the {len({1 for _ in col})} windows; {len(ctl)} control moments")
print("collapses that went on to LOSE at settlement:", sum(1 for r in col if r[3]), "of", len(col), "; by coin:", {c: sum(1 for r in col if r[1] == c) for c in sorted({r[1] for r in col})})


def auc(a, b):
    """P(a random collapse value is higher than a random control value)."""
    if not a or not b:
        return float("nan")
    allv = sorted([(v, 0) for v in a] + [(v, 1) for v in b])
    rank_sum = 0.0
    i = 0
    n = len(allv)
    while i < n:
        j = i
        while j < n and allv[j][0] == allv[i][0]:
            j += 1
        for k in range(i, j):
            if allv[k][1] == 0:
                rank_sum += (i + j + 1) / 2.0
        i = j
    return (rank_sum - len(a) * (len(a) + 1) / 2.0) / (len(a) * len(b))


for label, sel in (("all coins", lambda r: True), ("BTC", lambda r: r[1] == "BTC"), ("not BTC", lambda r: r[1] != "BTC")):
    c = [r for r in col if sel(r)]
    k = [r for r in ctl if sel(r)]
    if len(c) < 5:
        print(f"\n{label}: {len(c)} collapses (too few)")
        continue
    print(f"\n{label}: {len(c)} collapses against {len(k)} controls   (feature: median at a collapse | median at a control | AUC)")
    for name in c[0][2]:
        a = [r[2][name] for r in c]
        b = [r[2][name] for r in k]
        print(f"  {name:34} {statistics.median(a):10.3f} | {statistics.median(b):10.3f} | AUC {auc(a, b):.2f}")
