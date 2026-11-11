"""The user's manual rules, simulated (2026-11-11). The user trades the 15-minute markets by hand like this:
  wave trades: look at the last 5, 15 and 60 minutes and their midpoints; with no wave, skip the market. Sloping down under the target: wait until it starts to even out, then rest a zero-fee buy for UP; when it has gone up and starts to peak, sell. Sloping up above the target: wait until it levels out, buy DOWN; sell when it has sloped down to a dip. Neither needs the price to cross the target. If it does not work, sell unless the history suggests a rebound.
  favorites: on stabler curves, buy in the last 5 minutes at 0.70-0.85; if the bid peaks in the 0.90s before 3 minutes are left, cash out.
Question: written as rules in the market's own units, what do these pay as round trips in the contract's own price, with the fills a resting order would really get?
Units: the cushion is the log spot minus the log reference over the typical minute (rms one-second move of the last 900 s times sqrt(60)). A slope over L seconds is the change between the 15 s average now and the 15 s average L seconds ago, per minute.
WAVE (30 minutes before the reading, 10 s samples, a straight line taken out): amplitude = range of what is left, in typical minutes; turns = sign changes of what is left. "A wave" = amplitude at least A and at least 3 turns; A 0 (no filter) / 3 / 5.
TROUGH (buy YES): cushion under 0, the 5-minute slope at or under -S, the 60 s slope under -S/3 fifteen seconds ago and at or over -S/3 now ("starts to even out"). PEAK (buy NO): the mirror, above the target. S 0.3 / 0.6 typical minutes a minute. Minutes 1 to 11 of the window; the first signal in a window only.
ENTRY fill, two models to bracket it: "rest": a buy rests at the side's bid for 30 s and fills only if the side's ask comes down to that price (nobody is charged a fee on a resting fill); no fill, no trade. "touch": the same order is assumed filled at once at the bid (the best case).
EXIT: after the 60 s slope has been at or over +S/3 toward our side and then falls back under it ("starts to peak"), sell at the bid (taker fee). Give-up: 3 minutes after the buy with the slope never having turned our way, or the cushion 1.5 typical minutes further against us than at the buy, sell at the bid. Otherwise sell at the bid 30 s before the close.
FAVORITE: with 300 to 180 s left, a side whose ask is 0.70-0.85, in a window whose cushion range over the last 15 minutes is at most V typical minutes (V none / 4 / 8) and that has not crossed the target in the last 5 minutes; buy at the ask (taker fee). Cash-out variant: sell at the bid the first time it is 0.90 or more while over 180 s... (the buy is inside the last 5 minutes, so the cash-out window is from the buy to 60 s left; stated here because it differs from the user's wording) ; else hold to settlement.
Per setting: trades, fill rate, net per trade and in total, the two halves by time, and by feed kind. Grid fixed before results.
    scripts/cloud_job.sh scripts/manual_rules_sim.py STRIDE=3
"""

import bisect
import calendar
import collections
import math
import os
import time as _time

from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store

os.nice(10)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
STRIDE = int(os.environ.get("STRIDE", "3"))
SINCE_S = int(os.environ.get("SINCE_S", str(calendar.timegm((2026, 10, 28, 0, 0, 0)))))
from kalshi_moonshot.strategy import fee_per_contract as fee

SS = (0.3, 0.6)
AS = (0.0, 3.0, 5.0)
VS = (None, 4.0, 8.0)
wins = st.db.execute("SELECT ticker, close_ts, strike, result FROM windows WHERE result IN ('yes','no') AND close_ts > ? ORDER BY close_ts", (SINCE_S,)).fetchall()
W = collections.defaultdict(list)  # (kind, S, A, fill) -> trades (ts, pnl, why, side_kind)
F = collections.defaultdict(list)  # (kind, V, cash) -> trades (ts, pnl, hold)
tried = collections.Counter()
used = 0
t_start = _time.time()
for k, w in enumerate(wins):
    ser = w["ticker"].split("-")[0]
    crypto = variants.crypto_index(ser)
    ix = crypto or variants.biquote_index(ser)
    if not ser.endswith("15M") or not ix or k % STRIDE:
        continue
    close = float(w["close_ts"])
    tk = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts", (ix, close - 3620, close)).fetchall()
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (w["ticker"], close - 900, close)).fetchall()
    if len(tk) < 2000 or len(q) < 30 or float(tk[0][0]) > close - 3550:
        continue
    T_ = [float(x[0]) for x in tk]
    base = int(close) - 3600
    g = []
    j = 0
    last = None
    for t in range(base, int(close) + 1):
        while j < len(T_) and T_[j] <= t:
            last = float(tk[j][1])
            j += 1
        g.append(math.log(last) if last else None)
    o = 2700  # index of the window open in g
    if any(x is None for x in g[o - 1900 :]):
        continue
    if crypto:
        if not w["strike"] or float(w["strike"]) <= 0:
            continue
        ref = math.log(float(w["strike"]))
    else:
        ref = g[o]
    qt = [float(x[0]) for x in q]
    yb, ya = [], []
    for t in range(int(close) - 900, int(close) + 1):
        i = bisect.bisect_right(qt, t) - 1
        if i < 0 or t - qt[i] > 20:
            yb.append(None); ya.append(None)
        else:
            yb.append(float(q[i][1])); ya.append(float(q[i][2]))
    pre = [0.0]
    for x in g[o - 1900 :]:
        pre.append(pre[-1] + x)
    off = o - 1900

    def avg15(kk):  # mean of g over the 15 s ending at kk
        a_ = kk - off
        return (pre[a_ + 1] - pre[a_ - 14]) / 15.0

    seg = g[o - 900 : o + 1]
    u = math.sqrt(sum((b_ - a_) ** 2 for a_, b_ in zip(seg, seg[1:])) / 900.0) * math.sqrt(60.0)  # the typical minute going into the window
    if u <= 0:
        continue
    won_yes = w["result"] == "yes"
    kind = "settlement" if crypto else "proxy"
    used += 1

    def slope(kk, L):
        return (avg15(kk) - avg15(kk - L)) / (L / 60.0) / u

    def bid_of(sec, yes):
        return None if yb[sec] is None else (yb[sec] if yes else 1.0 - ya[sec])

    def ask_of(sec, yes):
        return None if yb[sec] is None else (ya[sec] if yes else 1.0 - yb[sec])

    # ---- wave trades
    for S in SS:
        sig = None
        for sec in range(60, 661, 5):
            kk = o + sec
            cush = (avg15(kk) - ref) / u
            s5, s1, s1p = slope(kk, 300), slope(kk, 60), slope(kk - 15, 60)
            if cush < 0 and s5 <= -S and s1p < -S / 3 and s1 >= -S / 3:
                sig = (sec, True, cush)
                break
            if cush > 0 and s5 >= S and s1p > S / 3 and s1 <= S / 3:
                sig = (sec, False, cush)
                break
        if sig is None:
            continue
        sec, yes, cush0 = sig
        kk = o + sec
        xs = list(range(kk - 1800, kk + 1, 10))
        ys = [g[x] for x in xs]
        n = len(xs)
        mx, my = sum(range(n)) / n, sum(ys) / n
        den = sum((i - mx) ** 2 for i in range(n))
        bsl = sum((i - mx) * (y - my) for i, y in zip(range(n), ys)) / den
        res = [(y - my - bsl * (i - mx)) / u for i, y in zip(range(n), ys)]
        amp = max(res) - min(res)
        turns = sum(1 for a_, b_ in zip(res, res[1:]) if (a_ >= 0) != (b_ >= 0))
        b0 = bid_of(sec, yes)
        if b0 is None or b0 <= 0.02 or b0 >= 0.98:
            continue
        sgn = 1.0 if yes else -1.0
        for fill in ("rest", "touch"):
            if fill == "rest":
                fs = next((s2 for s2 in range(sec + 1, min(sec + 31, 870)) if ask_of(s2, yes) is not None and ask_of(s2, yes) <= b0 + 1e-9), None)
            else:
                fs = sec
            for A in AS:
                if amp < A or (A > 0 and turns < 3):
                    continue
                tried[(kind, S, A, fill)] += 1
                if fs is None:
                    continue
                cost = b0  # a resting fill: no fee
                turned = False
                out = None
                why = "close"
                for s2 in range(fs + 5, 871, 5):
                    k2 = o + s2
                    sl = slope(k2, 60) * sgn
                    c2 = (avg15(k2) - ref) / u
                    bd = bid_of(s2, yes)
                    if bd is None:
                        continue
                    if sl >= S / 3:
                        turned = True
                    elif turned:
                        out, why = bd, "peak"
                        break
                    if (not turned and s2 - fs >= 180) or (c2 - cush0) * sgn <= -1.5:
                        out, why = bd, "give-up"
                        break
                if out is None:
                    s2 = 870
                    while s2 > fs and bid_of(s2, yes) is None:
                        s2 -= 1
                    out = bid_of(s2, yes) or 0.0
                pnl = (out - fee(out) if out > 0 else 0.0) - cost
                W[(kind, S, A, fill)].append((close - 900 + fs, pnl, why, "trough" if yes else "peak", b0))
    # ---- favorites
    done = False
    for sec in range(600, 721, 5):
        if done:
            break
        for yes in (True, False):
            a0 = ask_of(sec, yes)
            if a0 is None or not 0.70 <= a0 <= 0.85:
                continue
            kk = o + sec
            cs = [(g[x] - ref) / u for x in range(kk - 900, kk + 1, 10)]
            rng = max(cs) - min(cs)
            recent = cs[-30:]
            if min(recent) < 0 < max(recent):
                continue  # crossed the target in the last 5 minutes
            cost = a0 + fee(a0)
            hold = (1.0 if won_yes == yes else 0.0) - cost
            cash = None
            for s2 in range(sec + 1, 841):
                bd = bid_of(s2, yes)
                if bd is not None and bd >= 0.90:
                    cash = bd - fee(bd) - cost
                    break
            for V in VS:
                if V is not None and rng > V:
                    continue
                F[(kind, V, False)].append((close - 900 + sec, hold, hold))
                F[(kind, V, True)].append((close - 900 + sec, cash if cash is not None else hold, hold))
            done = True
            break
    if used % 400 == 0:
        print(f"  {used} windows, {_time.time() - t_start:.0f}s", flush=True)
print(f"windows {used} (1 in {STRIDE})")
for kind in ("settlement", "proxy"):
    print(f"\n=== feed kind {kind}: WAVE TRADES (dollars per contract)")
    for S in SS:
        for A in AS:
            for fill in ("rest", "touch"):
                T = sorted(W.get((kind, S, A, fill), []))
                n_try = tried[(kind, S, A, fill)]
                if len(T) < 20:
                    print(f"    slope {S}, wave amplitude {A:g}+, {fill:5s}: {n_try} signals, {len(T)} filled: too few")
                    continue
                mid = T[len(T) // 2][0]
                tot = sum(x[1] for x in T)
                h1 = sum(x[1] for x in T if x[0] <= mid)
                why = collections.Counter(x[2] for x in T)
                by = {s_: (sum(x[1] for x in T if x[3] == s_), sum(1 for x in T if x[3] == s_)) for s_ in ("trough", "peak")}
                print(f"    slope {S}, wave amplitude {A:g}+, {fill:5s}: {n_try:4d} signals, {len(T):4d} filled ({len(T) / n_try:.0%}) at {sum(x[4] for x in T) / len(T):.2f} avg | won {sum(1 for x in T if x[1] > 0) / len(T):.0%} | net {tot:+7.2f} ({tot / len(T) * 100:+.1f} cents a trade) | halves {h1:+.2f} / {tot - h1:+.2f} | troughs {by['trough'][0]:+.2f} on {by['trough'][1]}, peaks {by['peak'][0]:+.2f} on {by['peak'][1]} | exits {dict(why)} {'BOTH HALVES +' if h1 > 0 and tot - h1 > 0 else ''}")
    print(f"=== feed kind {kind}: FAVORITES (0.70-0.85 with 5 to 3 minutes left, dollars per contract)")
    for V in VS:
        for cash in (False, True):
            T = sorted(F.get((kind, V, cash), []))
            if len(T) < 20:
                continue
            mid = T[len(T) // 2][0]
            tot = sum(x[1] for x in T)
            h1 = sum(x[1] for x in T if x[0] <= mid)
            print(f"    stability {'any' if V is None else 'range at most ' + format(V, 'g'):18s}, {'cash out at 0.90' if cash else 'hold to settlement':18s}: {len(T):4d} buys | won {sum(1 for x in T if x[1] > 0) / len(T):.0%} | net {tot:+7.2f} ({tot / len(T) * 100:+.1f} cents a buy) | halves {h1:+.2f} / {tot - h1:+.2f} {'BOTH HALVES +' if h1 > 0 and tot - h1 > 0 else ''}")
print("DONE")
