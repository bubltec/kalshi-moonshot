"""A trained buy timer: at a moment, will this side's ask be cheaper a minute from now? (2026-11-12, user: "we need an adjustable buy timer, in the 15m window we need to find the optimal time to buy ... if we establish that behavior maybe we can train on it"; yes to building it.)
What is already known (same day): no clock minute pays on its own (2026-11-12-time-side-grid.txt); the best buy in hindsight is the cheap side at its low, around minutes 3 to 8.5 (2026-11-12-best-round-trip.txt); buying after the turn loses (2026-11-12-turn-rule.txt). A hindsight low cannot be seen as it forms, so the model is not asked for the low. It is asked the one thing a timer needs, and it predicts the price path, never the result. Stated before any result; one model, nothing swept.
Target: the side's ask 60 seconds from now minus its ask now, in the market's own typical minute (the root-mean-square one-second move of its mid since the open, times sqrt(60)). Kalshi's quotes only.
Inputs, each read from this market's own quotes since its open and in the same unit: the mid's move over the last 30, 60, 120 and 300 seconds, how far the mid is above its low and below its high of the window, its bend over the last minute (now - 2 x 30 seconds ago + 60 seconds ago), the spread, the share of the window left, and the mid. The no side is the yes side turned over. No asset, no lane, no gate.
Fit: a ridge regression (penalty 50, the curve model's own; the inputs are already in the curve's own units) on every 30-second moment from minute 2 to 13 of every market on all the days before the test day, at least 3; scored on the test day only.
Score 1, the prediction, per test day: its correlation with what the ask did, and what the ask did where the model called for a wait against the rest. A wait is called when the prediction is below zero and below the 25th percentile of the model's own predictions on its training days.
Score 2, the timer, on the bot's own settled bets since Monday 2026-11-09 (every lane and mode, the side it bought): from the bot's buy, look every 30 seconds and buy at the first look where the model does not call for a wait, or with a minute left. Same contracts and the same exit the bet really had, so only the price paid changes; if the real sale came before the timer's buy the bet is held to settlement; if there is never an ask the timer has no bet. Reported: n, dollars as traded and under the timer, how many bets the timer beat and lost to what happened, by curve shape (terciles of the last minute's move and of the height above the window's low). Control: the bets where the model said buy now, bought 60 seconds late anyway; if that does as well, the model is not what helps.
Window: DAYS (default 9) before the tape's end; the days are printed.
    scripts/cloud_job.sh scripts/buy_timer_model.py
"""

import array
import bisect
import collections
import datetime as dt
import itertools
import math
import os
import statistics
from zoneinfo import ZoneInfo

from kalshi_moonshot import config, db
from kalshi_moonshot.kalshiws import RECORD_ONLY
from kalshi_moonshot.lanes import lane_specs
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
ET = ZoneInfo("America/New_York")
MON = dt.datetime(2026, 11, 9, tzinfo=ET).timestamp()
DAYS = float(os.environ.get("DAYS", "9"))
HORIZON, STEP, STALE, LAM, MIN_TRAIN_DAYS, CALL = 60.0, 30.0, 20.0, 50.0, 3, 0.25
NAMES = ["intercept", "move 30s", "move 60s", "move 120s", "move 300s", "above low", "below high", "bend", "spread", "window left", "mid"]
K = len(NAMES)
W = K + 2  # a stored row: the inputs, the target in typical minutes, the target in dollars
hm = lambda t: dt.datetime.fromtimestamp(t, ET).strftime("%m-%d %H:%M")  # noqa: E731
day_of = lambda t: dt.datetime.fromtimestamp(t, ET).strftime("%m-%d")  # noqa: E731

series = {sr for sp in lane_specs(s).values() if sp.enabled and sp.model_buy for sr in sp.series}
store = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
tape_end = float(tape.execute("SELECT max(ts) FROM quotes").fetchone()[0])
wins = store.db.execute("SELECT ticker,series,close_ts FROM windows WHERE close_ts>? AND close_ts<=? ORDER BY close_ts", (tape_end - DAYS * 86400, tape_end)).fetchall()
wins = [w for w in wins if w["series"] in series and w["series"] not in RECORD_ONLY]
on_tape = {w["ticker"] for w in wins}
bets = store.db.execute("SELECT id,ts,mode,lane,ticker,side,ask,stake_usd,outcome,exit_price,exit_ts FROM decisions WHERE action='bet' AND ts>? AND outcome IS NOT NULL AND stake_usd>0 AND ask>0 ORDER BY ts", (MON,)).fetchall()
skipped = collections.Counter()
bets_of = collections.defaultdict(list)
for b in bets:
    if b["ticker"] in on_tape:
        bets_of[b["ticker"]].append(b)
    else:
        skipped["not a market on this tape"] += 1

ROWS = collections.defaultdict(lambda: array.array("d"))  # day -> rows, flat
GRAM = collections.defaultdict(lambda: [[0.0] * K for _ in range(K)])
XTY = collections.defaultdict(lambda: [0.0] * K)
PATHS = []  # (bet, day, [(t, inputs or None, the side's ask or None)], ask 60 s after the buy)
for w in wins:
    close = float(w["close_ts"])
    opened = close - 900.0
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], opened, close)).fetchall()
    q = [(float(x[0]), float(x[1] or 0.0), float(x[2] or 0.0)) for x in q]
    q = [x for x in q if x[2] > 0 and not (x[1] <= 0 and x[2] >= 1)]  # an empty book (0 / 1) is not a price
    if len(q) < 30:
        skipped["markets with under 30 quotes"] += 1
        continue
    bt = [x[0] for x in q]
    mid = [(x[1] + x[2]) / 2 for x in q]
    lo, hi, sq = [mid[0]], [mid[0]], [0.0]
    for a, b_ in itertools.pairwise(mid):
        lo.append(min(lo[-1], b_))
        hi.append(max(hi[-1], b_))
        sq.append(sq[-1] + (b_ - a) ** 2)

    def inputs(t, sign, bt=bt, mid=mid, lo=lo, hi=hi, sq=sq, q=q, opened=opened, close=close):
        """(the K inputs for the side, the typical minute), or None before 2 minutes of quotes or on a flat curve."""
        i = bisect.bisect_right(bt, t) - 1
        if i < 1 or t - opened < 120:
            return None
        u = math.sqrt(sq[i] / (t - opened)) * math.sqrt(60)
        if u <= 0:
            return None
        back = lambda k: mid[max(0, bisect.bisect_right(bt, t - k) - 1)]  # noqa: E731
        m = mid[i]
        mv = [(m - back(k)) / u for k in (30, 60, 120, 300)]
        above, below, bend = (m - lo[i]) / u, (hi[i] - m) / u, (m - 2 * back(30) + back(60)) / u
        sp, left = (q[i][2] - q[i][1]) / u, (close - t) / 900
        if sign > 0:
            return [1.0, *mv, above, below, bend, sp, left, m], u
        return [1.0, *(-x for x in mv), below, above, -bend, sp, left, 1 - m], u

    def ask(t, sign, bt=bt, q=q):
        """The side's ask from a two-sided quote no older than 20 seconds."""
        i = bisect.bisect_right(bt, t) - 1
        if i < 0 or t - bt[i] > STALE or not (q[i][1] > 0 and q[i][2] < 1):
            return None
        return q[i][2] if sign > 0 else 1 - q[i][1]

    d = day_of(close)
    g, xty, rows = GRAM[d], XTY[d], ROWS[d]
    for e in range(120, 781, 30):
        t = opened + e
        for sign in (1, -1):
            f = inputs(t, sign)
            a0, a1 = ask(t, sign), ask(t + HORIZON, sign)
            if f is None or a0 is None or a1 is None:
                continue
            x, u = f
            y = (a1 - a0) / u
            rows.extend(x)
            rows.append(y)
            rows.append(a1 - a0)
            for i in range(K):
                xi = x[i]
                xty[i] += xi * y
                gi = g[i]
                for j in range(i, K):
                    gi[j] += xi * x[j]
    for b in bets_of.get(w["ticker"], ()):
        ts = float(b["ts"])
        sign = 1 if (b["side"] or "yes") == "yes" else -1
        if not opened <= ts <= close - 90:
            skipped["bets with under 90 seconds left"] += 1
            continue
        if ask(ts, sign) is None:
            skipped["bets with no ask on the tape at the buy"] += 1
            continue
        path, t = [], ts
        while t <= close - 60:
            f = inputs(t, sign)
            path.append((t, None if f is None else f[0], ask(t, sign)))
            t += STEP
        PATHS.append((b, d, path, ask(ts + HORIZON, sign)))


def fit(days):
    g = [[0.0] * K for _ in range(K)]
    y = [0.0] * K
    for d in days:
        for i in range(K):
            y[i] += XTY[d][i]
            for j in range(i, K):
                g[i][j] += GRAM[d][i][j]
    for i in range(K):
        for j in range(i):
            g[i][j] = g[j][i]
        if i:
            g[i][i] += LAM  # the intercept is not penalized
    m = [[*g[i], y[i]] for i in range(K)]
    for c in range(K):
        p = max(range(c, K), key=lambda r: abs(m[r][c]))
        m[c], m[p] = m[p], m[c]
        pv = m[c][c] or 1e-12
        m[c] = [v / pv for v in m[c]]
        for r in range(K):
            if r != c and m[r][c]:
                fac = m[r][c]
                m[r] = [v - fac * z for v, z in zip(m[r], m[c], strict=True)]
    return [m[i][K] for i in range(K)]


def preds(d, wts):
    r = ROWS[d]
    return [(math.sumprod(wts, r[k : k + K]), r[k + K], r[k + K + 1]) for k in range(0, len(r), W)]


def corr(a, b):
    ma, mb = statistics.fmean(a), statistics.fmean(b)
    den = math.sqrt(sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b))
    return sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True)) / den if den else float("nan")


all_days = sorted(d for d in ROWS if len(ROWS[d]) >= 500 * W)
print(f"{sum(len(ROWS[d]) for d in all_days) // W} side-moments from {len(wins)} markets on the tape from {hm(tape_end - DAYS * 86400)} to {hm(tape_end)} ET; days {all_days}")
print("sat out: " + (", ".join(f"{k} {v}" for k, v in skipped.most_common()) or "none"))
MODEL = {}
print("\nscore 1, the prediction: per test day, fitted on all the days before it")
for n, td in enumerate(all_days):
    if n < MIN_TRAIN_DAYS:
        continue
    wts = fit(all_days[:n])
    tr = sorted(p for d in all_days[:n] for p, _, _ in preds(d, wts))
    cut = min(0.0, tr[int(CALL * (len(tr) - 1))])
    MODEL[td] = (wts, cut)
    te = preds(td, wts)
    called = [x for x in te if x[0] < cut]
    rest = [x for x in te if x[0] >= cut]
    print(
        f"  {td} n={len(te):6d} trained on {n} days | correlation {corr([x[0] for x in te], [x[1] for x in te]):+.3f} | wait called on {len(called) / len(te):.0%}:"
        f" the ask then moved {statistics.fmean(x[2] for x in called) if called else float('nan'):+.4f} (in typical minutes {statistics.fmean(x[1] for x in called) if called else float('nan'):+.3f}) against {statistics.fmean(x[2] for x in rest) if rest else float('nan'):+.4f} ({statistics.fmean(x[1] for x in rest) if rest else float('nan'):+.3f}) on the rest"
    )
if MODEL:
    last = max(MODEL)
    print(f"  the weights of the last fit ({last}): " + ", ".join(f"{nm} {v:+.3f}" for nm, v in zip(NAMES, MODEL[last][0], strict=True)))

OUT = []  # then the lane and the mode (labels only). (called a wait at the buy, last minute's move, height above the low, as traded $, timer $, how, seconds waited, ask change, real sale came first, 60 s late $)
for b, d, path, a60 in PATHS:
    if d not in MODEL:
        skipped["bets on a day with no model"] += 1
        continue
    wts, cut = MODEL[d]
    wait = lambda f, wts=wts, cut=cut: f is not None and math.sumprod(wts, f) < cut  # noqa: E731
    a0 = path[0][2]
    how, t_buy, a1 = "never", None, None
    for i, (t, f, a) in enumerate(path):
        if a is not None and (i == len(path) - 1 or not wait(f)):
            how, t_buy, a1 = ("at the buy" if i == 0 else "last minute" if i == len(path) - 1 else "model let go"), t, a
            break
    if how == "never":
        for t, _, a in reversed(path):
            if a is not None:
                how, t_buy, a1 = "last ask seen", t, a
                break
    side = b["side"] or "yes"
    rec, stake = float(b["ask"]), float(b["stake_usd"])
    n = stake / (rec + fee(rec))
    won = 1.0 if b["outcome"] == side else 0.0
    xp = b["exit_price"]
    traded = n * ((float(xp) - fee(float(xp))) if xp is not None else won) - stake
    sold_first = xp is not None and b["exit_ts"] is not None and t_buy is not None and float(b["exit_ts"]) <= t_buy

    def at(price, t, sold=False, n=n, won=won, traded=traded, a0=a0, xp=xp, b=b):
        if price is None:
            return 0.0
        if sold or (xp is not None and b["exit_ts"] is not None and float(b["exit_ts"]) <= t):
            return n * (won - price - fee(price))
        return traded + n * ((a0 + fee(a0)) - (price + fee(price)))

    f0 = path[0][1]
    OUT.append((wait(f0), f0[2] if f0 else 0.0, f0[5] if f0 else 0.0, traded, at(a1, t_buy, sold_first), how, 0.0 if t_buy is None else t_buy - float(b["ts"]), None if a1 is None else a1 - a0, sold_first, at(a60, float(b["ts"]) + HORIZON) if a60 is not None else traded, b["lane"], b["mode"]))


def line(label, xs, col=4):
    if len(xs) < 2:
        return f"  {label:34s} n={len(xs)}"
    dlt = [x[col] - x[3] for x in xs]
    hows = collections.Counter(x[5] for x in xs)
    chg = [x[7] for x in xs if x[7] is not None]
    cse = statistics.stdev(chg) / math.sqrt(len(chg)) if len(chg) > 1 else float("nan")
    tail = f" | ask change {statistics.fmean(chg):+.4f} (se {cse:.4f}) after {statistics.fmean(x[6] for x in xs):.0f}s | {', '.join(f'{k} {v}' for k, v in hows.most_common())} | real sale came first {sum(1 for x in xs if x[8])}" if col == 4 and chg else ""
    return (
        f"  {label:34s} n={len(xs):4d} | as traded {sum(x[3] for x in xs):+9.2f} | changed to {sum(x[col] for x in xs):+9.2f} | change {sum(dlt):+8.2f} (per bet {statistics.fmean(dlt):+.3f}, se {statistics.stdev(dlt) / math.sqrt(len(dlt)):.3f})"
        f" | beat {sum(1 for v in dlt if v > 1e-9)}, lost {sum(1 for v in dlt if v < -1e-9)}, same {sum(1 for v in dlt if abs(v) <= 1e-9)}{tail}"
    )


print(f"\nscore 2, the timer on the bets since {hm(MON)} ET: {len(OUT)} of {len(bets)} scored. Split 2026-11-12 (user: yes, split it): the lane 'manual' is the user's own hand-placed bets, far larger than the bot's, so the two are scored apart; the lane is a label, not a rule")
print("sat out: " + (", ".join(f"{k} {v}" for k, v in skipped.most_common()) or "none"))


def report(label, xs):
    called = [x for x in xs if x[0]]
    quiet = [x for x in xs if not x[0]]
    print(f"\n{label}: {len(xs)} bets, the timer as stated changes them by {sum(x[4] - x[3] for x in called):+.2f}")
    print(line("model called a wait at the buy", called))
    print(line("control: said buy now, 60s late", quiet, col=9))
    print(line("for scale: called a wait, 60s late", called, col=9))
    if len(called) > 30:
        for what, ix in (("last minute's move", 1), ("height above the low", 2)):
            v = sorted(x[ix] for x in called)
            c1, c2 = v[len(v) // 3], v[2 * len(v) // 3]
            print(line(f"  {what} low", [x for x in called if x[ix] < c1]))
            print(line(f"  {what} mid", [x for x in called if c1 <= x[ix] < c2]))
            print(line(f"  {what} high", [x for x in called if x[ix] >= c2]))


report("every bet", OUT)
report("the user's hand-placed bets (lane manual)", [x for x in OUT if x[10] == "manual"])
bot = [x for x in OUT if x[10] != "manual"]
report("the bot's bets, every lane", bot)
for md in sorted({x[11] for x in bot}):
    report(f"the bot's bets, mode {md}", [x for x in bot if x[11] == md])
print("DONE")
