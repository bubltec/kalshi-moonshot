"""Does the history matter more the lower the price? (2026-11-10, user: "the lower the price the heavier the hourly and previous 15 minute history should matter, it's the opposite with higher prices; important in both, but if you're buying a low hoping to win you need to be looking at history closer")
Question: on the same 10,065 underdog-priced tape states as scripts/underdog_signs_study.py (each side's first 0.20-0.40 ask, 30 s-7.5 min into a settled 15-minute crypto market since 10-29), is the spread in net return between the best and worst fourth of the hour's drift (and of the 15 minutes before) wider at low asks than at high ones?
Readings (signed with the bet, in the market's own typical minute): hour = the drift of the last 3600 s in the typical minute of that hour; m15 = the drift of the last 900 s in the typical minute of the last 900 s. Price thirds are the thirds of this sample's own asks. Each cell is net per $ after the buy fee (n) held to the result at the first qualifying ask; each row also shows the spread Q1 - Q4 (positive = the bet against the history does better). A halves check is printed for the spread of each price third.

    scripts/cloud_job.sh scripts/underdog_price_history.py
"""

import array
import calendar
import itertools
import math
import os
import time

from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

os.nice(10)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
SINCE = float(os.environ.get("SINCE", calendar.timegm((2026, 10, 29, 0, 0, 0))))
NOW = float(os.environ.get("UNTIL", time.time()))
NAN = float("nan")

COINS = {"BTC": "KXBTC15M", "ETH": "KXETH15M", "SOL": "KXSOL15M", "XRP": "KXXRP15M", "DOGE": "KXDOGE15M", "BNB": "KXBNB15M", "ZEC": "KXZEC15M", "HYPE": "KXHYPE15M", "NEAR": "KXNEAR15M"}
IDX = {c: variants.crypto_index(ser) for c, ser in COINS.items()}
IDX = {c: i for c, i in IDX.items() if i}
PERP = {"BTC", "ETH", "SOL", "XRP", "DOGE"}

wins = st.db.execute(
    "SELECT ticker,close_ts,strike,result FROM windows WHERE result IN ('yes','no') AND close_ts>? AND close_ts<? AND ticker LIKE 'KX%15M-%' ORDER BY close_ts", (SINCE + 600, NOW)
).fetchall()
wins = [w for w in wins if w["ticker"].split("-")[0] in COINS.values() and w["strike"]]
T0 = int(SINCE) - 3700
T1 = int(max(float(w["close_ts"]) for w in wins)) + 5
N = T1 - T0 + 1
print(f"markets {len(wins)} | coins {sorted(IDX)} | seconds {N}", flush=True)


def load_index(ix):
    g = array.array("d", [NAN]) * N
    day = 86400
    a = T0
    while a < T1:
        b = min(a + day, T1 + 1)
        for ts, v in tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>=? AND ts<? AND value>0 ORDER BY ts", (ix, a, b)).fetchall():
            g[int(ts) - T0] = math.log(float(v))
        a = b
    last = NAN
    for k in range(N):
        if g[k] == g[k]:
            last = g[k]
        else:
            g[k] = last
    sq, ab = array.array("d", [0.0]) * (N + 1), array.array("d", [0.0]) * (N + 1)
    for k in range(1, N):
        d = g[k] - g[k - 1]
        d = d if d == d else 0.0
        sq[k + 1] = sq[k] + d * d
        ab[k + 1] = ab[k] + abs(d)
    return g, sq, ab


G = {c: load_index(i) for c, i in IDX.items()}
print("indexes loaded", flush=True)


def load_perp(sym):
    px = array.array("d", [NAN]) * N
    net = array.array("d", [0.0]) * N
    for ts, p, b, sl in tape.execute("SELECT ts,px,buy_usd,sell_usd FROM fut_1s WHERE symbol=? AND ts>=? AND ts<=? ORDER BY ts", (sym, T0, T1)).fetchall():
        k = int(ts) - T0
        px[k] = math.log(float(p))
        net[k] += float(b) - float(sl)
    last = NAN
    for k in range(N):
        if px[k] == px[k]:
            last = px[k]
        else:
            px[k] = last
    cum, c2 = array.array("d", [0.0]) * (N + 1), array.array("d", [0.0]) * (N + 1)
    for k in range(N):
        cum[k + 1] = cum[k] + net[k]
        c2[k + 1] = c2[k] + net[k] * net[k]
    return px, cum, c2


P = {c: load_perp(c) for c in PERP if c in IDX}
print("perps loaded", flush=True)


def unit(c, k, w=900):
    sq = G[c][1]
    v = (sq[k + 1] - sq[k + 1 - w]) / w
    return math.sqrt(v) * math.sqrt(60.0) if v > 0 else None


def mv(c, k, w):
    g = G[c][0]
    return g[k] - g[k - w]


rows = []
for wi, w in enumerate(wins):
    tk, close, strike = w["ticker"], float(w["close_ts"]), float(w["strike"])
    coin = next(c for c, ser in COINS.items() if ser == tk.split("-")[0])
    if coin not in IDX:
        continue
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (tk, close - 900 + 30, close - 900 + 450)).fetchall()
    taken = set()
    for ts, bid, ask in q:
        yb, ya = float(bid), float(ask)
        if ya - yb > 0.06:
            continue
        for side, a in (("yes", ya), ("no", 1.0 - yb)):
            if side in taken or not (0.20 <= a <= 0.40):
                continue
            taken.add(side)
            k = int(ts) - T0
            if k < 3700 or k >= N - 5:
                continue
            sign = 1.0 if side == "yes" else -1.0
            u = unit(coin, k)
            ul = unit(coin, k, 3600)
            ush = unit(coin, k, 180)
            if not u or not ul or not ush:
                continue
            g, sq, ab = G[coin]
            f = {"cushion": (g[k] - math.log(strike)) * sign / u, "hour": mv(coin, k, 3600) * sign / ul, "ratio": ush / ul, "m15": mv(coin, k, 900) * sign / u}
            path = ab[k + 1] - ab[k + 1 - 300]
            f["eff"] = abs(g[k] - g[k - 300]) / path if path > 0 else NAN
            others = [c for c in G if c != coin]
            m60 = {c: (mv(c, k, 60) * sign / u_) if (u_ := unit(c, k)) else NAN for c in others}
            m180 = {c: (mv(c, k, 180) * sign / u_) if (u_ := unit(c, k)) else NAN for c in others}
            ok = [v for v in m60.values() if v == v]
            lead = "ETH" if coin == "BTC" else "BTC"
            f["lead60"], f["lead180"] = m60.get(lead, NAN), m180.get(lead, NAN)
            f["breadth60"] = sum(ok) / len(ok) if ok else NAN
            f["n_with"] = float(sum(1 for v in ok if v >= 1.0))
            own60 = mv(coin, k, 60) * sign / u
            f["gap60"] = own60 - f["breadth60"] if ok else NAN
            if coin in P:
                px, cum, c2 = P[coin]
                for hh in (60, 180):
                    f[f"perp_lead{hh}"] = ((px[k] - px[k - hh]) - mv(coin, k, hh)) * sign / u
                f["perp_move60"] = (px[k] - px[k - 60]) * sign / u
                rms = math.sqrt((c2[k + 1] - c2[k + 1 - 900]) / 900.0)
                for hh in (10, 30, 60):
                    f[f"flow{hh}"] = sign * (cum[k + 1] - cum[k + 1 - hh]) / (rms * math.sqrt(hh)) if rms > 0 else NAN
            won = w["result"] == side
            cost = a + fee(a)
            f.update(ts=float(ts), win=1.0 if won else 0.0, ret=((1.0 if won else 0.0) - cost) / cost, ask=a)
            rows.append(f)
    if wi % 1000 == 0:
        print(f"  {wi} markets, {len(rows)} rows", flush=True)


print(f"\nstates {len(rows)} | win {sum(r['win'] for r in rows) / len(rows):.3f} | net per $ {sum(r['ret'] for r in rows) / len(rows):+.4f}")
mid = sorted(r["ts"] for r in rows)[len(rows) // 2]
asks = sorted(r["ask"] for r in rows)
a1, a2 = asks[len(asks) // 3], asks[2 * len(asks) // 3]
thirds = (("low ask", lambda a: a <= a1), ("mid ask", lambda a: a1 < a <= a2), ("high ask", lambda a: a > a2))
print(f"price thirds: low <= {a1:.2f}, mid <= {a2:.2f}, high above")


def net(rs):
    return sum(r["ret"] for r in rs) / len(rs) if rs else float("nan")


for feat in ("hour", "m15"):
    print(f"\n{feat}: net per $ by quartile of the reading (Q1 = the bet against the history ... Q4 = with it), within each price third")
    cuts = None
    for name, test in thirds:
        rs = [r for r in rows if test(r["ask"]) and r[feat] == r[feat]]
        xs = sorted(r[feat] for r in rs)
        c = (xs[len(xs) // 4], xs[len(xs) // 2], xs[3 * len(xs) // 4])
        qs = [[r for r in rs if r[feat] <= c[0]], [r for r in rs if c[0] < r[feat] <= c[1]], [r for r in rs if c[1] < r[feat] <= c[2]], [r for r in rs if r[feat] > c[2]]]
        cells = " | ".join(f"Q{i + 1} {net(q):+.3f} (n={len(q)}, win {sum(r['win'] for r in q) / max(len(q), 1):.2f})" for i, q in enumerate(qs))
        h1 = net([r for r in qs[0] if r["ts"] <= mid]) - net([r for r in qs[3] if r["ts"] <= mid])
        h2 = net([r for r in qs[0] if r["ts"] > mid]) - net([r for r in qs[3] if r["ts"] > mid])
        print(f"  {name:8s} {cells} | spread Q1-Q4 {net(qs[0]) - net(qs[3]):+.3f} (halves {h1:+.3f} / {h2:+.3f})")
print("DONE")
