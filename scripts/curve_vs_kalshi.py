"""Does the curve table know more than Kalshi's own price? Table chance against Kalshi's 1-minute bid and ask, on finished 15-minute crypto markets. Read-only analysis of pulled files. Places nothing.

Inputs: Coinbase 1-minute candles (scripts/coinbase_minutes_pull.py), Kalshi 1-minute candles of a sample of finished markets (scripts/kalshi_candle_pull.py), and data/curve_table.json (the table is built from earlier months, so these markets are out of sample).
For each market and each decision minute m (5-12): the class and the table's chance of finishing above the open come from the Coinbase closes of the first m minutes (curve.py, the same code the bot runs); Kalshi's price is the YES ask and bid of the minute that ends at the decision.
 1. Log-loss against the result for: Kalshi's mid, the random-walk baseline, the table. A lower number is a better forecast; the table beats the market only if it is below Kalshi's.
 2. A buy rule: take the side whose table chance beats its ask by at least a bar, at the first such minute of the market, hold to the result, pay the buy fee. Standard errors across markets.
    python scripts/curve_vs_kalshi.py COINBASE_DIR KALSHI_DIR TABLE_JSON
"""
import csv
import json
import math
import os
import statistics
import sys
from collections import defaultdict

from kalshi_moonshot import curve
from kalshi_moonshot.strategy import fee_per_contract as fee

CB, KD, TABLE = sys.argv[1], sys.argv[2], sys.argv[3]
table = curve.CurveTable(json.load(open(TABLE)))
COIN = {"KXBTC15M": "BTC", "KXETH15M": "ETH", "KXSOL15M": "SOL", "KXXRP15M": "XRP", "KXDOGE15M": "DOGE"}


def load_closes(coin):
    out = {}
    for r in csv.DictReader(open(os.path.join(CB, f"{coin}.csv"))):
        out[int(r["time"])] = (float(r["open"]), float(r["close"]))
    return out


def ll(p, y):
    p = min(1 - 1e-4, max(1e-4, p))
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


rows = []  # one per market and decision minute
for series, coin in COIN.items():
    path = os.path.join(KD, f"{series}.jsonl")
    if not os.path.exists(path) or not os.path.exists(os.path.join(CB, f"{coin}.csv")):
        continue
    cb = load_closes(coin)
    last = {"t": None}

    def close_at(t, cb=cb):  # the close of the candle that STARTS at t-60 (i.e. the price at time t), forward-filled
        for back in range(0, 6):
            v = cb.get(t - 60 - 60 * back)
            if v:
                return v[1]
        return None

    for line in open(path):
        m = json.loads(line)
        o, res = m["open_ts"], 1.0 if m["result"] == "yes" else 0.0
        open_v = None
        for back in range(0, 5):
            if cb.get(o - 60 * back):
                open_v = cb[o - 60 * back][0] if back == 0 else cb[o - 60 * back][1]
                break
        if open_v is None:
            continue
        before = [v for k in range(-240, 1) if (v := close_at(o + 60 * k)) is not None]
        vol = curve.per_minute_vol(before)
        cand = {c["t"]: c for c in m["candles"]}
        for mm in range(curve.MIN_MINUTE, curve.MAX_MINUTE + 1):
            path_closes = [close_at(o + 60 * k) for k in range(1, mm + 1)]
            if None in path_closes or vol is None:
                continue
            c = cand.get(o + 60 * mm)
            if not c:
                continue
            bid, ask = float(c["bid_c"]), float(c["ask_c"])
            if not (0 < bid < ask < 1) or ask - bid > 0.08:
                continue
            info = curve.classify(open_v, path_closes, vol)
            if info is None:
                continue
            p_up, base, n = table.chance_up(series, info)
            rows.append({"series": series, "ticker": m["ticker"], "minute": mm, "y": res, "bid": bid, "ask": ask, "mid": (bid + ask) / 2, "base": base, "table": p_up, "n": n, "t": o})
print(f"{len(rows):,} market-minutes from {len({r['ticker'] for r in rows})} markets; coins: " + ", ".join(f"{COIN[s]} {len({r['ticker'] for r in rows if r['series'] == s})}" for s in COIN if any(r['series'] == s for r in rows)))
if not rows:
    sys.exit()


def report(label, rs):
    if len(rs) < 100:
        return f"  {label:28} only {len(rs)}"
    lm = statistics.mean(ll(r["mid"], r["y"]) for r in rs)
    lb = statistics.mean(ll(r["base"], r["y"]) for r in rs)
    lt = statistics.mean(ll(r["table"], r["y"]) for r in rs)
    return f"  {label:28} n={len(rs):5}  log-loss: Kalshi {lm:.4f}, random walk {lb:.4f}, table {lt:.4f}  (table minus Kalshi {1000 * (lt - lm):+.1f} per 1000; negative = the table is better)"


print("\\n1. forecasts against the result")
print(report("all coins, all minutes", rows))
for mm in (5, 8, 10, 12):
    print(report(f"minute {mm}", [r for r in rows if r["minute"] == mm]))
for series in COIN:
    rs = [r for r in rows if r["series"] == series]
    if rs:
        print(report(COIN[series], rs))


def trades(bar):
    seen, out = set(), []
    for r in sorted(rows, key=lambda r: (r["ticker"], r["minute"])):
        if r["ticker"] in seen:
            continue
        e_yes, e_no = r["table"] - r["ask"], (1 - r["table"]) - (1 - r["bid"])
        if max(e_yes, e_no) >= bar:
            seen.add(r["ticker"])
            if e_yes >= e_no:
                price, win = r["ask"], r["y"]
            else:
                price, win = 1 - r["bid"], 1 - r["y"]
            c = price + fee(price)
            out.append({"cl": r["ticker"], "ret": (win - c) / c, "gross": (win - price) / price, "price": price, "win": win, "series": r["series"]})
    return out


print("\\n2. the buy rule: the side whose table chance beats its ask by at least the bar, first such minute, held to the result")
for bar in (0.02, 0.03, 0.05, 0.08):
    t = trades(bar)
    if len(t) < 30:
        print(f"  bar {bar:.2f}: only {len(t)} trades")
        continue
    mm = [x["ret"] for x in t]
    se = 100 * statistics.pstdev(mm) / math.sqrt(len(mm))
    print(f"  bar {bar:.2f}: {len(t):4} trades, average price {100 * statistics.mean(x['price'] for x in t):5.1f}c, won {100 * statistics.mean(x['win'] for x in t):5.1f}%, return with fee {statistics.mean(mm):+6.1%} +-{se:.1f}, fee-free {statistics.mean(x['gross'] for x in t):+6.1%}")
