"""Sell the collapsing favorite AND buy the other side: does the other side win more than it costs? Read-only, from the archived tape (Parquet quotes), no database.

The trigger is the lane's drop exit: a side whose bid was at least FAV five seconds ago falls DROP or more by now, in the last 180 s (not the last 15) of a 15-minute market, the first time per side per market. The other side's
price then is 1 minus the fallen side's bid (its ask). Each row buys one contract of the OTHER side LAT seconds after the trigger (CONFIRM seconds later for the confirmed variants, which also need the bid to still be at least STAY under
its old level, as the drop_confirm / drop_stay settings do) at that ask plus the taker fee and holds it to the close. A market's result is read from its last quote (a bid of 0.95+ is YES, an ask of 0.05- is NO); a market whose last
quote is undecided is dropped. The columns: how many triggers, how often the OTHER side won, its mean price, the edge per ticket after the fee, and the same for the older and newer half of the days. "sell vs hold" is, per contract, what selling the
fallen side at its bid (less the sale fee) made against holding it to the close: the cost of the sale leg, so the flip has to earn at least that to make the pair worth it.

    .venv/bin/python scripts/flip_after_collapse_study.py ARCHIVE_DIR [LAT=1]       (ARCHIVE_DIR holds q<day>.parquet files fetched from the bucket)
"""
import bisect
import itertools
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import pyarrow.compute as pc
import pyarrow.parquet as pq

from kalshi_moonshot.strategy import fee_per_contract

ET = ZoneInfo("America/New_York")
MONTHS = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}
LAT = int(os.environ.get("LAT", "1"))
FAMILY = {"GOLD": "metals", "SILVER": "metals", "PLATINUM": "metals", "PALLADIUM": "metals", "COPPER": "metals", "WTI": "energy", "BRENT": "energy", "NATGAS": "energy",
          "BTC": "coins", "ETH": "coins", "SOL": "coins", "XRP": "coins", "DOGE": "coins"}


def close_of(ticker):
    m = re.match(r"^KX([A-Z]+)15M-(\d\d)([A-Z]{3})(\d\d)(\d\d)(\d\d)-", ticker)
    if not m or m.group(1) not in FAMILY:
        return None
    y, mon, d, hh, mm = 2000 + int(m.group(2)), MONTHS[m.group(3)], int(m.group(4)), int(m.group(5)), int(m.group(6))
    return m.group(1), datetime(y, mon, d, hh, mm, tzinfo=ET).timestamp()


folder = sys.argv[1]
quotes = defaultdict(list)
for f in sorted(os.listdir(folder)):
    if not f.endswith(".parquet"):
        continue
    t = pq.read_table(os.path.join(folder, f), columns=["ts", "ticker", "bid", "ask"])
    t = t.filter(pc.match_substring_regex(t.column("ticker"), r"^KX[A-Z]+15M-"))
    for ts, tk, bid, ask in zip(t.column("ts").to_pylist(), t.column("ticker").to_pylist(), t.column("bid").to_pylist(), t.column("ask").to_pylist()):
        if bid is not None and ask is not None and close_of(tk):
            quotes[tk].append((float(ts), float(bid), float(ask)))

markets = []  # (name, close, ts[], bid[], ask[], result)
for tk, q in quotes.items():
    name, close = close_of(tk)
    q.sort()
    ts = [x[0] for x in q]
    last = [x for x in q if x[0] >= close - 40]
    if len(q) < 30 or not last:
        continue
    lb, la = last[-1][1], last[-1][2]
    res = "yes" if lb >= 0.95 else "no" if la <= 0.05 else None
    if res:
        markets.append((name, close, ts, [x[1] for x in q], [x[2] for x in q], res))
print(f"{len(markets)} settled 15-minute markets in the archive ({len(set(m[0] for m in markets))} series)\n")


def val(m, t, side):
    """(held bid of `side`, the other side's ask) at the last quote at or before t, within 8 s."""
    ts, bid, ask = m[2], m[3], m[4]
    i = bisect.bisect_right(ts, t) - 1
    if i < 0 or t - ts[i] > 8:
        return None
    return (bid[i], 1.0 - bid[i]) if side == "yes" else (1.0 - ask[i], ask[i])


def events(fav, drop, confirm, stay):
    out = []
    for m in markets:
        name, close, ts, bid, ask, res = m
        for side in ("yes", "no"):
            fired = False
            for t in range(int(close - 180), int(close - 15)):
                a, b = val(m, t - 5, side), val(m, t, side)
                if not a or not b or a[0] < fav or a[0] - b[0] < drop:
                    continue
                t_buy = t + LAT
                if confirm:
                    c = val(m, t + confirm, side)
                    if not c or c[0] > a[0] - stay:
                        break  # recovered: no confirmed fall
                    t_buy = t + confirm + LAT
                f = val(m, t_buy, side)
                if not f or not 0.01 <= f[1] <= 0.99:
                    break
                other = "no" if side == "yes" else "yes"
                held_won = res == side
                sell = f[0] - fee_per_contract(f[0]) - (1.0 if held_won else 0.0)  # sale proceeds minus the hold payoff (before the entry price, which cancels)
                out.append((name, close, f[1], res == other, sell))
                fired = True
                break
            if fired:
                continue
    return out


def show(label, ev):
    if not ev:
        print(f"  {label:<24} no triggers")
        return
    mid = sorted(e[1] for e in ev)[len(ev) // 2]

    def edge(g):
        return sum((1.0 if w else 0.0) - a - fee_per_contract(a) for _n, _c, a, w, _s in g) / len(g) * 100

    old, new = [e for e in ev if e[1] < mid], [e for e in ev if e[1] >= mid]
    n, wins = len(ev), sum(1 for e in ev if e[3])
    ask = sum(e[2] for e in ev) / n
    oe = f"{edge(old):>+6.1f}c" if len(old) >= 5 else "     -"
    ne = f"{edge(new):>+6.1f}c" if len(new) >= 5 else "     -"
    print(f"  {label:<24} n {n:>4}  other side won {wins / n:>5.1%} at {ask * 100:>4.1f}c  edge {edge(ev):>+6.1f}c ({edge(ev) / (ask * 100):>+5.0%})  old {oe}  new {ne}   sell-vs-hold {sum(e[4] for e in ev) / n * 100:>+6.1f}c")


for fav, drop, confirm, stay in ((0.85, 0.15, 0, 0.0), (0.85, 0.15, 4, 0.10), (0.85, 0.25, 0, 0.0), (0.85, 0.25, 4, 0.10), (0.90, 0.15, 4, 0.10), (0.70, 0.15, 4, 0.10)):
    print(f"== favorite at least {fav:.2f}, falls {drop:.2f} in 5 s{', still down by 0.10 after 4 s' if confirm else ''}")
    ev = events(fav, drop, confirm, stay)
    show("all", ev)
    for fam in ("metals", "energy", "coins"):
        show(fam, [e for e in ev if FAMILY[e[0]] == fam])
    for lo, hi in ((0.0, 0.25), (0.25, 0.45), (0.45, 1.0)):
        show(f"other side {lo:.2f}-{hi:.2f}", [e for e in ev if lo <= e[2] < hi])
    print()
print("DONE")
