"""The user's hand-sold round trips by the price they bought at. (2026-11-19, user: "i know this works we just need to automate it, i can't do it fast enough myself")

Question: on the 15-minute positions the system recorded as opened by hand (manual_entries; sales are the account's fills on the ticker that are not the bot's orders), what did the round trips sold by hand before the close make, by the price paid (under 0.45, 0.45 to 0.60, 0.60 to 0.85, over 0.85), how long were they held, how much of the market was left at the buy and at the sale, how many round trips per market, and in the 0.60 to 0.85 band what they bought and sold at. Read-only; series are never used, only the price and the clock. Trades left to settlement are kept apart.
    scripts/cloud.sh run scripts/manual_roundtrips_by_price.py > studies/results/<date>-manual-roundtrips-by-price.txt
"""
import bisect
import collections
import math
import os
import time

from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store

os.nice(10)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
bot = {r[0] for r in st.db.execute("SELECT DISTINCT ticker FROM orders").fetchall()}
bot_ids = {r[0] for r in st.db.execute("SELECT order_id FROM orders WHERE order_id IS NOT NULL").fetchall()}
fills = st.db.execute("SELECT ts,ticker,book_side,count,yes_price,no_price,fee,order_id FROM fills ORDER BY ts").fetchall()
by = collections.defaultdict(list)
for f in fills:
    by[f["ticker"]].append(f)
# the buys the system itself recorded as made by hand (manual_entries), one position per ticker and side
man = collections.defaultdict(list)
for m in st.db.execute("SELECT ts,ticker,side,price,count,order_id FROM manual_entries ORDER BY ts").fetchall():
    man[(m["ticker"], m["side"])].append(m)
trips = []
for (tk, side), ms in man.items():
    ser = tk.split("-")[0]
    if not ser.endswith("15M"):
        continue
    w = st.db.execute("SELECT close_ts, strike, result FROM windows WHERE ticker=?", (tk,)).fetchone()
    if not w or w["result"] not in ("yes", "no"):
        continue
    close = float(w["close_ts"])
    t0 = float(ms[0]["ts"])
    bq = sum(float(m["count"] or 0.0) for m in ms)
    if bq <= 0:
        continue
    cost = sum(float(m["price"] or 0.0) * float(m["count"] or 0.0) for m in ms)
    sell_side = "ask" if side == "yes" else "bid"  # selling YES is an 'ask' fill; selling NO is a 'bid' fill
    sells = [f for f in by.get(tk, []) if f["book_side"] == sell_side and t0 < float(f["ts"]) < close and f["order_id"] not in bot_ids]
    px = lambda f: float(f["yes_price"]) if side == "yes" else (float(f["no_price"]) if f["no_price"] is not None else 1.0 - float(f["yes_price"]))  # noqa: E731
    raw = sum(float(f["count"]) for f in sells)
    sq = min(raw, bq)
    proceeds = sum(px(f) * float(f["count"]) for f in sells) * (sq / raw if raw else 0.0)
    fee = sum(float(f["fee"] or 0.0) for f in sells)
    settle = (bq - sq) * (1.0 if w["result"] == side else 0.0)
    trips.append({"tk": tk, "ser": ser, "side": side, "t_buy": t0, "t_sell": float(sells[-1]["ts"]) if sells else None, "close": close, "strike": w["strike"], "result": w["result"],
                  "bq": bq, "sq": sq, "entry": cost / bq, "exit": (proceeds / sq) if sq > 0 else None, "pnl": proceeds + settle - cost - fee, "held_pnl": bq * (1.0 if w["result"] == side else 0.0) - cost, "cost": cost})
import statistics

hand = [t for t in trips if t["sq"] >= 0.9 * t["bq"]]
ran = [t for t in trips if t["sq"] < 0.9 * t["bq"]]
print(f"hand positions settled: {len(trips)} | sold by hand: {len(hand)} (staked {sum(t['cost'] for t in hand):.2f}, made {sum(t['pnl'] for t in hand):+.2f}) | left to settle: {len(ran)} (staked {sum(t['cost'] for t in ran):.2f}, made {sum(t['pnl'] for t in ran):+.2f})")
print(f"first buy {time.strftime('%Y-%m-%d', time.gmtime(min(t['t_buy'] for t in trips)))} to last {time.strftime('%Y-%m-%d', time.gmtime(max(t['t_buy'] for t in trips)))}")
bands = (("under 0.45", 0, 0.45), ("0.45 to 0.60", 0.45, 0.60), ("0.60 to 0.85", 0.60, 0.85), ("over 0.85", 0.85, 1.01))
print("--- sold by hand, by the price paid")
for nm, lo, hi in bands:
    q = [t for t in hand if lo <= t["entry"] < hi]
    if not q:
        continue
    held = [t["t_sell"] - t["t_buy"] for t in q if t["t_sell"]]
    print(f"  {nm:13s} n {len(q):3d} | staked {sum(t['cost'] for t in q):7.2f} made {sum(t['pnl'] for t in q):+7.2f} ({100.0 * sum(t['pnl'] for t in q) / sum(t['cost'] for t in q):+.0f}%) | ahead {sum(1 for t in q if t['pnl'] > 0)} behind {sum(1 for t in q if t['pnl'] < 0)} | median paid {statistics.median(t['entry'] for t in q):.2f} sold {statistics.median(t['exit'] for t in q if t['exit']):.2f} | median held {statistics.median(held):.0f}s, left at buy {statistics.median(t['close'] - t['t_buy'] for t in q):.0f}s, at sale {statistics.median(t['close'] - t['t_sell'] for t in q if t['t_sell']):.0f}s | held to settlement would have made {sum(t['held_pnl'] for t in q):+.2f}")
print("--- left to settlement, by the price paid")
for nm, lo, hi in bands:
    q = [t for t in ran if lo <= t["entry"] < hi]
    if q:
        print(f"  {nm:13s} n {len(q):3d} | staked {sum(t['cost'] for t in q):7.2f} made {sum(t['pnl'] for t in q):+7.2f} ({100.0 * sum(t['pnl'] for t in q) / sum(t['cost'] for t in q):+.0f}%) | ahead {sum(1 for t in q if t['pnl'] > 0)} behind {sum(1 for t in q if t['pnl'] < 0)}")
print("--- hand-sold in 0.60 to 0.85: every trip (time ET, entry, exit, seconds held, left at buy, pnl, size)")
for t in sorted([t for t in hand if 0.60 <= t["entry"] < 0.85], key=lambda t: t["t_buy"]):
    print(f"  {time.strftime('%m-%d %H:%M', time.gmtime(t['t_buy'] - 5 * 3600))} {t['side']:3s} {t['entry']:.2f} -> {t['exit']:.2f} held {t['t_sell'] - t['t_buy']:4.0f}s left {t['close'] - t['t_buy']:4.0f}s pnl {t['pnl']:+6.2f} on {t['cost']:6.2f}")
print("--- round trips per market (positions on one ticker and way can be several buys and sells; counted: buys)")
cnt = collections.Counter(len(man[(t["tk"], t["side"])]) for t in hand)
print("  buys in a hand-sold position:", dict(sorted(cnt.items())))
