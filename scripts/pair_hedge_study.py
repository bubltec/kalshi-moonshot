"""Pairs hedge on Kalshi alone: buy the bet, and against it the opposite side of the most correlated market in the same 15-minute slot, at an OLS ratio.

For one family of series (metals, coins or forex):
  1. Every slot's markets are sampled every STEP seconds and the changes in each market's YES mid are paired across series. For every ordered pair (A, B)
     the OLS of A's change on B's gives the hedge ratio beta (contracts of B per contract of A) and the correlation r. Each series' partner is the one with
     the highest r.
  2. Every recorded bet of the family's lane is replayed held to settlement, with the hedge leg bought at the same moment: the opposite side of the partner
     (NO of B against a YES of A, YES of B against a NO of A), `beta` contracts per contract of A, or 1:1 as a comparison. Both legs are bought at the ask and
     pay Kalshi's taker fee, and the unhedged bet pays its own fee, so the cases are comparable. Results are per dollar staked on A.
The betas are fitted on the same days, so the hedged cases are flattered a little.

    scripts/cloud.sh run scripts/pair_hedge_study.py FAMILY=metals|coins|fx [DAYS=3] [STEP=15]
"""
import math
import os
import re
import time
from datetime import datetime

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)

FAMILY = os.environ.get("FAMILY", "metals")
DAYS = float(os.environ.get("DAYS", "3"))
STEP = float(os.environ.get("STEP", "15"))
METALS = {"GOLD", "SILVER", "PLATINUM", "PALLADIUM", "COPPER"}
COINS = {"BTC", "ETH", "SOL", "XRP", "DOGE"}
ENERGY = {"WTI", "BRENT", "NATGAS"}  # not metals, coins or currencies: left out of every family
LANE = {"metals": "commodity", "coins": "crypto_favorite", "fx": "fx_favorite"}[FAMILY]


def name_of(series):
    m = re.match(r"^KX([A-Z]+)15M$", series)
    return m.group(1) if m else None


def in_family(series):
    n = name_of(series)
    if n is None:
        return False
    return n in METALS if FAMILY == "metals" else n in COINS if FAMILY == "coins" else (len(n) == 6 and n not in METALS | COINS | ENERGY)


def latest(rows, t, within=6.0):
    lo, hi = 0, len(rows)
    while lo < hi:
        mid = (lo + hi) // 2
        if rows[mid][0] <= t:
            lo = mid + 1
        else:
            hi = mid
    if lo == 0 or t - rows[lo - 1][0] > within:
        return None
    return rows[lo - 1][1]


since = time.time() - DAYS * 86400
wrows = main.db.execute("SELECT ticker, series, close_ts, result FROM windows WHERE result IN ('yes','no') AND close_ts >= ? ORDER BY close_ts", (since,)).fetchall()
slots: dict[int, dict[str, tuple[str, str]]] = {}
for w in wrows:
    if in_family(w["series"]):
        slots.setdefault(int(w["close_ts"]), {})[name_of(w["series"])] = (w["ticker"], w["result"])
slots = {c: m for c, m in slots.items() if len(m) >= 2}

quotes_of: dict[str, list] = {}


def quotes(ticker, close):
    if ticker not in quotes_of:
        quotes_of[ticker] = [(float(t), (float(b) + float(a)) / 2.0, float(b), float(a)) for t, b, a in tape.execute(
            "SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 AND ts BETWEEN ? AND ? ORDER BY ts", (ticker, close - 960, close)).fetchall()]
    return quotes_of[ticker]


# 1. pairwise changes
acc: dict[tuple[str, str], list[float]] = {}  # (A, B) -> [n, sx, sy, sxx, syy, sxy] with x = dB, y = dA
for close, mk in slots.items():
    mids = {}
    for name, (ticker, _res) in mk.items():
        q = [(t, m) for t, m, _b, _a in quotes(ticker, close)]
        if len(q) >= 20:
            mids[name] = q
    names = sorted(mids)
    series = {}
    for name in names:
        row, prev = [], None
        t = close - 900 + STEP
        while t <= close - 1:
            m = latest(mids[name], t)
            row.append(None if m is None else m)
            t += STEP
        series[name] = row
    for a in names:
        for b in names:
            if a == b:
                continue
            cell = acc.setdefault((a, b), [0, 0.0, 0.0, 0.0, 0.0, 0.0])
            ra, rb = series[a], series[b]
            for k in range(1, len(ra)):
                if None in (ra[k], ra[k - 1], rb[k], rb[k - 1]):
                    continue
                y, x = ra[k] - ra[k - 1], rb[k] - rb[k - 1]
                cell[0] += 1
                cell[1] += x
                cell[2] += y
                cell[3] += x * x
                cell[4] += y * y
                cell[5] += x * y


def fit(a, b):
    n, sx, sy, sxx, syy, sxy = acc[(a, b)]
    if n < 150:
        return None
    cov = sxy / n - (sx / n) * (sy / n)
    vx = sxx / n - (sx / n) ** 2
    vy = syy / n - (sy / n) ** 2
    if vx <= 0 or vy <= 0:
        return None
    return cov / math.sqrt(vx * vy), cov / vx, n


partner: dict[str, tuple[str, float, float]] = {}
rank = []
for (a, b) in acc:
    f = fit(a, b)
    if f:
        rank.append((f[0], a, b, f[1], f[2]))
for a in {k[0] for k in acc}:
    best = max((r for r in rank if r[1] == a), default=None)
    if best:
        partner[a] = (best[2], best[3], best[0])
print(f"{FAMILY}: {len(slots)} slots with 2+ markets; each series' best partner (r = correlation of {STEP:g}s price changes, beta = contracts of partner per contract):")
for a, (b, beta, r) in sorted(partner.items(), key=lambda kv: -kv[1][2])[:6]:
    print(f"  {a:<9} ~ {b:<9} r {r:+.2f}  beta {beta:+.2f}")

# 2. the bets
bets = main.db.execute(
    "SELECT ts, ticker, side, ask, outcome, close_time FROM decisions WHERE action='bet' AND lane=? AND outcome IN ('yes','no') AND ticker LIKE '%15M%' ORDER BY ts", (LANE,)).fetchall()
res = {"unhedged": [], "ols": [], "one": []}
hedge_cost = []
for r in bets:
    series = r["ticker"].split("-")[0]
    me = name_of(series)
    if me is None or not in_family(series) or me not in partner:
        continue
    try:
        close = int(datetime.fromisoformat(str(r["close_time"])).timestamp())
    except (TypeError, ValueError):
        continue
    other, beta, _r = partner[me]
    mk = slots.get(close)
    if not mk or other not in mk or beta <= 0:
        continue
    pticker, presult = mk[other]
    pq = quotes(pticker, close)
    t0 = float(r["ts"])
    at = [q for q in pq if t0 - 8 <= q[0] <= t0]
    if not at:
        continue
    _t, _m, pbid, pask = at[-1]
    side = r["side"] or "yes"
    ask = float(r["ask"])
    n_a = 1.0 / ask
    a_win = 1.0 if r["outcome"] == side else 0.0
    base = n_a * (a_win - ask - fee_per_contract(ask))
    hedge_side = "no" if side == "yes" else "yes"
    hp = (1.0 - pbid) if hedge_side == "no" else pask  # the opposite side's ask
    if not 0.01 <= hp <= 0.99:
        continue
    h_win = 1.0 if presult == hedge_side else 0.0
    per = h_win - hp - fee_per_contract(hp)
    res["unhedged"].append(base)
    res["ols"].append(base + n_a * beta * per)
    res["one"].append(base + n_a * 1.0 * per)
    hedge_cost.append(n_a * beta * hp)


def stats(x):
    n = len(x)
    m = sum(x) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in x) / (n - 1)) if n > 1 else 0.0
    w = sorted(x)[: max(1, n // 10)]
    return m, sd, min(x), sum(w) / len(w)


if not res["unhedged"]:
    print("no bets with a partner quote")
else:
    print(f"\n{len(res['unhedged'])} {LANE} bets, both legs at the ask with fees, held to settlement; per dollar staked on the bet. Hedge leg costs ${sum(hedge_cost) / len(hedge_cost):.2f} per $1 staked at the OLS ratio")
    print(f"{'case':<26} {'mean':>8} {'stdev':>7} {'worst':>8} {'worst 10% avg':>14}")
    for key, label in (("unhedged", "unhedged"), ("ols", "pair hedge, OLS ratio"), ("one", "pair hedge, 1 : 1")):
        m, sd, w, c = stats(res[key])
        print(f"{label:<26} {m:>+8.1%} {sd:>7.1%} {w:>+8.0%} {c:>+14.0%}")
