"""Do the bot's bets pay when the slope is rising on the side bought, and lose when it is falling? (2026-11-12.)
Seen, not predicted: buy_timing.py's control row showed the bets bought on a rising slope at +$164.58 as traded and those on a falling slope at -$56.64 (result 2026-11-12-buy-timing.txt). The split was noticed after the result, so this run is a check of that one number and not evidence for a rule: is it real money or paper, does it hold on each day, how large is its error, and does it grow with the slope.
Same bets and same reading as buy_timing.py: the bot's settled bets since Monday 2026-11-09 00:00 ET, every lane and mode; S is the weighted slope of Kalshi's yes mid over the 36 five-minute segments behind the buy, in the window's own typical 5-minute move, signed for the side bought. Rising = S >= 0, falling = S < 0. Per group: n, dollars as traded (the real exit, fees in), per bet and its standard error, per $1 staked, how many bets made and lost money, the mean ask, and the same bets held to settlement. Split by mode, by day, and by thirds of S. The same bets sit out as in buy_timing.py except the ones that needed a tape ask or 90 seconds left, which this question does not need.
    scripts/cloud_job.sh scripts/slope_sign_split.py
"""

import bisect
import collections
import datetime as dt
import math
import statistics
from zoneinfo import ZoneInfo

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
ET = ZoneInfo("America/New_York")
MON = dt.datetime(2026, 11, 9, tzinfo=ET).timestamp()
NSEG, STEP, STALE, MIN_HIST = 36, 30.0, 20.0, 30
hm = lambda t: dt.datetime.fromtimestamp(t, ET).strftime("%m-%d %H:%M")  # noqa: E731

store = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
bets = store.db.execute("SELECT id,ts,lane,mode,ticker,side,ask,stake_usd,outcome,exit_price,exit_ts FROM decisions WHERE action='bet' AND ts>? AND outcome IS NOT NULL AND stake_usd>0 AND ask>0 ORDER BY ts", (MON,)).fetchall()
allw = store.db.execute("SELECT ticker,series,close_ts FROM windows WHERE close_ts>?", (MON - 86400 - 4 * 3600,)).fetchall()
tape_end = float(tape.execute("SELECT max(ts) FROM quotes").fetchone()[0])
by_ticker = {w["ticker"]: w for w in allw}
skipped = collections.Counter()
bets_of = collections.defaultdict(list)
for b in bets:
    w = by_ticker.get(b["ticker"])
    if w is None:
        skipped["not a quarter-hour market on the tape"] += 1
    elif float(w["close_ts"]) > tape_end:
        skipped["after the tape's end"] += 1
    else:
        bets_of[b["ticker"]].append(b)
series = {by_ticker[t]["series"] for t in bets_of}
wins = sorted((w for w in allw if w["series"] in series and float(w["close_ts"]) <= tape_end), key=lambda w: (w["series"], float(w["close_ts"])))


def thirds(qt, mids, opened):
    """The market's three 5-minute segments, oldest first: (slope, curvature) of the yes mid, or None where a point has no tick before it."""

    def at(o):
        i = bisect.bisect_right(qt, opened + o) - 1
        if i >= 0:
            return mids[i]
        return mids[0] if o == 0 and qt and qt[0] < opened + 150 else None

    p = [at(o) for o in range(0, 901, 150)]
    return [None if None in (p[2 * j], p[2 * j + 1], p[2 * j + 2]) else (p[2 * j + 2] - p[2 * j], p[2 * j + 2] - 2 * p[2 * j + 1] + p[2 * j]) for j in range(3)]


def reading(segs):
    have = [(j, x) for j, x in enumerate(segs) if x is not None]
    if not have:
        return None
    u = math.sqrt(sum(x[0] ** 2 for _, x in have) / len(have))
    if u <= 0:
        return None
    wsum = sum(NSEG - j for j, _ in have)
    return u, sum((NSEG - j) * x[0] for j, x in have) / wsum / u, sum((NSEG - j) * x[1] for j, x in have) / wsum / u


def q(xs, f):
    xs = sorted(xs)
    return xs[int(f * (len(xs) - 1))]


def segments(th, done, hist, key):
    segs = th[:done][::-1]
    for back in range(1, 13):
        segs += (hist.get(key - 900 * back) or [None] * 3)[::-1]
    return segs[:NSEG]


OUT = []  # (S for the side, mode, day, stake, as traded $, held to settlement $, recorded ask)
cur, hist, past = None, {}, []
for w in wins:
    if w["series"] != cur:
        cur, hist, past = w["series"], {}, []
    close = float(w["close_ts"])
    opened = close - 900.0
    rows = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], opened - 20, close)).fetchall()
    book = [(float(x[0]), float(x[1] or 0.0), float(x[2] or 0.0)) for x in rows]
    book = [x for x in book if x[2] > 0 and not (x[1] <= 0 and x[2] >= 1)]  # an empty book (0 / 1) is not a price
    th = thirds([x[0] for x in book], [(x[1] + x[2]) / 2 for x in book], opened)
    key = round(close)
    rd0 = reading(segments(th, 0, hist, key))
    recent = [x for x in past if x[0] > close - 86400]
    for b in bets_of.get(w["ticker"], ()):
        ts = float(b["ts"])
        side = b["side"] or "yes"
        sign = 1 if side == "yes" else -1
        if not opened <= ts <= close:
            skipped["bought outside the market"] += 1
            continue
        rd = reading(segments(th, int((ts - opened) // 300), hist, key))
        if rd is None:
            skipped["no reading"] += 1
            continue
        u, S, C = rd
        if len(recent) >= MIN_HIST and (u > 3 * statistics.median(x[1] for x in recent) or not q([x[2] for x in recent], 0.01) <= S <= q([x[2] for x in recent], 0.99) or not q([x[3] for x in recent], 0.01) <= C <= q([x[3] for x in recent], 0.99)):
            skipped["anomaly (rule 3)"] += 1
            continue
        rec, stake = float(b["ask"]), float(b["stake_usd"])
        n = stake / (rec + fee(rec))
        won = 1.0 if b["outcome"] == side else 0.0
        xp = b["exit_price"]
        traded = n * ((float(xp) - fee(float(xp))) if xp is not None else won) - stake
        OUT.append((sign * S, b["mode"], dt.datetime.fromtimestamp(ts, ET).strftime("%m-%d"), stake, traded, n * won - stake, rec))
    if rd0:
        past.append((close, *rd0))
    hist[key] = th

print(f"{len(OUT)} of the bot's {len(bets)} settled bets since {hm(MON)} ET; the tape ends {hm(tape_end)} ET")
print("sat out: " + (", ".join(f"{k} {v}" for k, v in skipped.most_common()) or "none"))


def line(label, xs):
    if len(xs) < 2:
        return f"  {label:26s} n={len(xs)}"
    v = [x[4] for x in xs]
    return (
        f"  {label:26s} n={len(xs):4d} | as traded {sum(v):+9.2f} | per bet {statistics.fmean(v):+.3f} (se {statistics.stdev(v) / math.sqrt(len(v)):.3f}) | per $1 staked {sum(v) / sum(x[3] for x in xs):+.3f}"
        f" | made money {sum(1 for a in v if a > 0)}, lost {sum(1 for a in v if a < 0)} | mean ask {statistics.fmean(x[6] for x in xs):.2f} | held to settlement {sum(x[5] for x in xs):+9.2f}"
    )


def both(label, xs):
    print(line(f"{label}: rising", [x for x in xs if x[0] >= 0]))
    print(line(f"{label}: falling", [x for x in xs if x[0] < 0]))


print("\nslope rising (S >= 0) against falling (S < 0) on the side bought, dollars as traded")
both("all bets", OUT)
print("\nby mode (label only)")
for md in sorted({x[1] for x in OUT}):
    both(str(md), [x for x in OUT if x[1] == md])
print("\nby day (ET)")
for d in sorted({x[2] for x in OUT}):
    both(d, [x for x in OUT if x[2] == d])
print("\nby thirds of the slope, all bets and then by mode")
c1, c2 = q([x[0] for x in OUT], 1 / 3), q([x[0] for x in OUT], 2 / 3)
for md in (None, *sorted({x[1] for x in OUT})):
    xs = [x for x in OUT if md is None or x[1] == md]
    for lab, f_ in (("lowest third", lambda x: x[0] < c1), ("middle third", lambda x: c1 <= x[0] < c2), ("highest third", lambda x: x[0] >= c2)):
        print(line(f"{md or 'all'}: {lab}", [x for x in xs if f_(x)]))
print("DONE")
