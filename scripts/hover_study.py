"""Currency 15-minute bets that hover around the strike: does selling at the first profit, or selling a minor loss late, beat holding?
Replays recorded quotes with the fx_favorite entry (ask 59-85c, minutes 4-10.5, spread <= 3c); places nothing. Run inside the bot container:
    docker compose exec -T bot python - < scripts/hover_study.py
"""
import bisect
import math
from collections import defaultdict

from kalshi_moonshot import config
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
from kalshi_moonshot.tape import Tape

s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = Tape(s.tape_path).db
SLIP = 0.02
SERIES = ("KXEURUSD15M", "KXGBPUSD15M", "KXUSDJPY15M", "KXAUDUSD15M", "KXUSDCAD15M")
rows = st.db.execute("select ticker, series, close_ts, result from windows where result in ('yes','no') and series in (%s)" % ",".join("?" * len(SERIES)), SERIES).fetchall()


def roi(cost, bid, won):
    ret = (bid - SLIP - fee(bid)) if bid is not None else (1.0 if won else 0.0)
    return ret / cost - 1


res = defaultdict(list)
hover_res = defaultdict(list)
n_hover = 0
for r in rows:
    tk, close, result = r["ticker"], float(r["close_ts"]), r["result"]
    q = tape.execute("select ts,bid,ask from quotes where ticker=? and bid is not null and ask is not null and ts between ? and ? order by ts", (tk, close - 900, close)).fetchall()
    if len(q) < 10:
        continue
    entry = None
    for i, (ts, b, a) in enumerate(q):
        el = (ts - (close - 900)) / 60
        if not 4 <= el <= 10.5:
            continue
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
    cost = ask + fee(ask)
    won = result == side
    path = [(ts, (b if side == "yes" else 1 - a)) for ts, b, a in q[i0 + 1:]]
    out = {"hold": roi(cost, None, won)}
    # take any profit the moment the sale would net more than the cost
    tp = next((bid for ts, bid in path if bid - SLIP - fee(bid) > cost), None)
    out["sell at first profit"] = roi(cost, tp, won) if tp is not None else out["hold"]
    # hovering: from minute 11 on, price between 35c and 65c and still under water -> sell the minor loss
    hv = next((bid for ts, bid in path if (ts - (close - 900)) / 60 >= 11 and 0.35 <= bid <= 0.65 and bid - SLIP - fee(bid) < cost), None)
    out["sell loss when hovering (min 11+)"] = roi(cost, hv, won) if hv is not None else out["hold"]
    both = tp if tp is not None and (hv is None or True) else None
    # profit first, else the hover sale, whichever the path reaches first
    ev = []
    if tp is not None:
        ev.append((next(ts for ts, bid in path if bid == tp), tp))
    if hv is not None:
        ev.append((next(ts for ts, bid in path if (ts - (close - 900)) / 60 >= 11 and bid == hv), hv))
    first = min(ev)[1] if ev else None
    out["profit-take + hover sale"] = roi(cost, first, won) if first is not None else out["hold"]
    for k, v in out.items():
        res[k].append(v)
    if hv is not None:
        n_hover += 1
        for k, v in out.items():
            hover_res[k].append(v)


def line(v):
    m = sum(v) / len(v)
    se = math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1) / len(v))
    return f"{m:+6.1%} ±{se:.1%}"


print(f"{len(res['hold'])} bets with the fx_favorite entry; {n_hover} of them hovered (35-65c, minute 11+, under water)")
print("\nall bets:")
for k, v in res.items():
    print(f"  {k:36} {line(v)}")
print("\nonly the bets that hovered:")
for k, v in hover_res.items():
    print(f"  {k:36} {line(v)}  (n={len(v)})")
