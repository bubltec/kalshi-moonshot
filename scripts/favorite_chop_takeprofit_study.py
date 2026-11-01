"""Choppy periods and a high-price sale, for 80c-90c favorites in the 15-minute BTC, ETH and DOGE markets. Replayed from the tape; places nothing.

Population: every settled window since the quotes began. One entry per window: the first moment from minute 7 to 12 when either side's ask is 0.78 to 0.90 (the lane's favorites), bought at that ask.
Chop at the entry, from the index's last 10 minutes: `range` = high minus low as a share of the strike (in percent); `cross` = how many times the index crossed the strike; `volx` = the 10-minute realized move against the coin's own 6-hour norm.
Part 1: win rate, whole-loss rate and return per $1 by chop tercile, so a gate "skip the buy when chop is high" can be judged.
Part 2: the same bets sold early. (a) when the bid first reaches X (take-profit); (b) when the bid has been at least P and then falls D below its peak (a trailing sale from a high price). Sale = bid minus the exit slip and fee. Held = the result.
    scripts/cloud.sh run scripts/favorite_chop_takeprofit_study.py
"""
import bisect
import math
from collections import defaultdict

from kalshi_moonshot import config, db
from kalshi_moonshot.digest import EXIT_SLIP
from kalshi_moonshot.kalshiws import index_for_series
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
SERIES = ("KXBTC15M", "KXETH15M", "KXDOGE15M")
wins = main.db.execute("SELECT ticker, series, result, close_ts, strike FROM windows WHERE series IN (?,?,?) AND result IN ('yes','no') AND strike > 0 ORDER BY close_ts", SERIES).fetchall()
ticks, norm = {}, {}
for ix in {index_for_series(x) for x in SERIES}:
    r = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND value > 0 ORDER BY ts", (ix,)).fetchall()
    ticks[ix] = ([x[0] for x in r], [x[1] for x in r])
    ts, v = ticks[ix]
    moves = []
    t = ts[0] + 700
    while t < ts[-1]:
        a, b = bisect.bisect_right(ts, t) - 1, bisect.bisect_right(ts, t - 600) - 1
        moves.append(abs(math.log(v[a] / v[b])))
        t += 300
    norm[ix] = sorted(moves)[len(moves) // 2]  # the typical 10-minute move


def ret_hold(ask, won):
    return (1.0 if won else 0.0) / (ask + fee(ask)) - 1


def ret_sale(ask, bid):
    return (bid - EXIT_SLIP - fee(bid)) / (ask + fee(ask)) - 1


pop = []
for w in wins:
    ix = index_for_series(w["series"])
    ts, v = ticks[ix]
    lo = w["close_ts"] - 900
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid > 0 AND ask > 0 ORDER BY ts", (w["ticker"], lo, w["close_ts"] + 3)).fetchall()
    if len(q) < 100:
        continue
    entry = next((r for r in q if lo + 420 <= r[0] <= lo + 720 and (0.78 <= r[2] <= 0.90 or 0.78 <= 1 - r[1] <= 0.90)), None)
    if entry is None:
        continue
    yes_side = 0.78 <= entry[2] <= 0.90
    side = "yes" if yes_side else "no"
    ask = entry[2] if yes_side else 1 - entry[1]
    i0, i1 = bisect.bisect_left(ts, entry[0] - 600), bisect.bisect_right(ts, entry[0])
    seg = v[i0:i1]
    if len(seg) < 200:
        continue
    k = w["strike"]
    rng = (max(seg) - min(seg)) / k * 100
    cross = sum(1 for a, b in zip(seg, seg[1:]) if (a - k) * (b - k) < 0)
    volx = abs(math.log(seg[-1] / seg[0])) / norm[ix] if False else (sum(abs(math.log(b / a)) for a, b in zip(seg[::30], seg[30::30])) / norm[ix])
    path = [(r[0], (r[1] if yes_side else 1 - r[2])) for r in q if r[0] > entry[0]]  # the side's bid
    pt = [p[0] for p in path]
    pb = [p[1] for p in path]
    pop.append({"series": w["series"][2:5], "ask": ask, "won": w["result"] == side, "rng": rng, "cross": cross, "volx": volx, "t": pt, "b": pb, "close": w["close_ts"]})
print(f"{len(pop)} windows with a favorite entry; held win {100 * sum(p['won'] for p in pop) / len(pop):.1f}%  return/$ {sum(ret_hold(p['ask'], p['won']) for p in pop) / len(pop):+.4f}\n")


def terciles(key):
    vals = sorted(p[key] for p in pop)
    return vals[len(vals) // 3], vals[2 * len(vals) // 3]


print("PART 1: chop at the entry")
for key in ("rng", "cross", "volx"):
    a, b = terciles(key)
    print(f"  {key} (cuts {a:.3g} / {b:.3g})")
    for name, f in (("low", lambda p: p[key] <= a), ("mid", lambda p: a < p[key] <= b), ("high", lambda p: p[key] > b)):
        g = [p for p in pop if f(p)]
        lost = sum(1 for p in g if not p["won"])
        print(f"    {name:<4} n {len(g):>4}  win {100 * (1 - lost / len(g)):>5.1f}%  whole losses {lost:>3}  return/$ {sum(ret_hold(p['ask'], p['won']) for p in g) / len(g):+.4f}")


def sell_first_reach(p, x):
    i = next((j for j, b in enumerate(p["b"]) if b >= x and p["t"][j] < p["close"] - 3), None)
    return None if i is None else p["b"][i]


def sell_trail(p, peak_min, drop):
    peak = 0.0
    for j, b in enumerate(p["b"]):
        if p["t"][j] >= p["close"] - 3:
            break
        peak = max(peak, b)
        if peak >= peak_min and b <= peak - drop:
            return b
    return None


def score(rule):
    tot = held = 0.0
    n = saved = cost = 0
    for p in pop:
        bid = rule(p)
        h = ret_hold(p["ask"], p["won"])
        held += h
        if bid is None:
            tot += h
        else:
            r = ret_sale(p["ask"], bid)
            tot += r
            n += 1
            saved += (not p["won"])
            cost += p["won"]
    return n, saved, cost, tot / len(pop), held / len(pop)


print("\nPART 2: early sales (return per $1 over all entries; held is the baseline)")
print(f"  {'rule':<28} {'sold':>5} {'were losers':>11} {'were winners':>12} {'return/$':>9} {'vs held':>8}")
for x in (0.90, 0.92, 0.94, 0.96, 0.98):
    n, sv, c, r, h = score(lambda p, x=x: sell_first_reach(p, x))
    print(f"  take-profit at bid >= {x:<5} {n:>5} {sv:>11} {c:>12} {r:>+9.4f} {r - h:>+8.4f}")
for pm in (0.88, 0.92, 0.95):
    for d in (0.08, 0.12, 0.18, 0.25, 0.35):
        n, sv, c, r, h = score(lambda p, pm=pm, d=d: sell_trail(p, pm, d))
        print(f"  peak >= {pm}, fall {d:<4} {n:>13} {sv:>11} {c:>12} {r:>+9.4f} {r - h:>+8.4f}")
print("\nhow high did the whole losers get after the buy (side bid peak)?")
peaks = sorted(max(p["b"]) for p in pop if not p["won"] and p["b"])
for q in (0.1, 0.25, 0.5, 0.75, 0.9):
    print(f"  {int(q * 100)}th percentile of losers' peak bid: {peaks[int(q * (len(peaks) - 1))]:.2f}")
print(f"  losers: {len(peaks)}; reached 0.90+: {sum(1 for x in peaks if x >= 0.90)}; reached 0.95+: {sum(1 for x in peaks if x >= 0.95)}")

# PART 3: the same chop measures for the lane's own live bets, with their real results (strike-through sales included), cut at the population's terciles
bets = main.db.execute(
    "SELECT d.ts, d.series_x FROM decisions d WHERE 0", ()).fetchall() if False else main.db.execute(
    "SELECT d.ts, d.ticker, d.side, d.ask, d.stake_usd, d.exit_price, d.outcome, w.strike, w.series FROM decisions d JOIN windows w ON w.ticker = d.ticker "
    "WHERE d.lane = 'crypto_favorite' AND d.action = 'bet' AND d.mode = 'trade' AND d.ts > 1793000000 AND d.outcome IN ('yes','no') AND w.strike > 0").fetchall()
rows = []
for b in bets:
    ix = index_for_series(b["series"])
    ts, v = ticks[ix]
    seg = v[bisect.bisect_left(ts, b["ts"] - 600):bisect.bisect_right(ts, b["ts"])]
    if len(seg) < 200:
        continue
    rng = (max(seg) - min(seg)) / b["strike"] * 100
    volx = sum(abs(math.log(y / x)) for x, y in zip(seg[::30], seg[30::30])) / norm[ix]
    won = b["outcome"] == b["side"]
    r = ret_sale(b["ask"], b["exit_price"]) if b["exit_price"] is not None else ret_hold(b["ask"], won)
    rows.append({"rng": rng, "volx": volx, "won": won, "pnl": b["stake_usd"] * r, "whole": b["exit_price"] is None and not won, "stake": b["stake_usd"]})
print(f"\nPART 3: the lane's own {len(rows)} live bets by chop (cuts from the population)")
for key in ("rng", "volx"):
    a, c = terciles(key)
    for name, f in (("low", lambda x: x[key] <= a), ("mid", lambda x: a < x[key] <= c), ("high", lambda x: x[key] > c)):
        g = [x for x in rows if f(x)]
        if g:
            print(f"  {key:<5} {name:<4} n {len(g):>3}  held whole losses {sum(x['whole'] for x in g):>2}  P&L ${sum(x['pnl'] for x in g):+8.2f}")
for key, cut in (("volx", terciles("volx")[1]), ("rng", terciles("rng")[1])):
    kept = [x for x in rows if x[key] <= cut]
    print(f"  skip {key} > {cut:.3g}: keeps {len(kept)} of {len(rows)}, P&L ${sum(x['pnl'] for x in kept):+.2f} (all bets ${sum(x['pnl'] for x in rows):+.2f}), whole losses left {sum(x['whole'] for x in kept)}")

# PART 4: do whole losses cluster? Entries in time order; after a whole loss (a bet that settled lost) in the last M minutes, how often is the next entry a loss?
ordered = sorted(pop, key=lambda p: p["close"])
base = sum(1 for p in ordered if not p["won"]) / len(ordered)
print(f"\nPART 4: loss clustering over {len(ordered)} entries (base whole-loss rate {100 * base:.1f}%)")
for minutes in (15, 30, 60):
    for need in (1, 2, 3):
        n = lost = 0
        for i, p in enumerate(ordered):
            recent = [q for q in ordered[:i] if p["close"] - 900 - minutes * 60 <= q["close"] <= p["close"] - 900 and not q["won"]]  # losses already settled when p was bought
            if len(recent) >= need:
                n += 1
                lost += not p["won"]
        if n:
            print(f"  >= {need} settled loss(es) in the last {minutes:>2} min: {n:>4} entries, loss rate {100 * lost / n:5.1f}%  (return/$ if skipped: {-sum(ret_hold(p['ask'], p['won']) for i, p in enumerate(ordered) if len([q for q in ordered[:i] if p['close'] - 900 - minutes * 60 <= q['close'] <= p['close'] - 900 and not q['won']]) >= need) / n:+.4f} per skipped bet)")
