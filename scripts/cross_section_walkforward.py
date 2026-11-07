"""Scan every crypto market at once and play the best-looking one: a cross-sectional walk-forward on the five coins' 15-minute markets.

Question (2026-11-07, user: a few spot checks across markets always turn up something whose hour or 15-minute history looks like a good bet, for a scalp and for a bet to win, and a bot can scan all of them in a fraction of a second): at every moment (every 30 s, minute 1 to 13) there are up to ten candidates, the lowest-priced side of each of the five coins' markets. Does choosing, among those candidates, the one or two a model fit on the EARLIER days likes best beat playing them all, and does it clear zero after fees on the LATER days?
Features are the single-market curve readings of scripts/wave_model_walkforward.py plus the cross-section: how far the OTHER four coins have moved in the bet's direction over 5, 15 and 60 minutes (each in its own typical moves), and how much this coin's own move is ahead of or behind theirs (a coin that lags the rest is the kind of thing spot checks across charts show). Two targets, each its own ridge fit: the scalp net (buy the ask, resting sell +0.10 for 180 s, else the bid, both taker fees) and the bet-to-win net (buy the ask, hold to the result, entry fee). No coin enters a reading.
Reported per target, on the test days only: every candidate; the best 1 and best 2 per time slice by predicted net; and the candidates whose predicted net is above zero. Net per contract, ROI on the price paid, and the first against the second half of the test days.

    scripts/hostrun3.sh-style: docker compose exec -T bot python - < scripts/cross_section_walkforward.py
"""

"""Waves in every crypto market we watch: where the contract price swung 10c or more from a low we could have bought, and the conditions around each one.

Question (2026-11-07, user): replay every crypto 15-minute market; find the lows (the side's ask) from which the side's bid rose at least 10c before 20 s to the close, however low the price; and describe the conditions around every wave: the spot's history at 1, 5, 15 and 60 minutes, the distance from the strike in typical minutes (z), the path's efficiency, the minute, the price.
Only the lowest-priced side counts (the side whose mid is under 0.50 at the low: its low is the other side's peak). Detector (the same for every market, no coin in it): per side, a zigzag over the quotes. A wave low is the lowest ask since the last wave ended; the wave is confirmed when the bid reaches that ask + 0.10; it runs to the bid's high and ends when the bid falls 0.10 from it. A low needs a quote at least 120 s before the close, from 30 s after the open, ask >= 0.02.
Part 1 is the census: how many markets and sides have a wave, how big, how long, at what price. Part 2 compares every moment (every 30 s, both sides, minute 1 to 13) that was followed by a wave within 180 s against all moments: for each condition bin, the share of moments followed by a 10c wave (the lift over the base share), in two halves of the days. Conditions are signed to the bet's side (the cushion is the distance from the strike on the side bought) and in the market's own typical minutes.

    scripts/hostrun3.sh-style: docker compose exec -T bot python - < scripts/wave_conditions_study.py
"""

import bisect
import calendar
import collections
import itertools
import math
import os
import statistics

from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

os.nice(15)
MOVE = 0.10
FWD = 180.0
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
t0 = calendar.timegm((2026, 11, 2, 6, 0, 0))
CRYPTO = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M")
wins = st.db.execute("SELECT ticker,series,close_ts,strike,result FROM windows WHERE close_ts>? AND series IN (?,?,?,?,?) AND result IN ('yes','no') AND strike>0 ORDER BY series,close_ts", (t0, *CRYPTO)).fetchall()
mid_ts = sorted(float(w["close_ts"]) for w in wins)[len(wins) // 2]
G0 = int(t0 - 4300)
G1 = int(max(float(w["close_ts"]) for w in wins)) + 2
LOOKS = {"1m": 60, "5m": 300, "15m": 900, "60m": 3600}
waves = []  # census rows
moments = []  # (half, label, features dict)
census = collections.Counter()


def grid(ix):
    r = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (ix, G0 - 60)).fetchall()
    ts = [float(x[0]) for x in r]
    vs = [float(x[1]) for x in r]
    g, j, last = [], 0, None
    for t in range(G0, G1):
        while j < len(ts) and ts[j] <= t:
            last = vs[j]
            j += 1
        g.append(last)
    sq, ab = [0.0], [0.0]
    for a, b in itertools.pairwise(g):
        d = math.log(b / a) if a and b else 0.0
        sq.append(sq[-1] + d * d)
        ab.append(ab[-1] + abs(d))
    return g, sq, ab


def zigzag(ts, ask, bid, end_i):
    """Wave lows and highs of one side: [(low_i, high_i)]."""
    out, n = [], len(ts)
    i = 0
    while i < end_i:
        m = i
        j = i
        found = None
        while j < end_i:
            if ask[j] > 0 and (ask[m] <= 0 or ask[j] < ask[m]):
                m = j
            if ask[m] >= 0.02 and ts[j] >= ts[m] + 3.0 and bid[j] >= ask[m] + MOVE - 1e-9:
                found = j
                break
            j += 1
        if found is None:
            break
        hi = found
        k = found
        while k < end_i and bid[k] > bid[hi] - MOVE + 1e-9:
            if bid[k] > bid[hi]:
                hi = k
            k += 1
        out.append((m, hi))
        i = max(k, hi + 1)
    return out


GR = {x_: grid(variants.crypto_index(x_)) for x_ in CRYPTO}
for ser in CRYPTO:
    g, sq, ab = GR[ser]
    for w in (x for x in wins if x["series"] == ser):
        close, K = float(w["close_ts"]), float(w["strike"])
        opened = close - 900.0
        q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (w["ticker"], opened, close)).fetchall()
        if len(q) < 30:
            continue
        ts = [float(x[0]) for x in q]
        half = 0 if close <= mid_ts else 1
        end_i = bisect.bisect_right(ts, close - 120.0)
        end_all = bisect.bisect_right(ts, close - 20.0)
        census["markets"] += 1
        market_waves = 0
        for side in ("yes", "no"):
            sg = 1 if side == "yes" else -1
            ask = [float(x[2]) if side == "yes" else 1.0 - float(x[1]) for x in q]
            bid = [float(x[1]) if side == "yes" else 1.0 - float(x[2]) for x in q]
            census["sides"] += 1
            zz = [(m, h) for m, h in zigzag(ts, ask, bid, end_i) if ts[h] <= close - 20.0 and ts[m] >= opened + 30.0 and ask[m] + bid[m] < 1.0]  # the lowest-priced side at the low: its mid is under 0.50
            market_waves += len(zz)
            census["sides_with_wave"] += 1 if zz else 0
            for m, h in zz:
                waves.append({"half": half, "ask": ask[m], "amp": bid[h] - ask[m], "rise_s": ts[h] - ts[m], "left": close - ts[m], "cheaper": ask[m] < 0.5, "n_in_side": len(zz)})
            lows = {m for m, _ in zz}
            # every moment: the conditions, and whether a wave low followed within FWD
            for e in range(60, 781, 30):
                t = opened + e
                i = bisect.bisect_right(ts, t) - 1
                if i < 0 or t - ts[i] > 20 or ask[i] < 0.02 or ask[i] + bid[i] >= 1.0:  # only the lowest-priced side is a candidate
                    continue
                k = int(t) - G0
                if g[k] is None or k - 3600 < 0 or g[k - 3600] is None:
                    continue
                left = close - t
                c = math.log(g[k] / K) * sg
                rate15 = math.sqrt((sq[k] - sq[k - 900]) / 900.0)
                if rate15 <= 0:
                    continue
                unit = rate15 * math.sqrt(60.0)
                f = {"z_tm": c / unit, "left_min": left / 60.0, "ask": ask[i], "spread": ask[i] - bid[i]}
                ok = True
                for name, L in LOOKS.items():
                    rate = math.sqrt((sq[k] - sq[k - L]) / L)
                    if rate <= 0 or g[k - L] is None:
                        ok = False
                        break
                    f["slope_" + name] = (c - math.log(g[k - L] / K) * sg) / (rate * math.sqrt(L))
                if not ok:
                    continue
                path5 = ab[k] - ab[k - 300]
                f["eff5"] = abs(math.log(g[k] / g[k - 300])) / path5 if path5 > 0 else 0.0
                f["amp15_tm"] = (max(x for x in g[k - 900 : k + 1] if x) / min(x for x in g[k - 900 : k + 1] if x) - 1.0) and math.log(max(x for x in g[k - 900 : k + 1] if x) / min(x for x in g[k - 900 : k + 1] if x)) / unit
                j0 = bisect.bisect_left(ts, t - 300.0)
                steps = [ask[a + 1] - ask[a] for a in range(j0, i) if a + 1 <= i]
                j120 = bisect.bisect_left(ts, t - 120.0)
                j60 = bisect.bisect_left(ts, t - 60.0)
                seg_hi = max(ask[j120 : i + 1])
                seg_lo = min(ask[j60 : i + 1])
                f["drop"] = seg_hi - seg_lo  # the dip: from the high of the last 120 s to the low of the last 60 s
                f["bounce"] = ask[i] - seg_lo  # how far the ask has already come back up off that low
                hs = [x for x in g[k - 3600 : k + 1 : 10] if x]
                lo_h, hi_h = min(hs), max(hs)
                pos_h = (g[k] - lo_h) / (hi_h - lo_h) if hi_h > lo_h else 0.5
                strike_h = (K - lo_h) / (hi_h - lo_h) if hi_h > lo_h else 0.5
                f["h_pos"] = pos_h if side == "yes" else 1.0 - pos_h  # where the spot sits in the hour's range, from the side bought
                f["h_strike"] = strike_h if side == "yes" else 1.0 - strike_h
                prior_lows = [ask[m_] for m_, h_ in zz if ts[h_] < t]
                f["near_prior_low"] = 1.0 if prior_lows and abs(ask[i] - min(prior_lows)) <= 0.02 else 0.0
                f["above_prior_low"] = (ask[i] - min(prior_lows)) if prior_lows else 0.3
                seg5 = ask[bisect.bisect_left(ts, t - 300.0) : i + 1]
                f["swings5"] = float(sum(1 for a_, b_, c_ in zip(seg5, seg5[1:], seg5[2:]) if (b_ - a_) * (c_ - b_) < 0))
                f["day"] = float(int(close // 86400))
                f["slice"] = (close, e)
                won_ = w["result"] == side
                f["pnl_hold"] = ((1.0 - ask[i]) if won_ else -ask[i]) - fee(ask[i])
                for L_, nm_ in ((300, "5"), (900, "15"), (3600, "60")):
                    own_r = math.sqrt((sq[k] - sq[k - L_]) / L_)
                    own = math.log(g[k] / g[k - L_]) * sg / (own_r * math.sqrt(L_)) if own_r > 0 else 0.0
                    others = []
                    for oser in CRYPTO:
                        if oser == ser:
                            continue
                        go_, sqo_, _a = GR[oser]
                        ro_ = math.sqrt((sqo_[k] - sqo_[k - L_]) / L_)
                        if ro_ > 0 and go_[k] and go_[k - L_]:
                            others.append(math.log(go_[k] / go_[k - L_]) * sg / (ro_ * math.sqrt(L_)))
                    cm_ = sum(others) / len(others) if others else 0.0
                    f["cross_" + nm_] = cm_
                    f["idio_" + nm_] = own - cm_
                f["contract_vol"] = (sum(x * x for x in steps) / len(steps)) ** 0.5 if len(steps) > 5 else 0.0
                f["waves_so_far"] = sum(1 for m, h in zz if ts[h] < t)
                j1 = bisect.bisect_right(ts, t + FWD)
                hit = any(ts[j] >= ts[i] + 3.0 and ts[j] <= close - 20.0 and bid[j] >= ask[i] + MOVE - 1e-9 for j in range(i + 1, min(j1, end_all + 1)))
                # the scalp as a policy: buy at this ask (taker fee), a resting sell at +0.10 (no fee) for FWD seconds, else sell at the bid then (taker fee)
                last = min(j1, end_all + 1) - 1
                if hit:
                    f["pnl"] = MOVE - fee(ask[i])
                else:
                    f["pnl"] = bid[last] - ask[i] - fee(ask[i]) - (fee(bid[last]) if bid[last] > 0 else 0.0)
                moments.append((half, hit, f))
        census["markets_with_wave"] += 1 if market_waves else 0
        census["waves"] += market_waves



FEATS = ["z_tm", "slope_1m", "slope_5m", "slope_15m", "slope_60m", "eff5", "amp15_tm", "left_min", "ask", "spread", "contract_vol", "waves_so_far", "drop", "bounce", "h_pos", "h_strike", "near_prior_low", "above_prior_low", "swings5", "cross_5", "cross_15", "cross_60", "idio_5", "idio_15", "idio_60"]
rows = [f for _hf, _h, f in moments]
mu = {n_: sum(f[n_] for f in rows) / len(rows) for n_ in FEATS}
sd = {n_: (sum((f[n_] - mu[n_]) ** 2 for f in rows) / len(rows)) ** 0.5 or 1.0 for n_ in FEATS}
X = [[1.0] + [(f[n_] - mu[n_]) / sd[n_] for n_ in FEATS] for f in rows]
D = [int(f["day"]) for f in rows]
days = sorted(set(D))


def ridge(Xs, Ys, lam=50.0):
    k = len(Xs[0])
    A = [[0.0] * k for _ in range(k)]
    b = [0.0] * k
    for x, y in zip(Xs, Ys):
        for i_ in range(k):
            b[i_] += x[i_] * y
            xi = x[i_]
            Ai = A[i_]
            for j_ in range(i_, k):
                Ai[j_] += xi * x[j_]
    for i_ in range(k):
        for j_ in range(i_):
            A[i_][j_] = A[j_][i_]
        if i_:
            A[i_][i_] += lam
    M = [A[i_][:] + [b[i_]] for i_ in range(k)]
    for c_ in range(k):
        p_ = max(range(c_, k), key=lambda r_: abs(M[r_][c_]))
        M[c_], M[p_] = M[p_], M[c_]
        pv = M[c_][c_] or 1e-9
        M[c_] = [v / pv for v in M[c_]]
        for r_ in range(k):
            if r_ != c_ and M[r_][c_]:
                fac = M[r_][c_]
                M[r_] = [v - fac * w_ for v, w_ in zip(M[r_], M[c_])]
    return [M[i_][k] for i_ in range(k)]


print("moments", len(rows), "days", days)
for target, key in (("scalp (+0.10 resting sell, 180 s, else the bid)", "pnl"), ("bet to win (hold to the result)", "pnl_hold")):
    Y = [f[key] for f in rows]
    picks = {"every candidate": [], "best 1 per slice": [], "best 2 per slice": [], "predicted net > 0": [], "predicted net > 0.02": []}
    for di, d in enumerate(days):
        if di < 3:
            continue
        tr = [i_ for i_ in range(len(rows)) if D[i_] < d]
        te = [i_ for i_ in range(len(rows)) if D[i_] == d]
        if len(tr) < 3000 or not te:
            continue
        w_ = ridge([X[i_] for i_ in tr], [Y[i_] for i_ in tr])
        pred = {i_: sum(a * b for a, b in zip(w_, X[i_])) for i_ in te}
        by_slice = {}
        for i_ in te:
            by_slice.setdefault(rows[i_]["slice"], []).append(i_)
        for i_ in te:
            picks["every candidate"].append((d, Y[i_], rows[i_]["ask"]))
            if pred[i_] > 0:
                picks["predicted net > 0"].append((d, Y[i_], rows[i_]["ask"]))
            if pred[i_] > 0.02:
                picks["predicted net > 0.02"].append((d, Y[i_], rows[i_]["ask"]))
        for _sl, idx in by_slice.items():
            idx.sort(key=lambda i_: pred[i_], reverse=True)
            for rank, i_ in enumerate(idx[:2]):
                if rank == 0:
                    picks["best 1 per slice"].append((d, Y[i_], rows[i_]["ask"]))
                picks["best 2 per slice"].append((d, Y[i_], rows[i_]["ask"]))
    print("\n" + target)
    for name, xs in picks.items():
        if not xs:
            print(f"  {name:22s} none")
            continue
        half = sorted(d for d, _, _ in xs)[len(xs) // 2]
        a = [(p, c) for d, p, c in xs if d < half]
        b = [(p, c) for d, p, c in xs if d >= half]
        r = lambda ys: (sum(p for p, _c in ys) / max(len(ys), 1), 100 * sum(p for p, _c in ys) / max(sum(c for _p, c in ys), 1e-9))
        print(f"  {name:22s} n {len(xs):6d} net {r([(p, c) for _d, p, c in xs])[0]:+.4f} ROI {r([(p, c) for _d, p, c in xs])[1]:+5.1f}% | earlier test days {r(a)[0]:+.4f} (n={len(a)}) / later {r(b)[0]:+.4f} (n={len(b)})")
print("DONE")
