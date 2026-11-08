"""The strike-cross exit on crypto_multiscale (2026-11-08, review of the commit that set cross_units = 0.0 on the lane): the lane buys behind its strike most of the time (82% of 168 settled bets had the spot already on the wrong side at the buy), and the study behind the setting (studies/results/2026-11-07-strike-cross-replay.txt, +$82.69 on 664 live bets) was mostly favourites.
Question: on the live crypto_multiscale bets since 11-07 (and, for comparison, the other crypto lanes), what does selling in the last 300 s whenever the cushion is at or under 0 typical minutes (cross_units = 0.0) at a bid of 0.05-0.89 do against holding to the result, split by whether the bet was bought ahead of or behind its strike?
Replay: the first second inside the last 300 s with cushion <= 0 (the spot's distance to the strike on the bet's side; inside the last 60 s the cushion uses the settlement average projected as watcher._shock_hit does: the known ticks and the spot now for the rest), bid 0.05-0.89, sold at the displayed bid with the full taker fee. Change = sale proceeds - what the position pays at the result (contracts of the decision). Reported: n, net, mean per bet with its standard error, beat / lost, two halves each with its standard error, by lane group and by ahead / behind at the buy, and by the bid at the sale.
Not covered: the live shock exit and value exit running alongside (the first to fire wins live), real fills.

    scripts/hostrun3.sh-style: docker compose exec -T bot python - < scripts/cross_exit_multiscale_replay.py
"""

import bisect
import calendar
import math
import os

from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

os.nice(15)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
t0 = calendar.timegm((2026, 11, 2, 6, 0, 0))
LAST = 300
wins = {w["ticker"]: w for w in st.db.execute("SELECT ticker,series,close_ts,strike,result FROM windows WHERE close_ts>? AND result IN ('yes','no') AND strike>0", (t0,)).fetchall()}
bets = [b for b in st.db.execute("SELECT ts,ticker,lane,side,ask,contracts,stake_usd FROM decisions WHERE action='bet' AND mode='trade' AND lane LIKE 'crypto%' AND ts>? AND contracts>0 ORDER BY ts", (t0,)).fetchall() if b["ticker"] in wins]
G0 = int(t0 - 1200)
G1 = int(max(float(w["close_ts"]) for w in wins.values())) + 2
GRID = {}


def grid(ix):
    r = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (ix, G0 - 60)).fetchall()
    ts = [float(x[0]) for x in r]
    vs = [float(x[1]) for x in r]
    g, j, last = [], 0, None
    for t in range(G0, G1):
        while j < len(ts) and ts[j] <= t:
            last = vs[j]
            j += 1
        g.append(math.log(last) if last else None)
    return g


def one(b):
    w = wins[b["ticker"]]
    ix = variants.crypto_index(w["series"])
    if not ix:
        return None
    if ix not in GRID:
        GRID[ix] = grid(ix)
    g = GRID[ix]
    close = float(w["close_ts"])
    K = math.log(float(w["strike"]))
    sign = 1.0 if b["side"] == "yes" else -1.0
    k0 = int(b["ts"]) - G0
    if g[k0] is None:
        return None
    behind = (g[k0] - K) * sign < 0
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (b["ticker"], b["ts"] - 20, close)).fetchall()
    if len(q) < 5:
        return None
    qt = [float(x[0]) for x in q]
    n = float(b["contracts"])
    hold = n if w["result"] == b["side"] else 0.0
    for t in range(max(int(b["ts"]), int(close) - LAST), int(close) - 5):
        k = t - G0
        if g[k] is None:
            continue
        left = close - t
        cush = (g[k] - K) * sign
        if left < 60.0:
            known = [math.exp(x) for x in g[int(close - 60) - G0 : k + 1] if x is not None]
            if known:
                secs = min(60.0, max(1.0, 60.0 - left))
                proj = (sum(known) / len(known) * secs + math.exp(g[k]) * (60.0 - secs)) / 60.0
                cush = (math.log(proj) - K) * sign
        if cush > 0:
            continue
        i = bisect.bisect_right(qt, t) - 1
        if i < 0 or t - qt[i] > 20:
            continue
        bid = float(q[i][1]) if b["side"] == "yes" else 1.0 - float(q[i][2])
        if not (0.05 <= bid <= 0.89):
            continue
        return {"left": left, "bid": bid, "change": n * bid - n * fee(bid) - hold, "ts": b["ts"], "lane": b["lane"], "behind": behind}
    return {"none": True, "behind": behind, "lane": b["lane"], "ts": b["ts"]}


res = [r for r in (one(b) for b in bets) if r]
mid = sorted(r["ts"] for r in res)[len(res) // 2]
print("live crypto bets replayed:", len(res), "| sold by the cross rule:", sum(1 for r in res if not r.get("none")))


def ms(xs):
    n = len(xs)
    if n == 0:
        return "n=0"
    m = sum(xs) / n
    se = (sum((x - m) ** 2 for x in xs) / max(n - 1, 1)) ** 0.5 / math.sqrt(n)
    return f"{m:+.3f} +/- {se:.3f} (n={n})"


def line(label, rs):
    sells = [r for r in rs if not r.get("none")]
    if not sells:
        print(f"   {label:34s} sells 0 of {len(rs)} bets")
        return
    ch = [r["change"] for r in sells]
    a = [r["change"] for r in sells if r["ts"] <= mid]
    h = [r["change"] for r in sells if r["ts"] > mid]
    print(f"   {label:34s} sells {len(sells):3d} of {len(rs):3d} bets | net ${sum(ch):+8.2f} | per sale {ms(ch)} | beat hold {sum(1 for c in ch if c > 0)} lost {sum(1 for c in ch if c <= 0)} | first half {ms(a)} | second half {ms(h)}")


ms_lane = [r for r in res if r["lane"] == "crypto_multiscale"]
other = [r for r in res if r["lane"] != "crypto_multiscale"]
print("\nthe cross exit (cross_units = 0.0) replayed in the last 300 s vs holding:")
line("crypto_multiscale", ms_lane)
line("  bought behind its strike", [r for r in ms_lane if r["behind"]])
line("  bought ahead of its strike", [r for r in ms_lane if not r["behind"]])
line("the other crypto lanes", other)
line("  bought behind its strike", [r for r in other if r["behind"]])
line("  bought ahead of its strike", [r for r in other if not r["behind"]])
print("\ncrypto_multiscale sales by the bid at the sale:")
for lo, hi in ((0.05, 0.15), (0.15, 0.30), (0.30, 0.60), (0.60, 0.90)):
    line(f"  bid {lo:.2f}-{hi:.2f}", [r for r in ms_lane if r.get("none") or lo <= r["bid"] < hi] if False else [r for r in ms_lane if not r.get("none") and lo <= r["bid"] < hi])
print("DONE")
