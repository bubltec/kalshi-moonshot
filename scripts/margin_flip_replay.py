"""Sell when the underlying has crossed the strike against us by a multiple of the distance we were winning by at entry, in that market's own units. Replay on every live commodity bet of the last DAYS days. Read-only.

At entry the bet's MARGIN is how far the offset-corrected feed price (the feed moved onto Kalshi's reference with the learned offset, commodities.window_basis) is on OUR side of the strike: m0. Later, at each second up to 10 s before the close, the margin is
measured again, positive while we are winning and negative once it has crossed. The rule sells when it is K times m0 on the wrong side (we were $1 above and it is now $2 below the target: K = 2 on a $1 entry margin, 3 on a $1 margin that is now $3 down),
using at least a floor of FLOOR units for m0 so a bet that started on the strike is not judged against a margin of nothing. UNITS are the market's own: one unit is how far the feed typically moves in a minute (the std of its moves per root-second over the 10 minutes
before the window opened, times root 60), so a dollar of oil and a dollar of BTC are not compared, and a quiet market needs a smaller crossing than a wild one. The sale gets the held bid LAT s later less the taker fee. Scored as in exit_tradeoff.py:
LOSS KEPT (the share of the losing bets' dollars still lost, 100% = holding), GAVE UP (winnings given up by selling bets that would have won), NET (recovered minus gave up), PRICE (cents given up per dollar recovered), BACK (the average share of its stake a sold loser got back).

    scripts/cloud.sh run scripts/margin_flip_replay.py [DAYS=7] [LAT=1]
"""
import bisect
import math
import os
import re
import statistics
import time

from kalshi_moonshot import config, db
from kalshi_moonshot.grid import SOURCES
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
DAYS = float(os.environ.get("DAYS", "7"))
LAT = int(os.environ.get("LAT", "1"))
WINDOW_MAX_AGE, MIN_POINTS, UNC_FLOOR = 3 * 3600.0, 6, 0.0003

wins = {}
for w in main.db.execute("SELECT series, close_ts - 900 AS open_ts, strike FROM windows WHERE strike IS NOT NULL AND close_ts > ? ORDER BY close_ts", (time.time() - (DAYS + 1) * 86400,)).fetchall():
    wins.setdefault(w["series"], []).append((float(w["open_ts"]), float(w["strike"])))
feeds = {}


def feed_of(symbol):
    if symbol not in feeds:
        rows = tape.execute("SELECT ts, px FROM feed_ticks WHERE symbol=? AND ts > ? ORDER BY ts", (symbol, time.time() - (DAYS + 2) * 86400)).fetchall()
        feeds[symbol] = ([float(r[0]) for r in rows], [float(r[1]) for r in rows])
    return feeds[symbol]


def px_near(symbol, t, tol=30.0):
    ts, px = feed_of(symbol)
    i = bisect.bisect_right(ts, t) - 1
    return px[i] if i >= 0 and t - ts[i] <= tol else None


def learned_basis(series, symbol, open_ts):
    offs = []
    for ots, strike in wins.get(series, ()):
        if open_ts - WINDOW_MAX_AGE <= ots <= open_ts and strike > 0:
            px = px_near(symbol, ots)
            if px:
                offs.append(math.log(strike / px))
    if len(offs) < MIN_POINTS:
        return None
    med = statistics.median(offs)
    return med, 1.4826 * statistics.median(abs(o - med) for o in offs)


bets = main.db.execute(
    "SELECT d.ts, d.ticker, d.side, d.ask, d.contracts, w.close_ts, w.result, w.strike FROM decisions d JOIN windows w ON w.ticker = d.ticker "
    "WHERE d.lane='commodity' AND d.action='bet' AND d.mode='trade' AND d.ts > ? AND w.result IN ('yes','no') AND d.contracts > 0 AND w.strike IS NOT NULL ORDER BY d.ts", (time.time() - DAYS * 86400,)).fetchall()
data = []
for b in bets:
    series = re.match(r"^(KX[A-Z]+15M)-", b["ticker"]).group(1)
    src = SOURCES.get(series)
    if not src or src[0] != "feed":
        continue
    close = float(b["close_ts"])
    lb = learned_basis(series, src[1], close - 900)
    q = [(float(t), float(bid), float(ask)) for t, bid, ask in tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (b["ticker"], close - 900, close + 1)).fetchall()]
    fts, fpx = feed_of(src[1])
    pre = [(t, p) for t, p in zip(fts[bisect.bisect_left(fts, close - 1500):bisect.bisect_right(fts, close - 900)], fpx[bisect.bisect_left(fts, close - 1500):bisect.bisect_right(fts, close - 900)])]
    moves = [(p1 - p0) / math.sqrt(t1 - t0) for (t0, p0), (t1, p1) in zip(pre, pre[1:]) if 0 < t1 - t0 <= 30]
    sigma = statistics.pstdev(moves) if len(moves) > 10 else 0.0
    if lb is None or len(q) < 10 or sigma <= 0:
        continue
    pts = [(t, bid if b["side"] == "yes" else 1.0 - ask) for t, bid, ask in q]
    data.append((b, close, pts, src[1], lb, sigma))


def at(pts, t):
    i = bisect.bisect_right([p[0] for p in pts], t) - 1
    return pts[i][1] if i >= 0 and t - pts[i][0] <= 8 else None


FLOOR_UNITS = float(os.environ.get("FLOOR", "0.25"))


def margin(b, symbol, lb, t, tol):
    px = px_near(symbol, t, tol)
    if px is None:
        return None
    center = px * math.exp(lb[0])
    strike = float(b["strike"])
    return (center - strike) if b["side"] == "yes" else (strike - center)


def trigger(row, k):
    b, close, pts, symbol, lb, sigma = row
    entry = float(b["ts"])
    m0 = margin(b, symbol, lb, entry, 30.0)
    if m0 is None:
        return None
    unit = sigma * math.sqrt(60.0)
    base = max(m0, FLOOR_UNITS * unit)
    times = [p[0] for p in pts]
    for t in range(int(entry) + 1, int(close - 10)):
        m = margin(b, symbol, lb, t, 20.0)
        if m is not None and m <= -k * base:
            i = bisect.bisect_right(times, t + LAT) - 1
            return pts[i][1] if i >= 0 and t + LAT - times[i] <= 8 else None
    return None


def score(k):
    held_loss = kept = gave = 0.0
    sold = sl = sw = 0
    backs = []
    for row in data:
        b = row[0]
        ask, n = float(b["ask"]), float(b["contracts"])
        won = b["result"] == b["side"]
        hold = ((1.0 if won else 0.0) - ask - fee_per_contract(ask)) * n
        px = trigger(row, k)
        sale = (px - ask - fee_per_contract(ask) - fee_per_contract(px)) * n if px is not None and px > 0 else None
        pnl = sale if sale is not None else hold
        if not won:
            held_loss += -hold
            kept += -pnl
        if sale is not None:
            sold += 1
            if won:
                sw += 1
                gave += hold - sale
            else:
                sl += 1
                backs.append(px / ask)
    rec = held_loss - kept
    return sold, sl, sw, kept / held_loss if held_loss else 0.0, gave, rec - gave, (gave / rec * 100 if rec > 0 else float("nan")), (sum(backs) / len(backs) if backs else 0.0)


nwin = sum(1 for r in data if r[0]["result"] == r[0]["side"])
hl = sum(-(((1.0 if r[0]["result"] == r[0]["side"] else 0.0) - float(r[0]["ask"]) - fee_per_contract(float(r[0]["ask"]))) * float(r[0]["contracts"])) for r in data if r[0]["result"] != r[0]["side"])
print(f"{len(data)} live commodity bets with a feed, a strike and a learned offset: {nwin} won, {len(data) - nwin} lost; holding, the losers cost ${hl:.2f}\n")
print(f"{'crossed by':>12} | {'sold':>4} {'lost':>4} {'won':>4} | {'loss kept':>9} {'gave up':>8} {'NET':>7} {'price':>7} {'back':>5}")
for k in (0.5, 1.0, 1.5, 2.0, 3.0, 4.0):
    sold, sl, sw, kept, gave, net, price, back = score(k)
    print(f"{k:>9.1f}x m0 | {sold:>4} {sl:>4} {sw:>4} | {kept:>8.0%} {gave:>+8.2f} {net:>+7.2f} {price:>6.0f}c {back:>5.0%}")
print("DONE")
