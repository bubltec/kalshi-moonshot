"""Does the slope reading tell the bot when to buy, on the bets it made? (2026-11-12, user: "our goal isn't to replace kalshi's estimate, its to give us advice on the time and price to buy at".)
slope_factor.py and tick_lookalike.py asked whether a curve reading beats Kalshi's chance of the result; it does not, and that was the wrong question. This one takes the side as given and asks only about the price paid. Stated before any result; nothing is fitted and nothing is swept.
Bets: the bot's own settled bets since Monday 2026-11-09 00:00 ET (the logic rewrite), every lane and mode, on the side it bought. The reading is slope_factor.py's, as last run (no gates): the 36 five-minute segments of Kalshi's yes mid behind the buy (this market's finished thirds, then the series' earlier markets), slope and curvature per segment in the window's own typical 5-minute move, weighted in a straight line toward the newest, signed for the side bought: S (slope) and C (curvature).
The rule: if S < 0 at the bot's buy (the curve has been falling on that side), do not buy yet. Look again every 30 seconds and buy at the first look where the side's mid is above the look before (it has turned up). With a minute left and no turn, buy then. If S >= 0, buy as the bot did.
The score, on the same bets: the same number of contracts and the same exit the bet really had, so only the price paid changes (the tape's ask at the rule's moment against the tape's ask at the bot's moment, the fee on each). If the bet's real sale came before the rule's buy, the rule's bet is held to settlement. If there was never an ask to buy at, the rule has no bet (zero) and that is counted. Reported: n, dollars as traded and under the rule, how many bets the rule beat and lost to what happened, and by curve shape (terciles of S and of C).
The control: the same wait applied to the bets where the reading said buy now. If waiting does as well there, the reading is not what helps.
Sat out and counted: bets not on a quarter-hour market on the tape, bets after the tape's end (a cloud job restores last night's backup), no reading, no ask on the tape within 20 seconds of the buy, under 90 seconds left, and anomalies (rule 3: the window's typical move over three times the series' 24-hour median, or S or C outside that history's 1st/99th percentile).
    scripts/cloud_job.sh scripts/buy_timing.py
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


OUT = []  # (S, C, mode, as traded $, rule $, how the rule bought, seconds waited, ask change, exit passed)
gap = []  # recorded ask minus the tape's ask at the buy
cur, hist, past = None, {}, []
for w in wins:
    if w["series"] != cur:
        cur, hist, past = w["series"], {}, []
    close = float(w["close_ts"])
    opened = close - 900.0
    rows = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], opened - 20, close)).fetchall()
    rows = [(float(x[0]), float(x[1] or 0.0), float(x[2] or 0.0)) for x in rows]
    book = [x for x in rows if x[2] > 0 and not (x[1] <= 0 and x[2] >= 1)]  # an empty book (0 / 1) is not a price
    bt, bm = [x[0] for x in book], [(x[1] + x[2]) / 2 for x in book]
    th = thirds(bt, bm, opened)
    key = round(close)
    rd0 = reading(segments(th, 0, hist, key))
    recent = [x for x in past if x[0] > close - 86400]
    trade = [x for x in rows if x[1] > 0 and 0 < x[2] < 1]
    tt = [x[0] for x in trade]
    for b in bets_of.get(w["ticker"], ()):
        ts = float(b["ts"])
        yes = (b["side"] or "yes") == "yes"
        sign = 1 if yes else -1
        if not opened <= ts <= close - 90:
            skipped["under 90 seconds left (or bought outside the market)"] += 1
            continue
        rd = reading(segments(th, int((ts - opened) // 300), hist, key))
        if rd is None:
            skipped["no reading"] += 1
            continue
        u, S, C = rd
        if len(recent) >= MIN_HIST and (u > 3 * statistics.median(x[1] for x in recent) or not q([x[2] for x in recent], 0.01) <= S <= q([x[2] for x in recent], 0.99) or not q([x[3] for x in recent], 0.01) <= C <= q([x[3] for x in recent], 0.99)):
            skipped["anomaly (rule 3)"] += 1
            continue

        def ask_at(t, yes=yes, tt=tt, trade=trade):  # the side's ask from a two-sided quote no older than 20 seconds
            i = bisect.bisect_right(tt, t) - 1
            if i < 0 or t - tt[i] > STALE:
                return None
            return trade[i][2] if yes else 1 - trade[i][1]

        def mid_at(t, sign=sign, bt=bt, bm=bm):
            i = bisect.bisect_right(bt, t) - 1
            return None if i < 0 else (bm[i] if sign > 0 else 1 - bm[i])

        a0 = ask_at(ts)
        if a0 is None:
            skipped["no ask on the tape at the buy"] += 1
            continue
        gap.append(float(b["ask"]) - a0)
        how, t_rule, a1 = "never", None, None
        prev, t = mid_at(ts), ts + STEP
        while t <= close - 60:
            m = mid_at(t)
            if prev is not None and m is not None and m > prev + 1e-9 and ask_at(t) is not None:
                how, t_rule, a1 = "turned up", t, ask_at(t)
                break
            prev = m if m is not None else prev
            t += STEP
        if how == "never" and ask_at(close - 60) is not None:
            how, t_rule, a1 = "last minute", close - 60, ask_at(close - 60)
        rec = float(b["ask"])
        n = float(b["stake_usd"]) / (rec + fee(rec))
        won = 1.0 if b["outcome"] == (b["side"] or "yes") else 0.0
        xp = b["exit_price"]
        traded = n * ((float(xp) - fee(float(xp))) if xp is not None else won) - float(b["stake_usd"])
        passed = False
        if how == "never":
            rule = 0.0
        elif xp is not None and b["exit_ts"] is not None and float(b["exit_ts"]) <= t_rule:
            passed = True
            rule = n * (won - a1 - fee(a1))
        else:
            rule = traded + n * ((a0 + fee(a0)) - (a1 + fee(a1)))
        OUT.append((sign * S, sign * C, b["mode"], traded, rule, how, 0.0 if t_rule is None else t_rule - ts, None if a1 is None else a1 - a0, passed))
    if rd0:
        past.append((close, *rd0))
    hist[key] = th

print(f"{len(OUT)} of the bot's {len(bets)} settled bets since {hm(MON)} ET are scored; the tape ends {hm(tape_end)} ET")
print("sat out: " + (", ".join(f"{k} {v}" for k, v in skipped.most_common()) or "none"))
print("by mode (label only): " + ", ".join(f"{k} {v}" for k, v in collections.Counter(x[2] for x in OUT).most_common()))
if gap:
    print(f"the recorded ask minus the tape's ask at the buy: mean {statistics.fmean(gap):+.4f}, median {statistics.median(gap):+.4f}")


def line(label, xs):
    if not xs:
        return f"  {label:30s} n=0"
    d = [x[4] - x[3] for x in xs]
    hows = collections.Counter(x[5] for x in xs)
    chg = [x[7] for x in xs if x[7] is not None]
    se = statistics.stdev(d) / math.sqrt(len(d)) if len(d) > 1 else float("nan")
    return (
        f"  {label:30s} n={len(xs):4d} | as traded {sum(x[3] for x in xs):+9.2f} | waiting {sum(x[4] for x in xs):+9.2f} | change {sum(d):+8.2f} (per bet {statistics.fmean(d):+.3f}, se {se:.3f})"
        f" | beat {sum(1 for v in d if v > 1e-9)}, lost {sum(1 for v in d if v < -1e-9)}, same {sum(1 for v in d if abs(v) <= 1e-9)}"
        f" | ask change {statistics.fmean(chg) if chg else float('nan'):+.4f} after {statistics.fmean(x[6] for x in xs):.0f}s | turned up {hows['turned up']}, last minute {hows['last minute']}, never {hows['never']}, real sale came first {sum(1 for x in xs if x[8])}"
    )


wait = [x for x in OUT if x[0] < 0]
now = [x for x in OUT if x[0] >= 0]
print("\nthe rule: wait where the slope was falling on the side bought (dollars; the wait is what changes)")
print(line("reading said wait", wait))
print(line("control: reading said buy now", now))
print(f"  the rule as stated (wait only where it said wait) changes the book by {sum(x[4] - x[3] for x in wait):+.2f} on {len(OUT)} bets")
if len(wait) > 30:
    print("\nby curve shape, the bets where the reading said wait")
    for what, ix in (("slope", 0), ("curvature", 1)):
        c1, c2 = q([x[ix] for x in wait], 1 / 3), q([x[ix] for x in wait], 2 / 3)
        print(line(f"{what} low", [x for x in wait if x[ix] < c1]))
        print(line(f"{what} mid", [x for x in wait if c1 <= x[ix] < c2]))
        print(line(f"{what} high", [x for x in wait if x[ix] >= c2]))
    print("\nby mode (label only), the bets where the reading said wait")
    for md in sorted({x[2] for x in wait}):
        print(line(str(md), [x for x in wait if x[2] == md]))
print("DONE")
