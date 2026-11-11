"""The user's own hand-made scalps (2026-11-11, user: "you can look at my scalps yes"; trades left to run to settlement are treated as unwatched and kept apart, at the user's caution that many were forgotten).
Question: on the 15-minute positions the system recorded as opened by hand (the manual_entries table; sales are the account's fills on that ticker that are not the bot's orders), what did the round trips that were SOLD BY HAND before the close make, and what was the curve doing at the buy and at the sale? Read-only.
A round trip: the first fill on a ticker opens a position (a YES bought at yes_price, or a NO bought at no_price); fills the same way add to it; fills the other way before the close sell it. "Hand-sold" = at least 90% of it sold before the close; the rest is "ran to settlement" and only counted, not studied.
Curve at the buy and at the sale, in the market's own units (typical minute going into the window): the cushion toward the side bought, the 5-minute and 1-minute slopes toward that side (15 s averages), the seconds left. After the sale: how much higher and lower the side's bid went before the close.
    scripts/cloud.sh run scripts/manual_scalps_review.py
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
hand = [t for t in trips if t["sq"] >= 0.9 * t["bq"]]
ran = [t for t in trips if t["sq"] < 0.9 * t["bq"]]
print(f"15-minute positions opened by hand (manual_entries) and settled: {len(trips)} | sold by hand before the close: {len(hand)} | ran to settlement (set aside): {len(ran)}")
print(f"  hand-sold: staked {sum(t['cost'] for t in hand):.2f}, made {sum(t['pnl'] for t in hand):+.2f} ({sum(1 for t in hand if t['pnl'] > 0)} ahead, {sum(1 for t in hand if t['pnl'] < 0)} behind); the same trades held to settlement would have made {sum(t['held_pnl'] for t in hand):+.2f}")
print(f"  ran to settlement: staked {sum(t['cost'] for t in ran):.2f}, made {sum(t['pnl'] for t in ran):+.2f} ({sum(1 for t in ran if t['pnl'] > 0)} ahead, {sum(1 for t in ran if t['pnl'] < 0)} behind)")


def curve(t):
    ix = variants.crypto_index(t["ser"]) or variants.biquote_index(t["ser"])
    if not ix:
        return None
    close = t["close"]
    tk = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts", (ix, close - 2720, close)).fetchall()
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (t["tk"], close - 900, close)).fetchall()
    if len(tk) < 1200 or float(tk[0][0]) > close - 2650:
        return None
    T_ = [float(x[0]) for x in tk]
    base = int(close) - 2700
    g = []
    j = 0
    last = None
    for sec in range(base, int(close) + 1):
        while j < len(T_) and T_[j] <= sec:
            last = float(tk[j][1])
            j += 1
        g.append(math.log(last) if last else None)
    o = 1800
    if any(x is None for x in g[o - 1000 :]):
        return None
    seg = g[o - 900 : o + 1]
    u = math.sqrt(sum((b - a) ** 2 for a, b in zip(seg, seg[1:])) / 900.0) * math.sqrt(60.0)
    if u <= 0:
        return None
    crypto = variants.crypto_index(t["ser"])
    if crypto:
        if not t["strike"] or float(t["strike"]) <= 0:
            return None
        ref = math.log(float(t["strike"]))
    else:
        ref = g[o]
    sgn = 1.0 if t["side"] == "yes" else -1.0

    def a15(k):
        return sum(g[k - 14 : k + 1]) / 15.0

    def read(ts):
        k = int(ts) - base
        if k < o - 600 or k >= len(g):
            return None
        return {"cush": (a15(k) - ref) / u * sgn, "s5": (a15(k) - a15(k - 300)) / 5.0 / u * sgn, "s1": (a15(k) - a15(k - 60)) / u * sgn, "left": close - ts}

    out = {"buy": read(t["t_buy"]), "sell": read(t["t_sell"]) if t["t_sell"] else None, "after_hi": None, "after_lo": None}
    if t["t_sell"] and q:
        after = [(float(x[1]) if t["side"] == "yes" else 1.0 - float(x[2])) for x in q if float(x[0]) > t["t_sell"]]
        if after:
            out["after_hi"], out["after_lo"] = max(after), min(after)
    return out


rows = []
for t in sorted(hand, key=lambda x: x["t_buy"]):
    c = curve(t)
    if c and c["buy"] and c["sell"]:
        rows.append((t, c))
print(f"\nhand-sold scalps with a curve reading at the buy and the sale: {len(rows)} of {len(hand)}")
if rows:
    def med(xs):
        xs = sorted(xs)
        return xs[len(xs) // 2] if xs else float("nan")

    for lab, S in (("all", rows), ("ended ahead", [r for r in rows if r[0]["pnl"] > 0]), ("ended behind", [r for r in rows if r[0]["pnl"] <= 0])):
        if not S:
            continue
        print(f"  {lab}: {len(S)} | made {sum(r[0]['pnl'] for r in S):+.2f} on {sum(r[0]['cost'] for r in S):.2f} staked | medians: paid {med([r[0]['entry'] for r in S]):.2f}, sold {med([r[0]['exit'] for r in S]):.2f}, held {med([r[0]['t_sell'] - r[0]['t_buy'] for r in S]):.0f}s, bought with {med([r[1]['buy']['left'] for r in S]):.0f}s left, sold with {med([r[1]['sell']['left'] for r in S]):.0f}s left")
        print(f"      at the buy: cushion toward the side {med([r[1]['buy']['cush'] for r in S]):+.2f} typical minutes, 5-minute slope {med([r[1]['buy']['s5'] for r in S]):+.2f}/min, 1-minute slope {med([r[1]['buy']['s1'] for r in S]):+.2f}/min | at the sale: cushion {med([r[1]['sell']['cush'] for r in S]):+.2f}, 1-minute slope {med([r[1]['sell']['s1'] for r in S]):+.2f}/min | after the sale the bid went up to {med([r[1]['after_hi'] - r[0]['exit'] for r in S if r[1]['after_hi'] is not None]):+.2f} over the sale price and down to {med([r[1]['after_lo'] - r[0]['exit'] for r in S if r[1]['after_lo'] is not None]):+.2f}")
    print("  by the setup at the buy:")
    def setup(r):
        b = r[1]["buy"]
        if b["cush"] < 0 and b["s5"] < 0:
            return "behind the target and still falling away (a trough buy)"
        if b["cush"] < 0:
            return "behind the target, already turning toward it"
        if b["s5"] < 0:
            return "ahead of the target, slipping back"
        return "ahead of the target and moving further ahead"
    g2 = collections.defaultdict(list)
    for r in rows:
        g2[setup(r)].append(r)
    for k, S in sorted(g2.items(), key=lambda kv: -len(kv[1])):
        print(f"      {k:56s} n {len(S):3d} | ahead {sum(1 for r in S if r[0]['pnl'] > 0):3d} | made {sum(r[0]['pnl'] for r in S):+7.2f} on {sum(r[0]['cost'] for r in S):7.2f} | median paid {med([r[0]['entry'] for r in S]):.2f}, sold {med([r[0]['exit'] for r in S]):.2f}")
    print("  by the price paid:")
    for lo, hi in ((0.0, 0.25), (0.25, 0.45), (0.45, 0.60), (0.60, 0.80), (0.80, 1.01)):
        S = [r for r in rows if lo <= r[0]["entry"] < hi]
        if S:
            print(f"      paid {lo:.2f} to {hi:.2f}: n {len(S):3d} | ahead {sum(1 for r in S if r[0]['pnl'] > 0):3d} | made {sum(r[0]['pnl'] for r in S):+7.2f} on {sum(r[0]['cost'] for r in S):7.2f}")
    print("  the 30 most recent (time ET, market, side, paid -> sold, contracts, made, seconds held, cushion and 1-minute slope at the buy -> at the sale, bid high after the sale):")
    for t, c in rows[-30:]:
        print(f"    {time.strftime('%m-%d %H:%M', time.gmtime(t['t_buy'] - 5 * 3600))} {t['ser']:15s} {t['side']:3s} {t['entry']:.2f} -> {t['exit']:.2f} x{t['bq']:6.1f} {t['pnl']:+7.2f} {t['t_sell'] - t['t_buy']:4.0f}s | {c['buy']['cush']:+.1f} {c['buy']['s1']:+.2f} -> {c['sell']['cush']:+.1f} {c['sell']['s1']:+.2f} | high after {c['after_hi'] if c['after_hi'] is not None else float('nan'):.2f}, result {t['result']}")
print("DONE")
