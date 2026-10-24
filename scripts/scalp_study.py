"""Can repeated in-and-out trades inside one 15-minute window make money after the spread and both fees? Replays ticks; places nothing.

Per window and side, a rule watches the side's bid/ask once a second from minute 1 to the last 40 seconds:
  entry   "dip": the ask is `d` under the highest bid of the last 90 s;  "surge": the bid is `d` over its level 30 s ago;  "any": every `gap` seconds (a control)
  exit    sell when the bid is `g` over the price paid, or `s` under it, or `hold` seconds have passed; what is still held at the end rides to settlement
  again   after an exit wait `cool` seconds, then it may enter again, up to `max_trades` times per window and side
Every buy pays the ask plus the fee, every sale takes the bid less the fee. Rules are scored on the earliest 60% of windows and checked on the latest 40%.
    docker compose exec -T bot python - < scripts/scalp_study.py
"""
import itertools
import math
import random

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
SER = ("KXGOLD15M", "KXSILVER15M", "KXWTI15M", "KXNATGAS15M", "KXCOPPER15M", "KXPLATINUM15M", "KXPALLADIUM15M", "KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M")
wins = st.db.execute("select ticker, series, close_ts, result from windows where result in ('yes','no') and series in (%s) order by close_ts" % ",".join("'%s'" % x for x in SER)).fetchall()
random.seed(1)
data = []
for w in wins:
    close = float(w["close_ts"])
    q = tape.execute("select ts,bid,ask from quotes where ticker=? and bid is not null and ask is not null order by ts", (w["ticker"],)).fetchall()
    if not q or q[0][0] > close - 900 + 90:
        continue
    # one point per second: the last quote at or before each second
    sec, last, k = {}, None, 0
    start = close - 900
    for t in range(int(start) + 60, int(close) - 40):
        while k < len(q) and q[k][0] <= t:
            last = q[k]
            k += 1
        if last:
            sec[t - int(start)] = (last[1], last[2])
    if len(sec) > 600:
        data.append((w["result"], sec))
cut = int(len(data) * 0.6)
train, test = data[:cut], data[cut:]
print(f"{len(data)} windows with a full tape ({len(train)} to tune on, {len(test)} to check)")


def run(windows, entry, d, g, stop, hold, cool, max_trades, gap=45):
    pnl, cost, n, won = 0.0, 0.0, 0, 0
    for result, sec in windows:
        keys = sorted(sec)
        for side in ("yes", "no"):
            def px(i):
                b, a = sec[i]
                return (b, a) if side == "yes" else (1 - a, 1 - b)
            bids = {i: px(i)[0] for i in keys}
            trades, i, last_exit = 0, keys[0] + 30, -999
            while i <= keys[-1] and trades < max_trades:
                if i not in sec or i - last_exit < cool:
                    i += 1
                    continue
                bid, ask = px(i)
                sig = False
                if entry == "dip":
                    top = max(bids.get(j, 0) for j in range(i - 90, i + 1))
                    sig = ask <= top - d and 0.2 <= ask <= 0.8
                elif entry == "surge":
                    prev = bids.get(i - 30)
                    sig = prev is not None and bid >= prev + d and 0.2 <= ask <= 0.8
                else:
                    sig = (i % gap == 0) and 0.2 <= ask <= 0.8
                if not sig:
                    i += 1
                    continue
                paid = ask + fee(ask)
                trades += 1
                j, sold = i + 1, None
                while j <= keys[-1]:
                    if j in sec:
                        b2 = px(j)[0]
                        if b2 >= ask + g or (stop and b2 <= ask - stop) or j - i >= hold:
                            sold = b2
                            break
                    j += 1
                if sold is None:  # rides to settlement
                    val = 1.0 if result == side else 0.0
                    last_exit = keys[-1]
                    j = keys[-1] + 1
                else:
                    val = sold - fee(sold)
                    last_exit = j
                pnl += val - paid
                cost += paid
                n += 1
                won += val > paid
                i = j + 1
    return n, won, pnl, cost


rules = []
for entry, d in itertools.chain(itertools.product(("dip", "surge"), (0.03, 0.06, 0.10)), [("any", 0.0)]):
    for g in (0.04, 0.08):
        for stop in (0.0, 0.08):
            rules.append((entry, d, g, stop))
print(f"{len(rules)} rules, each with hold 120 s, cooldown 20 s, up to 6 trades per window and side\n")
res = []
for r in rules:
    a = run(train, r[0], r[1], r[2], r[3], 120, 20, 6)
    if a[0] >= 200:
        res.append((a[2] / a[3], r, a))
res.sort(reverse=True)
print(f"{'rule':44}{'train trades':>13}{'ROI':>8}  |{'test trades':>12}{'won':>6}{'ROI':>8}")
for roi, r, a in res[:8] + res[-3:]:
    b = run(test, r[0], r[1], r[2], r[3], 120, 20, 6)
    name = f"{r[0]} {r[1]:.2f}, sell +{r[2]:.2f}, stop {r[3]:.2f}" if r[0] != "any" else f"control: every 45 s, sell +{r[2]:.2f}, stop {r[3]:.2f}"
    print(f"{name:44}{a[0]:13}{roi:+8.1%}  |{b[0]:12}{b[1] / max(1, b[0]):6.0%}{b[2] / b[3] if b[3] else 0:+8.1%}")
