"""How much of a favorite's price movement does the underlying explain, and what hedge ratio does OLS give? Replayed from the tape.

Every settled 15-minute market on the tape (commodities and the coins) is sampled every STEP seconds: the contract's YES mid and the underlying's
price (feed_ticks, the same feeds the bot's spot checks use). Each pair of neighbouring samples gives one observation: the change in the
FAVORITE's price (the side priced over 50c) against the change in the underlying as a fraction of the strike, signed so a positive number is
the underlying moving the favorite's way. OLS of the first on the second, pooled in cells of favorite price and time left, gives

    beta  = dollars the contract gains per 1% move of the underlying in its favor
    R2    = the share of the contract's variance that move explains (what a hedge could remove)
    hedge = dollars of underlying to short per contract held to cancel the contract's move, = beta / 0.01

Out of sample: betas are fitted on the earlier 60% of markets and the variance removed is measured on the later 40%.
Analysis only: this places nothing and assumes no costs (fees, funding, slippage on the hedge).

    scripts/cloud.sh run scripts/hedge_ols_study.py [DAYS=3] [STEP=15] [LEFT=0,1,2,3]    # LEFT picks which time-left rows to print
"""
import math
import os
import time

from kalshi_moonshot import config, db
from kalshi_moonshot.runner import _spot_symbols
from kalshi_moonshot.store import Store

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)

DAYS = float(os.environ.get("DAYS", "3"))
STEP = float(os.environ.get("STEP", "15"))
SHOW = {int(x) for x in os.environ.get("LEFT", "0,1,2,3").split(",")}
PRICE_BANDS = ((0.50, 0.70), (0.70, 0.80), (0.80, 0.90), (0.90, 0.97))
LEFT_BANDS = ((0, 60), (60, 180), (180, 360), (360, 600))  # seconds left when the pair starts


def latest(rows, t, within=6.0):
    """The last (ts, value) at or before t and no older than `within` seconds (rows are sorted by time)."""
    lo, hi = 0, len(rows)
    while lo < hi:
        mid = (lo + hi) // 2
        if rows[mid][0] <= t:
            lo = mid + 1
        else:
            hi = mid
    if lo == 0 or t - rows[lo - 1][0] > within:
        return None
    return rows[lo - 1][1]


since = time.time() - DAYS * 86400
windows = main.db.execute(
    "SELECT ticker, series, close_ts, strike FROM windows WHERE result IN ('yes','no') AND close_ts >= ? AND strike > 0 ORDER BY close_ts", (since,)
).fetchall()

obs = []  # (market index, price of the favorite, seconds left, d favorite price, d spot as % of strike)
used = 0
used_idx = []
for idx, w in enumerate(windows):
    syms = _spot_symbols(w["series"])
    if not syms:
        continue
    close = float(w["close_ts"])
    quotes = [(float(t), (float(b) + float(a)) / 2.0) for t, b, a in tape.execute(
        "SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], close - 900, close)).fetchall()]
    if len(quotes) < 20:
        continue
    ticks = []
    for sym in syms:
        ticks = [(float(t), float(p)) for t, p in tape.execute("SELECT ts, px FROM feed_ticks WHERE symbol=? AND ts BETWEEN ? AND ? ORDER BY ts", (sym, close - 920, close)).fetchall()]
        if len(ticks) > 20:
            break
    if len(ticks) < 20:
        continue
    used += 1
    used_idx.append(idx)
    strike = float(w["strike"])
    t = close - 900 + STEP
    prev = None
    while t <= close - 1:
        m, p = latest(quotes, t), latest(ticks, t)
        if m is not None and p is not None:
            if prev is not None and t - prev[0] <= STEP * 1.5:
                fav = max(prev[1], 1.0 - prev[1])
                sign = 1.0 if prev[1] >= 0.5 else -1.0  # a NO favorite gains when YES falls and the underlying falls
                dfav = sign * (m - prev[1])
                dspot = sign * (p - prev[2]) / strike * 100.0
                obs.append((idx, fav, close - prev[0], dfav, dspot))
            prev = (t, m, p)
        else:
            prev = None
        t += STEP


def ols(rows):
    """(n, beta, R2) of d favorite price on d spot, through the origin plus an intercept."""
    n = len(rows)
    if n < 30:
        return None
    xs, ys = [r[4] for r in rows], [r[3] for r in rows]
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    if sxx <= 0 or syy <= 0:
        return None
    return n, sxy / sxx, sxy * sxy / (sxx * syy)


print(f"{used} markets, {len(obs)} {STEP:g}s observations, last {DAYS:g} days")
print(f"{'left':>9} {'fav price':>10} {'obs':>6} {'beta $/1%':>10} {'R2':>6} {'hedge $/contract':>17}", flush=True)
for li, (lo, hi) in enumerate(LEFT_BANDS):
    if li not in SHOW:
        continue
    for plo, phi in PRICE_BANDS:
        cell = [r for r in obs if lo <= r[2] < hi and plo <= r[1] < phi]
        got = ols(cell)
        if got is None:
            continue
        n, beta, r2 = got
        print(f"{lo // 60:>3}-{hi // 60:<3}min {plo:>4.2f}-{phi:<4.2f} {n:>6} {beta:>+10.3f} {r2:>6.2f} {beta / 0.01:>17.1f}")

if 0 in SHOW:
    cut = sorted(used_idx)[int(len(used_idx) * 0.6)]  # the earlier 60% of the markets that have data
    fit = [r for r in obs if r[0] < cut and 60 <= r[2] < 360 and 0.80 <= r[1] < 0.97]
    test = [r for r in obs if r[0] >= cut and 60 <= r[2] < 360 and 0.80 <= r[1] < 0.97]
    got = ols(fit)
    if got and len(test) > 30:
        beta = got[1]
        raw = sum(r[3] ** 2 for r in test) / len(test)
        hedged = sum((r[3] - beta * r[4]) ** 2 for r in test) / len(test)
        print(f"\nout of sample (favorites 0.80-0.97, 1-6 min left): beta {beta:+.3f} fitted on {len(fit)} obs; on {len(test)} later obs the hedge removes {1 - hedged / raw:.0%} of the {STEP:g}s variance (rms {math.sqrt(raw):.3f} -> {math.sqrt(hedged):.3f} $/contract)")
