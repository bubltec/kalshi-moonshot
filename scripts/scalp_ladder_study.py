"""Automated scalping: resting buys under the market, resting sells over the price paid, on every 15-minute market.

Question (2026-11-07, user: "almost every single market has a position you can buy in and scalp for a good profit (20-50%)" and "the automation is where we should be killing it"):
A. Is the claim true in hindsight? For every settled crypto and commodity 15-minute window since 11-02 and both sides: is there a moment whose ask, bought, is followed by a bid at least 20% / 50% higher before 20 s to the close (the buy pays the taker fee, the resting sell none)? How many of the market sides, at what price, how many seconds later?
B. Can a machine that rests orders in every market capture it without seeing the future? Per market side: a resting buy at a fixed price level placed 60 s after the open and cancelled 120 s before the close; it fills when the side's ask trades through the level (ask <= level - 0.01, the strict reading; ask <= level is the touch reading); a resting sell at level * (1 + gain) placed on the fill, filled when the side's bid reaches it before 20 s to the close; an unsold position is held to the result ("hold") or sold at the bid 90 s before the close ("cut"). Resting orders pay no fee. Reported per level and gain: fills, share of fills that sold at the target, net per filled contract and per dollar staked, per market side offered (an unfilled order is zero), the two halves, and the class as a label.
Not covered: queue position and the market moving against a filled order faster than the quote shows; a resting order fills at the quote the tape shows only if someone trades against it.

    scripts/hostrun3.sh-style: docker compose exec -T bot python - < scripts/scalp_ladder_study.py
"""

import bisect
import calendar
import collections
import os
import statistics

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

os.nice(15)
LEVELS = (0.08, 0.12, 0.16, 0.20, 0.25, 0.30, 0.40)
GAINS = (0.20, 0.30, 0.50)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
t0 = calendar.timegm((2026, 11, 2, 6, 0, 0))
wins = st.db.execute("SELECT ticker,series,close_ts,result FROM windows WHERE close_ts>? AND result IN ('yes','no') ORDER BY close_ts", (t0,)).fetchall()
mid = wins[len(wins) // 2]["close_ts"]
CRYPTO = ("KXBTC", "KXETH", "KXSOL", "KXXRP", "KXDOGE")
oracle = collections.defaultdict(list)  # class -> [(best gain, entry price, seconds to peak)]
sides_seen = collections.Counter()
res = collections.defaultdict(lambda: [0, 0, 0, 0.0, 0.0, 0.0, 0.0])  # key -> fills, sold, offered, net, staked, net first half, net second half
fills_half = collections.defaultdict(lambda: [0, 0])


def cls(series):
    return "crypto" if series.startswith(CRYPTO) else "commodity"


for w in wins:
    close = float(w["close_ts"])
    opened = close - 900.0
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid IS NOT NULL AND ask IS NOT NULL ORDER BY ts", (w["ticker"], opened, close)).fetchall()
    if len(q) < 30:
        continue
    ts = [float(x[0]) for x in q]
    c = cls(w["series"])
    for side in ("yes", "no"):
        ask = [float(x[2]) if side == "yes" else 1.0 - float(x[1]) for x in q]
        bid = [float(x[1]) if side == "yes" else 1.0 - float(x[2]) for x in q]
        won = w["result"] == side
        sides_seen[c] += 1
        n = len(q)
        # A. the oracle: the best buy at an ask in 0.05-0.70, spread <= 0.04, from minute 1, and the best bid after it (3 s later) until 20 s to the close
        end = bisect.bisect_right(ts, close - 20.0)
        suf = [0.0] * (n + 1)
        sufi = [0] * (n + 1)
        for i in range(n - 1, -1, -1):
            if i < end and bid[i] > suf[i + 1]:
                suf[i], sufi[i] = bid[i], i
            else:
                suf[i], sufi[i] = suf[i + 1], sufi[i + 1]
        best = (0.0, 0.0, 0.0)
        for i in range(n):
            if ts[i] < opened + 60 or ts[i] > close - 120 or not (0.05 <= ask[i] <= 0.70) or ask[i] - bid[i] > 0.04 or ask[i] <= 0:
                continue
            j = bisect.bisect_right(ts, ts[i] + 3.0)
            mx = suf[j] if j <= n else 0.0
            g = (mx - ask[i] - fee(ask[i])) / ask[i]
            if g > best[0]:
                best = (g, ask[i], ts[sufi[j]] - ts[i])
        oracle[c].append(best)
        # B. the resting ladder
        a0 = bisect.bisect_left(ts, opened + 60.0)
        a1 = bisect.bisect_right(ts, close - 120.0)
        negmin, run = [], 9.0
        for i in range(a0, a1):
            run = min(run, ask[i] if ask[i] > 0 else 9.0)
            negmin.append(-run)  # non-decreasing: the first index with ask <= thr is a bisect
        for level in LEVELS:
            for mode in ("strict", "touch"):
                thr = level - 0.01 if mode == "strict" else level
                k = bisect.bisect_left(negmin, -(thr + 1e-9))
                fi = a0 + k if k < len(negmin) else None
                for gain in GAINS:
                    key = (level, gain, mode)
                    r = res[key]
                    r[2] += 1
                    if fi is None:
                        continue
                    target = level * (1.0 + gain)
                    r[0] += 1
                    r[4] += level
                    sold = next((j for j in range(fi + 1, end) if ts[j] >= ts[fi] + 1.0 and bid[j] >= target - 1e-9), None)
                    if sold is not None:
                        r[1] += 1
                        net = target - level
                    else:
                        cut_i = bisect.bisect_right(ts, close - 90.0) - 1
                        # hold to the result and cut at the bid 90 s before the close are both kept: hold in net, cut in cutnet below
                        net = (1.0 - level) if won else -level
                        cutnet = (bid[cut_i] - level) if cut_i > fi else net
                        res[(level, gain, mode + "-cut")][3] += cutnet
                    r[3] += net
                    if sold is not None:
                        res[(level, gain, mode + "-cut")][3] += net
                    r[5 if close <= mid else 6] += net
                    res[(level, gain, mode + "-cut")][0] += 1
                    res[(level, gain, mode + "-cut")][4] += level
                    fills_half[key][0 if close <= mid else 1] += 1
print("windows", len(wins), "market sides", dict(sides_seen))
print("A. the oracle (best buy and later bid per market side, taker fee on the buy):")
for c, rows in oracle.items():
    n = len(rows)
    for th in (0.20, 0.50, 1.00):
        hit = [r for r in rows if r[0] >= th]
        print(f"  {c:9s} market sides with a +{th * 100:.0f}% scalp: {len(hit):5d} of {n} ({100 * len(hit) / n:.0f}%)", end="")
        if hit:
            print(f" | median entry {statistics.median(r[1] for r in hit):.2f}, median wait {statistics.median(r[2] for r in hit):.0f}s")
        else:
            print()
print("B. the resting ladder (per level and gain; hold = unsold positions run to the result, cut = sold at the bid 90 s before the close):")
print("   level gain fill     fills  filled  sold-at-target | hold: net/contract  ROI | per side offered ($ per contract slot) | halves(net) | cut: net/contract ROI")
for level in LEVELS:
    for gain in GAINS:
        for mode in ("strict", "touch"):
            r = res[(level, gain, mode)]
            cut = res[(level, gain, mode + "-cut")]
            if r[0] == 0:
                continue
            print(f"  {level:.2f} +{gain:.0%} {mode:6s} fills {r[0]:5d} of {r[2]} ({100 * r[0] / r[2]:3.0f}%) sold {100 * r[1] / r[0]:3.0f}% | hold {r[3] / r[0]:+.3f} ROI {100 * r[3] / r[4]:+5.1f}% | per offered {r[3] / r[2]:+.4f} | halves {r[5]:+.1f} ({fills_half[(level, gain, mode)][0]}) / {r[6]:+.1f} ({fills_half[(level, gain, mode)][1]}) | cut {cut[3] / max(cut[0], 1):+.3f} ROI {100 * cut[3] / max(cut[4], 1e-9):+5.1f}%")
print("DONE")
