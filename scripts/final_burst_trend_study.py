"""Follow the burst: when the last seconds of a 15-minute market see a burst of volume, buy the side the takers are buying. Replayed from the tape.

Part 1, how unusual was the NATGAS finish: the contracts traded in the last 12 seconds as a share of everything traded in the market, for this market and for every settled market.

Part 2, the trade. For every settled market, each second from 20 to 3 seconds before the close is checked. A burst is a 3-second window with at least MIN_CONTRACTS contracts taken AND at least SHARE of
all the contracts traded in the market so far, with a clear direction: takers on one side bought at least 60% of that window's contracts. The first burst of a market triggers a taker buy of that side
LAT seconds later (decision to fill; the live buys take about 1.75 s) at the ask then, held to settlement, taker fee paid. If that ask is already above MAX_ASK (0.97) the ticket is skipped (nothing
left to win). A ticket wins if its side settles in its favor and pays $1. P&L is per ticket in cents and ROI is on the price paid; rows split by the price paid.

    scripts/cloud.sh run scripts/final_burst_trend_study.py [DAYS=3] [TICKER=KXNATGAS15M-26OCT291730-30] [LAT=2]
"""
import os
import time

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)

DAYS = float(os.environ.get("DAYS", "3"))
LAT = int(os.environ.get("LAT", "2"))
TICKER = os.environ.get("TICKER", "KXNATGAS15M-26OCT291730-30")
MIN_CONTRACTS, MAX_ASK = 300, 0.97
SHARES = (0.03, 0.06, 0.12)
BUCKETS = ((0.0, 0.50, "<= 0.50"), (0.50, 0.80, "0.50-0.80"), (0.80, 0.9701, "0.80-0.97"))

since = time.time() - DAYS * 86400
wrows = main.db.execute("SELECT ticker, series, close_ts, result FROM windows WHERE result IN ('yes','no') AND close_ts >= ? ORDER BY close_ts", (since,)).fetchall()

shares12 = []  # (last 12 s contracts / all contracts) per market
detail = {}
tickets = {sh: [] for sh in SHARES}  # (price paid, won, pnl cents)
skipped = {sh: 0 for sh in SHARES}
markets = 0
for w in wrows:
    close = float(w["close_ts"])
    flow = [(int(t), float(y or 0), float(n or 0)) for t, y, n in tape.execute("SELECT ts, yes_vol, no_vol FROM flow_1s WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], int(close - 905), int(close))).fetchall()]
    total = sum(y + n for _t, y, n in flow)
    if total < 50:
        continue
    quotes = [(float(t), float(b), float(a)) for t, b, a in tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], close - 40, close)).fetchall()]
    if len(quotes) < 5:
        continue
    markets += 1
    last12 = sum(y + n for t, y, n in flow if t > close - 12)
    shares12.append(last12 / total)
    if w["ticker"] == TICKER:
        detail = {"total": total, "last12": last12, "share": last12 / total, "per_min_before": (total - last12) / ((900 - 12) / 60.0)}

    def ask_at(t, side):
        got = [q for q in quotes if q[0] <= t and t - q[0] <= 6]
        if not got:
            return None
        return got[-1][2] if side == "yes" else 1.0 - got[-1][1]

    for sh in SHARES:
        for t in range(int(close - 20), int(close - 3)):
            win = [(y, n) for ft, y, n in flow if t - 3 < ft <= t]
            vol = sum(y + n for y, n in win)
            sofar = sum(y + n for ft, y, n in flow if ft <= t)
            if vol < MIN_CONTRACTS or sofar <= 0 or vol / sofar < sh:
                continue
            yv, nv = sum(y for y, _n in win), sum(n for _y, n in win)
            if max(yv, nv) / vol < 0.6:
                continue
            side = "yes" if yv > nv else "no"
            ask = ask_at(t + LAT, side)
            if ask is None or ask > MAX_ASK:
                skipped[sh] += 1
            else:
                won = w["result"] == side
                tickets[sh].append((ask, won, (100.0 if won else 0.0) - ask * 100 - fee_per_contract(ask) * 100))
            break

shares12.sort()
q = lambda p: shares12[min(len(shares12) - 1, int(p * len(shares12)))]  # noqa: E731
print(f"PART 1, {markets} settled markets with flow: contracts in the last 12 s as a share of the whole market's volume")
if detail:
    print(f"  {TICKER}: {detail['last12']:,.0f} of {detail['total']:,.0f} contracts = {detail['share']:.0%} (the 14.8 minutes before averaged {detail['per_min_before']:,.0f} a minute)")
print(f"  all markets: median {q(0.5):.1%}, 75th {q(0.75):.1%}, 90th {q(0.9):.1%}, 99th {q(0.99):.1%}; {sum(1 for x in shares12 if detail and x >= detail['share'])} markets at or above the NATGAS share")
print(f"\nPART 2, buy the burst's direction {LAT} s after it, held to settlement (min {MIN_CONTRACTS} contracts in 3 s, clear direction)")
print(f"{'burst >= % of market':>20} {'bursts':>7} {'too late':>8} {'bought':>7} {'wins':>5} {'win%':>6} {'avg price':>9} {'P&L/ticket':>11} {'ROI':>7}", flush=True)
for sh in SHARES:
    rows = tickets[sh]
    n = len(rows)
    if not n:
        print(f"{sh:>19.0%} {skipped[sh]:>16}")
        continue
    wins = sum(1 for _a, w, _p in rows if w)
    price = sum(a for a, _w, _p in rows) / n * 100
    pnl = sum(p for _a, _w, p in rows) / n
    print(f"{sh:>19.0%} {n + skipped[sh]:>7} {skipped[sh]:>8} {n:>7} {wins:>5} {wins / n:>6.1%} {price:>8.1f}c {pnl:>+10.2f}c {pnl / price:>+7.0%}")
print("\nby price paid (smallest share bar):")
rows = tickets[SHARES[0]]
for lo, hi, label in BUCKETS:
    g = [r for r in rows if lo < r[0] <= hi] if lo > 0 else [r for r in rows if r[0] <= hi]
    if g:
        n = len(g)
        price = sum(a for a, _w, _p in g) / n * 100
        pnl = sum(p for _a, _w, p in g) / n
        print(f"  {label:>10}: {n:>4} tickets, {sum(1 for _a, w, _p in g if w) / n:>5.1%} win, avg {price:.1f}c, {pnl:+.2f}c each ({pnl / price:+.0%})")
