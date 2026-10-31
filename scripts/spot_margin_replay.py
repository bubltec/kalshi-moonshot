"""A cash-out that sells a bet the SPOT price says is lost, not the 15-minute market's price. Replay on every live commodity bet of the last DAYS days. Read-only.

For each second t in the last WITHIN seconds of a bet: the spot is the newest feed price (feed_ticks, the hl:xyz perpetuals the bot prices from) at most 20 s old. Kalshi settles on its own reference, not the feed, so the spot is
moved onto that reference the way the bot does (commodities.window_basis): the center is spot x exp(basis), the basis being the median of ln(strike / feed price) at the series' previous 15-minute boundaries (a window's strike is
Kalshi's reference at its open; at least 6 boundaries, none older than 3 h before the bet's window opened, the bet's own open included). The margin is how far the center is on the WRONG side of the strike for the held side (YES needs
it over the strike, NO under it), and z is that margin in units of the width left: the feed's normal wander over the seconds left (std of its moves per root-second over the 10 minutes before the window opened, so nothing after
entry) combined with the basis uncertainty (the spread of those offsets, at least 3 bp). The rule SELLS at the held bid LAT seconds later (less the taker fee) the first second z <= -Z. A bet never sold settles at 1 or 0. "saved" = sold bets that settled as a loss, "cost" = sold bets that would have
WON, "got back" = a saved bet's sale proceeds as a share of its stake. "vs holding" is the rule's P&L minus holding. The exits the lane already runs are not modeled.

    scripts/cloud.sh run scripts/spot_margin_replay.py [DAYS=7] [LAT=1]
"""
import bisect
import itertools
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
bets = main.db.execute(
    "SELECT d.ticker, d.side, d.ask, d.contracts, w.close_ts, w.result, w.strike FROM decisions d JOIN windows w ON w.ticker = d.ticker "
    "WHERE d.lane='commodity' AND d.action='bet' AND d.mode='trade' AND d.ts > ? AND w.result IN ('yes','no') AND d.contracts > 0 AND w.strike IS NOT NULL ORDER BY d.ts", (time.time() - DAYS * 86400,)).fetchall()
BOUNDARIES, WINDOW_MAX_AGE, MIN_POINTS, UNC_FLOOR = 15 * 60, 3 * 3600.0, 6, 0.0003
wins = {}  # series -> [(open_ts, strike)]
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
    """(basis, spread, n) from the boundaries up to this window's open, or None."""
    offs = []
    for ots, strike in wins.get(series, ()):
        if open_ts - WINDOW_MAX_AGE <= ots <= open_ts and strike > 0:
            px = px_near(symbol, ots)
            if px:
                offs.append(math.log(strike / px))
    if len(offs) < MIN_POINTS:
        return None
    med = statistics.median(offs)
    return med, 1.4826 * statistics.median(abs(o - med) for o in offs), len(offs)


data = []
for b in bets:
    series = re.match(r"^(KX[A-Z]+15M)-", b["ticker"]).group(1)
    src = SOURCES.get(series)
    if not src or src[0] != "feed":
        continue
    close, strike = float(b["close_ts"]), float(b["strike"])
    open_ts = close - BOUNDARIES
    lb = learned_basis(series, src[1], open_ts)
    fts, fpx = feed_of(src[1])
    lo, hi = bisect.bisect_left(fts, open_ts - 600), bisect.bisect_right(fts, close + 1)
    ticks = list(zip(fts[lo:hi], fpx[lo:hi]))
    if lb is None:
        continue
    q = [(float(t), float(bid), float(ask)) for t, bid, ask in tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (b["ticker"], close - 130, close + 1)).fetchall()]
    if len(ticks) < 20 or len(q) < 5:
        continue
    pre = [x for x in ticks if x[0] <= open_ts]
    moves = [(p1 - p0) / math.sqrt(t1 - t0) for (t0, p0), (t1, p1) in zip(pre, pre[1:]) if 0 < t1 - t0 <= 30]
    sigma = statistics.pstdev(moves) if len(moves) > 10 else 0.0
    if sigma <= 0:
        continue
    held = lambda t, q=q, side=b["side"]: next(((x[1] if side == "yes" else 1.0 - x[2]) for x in reversed(q) if x[0] <= t), None)  # noqa: E731
    spot = lambda t, ticks=ticks: next(((px) for ts, px in reversed(ticks) if ts <= t and t - ts <= 20), None)  # noqa: E731
    data.append((b, close, strike, sigma, held, spot, lb))


def base(b):
    won = b["result"] == b["side"]
    ask, n = float(b["ask"]), float(b["contracts"])
    return ((1.0 if won else 0.0) - ask - fee_per_contract(ask)) * n


def run(within, z_cut, bid_min=0.0):
    total = held_total = 0.0
    sold = saved = cost = 0
    got = []
    for b, close, strike, sigma, held, spot, lb in data:
        ask, n = float(b["ask"]), float(b["contracts"])
        pnl = base(b)
        for t in range(int(close - within), int(close)):
            px = spot(t)
            if px is None:
                continue
            center = px * math.exp(lb[0])
            margin = (center - strike) if b["side"] == "yes" else (strike - center)
            width = math.sqrt((sigma * math.sqrt(max(close - t, 1.0))) ** 2 + (max(lb[1], UNC_FLOOR) * center) ** 2)
            z = margin / width
            if z <= -z_cut:
                bid, fill = held(t), held(t + LAT)
                if bid is None or fill is None or fill <= 0.02 or bid < bid_min:
                    continue
                pnl = (fill - ask - fee_per_contract(ask) - fee_per_contract(fill)) * n
                sold += 1
                if b["result"] == b["side"]:
                    cost += 1
                else:
                    saved += 1
                    got.append(fill / ask)
                break
        total += pnl
        held_total += base(b)
    return sold, saved, cost, (sum(got) / len(got) if got else 0.0), total - held_total


print(f"{len(data)} live commodity bets over {DAYS:g} days with a feed, a strike and a learned offset; holding every one to settlement: {sum(base(b) for b, *_ in data):+.2f} dollars; settled as a loss: {sum(1 for b, *_ in data if b['result'] != b['side'])}\n")
print("(bid at least = the market has NOT yet caught up to the spot: the sale is only made while the held bid is still this high)")
print(f"{'within':>6} {'z <=':>5} {'bid>=':>5} | {'sold':>4} {'saved':>5} {'cost':>4} {'got back':>8} | {'vs holding':>10}")
for within, z, bm in itertools.product((60, 120), (0.5, 1.0, 1.5), (0.0, 0.25, 0.40, 0.55)):
    sold, saved, cost, back, vs = run(within, z, bm)
    print(f"{within:>5}s {-z:>5.1f} {bm:>5.2f} | {sold:>4} {saved:>5} {cost:>4} {back:>7.0%} | {vs:>+10.2f}")
print("DONE")
