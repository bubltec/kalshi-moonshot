"""Buy MORE of the favorite after it collapses: does the held side win more than it then costs? Read-only.

The trigger is the lane's drop exit: a side whose bid was at least FAV five seconds ago falls DROP or more by now, in the last WITHIN seconds (not the last 15) of a 15-minute market, the first time per side per market. The study buys one contract
of that SAME side at its ask (the real ask of the side, so the spread is paid) LAT seconds after the trigger (CONFIRM seconds later for the confirmed variants, which also need the bid still STAY under its old level, as drop_confirm / drop_stay
do), plus the taker fee, and holds it to the close. Compare buying the other side after selling: that is the same bet as the sale and comes out the exact opposite of this one (less the spread). "edge" is per contract in cents after the fee, with the older
and newer half of the days; "won" is how often the held side then won, "ask" its mean price. Rows with fewer than 5 events in a half show "-". Two sources:

    .venv/bin/python scripts/dip_buy_study.py ARCHIVE_DIR              the archived Parquet quotes (results read from each market's last quote)
    scripts/cloud.sh run scripts/dip_buy_study.py SOURCE=tape          the cloud tape and its windows table (the newest days, real results)
"""
import bisect
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from kalshi_moonshot.strategy import fee_per_contract

ET = ZoneInfo("America/New_York")
MONTHS = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}
LAT = int(os.environ.get("LAT", "1"))
SOURCE = os.environ.get("SOURCE", "archive" if len(sys.argv) > 1 else "tape")
FAMILY = {"GOLD": "metals", "SILVER": "metals", "PLATINUM": "metals", "PALLADIUM": "metals", "COPPER": "metals", "WTI": "energy", "BRENT": "energy", "NATGAS": "energy",
          "BTC": "coins", "ETH": "coins", "SOL": "coins", "XRP": "coins", "DOGE": "coins"}


def close_of(ticker):
    m = re.match(r"^KX([A-Z]+)15M-(\d\d)([A-Z]{3})(\d\d)(\d\d)(\d\d)-", ticker)
    if not m or m.group(1) not in FAMILY:
        return None
    return m.group(1), datetime(2000 + int(m.group(2)), MONTHS[m.group(3)], int(m.group(4)), int(m.group(5)), int(m.group(6)), tzinfo=ET).timestamp()


markets = []  # (name, close, ts[], bid[], ask[], result)
if SOURCE == "archive":
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    quotes = defaultdict(list)
    for f in sorted(os.listdir(sys.argv[1])):
        if f.endswith(".parquet"):
            t = pq.read_table(os.path.join(sys.argv[1], f), columns=["ts", "ticker", "bid", "ask"])
            t = t.filter(pc.match_substring_regex(t.column("ticker"), r"^KX[A-Z]+15M-"))
            for ts, tk, bid, ask in zip(t.column("ts").to_pylist(), t.column("ticker").to_pylist(), t.column("bid").to_pylist(), t.column("ask").to_pylist()):
                if bid is not None and ask is not None and close_of(tk):
                    quotes[tk].append((float(ts), float(bid), float(ask)))
    for tk, q in quotes.items():
        name, close = close_of(tk)
        q.sort()
        last = [x for x in q if x[0] >= close - 40]
        if len(q) < 30 or not last:
            continue
        res = "yes" if last[-1][1] >= 0.95 else "no" if last[-1][2] <= 0.05 else None
        if res:
            markets.append((name, close, [x[0] for x in q], [x[1] for x in q], [x[2] for x in q], res))
else:
    from kalshi_moonshot import config, db
    from kalshi_moonshot.store import Store

    s = config.reload()
    tape = db.connect(s.tape_path, "tape", shared=True)
    main = Store(s.db_path, s.exclude_versions)
    for w in main.db.execute("SELECT ticker, close_ts, result FROM windows WHERE result IN ('yes','no') AND close_ts > 0 ORDER BY close_ts").fetchall():
        c = close_of(w["ticker"])
        if not c:
            continue
        q = [(float(t), float(b), float(a)) for t, b, a in tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid IS NOT NULL AND ask IS NOT NULL AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], c[1] - 260, c[1] + 1)).fetchall()]
        if len(q) >= 30:
            markets.append((c[0], c[1], [x[0] for x in q], [x[1] for x in q], [x[2] for x in q], w["result"]))
print(f"{len(markets)} settled 15-minute markets from the {SOURCE} ({len(set(m[0] for m in markets))} series)\n")


def val(m, t, side):
    """(bid, ask) of `side` at the last quote at or before t, within 8 s."""
    ts, bid, ask = m[2], m[3], m[4]
    i = bisect.bisect_right(ts, t) - 1
    if i < 0 or t - ts[i] > 8:
        return None
    return (bid[i], ask[i]) if side == "yes" else (1.0 - ask[i], 1.0 - bid[i])


def events(fav, drop, within, confirm, stay):
    out = []
    for m in markets:
        name, close, res = m[0], m[1], m[5]
        for side in ("yes", "no"):
            for t in range(int(close - within), int(close - 15)):
                a, b = val(m, t - 5, side), val(m, t, side)
                if not a or not b or a[0] < fav or a[0] - b[0] < drop:
                    continue
                t_buy = t + LAT
                if confirm:
                    c = val(m, t + confirm, side)
                    if not c or c[0] > a[0] - stay:
                        break
                    t_buy = t + confirm + LAT
                f = val(m, t_buy, side)
                if f and 0.02 <= f[1] <= 0.98:
                    out.append((name, close, f[1], res == side))
                break
    return out


def show(label, ev):
    if not ev:
        print(f"  {label:<10} no events")
        return
    mid = sorted(e[1] for e in ev)[len(ev) // 2]

    def edge(g):
        return sum((1.0 if w else 0.0) - a - fee_per_contract(a) for _n, _c, a, w in g) / len(g) * 100

    old, new = [e for e in ev if e[1] < mid], [e for e in ev if e[1] >= mid]
    n, wins = len(ev), sum(1 for e in ev if e[3])
    ask = sum(e[2] for e in ev) / n
    oe = f"{edge(old):>+6.1f}c" if len(old) >= 5 else "     -"
    ne = f"{edge(new):>+6.1f}c" if len(new) >= 5 else "     -"
    print(f"  {label:<10} n {n:>4}  held side won {wins / n:>5.1%} at {ask * 100:>4.1f}c  edge {edge(ev):>+6.1f}c ({edge(ev) / (ask * 100):>+5.0%})  old {oe}  new {ne}")


for fav, drop, within, confirm, stay in ((0.85, 0.15, 180, 0, 0.0), (0.85, 0.15, 180, 4, 0.10), (0.85, 0.25, 180, 0, 0.0), (0.80, 0.10, 240, 0, 0.0), (0.80, 0.10, 240, 4, 0.05), (0.70, 0.15, 180, 4, 0.10)):
    print(f"== favorite at least {fav:.2f}, falls {drop:.2f} in 5 s, last {within} s{', still down by %.2f after 4 s' % stay if confirm else ''}")
    ev = events(fav, drop, within, confirm, stay)
    show("all", ev)
    for fam in ("metals", "energy", "coins"):
        show(fam, [e for e in ev if FAMILY[e[0]] == fam])
    for nm in ("GOLD", "SILVER", "NATGAS", "WTI"):
        show(nm, [e for e in ev if e[0] == nm])
    print()
print("DONE")
