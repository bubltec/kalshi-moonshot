"""Which strike-through line fits each coin? Every settled BTC, ETH and DOGE 15-minute window since the quotes began, one favorite entry each (the first moment from minute 7 to 12 when a side's ask is 0.78 to 0.90),
sold by the strike-through rule (same replay as strike_through_replay.py) at each line and stay, against holding. The line is a percent of the strike; a coin that moves more needs a wider line.
Prints each coin's typical 1-minute move (in percent of the price) so a line can be read as a multiple of it, and the best few settings per coin with how many sales they make. Places nothing.
    scripts/cloud.sh run scripts/strike_through_by_coin.py
"""
import bisect
import math
import os
import statistics

from kalshi_moonshot import config, db
from kalshi_moonshot.digest import EXIT_SLIP
from kalshi_moonshot.kalshiws import index_for_series
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
SERIES = tuple(os.environ.get("SERIES", "KXBTC15M,KXETH15M,KXDOGE15M").split(","))
LINES = tuple(float(x) for x in os.environ.get("LINES", "0.002,0.003,0.005,0.008,0.012,0.018,0.027,0.04").split(","))
STAYS = (20, 30, 45, 60)
WINDOW = 60


def hold_ret(ask, won):
    return (1.0 if won else 0.0) / (ask + fee(ask)) - 1


def sale_ret(ask, bid):
    return (bid - EXIT_SLIP - fee(bid)) / (ask + fee(ask)) - 1


def simulate(p, line, stay):
    """Return of the strike-through rule on one entry, or None if it holds. The same state machine as the watcher: a cross of `line` starts a watch, a return holds to the close, a stay of `stay` seconds sells,
    and in the last WINDOW seconds a turn back down after a bounce, or a pause of half the window, sells."""
    on, since, worst, best, best_ts, bounce = False, 0.0, 0.0, 0.0, 0.0, False
    for ts, v in p["ticks"]:
        left = p["close"] - ts
        if left <= 3:
            return None
        th = (p["strike"] - v) / p["strike"] if p["side"] == "yes" else (v - p["strike"]) / p["strike"]
        if th <= 0:
            if on:
                return None
            continue
        if th >= line:
            since = since or ts
        else:
            since = 0.0
        stayed = since > 0 and ts - since >= stay
        if not on:
            if th >= line:
                on, worst, best, best_ts, bounce = True, th, th, ts, False
            continue
        in_win = left <= WINDOW
        fell = False
        if in_win:
            if th < best:
                best, best_ts, bounce = th, ts, True
            else:
                fell = bounce and th > best
                if not fell and th > worst:
                    worst, best, best_ts, bounce = th, th, ts, False
        if not stayed and not (in_win and (fell or ts - best_ts >= WINDOW / 2)):
            continue
        i = bisect.bisect_right(p["qts"], ts) - 1
        if i < 0 or ts - p["qts"][i] > 5 or p["bids"][i] >= 0.99:
            continue
        return sale_ret(p["ask"], p["bids"][i])
    return None


pop = []
move = {}
for w in main.db.execute(f"SELECT ticker, series, result, close_ts, strike FROM windows WHERE series IN ({','.join('?' * len(SERIES))}) AND result IN ('yes','no') AND strike > 0 ORDER BY close_ts", SERIES).fetchall():
    ix = index_for_series(w["series"])
    lo = w["close_ts"] - 900
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid > 0 AND ask > 0 ORDER BY ts", (w["ticker"], lo, w["close_ts"] + 3)).fetchall()
    entry = next((r for r in q if lo + 420 <= r[0] <= lo + 720 and (0.78 <= r[2] <= 0.90 or 0.78 <= 1 - r[1] <= 0.90)), None)
    if entry is None:
        continue
    yes = 0.78 <= entry[2] <= 0.90
    side = "yes" if yes else "no"
    ticks = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value > 0 ORDER BY ts", (ix, entry[0], w["close_ts"] + 2)).fetchall()
    if len(ticks) < 200:
        continue
    after = [r for r in q if r[0] > entry[0]]
    pop.append({"coin": w["series"][2:5], "side": side, "ask": entry[2] if yes else 1 - entry[1], "won": w["result"] == side, "strike": w["strike"], "close": w["close_ts"], "ticks": [(a, b) for a, b in ticks],
                "qts": [r[0] for r in after], "bids": [r[1] if yes else 1 - r[2] for r in after]})
    v = [b for _, b in ticks]
    mins = [abs(math.log(v[i + 60] / v[i])) * 100 for i in range(0, len(v) - 60, 60)]
    move.setdefault(w["series"][2:5], []).extend(mins)

for coin in sorted(move):
    print(f"{coin}: typical 1-minute move {statistics.median(move[coin]):.4f}% (mean {statistics.mean(move[coin]):.4f}%)")
for coin in sorted({p["coin"] for p in pop}):
    g = [p for p in pop if p["coin"] == coin]
    held = sum(hold_ret(p["ask"], p["won"]) for p in g) / len(g)
    res = []
    for line in LINES:
        for stay in STAYS:
            n = lost = 0
            tot = 0.0
            for p in g:
                r = simulate(p, line / 100, stay)
                if r is None:
                    tot += hold_ret(p["ask"], p["won"])
                else:
                    n += 1
                    lost += not p["won"]
                    tot += r
            res.append((tot / len(g), line, stay, n, lost))
    print(f"\n{coin}: {len(g)} entries, held return/$ {held:+.4f}, whole losses {sum(not p['won'] for p in g)}")
    print(f"  {'line%':>6} {'stay':>4} {'sold':>5} {'were losers':>11} {'return/$':>9} {'vs held':>8}")
    for r, line, stay, n, lost in sorted(res, reverse=True)[: int(os.environ.get('TOP', '6'))]:
        print(f"  {line:>6} {stay:>4} {n:>5} {lost:>11} {r:>+9.4f} {r - held:>+8.4f}")
    byline = {}
    for r, line, stay, n, lost in res:
        byline.setdefault(line, []).append(r - held)
    print("  mean gain over held by line (all stays): " + "  ".join(f"{ln}:{sum(v) / len(v):+.4f}" for ln, v in sorted(byline.items())))
