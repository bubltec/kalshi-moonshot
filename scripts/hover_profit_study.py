"""Sell at the first profit only after the bet has hovered (its bid dipped to D or below at some point since entry), compared with holding.
Same entries and costs as hover_study.py. Run:  docker compose exec -T bot python - < scripts/hover_profit_study.py
"""
import math

from kalshi_moonshot import config
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
from kalshi_moonshot.tape import Tape

s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = Tape(s.tape_path).db
SLIP = 0.02
SERIES = ("KXEURUSD15M", "KXGBPUSD15M", "KXUSDJPY15M", "KXAUDUSD15M", "KXUSDCAD15M")
rows = st.db.execute("select ticker, close_ts, result from windows where result in ('yes','no') and series in (%s)" % ",".join("?" * len(SERIES)), SERIES).fetchall()
LEVELS = (0.70, 0.60, 0.55, 0.50, 0.45)


def roi(cost, bid, won):
    ret = (bid - SLIP - fee(bid)) if bid is not None else (1.0 if won else 0.0)
    return ret / cost - 1


hold, trig = [], {d: [] for d in LEVELS}
for r in rows:
    tk, close, result = r["ticker"], float(r["close_ts"]), r["result"]
    q = tape.execute("select ts,bid,ask from quotes where ticker=? and bid is not null and ask is not null and ts between ? and ? order by ts", (tk, close - 900, close)).fetchall()
    entry = None
    for i, (ts, b, a) in enumerate(q):
        if 4 <= (ts - (close - 900)) / 60 <= 10.5:
            for side in ("yes", "no"):
                ask, bid = (a, b) if side == "yes" else (1 - b, 1 - a)
                if 0.59 <= ask <= 0.85 and ask - bid <= 0.03:
                    entry = (i, side, ask)
                    break
        if entry:
            break
    if not entry:
        continue
    i0, side, ask = entry
    cost, won = ask + fee(ask), result == side
    path = [(b if side == "yes" else 1 - a) for ts, b, a in q[i0 + 1:]]
    h = roi(cost, None, won)
    hold.append(h)
    for d in LEVELS:
        visited, sold = False, None
        for bid in path:
            if bid <= d:
                visited = True
            elif visited and bid - SLIP - fee(bid) > cost:
                sold = bid
                break
        if sold is not None:
            trig[d].append((roi(cost, sold, won), h))


def ms(v):
    m = sum(v) / len(v)
    se = math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1) / len(v)) if len(v) > 1 else 0
    return m, se


m, se = ms(hold)
print(f"{len(hold)} bets, hold = {m:+.1%} ±{se:.1%}\n")
print("dip level D | bets that dipped to D then reached a profit | sold at that profit | held instead | gain per triggered bet | over all bets")
for d in LEVELS:
    t = trig[d]
    if len(t) < 3:
        print(f"{d:.2f}  too few ({len(t)})")
        continue
    a, sa = ms([x for x, _ in t]); b, sb = ms([y for _, y in t]); diff, sd = ms([x - y for x, y in t])
    print(f"{d:.2f}  n={len(t):>3}   sold {a:+6.1%} ±{sa:.1%}   held {b:+6.1%} ±{sb:.1%}   gain {diff:+6.1%} ±{sd:.1%}   over all bets {diff*len(t)/len(hold):+.1%}")
