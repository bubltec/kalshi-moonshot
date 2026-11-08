"""The crypto_multiscale lane's whole book replayed on the commodity markets, same settings (2026-11-08, user: "i want to replay commodity (commodity_favorites is just a template, it doesn't run) with the same settings as crypto_multiscale").

Question: what would the commodity markets (gold, silver, platinum, palladium, WTI, Brent, natgas, copper; every settled 15-minute window since 11-02) have returned under crypto_multiscale's settings, from data/strategies.toml as of 2026-11-08?
Buy: the curve model (curvemodel.fit, the live ridge on the 18 Fibonacci readings plus the ask) fit on the EARLIER days only (the first two days only train), buy the first moment per market side, minutes 2-14, ask 0.20-0.70, spread 0.04 or less, where the predicted net per $1 is in the top 20% of the training moments (model_top 0.20) and above zero. Moments are every 30 s. Stake $3 scaled by price (stake_price_full 0.70, a quarter of it at 0.20, in a straight line). Bought at the ask, no resting-buy fill model.
Exits, each in the market's own typical minute (the one-second RMS of the last 15 minutes x sqrt(60)): shock exit (last 300 s: the worst 60 s move in the last 120 s is 1.0 typical minute or more against the bet and the spot is within 0.6 typical minutes of the reference), cross exit (cross_units 0: the spot is on the wrong side of the reference, last 300 s), both only for a bid of 0.05-0.89; value exit (value_exit 0.02: bid after the fee over the chance that the side pays by 0.02, bid 0.30-0.70, never a loss after both fees; the chance is the commodity-fit logistic of scripts/commodity_exit_replay.py, not the crypto-trained confidence model, which is not available). The first exit that fires sells at the bid less 0.01.
Reference: the index's own price at the window open (the lane's shock_ref for BiQuote commodities), not Kalshi's strike. Fees on both legs.
Variants, in the order exits are added: held to the result; + shock; + shock + cross; + shock + cross + value (all three are the lane's settings). Reported: bets, dollars staked, net dollars, net per $ staked, win rate, sold, two halves, minutes in, curve shapes (path efficiency of the spot in the 5 minutes before the buy, in thirds), per test day (UTC day 20763 = Friday 11-06).
Not covered: fills (the resting buy, the sell queue), the lane's other gates, the 60 s settlement average in the last minute, a stale tick check.

    scripts/cloud.sh run scripts/commodity_multiscale_replay.py
"""

import bisect
import calendar
import collections
import itertools
import math
import os

from kalshi_moonshot import config, curvemodel, db
from kalshi_moonshot.biquotefeed import index_for_series
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

os.nice(15)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
t0 = calendar.timegm((2026, 11, 2, 13, 0, 0))
SER = ("KXGOLD15M", "KXSILVER15M", "KXPLATINUM15M", "KXPALLADIUM15M", "KXWTI15M", "KXBRENT15M", "KXNATGAS15M", "KXCOPPER15M")
G0 = int(t0 - 3800)
G1 = calendar.timegm((2026, 11, 8, 14, 0, 0))
LOOK13, LOOK55 = 13 * 60, 55 * 60
SQ = "(" + ",".join("?" * len(SER)) + ")"
wins = st.db.execute(f"SELECT ticker,series,close_ts,result FROM windows WHERE close_ts>? AND series IN {SQ} AND result IN ('yes','no') ORDER BY series,close_ts", (t0, *SER)).fetchall()
print("commodity windows", len(wins), flush=True)
GR, REF = {}, {}
rows, meta = [], []  # rows: (18 readings, ask, net per $1); meta: (close, elapsed, ticker, side, bid, spread, eff, day, ser)
vmoments = []  # (day, [1, z, s13, s55, logit bid], won) for the value exit's chance


def vfeat(g, sq, k, ref_log, sg, left, bid):
    if k - LOOK55 < 0 or k >= len(g) or g[k] is None or g[k - LOOK13] is None or g[k - LOOK55] is None or left <= 1 or not 0 < bid < 1:
        return None
    r13 = math.sqrt((sq[k] - sq[k - LOOK13]) / LOOK13)
    r55 = math.sqrt((sq[k] - sq[k - LOOK55]) / LOOK55)
    if r13 <= 0 or r55 <= 0:
        return None
    c = lambda v: max(-6.0, min(6.0, v))
    return [1.0, c((g[k] - ref_log) * sg / (r13 * math.sqrt(left))), c((g[k] - g[k - LOOK13]) * sg / (r13 * math.sqrt(LOOK13))), c((g[k] - g[k - LOOK55]) * sg / (r55 * math.sqrt(LOOK55))), math.log(bid / (1 - bid))]


quotes_of = {}
for ser in SER:
    ix = index_for_series(ser)
    r = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (ix, G0 - 3600)).fetchall()
    ts, vs = [float(x[0]) for x in r], [float(x[1]) for x in r]
    g, j, last = [], 0, None
    for t in range(G0, G1):
        while j < len(ts) and ts[j] <= t:
            last = vs[j]
            j += 1
        g.append(math.log(last) if last else None)
    sq = [0.0]
    for a, b in itertools.pairwise(g):
        sq.append(sq[-1] + ((b - a) ** 2 if a is not None and b is not None else 0.0))
    GR[ser] = (g, sq)
    print("grid built", ser, len(rows), flush=True)
    for w in (x for x in wins if x["series"] == ser):
        close = float(w["close_ts"])
        opened = close - 900.0
        a = bisect.bisect_left(ts, opened - 5.0)
        if a >= len(ts) or ts[a] > opened + 20.0:
            continue
        ref_log = math.log(vs[a])
        REF[w["ticker"]] = ref_log
        q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (w["ticker"], opened - 20, close)).fetchall()
        if len(q) < 20:
            continue
        quotes_of[w["ticker"]] = [(float(x[0]), float(x[1]), float(x[2])) for x in q]
        qt = [x[0] for x in quotes_of[w["ticker"]]]
        for e in range(120, 841, 30):
            at = opened + e
            i = bisect.bisect_right(qt, at) - 1
            if i < 0 or at - qt[i] > 20:
                continue
            yb, ya = quotes_of[w["ticker"]][i][1], quotes_of[w["ticker"]][i][2]
            k = int(at) - G0
            seg = [math.exp(x) for x in g[k - 300 : k + 1] if x is not None] if k >= 300 else []
            path = sum(abs(math.log(b / a_)) for a_, b in itertools.pairwise(seg)) if len(seg) > 10 else 0.0
            eff = abs(math.log(seg[-1] / seg[0])) / path if path > 0 else None
            for side, bid, ask in (("yes", yb, ya), ("no", 1 - ya, 1 - yb)):
                sg = 1 if side == "yes" else -1
                won = w["result"] == side
                if 0.20 <= bid <= 0.97 and ask - bid <= 0.06:
                    f = vfeat(g, sq, k, ref_log, sg, close - at, bid)
                    if f is not None:
                        vmoments.append((int(close // 86400), f, int(won)))
                if 0.20 <= ask <= 0.70 and ask - bid <= 0.04:
                    read = curvemodel.features(g, sq, k, ref_log, sg)
                    if read is not None:
                        rows.append((read, ask, ((1 - ask) / ask if won else -1.0) - fee(ask) / ask))
                        meta.append((close, float(e), w["ticker"], side, bid, ask - bid, eff, int(close // 86400), ser))
print("buy moments", len(rows), "value moments", len(vmoments), flush=True)
days = sorted({m[7] for m in meta})
print("UTC days", days)
by_day = collections.defaultdict(list)
for i, m in enumerate(meta):
    by_day[m[7]].append(i)


def vfit(rs, iters=8, lam=1.0):
    n = len(rs[0][1])
    w = [0.0] * n
    for _ in range(iters):
        grad = [0.0] * n
        hess = [[lam if i == j else 0.0 for j in range(n)] for i in range(n)]
        for _, x, y in rs:
            p = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, sum(a * b for a, b in zip(w, x, strict=False))))))
            d, wt = y - p, p * (1 - p)
            for i in range(n):
                grad[i] += d * x[i]
                for j in range(n):
                    hess[i][j] += wt * x[i] * x[j]
        for i in range(n):
            grad[i] -= lam * w[i]
        a = [[*row, grad[i]] for i, row in enumerate(hess)]
        for c in range(n):
            piv = max(range(c, n), key=lambda r_: abs(a[r_][c]))
            a[c], a[piv] = a[piv], a[c]
            for r_ in range(n):
                if r_ != c:
                    f = a[r_][c] / a[c][c]
                    for cc in range(c, n + 1):
                        a[r_][cc] -= f * a[c][cc]
        w = [w[i] + a[i][n] / a[i][i] for i in range(n)]
    return w


def stake_for(ask):
    return 3.0 * (0.25 + 0.75 * min(1.0, max(0.0, (ask - 0.20) / 0.50)))


def sale_pnl(ask, stake, bid):
    c = stake / ask
    sp = max(bid - 0.01, 0.01)
    return (sp - ask) * c - (fee(ask) + fee(sp)) * c


def hold_pnl(ask, stake, won):
    c = stake / ask
    return ((1 - ask) * c if won else -ask * c) - fee(ask) * c


bets = []  # one per bought market side: dict
for di, d in enumerate(days):
    if di < 2:
        continue
    train = [i for dd in days[:di] for i in by_day[dd]][::3]
    model = curvemodel.fit([(rows[i][0], rows[i][1], rows[i][2]) for i in train])
    vw = vfit([m for m in vmoments if m[0] < d][::2])
    if model is None:
        print("day", d, "not fitted")
        continue
    cut = model["cuts"]["0.2"]
    got = {}
    for i in sorted(by_day[d], key=lambda i: meta[i][1]):
        m = meta[i]
        if (m[2], m[3]) in got:
            continue
        p = curvemodel.predict(model, rows[i][0], rows[i][1])
        if p >= cut and p > 0:
            got[(m[2], m[3])] = i
    for (tk, side), i in got.items():
        m = meta[i]
        close, ask, bid0 = m[0], rows[i][1], m[4]
        ser = m[8]
        g, sq = GR[ser]
        sg = 1 if side == "yes" else -1
        ref_log = REF[tk]
        stake = stake_for(ask)
        result_won = (st.db.execute("SELECT result FROM windows WHERE ticker=?", (tk,)).fetchone()["result"] == side)
        at = close - 900.0 + m[1]
        exits = {"shock": None, "cross": None, "value": None}
        moves = collections.deque()
        for now, yb, ya in quotes_of[tk]:
            if now <= at:
                continue
            left = close - now
            if left <= 3:
                continue
            k = int(now) - G0
            if k - 900 < 0 or g[k] is None or g[k - 60] is None or g[k - 900] is None:
                continue
            bid = yb if side == "yes" else 1.0 - ya
            unit_bp = math.sqrt((sq[k] - sq[k - 900]) / 900.0) * math.sqrt(60.0) * 1e4
            if unit_bp <= 0:
                continue
            move = (g[k] - g[k - 60]) * sg * 1e4
            margin = (g[k] - ref_log) * sg * 1e4
            if left <= 420:
                moves.append((now, move))
            while moves and now - moves[0][0] > 120:
                moves.popleft()
            if left <= 300 and 0.05 <= bid <= 0.89:
                worst = min((mv for _, mv in moves), default=move)
                if exits["shock"] is None and worst <= -1.0 * unit_bp and margin <= 0.6 * unit_bp:
                    exits["shock"] = (now, bid)
                if exits["cross"] is None and margin <= 0.0:
                    exits["cross"] = (now, bid)
            if exits["value"] is None and 0.30 <= bid <= 0.70 and bid - fee(bid) > ask + fee(ask):
                f = vfeat(g, sq, k, ref_log, sg, left, bid)
                if f is not None:
                    pv = 1.0 / (1.0 + math.exp(-sum(a * b for a, b in zip(vw, f, strict=False))))
                    if bid - fee(bid) - pv >= 0.02:
                        exits["value"] = (now, bid)
        bets.append({"ts": at, "day": d, "tk": tk, "stake": stake, "ask": ask, "won": result_won, "elapsed": m[1], "eff": m[6], "exits": exits})
print("bets", len(bets), "staked $%.2f" % sum(b["stake"] for b in bets))
effs = sorted(b["eff"] for b in bets if b["eff"] is not None)
lo, hi = effs[len(effs) // 3], effs[2 * len(effs) // 3]
mid = sorted(b["ts"] for b in bets)[len(bets) // 2]
shape = lambda e: "unknown" if e is None else "choppy" if e <= lo else "straight" if e >= hi else "middle"


def outcome(b, use):
    cands = [b["exits"][k] for k in use if b["exits"][k]]
    if cands:
        t, bid = min(cands)
        return sale_pnl(b["ask"], b["stake"], bid), True
    return hold_pnl(b["ask"], b["stake"], b["won"]), False


def report(label, use, subset=None, indent=""):
    sel = [b for b in bets if subset is None or subset(b)]
    if not sel:
        print(f"{indent}{label:44s} n=0")
        return
    res = [outcome(b, use) for b in sel]
    net = sum(r[0] for r in res)
    stk = sum(b["stake"] for b in sel)
    a = sum(r[0] for r, b in zip(res, sel, strict=True) if b["ts"] <= mid)
    c = sum(r[0] for r, b in zip(res, sel, strict=True) if b["ts"] > mid)
    wins_ = sum(1 for r in res if r[0] > 0)
    print(f"{indent}{label:44s} n={len(sel):4d} staked ${stk:7.2f} net ${net:+8.2f} ({100 * net / stk:+5.1f}% of stake) won {100 * wins_ / len(sel):4.1f}% sold {sum(r[1] for r in res):3d} | halves {a:+.2f} / {c:+.2f}")


VARIANTS = (("held to the result", ()), ("+ shock exit", ("shock",)), ("+ shock + cross", ("shock", "cross")), ("+ shock + cross + value (the lane)", ("shock", "cross", "value")))
print(f"\nall {len(bets)} bets, test days {days[2:]}; path-efficiency cutoffs {lo:.3f} {hi:.3f}, halves split at {mid:.0f}")
for name, use in VARIANTS:
    report(name, use)
print("\nby minutes in (the lane, all three exits):")
for lo_, hi_ in ((120, 420), (420, 660), (660, 900)):
    report(f"{lo_ // 60}-{hi_ // 60} min", VARIANTS[3][1], lambda b, lo_=lo_, hi_=hi_: lo_ <= b["elapsed"] < hi_)
print("\nby curve shape (held / the lane):")
for sh in ("choppy", "middle", "straight"):
    report(f"{sh}, held", (), lambda b, sh=sh: shape(b["eff"]) == sh)
    report(f"{sh}, the lane", VARIANTS[3][1], lambda b, sh=sh: shape(b["eff"]) == sh)
print("\nper test day (UTC day 20763 = Friday 11-06; held / the lane):")
for d in days[2:]:
    report(f"day {d}, held", (), lambda b, d=d: b["day"] == d)
    report(f"day {d}, the lane", VARIANTS[3][1], lambda b, d=d: b["day"] == d)
print("DONE")
