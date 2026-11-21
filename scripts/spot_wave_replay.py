"""Replay of the user's wave-top reversal scalp (2026-11-21). User: "I usually bet on when the slope is larger than previous slopes. So it made a larger wave, bet around the top of that one ... You need to watch it go back and forth a little and then the spot will start to go the opposite slope, if it doesn't we aren't hitting a curve and need to exit but probably shouldn't have bet on the first place." (Confirmed: the bet is the reversal, the side that wins if the spot turns back.)
Question: on the bets this rule makes, what does each exit make net of the fee on every leg, against holding to settlement?
Spot = Kalshi's own perp reference (KP_* tape indexes; commodities through perpref.SERIES_INDEX, coins as KP_<coin>). A typical minute = the root-mean-square 60-second change of that spot over the hour before the window opened. Series without a Kalshi perp are skipped. Series are never used by the rule; they label one table.
Legs: a zigzag over the spot (1-second grid) that confirms a turn when the spot retraces R=1 typical minute from the running extreme. A leg's slope = its move in typical minutes per minute of its own duration.
Signal, tested each second from 120 s after the open until 300 s before the close (all fractions are of the big leg's own duration, never fixed seconds):
  1. the running leg (last pivot to the running extreme E at time t_e) moved at least 2 typical minutes and its slope is above the slopes of each of the previous 3 confirmed legs (at least 2 of them must exist);
  2. the top has held: the spot has not passed E since t_e, and t_e is at least 0.25 x the leg's duration ago;
  3. it went back and forth: the spot is back at least 0.25 typical minute from E and has bounced at least 0.1 typical minute off its low since t_e;
  4. the opposite slope has started: the spot is lower (for an up leg; higher for a down leg) by at least 0.15 typical minute than 0.25 x the leg's duration ago.
Bet: the side that wins if the spot keeps reversing (NO after an up leg, YES after a down leg), bought at the ask when it is 0.15 to 0.45 (the user's cheap-side band; a signal on a dearer side is counted and passed), 25 contracts, first signal per window only.
Exits (all from the same bets):
  H  hold to settlement.
  R  failed turn: sell at the bid when the spot retakes E (within 0.1 typical minute of the top); else hold.
  RF R plus sell at the bid when the side's bid reaches 0.50 (flipped).
  RC R plus the user's late check: at 120 s left sell at the bid unless the side has flipped, its bid is within 1 typical minute of 0.50 or it is up 1 typical minute on the entry.
Settings above were set before seeing results. Taker fee ceil(0.07*C*P*(1-P)) on the buy and on every sale before settlement; a second net column shows the sales fee-free (the user's own sales were makers).
Curve shape at the bet, from the wave itself (the curve model's features are not loaded here): direction of the leg, and whether its slope is at least 1.5 x the largest of the previous 3 legs.
    scripts/cloud_job.sh scripts/spot_wave_replay.py DAYS=3
Read-only."""

import bisect
import collections
import math
import os
import time

from kalshi_moonshot import config, db, perpref

os.nice(10)
DAYS = float(os.environ.get("DAYS", "3"))
R_TURN, MIN_MOVE, PREV, HOLD_F, BACK_U, BOUNCE_U, SLOPE_U = 1.0, 2.0, 3, 0.25, 0.25, 0.10, 0.15
LO, HI, QTY = 0.15, 0.45, 25
s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
end = float(tape.execute("SELECT max(ts) AS m FROM quotes").fetchone()["m"])
t0 = end - DAYS * 86400
print(f"period {time.strftime('%m-%d %H:%M', time.gmtime(t0))} to {time.strftime('%m-%d %H:%M', time.gmtime(end))} UTC; R={R_TURN} min move={MIN_MOVE} qty={QTY} band {LO}-{HI}")

COINS = {"BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE", "NEAR", "ADA", "BCH"}


def index_for(series):
    core = series[2:-3]
    if core in COINS:
        return "KP_" + core
    return perpref.index_for_series(series)


def fee(p, q=QTY):
    return math.ceil(0.07 * q * p * (1 - p) * 100 - 1e-9) / 100


spot = {}  # index -> (start second, list of last value each second)


def load_spot(ix):
    if ix in spot:
        return spot[ix]
    lo = int(t0 - 3700)
    rows = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (ix, lo - 1)).fetchall()
    n = int(end) - lo + 2
    arr = [None] * n
    for r in rows:
        k = int(r["ts"]) - lo
        if 0 <= k < n:
            arr[k] = float(r["value"])
    last = None
    for k in range(n):
        if arr[k] is None:
            arr[k] = last
        else:
            last = arr[k]
    spot[ix] = (lo, arr)
    return spot[ix]


def grid(rows, a, b):
    ob, oa = [None] * (b - a), [None] * (b - a)
    for ts, bid, ask in rows:
        k = int(ts) - a
        if 0 <= k < b - a and bid is not None and ask is not None:
            ob[k], oa[k] = float(bid), float(ask)
    lb = la = None
    for k in range(b - a):
        if ob[k] is None:
            ob[k], oa[k] = lb, la
        else:
            lb, la = ob[k], oa[k]
    return ob, oa


tickers = [r["ticker"] for r in tape.execute("SELECT DISTINCT ticker FROM quotes WHERE ts > ? AND ticker LIKE 'KX%15M-%'", (t0,)).fetchall()]
bets, signals, dear, nowin, skipped = [], 0, 0, 0, 0
for tk in sorted(tickers):
    series = tk.split("-")[0]
    ix = index_for(series)
    if ix is None:
        continue
    rows = [(r["ts"], r["bid"], r["ask"]) for r in tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts>? ORDER BY ts", (tk, t0 - 1)).fetchall()]
    if len(rows) < 200:
        continue
    first, last = int(rows[0][0]), int(rows[-1][0])
    close = (last // 900 + 1) * 900 if last % 900 else last
    a = close - 900
    if first > a + 120 or last < close - 30:
        continue
    bids, asks = grid(rows, a, close)
    if None in bids:
        continue
    base, sp = load_spot(ix)
    ia = a - base
    if ia < 1900 or ia + 900 >= len(sp) or None in sp[ia - 1900:ia + 900]:
        skipped += 1
        continue
    # typical minute of this spot over the hour before the open: rms of 60 s changes sampled every 10 s
    ch = [sp[k] - sp[k - 60] for k in range(max(60, ia - 3600), ia, 10)]
    unit = math.sqrt(sum(c * c for c in ch) / len(ch)) if ch else 0.0
    if unit <= 0:
        skipped += 1
        continue
    yes_won = (bids[-1] + asks[-1]) / 2 > 0.5
    # zigzag over the 30 minutes before the open and the window, second by second
    legs = []  # (dir, slope units/min)
    d = 0
    piv = (ia - 1800, sp[ia - 1800])
    ext = piv
    sig = None
    for k in range(ia - 1800 + 1, ia + 900):
        v = sp[k]
        if d == 0:
            if abs(v - piv[1]) >= R_TURN * unit:
                d = 1 if v > piv[1] else -1
                ext = (k, v)
            continue
        if (v - ext[1]) * d > 0:
            ext = (k, v)
        elif (ext[1] - v) * d >= R_TURN * unit:  # turn confirmed: the running leg is complete
            dur = max(ext[0] - piv[0], 1)
            legs.append((d, abs(ext[1] - piv[1]) / unit / (dur / 60.0)))
            piv = ext
            d = -d
            ext = (k, v)
        t = k - ia
        if t < 120 or t > 600 or sig is not None or len(legs) < 2:
            continue
        dur = ext[0] - piv[0]
        move = abs(ext[1] - piv[1]) / unit
        if dur < 1 or move < MIN_MOVE:
            continue
        slope = move / (dur / 60.0)
        prev = [x[1] for x in legs[-PREV:]]
        if slope <= max(prev):
            continue
        tau = k - ext[0]
        if tau < HOLD_F * dur:
            continue
        seg = sp[ext[0]:k + 1]
        if max((x - ext[1]) * d for x in seg) > 0:
            continue
        # work in signed units w = value * leg direction, so the top is a maximum for either direction
        low_w = min(x * d for x in seg)  # the deepest point away from the top since the top
        back = (ext[1] * d - v * d) / unit
        if back < BACK_U:
            continue
        if (v * d - low_w) / unit < BOUNCE_U:  # it has bounced off that low: back and forth
            continue
        kk = k - int(HOLD_F * dur)
        if (sp[kk] - v) * d / unit < SLOPE_U:
            continue
        sig = (k, d, ext, slope, max(prev))
    if sig is None:
        continue
    signals += 1
    k, d, ext, slope, pmax = sig
    i0 = k - ia
    side = "n" if d == 1 else "y"
    ask0 = (1 - bids[i0]) if side == "n" else asks[i0]
    if not LO <= ask0 <= HI:
        dear += 1
        continue
    sb = [(1 - x) for x in asks] if side == "n" else list(bids)  # the side's bid each second
    won = (not yes_won) if side == "n" else yes_won
    cost = ask0 * QTY + fee(ask0)
    settle = (1.0 if won else 0.0) * QTY - cost

    def sell(i, fees):
        return sb[i] * QTY - cost - (fee(sb[i]) if fees else 0.0)

    res = {}
    for fees in (True, False):
        out = {"H": settle}
        for name in ("R", "RF", "RC"):
            val = None
            for i in range(i0 + 1, 899):
                kk = ia + i
                left = 900 - i
                if (sp[kk] - ext[1]) * d >= -0.1 * unit:  # the top is retaken: the turn failed
                    val = sell(i, fees)
                    break
                if name in ("RF", "RC") and sb[i] >= 0.5:
                    val = sell(i, fees)
                    break
                if name == "RC" and left <= 120:
                    dn = (0.5 - sb[i]) / unit
                    if not (sb[i] >= 0.5 or dn <= 1.0 or (sb[i] - ask0) >= unit):
                        val = sell(i, fees)
                        break
            out[name] = val if val is not None else settle
        res[fees] = out
    bets.append({"series": series, "d": d, "strong": slope >= 1.5 * pmax, "res": res})

print(f"{signals} signals, {dear} passed (side outside {LO}-{HI}), {skipped} windows skipped for missing spot; {len(bets)} bets")
for name, label in (("H", "hold to settlement"), ("R", "failed-turn exit"), ("RF", "failed-turn + flip sale"), ("RC", "failed-turn + 2-min check")):
    r = [b["res"][True][name] for b in bets]
    rf = [b["res"][False][name] for b in bets]
    h = [b["res"][True]["H"] for b in bets]
    beat = sum(1 for x, y in zip(r, h) if x > y + 1e-9)
    lost = sum(1 for x, y in zip(r, h) if x < y - 1e-9)
    print(f"{name:3} {label:26} n={len(r)} net ${sum(r):8.2f} (sales fee-free ${sum(rf):8.2f}) win rate {sum(1 for x in r if x > 0)/max(len(r),1):4.0%} beat H {beat:3d} lost to H {lost:3d}")
print("\nby curve shape at the bet (net $, with sales fees: H R RF RC)")
for key in sorted({(b["d"], b["strong"]) for b in bets}):
    L = [b for b in bets if (b["d"], b["strong"]) == key]
    print(f"  leg {'up  ' if key[0] == 1 else 'down'} slope>=1.5x prev {key[1]!s:5} n={len(L):3d}  " + "  ".join(f"{m} {sum(b['res'][True][m] for b in L):8.2f}" for m in ("H", "R", "RF", "RC")))
print("\nby series (label only): n, H, RF")
by = collections.defaultdict(list)
for b in bets:
    by[b["series"]].append(b)
for sr, L in sorted(by.items(), key=lambda kv: -len(kv[1])):
    print(f"  {sr:14} n={len(L):3d}  H {sum(b['res'][True]['H'] for b in L):8.2f}  RF {sum(b['res'][True]['RF'] for b in L):8.2f}")
print("DONE")
