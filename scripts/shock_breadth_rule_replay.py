"""A breadth check and a 3 s persistence for the shock exit (2026-11-08, user: "the crypto markets move together in those shock moments ... so we can use it as a check that we don't need to sell, or if it stays for 3 s on the check we should sell"). Follows scripts/shock_breadth_study.py (shock sales with 0-1 of the nine coins moving against the bet lost to holding, -0.123 +/- 0.183; with 4-6 coins they beat holding, +0.891 +/- 0.195).
Rule (a grid, fixed before the results): when the lane's shock conditions hold at second t (the worst 60 s move over 120 s at least 1.0 typical minutes against the bet, the cushion within 0.6 typical minutes, last 300 s; the same as watcher._shock_hit), count B = how many of the nine coins (settlement indexes) moved a typical minute or more in the bet's adverse direction over the last 60 s (the coin itself included). Sell now if B >= K. Otherwise (an isolated shock) do not sell, and sell only once the shock conditions have held for P consecutive seconds. K in 2 and 4; P in 3 and 5. The current exit is K = 0 or P = 0: sell at the first second the conditions hold.
Replay on the live crypto bets since 11-02 (contracts, side from the decision; strike and result from the window): the sale at the displayed bid (0.05-0.89 band) at the sale second less the full sell fee; a bet the rule never sells is held to the result. Per variant: the net against holding, against the current exit (the difference per bet it changes), how many bets it sells later / not at all, the two halves each with a standard error.
Not covered: the real fill (the bid can slide in the 3 s a persistence waits, which is the cost of waiting and is in the replay as the bid at that second), other exits running alongside, a rule that is wrong for bets bought after the move.

    scripts/hostrun3.sh-style: docker compose exec -T bot python - < scripts/shock_breadth_rule_replay.py
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

os.nice(15)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
t0 = calendar.timegm((2026, 11, 2, 6, 0, 0))
SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M", "KXNEAR15M", "KXBNB15M", "KXHYPE15M", "KXZEC15M")
VARIANTS = [(k, p) for k in (2, 4) for p in (3, 5)]
wins = {w["ticker"]: w for w in st.db.execute("SELECT ticker,series,close_ts,strike,result FROM windows WHERE close_ts>? AND result IN ('yes','no') AND strike>0", (t0,)).fetchall()}
bets = [b for b in st.db.execute("SELECT ts,ticker,lane,side,contracts FROM decisions WHERE action='bet' AND mode='trade' AND lane LIKE 'crypto%' AND ts>? AND contracts>0 ORDER BY ts", (t0,)).fetchall() if b["ticker"] in wins]
print("live crypto bets with a settled window:", len(bets))
G0 = int(t0 - 1200)
G1 = int(max(float(w["close_ts"]) for w in wins.values())) + 2
GRID = {}


def grid(ix):
    r = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (ix, G0 - 60)).fetchall()
    ts = [float(x[0]) for x in r]
    vs = [float(x[1]) for x in r]
    g, j, last = [], 0, None
    for t in range(G0, G1):
        while j < len(ts) and ts[j] <= t:
            last = vs[j]
            j += 1
        g.append(math.log(last) if last else None)
    sq = [0.0]
    for a, b in itertools.pairwise(g):
        sq.append(sq[-1] + ((b - a) ** 2 if a is not None and b is not None else 0.0))
    return g, sq


IDS = [variants.crypto_index(x) for x in SERIES]
for ix in IDS:
    if ix:
        GRID[ix] = grid(ix)


def adverse60(ix, k, sign):
    g, sq = GRID[ix]
    if k < 960 or g[k] is None or g[k - 60] is None:
        return 0.0
    rate = math.sqrt((sq[k] - sq[k - 900]) / 900)
    return -(g[k] - g[k - 60]) * sign / (rate * math.sqrt(60.0)) if rate > 0 else 0.0


def one(b):
    w = wins[b["ticker"]]
    ix = variants.crypto_index(w["series"])
    g, sq = GRID[ix]
    close = float(w["close_ts"])
    K0 = math.log(float(w["strike"]))
    sign = 1.0 if b["side"] == "yes" else -1.0
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (b["ticker"], b["ts"] - 20, close)).fetchall()
    if len(q) < 5:
        return None
    qt = [float(x[0]) for x in q]
    n = float(b["contracts"])
    hold = n if w["result"] == b["side"] else 0.0

    def sale(t):
        i = bisect.bisect_right(qt, t) - 1
        if i < 0 or t - qt[i] > 20:
            return None
        bid = float(q[i][1]) if b["side"] == "yes" else 1.0 - float(q[i][2])
        return n * (bid - fee(bid)) - hold if 0.05 <= bid <= 0.89 else None

    moves = collections.deque()
    run = 0  # consecutive seconds the conditions have held
    first = None
    done = dict.fromkeys(VARIANTS)
    for t in range(int(b["ts"]), int(close) - 5):
        k = t - G0
        if k < 960 or g[k] is None or g[k - 60] is None:
            continue
        moves.append((t, (g[k] - g[k - 60]) * sign))
        while moves and t - moves[0][0] > 120:
            moves.popleft()
        rate = math.sqrt((sq[k] - sq[k - 900]) / 900)
        if rate <= 0 or close - t > 300:
            run = 0
            continue
        unit = rate * math.sqrt(60.0)
        cond = min(m for _, m in moves) <= -unit and (g[k] - K0) * sign <= 0.6 * unit
        run = run + 1 if cond else 0
        if not cond:
            continue
        sl = sale(t)
        if first is None and sl is not None:
            first = (t, sl)
        bcount = None
        for v in VARIANTS:
            if done[v] is not None:
                continue
            if bcount is None:
                bcount = sum(1 for o in IDS if o and adverse60(o, k, sign) >= 1.0)
            if bcount >= v[0] or run >= v[1]:
                if sl is not None:
                    done[v] = (t, sl)
        if first is not None and all(done[v] is not None for v in VARIANTS):
            break
    return b["ts"], hold, first, done


res = [r for r in (one(b) for b in bets) if r]
mid = sorted(r[0] for r in res)[len(res) // 2]
base_net = [(r[2][1] if r[2] else r[1]) for r in res]
print("bets replayed:", len(res), "| the current exit sells:", sum(1 for r in res if r[2]))


def ms(xs):
    n = len(xs)
    if n == 0:
        return "n=0"
    m = sum(xs) / n
    se = (sum((x - m) ** 2 for x in xs) / max(n - 1, 1)) ** 0.5 / math.sqrt(n)
    return f"{m:+.3f} +/- {se:.3f}"


print(f"\nthe current exit (sell at the first second the shock conditions hold) vs holding: net {sum((r[2][1] if r[2] else 0.0) for r in res):+.2f} over {len(res)} bets")
for v in VARIANTS:
    diffs, later, never = [], 0, 0
    a, h = [], []
    for r in res:
        cur = r[2][1] if r[2] else 0.0
        new = r[3][v][1] if r[3][v] else 0.0
        d = new - cur
        if d != 0 or (r[2] and r[3][v] and r[2][0] != r[3][v][0]):
            diffs.append(d)
            if r[2] and not r[3][v]:
                never += 1
            elif r[2] and r[3][v] and r[3][v][0] > r[2][0]:
                later += 1
        (a if r[0] <= mid else h).append(d)
    tot = sum(diffs)
    print(f"  K={v[0]} (sell at once at {v[0]}+ coins), P={v[1]} s (else wait): bets changed {len(diffs)} (sold later {later}, never {never}) | net vs the current exit {tot:+.2f} | per changed bet {ms(diffs)} | first half {sum(a):+.2f} (n={len(a)}) / second half {sum(h):+.2f} (n={len(h)})")
print("DONE")
