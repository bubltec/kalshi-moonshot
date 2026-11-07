"""Would the shock exit, read in typical minutes, have sold the live crypto bets that went to zero without selling the ones that won?

Question (2026-11-07, user: ETH/BTC/SOL favorites losing in full, no exits): on the same live crypto favorite bets since 10-28 (crypto_favorite, crypto_eth, crypto_btc, crypto_sol; one row per market and side), compare holding to settlement with the shock exit read in basis points (the lanes' 8 bp / 5 bp rule, today log-only) and in typical minutes (units of the market's own last 900 s of one-second moves, as watcher._shock_hit reads it with shock_units).
Rules replayed as the watcher runs them: active in the last L seconds, the worst spot move against the bet over the last 60 s window kept for 120 s is at least the threshold, the spot is within the margin of the strike, the held-side bid is in [0.05, 0.89]. Sale at bid - 0.01, fee on both legs.
Reported per rule: n, hold net, rule net, sales, bets helped/hurt, in two halves of the period, and by curve shape (the spot's path efficiency over the 5 minutes before the buy, in terciles of this bet set).

    scripts/hostrun3.sh-style: docker compose exec -T bot python - < scripts/shock_units_replay.py
"""

import bisect
import calendar
import math

from kalshi_moonshot import config, db
from kalshi_moonshot.kalshiws import index_for_series
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

LANES = ("crypto_favorite", "crypto_eth", "crypto_btc", "crypto_sol")
SHOCK_SECONDS, MEMORY = 60, 120
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
t0 = calendar.timegm((2026, 10, 28, 0, 0, 0))
marks = ",".join("?" * len(LANES))
rows = st.db.execute(
    f"SELECT ts,ticker,lane,side,ask,stake_usd,outcome FROM decisions WHERE action='bet' AND mode='trade' AND stake_usd>0 AND ts>? AND lane IN ({marks}) ORDER BY ts",
    (t0, *LANES),
).fetchall()
seen = {}
for r in rows:
    seen[(r["ticker"], r["side"])] = r
print("live bets", len(seen))


def hold_pnl(r):
    c = r["stake_usd"] / r["ask"]
    return ((1 - r["ask"]) * c if r["outcome"] == r["side"] else -r["ask"] * c) - fee(r["ask"]) * c


def grid(T, V, t_from, t_to):
    """One-second grid of the last value, from t_from to t_to."""
    out, i = [], 0
    n = len(T)
    t = int(t_from)
    while t <= t_to:
        while i + 1 < n and T[i + 1] <= t:
            i += 1
        out.append(V[i] if T[i] <= t else 0.0)
        t += 1
    return out


data = []
for r in seen.values():
    ser = r["ticker"].split("-")[0]
    idx = index_for_series(ser)
    w = st.db.execute("SELECT close_ts,strike,result FROM windows WHERE ticker=?", (r["ticker"],)).fetchone()
    if not idx or not w or not w["strike"] or r["outcome"] not in ("yes", "no"):
        continue
    close, strike = float(w["close_ts"]), float(w["strike"])
    tk = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND ts<=? AND value>0 ORDER BY ts", (idx, r["ts"] - 1500, close)).fetchall()
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts>=? AND ts<=? AND bid>0 AND ask>0 ORDER BY ts", (r["ticker"], r["ts"], close)).fetchall()
    if len(tk) < 300 or len(q) < 5:
        continue
    T = [float(x[0]) for x in tk]
    V = [float(x[1]) for x in tk]
    sg = 1 if r["side"] == "yes" else -1
    g0 = int(T[0]) + 1
    G = grid(T, V, g0, int(close))
    gi = lambda t: min(max(int(t) - g0, 0), len(G) - 1)
    # curve shape at the buy: path efficiency of the spot over the 5 minutes before it
    a, b = gi(r["ts"] - 300), gi(r["ts"])
    seg = [x for x in G[a : b + 1] if x > 0]
    path = sum(abs(math.log(y / x)) for x, y in zip(seg, seg[1:]))
    eff = abs(math.log(seg[-1] / seg[0])) / path if len(seg) > 10 and path > 0 else None
    # per quote reading: (left, worst move in bp over MEMORY, margin bp, typical minute in bp, held-side bid)
    reads, moves = [], []
    for x in q:
        now = float(x["ts"])
        left = close - now
        if left <= 3 or left > 330:
            continue
        i = gi(now)
        j = gi(now - SHOCK_SECONDS)
        if G[i] <= 0 or G[j] <= 0 or now - T[max(bisect.bisect_right(T, now) - 1, 0)] > 5:
            continue
        mv = math.log(G[i] / G[j]) * sg * 1e4
        moves.append((now, mv))
        while moves and now - moves[0][0] > MEMORY:
            moves.pop(0)
        worst = min(m for _, m in moves)
        k0 = gi(now - 900)
        seg9 = [z for z in G[k0 : i + 1] if z > 0]
        if len(seg9) < 300:
            continue
        d = [math.log(y / x) for x, y in zip(seg9, seg9[1:])]
        unit = math.sqrt(sum(z * z for z in d) / len(d)) * math.sqrt(60) * 1e4
        margin = math.log(G[i] / strike) * sg * 1e4
        bid = x["bid"] if r["side"] == "yes" else 1 - x["ask"]
        reads.append((left, worst, margin, unit, bid))
    data.append((r, eff, reads))
print("bets replayed", len(data), "hold net $%.2f" % sum(hold_pnl(r) for r, _, _ in data))
effs = sorted(e for _, e, _ in data if e is not None)
lo, hi = effs[len(effs) // 3], effs[2 * len(effs) // 3]
times = sorted(r["ts"] for r, _, _ in data)
mid = times[len(times) // 2]


def shape(e):
    return "unknown" if e is None else "choppy" if e <= lo else "straight" if e >= hi else "middle"


def decide(r, reads, L, kind, thr, mar):
    e = r["ask"]
    c = r["stake_usd"] / e
    for left, worst, margin, unit, bid in reads:
        if left > L:
            continue
        need, room = (thr * unit, mar * unit) if kind == "u" else (thr, mar)
        if worst <= -need and margin <= room and 0.05 <= bid <= 0.89:
            sp = max(bid - 0.01, 0.01)
            return (sp - e) * c - (fee(e) + fee(sp)) * c, True
    return hold_pnl(r), False


def run(label, L, kind, thr, mar):
    tot = {"all": [0, 0.0, 0, 0, 0.0, 0.0]}
    parts = {"first half": [0.0, 0.0, 0, 0], "second half": [0.0, 0.0, 0, 0]}
    shp = {}
    big = [0, 0.0, 0.0]
    wins_sold = [0, 0.0]
    for r, e, reads in data:
        h = hold_pnl(r)
        p, fired = decide(r, reads, L, kind, thr, mar)
        t = tot["all"]
        t[0] += 1
        t[1] += p
        t[2] += fired
        t[3] += p > h + 1e-9
        t[4] += max(p - h, 0)
        t[5] += max(h - p, 0)
        part = parts["first half" if r["ts"] <= mid else "second half"]
        part[0] += h
        part[1] += p
        part[2] += p > h + 1e-9
        part[3] += p < h - 1e-9
        sh = shp.setdefault(shape(e), [0.0, 0.0, 0, 0])
        sh[0] += h
        sh[1] += p
        sh[2] += p > h + 1e-9
        sh[3] += p < h - 1e-9
        if h < -0.7 * r["stake_usd"]:
            big[0] += 1
            big[1] += h
            big[2] += p
        if fired and h > 0:
            wins_sold[0] += 1
            wins_sold[1] += p - h
    t = tot["all"]
    ph = " | ".join(f"{k} {v[1] - v[0]:+.2f} (beat {v[2]}, lost {v[3]})" for k, v in parts.items())
    ps = " | ".join(f"{k} {v[1] - v[0]:+.2f} (beat {v[2]}, lost {v[3]})" for k, v in sorted(shp.items()))
    print(f"{label:34s} L={L:3d}: change {t[1] - sum(hold_pnl(r) for r, _, _ in data):+7.2f} sold {t[2]:3d} beat {t[3]:3d} helped {t[4]:6.2f} hurt {t[5]:6.2f} | total losers {big[0]}: hold {big[1]:+.2f} -> {big[2]:+.2f}; winners sold {wins_sold[0]} ({wins_sold[1]:+.2f})")
    print(f"      halves: {ph}")
    print(f"      shapes: {ps}")


print("path-efficiency cutoffs (this bet set):", round(lo, 3), round(hi, 3), "| halves split at", mid)
for L in (120, 180, 300):
    run("8 bp / 5 bp (today, log-only)", L, "b", 8.0, 5.0)
for L in (120, 180, 300):
    for thr in (0.75, 1.0, 1.5, 2.0):
        for mar in (0.6, 1.0, 99.0):
            run(f"{thr:g} typical min / margin {mar:g}", L, "u", thr, mar)
print("DONE")
