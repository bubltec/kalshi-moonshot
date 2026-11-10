"""Walk-forward of the underdog signs (2026-11-10, user: "yes, walk-forward"), after studies/results/2026-11-10-underdog-signs.txt found a lead: the perp running ahead of the spot index in the bet's direction, with the cushion near the strike and a choppy path.
Question: fitted on EARLIER days only, do the rules below earn more than the price paid on LATER days, day after day?
Same population and readings as scripts/underdog_signs_study.py (every underdog-priced state on the tape since 10-29: each side's first 0.20-0.40 ask 30 s-7.5 min into a settled 15-minute crypto market; readings signed with the bet, in the market's own units; the perp readings only for BTC, ETH, SOL, XRP, DOGE).
Method: one UTC day is a fold. The first 5 days only train. For each later day the cutoffs (quantiles) are taken from every row of the days before it (expanding window), the rule is applied to that day's rows, and the day's returns are kept. Nothing from the test day or later is used for a cutoff. The rules were written down before this run, but they come from the full-period look at the same data (the lead), so the first walk-forward fold is the only fully clean one: the later folds still share the days the lead was seen on. Rules (q = the train quantile):
  R1  perp_lead60 > q75  and  cushion > q75
  R2  perp_lead60 > q75  and  ratio > q75
  R3  perp_lead60 > q75  and  eff < q50 (choppy)
  R4  R1  and  hour < q50 (the bet against the hour's drift)
  R5  perp_lead60 > q75  and  cushion > q75  and  eff < q50
  R6  hour < q25 (all nine coins, no perp)
  R7  perp_lead60 > q75 (alone)
  R8  eff < q25 (alone, all nine coins)
  ALL every underdog-priced state in the test days (the control: -the fee)
Report per rule: test rows, win rate, mean ask, net per $ after the buy fee (standard error), the test days with at least 5 rows and how many of them were positive, and the two halves of the test period. A rule counts only when positive in both halves and in most test days, with a net well above the control.

    scripts/cloud_job.sh scripts/underdog_walkforward.py
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
            f = {"cushion": (g[k] - math.log(strike)) * sign / u, "hour": mv(coin, k, 3600) * sign / ul, "ratio": ush / ul}
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


for r in rows:
    r["day"] = int(r["ts"] // 86400)
days = sorted({r["day"] for r in rows})
test_days = days[5:]
print(f"\nrows {len(rows)} | days {len(days)} | test days {len(test_days)}")


def q(rs, f, p):
    xs = sorted(r[f] for r in rs if f in r and r[f] == r[f])
    return xs[min(len(xs) - 1, int(p * len(xs)))] if len(xs) >= 100 else None


RULES = {
    "R1 perp_lead60>q75 & cushion>q75": [("perp_lead60", ">", 0.75), ("cushion", ">", 0.75)],
    "R2 perp_lead60>q75 & ratio>q75": [("perp_lead60", ">", 0.75), ("ratio", ">", 0.75)],
    "R3 perp_lead60>q75 & eff<q50": [("perp_lead60", ">", 0.75), ("eff", "<", 0.50)],
    "R4 R1 & hour<q50": [("perp_lead60", ">", 0.75), ("cushion", ">", 0.75), ("hour", "<", 0.50)],
    "R5 R1 & eff<q50": [("perp_lead60", ">", 0.75), ("cushion", ">", 0.75), ("eff", "<", 0.50)],
    "R6 hour<q25": [("hour", "<", 0.25)],
    "R7 perp_lead60>q75": [("perp_lead60", ">", 0.75)],
    "R8 eff<q25": [("eff", "<", 0.25)],
    "ALL (control)": [],
}
picked = {k: [] for k in RULES}
for d in test_days:
    train = [r for r in rows if r["day"] < d]
    test = [r for r in rows if r["day"] == d]
    for name, conds in RULES.items():
        cuts = [(f, op, q(train, f, p)) for f, op, p in conds]
        if any(c is None for _, _, c in cuts):
            continue
        for r in test:
            if all(f in r and r[f] == r[f] and (r[f] > c if op == ">" else r[f] < c) for f, op, c in cuts):
                picked[name].append(r)
mid = sorted(r["ts"] for rs in picked.values() for r in rs)
mid = mid[len(mid) // 2] if mid else 0
print("\nrule | test rows | win | mean ask | net per $ (se) | days positive / days with >= 5 rows | halves of the test period")
for name, rs in picked.items():
    n = len(rs)
    if n < 30:
        print(f"{name}: n={n}, too few")
        continue
    rets = [r["ret"] for r in rs]
    m = sum(rets) / n
    se = math.sqrt(sum((x - m) ** 2 for x in rets) / (n - 1) / n)
    by = {}
    for r in rs:
        by.setdefault(r["day"], []).append(r["ret"])
    ds = [(sum(v) / len(v)) for v in by.values() if len(v) >= 5]
    h1 = [r["ret"] for r in rs if r["ts"] <= mid]
    h2 = [r["ret"] for r in rs if r["ts"] > mid]
    print(f"{name}: n={n} win {sum(r['win'] for r in rs) / n:.3f} ask {sum(r['ask'] for r in rs) / n:.3f} net/$ {m:+.3f} ({se:.3f}) | days +{sum(1 for x in ds if x > 0)}/{len(ds)} | halves {sum(h1) / max(len(h1), 1):+.3f} (n={len(h1)}) / {sum(h2) / max(len(h2), 1):+.3f} (n={len(h2)})")
print("DONE")
