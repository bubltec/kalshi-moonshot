"""Does selling on a spike beat holding, for cheap 15-minute tickets? Replays exits on stored minute candles.

Entry: the cheap side (ask LO-HI) at minute 4, 7 or 10 of a window. Each exit rule is replayed against holding to
settlement, fees on both legs, a sale at the bid. Nothing is traded.

  target   a resting sell at m x the price paid, filled when a minute's best bid reaches it
  spike    sell at the close of the first minute whose bid closed `c` cents or more above the previous close
  either   whichever of target / spike happens first
  ceiling  the same spike, sold at the minute's best bid: no real exit does this, it bounds what faster reflexes could add
  trail    once the bid has reached 2x, sell when it gives back a third of its peak gain

Tuned on the earliest 60% of windows, judged on the latest 40%, with a 90% interval from resampling whole windows.
    python scripts/spike_exit_study.py moonshot.db [low high]
"""
import random, sqlite3, sys
from kalshi_moonshot.strategy import fee_per_contract as fee

LO, HI = (float(sys.argv[2]), float(sys.argv[3])) if len(sys.argv) > 3 else (0.05, 0.25)
db = sqlite3.connect(sys.argv[1]); db.row_factory = sqlite3.Row
trades = []                     # (window close, ticker, price, [(bid_close, bid_high) per later minute], won)
for w in db.execute("select ticker, close_ts, result from windows where series like '%15M' and result in ('yes','no') order by close_ts"):
    cs = {r["end_ts"]: r for r in db.execute("select end_ts, bid_c, ask_c, bid_hi, ask_lo from candles where ticker=?", (w["ticker"],))}
    for minute in (4, 7, 10):
        t0 = w["close_ts"] - (15 - minute) * 60
        c0 = cs.get(t0)
        if not c0 or c0["bid_c"] is None or c0["ask_c"] is None:
            continue
        for side in ("yes", "no"):
            price = c0["ask_c"] if side == "yes" else 1 - c0["bid_c"]
            if not LO <= price <= HI:
                continue
            path, ok = [], True
            for k in range(1, 15 - minute):                       # minutes after entry, before the last one
                c = cs.get(t0 + 60 * k)
                if not c or c["bid_c"] is None:
                    ok = False; break
                if side == "yes":
                    path.append((c["bid_c"], c["bid_hi"] if c["bid_hi"] is not None else c["bid_c"]))
                else:
                    if c["ask_c"] is None:
                        ok = False; break
                    path.append((1 - c["ask_c"], (1 - c["ask_lo"]) if c["ask_lo"] is not None else 1 - c["ask_c"]))
            if ok and path:
                trades.append((w["close_ts"], w["ticker"], price, path, w["result"] == side))
print(f"{len(trades)} tickets priced {LO:.2f}-{HI:.2f} in {len({t[1] for t in trades})} windows")


def play(price, path, won, target=None, spike=None, trail=False, best=False):
    prev, peak = price, price
    for close, high in path:
        if target is not None and price * target < 1 and high >= price * target:
            return price * target - fee(price * target)
        if spike is not None and (high if best else close) - prev >= spike:
            got = high if best else close                          # best: sold at the minute's top (a ceiling, not a fill)
            return got - fee(got)
        peak = max(peak, high)
        if trail and peak >= 2 * price and close <= peak - (peak - price) / 3:
            return close - fee(close)
        prev = close
    return 1.0 if won else 0.0


def roi(items, **rule):
    cost = sum(p + fee(p) for _, _, p, _, _ in items)
    return sum(play(p, path, won, **rule) for _, _, p, path, won in items) / cost - 1 if cost else float("nan")


cut = trades[int(len(trades) * 0.6)][0]
train = [t for t in trades if t[0] < cut]; test = [t for t in trades if t[0] >= cut]
rules = [("hold to settlement", {})]
rules += [(f"target {m:g}x", {"target": m}) for m in (2, 3, 4, 6)]
rules += [(f"spike +{int(c * 100)}c", {"spike": c}) for c in (0.05, 0.10, 0.15, 0.20)]
rules += [(f"either: target {m:g}x or spike +{int(c * 100)}c", {"target": m, "spike": c}) for m in (3, 4) for c in (0.10, 0.20)]
rules += [("trail (after 2x, give back 1/3)", {"trail": True})]
rules += [(f"spike +{int(c * 100)}c, sold at the minute's high (ceiling)", {"spike": c, "best": True}) for c in (0.10, 0.20)]
random.seed(4)
print(f"\n{'rule':40} {'train':>8} {'test':>8}   test 90% range ({len(test)} tickets)")
for name, rule in rules:
    by = {}
    for t in test:
        by.setdefault(t[1], []).append(t)
    ks = list(by)
    bs = sorted(roi([x for k in random.choices(ks, k=len(ks)) for x in by[k]], **rule) for _ in range(200))
    print(f"{name:40} {roi(train, **rule):>+8.1%} {roi(test, **rule):>+8.1%}   [{bs[10]:+.0%}, {bs[190]:+.0%}]")
sold = sum(1 for _, _, p, path, won in test if play(p, path, won, target=3) != (1.0 if won else 0.0))
print(f"\nOf {len(test)} test tickets: {sum(t[4] for t in test)} won at settlement; {sold} reached 3x and would have sold.")
