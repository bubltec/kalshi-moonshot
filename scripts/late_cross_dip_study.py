"""Late cheap-side buy after an earlier strike cross, sold at 0.50 unless the bid spikes (2026-11-12, user's idea).
Question: on markets in their last 2 minutes where one side's bid is 0.08-0.20 and the other's is 0.70-0.80, does buying the cheap side when the 15-minute curve crossed the strike earlier AND the spot now sits in a dip (the cheap side's cushion at the low end of its own 15-minute readings) beat holding those same bets to settlement?
BASE bets: the first second in the last 120 to 30 s left where the cheap side's bid + 0.01 is in 0.08-0.20 and the other side's bid is 0.70-0.80. Every base bet is scored; the user's rule is the subset CROSS and DIP.
  CROSS: the spot's cushion (spot log minus strike log; for non-settlement feeds the open price) changed sign at least once between the open and 60 s before the buy.
  DIP: the 15 s mean cushion toward the cheap side is in the lowest 20% of that market's own 15 s mean readings from the open to the buy (a quantile of its own history, no fixed distance).
EXITS on the same bets (all with the fee only on a taker sale or buy; a resting sell is a maker, no fee):
  hold: to settlement.
  sell50: a sell resting at 0.50 fills when the bid reaches it, else settlement.
  sell50_spike: as sell50, but the sell is cancelled (held to settlement) when the bid spikes: its 10 s rise is at least 3 times the root-mean-square of that bid's own one-second moves since the open times sqrt(10), and at least 0.03. The 3 and the 0.03 are stated before the results and not tuned.
FILL: "touch" bought at the bid at once (no fee, best case), "rest" rests 30 s at the bid and fills only if the ask comes down to it, "take" bought at the ask plus the taker fee.
Curve shape: the curve model's own curvature readings (level, slope, curvature of the cushion path over 5 and 13 minutes, in that window's typical move; curvemodel.features_curvature) at the buy, cut at the terciles of this sample's readings (the cuts are the sample's, stated).
Window: closes since SINCE_S (default Mon 2026-11-09 05:00 UTC; nothing before the logic rewrite).
    scripts/cloud_job.sh scripts/late_cross_dip_study.py STRIDE=1
"""

import bisect
import calendar
import collections
import math
import os
import time as _time

from kalshi_moonshot import config, curvemodel, db, variants
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

os.nice(10)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
STRIDE = int(os.environ.get("STRIDE", "1"))
SINCE_S = int(os.environ.get("SINCE_S", str(calendar.timegm((2026, 11, 9, 5, 0, 0)))))
TARGET = 0.50
SPIKE_Z, SPIKE_MIN, SPIKE_SPAN = 3.0, 0.03, 10
FILLS = ("touch", "rest", "take")
EXITS = ("hold", "sell50", "sell50_spike")
READ = [f"{k}_{m}m" for m in (5, 13) for k in ("level", "slope", "curv")]  # order of curvemodel.features_curvature output

wins = st.db.execute("SELECT ticker, close_ts, strike, result FROM windows WHERE result IN ('yes','no') AND close_ts > ? ORDER BY close_ts", (SINCE_S,)).fetchall()
bets = []  # one dict per base bet
t_start = _time.time()
used = 0
for k_, w in enumerate(wins):
    ser = w["ticker"].split("-")[0]
    crypto = variants.crypto_index(ser)
    ix = crypto or variants.biquote_index(ser)
    if not ser.endswith("15M") or not ix or k_ % STRIDE:
        continue
    close = float(w["close_ts"])
    tk = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts", (ix, close - 2720, close)).fetchall()
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (w["ticker"], close - 900, close)).fetchall()
    if len(tk) < 1200 or len(q) < 30 or float(tk[0][0]) > close - 2650:
        continue
    T_ = [float(x[0]) for x in tk]
    base = int(close) - 2700
    g, j, last = [], 0, None
    for t in range(base, int(close) + 1):
        while j < len(T_) and T_[j] <= t:
            last = float(tk[j][1])
            j += 1
        g.append(math.log(last) if last else None)
    o = 1800
    if any(x is None for x in g[o - 1000 :]):
        continue
    sq = [0.0]
    for a_, b_ in zip(g, g[1:]):
        sq.append(sq[-1] + (((b_ - a_) ** 2) if a_ is not None and b_ is not None else 0.0))
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
    used += 1
    pre = [0.0]
    for x in g[o - 1000 :]:
        pre.append(pre[-1] + x)
    off = o - 1000

    def a15(kk):
        a_ = kk - off
        return (pre[a_ + 1] - pre[a_ - 14]) / 15.0

    def bid_of(sec, yes):
        return None if yb[sec] is None else (yb[sec] if yes else 1.0 - ya[sec])

    def ask_of(sec, yes):
        return None if yb[sec] is None else (ya[sec] if yes else 1.0 - yb[sec])

    won_yes = w["result"] == "yes"
    sig = None
    for sec in range(780, 871):  # 120 to 30 s left
        if yb[sec] is None:
            continue
        for yes in (True, False):
            b0, bo = bid_of(sec, yes), bid_of(sec, not yes)
            if 0.08 <= b0 + 0.01 <= 0.20 and 0.70 <= bo <= 0.80:
                sig = (sec, yes, b0)
                break
        if sig:
            break
    if sig is None:
        continue
    sec, yes, b0 = sig
    sgn = 1.0 if yes else -1.0
    kk = o + sec
    cross = any((g[t] - ref) * (g[t + 1] - ref) < 0 for t in range(o, kk - 60))
    cush = [(a15(t) - ref) * sgn for t in range(o + 15, kk + 1)]
    now_c = cush[-1]
    dip = sum(1 for x in cush if x < now_c) / len(cush) <= 0.20
    read = curvemodel.features_curvature(g, sq, kk, ref, int(sgn), windows=(5, 13))
    if read is None:
        continue
    # the bid's own one-second moves since the open, in the side bought
    mv = []
    prev = None
    for t in range(0, sec + 1):
        b = bid_of(t, yes)
        if b is not None and prev is not None:
            mv.append(b - prev)
        prev = b
    rms = math.sqrt(sum(x * x for x in mv) / len(mv)) if mv else 0.0
    spike_at = None
    for s2 in range(sec + 1, 901):
        b_now, b_then = bid_of(s2, yes), bid_of(s2 - SPIKE_SPAN, yes)
        if b_now is None or b_then is None:
            continue
        rise = b_now - b_then
        if rise >= SPIKE_MIN and rise >= SPIKE_Z * max(rms, 0.005) * math.sqrt(SPIKE_SPAN):
            spike_at = s2
            break
    for fill in FILLS:
        if fill == "rest":
            fs = next((s2 for s2 in range(sec + 1, min(sec + 31, 880)) if ask_of(s2, yes) is not None and ask_of(s2, yes) <= b0 + 1e-9), None)
            price = cost = b0
        elif fill == "touch":
            fs, price, cost = sec, b0, b0
        else:
            a0 = ask_of(sec, yes)
            fs, price, cost = sec, a0, a0 + fee(a0)
        if fs is None:
            continue
        hold = (1.0 if won_yes == yes else 0.0) - cost
        hit = next((s2 for s2 in range(fs + 1, 901) if bid_of(s2, yes) is not None and bid_of(s2, yes) >= TARGET), None)
        out = {"hold": hold}
        out["sell50"] = (TARGET - cost) if hit is not None else hold
        out["sell50_spike"] = (TARGET - cost) if (hit is not None and (spike_at is None or spike_at > hit or spike_at <= fs)) else hold
        bets.append({"kind": "settlement" if crypto else "proxy", "fill": fill, "ts": close - 900 + fs, "price": price, "cross": cross, "dip": dip, "read": read, "hit": hit is not None, "spike": spike_at is not None, **{f"x_{e}": out[e] for e in EXITS}})
    if used % 500 == 0:
        print(f"  {used} windows, {len(bets)} bet rows, {_time.time() - t_start:.0f}s", flush=True)

print(f"windows read {used} (1 in {STRIDE}), closes since {SINCE_S}; base bet rows {len(bets)}")


def line(rows, label):
    n = len(rows)
    if n < 15:
        return f"    {label:38s} n {n:4d}: too few"
    staked = sum(r["price"] for r in rows)
    parts = []
    for e in EXITS:
        tot = sum(r[f"x_{e}"] for r in rows)
        parts.append(f"{e} {tot:+7.2f} ({tot / n * 100:+5.1f}c)")
    beat = sum(1 for r in rows if r["x_sell50"] > r["x_hold"] + 1e-9)
    lost = sum(1 for r in rows if r["x_sell50"] < r["x_hold"] - 1e-9)
    beat2 = sum(1 for r in rows if r["x_sell50_spike"] > r["x_hold"] + 1e-9)
    lost2 = sum(1 for r in rows if r["x_sell50_spike"] < r["x_hold"] - 1e-9)
    won = sum(1 for r in rows if r["x_hold"] > 0) / n
    return f"    {label:38s} n {n:4d} avg {staked / n:.2f} won {won:3.0%} 50c hit {sum(r['hit'] for r in rows) / n:3.0%} spike {sum(r['spike'] for r in rows) / n:3.0%} | " + " | ".join(parts) + f" | sell50 beat/lost held {beat}/{lost}, spike-cancel {beat2}/{lost2}"


GROUPS = (("ALL base", lambda r: True), ("CROSS", lambda r: r["cross"]), ("no cross", lambda r: not r["cross"]), ("DIP", lambda r: r["dip"]), ("CROSS & DIP (user's rule)", lambda r: r["cross"] and r["dip"]), ("not (CROSS & DIP)", lambda r: not (r["cross"] and r["dip"])))
for kind in ("settlement", "proxy"):
    for fill in FILLS:
        rows = [r for r in bets if r["kind"] == kind and r["fill"] == fill]
        if not rows:
            continue
        print(f"\n=== feed {kind}, fill {fill} (dollars per contract; c = cents a bet)")
        for label, f in GROUPS:
            print(line([r for r in rows if f(r)], label))
        user = [r for r in rows if r["cross"] and r["dip"]]
        if len(user) < 45:
            print("  curve-shape split skipped: too few user-rule bets")
            continue
        print("  -- the user's rule by curve shape (terciles of the readings over all base bets of this feed and fill):")
        for ri, name in enumerate(READ):
            vals = sorted(r["read"][ri] for r in rows)
            lo, hi = vals[len(vals) // 3], vals[2 * len(vals) // 3]
            for tag, test in (("low", lambda x: x < lo), ("mid", lambda x: lo <= x < hi), ("high", lambda x: x >= hi)):
                sub = [r for r in user if test(r["read"][ri])]
                print(line(sub, f"{name} {tag} (cuts {lo:+.2f},{hi:+.2f})"))
print("DONE")
