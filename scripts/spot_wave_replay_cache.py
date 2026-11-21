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
    .venv/bin/python scripts/spot_wave_replay_cache.py [CACHE_DIR]   (the study cache, scripts/study_cache.py; seconds, on the Mac)
Spot = the cache's own ticks: the coin's Kalshi settlement index and the commodity's Kalshi perp reference. Read-only."""

import bisect
import collections
import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import study_cache as sc

R_TURN, MIN_MOVE, PREV, HOLD_F, BACK_U, BOUNCE_U, SLOPE_U = 1.0, 2.0, 3, 0.25, 0.25, float(os.environ.get('BOUNCE_U', '0.10')), 0.15
LO, HI, QTY = 0.15, 0.45, 25
QUANT = float(os.environ.get('QUANT', '0'))  # added after the first results: the wave's slope must be at or above this quantile of the market's own legs over the hour before (rule 2); 0 = off
CONTROL = int(os.environ.get('CONTROL', '0'))  # random-entry control (seed): one random second per window in the same entry span, the cheap side when its ask is in the band; no wave signal
import random
rng = random.Random(CONTROL)
MIN_CLOSE = float(os.environ.get('MIN_CLOSE', '0'))  # only windows closing at or after this epoch second (the fresh-days test)
MODULE = int(os.environ.get('MODULE', '0'))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
from kalshi_moonshot import wavetop
DROP = set(os.environ.get('DROP', '').split(','))  # ablation: 1 = bigger than the previous legs, 2 = the top has held, 3 = back and forth, 4 = opposite slope started
cache = sc.Cache(sys.argv[1] if len(sys.argv) > 1 else ".claude/worktrees/study-cache-02a1b7/job-results/20261121-074523-83364")
print(f"QUANT={QUANT}; {len(cache.windows)} windows, newest close {cache.meta['newest_close_iso']}; R={R_TURN} min move={MIN_MOVE} qty={QTY} band {LO}-{HI}")
t_start = time.time()


def fee(p, q=QTY):
    return math.ceil(0.07 * q * p * (1 - p) * 100 - 1e-9) / 100


def second_grid(T, V, a, b):
    """last value at or before each second in [a, b)."""
    j = bisect.bisect_right(T, a) - 1
    out = []
    cur = V[j] if j >= 0 else None
    n = len(T)
    for s_ in range(a, b):
        while j + 1 < n and T[j + 1] <= s_:
            j += 1
            cur = V[j]
        out.append(cur)
    return out


def quote_grid(qt, qb, qa, a, b):
    bg = second_grid(qt, qb, a, b)
    ag = second_grid(qt, qa, a, b)
    return bg, ag


SALV = {"SV0": 0.0, "SV1": 0.5}
STOP = {"S05": 0.05, "S10": 0.10, "S15": 0.15}
TK = {"T00": 0.0, "T05": 0.05, "T10": 0.10, "T15": 0.15}
bets, signals, dear, skipped = [], 0, 0, 0
diag = []
for w in sorted(cache.windows, key=lambda x: (x["close"], x["ticker"])):
    if w["result"] not in ("yes", "no"):
        continue
    if w["close"] < MIN_CLOSE:
        continue
    close = int(w["close"])
    a = close - 900
    T, V = cache.ticks(w["index"])
    sp = second_grid(T, V, a - 3700, close)  # index 0 is a-3700; window opens at ia
    ia = 3700
    if None in sp[ia - 3600:ia + 900]:
        skipped += 1
        continue
    qt, qb, qa = cache.quotes(w)
    if len(qt) < 200:
        skipped += 1
        continue
    bids, asks = quote_grid(qt, qb, qa, a, close)
    qfirst = max(0, int(qt[0]) - a + 1)  # no quote before this second: no entry before it
    if qfirst > 500 or None in bids[qfirst:] or None in asks[qfirst:]:
        skipped += 1
        continue
    bids = [bids[qfirst]] * qfirst + bids[qfirst:]
    asks = [asks[qfirst]] * qfirst + asks[qfirst:]
    ch = [sp[k] - sp[k - 60] for k in range(ia - 3600 + 60, ia, 10)]
    unit = math.sqrt(sum(c * c for c in ch) / len(ch)) if ch else 0.0  # the hour before the open
    if unit <= 0:
        skipped += 1
        continue
    yes_won = w["result"] == "yes"
    # zigzag over the 30 minutes before the open and the window, second by second
    legs = []  # (dir, slope units/min)
    d = 0
    z0 = ia - 3500 if QUANT else ia - 1800
    piv = (z0, sp[z0])
    ext = piv
    sig = None
    for k in range(z0 + 1, ia + 900):
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
        if t < max(120, qfirst) or t > 600 or sig is not None or len(legs) < 2:
            continue
        dur = ext[0] - piv[0]
        move = abs(ext[1] - piv[1]) / unit
        if dur < 1 or move < MIN_MOVE:
            continue
        slope = move / (dur / 60.0)
        prev = [x[1] for x in legs[-PREV:]]
        if '1' not in DROP and slope <= max(prev):
            continue
        if QUANT:
            allv = sorted(x[1] for x in legs)
            if len(allv) < 8 or slope < allv[min(int(QUANT * len(allv)), len(allv) - 1)]:
                continue
        tau = k - ext[0]
        seg = sp[ext[0]:k + 1]
        if '2' not in DROP and (tau < HOLD_F * dur or max((x - ext[1]) * d for x in seg) > 0):
            continue
        # work in signed units w = value * leg direction, so the top is a maximum for either direction
        low_w = min(x * d for x in seg)  # the deepest point away from the top since the top
        back = (ext[1] * d - v * d) / unit
        if '3' not in DROP and (back < BACK_U or (v * d - low_w) / unit < BOUNCE_U):  # pulled back, then bounced off that low: back and forth
            continue
        kk = k - int(HOLD_F * dur)
        if '4' not in DROP and (sp[kk] - v) * d / unit < SLOPE_U:
            continue
        sig = (k, d, ext, slope, max(prev), dur)
    if MODULE:  # the detector the lane runs (src/kalshi_moonshot/wavetop.py), in place of the inline one: the same bets must come out
        zz = wavetop.Zig(z0, sp[z0], unit, QUANT)
        sig = None
        for k in range(z0 + 1, ia + 900):
            zz.step(k, sp[k])
            t = k - ia
            if t < max(120, qfirst) or t > 600:
                continue
            wv, _why = zz.signal(k, sp)
            if wv is not None:
                sig = (k, wv.d, zz.ext, wv.slope, wv.prev_max, zz.ext[0] - zz.piv[0])
                break
    if CONTROL:
        t_ = rng.randint(max(120, qfirst), 600)
        ya, na = asks[t_], 1 - bids[t_]
        pick = [(p_, d_) for p_, d_ in ((ya, -1), (na, 1)) if LO <= p_ <= HI]
        sig = (ia + t_, min(pick)[1], (ia + t_, sp[ia + t_]), 1.0, 1.0, 60) if pick else None
    if sig is None:
        continue
    signals += 1
    k, d, ext, slope, pmax, legdur = sig
    i0 = k - ia
    side = "n" if d == 1 else "y"
    ask0 = (1 - bids[i0]) if side == "n" else asks[i0]
    if not LO <= ask0 <= HI:
        dear += 1
        continue
    sb = [(1 - x) for x in asks] if side == "n" else list(bids)  # the side's bid each second
    chq = [sb[k_] - sb[k_ - 60] for k_ in range(60, i0, 5)]
    qu = max(math.sqrt(sum(c * c for c in chq) / len(chq)), 0.01) if chq else 0.05  # the side bid's own typical minute before the buy
    won = (not yes_won) if side == "n" else yes_won
    cost = ask0 * QTY + fee(ask0)
    settle = (1.0 if won else 0.0) * QTY - cost

    def sell(i, fees):
        return sb[i] * QTY - cost - (fee(sb[i]) if fees else 0.0)

    res = {}
    for fees in (True, False):
        out = {"H": settle}
        for name in ("R", "R2", "RF", "RC", "P20", "PF", "P20R", "SL", "SLC", "SLF", "T00", "T05", "T10", "T15", "S05", "S10", "S15", "SV0", "SV1", "SV2", "SVR"):
            val = None
            pk, ipk, armed = ask0, i0, False
            lo_b, ilo = sb[i0], i0
            pk2 = lambda i_: max(sb[i0:i_ + 1])
            for i in range(i0 + 1, 899):
                kk = ia + i
                left = 900 - i
                if name in ("SL", "SLC", "SLF"):
                    # the user's exit (2026-11-21): a resting sell at entry + 0.40 (maker, no fee, for the spikes); aim for +0.20 or more and keep waiting until the climb slows down; the SLC variant adds the late check
                    if sb[i] >= ask0 + 0.40:
                        val = (ask0 + 0.40) * QTY - cost
                        if fees: diag.append(("rest", val))
                        break
                    if sb[i] > pk:
                        pk, ipk = sb[i], i
                    if pk - ask0 >= 0.20:
                        armed = True
                    if armed:
                        s_run = (pk - ask0) / max(ipk - i0, 1)
                        wl = max(int(0.25 * (i - i0)), 1)  # a quarter of the time since the buy, never fixed seconds
                        s_rec = (sb[i] - sb[i - wl]) / wl
                        if s_rec <= 0.25 * s_run:  # the climb has slowed to a quarter of the run's own pace
                            val = sell(i, fees)
                            if fees and name == "SL": diag.append(("slow", val))
                            break
                    elif name == "SLF" and i == i0 + max(int(0.5 * legdur), 1) and (sp[ia + i0] - sp[kk]) * d < 0.25 * unit and pk - ask0 < 0.10:
                        # no curve (added after seeing the first SL result): half a leg-duration after the buy the spot has not gone another 0.25 typical minute the opposite way and the bid is not up 0.10: sell
                        val = sell(i, fees)
                        break
                    elif name == "SLC" and left <= 120:
                        dn = (0.5 - sb[i]) / unit
                        if not (sb[i] >= 0.5 or dn <= 1.0 or (sb[i] - ask0) >= unit):
                            val = sell(i, fees)
                            break
                    continue
                if name == "SVR" and sb[i] >= ask0 + 0.40:  # SV2 plus the user's resting sale at entry + 0.40 (maker, no fee)
                    val = (ask0 + 0.40) * QTY - cost
                    break
                if name in ("SV2", "SVR"):  # the user's 2026-11-21 watch: sell when the spot has stopped falling back AND the Kalshi bid's climb has faded; armed only after the bid dipped 1 unit under the entry bid and has since recovered at least half a unit off its low
                    if sb[i] < sb[i0] - qu:
                        armed = True
                    if armed:
                        if sb[i] < lo_b:
                            lo_b, ilo = sb[i], i
                        elif sb[i] - lo_b >= 0.5 * qu and i - ilo >= 4:
                            wl = max(int(0.25 * (i - ilo)), 1)
                            s_run = (pk2(i) - lo_b) / max(i - ilo, 1)
                            s_rec = (sb[i] - sb[i - wl]) / wl
                            spot_turn = (sp[kk] - sp[kk - wl]) * d >= 0  # spot no longer falling away from the top (d = the leg direction; the bet wins when the spot moves against d)
                            if s_rec <= 0.25 * s_run and spot_turn:
                                val = sell(i, fees)
                                break
                    continue
                if name in SALV:  # the user's salvage sale (2026-11-21 chart): only a bet that has gone under water (bid at least 1 quote-unit below the entry bid) is sold, when its bid recovers to the entry bid (SV0) or to half a unit under it (SV1); a bet that never dipped is held to settlement
                    if sb[i] < sb[i0] - qu:
                        armed = True
                    if armed and sb[i] >= sb[i0] - SALV[name] * qu:
                        val = sell(i, fees)
                        break
                    continue
                if name in STOP:  # +0.05 target and a stop at the entry less x (added after seeing the targets: the bets that never get +0.05 end at zero)
                    if sb[i] - ask0 >= 0.05 or sb[i] <= sb[i0] - STOP[name]:
                        val = sell(i, fees)
                        break
                    continue
                if name in TK:
                    if sb[i] - ask0 >= TK[name]:  # added after the user asked what lower targets do (2026-11-21): sell at the bid at +x, else hold
                        val = sell(i, fees)
                        break
                    continue
                if name == "P20" and sb[i] - ask0 >= 0.20:  # profit take only (added after the first run: the user's scalps sold about +0.20; no failed-turn sale)
                    val = sell(i, fees)
                    break
                if name == "PF" and sb[i] >= 0.5:  # flip sale only
                    val = sell(i, fees)
                    break
                if name == "P20R" and sb[i] - ask0 >= 0.20:
                    val = sell(i, fees)
                    break
                if name in ("P20", "PF"):
                    continue
                if (sp[kk] - ext[1]) * d >= (0.0 if name in ("R2", "P20R") else -0.1 * unit):  # the top is retaken (R2, added after the first result: only a new extreme counts): the turn failed
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
            if name == "SL" and fees and val is None: diag.append(("held", settle))
            out[name] = val if val is not None else settle
        res[fees] = out
    tr = {}
    for nm, ref in (("24h", w.get("ref24")), ("72h", w.get("ref72")), ("1h", sp[ia - 3600])):
        tr[nm] = None if not ref or sp[ia] == ref else ("bet with trend" if (sp[ia] - ref) * d < 0 else "bet against trend")  # the bet is the reversal of the wave: a wave against the trend makes it a bet with the trend
    inv_ask = 1 - sb[i0]  # the other side (the wave continues), bought at its ask
    inv_h = (0.0 if won else 1.0) * QTY - inv_ask * QTY - fee(inv_ask)
    up24 = None if not w.get("ref24") or sp[ia] == w["ref24"] else sp[ia] > w["ref24"]
    ups = {}
    for hh in (1, 3, 6):
        jj = bisect.bisect_right(T, a - hh * 3600) - 1
        ups[hh] = None if jj < 0 or a - hh * 3600 - T[jj] > 1800 or V[jj] == sp[ia] else sp[ia] > V[jj]
    ups[24] = up24
    bets.append({"ups": ups, "i0": i0, "inv_ask": inv_ask, "inv_h": inv_h, "up24": up24, "trend": tr, "ask0": ask0, "age": (a + i0) - qt[max(bisect.bisect_right(qt, a + i0) - 1, 0)], "spread": asks[i0] - bids[i0], "day": time.strftime("%m-%d", time.gmtime(close)), "peak": max(sb[i0 + 1:899]) - ask0, "won": won, "series": w["series"], "kind": w["kind"], "d": d, "strong": slope >= 1.5 * pmax, "res": res})

print(f"{signals} signals, {dear} passed (side outside {LO}-{HI}), {skipped} windows skipped for missing spot; {len(bets)} bets")
for name, label in (("H", "hold to settlement"), ("R", "failed-turn exit"), ("R2", "failed turn = new extreme"), ("RF", "failed-turn + flip sale"), ("RC", "failed-turn + 2-min check"), ("P20", "profit take +0.20 only"), ("PF", "flip sale only"), ("P20R", "+0.20 take and new-extreme fail"), ("SL", "0.40 resting + slow-down"), ("T00", "sell at bid >= entry ask, else hold"), ("T05", "sell at +0.05, else hold"), ("T10", "sell at +0.10, else hold"), ("T15", "sell at +0.15, else hold"), ("SV0", "salvage: dipped 1 unit, sell back at entry bid"), ("SV2", "salvage: dipped, bid climb faded and spot turned"), ("SVR", "SV2 + resting sale at +0.40"), ("SV1", "salvage: sell at 0.5 unit under entry bid"), ("S05", "+0.05 target, stop -0.05"), ("S10", "+0.05 target, stop -0.10"), ("S15", "+0.05 target, stop -0.15"), ("SLC", "SL + 2-min check"), ("SLF", "SL + no-curve exit")):
    r = [b["res"][True][name] for b in bets]
    rf = [b["res"][False][name] for b in bets]
    h = [b["res"][True]["H"] for b in bets]
    beat = sum(1 for x, y in zip(r, h) if x > y + 1e-9)
    lost = sum(1 for x, y in zip(r, h) if x < y - 1e-9)
    print(f"{name:3} {label:26} n={len(r)} net ${sum(r):8.2f} (sales fee-free ${sum(rf):8.2f}) win rate {sum(1 for x in r if x > 0)/max(len(r),1):4.0%} beat H {beat:3d} lost to H {lost:3d}")
print("\nreach: share of bets whose bid ever got to entry +x, and of those that did not, the share that ended at zero (total loss)")
for x in (0.0, 0.05, 0.10, 0.15, 0.20, 0.40):
    miss = [b for b in bets if b["peak"] < x - 1e-9]
    print(f"  +{x:.2f}: reached {1 - len(miss)/len(bets):4.0%}  not reached n={len(miss):4d}  of those ended at zero {sum(1 for b in miss if not b['won'])/max(len(miss),1):4.0%}")
print(f"  all bets: won {sum(1 for b in bets if b['won'])/len(bets):4.0%}")
for kd in ("rest", "slow", "held"):
    L = [v for k_, v in diag if k_ == kd]
    print(f"SL exits {kd:5} n={len(L):4d} avg ${sum(L)/max(len(L),1):6.2f} total ${sum(L):8.2f}")
print("\nby curve shape at the bet (net $, with sales fees: H R RF RC)")
for key in sorted({(b["d"], b["strong"]) for b in bets}):
    L = [b for b in bets if (b["d"], b["strong"]) == key]
    print(f"  leg {'up  ' if key[0] == 1 else 'down'} slope>=1.5x prev {key[1]!s:5} n={len(L):3d}  " + "  ".join(f"{m} {sum(b['res'][True][m] for b in L):8.2f}" for m in ("H", "P20", "SL", "SLC", "SLF")))
print("\nby series (label only): n, H, RF")
by = collections.defaultdict(list)
for b in bets:
    by[b["series"]].append(b)
for sr, L in sorted(by.items(), key=lambda kv: -len(kv[1])):
    print(f"  {sr:14} n={len(L):3d}  H {sum(b['res'][True]['H'] for b in L):8.2f}  RF {sum(b['res'][True]['RF'] for b in L):8.2f}")
print("\nby the longer trend (user 2026-11-21: the wave effect likely has to do with the general daily trend); trend = the spot at the open against its value 24 h, 72 h and 1 h before; net $ with fees")
for nm in ("24h", "72h", "1h"):
    for lab in ("bet with trend", "bet against trend"):
        L = [b for b in bets if b["trend"][nm] == lab]
        if L:
            hs_ = [b["res"][True]["H"] for b in L]; m2 = sum(hs_) / len(L); sd2 = math.sqrt(sum((x - m2) ** 2 for x in hs_) / len(L))
            print(f"  {nm:3} {lab:17} n={len(L):4d} mean ask {sum(b['ask0'] for b in L)/len(L):.3f} win {sum(b['won'] for b in L)/len(L):.3f}  H {sum(hs_):8.2f} +- {sd2*math.sqrt(len(L)):4.0f}  SV2 {sum(b['res'][True]['SV2'] for b in L):8.2f}  SL {sum(b['res'][True]['SL'] for b in L):8.2f}")
print("\nday direction x wave direction (user 2026-11-21: maybe inverse the wave when the day is trending up); reversal = the cheap side as bet, inverse = the other side at its ask, both held to settlement, fee on the buy")
for u in (True, False):
    for dd in (1, -1):
        L = [b for b in bets if b["up24"] is u and b["d"] == dd]
        if L:
            n2 = len(L); hs_ = [b["res"][True]["H"] for b in L]; iv = [b["inv_h"] for b in L]
            se = lambda v: math.sqrt(sum((x - sum(v) / n2) ** 2 for x in v))
            print(f"  day {'up  ' if u else 'down'} wave {'up  ' if dd == 1 else 'down'} n={n2:4d} reversal: ask {sum(b['ask0'] for b in L)/n2:.3f} win {sum(b['won'] for b in L)/n2:.3f} H {sum(hs_):8.2f} +- {se(hs_):4.0f} SV2 {sum(b['res'][True]['SV2'] for b in L):8.2f} | inverse: ask {sum(b['inv_ask'] for b in L)/n2:.3f} H {sum(iv):8.2f} +- {se(iv):4.0f}")
print("\ntrend horizon x wave (user 2026-11-21: not a whole day, the past hour or 3 hours): rising = the spot at the open above its value h hours before; reversal bet held, fees on")
for hh in (1, 3, 6, 24):
    for u in (True, False):
        for dd in (1, -1):
            L = [b for b in bets if b["ups"][hh] is u and b["d"] == dd]
            if L:
                n2 = len(L); hs_ = [b["res"][True]["H"] for b in L]; m2 = sum(hs_) / n2
                print(f"  {hh:2d}h {'rising ' if u else 'falling'} {'peak bet' if dd == 1 else 'dip bet '} n={n2:4d} win {sum(b['won'] for b in L)/n2:.3f} H {sum(hs_):8.2f} +- {math.sqrt(sum((x - m2) ** 2 for x in hs_)):4.0f} per bet {m2:+.2f} SV2 {sum(b['res'][True]['SV2'] for b in L):8.2f}")
print("\nby when the buy happened (minutes after the open; buys are allowed from 2:00 to 10:00)")
for lo_, hi_ in ((120, 240), (240, 360), (360, 480), (480, 601)):
    L = [b for b in bets if lo_ <= b["i0"] < hi_]
    if L:
        n2 = len(L); hs_ = [b["res"][True]["H"] for b in L]; m2 = sum(hs_) / n2
        print(f"  {lo_//60}-{hi_//60} min n={n2:4d} ({n2/len(bets):3.0%}) ask {sum(b['ask0'] for b in L)/n2:.3f} win {sum(b['won'] for b in L)/n2:.3f} H {sum(hs_):8.2f} +- {math.sqrt(sum((x - m2) ** 2 for x in hs_)):4.0f} SV2 {sum(b['res'][True]['SV2'] for b in L):8.2f} SL {sum(b['res'][True]['SL'] for b in L):8.2f} reach+0.20 {sum(1 for b in L if b['peak'] >= 0.20)/n2:.2f}")
print("\nsimulation checks (hold to settlement)")
n_ = len(bets); hs = [b["res"][True]["H"] for b in bets]
m_ = sum(hs) / n_; sd = math.sqrt(sum((x - m_) ** 2 for x in hs) / n_)
print(f"  mean ask {sum(b['ask0'] for b in bets)/n_:.3f}  win rate {sum(b['won'] for b in bets)/n_:.3f}  net/bet {m_:.2f}  sd {sd:.2f}  total {sum(hs):.0f} +- {sd*math.sqrt(n_):.0f} (one standard error)")
ages = sorted(b["age"] for b in bets); sprs = sorted(b["spread"] for b in bets)
print(f"  quote age at the buy (s): median {ages[n_//2]:.0f}  90th pct {ages[int(.9*n_)]:.0f}  max {ages[-1]:.0f};  spread median {sprs[n_//2]:.2f} 90th {sprs[int(.9*n_)]:.2f}")
for lo_, hi_ in ((0, 2), (2, 10), (10, 9999)):
    L = [b for b in bets if lo_ <= b["age"] < hi_]
    if L: print(f"  quote age {lo_}-{hi_}s n={len(L):4d} mean ask {sum(b['ask0'] for b in L)/len(L):.3f} win {sum(b['won'] for b in L)/len(L):.3f} H ${sum(b['res'][True]['H'] for b in L):8.2f}")
for lo_, hi_ in ((0.15, 0.25), (0.25, 0.35), (0.35, 0.4501)):
    L = [b for b in bets if lo_ <= b["ask0"] < hi_]
    if L: print(f"  ask {lo_:.2f}-{hi_:.2f} n={len(L):4d} mean ask {sum(b['ask0'] for b in L)/len(L):.3f} win {sum(b['won'] for b in L)/len(L):.3f} H ${sum(b['res'][True]['H'] for b in L):8.2f}")
for dy in sorted({b["day"] for b in bets}):
    L = [b for b in bets if b["day"] == dy]
    print(f"  {dy} n={len(L):4d} win {sum(b['won'] for b in L)/len(L):.2f} H ${sum(b['res'][True]['H'] for b in L):8.2f}")
print("DONE")
print(f"{time.time() - t_start:.0f}s")
