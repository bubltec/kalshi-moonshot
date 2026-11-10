"""Late-drop and volume, scored against what each bet's own live exit actually did (2026-11-10, user: "yes" to the stacked replay; reviewer: the earlier replays only compared with holding).
Question: on the same live bets, what is the net of "the exits the bet actually had, then also this rule if the bet is still held at that second" against what the bet actually made?
Baseline = the live result: decisions.exit_ts/exit_price when the bet was sold (proceeds = contracts x (exit price - fee) - stake), else held to the result. The bets were made under whatever exit settings were live then (shock settings changed on 11-09 at 22:00 ET; late-drop is new today), so the baseline is "what happened", not one fixed rule set.
Stacking: a rule can only act while the bet is still held, i.e. before the live exit_ts. If it fires first, it replaces the live sale with a sale at the bid 2 s later less the fee; otherwise the live result stands. Held bets (no live exit) get the rule's sale or their held result.
Rules: A late drop (W 30 / 45, K 1 / 2 bid-units, high 0 / 0.75); B Hyperliquid taker flow against the bet (h 30 / 60, K 3 / 4 / 6, L 180), only for the markets with a perp row; C late drop plus flow confirmation (late drop A with W 30, K 1, and the flow reading over the last 30 s at least F = 1 / 2 / 3), perp markets only.
Reported per rule: bets changed, net change against the actual result, live whole-stake losses (no live sale, lost, or sold for under 5% of the stake) turned partial, winners sold that the live result kept, halves, curve shapes. A rule counts only if positive in both halves and each shape it claims. Settings were chosen before the results.

    scripts/cloud_job.sh scripts/stacked_exit_replay.py
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
        "SELECT d.ts,d.ticker,d.lane,d.side,d.contracts,d.stake_usd,w.close_ts,w.result,d.exit_ts,d.exit_price,d.exit_reason FROM decisions d JOIN windows w ON w.ticker=d.ticker "
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



def live_result(r):
    n, cost = float(r["contracts"]), float(r["stake_usd"])
    if r["exit_ts"] is not None and r["exit_price"] is not None:
        p = float(r["exit_price"])
        return n * (p - fee(p)) - cost, float(r["exit_ts"])
    return (n if r["result"] == r["side"] else 0.0) - cost, None


def late_fire(r, base, bids, W, K, HI):
    close = int(float(r["close_ts"]))
    for t in range(max(close - W, int(float(r["ts"])) + 5), close - DELAY):
        k = t - base
        now = bids[k]
        if now is None:
            continue
        win = [b for b in bids[k - 60 : k] if b is not None]
        if len(win) < 20:
            continue
        ch = [(b - a) ** 2 for a, b in itertools.pairwise(bids[k - 300 : k + 1]) if a is not None and b is not None]
        if len(ch) < 100:
            continue
        unit = math.sqrt(sum(ch) / len(ch) * 60.0)
        if unit > 0 and max(win) >= HI and (max(win) - now) >= K * unit:
            sell = bids[k + DELAY]
            if sell is not None and sell >= 0.05:
                return t, sell
    return None


def flow_fire(r, base, bids, rd, K, L):
    close = int(float(r["close_ts"]))
    for t in range(max(close - L, int(float(r["ts"])) + 5), close - DELAY):
        got = rd.get(t)
        if got and got[0] >= K:
            sell = bids[t + DELAY - base]
            if sell is not None and sell >= 0.05:
                return t, sell
    return None


def stacked(r, live, live_ts, fire):
    """The result when the rule's sale (time, bid) is allowed to act only before the live sale."""
    if fire is None:
        return live, False
    t, sell = fire
    if live_ts is not None and t >= live_ts:
        return live, False
    n, cost = float(r["contracts"]), float(r["stake_usd"])
    return n * (sell - fee(sell)) - cost, True


for gname, (lanes, since) in GROUPS.items():
    data, left_out = [], 0
    bets = load(lanes, since)
    for r in bets:
        got = prepare(r)
        if not got:
            continue
        fl = flow(r, got[1], 0)
        data.append((r, *got, fl))
        if fl is None:
            left_out += 1
    print(f"\n=== {gname}: settled live bets {len(bets)} | replayed {len(data)} | with a perp {len(data) - left_out}")
    if len(data) < 20:
        print("  too few bets")
        continue
    lo, hi = shapes([e for _, e, _, _, _ in data])
    mid = sorted(float(r["ts"]) for r, *_ in data)[len(data) // 2]
    lives = [live_result(r) for r, *_ in data]
    stake = [float(r["stake_usd"]) for r, *_ in data]
    zeros = [i for i, (p, _) in enumerate(lives) if p <= -0.95 * stake[i]]
    sold_live = sum(1 for _, t in lives if t is not None)
    print(f"  actual live: net {sum(p for p, _ in lives):+.2f}, sold live {sold_live} of {len(data)}, live whole-stake losses {len(zeros)} (${sum(stake[i] for i in zeros):.2f})")

    def shape(e):
        return "unknown" if e is None else "choppy" if e <= lo else "straight" if e >= hi else "middle"

    rds = {h: [readings(r, *fl, h) if fl else None for r, e, base, bids, fl in data] for h in (30, 60)}
    rules = []
    for W in (30, 45):
        for K in (1, 2):
            for HI in (0.0, 0.75):
                rules.append((f"A late drop W{W} K{K} high{HI}", lambda i, W=W, K=K, HI=HI: late_fire(data[i][0], data[i][2], data[i][3], W, K, HI), False))
    for h in (30, 60):
        for K in (3, 4, 6):
            rules.append((f"B flow h{h} K{K} L180", lambda i, h=h, K=K: flow_fire(data[i][0], data[i][2], data[i][3], rds[h][i], K, 180) if rds[h][i] is not None else None, True))
    for F in (1, 2, 3):
        def conf(i, F=F):
            if rds[30][i] is None:
                return None
            f = late_fire(data[i][0], data[i][2], data[i][3], 30, 1, 0.0)
            if f is None:
                return None
            got = rds[30][i].get(f[0])
            return f if got and got[0] >= F else None
        rules.append((f"C late drop W30 K1 + flow h30 >= {F}", conf, True))
    out = []
    for name, fn, perp_only in rules:
        res = []
        for i, (r, e, base, bids, fl) in enumerate(data):
            if perp_only and fl is None:
                res.append((lives[i][0], False))
                continue
            res.append(stacked(r, lives[i][0], lives[i][1], fn(i)))
        d = [p - lives[i][0] for i, (p, _) in enumerate(res)]
        ch = [(x, i) for i, x in enumerate(d) if abs(x) > 1e-9]
        if not ch:
            out.append((0.0, name, 0, 0, 0, 0.0, 0.0, {}))
            continue
        h1 = sum(x for x, i in ch if float(data[i][0]["ts"]) <= mid)
        h2 = sum(x for x, i in ch if float(data[i][0]["ts"]) > mid)
        by = {}
        for x, i in ch:
            by[shape(data[i][1])] = by.get(shape(data[i][1]), 0.0) + x
        fixed = sum(1 for i in zeros if res[i][1] and res[i][0] > -0.95 * stake[i])
        lost_win = sum(1 for (p, f), (lv, _) in zip(res, lives, strict=True) if f and p < lv - 1e-9 and lv > 0)
        out.append((sum(d), name, len(ch), fixed, lost_win, h1, h2, by))
    print("  rule: change vs actual | bets changed | live zeros turned partial (of %d) | winners cut | halves | shapes" % len(zeros))
    for o in sorted(out, key=lambda o: -o[0]):
        sh = " ".join(f"{k} {v:+.1f}" for k, v in sorted(o[7].items()))
        flag = " <- positive both halves" if o[5] > 0 and o[6] > 0 else ""
        print(f"    {o[1]}: {o[0]:+.2f} | {o[2]} | {o[3]} | {o[4]} | {o[5]:+.2f} / {o[6]:+.2f} | {sh}{flag}")
print("DONE")
