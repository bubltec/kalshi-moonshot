"""Catching a jump: an exit for a fast, large move against a held bet, any time in the window (2026-11-08, user: "how do we catch those flips ... a bot could"; 06:21 ET every coin jumped 30 to 140 bp within about 20 seconds, the NO side of NEAR went from 0.35 to 0.04 in 18 s, and the lane's shock exit is only armed in the last 300 s).

Question: does selling a held crypto bet when the spot has moved against it by J typical minutes within SECS seconds (a jump far larger than a normal wobble: a Brownian move of 15 s is about 0.5 typical minutes), at any time left, beat holding on the live bets and add to the live exit? A second variant needs breadth: the same jump, and at least 3 of the 9 coins moved at least 1 typical minute in the same direction in 30 s (a market-wide event, not one coin's noise).
Setup: the 739 live crypto bets since 11-02 (contracts, side from the decision; strike from the window). The typical minute = one-second root-mean-square log move of the last 900 s times sqrt(60). Sale at the bid shown LAG seconds after the trigger (LAG 2 s: the bid collapses in seconds, so a sale that reads the bid at the trigger second is too kind), bid between 0.05 and 0.89, full taker fee. Change = sale proceeds - what the position pays at the result. J in 1.5, 2, 3 and SECS in 15, 30 are a grid.
Reported: each variant alone vs holding; added to the live exit (sold earlier than it, or where it did not sell); two halves by bet time; by minutes left at the sale; by lane.
Not covered: real fills (a sale needs the bid to be there), the trend-hold guard, sells that rest.

    scripts/hostrun3.sh-style: docker compose exec -T bot python - < scripts/jump_exit_replay.py
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
UNITS, MARGIN_UNITS, SECS_L, MEMORY, LAST = 1.0, 0.6, 60, 120, 300
BID_LO, BID_HI = 0.05, 0.89
LAG = 2
JS, SECSS = (1.5, 2.0, 3.0), (15, 30)
CRYPTO = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M", "KXNEAR15M", "KXBNB15M", "KXHYPE15M", "KXZEC15M")
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


IDS = [variants.crypto_index(x) for x in CRYPTO]
for ix in IDS:
    if ix and ix not in GRID:
        GRID[ix] = grid(ix)


def adverse(ix, k, secs, sign):
    """The move against a bet of direction `sign` over `secs`, in that coin's typical minutes (positive = against)."""
    g, sq = GRID[ix]
    if k < 960 or g[k] is None or g[k - secs] is None:
        return 0.0
    rate = math.sqrt((sq[k] - sq[k - 900]) / 900)
    if rate <= 0:
        return 0.0
    return -(g[k] - g[k - secs]) * sign / (rate * math.sqrt(60.0))


def one(b):
    w = wins[b["ticker"]]
    ix = variants.crypto_index(w["series"])
    if not ix:
        return None
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
    jump = {}

    def sale(t_):
        i = bisect.bisect_right(qt, t_) - 1
        if i < 0 or t_ - qt[i] > 20:
            return None
        bid = float(q[i][1]) if b["side"] == "yes" else 1.0 - float(q[i][2])
        if not (BID_LO <= bid <= BID_HI):
            return None
        return bid, n * bid - n * fee(bid) - hold

    for t in range(int(b["ts"]), int(close) - 15 - LAG):
        k = t - G0
        if k < 960 or g[k] is None or g[k - SECS_L] is None:
            continue
        moves.append((t, (g[k] - g[k - SECS_L]) * sign))
        while moves and t - moves[0][0] > MEMORY:
            moves.popleft()
        left = close - t
        rate = math.sqrt((sq[k] - sq[k - 900]) / 900)
        if rate <= 0:
            continue
        unit = rate * math.sqrt(60.0)
        if live is None and left <= LAST:
            cushion = (g[k] - K) * sign
            if min(m for _, m in moves) <= -UNITS * unit and cushion <= MARGIN_UNITS * unit:
                sl = sale(t)
                if sl:
                    live = (t, left, sl[0], sl[1])
        for secs in SECSS:
            adv = adverse(ix, k, secs, sign)
            for J in JS:
                if adv < J:
                    continue
                for breadth in (False, True):
                    key = (secs, J, breadth)
                    if key in jump:
                        continue
                    if breadth and sum(1 for o in IDS if o and adverse(o, k, 30, sign) >= 1.0) < 3:
                        continue
                    sl = sale(t + LAG)
                    if sl:
                        jump[key] = (t, left, sl[0], sl[1])
        if live is not None and len(jump) == len(JS) * len(SECSS) * 2:
            break
    return live, jump


res = []
for b in bets:
    r = one(b)
    if r:
        res.append((b, r[0], r[1]))
mid = sorted(b["ts"] for b, _, _ in res)[len(res) // 2]
print("bets replayed:", len(res), "| live exit sells:", sum(1 for _, lv, _ in res if lv))


def line(label, items):
    if not items:
        print(f"   {label:40s} n=0")
        return
    ch = [c for _, c in items]
    a = [c for b, c in items if b["ts"] <= mid]
    h = [c for b, c in items if b["ts"] > mid]
    print(f"   {label:40s} n={len(items):3d} | net ${sum(ch):+8.2f} | beat hold {sum(1 for c in ch if c > 0)} lost {sum(1 for c in ch if c <= 0)} | halves ${sum(a):+.2f} (n={len(a)}) / ${sum(h):+.2f} (n={len(h)})")


line("live shock exit (last 300 s) vs holding", [(b, lv[3]) for b, lv, _ in res if lv])
for breadth in (False, True):
    for secs in SECSS:
        for J in JS:
            key = (secs, J, breadth)
            print(f"\n{'breadth (3+ coins) ' if breadth else 'one coin '}jump >= {J} typical minutes in {secs} s, sale {LAG} s later")
            alone = [(b, jp[key][3]) for b, _, jp in res if key in jp]
            line("alone vs holding", alone)
            added = [(b, jp[key], lv) for b, lv, jp in res if key in jp and (lv is None or jp[key][0] < lv[0])]
            val = lambda ev, lv: ev[3] - (lv[3] if lv else 0.0)  # noqa: E731
            line("added to the live exit", [(b, val(ev, lv)) for b, ev, lv in added])
            for lo, hi in ((15, 300), (300, 600), (600, 900)):
                line(f"  added, {lo}-{hi} s left", [(b, val(ev, lv)) for b, ev, lv in added if lo < ev[1] <= hi])
            by = collections.defaultdict(list)
            for b, ev, lv in added:
                by[b["lane"]].append((b, val(ev, lv)))
            for lane, it in sorted(by.items()):
                if len(it) >= 3:
                    line(f"  added, {lane}", it)
print("DONE")
