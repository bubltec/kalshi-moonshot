"""How do you cut losses on a late-window ticket? Replays exits on stored 15-minute candles (no orders).

Entry: a side priced LO-HI at 3 or 2 minutes left. Exits tried, each against holding to settlement:
  stop   sell when the side's bid has lost `d` of the price paid (checked on each later minute's close, sold at that
         close's bid: the realistic fill; `stop*` is the optimistic one, sold at exactly the stop level)
  slope  sell when the bid fell `c` cents or more over the last minute
  target sell when the bid reaches `m` times the price paid (a resting order, filled at that level)
Fees are charged on every leg; a sale is at the bid.   python scripts/late_exit_study.py moonshot.db [low high]
"""
import sqlite3, sys
from kalshi_moonshot.strategy import fee_per_contract as fee

LO, HI = (float(sys.argv[2]), float(sys.argv[3])) if len(sys.argv) > 3 else (0.15, 0.35)
db = sqlite3.connect(sys.argv[1]); db.row_factory = sqlite3.Row
trades = []
for w in db.execute("select ticker, close_ts, result from windows where series like '%15M' and result in ('yes','no')"):
    cs = {r["end_ts"]: r for r in db.execute("select end_ts, bid_c, ask_c, bid_lo, bid_hi, ask_lo, ask_hi from candles where ticker=?", (w["ticker"],))}
    for left in (3, 2):
        t0 = w["close_ts"] - left * 60
        c0 = cs.get(t0)
        if not c0 or c0["bid_c"] is None or c0["ask_c"] is None:
            continue
        for side in ("yes", "no"):
            price = c0["ask_c"] if side == "yes" else 1 - c0["bid_c"]
            if not LO <= price <= HI:
                continue
            path = []                                  # (bid close, bid low, bid high) of the side, minute by minute
            for k in range(1, left + 1):
                c = cs.get(t0 + 60 * k)
                if not c or c["bid_c"] is None:
                    break
                if side == "yes":
                    path.append((c["bid_c"], c["bid_lo"], c["bid_hi"]))
                else:
                    ok = c["ask_c"] is not None and c["ask_hi"] is not None and c["ask_lo"] is not None
                    path.append((1 - c["ask_c"], 1 - c["ask_hi"], 1 - c["ask_lo"]) if ok else (None, None, None))
            if len(path) < left or any(p[0] is None for p in path):
                continue
            trades.append((w["ticker"], price, path, w["result"] == side))

def run(rule):
    cost = back = 0.0; n = sold = wins = 0
    for _, price, path, won in trades:
        cost += price + fee(price); n += 1
        got = None
        for i, (close, low, high) in enumerate(path[:-1]):          # the last minute is the settlement
            got = rule(price, path, i)
            if got is not None:
                break
        if got is None:
            back += 1.0 if won else 0.0
        else:
            back += got - fee(got); sold += 1
        wins += (back and False)
    return n, sold, back / cost - 1

def stop(d, optimistic=False):
    def rule(price, path, i):
        level = price * (1 - d)
        close, low, _ = path[i]
        if optimistic and low is not None and low <= level:
            return level
        if not optimistic and close <= level:
            return close
    return rule
def slope(c):
    def rule(price, path, i):
        prev = price if i == 0 else path[i - 1][0]
        if prev - path[i][0] >= c:
            return path[i][0]
    return rule
def target(m):
    def rule(price, path, i):
        if price * m < 1 and path[i][2] is not None and path[i][2] >= price * m:
            return price * m
    return rule

print(f"{len(trades)} tickets priced {LO:.2f}-{HI:.2f} with 2-3 minutes left, in {len({t[0] for t in trades})} windows\n")
print(f"{'exit rule':36} {'sold early':>10} {'ROI':>8}")
n, _, base = run(lambda *a: None); print(f"{'hold to settlement':36} {0:>10} {base:>+8.1%}")
for d in (0.2, 0.3, 0.4, 0.5):
    _, s, r = run(stop(d)); print(f"{'stop at -' + format(d, '.0%') + ' (next close)':36} {s:>10} {r:>+8.1%}")
    _, s, r = run(stop(d, True)); print(f"{'stop at -' + format(d, '.0%') + ' (exact level, optimistic)':36} {s:>10} {r:>+8.1%}")
for c in (0.03, 0.05, 0.08):
    _, s, r = run(slope(c)); print(f"{'bid fell ' + format(c * 100, '.0f') + 'c in a minute':36} {s:>10} {r:>+8.1%}")
for m in (1.5, 2.0):
    _, s, r = run(target(m)); print(f"{'resting sell at ' + format(m, 'g') + 'x':36} {s:>10} {r:>+8.1%}")
