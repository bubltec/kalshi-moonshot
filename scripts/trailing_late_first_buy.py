"""The trailing-side buy as the paper lane makes it: one buy per market, day by day (2026-11-16, user: "lets start working on better buys").

The state table (scripts/buy_state_table.py) scored every 15-second moment, so one market counted many times. The paper lane crypto_trailing_late buys ONCE per market: the first moment from minute 7.5 at which a side led under 20% of the market so far, sits between 0.2 and 0.8 of its own range since the open, and asks 0.15 to 0.50 (spread 0.03 or under). This scores exactly that buy on every settled market of the live crypto lane's series since Monday 2026-11-09: held to settlement at the ask, fee in.
Kalshi's quotes only; every reading at a moment uses quotes stamped at or before it (the range and the lead are built up tick by tick in time order; nothing from another feed is joined in). By day, by the ask, by the minute of the buy, and against the same market's OTHER side bought at that second. Bands fixed before the run. No coin is named.

Run on the host: scripts/cloud.sh run scripts/trailing_late_first_buy.py
"""
import bisect
import datetime
from collections import defaultdict

from kalshi_moonshot import config, db
from kalshi_moonshot.lanes import lane_specs
from kalshi_moonshot.strategy import fee_per_contract as fee

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 9, 0, 0, tzinfo=ET).timestamp()
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
series = set(lane_specs(s)["crypto_multiscale"].series)
wins = [w for w in st.execute("SELECT ticker, series, close_ts, result FROM windows WHERE close_ts>? AND result IN ('yes','no') ORDER BY close_ts", (SINCE,)).fetchall() if w["series"] in series]
buys = []  # (day, ask, won, minutes in, other side's ask, position, led)
seen = 0
for w in wins:
    close = float(w["close_ts"])
    opened = close - 900.0
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], opened, close - 30)).fetchall()
    q = [(float(x[0]), float(x[1] or 0.0), float(x[2] or 0.0)) for x in q if x[2] and float(x[2]) > 0 and not (float(x[1] or 0.0) <= 0 and float(x[2]) >= 1)]
    if len(q) < 50:
        continue
    seen += 1
    lo = hi = None
    n = fav = 0
    last_s = None
    for t, bid, ask in q:  # in time order: every reading below uses only the quotes up to this one
        mid = (bid + ask) / 2
        sec = int(t)
        if sec == last_s:
            continue
        last_s = sec
        lo, hi = (mid, mid) if lo is None else (min(lo, mid), max(hi, mid))
        if t - opened >= 450.0 and n >= 90 and hi > lo and ask - bid <= 0.03:
            done = False
            for side in ("yes", "no"):
                a = ask if side == "yes" else 1 - bid
                led = fav / n if side == "yes" else (n - fav) / n
                pos = (mid - lo) / (hi - lo) if side == "yes" else (hi - mid) / (hi - lo)
                if 0.15 <= a <= 0.50 and led <= 0.2 and 0.2 <= pos <= 0.8:
                    other = (1 - bid) if side == "yes" else ask
                    buys.append((datetime.datetime.fromtimestamp(close, ET).strftime("%m-%d"), a, w["result"] == side, (t - opened) / 60.0, other, pos, led))
                    done = True
                    break
            if done:
                break
        n += 1
        fav += mid > 0.5


def line(name, rs):
    if len(rs) < 15:
        print(f"  {name:28s} n {len(rs):4d}")
        return
    n = len(rs)
    net = sum((1.0 if r[2] else 0.0) / (r[1] + fee(r[1])) - 1.0 for r in rs) / n * 100
    need = sum(r[1] + fee(r[1]) for r in rs) / n
    oth = [r for r in rs if 0.05 < r[4] < 0.99]
    onet = sum((0.0 if r[2] else 1.0) / (r[4] + fee(r[4])) - 1.0 for r in oth) / max(len(oth), 1) * 100
    print(f"  {name:28s} n {n:4d} | won {sum(1 for r in rs if r[2]) / n:4.0%} needs {need:4.0%} | ask {sum(r[1] for r in rs) / n:.3f} | held net per $ {net:+6.1f}% | the other side at that second {onet:+6.1f}%")


print(f"window: 2026-11-09 00:00 ET on; {len(wins)} settled markets, {seen} with quotes, {len(buys)} with a trailing-side buy ({len(buys) / max(seen, 1):.0%} of markets)")
line("all", buys)
print("\nby day")
for day in sorted({b[0] for b in buys}):
    line(day, [b for b in buys if b[0] == day])
print("\nby the ask")
for lo_, hi_ in ((0.15, 0.22), (0.22, 0.30), (0.30, 0.40), (0.40, 0.51)):
    line(f"ask {lo_:.2f} to {hi_:.2f}", [b for b in buys if lo_ <= b[1] < hi_])
print("\nby the minute of the buy")
for lo_, hi_ in ((7.5, 8.5), (8.5, 10), (10, 12), (12, 14.5)):
    line(f"minute {lo_:g} to {hi_:g}", [b for b in buys if lo_ <= b[3] < hi_])
print("\nby where it sat in its range")
for lo_, hi_ in ((0.2, 0.4), (0.4, 0.6), (0.6, 0.81)):
    line(f"position {lo_:.1f} to {hi_:.1f}", [b for b in buys if lo_ <= b[5] < hi_])
print("DONE")
