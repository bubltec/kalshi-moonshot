"""The shock exit over the whole window, not only the last 300 seconds (2026-11-08, user: "we can't have the shock exit on only in the last 300 s if we are betting the whole market"; 06:21 ET a market-wide jump at 8 minutes left cost two NO bets before the exit was armed).

Question: on the live crypto bets since 11-02 (all crypto lanes, held positions as bought: contracts and side from the decision), how much would the lane's shock exit have changed the result if it ran from the bet to the close (shock_last 900) instead of only in the last 300 seconds? Replay of the rule in watcher._shock_hit with the lane's settings: sell when the worst 60 s move against the bet inside the last 120 s is at least 1.0 typical minutes and the cushion is at most 0.6 typical minutes (the typical minute = the one-second root-mean-square log move of the last 900 s times sqrt(60)), at a bid between 0.05 and 0.89, the first second it holds, at the displayed bid with the full taker fee on the sale. The strike is the window's. Change = sale proceeds - what the position pays at the result (1 per contract if it won, 0 if not), in dollars at the bet's own contracts.
Reported: shock_last 300 (as live) and 900 (the whole window): sells, net change in dollars, sells that beat holding and lost to it, two halves by bet time; and the sells that exist only with 900 (the new ones) by price band of the sale and by minutes left.
Not covered: the exit's trend-hold guard (shock_trend_hold), sells that rest instead of taking, other exits (cross_units is off on these lanes), the real fill (the bid shown is assumed taken whole).

    scripts/hostrun3.sh-style: docker compose exec -T bot python - < scripts/shock_window_replay.py
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
UNITS, MARGIN_UNITS, SECS, MEMORY = 1.0, 0.6, 60, 120
BID_LO, BID_HI = 0.05, 0.89
LASTS = (300, 900)
wins = {w["ticker"]: w for w in st.db.execute("SELECT ticker,series,close_ts,strike,result FROM windows WHERE close_ts>? AND result IN ('yes','no') AND strike>0", (t0,)).fetchall()}
bets = [b for b in st.db.execute("SELECT ts,ticker,lane,side,ask,contracts,stake_usd FROM decisions WHERE action='bet' AND mode='trade' AND lane LIKE 'crypto%' AND ts>? AND contracts>0 ORDER BY ts", (t0,)).fetchall() if b["ticker"] in wins]
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


def replay(b, last_s):
    w = wins[b["ticker"]]
    ser = w["series"]
    ix = variants.crypto_index(ser)
    if not ix:
        return None
    if ix not in GRID:
        GRID[ix] = grid(ix)
    g, sq = GRID[ix]
    close = float(w["close_ts"])
    K = math.log(float(w["strike"]))
    sign = 1.0 if b["side"] == "yes" else -1.0
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (b["ticker"], b["ts"] - 20, close)).fetchall()
    if len(q) < 5:
        return None
    qt = [float(x[0]) for x in q]
    moves = collections.deque()
    for t in range(int(b["ts"]), int(close) - 5):
        k = t - G0
        if k < 960 or g[k] is None or g[k - SECS] is None:
            continue
        move = (g[k] - g[k - SECS]) * sign
        moves.append((t, move))
        while moves and t - moves[0][0] > MEMORY:
            moves.popleft()
        left = close - t
        if left > last_s:
            continue
        rate = math.sqrt((sq[k] - sq[k - 900]) / 900)
        if rate <= 0:
            continue
        unit = rate * math.sqrt(60.0)
        worst = min(m for _, m in moves)
        cushion = (g[k] - K) * sign
        if not (worst <= -UNITS * unit and cushion <= MARGIN_UNITS * unit):
            continue
        i = bisect.bisect_right(qt, t) - 1
        if i < 0 or t - qt[i] > 20:
            continue
        bid = float(q[i][1]) if b["side"] == "yes" else 1.0 - float(q[i][2])
        if not (BID_LO <= bid <= BID_HI):
            continue
        n = float(b["contracts"])
        sale = n * bid - n * fee(bid)
        hold = n if w["result"] == b["side"] else 0.0
        return {"t": t, "left": left, "bid": bid, "change": sale - hold, "bet_ts": b["ts"], "lane": b["lane"]}
    return None


mid = sorted(b["ts"] for b in bets)[len(bets) // 2]
out = {}
for L in LASTS:
    out[L] = [r for r in (replay(b, L) for b in bets) if r]


def line(label, rs):
    if not rs:
        print(f"   {label:34s} n=0")
        return
    ch = [r["change"] for r in rs]
    a = [r["change"] for r in rs if r["bet_ts"] <= mid]
    b = [r["change"] for r in rs if r["bet_ts"] > mid]
    print(f"   {label:34s} sells {len(rs):3d} | net change ${sum(ch):+8.2f} | beat hold {sum(1 for c in ch if c > 0)} lost {sum(1 for c in ch if c <= 0)} | halves ${sum(a):+.2f} (n={len(a)}) / ${sum(b):+.2f} (n={len(b)})")


print("\nthe exit replayed over the live crypto bets (change vs holding to the result):")
for L in LASTS:
    line(f"shock_last {L}", out[L])
keys300 = {(r["bet_ts"], r["lane"]) for r in out[300]}
new = [r for r in out[900] if (r["bet_ts"], r["lane"]) not in keys300]
print("\nthe sells that exist only with the whole window (not sold in the last 300 s either):")
line("new sells", new)
for lo, hi in ((300, 480), (480, 660), (660, 900)):
    line(f"new, {lo // 60}-{hi // 60} min left", [r for r in new if lo < r["left"] <= hi])
for lo, hi in ((0.05, 0.2), (0.2, 0.45), (0.45, 0.9)):
    line(f"new, sale bid {lo:.2f}-{hi:.2f}", [r for r in new if lo <= r["bid"] < hi])
early = [r for r in out[900] if (r["bet_ts"], r["lane"]) in keys300]
print("\nbets that the last-300 s window already sold, now sold earlier:")
line("same bets, earlier sale", early)
by = collections.defaultdict(list)
for r in new:
    by[r["lane"]].append(r)
print("\nnew sells by lane:")
for lane, rs in sorted(by.items()):
    line(lane, rs)
print("DONE")
