"""What would hedging each commodity bet with the underlying have done to its P&L, once the hedge's own fees are counted? Replayed from the tape.

Every commodity 15-minute bet on record (live and paper) is held to settlement as before. On top of it a hedge in the underlying is run from the
buy to the close: every STEP seconds its size is set from the bucketed OLS betas of scripts/hedge_ols_study.py (dollars the contract gains per 1%
move of the underlying, by the side's price and the time left), as `RATIO` times that beta/0.01 dollars of underlying per contract, short the
underlying against a YES and long it against a NO. The hedge earns the underlying's move since the last step; each time its size changes (the
opening, every re-size, the close) a fee of FEE bps of the dollars traded is charged. No funding, no margin interest, no slippage beyond the fee.
Results are per dollar staked. The betas are fitted on the same days, so the hedge here is flattered a little.

    scripts/cloud.sh run scripts/hedge_sim_study.py [RATIO=1.0] [STEP=15] [FEES=0,3,5,10] [LAST=0]
"""
import math
import os

from kalshi_moonshot import config, db
from kalshi_moonshot.runner import _spot_symbols
from kalshi_moonshot.store import Store

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)

RATIO = float(os.environ.get("RATIO", "1.0"))
STEP = float(os.environ.get("STEP", "15"))
FEES = tuple(float(x) for x in os.environ.get("FEES", "0,3,5,10").split(","))
LAST = float(os.environ.get("LAST", "0"))  # hedge only in the last this many seconds before the close (0 = from the buy)

# OLS betas, dollars per 1% move of the underlying: rows are time left (0-1, 1-3, 3-6, 6+ minutes), columns the favorite's price (.5-.7, .7-.8, .8-.9, .9+)
BETA = ((5.334, 4.723, 4.550, 1.779), (3.265, 3.180, 1.993, 1.131), (2.467, 1.693, 1.186, 0.508), (1.577, 1.241, 0.811, 0.378))


def beta_for(side_price, left):
    fav = max(side_price, 1.0 - side_price)
    col = 0 if fav < 0.70 else 1 if fav < 0.80 else 2 if fav < 0.90 else 3
    row = 0 if left < 60 else 1 if left < 180 else 2 if left < 360 else 3
    return BETA[row][col]


def latest(rows, t, within=6.0):
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


rows = main.db.execute(
    """SELECT d.ts, d.ticker, d.side, d.ask, d.outcome, d.close_time, w.strike FROM decisions d JOIN windows w ON w.ticker = d.ticker
       WHERE d.action='bet' AND d.lane='commodity' AND d.outcome IN ('yes','no') AND d.ticker LIKE '%15M%' AND w.strike > 0 ORDER BY d.ts"""
).fetchall()

from datetime import datetime  # noqa: E402

bets = []  # (base pnl per $, gross hedge pnl per $, dollars traded per $)
for r in rows:
    try:
        close = datetime.fromisoformat(str(r["close_time"])).timestamp()
    except (TypeError, ValueError):
        continue
    syms = _spot_symbols(r["ticker"].split("-")[0])
    if not syms:
        continue
    t0, side, ask, strike = float(r["ts"]), r["side"] or "yes", float(r["ask"]), float(r["strike"])
    quotes = [(float(t), (float(b) + float(a)) / 2.0) for t, b, a in tape.execute(
        "SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 AND ts BETWEEN ? AND ? ORDER BY ts", (r["ticker"], t0 - 10, close)).fetchall()]
    ticks = []
    for sym in syms:
        ticks = [(float(t), float(p)) for t, p in tape.execute("SELECT ts, px FROM feed_ticks WHERE symbol=? AND ts BETWEEN ? AND ? ORDER BY ts", (sym, t0 - 20, close + 5)).fetchall()]
        if len(ticks) > 20:
            break
    if len(quotes) < 10 or len(ticks) < 20:
        continue
    n = 1.0 / ask  # contracts per dollar staked
    base = (1.0 / ask - 1.0) if r["outcome"] == side else -1.0
    yes_sign = 1.0 if side == "yes" else -1.0
    gross, traded, size_prev, spot_prev = 0.0, 0.0, 0.0, None
    t = t0
    while t < close - 0.5:
        spot = latest(ticks, t)
        mid = latest(quotes, t)
        if spot is not None and mid is not None:
            side_mid = mid if side == "yes" else 1.0 - mid
            size = RATIO * beta_for(side_mid, close - t) / 0.01 * n  # dollars of underlying
            if LAST and close - t > LAST:
                size = 0.0
            if spot_prev is not None:
                gross += -yes_sign * size_prev * (spot - spot_prev) / strike
            traded += abs(size - size_prev)
            size_prev, spot_prev = size, spot
        t += STEP
    spot_end = latest(ticks, close, within=20.0)
    if spot_end is not None and spot_prev is not None:
        gross += -yes_sign * size_prev * (spot_end - spot_prev) / strike
    traded += abs(size_prev)  # the close
    bets.append((base, gross, traded))


def stats(pnls):
    n = len(pnls)
    mean = sum(pnls) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in pnls) / (n - 1)) if n > 1 else 0.0
    worst = sorted(pnls)[: max(1, n // 10)]
    return mean, sd, min(pnls), sum(worst) / len(worst)


print(f"{len(bets)} commodity bets, hedge {RATIO:g}x the OLS beta, re-sized every {STEP:g}s{f', only the last {LAST:g}s' if LAST else ''}; per dollar staked")
avg_notional = sum(b[2] for b in bets) / len(bets)
print(f"average hedge traded per $1 staked: ${avg_notional:.0f}", flush=True)
print(f"{'case':<20} {'mean':>8} {'stdev':>7} {'worst':>8} {'worst 10% avg':>14}")
m, sd, w, c = stats([b[0] for b in bets])
print(f"{'unhedged':<20} {m:>+8.1%} {sd:>7.1%} {w:>+8.0%} {c:>+14.0%}")
for fee in FEES:
    m, sd, w, c = stats([b[0] + b[1] - fee / 1e4 * b[2] for b in bets])
    print(f"{f'hedged, {fee:g} bps/side':<20} {m:>+8.1%} {sd:>7.1%} {w:>+8.0%} {c:>+14.0%}")
