"""Does the market's own recent history of jumps say the curve overstates the chance? (2026-11-13, user: "using the curve we can see history as a factor too, so we would be able to adjust our risk factor based on repeated events").
Question: on the same live bets (crypto_multiscale and the commodity favorites lanes, trade mode, since Monday 11-09 00:00 ET), do bets placed after repeated jumps in that market's last hour win less than the curve's chance at the buy, or than the price paid says, than bets placed in a quiet hour?
  J = jumps in the hour before the buy (1-minute moves over 3 robust sigmas of that hour), A = jumps against the side bought in the last 30 minutes. Buckets: J 0 / 1 / 2 or more, and A 0 / 1 or more.
  Per bucket: n, mean curve chance at the first reading after the buy, win rate, mean price paid, held to settlement in dollars, per dollar staked, and the whole-stake losses. Then the A split by curve shape (path efficiency of the 5 minutes before the buy).
First look; buckets chosen before the run. Fees are not in the held figure (the buy fee is in the stake).
    scripts/cloud_job.sh scripts/jump_history_study.py
"""

import bisect
import calendar
import collections
import itertools
import math
import os

from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

os.nice(10)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)

MONDAY = calendar.timegm((2026, 11, 9, 5, 0, 0))  # 11-09 00:00 ET: the logic was rewritten that Monday, nothing earlier counts
GROUPS = {
    "crypto": (("crypto_multiscale",), 300.0, MONDAY),
    "commodity favorites": (("commodity_energy", "commodity_wti", "commodity_precious", "commodity_industrial", "commodity_natgas", "commodity_multiscale"), 120.0, MONDAY),
}
MEM, SEC = 120, 60
BAND = (0.05, 0.89)


def load(lanes, since):
    marks = ",".join("?" * len(lanes))
    rows = st.db.execute(
        "SELECT d.ts,d.ticker,d.lane,d.side,d.contracts,d.stake_usd,d.exit_price,w.close_ts,w.strike,w.result FROM decisions d JOIN windows w ON w.ticker=d.ticker "
        f"WHERE d.action='bet' AND d.mode='trade' AND d.contracts>0 AND d.stake_usd>0 AND d.ts>? AND d.lane IN ({marks}) AND w.result IN ('yes','no') ORDER BY d.ts",
        (since, *lanes),
    ).fetchall()
    seen = {}
    for r in rows:
        seen[(r["ticker"], r["side"])] = r
    return list(seen.values())


def prepare(r):
    """The per-second readings the exit sees from the buy to the close, plus the curve shape at the buy."""
    ser = r["ticker"].split("-")[0]
    crypto = variants.crypto_index(ser)
    ix = crypto or variants.biquote_index(ser)
    if not ix:
        return None
    close, t0 = float(r["close_ts"]), float(r["ts"])
    tk = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts", (ix, t0 - 3700, close)).fetchall()
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (r["ticker"], t0 - 5, close)).fetchall()
    if len(tk) < 1000 or len(q) < 5:
        return None
    T = [float(x[0]) for x in tk]
    V = [float(x[1]) for x in tk]
    if crypto:
        if not r["strike"] or float(r["strike"]) <= 0:
            return None
        ref = math.log(float(r["strike"]))
    else:
        opened = close - 900.0
        a = bisect.bisect_left(T, opened - 5.0)
        if a >= len(T) or T[a] > opened + 20.0:
            return None
        ref = math.log(V[a])
    base = int(T[0])
    g, j, last = [], 0, None
    for t in range(base, int(close) + 1):
        while j < len(T) and T[j] <= t:
            last = V[j]
            j += 1
        g.append(math.log(last) if last else None)
    c60 = [0.0] * len(g)
    for j in range(60, len(g)):
        c60[j] = c60[j - 1] + (((g[j] - g[j - 60]) ** 2) / 60.0 if g[j] is not None and g[j - 60] is not None else 0.0)
    sq = [0.0]
    for x, y in itertools.pairwise(g):
        sq.append(sq[-1] + ((y - x) ** 2 if x is not None and y is not None else 0.0))
    kb = int(t0) - base
    seg = [x for x in g[max(kb - 300, 0) : kb + 1] if x is not None]
    path = sum(abs(y - x) for x, y in itertools.pairwise(seg))
    eff = abs(seg[-1] - seg[0]) / path if len(seg) > 30 and path > 0 else None
    sign = 1.0 if r["side"] == "yes" else -1.0
    qt = [float(x[0]) for x in q]
    moves = collections.deque()
    reads = []
    for t in range(int(t0), int(close) - 2):
        k = t - base
        if k < 960 or k >= len(g) or g[k] is None or g[k - SEC] is None:
            continue
        moves.append((t, (g[k] - g[k - SEC]) * sign))
        while moves and t - moves[0][0] > MEM:
            moves.popleft()
        left = close - t
        if left > 900:
            continue
        rates = (math.sqrt((sq[k] - sq[k - 900]) / 900), math.sqrt((c60[k] - c60[k - 900]) / 900), math.sqrt((c60[k] - c60[k - 180]) / 180))
        if rates[0] <= 0 or rates[1] <= 0 or rates[2] < 0:
            continue
        i = bisect.bisect_right(qt, t) - 1
        if i < 0 or t - qt[i] > 20:
            continue
        yb, ya = float(q[i][1]), float(q[i][2])
        bid = yb if r["side"] == "yes" else 1.0 - ya
        reads.append((t, left, (g[k] - ref) * sign, rates, bid))
    # the market's own last hour before the buy: 1-minute moves, a jump is one over 3 robust sigmas of that hour (1.4826 * median absolute move); A counts the jumps against the side bought in the last 30 minutes
    rets = []
    for j in range(kb, kb - 3600, -60):
        if j - 60 >= 0 and j < len(g) and g[j] is not None and g[j - 60] is not None:
            rets.append((g[j] - g[j - 60]) * sign)
    hist = None
    if len(rets) >= 40:
        mad = sorted(abs(x) for x in rets)[len(rets) // 2] * 1.4826
        if mad > 0:
            hist = (sum(1 for x in rets if abs(x) > 3 * mad), sum(1 for x in rets[:30] if x < -3 * mad))
    return eff, reads, hist


def phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def entry_price(r):
    return float(r["stake_usd"]) / float(r["contracts"])


def stats(rows):
    n = len(rows)
    if n == 0:
        return "n=0"
    stake = sum(x["stake"] for x in rows)
    net = sum(x["net"] for x in rows)
    won = sum(1 for x in rows if x["net"] > 0)
    zero = sum(1 for x in rows if x["net"] < 0)
    return (f"n={n:4d} | chance {sum(x['chance'] for x in rows) / n:.3f} | won {won / n:.3f} | paid {sum(x['paid'] for x in rows) / n:.3f} | win - paid {won / n - sum(x['paid'] for x in rows) / n:+.3f} | "
            f"held {net:+7.2f} | per $1 {net / stake:+.3f} | whole-stake losses {zero}")


for gname, (lanes, _, since) in GROUPS.items():
    bets = load(lanes, since)
    rows = []
    for r in bets:
        got = prepare(r)
        if not got or got[2] is None or not got[1]:
            continue
        eff, reads, (J, A) = got
        t, left, c, rates, bid = reads[0]
        if rates[1] <= 0:
            continue
        n, cost = float(r["contracts"]), float(r["stake_usd"])
        rows.append({"J": J, "A": A, "eff": eff, "chance": phi(c / (rates[1] * math.sqrt(left))), "paid": cost / n, "stake": cost, "net": (n if r["result"] == r["side"] else 0.0) - cost, "ts": float(r["ts"])})
    print(f"\n=== {gname}: lanes {lanes} | settled live bets {len(bets)} | with a full hour of ticks {len(rows)}")
    if len(rows) < 30:
        print("  too few bets")
        continue
    print("  all: " + stats(rows))
    for lab, f in (("J = 0", lambda x: x["J"] == 0), ("J = 1", lambda x: x["J"] == 1), ("J >= 2", lambda x: x["J"] >= 2), ("A = 0", lambda x: x["A"] == 0), ("A >= 1", lambda x: x["A"] >= 1)):
        print(f"  {lab:7s}: " + stats([x for x in rows if f(x)]))
    effs = sorted(x["eff"] for x in rows if x["eff"] is not None)
    lo, hi = effs[len(effs) // 3], effs[2 * len(effs) // 3]
    for sh, f in (("choppy", lambda e: e is not None and e <= lo), ("middle", lambda e: e is not None and lo < e < hi), ("straight", lambda e: e is not None and e >= hi)):
        for lab, g in (("A = 0", lambda x: x["A"] == 0), ("A >= 1", lambda x: x["A"] >= 1)):
            print(f"  {sh:8s} {lab:7s}: " + stats([x for x in rows if f(x["eff"]) and g(x)]))
    mid = sorted(x["ts"] for x in rows)[len(rows) // 2]
    for lab, f in (("first half", lambda x: x["ts"] <= mid), ("second half", lambda x: x["ts"] > mid)):
        print(f"  A >= 1 minus A = 0, per $1, {lab}: " + (lambda a, b: f"{sum(x['net'] for x in a) / max(sum(x['stake'] for x in a), 1e-9) - sum(x['net'] for x in b) / max(sum(x['stake'] for x in b), 1e-9):+.3f} (n {len(a)} vs {len(b)})")([x for x in rows if x["A"] >= 1 and f(x)], [x for x in rows if x["A"] == 0 and f(x)]))
print("DONE")
