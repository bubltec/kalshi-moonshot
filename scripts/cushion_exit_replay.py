"""An early exit read from the cushion over the time left (2026-11-08, user: "yes run that study" after scripts/shock_window_replay.py showed the shock exit armed over the whole window loses -$56.55 on 43 extra sells).

Question: when a held crypto side's cushion (the spot's distance to the strike on the bet's side) has gone so far against it that the seconds left cannot plausibly bring it back, does selling then beat holding, and does it add to the live shock exit (armed in the last 300 s)? z = cushion / (rate x sqrt(seconds left)), rate = the one-second root-mean-square log move of the last 900 s; z is the cushion in units of what the remaining time can move, the win chance of the side is about Phi(z). The exit sells the first second z <= -Z at a bid between 0.05 and 0.89, at the displayed bid with the full taker fee. Z in 0.5, 1.0, 1.5, 2.0, 3.0 is a grid, not a choice after the fact.
Setup: the 739 live crypto bets since 11-02 (contracts and side from the decision; the strike from the window). Per bet: the live exit alone (the replay of the lane's shock rule, shock_last 300), the cushion exit alone, and both together (whichever sells first). Change = sale proceeds - what the position pays at the result.
Reported per Z: cushion exit alone vs holding; both together vs the live exit alone (the added value), sells it adds, two halves by bet time, by minutes left at the sale, by bid at the sale, by lane.
Not covered: the trend-hold guard, sells that rest, real fills, queue.

    scripts/hostrun3.sh-style: docker compose exec -T bot python - < scripts/cushion_exit_replay.py
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
UNITS, MARGIN_UNITS, SECS, MEMORY, LAST = 1.0, 0.6, 60, 120, 300
BID_LO, BID_HI = 0.05, 0.89
ZS = (0.5, 1.0, 1.5, 2.0, 3.0)
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


def one(b):
    """(live exit event or None, {Z: cushion exit event}, hold change baseline 0). An event = (second, left, bid, change vs hold)."""
    w = wins[b["ticker"]]
    ix = variants.crypto_index(w["series"])
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
    n = float(b["contracts"])
    hold = n if w["result"] == b["side"] else 0.0
    moves = collections.deque()
    live = None
    cz = {}
    for t in range(int(b["ts"]), int(close) - 15):
        k = t - G0
        if k < 960 or g[k] is None or g[k - SECS] is None:
            continue
        moves.append((t, (g[k] - g[k - SECS]) * sign))
        while moves and t - moves[0][0] > MEMORY:
            moves.popleft()
        left = close - t
        rate = math.sqrt((sq[k] - sq[k - 900]) / 900)
        if rate <= 0:
            continue
        i = bisect.bisect_right(qt, t) - 1
        if i < 0 or t - qt[i] > 20:
            continue
        bid = float(q[i][1]) if b["side"] == "yes" else 1.0 - float(q[i][2])
        if not (BID_LO <= bid <= BID_HI):
            continue
        change = n * bid - n * fee(bid) - hold
        cushion = (g[k] - K) * sign
        if live is None and left <= LAST:
            unit = rate * math.sqrt(60.0)
            if min(m for _, m in moves) <= -UNITS * unit and cushion <= MARGIN_UNITS * unit:
                live = (t, left, bid, change)
        z = cushion / (rate * math.sqrt(left))
        for Z in ZS:
            if Z not in cz and z <= -Z:
                cz[Z] = (t, left, bid, change)
        if live is not None and len(cz) == len(ZS):
            break
    return live, cz


res = []
for b in bets:
    r = one(b)
    if r:
        res.append((b, r[0], r[1]))
mid = sorted(b["ts"] for b, _, _ in res)[len(res) // 2]
print("bets replayed:", len(res), "| live exit sells:", sum(1 for _, lv, _ in res if lv))


def line(label, items):
    """items: (bet, change) pairs."""
    if not items:
        print(f"   {label:38s} n=0")
        return
    ch = [c for _, c in items]
    a = [c for b, c in items if b["ts"] <= mid]
    h = [c for b, c in items if b["ts"] > mid]
    print(f"   {label:38s} n={len(items):3d} | net ${sum(ch):+8.2f} | beat hold {sum(1 for c in ch if c > 0)} lost {sum(1 for c in ch if c <= 0)} | halves ${sum(a):+.2f} (n={len(a)}) / ${sum(h):+.2f} (n={len(h)})")


print("\nthe live exit alone vs holding:")
line("live shock exit (last 300 s)", [(b, lv[3]) for b, lv, _ in res if lv])
for Z in ZS:
    print(f"\nZ = {Z}  (sell the first second z <= -{Z})")
    alone = [(b, cz[Z][3]) for b, _, cz in res if Z in cz]
    line("cushion exit alone vs holding", alone)
    added = []
    for b, lv, cz in res:
        ev = cz.get(Z)
        if ev and (lv is None or ev[0] < lv[0]):
            added.append((b, ev, lv))
    # the added value of the cushion exit on top of the live exit: its change minus what the live exit (or holding) gave on that bet
    line("added to the live exit (sold earlier)", [(b, ev[3] - (lv[3] if lv else 0.0)) for b, ev, lv in added])
    for lo, hi in ((15, 120), (120, 300), (300, 600), (600, 900)):
        line(f"  added, {lo}-{hi} s left", [(b, ev[3] - (lv[3] if lv else 0.0)) for b, ev, lv in added if lo < ev[1] <= hi])
    for lo, hi in ((0.05, 0.2), (0.2, 0.45), (0.45, 0.9)):
        line(f"  added, sale bid {lo:.2f}-{hi:.2f}", [(b, ev[3] - (lv[3] if lv else 0.0)) for b, ev, lv in added if lo <= ev[2] < hi])
    by = collections.defaultdict(list)
    for b, ev, lv in added:
        by[b["lane"]].append((b, ev[3] - (lv[3] if lv else 0.0)))
    for lane, it in sorted(by.items()):
        line(f"  added, {lane}", it)
print("DONE")
