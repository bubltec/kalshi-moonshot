"""Sell into the first bounce only when the underlying is already past the strike against us. Replay on every live commodity bet of the last DAYS days. Read-only.

The bounce rule (see exit_tradeoff.py): after entry the bid falls FALL or more under the price paid, then bounces BOUNCE or more off its low; the sale gets the held bid LAT s later less the taker fee, any time up to 10 s before the close.
The FILTER adds one test when the bounce is seen: the feed price (feed_ticks, newest tick at most 20 s old) moved onto Kalshi's reference with the learned offset (commodities.window_basis: the median of ln(strike / feed price) at the series'
previous 15-minute boundaries) must be on the WRONG side of the strike for the held side by at least Z standard deviations of what the feed can still move in the seconds left (nothing after entry is used for the width). Z = none is the
unfiltered rule; Z = 0 means just past the strike; a negative Z lets a bet still short of the strike through (sell if the center is within |Z| deviations of the strike on the right side). A bounce with no filter pass is skipped and the rule keeps
watching. Scored as in exit_tradeoff.py: LOSS KEPT (share of the losing bets' dollars still lost, 100% = holding), GAVE UP (winnings given up by selling bets that would have won), NET (recovered minus gave up), PRICE (cents given up per dollar recovered).

    scripts/cloud.sh run scripts/bounce_spot_filter.py [DAYS=7] [LAT=1]
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


def zscore(b, close, symbol, lb, sigma, t):
    px = px_near(symbol, t, 20.0)
    if px is None:
        return None
    center = px * math.exp(lb[0])
    strike = float(b["strike"])
    margin = (center - strike) if b["side"] == "yes" else (strike - center)
    width = math.sqrt((sigma * math.sqrt(max(close - t, 1.0))) ** 2 + (max(lb[1], UNC_FLOOR) * center) ** 2)
    return margin / width


def trigger(row, fall, bounce, zcut):
    b, close, pts, symbol, lb, sigma = row
    entry, paid, low = float(b["ts"]), float(b["ask"]), None
    times = [p[0] for p in pts]
    for t in range(int(entry) + 1, int(close - 10)):
        i = bisect.bisect_right(times, t) - 1
        if i < 0 or t - times[i] > 8 or pts[i][1] <= 0.0:
            continue
        bid = pts[i][1]
        low = bid if low is None else min(low, bid)
        if paid - low >= fall and bid - low >= bounce:
            if zcut is not None:
                z = zscore(b, close, symbol, lb, sigma, t)
                if z is None or z > -zcut:
                    continue
            return at(pts, t + LAT) or bid
    return None


def score(fall, bounce, zcut):
    held_loss = kept = gave = 0.0
    sold = sl = sw = 0
    for row in data:
        b = row[0]
        ask, n = float(b["ask"]), float(b["contracts"])
        won = b["result"] == b["side"]
        hold = ((1.0 if won else 0.0) - ask - fee_per_contract(ask)) * n
        px = trigger(row, fall, bounce, zcut)
        sale = (px - ask - fee_per_contract(ask) - fee_per_contract(px)) * n if px is not None and px > 0 else None
        pnl = sale if sale is not None else hold
        if not won:
            held_loss += -hold
            kept += -pnl
        if sale is not None:
            sold += 1
            sw += 1 if won else 0
            sl += 0 if won else 1
            if won:
                gave += hold - sale
    rec = held_loss - kept
    return sold, sl, sw, kept / held_loss if held_loss else 0.0, gave, rec - gave, (gave / rec * 100 if rec > 0 else float("nan"))


nwin = sum(1 for r in data if r[0]["result"] == r[0]["side"])
hl = sum(-(((1.0 if r[0]["result"] == r[0]["side"] else 0.0) - float(r[0]["ask"]) - fee_per_contract(float(r[0]["ask"]))) * float(r[0]["contracts"])) for r in data if r[0]["result"] != r[0]["side"])
print(f"{len(data)} live commodity bets with a feed, a strike and a learned offset: {nwin} won, {len(data) - nwin} lost; holding, the losers cost ${hl:.2f}\n")
print(f"{'fall':>5} {'bounce':>6} {'spot past strike by':>20} | {'sold':>4} {'lost':>4} {'won':>4} | {'loss kept':>9} {'gave up':>8} {'NET':>7} {'price':>7}")
for fall, bounce in ((0.15, 0.04), (0.10, 0.04), (0.25, 0.04)):
    for zc, label in ((None, "no filter"), (-1.0, "within 1 dev of it"), (0.0, "just past (z<=0)"), (0.5, "0.5 dev"), (1.0, "1 dev"), (2.0, "2 dev")):
        sold, sl, sw, kept, gave, net, price = score(fall, bounce, zc)
        print(f"{fall:>5.2f} {bounce:>6.2f} {label:>20} | {sold:>4} {sl:>4} {sw:>4} | {kept:>8.0%} {gave:>+8.2f} {net:>+7.2f} {price:>6.0f}c")
print("DONE")
