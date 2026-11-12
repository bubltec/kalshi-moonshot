"""One causal turn rule on every market: buy after the bid turns up, sell after it turns down. (2026-11-12, user: "do it"; the best-round-trip study found a timed round trip above 10% in 96% of markets in hindsight, median +127% per $1, but the curve cannot mark a trough as it forms, so this asks how much survives having to wait for the turn.)
The rule, stated before any result and run once (no sweep):
  unit u = the side's own typical minute: the root-mean-square one-second move of its bid over the last 10 minutes (at least 3), times sqrt(60). Nothing is in cents; no asset enters.
  BUY the side at its ask when its bid has risen K = 1 unit above the lowest bid it has had since minute 1, from minute 2, ask between 0.05 and 0.90, until minute 13. The first buy across the two sides of a market is the market's one trade.
  SELL at the bid (less the 0.02 exit slip and the taker fee) when the bid has fallen K = 1 unit from the highest bid since the buy; otherwise hold to settlement (1 or 0, no costs). The buy pays its fee.
Scored on every market that has a trade, as it happened. Reported: markets, trades, net dollars at $5 a trade and per $1 spent, how many trades won, the same buys held to settlement, how many the rule beat, how it ended (sold on the turn or held), by the price paid, by minute, and by curve shape (terciles of the spot's 13-minute level and slope at the buy, the model's own readings; the result must be positive in each shape before it goes to shadow). Moments every 5 seconds.
    scripts/cloud_job.sh scripts/turn_rule.py          # MAX_MARKETS=300 for a quick look
"""


import bisect, datetime as dt, itertools, math, os, statistics, time
from kalshi_moonshot import config, db, variants
from kalshi_moonshot.curvefeatures import moment_features
from kalshi_moonshot.kalshiws import RECORD_ONLY
from kalshi_moonshot.lanes import lane_specs
from kalshi_moonshot.runner import EXIT_SLIP
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
ET = dt.timezone(dt.timedelta(hours=-5))
MON = dt.datetime(2026, 11, 9, tzinfo=ET).timestamp()
HORIZ = (60, 120, 180)
MAXM = int(os.environ.get("MAX_MARKETS", "0"))
SELL_CAP = 0.90  # a bid above this is the winner being sold into settlement, the hindsight trade again; the waves are what is left
hm = lambda t: dt.datetime.fromtimestamp(t, ET).strftime("%m-%d %H:%M")  # noqa: E731
series = {sr for sp in lane_specs(s).values() if sp.enabled and sp.model_buy for sr in sp.series}
store = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
wins = store.db.execute("SELECT ticker,series,close_ts,strike,result FROM windows WHERE close_ts>? AND result IN ('yes','no') AND strike>0 ORDER BY series,close_ts", (MON,)).fetchall()
wins = [w for w in wins if w["series"] in series and w["series"] not in RECORD_ONLY]
if MAXM:
    wins = wins[-MAXM:]
now = time.time()
g0 = int(MON - 3 * 3600 - 300)
g1 = int(now) + 2


K = 1.0
STAKE = 5.0
ASK_LO, ASK_HI = 0.05, 0.90
opened_min, last_buy_min = 2.0, 13.0
cur_ix, g, sq, ts, vs = None, [], [0.0], [], []
T = []  # trades: dicts
nm = 0
for w in wins:
    ser = w["series"]
    crypto = variants.crypto_index(ser)
    ix = crypto or variants.biquote_index(ser)
    if not ix:
        continue
    if ix != cur_ix:
        cur_ix = ix
        r = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (ix, g0 - 60)).fetchall()
        ts, vs = [float(x[0]) for x in r], [float(x[1]) for x in r]
        g, j, last = [], 0, None
        for t in range(g0, g1):
            while j < len(ts) and ts[j] <= t:
                last = vs[j]
                j += 1
            g.append(math.log(last) if last else None)
        sq = [0.0]
        for a, b in itertools.pairwise(g):
            sq.append(sq[-1] + ((b - a) ** 2 if a is not None and b is not None else 0.0))
    close = float(w["close_ts"])
    opened = close - 900.0
    if crypto:
        strike_log = math.log(float(w["strike"]))
    else:
        a = bisect.bisect_left(ts, opened - 5.0)
        if a >= len(ts) or ts[a] > opened + 20.0:
            continue
        strike_log = math.log(vs[a])
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (w["ticker"], opened - 700, close)).fetchall()
    if len(q) < 20:
        continue
    nm += 1
    qt = [float(x[0]) for x in q]
    base = int(opened) - 700
    n = int(close) - base + 1
    yb, ya = [None] * n, [None] * n
    j = 0
    for i in range(n):
        t = base + i
        while j + 1 < len(qt) and qt[j + 1] <= t:
            j += 1
        if qt[j] <= t and t - qt[j] <= 20:
            yb[i], ya[i] = float(q[j][1]), float(q[j][2])
    sides = {}
    for side in ("yes", "no"):
        bid = [(yb[i] if side == "yes" else (1 - ya[i] if ya[i] is not None else None)) if yb[i] is not None else None for i in range(n)]
        ask = [(ya[i] if side == "yes" else 1 - yb[i]) if yb[i] is not None else None for i in range(n)]
        cs = [0.0]
        for i in range(1, n):
            cs.append(cs[-1] + ((bid[i] - bid[i - 1]) ** 2 if bid[i] is not None and bid[i - 1] is not None else 0.0))
        sides[side] = (bid, ask, cs)
    trade = None
    for side in ("yes", "no"):
        bid, ask, cs = sides[side]
        low = None
        for e in range(60, int(last_buy_min * 60) + 1, 5):
            i = int(opened) - base + e
            b = bid[i]
            if b is None:
                continue
            low = b if low is None else min(low, b)
            if e < opened_min * 60 or ask[i] is None or not (ASK_LO <= ask[i] <= ASK_HI):
                continue
            span = min(600, i - 1)
            if span < 180:
                continue
            u = math.sqrt((cs[i] - cs[i - span]) / span * 60.0)
            if u <= 0 or b - low < K * u:
                continue
            if trade is None or e < trade["e"]:
                trade = dict(side=side, e=e, ask=ask[i], i=i, u=u, ticker=w["ticker"])
            break
    if trade is None:
        continue
    bid, ask, cs = sides[trade["side"]]
    cost = trade["ask"] + fee(trade["ask"])
    high, sold = bid[trade["i"]], None
    for e in range(trade["e"] + 5, 900, 5):
        i = int(opened) - base + e
        if i >= n:
            break
        b = bid[i]
        if b is None:
            continue
        high = max(high, b)
        if high - b >= K * trade["u"] and b > 0:
            sold = (b - EXIT_SLIP - fee(b)) / cost - 1.0
            trade["sell_e"] = e
            break
    won = w["result"] == trade["side"]
    trade["held"] = (1.0 if won else 0.0) / cost - 1.0
    trade["net"] = sold if sold is not None else trade["held"]
    trade["how"] = "sold on the turn" if sold is not None else "held to settlement"
    k = int(opened) + trade["e"] - g0
    sign = 1 if trade["side"] == "yes" else -1
    f = moment_features(g, sq, k, strike_log, sign) if 0 <= k < len(g) else {}
    trade["level"], trade["slope"] = f.get("level_13m"), f.get("slope_13m")
    T.append(trade)
print(f"{nm} markets, {len(T)} with a trade since Monday 11-09; the tape ends {hm(max(float(w['close_ts']) for w in wins))} ET")


def line(label, xs):
    if not xs:
        return f"  {label:32s} n=0"
    net = [STAKE * x["net"] for x in xs]
    held = [STAKE * x["held"] for x in xs]
    beat = sum(1 for x in xs if x["net"] > x["held"] + 1e-9)
    lost = sum(1 for x in xs if x["net"] < x["held"] - 1e-9)
    return f"  {label:32s} n={len(xs):4d} | rule {sum(net):+9.2f} on ${STAKE * len(xs):8.2f} ({sum(net) / (STAKE * len(xs)):+6.1%}) | won {sum(1 for x in xs if x['net'] > 0):4d} | held-to-settlement {sum(held):+9.2f} ({sum(held) / (STAKE * len(xs)):+6.1%}) | rule beat held {beat}, lost to held {lost}"


print("\nthe rule, as it happened (K = 1 unit, buy 0.05-0.90 from minute 2 to 13, one trade per market)")
print(line("all trades", T))
for how in ("sold on the turn", "held to settlement"):
    print(line(how, [x for x in T if x["how"] == how]))
print("by the price paid")
for lo, hi in ((0.05, 0.20), (0.20, 0.40), (0.40, 0.60), (0.60, 0.90)):
    print(line(f"ask {lo:.2f}-{hi:.2f}", [x for x in T if lo <= x["ask"] < hi]))
print("by the minute of the buy")
for lo, hi in ((2, 4), (4, 7), (7, 10), (10, 14)):
    print(line(f"minute {lo}-{hi}", [x for x in T if lo * 60 <= x["e"] < hi * 60]))
for nmx in ("level", "slope"):
    v = sorted(x[nmx] for x in T if x[nmx] is not None)
    if len(v) > 90:
        c1, c2 = v[len(v) // 3], v[2 * len(v) // 3]
        print(f"by the spot's 13-minute {nmx} at the buy (cuts {c1:+.2f}, {c2:+.2f}; the cushion on the bet's side, in typical moves)")
        print(line("low", [x for x in T if x[nmx] is not None and x[nmx] < c1]))
        print(line("mid", [x for x in T if x[nmx] is not None and c1 <= x[nmx] < c2]))
        print(line("high", [x for x in T if x[nmx] is not None and x[nmx] >= c2]))
print("DONE")
