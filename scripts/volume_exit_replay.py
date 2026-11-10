"""Spot volume as the exit (2026-11-10, user: "I would love it if we didn't have an exit rule and the curve logic could handle exits ... volume spiking on the opposite side held means sell"; "look at the spot volume on Hyperliquid, a tool just for the markets it applies to").
Question: on the same live bets, does selling when taker volume on the Hyperliquid perpetual turns against the bet, in units of that perp's own normal flow, beat holding, and how many whole-stake losses does it remove?
Coverage: tape.fut_1s records taker buy and sell dollars per second for the perps of BTC, ETH, SOL, XRP, DOGE and (since 10-30) gold, WTI, Brent. The study uses only live bets on markets with a perp row (the others are left out and counted). A perp is a future, not the spot the market settles on; the study measures whether its flow predicts the loss, not that it is the settlement price.
Reading: net flow of a second = taker buy dollars - taker sell dollars, signed with the bet (YES wants buying, NO wants selling): adverse flow = -(signed net). Over h seconds the adverse flow is summed and divided by that perp's own normal: the root-mean-square one-second net flow over the 900 s before, times sqrt(h). Rule: at a second with L or fewer seconds left, sell when the reading is at least K; "px" variants also need the perp's price to have moved against the bet over the same h seconds. Sale at the bid 2 s later less the fee.
Variants fixed before the results: h 10 / 30 / 60; K 2 / 3 / 4 / 6; price confirm off / on; L 60 / 180 / 300.
Reported per variant: bets sold, net change against holding, whole-stake losses turned partial (of the held-to-result whole losses), winners sold, the two halves (median ts), curve shapes (terciles of spot path efficiency over the 5 minutes before the buy). A variant counts only when positive in both halves and in each shape it claims.

    scripts/cloud_job.sh scripts/volume_exit_replay.py
"""

import bisect
import calendar
import itertools
import math
import os

from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

os.nice(10)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)

GROUPS = {
    "crypto": (("crypto_multiscale", "crypto_multiscale_underdog"), int(os.environ.get("SINCE_CRYPTO", "1794000000"))),
    "commodity_multiscale": (("commodity_multiscale",), calendar.timegm((2026, 11, 9, 14, 0, 0))),
    "favorites": (("commodity_energy", "commodity_wti", "commodity_precious", "commodity_industrial"), calendar.timegm((2026, 11, 3, 0, 0, 0))),
}
DELAY = 2


def load(lanes, since):
    marks = ",".join("?" * len(lanes))
    rows = st.db.execute(
        "SELECT d.ts,d.ticker,d.lane,d.side,d.contracts,d.stake_usd,w.close_ts,w.result FROM decisions d JOIN windows w ON w.ticker=d.ticker "
        f"WHERE d.action='bet' AND d.mode='trade' AND d.contracts>0 AND d.stake_usd>0 AND d.ts>? AND d.lane IN ({marks}) AND w.result IN ('yes','no') ORDER BY d.ts",
        (since, *lanes),
    ).fetchall()
    seen = {}
    for r in rows:
        seen[(r["ticker"], r["side"])] = r
    return list(seen.values())


def prepare(r):
    """(path efficiency before the buy, per-second held bid from the buy to the close or None)."""
    ser = r["ticker"].split("-")[0]
    ix = variants.crypto_index(ser) or variants.biquote_index(ser)
    close, t0 = float(r["close_ts"]), float(r["ts"])
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (r["ticker"], t0 - 400, close)).fetchall()
    if len(q) < 10:
        return None
    eff = None
    if ix:
        tk = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts", (ix, t0 - 300, t0)).fetchall()
        if len(tk) > 30:
            seg = [math.log(float(x[1])) for x in tk]
            path = sum(abs(b - a) for a, b in itertools.pairwise(seg))
            eff = abs(seg[-1] - seg[0]) / path if path > 0 else None
    qt = [float(x[0]) for x in q]
    bids = []
    for t in range(int(t0) - 400, int(close) + 1):
        i = bisect.bisect_right(qt, t) - 1
        if i < 0 or t - qt[i] > 20:
            bids.append(None)
            continue
        yb, ya = float(q[i][1]), float(q[i][2])
        bids.append(yb if r["side"] == "yes" else 1.0 - ya)
    return eff, int(t0) - 400, bids



def shapes(effs):
    e = sorted(x for x in effs if x is not None)
    return (e[len(e) // 3], e[2 * len(e) // 3]) if len(e) >= 9 else (0.0, 1.0)


SYM = {"KXBTC15M": "BTC", "KXETH15M": "ETH", "KXSOL15M": "SOL", "KXXRP15M": "XRP", "KXDOGE15M": "DOGE", "KXGOLD15M": "xyz:GOLD", "KXWTI15M": "xyz:CL", "KXBRENT15M": "xyz:BRENTOIL"}


def flow(r, base, n):
    """Per-second perp (net buy dollars, last price) on the bet's grid, or None when the market has no perp row."""
    sym = SYM.get(r["ticker"].split("-")[0])
    if not sym:
        return None
    rows = tape.execute("SELECT ts,px,buy_usd,sell_usd FROM fut_1s WHERE symbol=? AND ts BETWEEN ? AND ? ORDER BY ts", (sym, base - 900, int(float(r["close_ts"])) + 1)).fetchall()
    if len(rows) < 100:
        return None
    start = base - 900
    size = int(float(r["close_ts"])) + 2 - start
    net, px = [0.0] * size, [None] * size
    for ts, p, b, s_ in rows:
        k = int(ts) - start
        if 0 <= k < size:
            net[k] += float(b) - float(s_)
            px[k] = float(p)
    last = None
    for k in range(size):
        if px[k] is None:
            px[k] = last
        else:
            last = px[k]
    return start, net, px


def readings(r, start, net, px, h):
    """(second, adverse-flow reading, perp price move against the bet in log units) for the seconds from the buy to the close."""
    sign = 1.0 if r["side"] == "yes" else -1.0
    cum, c2 = [0.0], [0.0]
    for x in net:
        cum.append(cum[-1] + x)
        c2.append(c2[-1] + x * x)
    out = {}
    for t in range(int(float(r["ts"])) + 5, int(float(r["close_ts"])) - 3):
        k = t - start
        if k < 900 or k >= len(net):
            continue
        rms = math.sqrt((c2[k] - c2[k - 900]) / 900.0)
        if rms <= 0:
            continue
        adv = -sign * (cum[k + 1] - cum[k + 1 - h])
        move = None
        if px[k] and px[k - h]:
            move = -sign * math.log(px[k] / px[k - h])
        out[t] = (adv / (rms * math.sqrt(h)), move)
    return out


def decide_v(r, base, bids, rd, K, confirm, L):
    n, cost = float(r["contracts"]), float(r["stake_usd"])
    hold = (n if r["result"] == r["side"] else 0.0) - cost
    close = int(float(r["close_ts"]))
    for t in range(max(close - L, int(float(r["ts"])) + 5), close - DELAY):
        got = rd.get(t)
        if not got or got[0] < K or (confirm and (got[1] is None or got[1] <= 0)):
            continue
        sell = bids[t + DELAY - base]
        if sell is None or sell < 0.05:
            continue
        return n * (sell - fee(sell)) - cost, True
    return hold, False


for gname, (lanes, since) in GROUPS.items():
    data, left_out = [], 0
    bets = load(lanes, since)
    for r in bets:
        got = prepare(r)
        if not got:
            continue
        fl = flow(r, got[1], 0)
        if fl is None:
            left_out += 1
            continue
        data.append((r, *got, fl))
    print(f"\n=== {gname}: settled live bets {len(bets)} | with a perp {len(data)} | without (left out) {left_out}")
    if len(data) < 20:
        print("  too few bets")
        continue
    lo, hi = shapes([e for _, e, _, _, _ in data])
    mid = sorted(float(r["ts"]) for r, *_ in data)[len(data) // 2]
    stake = [float(r["stake_usd"]) for r, *_ in data]
    hold = [((float(r["contracts"]) if r["result"] == r["side"] else 0.0) - c) for (r, *_), c in zip(data, stake, strict=True)]
    zeros = [i for i, h in enumerate(hold) if abs(h + stake[i]) < 1e-9]
    print(f"  held: net {sum(hold):+.2f}, whole-stake losses {len(zeros)} of {len(data)}, shape cutoffs {lo:.3f} {hi:.3f}")

    def shape(e):
        return "unknown" if e is None else "choppy" if e <= lo else "straight" if e >= hi else "middle"

    out = []
    for h in (10, 30, 60):
        rds = [readings(r, *fl, h) for r, e, base, bids, fl in data]
        for K in (2, 3, 4, 6):
            for confirm in (False, True):
                for L in (60, 180, 300):
                    res = [decide_v(r, base, bids, rd, K, confirm, L) for (r, e, base, bids, fl), rd in zip(data, rds, strict=True)]
                    d = [p - x for (p, _), x in zip(res, hold, strict=True)]
                    ch = [(x, i) for i, x in enumerate(d) if abs(x) > 1e-9]
                    if not ch:
                        continue
                    h1 = sum(x for x, i in ch if float(data[i][0]["ts"]) <= mid)
                    h2 = sum(x for x, i in ch if float(data[i][0]["ts"]) > mid)
                    by = {}
                    for x, i in ch:
                        by[shape(data[i][1])] = by.get(shape(data[i][1]), 0.0) + x
                    fixed = sum(1 for i in zeros if abs(res[i][0] + stake[i]) > 1e-9)
                    wins = sum(1 for (p, f), x in zip(res, hold, strict=True) if f and x > 0)
                    saved = sum(1 for (p, f), x in zip(res, hold, strict=True) if f and x < 0)
                    out.append((sum(d), (h, K, "px" if confirm else "flow", L), len(ch), saved, wins, fixed, h1, h2, by))
    out.sort(key=lambda o: -o[0])
    print("  best 15 by net change against holding: (h, K, confirm, L)")
    for o in out[:15]:
        sh = " ".join(f"{k} {v:+.1f}" for k, v in sorted(o[8].items()))
        print(f"    {o[1]}: vs holding {o[0]:+.2f} on {o[2]} sold | losers saved {o[3]}, winners sold {o[4]}, zeros fixed {o[5]} of {len(zeros)} | halves {o[6]:+.2f} / {o[7]:+.2f} | {sh}")
    both = [o for o in out if o[6] > 0 and o[7] > 0]
    print(f"  positive in both halves: {len(both)} of {len(out)}; best by zeros fixed among them:")
    for o in sorted(both, key=lambda o: (-o[5], -o[0]))[:6]:
        print(f"    {o[1]}: vs holding {o[0]:+.2f} | zeros fixed {o[5]} of {len(zeros)} | winners sold {o[4]} | halves {o[6]:+.2f} / {o[7]:+.2f}")
print("DONE")
