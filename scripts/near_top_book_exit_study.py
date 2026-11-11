"""Near the top, sell when the other side of the book builds (2026-11-10, user: "when we are at like 98% and order book on the other side starts filling up we decide to sell").

Question: a position whose bid has reached P (0.95 / 0.97 / 0.98) has 2-5 cents left to gain and nearly a dollar to lose. Does selling it when the other side presses, in units of that market's own recent readings, beat holding?
Other-side pressure, read each second while the bid is at P or over:
  imb   the other side's share of the top of the book: other_size / (my_size + other_size)
  grow  the other side's top size against its own median over the 30 s before
  thin  my side's top size shrinking: its median over the 30 s before / its size now
  flow  contracts traded by takers against the position over the last 10 s
A sale fires when a reading sits between the q-th and the 99th percentile of that series' own near-top readings from the 24 h before the window opened (recomputed hourly), for K seconds in a row. A reading over the 99th percentile is an anomaly (rule 3): the rule sits out, and "anomaly" rows report what selling there would have done.
The sale is at the bid one second after the trigger, less the fee. Grid fixed before any result: P x measure x q (0.80, 0.90, 0.95) x K (1, 3).
Two samples, same pass: (tape) every settled 15-minute window and side, one contract from the first second its bid is at P with 20-600 s left, against settlement; (live) the settled live bets, from the buy, against what happened (the lane's own sale or settlement).
    scripts/cloud_job.sh scripts/near_top_book_exit_study.py
"""

import collections
import itertools
import os

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

os.nice(10)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)

LEVELS = (0.95, 0.97, 0.98)
MEASURES = ("imb", "grow", "thin", "flow")
QS = (0.80, 0.90, 0.95)
KS = (1, 3)
STATE = 0.95  # readings at or over this bid feed the series' own history
LOOK = 24 * 3600.0
MIN_HISTORY = 300
LAST = 8  # no order in the last seconds
EVERY = int(os.environ.get("EVERY", "1"))


def median(xs):
    ys = sorted(xs)
    n = len(ys)
    return ys[n // 2] if n % 2 else 0.5 * (ys[n // 2 - 1] + ys[n // 2])


def series_of(ticker):
    return ticker.split("-")[0]


def window_readings(ticker, close):
    """Per second of the window, per side: bid, and the four readings where the bid is at STATE or over (None otherwise)."""
    opened = int(close) - 900
    q = tape.execute("SELECT ts,bid,ask,bid_size,ask_size FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (ticker, opened - 40, close)).fetchall()
    if len(q) < 300:
        return None
    f = {int(r[0]): (float(r[1] or 0.0), float(r[2] or 0.0)) for r in tape.execute("SELECT ts,yes_vol,no_vol FROM flow_1s WHERE ticker=? AND ts BETWEEN ? AND ?", (ticker, opened - 40, close)).fetchall()}
    n = int(close) - (opened - 40) + 1
    base = opened - 40
    rows = [None] * n
    for r in q:
        i = int(r[0]) - base
        if 0 <= i < n and r[1] is not None and r[2] is not None:
            rows[i] = (float(r[1]), float(r[2]), r[3], r[4], i)
    last = None
    for i in range(n):  # carry a quote forward up to 5 s
        if rows[i] is not None:
            last = rows[i]
        elif last is not None and i - last[4] <= 5:
            rows[i] = last
    out = {}
    for side in ("yes", "no"):
        bids, mine, other, against = [0.0] * n, [None] * n, [None] * n, [0.0] * n
        for i in range(n):
            r = rows[i]
            if r is None:
                continue
            bid, ask, bs, az = r[0], r[1], r[2], r[3]
            if side == "yes":
                bids[i], mine[i], other[i] = bid, bs, az
            else:
                bids[i], mine[i], other[i] = (1.0 - ask if ask > 0 else 0.0), az, bs
            fl = f.get(base + i)
            if fl:
                against[i] = fl[1] if side == "yes" else fl[0]
        read = [None] * n
        for i in range(40, n):
            if bids[i] < STATE or mine[i] is None or other[i] is None:
                continue
            m, o = float(mine[i]), float(other[i])
            prev_o = [float(x) for x in other[i - 30:i] if x is not None]
            prev_m = [float(x) for x in mine[i - 30:i] if x is not None]
            if len(prev_o) < 10 or len(prev_m) < 10 or m + o <= 0:
                continue
            mo, mm = median(prev_o), median(prev_m)
            read[i] = {
                "imb": o / (m + o),
                "grow": (o / mo) if mo > 0 else None,
                "thin": (mm / m) if m > 0 else None,
                "flow": sum(against[i - 9:i + 1]),
            }
        out[side] = (bids, read)
    return base, n, out


def efficiency(bids, i):
    """Path efficiency of the side's bid over the 300 s before second i: net move over the sum of moves (1 straight, 0 choppy)."""
    xs = [b for b in bids[max(0, i - 300):i + 1] if b > 0]
    if len(xs) < 30:
        return None
    total = sum(abs(b - a) for a, b in itertools.pairwise(xs))
    return abs(xs[-1] - xs[0]) / total if total > 0 else 1.0


history = collections.defaultdict(collections.deque)  # series -> (close_ts, {measure: [readings]})
cuts_cache = {}


def cuts(series, opened):
    key = (series, int(opened // 3600))
    if key in cuts_cache:
        return cuts_cache[key]
    h = history[series]
    while h and h[0][0] < opened - LOOK:
        h.popleft()
    out = {}
    for m in MEASURES:
        pool = sorted(x for close_ts, d in h if close_ts <= opened for x in d[m])
        if len(pool) < MIN_HISTORY:
            out[m] = None
            continue
        out[m] = ({qq: pool[min(len(pool) - 1, int(qq * len(pool)))] for qq in QS}, pool[min(len(pool) - 1, int(0.99 * len(pool)))])
    cuts_cache[key] = out
    return out


def triggers(read, start, end, cut):
    """First second each (measure, q, K) fires from `start`, and each ("anomaly", measure, K)."""
    fired = {}
    for m in MEASURES:
        if cut.get(m) is None:
            continue
        qcut, top = cut[m]
        runs = dict.fromkeys(QS, 0)
        arun = 0
        for i in range(start, end):
            r = read[i]
            v = r.get(m) if r else None
            if v is None:
                runs = dict.fromkeys(QS, 0)
                arun = 0
                continue
            arun = arun + 1 if v > top else 0
            for k in KS:
                if arun >= k:
                    fired.setdefault(("anomaly", m, 0.99, k), i)
            for qq in QS:
                runs[qq] = runs[qq] + 1 if (qcut[qq] <= v <= top and v > 0) else 0
                for k in KS:
                    if runs[qq] >= k:
                        fired.setdefault(("rule", m, qq, k), i)
    return fired


live = collections.defaultdict(list)
for r in st.db.execute(
    "SELECT d.ts,d.ticker,d.lane,d.side,d.contracts,d.stake_usd,d.exit_price,d.exit_ts FROM decisions d JOIN windows w ON w.ticker=d.ticker "
    "WHERE d.action='bet' AND d.mode='trade' AND d.contracts>0 AND d.stake_usd>0 AND w.result IN ('yes','no') AND d.ticker LIKE '%15M%' ORDER BY d.ts"
).fetchall():
    live[r["ticker"]].append(r)

wins = st.db.execute("SELECT ticker, close_ts, result FROM windows WHERE result IN ('yes','no') AND ticker LIKE '%15M%' AND close_ts>0 ORDER BY close_ts").fetchall()
print(f"windows {len(wins)} (every {EVERY}), live bets {sum(len(v) for v in live.values())}")

# results[(sample, P)] -> list of positions: dict(ts, won, shape_eff, base value, fired {variant: sale value})
results = collections.defaultdict(list)
no_history = 0
for wi, w in enumerate(wins):
    ticker, close, result = w["ticker"], float(w["close_ts"]), w["result"]
    use = wi % EVERY == 0 or ticker in live
    got = window_readings(ticker, close)
    if got is None:
        continue
    base, n, sides = got
    series = series_of(ticker)
    cut = cuts(series, close - 900)
    end = n - LAST
    if use:
        for side in ("yes", "no"):
            bids, read = sides[side]
            won = 1.0 if result == side else 0.0
            jobs = []
            for P in LEVELS:
                i0 = next((i for i in range(40 + 300, n - 20) if bids[i] >= P), None)  # 20-600 s left
                if i0 is not None and wi % EVERY == 0:
                    jobs.append(("tape", P, i0, end, won))
            for b in live.get(ticker, ()):
                if (b["side"] or "yes") != side:
                    continue
                bi = max(40, int(float(b["ts"])) - base + 1)
                stop = min(end, int(float(b["exit_ts"])) - base) if b["exit_ts"] else end
                actual = (float(b["exit_price"]) - fee(float(b["exit_price"]))) if b["exit_price"] is not None and b["exit_ts"] else won
                for P in LEVELS:
                    i0 = next((i for i in range(bi, stop) if bids[i] >= P), None)
                    if i0 is not None:
                        jobs.append(("live", P, i0, stop, actual))
            for sample, P, i0, stop, value in jobs:
                if all(cut.get(m) is None for m in MEASURES):
                    no_history += 1
                    continue
                fired = {}
                for var, i in triggers(read, i0, stop, cut).items():
                    j = min(i + 1, n - 1)
                    if bids[j] > 0:
                        fired[var] = bids[j] - fee(bids[j])
                results[(sample, P)].append({"ts": close, "won": won, "eff": efficiency(bids, i0), "value": value, "fired": fired})
    pooled = {m: [] for m in MEASURES}
    for side in ("yes", "no"):
        read = sides[side][1]
        for i in range(40, end, 3):
            r = read[i]
            if r:
                for m in MEASURES:
                    if r.get(m) is not None:
                        pooled[m].append(r[m])
    history[series].append((close, pooled))

print(f"positions with no 24 h history for their series (left out): {no_history}")
for (sample, P), pos in sorted(results.items()):
    if not pos:
        continue
    mid = sorted(p["ts"] for p in pos)[len(pos) // 2]
    effs = sorted(p["eff"] for p in pos if p["eff"] is not None)
    c1, c2 = (effs[len(effs) // 3], effs[2 * len(effs) // 3]) if len(effs) >= 3 else (0.0, 0.0)

    def shape(p, c1=c1, c2=c2):
        return "unknown" if p["eff"] is None else "choppy" if p["eff"] < c1 else "straight" if p["eff"] >= c2 else "middle"

    lost = sum(1 for p in pos if p["won"] == 0.0)
    print(f"\n=== {sample}, bid reached {P:.2f}: {len(pos)} positions, {lost} settled against ({100.0 * lost / len(pos):.2f}%), what happened {sum(p['value'] for p in pos):.2f} per contract-dollar; shape cutoffs {c1:.3f} {c2:.3f} (sample terciles)")
    rows = []
    variants_seen = sorted({v for p in pos for v in p["fired"]})
    for var in variants_seen:
        sold = [(p, p["fired"][var]) for p in pos if var in p["fired"]]
        delta = sum(v - p["value"] for p, v in sold)
        h1 = sum(v - p["value"] for p, v in sold if p["ts"] < mid)
        h2 = delta - h1
        by = collections.defaultdict(float)
        for p, v in sold:
            by[shape(p)] += v - p["value"]
        beat = sum(1 for p, v in sold if v > p["value"] + 1e-9)
        worse = sum(1 for p, v in sold if v < p["value"] - 1e-9)
        caught = sum(1 for p, v in sold if p["won"] == 0.0)
        rows.append((delta, f"  {var[0]:7s} {var[1]:4s} q{var[2]:.2f} K{var[3]}: vs what happened {delta:+7.2f} on {len(sold)} sold | beat {beat}, lost to {worse} | settled-against caught {caught} of {lost}, winners sold {len(sold) - caught} | halves {h1:+.2f} / {h2:+.2f} | "
                     + " ".join(f"{k} {by[k]:+.2f}" for k in ("choppy", "middle", "straight", "unknown") if k in by)))
    for _d, line in sorted(rows, reverse=True):
        print(line)
