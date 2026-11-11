"""An exit record: the measured chance a position wins from its curve state, and one exit rule on it (2026-11-10, user: "if nothing is helping save from a 0.00 result, we need to figure out a new exit for the curve that applies to everything using it (needs to be tweakable)").
Question: on the same live bets, does selling when the bid net of the fee is above the RECORDED chance of winning for the position's curve state (by a margin, 5 s, 8 s guard) beat holding, the curve value sale alone, and the live stacked exits, walk-forward?
The record: every settled 15-minute window with a spot index in the tape (a 1-in-STRIDE sample of them), YES side, one reading each 10 s of its last 900 s. State = (time-left bucket, the curve's chance P in tenths (900 s horizon rate), the bid in tenths, the cushion's change over the last 60 s in typical minutes: fell 1+ / flat / rose 1+). Curve readings only, never a market name (rule 1). Outcome = the side settled at $1. A NO position reads its own side's P and bid, the mirror of a YES state.
Walk-forward by UTC day: a bet on day D reads cells built from windows that closed on earlier days only. Recorded chance = (wins + N0 * bid) / (n + N0): a thin cell falls back to the market's own price. Variants (grid fixed before results; the choice is made after seeing them and is said to be so): margin 0.02 / 0.05 / 0.08; N0 20 / 60. Sales fill at the next second's bid less the fee.
    scripts/cloud_job.sh scripts/exit_record_study.py STRIDE=3
"""

import bisect
import calendar
import collections
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
    "crypto": (("crypto_multiscale", "crypto_multiscale_underdog"), 300.0, int(os.environ.get("SINCE_CRYPTO", "1794000000"))),
    "commodity_multiscale": (("commodity_multiscale",), 300.0, calendar.timegm((2026, 11, 9, 14, 0, 0))),
    "favorites": (("commodity_energy", "commodity_wti", "commodity_precious", "commodity_industrial"), 120.0, calendar.timegm((2026, 11, 3, 0, 0, 0))),
}
MEM, SEC = 120, 60
BAND = (0.05, 0.89)


def load(lanes, since):
    marks = ",".join("?" * len(lanes))
    rows = st.db.execute(
        "SELECT d.ts,d.ticker,d.lane,d.side,d.contracts,d.stake_usd,d.exit_price,w.close_ts,w.strike,w.result FROM decisions d JOIN windows w ON w.ticker=d.ticker "
        f"WHERE d.action='bet' AND d.mode='trade' AND d.contracts>0 AND d.stake_usd>0 AND d.ts>? AND d.lane IN ({marks}) AND w.result IN ('yes','no') ORDER BY d.ts",
        (since, *lanes),
    ).fetchall()
    seen = {}
    for r in rows:
        seen[(r["ticker"], r["side"])] = r
    return list(seen.values())


def prepare(r):
    """The per-second readings the exit sees from the buy to the close, plus the curve shape at the buy."""
    ser = r["ticker"].split("-")[0]
    crypto = variants.crypto_index(ser)
    ix = crypto or variants.biquote_index(ser)
    if not ix:
        return None
    close, t0 = float(r["close_ts"]), float(r["ts"])
    tk = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts", (ix, t0 - 1500, close)).fetchall()
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (r["ticker"], t0 - 5, close)).fetchall()
    if len(tk) < 1000 or len(q) < 5:
        return None
    T = [float(x[0]) for x in tk]
    V = [float(x[1]) for x in tk]
    if crypto:
        if not r["strike"] or float(r["strike"]) <= 0:
            return None
        ref = math.log(float(r["strike"]))
    else:
        opened = close - 900.0
        a = bisect.bisect_left(T, opened - 5.0)
        if a >= len(T) or T[a] > opened + 20.0:
            return None
        ref = math.log(V[a])
    base = int(T[0])
    g, j, last = [], 0, None
    for t in range(base, int(close) + 1):
        while j < len(T) and T[j] <= t:
            last = V[j]
            j += 1
        g.append(math.log(last) if last else None)
    c60 = [0.0] * len(g)
    for j in range(60, len(g)):
        c60[j] = c60[j - 1] + (((g[j] - g[j - 60]) ** 2) / 60.0 if g[j] is not None and g[j - 60] is not None else 0.0)
    sq = [0.0]
    for x, y in itertools.pairwise(g):
        sq.append(sq[-1] + ((y - x) ** 2 if x is not None and y is not None else 0.0))
    kb = int(t0) - base
    seg = [x for x in g[max(kb - 300, 0) : kb + 1] if x is not None]
    path = sum(abs(y - x) for x, y in itertools.pairwise(seg))
    eff = abs(seg[-1] - seg[0]) / path if len(seg) > 30 and path > 0 else None
    sign = 1.0 if r["side"] == "yes" else -1.0
    qt = [float(x[0]) for x in q]
    moves = collections.deque()
    reads = []
    for t in range(int(t0), int(close) - 2):
        k = t - base
        if k < 960 or k >= len(g) or g[k] is None or g[k - SEC] is None:
            continue
        moves.append((t, (g[k] - g[k - SEC]) * sign))
        while moves and t - moves[0][0] > MEM:
            moves.popleft()
        left = close - t
        if left > 900:
            continue
        rates = (math.sqrt((sq[k] - sq[k - 900]) / 900), math.sqrt((c60[k] - c60[k - 900]) / 900), math.sqrt((c60[k] - c60[k - 180]) / 180))
        if rates[0] <= 0 or rates[1] <= 0 or rates[2] < 0:
            continue
        i = bisect.bisect_right(qt, t) - 1
        if i < 0 or t - qt[i] > 20:
            continue
        yb, ya = float(q[i][1]), float(q[i][2])
        bid = yb if r["side"] == "yes" else 1.0 - ya
        reads.append((t, left, (g[k] - ref) * sign, rates, bid))
    return eff, reads


def phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


import collections
import time as _time

STRIDE = int(os.environ.get("STRIDE", "3"))
SINCE_REC = int(os.environ.get("SINCE_REC", str(calendar.timegm((2026, 10, 28, 0, 0, 0)))))
LEFTS = (30, 60, 120, 240, 480, 900)


def states(reads):
    """(index, state) for each reading: state = (left bucket, P tenth, bid tenth, trajectory)."""
    tms = {}
    out = []
    for idx, (t, left, c, rates, bid) in enumerate(reads):
        tm = c / (rates[0] * math.sqrt(60.0))
        tms[t] = tm
        if left > 900 or left < 8:
            continue
        p = phi(c / (rates[1] * math.sqrt(left))) if rates[1] > 0 else (1.0 if c > 0 else 0.0)
        past = tms.get(t - 60)
        tr = 0 if past is None else (-1 if tm - past <= -1.0 else 1 if tm - past >= 1.0 else 0)
        lb = next(i for i, x in enumerate(LEFTS) if left <= x)
        out.append((idx, (lb, min(int(p * 10), 9), min(int(bid * 10), 9), tr), p))
    return out


# 1. the record, by UTC day
wins = st.db.execute("SELECT ticker, close_ts, strike, result FROM windows WHERE result IN ('yes','no') AND close_ts > ? ORDER BY close_ts", (SINCE_REC,)).fetchall()
cells = collections.defaultdict(lambda: [0, 0])  # (day, state) -> [n, wins]
used = 0
t_start = _time.time()
for k, w in enumerate(wins):
    ser = w["ticker"].split("-")[0]
    if not ser.endswith("15M") or not (variants.crypto_index(ser) or variants.biquote_index(ser)) or k % STRIDE:
        continue
    r = {"ticker": w["ticker"], "close_ts": w["close_ts"], "ts": float(w["close_ts"]) - 880.0, "side": "yes", "strike": w["strike"], "contracts": 1.0, "stake_usd": 1.0, "result": w["result"]}
    got = prepare(r)
    if not got:
        continue
    used += 1
    day = int(float(w["close_ts"]) // 86400)
    won = 1 if w["result"] == "yes" else 0
    for idx, state, _ in states(got[1]):
        if got[1][idx][0] % 10 == 0:
            c = cells[(day, state)]
            c[0] += 1
            c[1] += won
    if used % 500 == 0:
        print(f"  record: {used} windows read, {_time.time() - t_start:.0f}s", flush=True)
print(f"record: {used} windows (1 in {STRIDE}), {len(cells)} day-cells, {sum(c[0] for c in cells.values())} readings")
days = sorted({d for d, _ in cells})
cum = {}  # day -> {state: [n, wins]} from all earlier days
run = collections.defaultdict(lambda: [0, 0])
for d in days:
    cum[d] = {s: list(v) for s, v in run.items()}
    for (dd, s), v in cells.items():
        if dd == d:
            run[s][0] += v[0]
            run[s][1] += v[1]
last_day = days[-1] if days else 0
cum[last_day + 1] = {s: list(v) for s, v in run.items()}

# calibration of the curve's chance and of the record's own cells (all days pooled), to read alongside
cal = collections.defaultdict(lambda: [0, 0])
for s, v in run.items():
    cal[s[1]][0] += v[0]
    cal[s[1]][1] += v[1]
print("curve chance P (tenths) -> share that won:", " ".join(f"{k/10:.1f}:{v[1]/v[0]:.2f}(n{v[0]})" for k, v in sorted(cal.items()) if v[0]))
big = collections.defaultdict(lambda: [0, 0])
for s, v in run.items():
    if s[1] - s[2] >= 3:
        big[s[2]][0] += v[0]
        big[s[2]][1] += v[1]
print("where P is 0.3+ above the bid, by bid tenth -> share that won:", " ".join(f"{k/10:.1f}:{v[1]/v[0]:.2f}(n{v[0]})" for k, v in sorted(big.items()) if v[0]))


def table_for(ts):
    d = int(ts // 86400)
    while d not in cum and d > (days[0] if days else 0):
        d -= 1
    return cum.get(d, {})


def decide(r, reads, v, margin_value=None):
    """(pnl, sold). v = ("record", margin, n0) or ("value", margin)."""
    n, cost = float(r["contracts"]), float(r["stake_usd"])
    hold = (n if r["result"] == r["side"] else 0.0) - cost
    tab = table_for(float(r["ts"]))
    since = None
    for idx, state, p in states(reads):
        t, left, c, rates, bid = reads[idx]
        if bid < 0.05:
            since = None
            continue
        net = bid - fee(bid)
        if v[0] == "value":
            ref = p
            m = v[1]
        else:
            cell = tab.get(state, (0, 0))
            ref = (cell[1] + v[2] * bid) / (cell[0] + v[2])
            m = v[1]
        if net - ref >= m:
            since = since if since is not None else t
            if t - since >= 5 and idx + 1 < len(reads):
                sell = reads[idx + 1][4]
                if sell > 0.0:
                    return n * (sell - fee(sell)) - cost, True
        else:
            since = None
    return hold, False


VARIANTS = [("value", 0.15), ("value", 0.06)] + [("record", m, n0) for m in (0.02, 0.05, 0.08) for n0 in (20, 60)]
for gname, (lanes, own_L, since) in GROUPS.items():
    bets = [b for b in load(lanes, since) if int(float(b["ts"]) // 86400) > (days[0] if days else 0)]
    data = []
    for r in bets:
        got = prepare(r)
        if got:
            data.append((r, got[0], got[1]))
    print(f"\n=== {gname}: lanes {lanes} | settled live bets {len(bets)} | replayed {len(data)}")
    if len(data) < 20:
        print("  too few bets")
        continue
    effs = sorted(e for _, e, _ in data if e is not None)
    lo, hi = effs[len(effs) // 3], effs[2 * len(effs) // 3]
    mid = sorted(float(r["ts"]) for r, _, _ in data)[len(data) // 2]

    def shape(e):
        return "unknown" if e is None else "choppy" if e <= lo else "straight" if e >= hi else "middle"

    hold = [((float(r["contracts"]) if r["result"] == r["side"] else 0.0) - float(r["stake_usd"])) for r, _, _ in data]
    live = [((float(r["contracts"]) * float(r["exit_price"]) - float(r["stake_usd"])) if r["exit_price"] else h) for (r, _, _), h in zip(data, hold, strict=True)]
    zeros = [i for i, (r, _, _) in enumerate(data) if hold[i] <= -float(r["stake_usd"]) + 1e-9]
    print(f"  held to the result {sum(hold):+.2f} | live (own sales, stacked exits) {sum(live):+.2f} | whole-stake losses held {len(zeros)}")
    for v in VARIANTS:
        res = [decide(r, reads, v) for r, e, reads in data]
        d = [p - h for (p, _), h in zip(res, hold, strict=True)]
        ch = [(x, i) for i, x in enumerate(d) if abs(x) > 1e-9]
        h1 = sum(x for x, i in ch if float(data[i][0]["ts"]) <= mid)
        h2 = sum(x for x, i in ch if float(data[i][0]["ts"]) > mid)
        by = {}
        for x, i in ch:
            by[shape(data[i][1])] = by.get(shape(data[i][1]), 0.0) + x
        won = sum(1 for (p, f), h in zip(res, hold, strict=True) if f and h > 0)
        saved = sum(1 for (p, f), h in zip(res, hold, strict=True) if f and h < 0)
        fixed = sum(1 for i in zeros if res[i][1])
        net = sum(p for p, _ in res)
        sh = " ".join(f"{k} {x:+.1f}" for k, x in sorted(by.items()))
        both = "BOTH HALVES +" if h1 > 0 and h2 > 0 else ""
        print(f"    {v}: net {net:+.2f} (vs holding {sum(d):+.2f}, vs live {net - sum(live):+.2f}) on {len(ch)} sold | losers saved {saved}, winners sold {won}, zeros partial {fixed} of {len(zeros)} | halves {h1:+.2f} / {h2:+.2f} | {sh} {both}")
print("DONE")
