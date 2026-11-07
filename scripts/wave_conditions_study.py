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


for ser in CRYPTO:
    ix = variants.crypto_index(ser)
    if not ix:
        continue
    g, sq, ab = grid(ix)
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

n = census["markets"]
print("markets", n, "sides", census["sides"], "| markets with a 10c wave on either side:", census["markets_with_wave"], f"({100 * census['markets_with_wave'] / n:.0f}%)", "| sides with a wave:", census["sides_with_wave"], f"({100 * census['sides_with_wave'] / census['sides']:.0f}%)", "| waves per market", round(census["waves"] / n, 2))
if waves:
    q = lambda xs, p: sorted(xs)[int(p * (len(xs) - 1))]
    print("wave amplitude (bid high over the ask at the low): median", round(statistics.median(w["amp"] for w in waves), 2), "p25", round(q([w["amp"] for w in waves], 0.25), 2), "p75", round(q([w["amp"] for w in waves], 0.75), 2))
    print("ask at the low: p10", round(q([w["ask"] for w in waves], 0.10), 2), "median", round(statistics.median(w["ask"] for w in waves), 2), "p90", round(q([w["ask"] for w in waves], 0.90), 2), "| the cheaper side at the low:", f"{100 * sum(w['cheaper'] for w in waves) / len(waves):.0f}%")
    print("seconds from the low to the high: median", round(statistics.median(w["rise_s"] for w in waves)), "| seconds left at the low: median", round(statistics.median(w["left"] for w in waves)))
base = sum(1 for _, h, _ in moments if h) / len(moments)
print(f"\nmoments {len(moments)}; a 10c wave low followed within {FWD:.0f}s in {100 * base:.1f}% (the base share)")
BINS = {
    "z_tm": [-2.0, -1.0, 0.0, 1.0, 2.0],
    "slope_1m": [-1.0, -0.3, 0.3, 1.0],
    "slope_5m": [-1.0, -0.5, 0.5, 1.0],
    "slope_15m": [-1.0, -0.5, 0.5, 1.0],
    "slope_60m": [-1.0, -0.5, 0.5, 1.0],
    "eff5": [0.05, 0.10, 0.20, 0.40],
    "left_min": [3.0, 6.0, 9.0, 12.0],
    "ask": [0.10, 0.20, 0.35, 0.50, 0.70],
    "contract_vol": [0.005, 0.01, 0.02, 0.04],
    "waves_so_far": [0.5, 1.5, 2.5],
    "amp15_tm": [2.0, 4.0, 6.0, 9.0],
    "spread": [0.011, 0.021, 0.041],
    "drop": [0.03, 0.06, 0.10, 0.15],
    "bounce": [0.005, 0.015, 0.03, 0.05],
}
print("\nlift = share of moments in the bin followed by a 10c wave / the base share; halves are the lift in the first and second half of the days")
print(f"{'condition':14s} {'bin':18s} {'n':>7s} {'share':>6s} {'lift':>5s}   half1 lift (n) / half2 lift (n)")
for name, cuts in BINS.items():
    edges = [-1e9, *cuts, 1e9]
    for lo, hi in itertools.pairwise(edges):
        rs = [(hf, h) for hf, h, f in moments if lo <= f[name] < hi]
        if len(rs) < 300:
            continue
        sh = sum(1 for _, h in rs if h) / len(rs)
        parts = []
        for hf in (0, 1):
            sub = [h for x, h in rs if x == hf]
            bsub = [h for x, h, _ in moments if x == hf]
            b = sum(bsub) / len(bsub)
            parts.append(f"{(sum(sub) / len(sub)) / b:4.2f} ({len(sub)})" if sub and b else "n/a")
        label = f"[{lo:g}, {hi:g})".replace("-1000000000", "-inf").replace("1000000000", "inf")
        print(f"{name:14s} {label:18s} {len(rs):7d} {100 * sh:5.1f}% {sh / base:5.2f}   {parts[0]} / {parts[1]}")

print("\nthe scalp as a policy (buy the lowest-priced side at this ask, resting sell at +0.10, else sell at the bid after 180 s or 20 s before the close; both taker fees counted): net per contract and per dollar staked")
print(f"{'condition':14s} {'bin':18s} {'n':>7s} {'hit%':>5s} {'net/contract':>12s} {'ROI':>7s}   half1 net (n) / half2 net (n)")
allm = [(hf, f) for hf, _h, f in moments]
print(f"{'every moment':14s} {'':18s} {len(allm):7d} {100 * base:4.0f}% {sum(f['pnl'] for _, f in allm) / len(allm):+12.4f} {100 * sum(f['pnl'] for _, f in allm) / sum(f['ask'] for _, f in allm):+6.1f}%")
for name, cuts in BINS.items():
    edges = [-1e9, *cuts, 1e9]
    for lo, hi in itertools.pairwise(edges):
        rs = [(hf, h, f) for hf, h, f in moments if lo <= f[name] < hi]
        if len(rs) < 300:
            continue
        a = [f["pnl"] for hf, _h, f in rs if hf == 0]
        b = [f["pnl"] for hf, _h, f in rs if hf == 1]
        label = f"[{lo:g}, {hi:g})".replace("-1000000000", "-inf").replace("1000000000", "inf")
        print(f"{name:14s} {label:18s} {len(rs):7d} {100 * sum(1 for _, h, _ in rs if h) / len(rs):4.0f}% {sum(f['pnl'] for _, _, f in rs) / len(rs):+12.4f} {100 * sum(f['pnl'] for _, _, f in rs) / sum(f['ask'] for _, _, f in rs):+6.1f}%   {sum(a) / max(len(a), 1):+.4f} ({len(a)}) / {sum(b) / max(len(b), 1):+.4f} ({len(b)})")
print("\ncombinations chosen after reading the lifts above (so they are in-sample until a later day confirms them):")
COMBOS = {
    "z>=-1.5, eff5<0.10": lambda f: f["z_tm"] >= -1.5 and f["eff5"] < 0.10,
    "z>=-1.5, eff5<0.10, ask 0.25-0.50": lambda f: f["z_tm"] >= -1.5 and f["eff5"] < 0.10 and 0.25 <= f["ask"] < 0.50,
    "z>=-1.5, eff5<0.10, ask 0.25-0.50, contract_vol>=0.01": lambda f: f["z_tm"] >= -1.5 and f["eff5"] < 0.10 and 0.25 <= f["ask"] < 0.50 and f["contract_vol"] >= 0.01,
    "z>=-1.5, eff5<0.10, ask 0.25-0.50, amp15<6": lambda f: f["z_tm"] >= -1.5 and f["eff5"] < 0.10 and 0.25 <= f["ask"] < 0.50 and f["amp15_tm"] < 6,
}
for name, fn in COMBOS.items():
    rs = [(hf, h, f) for hf, h, f in moments if fn(f)]
    if not rs:
        continue
    a = [f["pnl"] for hf, _h, f in rs if hf == 0]
    b = [f["pnl"] for hf, _h, f in rs if hf == 1]
    print(f"{name:56s} n {len(rs):6d} hit {100 * sum(1 for _, h, _ in rs if h) / len(rs):3.0f}% net/contract {sum(f['pnl'] for _, _, f in rs) / len(rs):+.4f} ROI {100 * sum(f['pnl'] for _, _, f in rs) / sum(f['ask'] for _, _, f in rs):+5.1f}% | halves {sum(a) / max(len(a), 1):+.4f} ({len(a)}) / {sum(b) / max(len(b), 1):+.4f} ({len(b)})")

print("\nbuying the turn (a dip of at least D from the 120 s high to the 60 s low, and the ask already b above that low); the same policy, net per contract and ROI, in two halves. D and b are a grid, not a choice made after the fact:")
print(f"{'dip D':>6s} {'bounce b':>9s} {'n':>7s} {'hit%':>5s} {'net/contract':>12s} {'ROI':>7s}   half1 net (n) / half2 net (n)")
for D in (0.0, 0.05, 0.10, 0.15):
    for b in (0.0, 0.01, 0.02, 0.04):
        rs = [(hf, h, f) for hf, h, f in moments if f["drop"] >= D - 1e-9 and f["bounce"] >= b - 1e-9]
        if len(rs) < 200:
            continue
        a = [f["pnl"] for hf, _h, f in rs if hf == 0]
        c2 = [f["pnl"] for hf, _h, f in rs if hf == 1]
        print(f"{D:6.2f} {b:9.2f} {len(rs):7d} {100 * sum(1 for _, h, _ in rs if h) / len(rs):4.0f}% {sum(f['pnl'] for _, _, f in rs) / len(rs):+12.4f} {100 * sum(f['pnl'] for _, _, f in rs) / sum(f['ask'] for _, _, f in rs):+6.1f}%   {sum(a) / max(len(a), 1):+.4f} ({len(a)}) / {sum(c2) / max(len(c2), 1):+.4f} ({len(c2)})")
print("DONE")
