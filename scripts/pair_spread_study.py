"""Trading the spread of two correlated contracts, not hedging a bet: does the residual mean-revert by more than the round trip costs? Replayed from the tape.

For a family (metals, coins or forex) each pair of series with a correlation of RMIN or more (of their STEP-second YES-mid changes, fitted on all days) has an
OLS ratio beta: contracts of B per contract of A. In every slot where both markets exist, on a STEP-second grid:
    spread = YES mid of A  -  beta * YES mid of B
and its z-score against the previous WINDOW steps. When the z-score is at or under -Z the pair is cheap: buy YES of A and `beta` NO of B (the package gains if A
rises against B). At or over +Z it is rich: buy NO of A and `beta` YES of B. The position is closed on the first of: the z-score back across zero, MAXHOLD seconds,
or LEFT_EXIT seconds before the close (never held to settlement). Entries need both mids between 12c and 88c and at least LEFT_MIN seconds left.
Everything is paid at the real quotes: the legs are bought at the ask and sold at the bid, and every purchase and sale pays Kalshi's taker fee. One position at a
time per pair and slot. Results are per trade; "% capital" divides by what the package cost.

The betas are fitted on the same days, so the signal is flattered a little. Analysis only: this places nothing.

    scripts/cloud.sh run scripts/pair_spread_study.py FAMILY=metals|coins|fx [DAYS=3] [STEP=15] [RMIN=0.5] [ZS=1.5,2,3]
"""
import math
import os
import re
import time

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)

FAMILY = os.environ.get("FAMILY", "metals")
DAYS = float(os.environ.get("DAYS", "3"))
STEP = float(os.environ.get("STEP", "15"))
RMIN = float(os.environ.get("RMIN", "0.5"))
ZS = tuple(float(x) for x in os.environ.get("ZS", "1.5,2,3").split(","))
WINDOW, MAXHOLD, LEFT_EXIT, LEFT_MIN = 20, 300.0, 90.0, 120.0
METALS = {"GOLD", "SILVER", "PLATINUM", "PALLADIUM", "COPPER"}
COINS = {"BTC", "ETH", "SOL", "XRP", "DOGE"}
ENERGY = {"WTI", "BRENT", "NATGAS"}


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
wrows = main.db.execute("SELECT ticker, series, close_ts FROM windows WHERE result IN ('yes','no') AND close_ts >= ? ORDER BY close_ts", (since,)).fetchall()
slots: dict[int, dict[str, str]] = {}
for w in wrows:
    if in_family(w["series"]):
        slots.setdefault(int(w["close_ts"]), {})[name_of(w["series"])] = w["ticker"]
slots = {c: m for c, m in slots.items() if len(m) >= 2}

cache: dict[str, list] = {}


def grid(ticker, close):
    """One row per STEP on the slot's clock: (mid, bid, ask) of YES, or None."""
    if ticker not in cache:
        rows = [(float(t), float(b), float(a)) for t, b, a in tape.execute(
            "SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 AND ts BETWEEN ? AND ? ORDER BY ts", (ticker, close - 960, close)).fetchall()]
        out, t = [], close - 900 + STEP
        keyed = [(r[0], r) for r in rows]
        while t <= close - 1:
            got = latest(keyed, t)
            out.append(None if got is None else ((got[1] + got[2]) / 2.0, got[1], got[2]))
            t += STEP
        cache[ticker] = out if sum(x is not None for x in out) >= 20 else []
    return cache[ticker]


# 1. which pairs, and their ratio
acc: dict[tuple[str, str], list[float]] = {}
for close, mk in slots.items():
    g = {n: grid(t, close) for n, t in mk.items()}
    g = {n: v for n, v in g.items() if v}
    for a in g:
        for b in g:
            if a == b:
                continue
            cell = acc.setdefault((a, b), [0, 0.0, 0.0, 0.0, 0.0, 0.0])
            for k in range(1, len(g[a])):
                p, q = g[a][k], g[a][k - 1]
                u, v = g[b][k], g[b][k - 1]
                if None in (p, q, u, v):
                    continue
                y, x = p[0] - q[0], u[0] - v[0]
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
    return cov / math.sqrt(vx * vy), cov / vx


pairs = []
for (a, b) in acc:
    if a < b:
        f = fit(a, b)
        if f and f[0] >= RMIN and f[1] > 0:
            pairs.append((a, b, f[0], f[1]))
print(f"{FAMILY}: {len(slots)} slots; {len(pairs)} pairs with r >= {RMIN:g}: " + ", ".join(f"{a}~{b} r{r:.2f} b{be:.2f}" for a, b, r, be in pairs[:6]))


# 2. the spread trade
def trade(ga, gb, beta, close, z_in):
    """Trades on one pair in one slot: list of (gross pnl, net pnl, capital, seconds held)."""
    out = []
    spread = [None if (x is None or y is None) else x[0] - beta * y[0] for x, y in zip(ga, gb, strict=True)]
    pos = None
    for k in range(WINDOW, len(ga)):
        t = close - 900 + STEP * (k + 1)
        left = close - t
        if spread[k] is None or ga[k] is None or gb[k] is None:
            continue
        past = [v for v in spread[k - WINDOW:k] if v is not None]
        if len(past) < WINDOW * 0.6:
            continue
        mean = sum(past) / len(past)
        sd = max(math.sqrt(sum((v - mean) ** 2 for v in past) / len(past)), 0.005)
        z = (spread[k] - mean) / sd
        am, ab, aa = ga[k]
        bm, bb, ba = gb[k]
        if pos is not None:
            held = t - pos["t"]
            back = z * pos["dir"] >= 0  # dir +1: bought the cheap pair (entered at z <= -Z), done once z is back at or above 0
            if back or held >= MAXHOLD or left <= LEFT_EXIT:
                if pos["dir"] > 0:  # sell YES of A and NO of B at the bids
                    pa, pb = ab, 1.0 - ba
                else:  # sell NO of A and YES of B
                    pa, pb = 1.0 - aa, bb
                gross = (pa - pos["pa"]) + beta * (pb - pos["pb"])
                fees = fee_per_contract(pos["pa"]) + beta * fee_per_contract(pos["pb"]) + fee_per_contract(max(pa, 0.01)) + beta * fee_per_contract(max(pb, 0.01))
                out.append((gross, gross - fees, pos["pa"] + beta * pos["pb"], held))
                pos = None
            continue
        if left < LEFT_MIN or not (0.12 <= am <= 0.88 and 0.12 <= bm <= 0.88) or abs(z) < z_in:
            continue
        if z <= -z_in:  # cheap: buy YES of A and NO of B
            pos = {"dir": 1, "t": t, "pa": aa, "pb": 1.0 - bb}
        else:  # rich: buy NO of A and YES of B
            pos = {"dir": -1, "t": t, "pa": 1.0 - ab, "pb": ba}
    return out


print(f"{'z':>4} {'trades':>6} {'win%':>5} {'net/trade':>10} {'net % capital':>13} {'gross/trade':>12} {'avg hold':>9}", flush=True)
for z_in in ZS:
    rows = []
    for a, b, _r, beta in pairs:
        for close, mk in slots.items():
            if a in mk and b in mk:
                ga, gb = grid(mk[a], close), grid(mk[b], close)
                if ga and gb:
                    rows += trade(ga, gb, beta, close, z_in)
    if not rows:
        print(f"{z_in:>4g} no trades")
        continue
    n = len(rows)
    net = sum(r[1] for r in rows) / n
    gross = sum(r[0] for r in rows) / n
    cap = sum(r[1] for r in rows) / sum(r[2] for r in rows)
    wins = sum(1 for r in rows if r[1] > 0) / n
    hold = sum(r[3] for r in rows) / n
    print(f"{z_in:>4g} {n:>6} {wins:>5.0%} {net * 100:>+8.2f} c {cap:>+13.1%} {gross * 100:>+10.2f} c {hold:>7.0f} s")
