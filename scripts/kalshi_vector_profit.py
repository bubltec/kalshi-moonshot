"""Which price-curve shapes line up with profit, scalp or win? Kalshi's own 1-minute bid and ask candles only. Read-only analysis of pulled files. Places nothing.

Per coin, for each finished market and each side (YES and NO, each on its own path), at the end of minute m (5 to 11) the side is a buy candidate if its ask is 0.55-0.95. Its class:
 price level (0.55-0.70, 0.70-0.85, 0.85-0.95) x which third of its own mid-price range the latest mid is in x how long ago the high was (now, 1-2 minutes, 3 or more) x whether the last 3 minutes rose or fell.
Two ways of being paid, both bought at the ask (YES ask, or 1 - the YES bid for NO), each reported fee-free and with the buy fee:
 win    hold to the result.
 scalp  rest a sell `target` above the price paid (no fee); filled if a later minute's highest bid reaches it, within 3 minutes; otherwise hold to the result.
Walk-forward: classes are chosen on the earlier 60% of the markets (return above a bar, 300+ entries), then scored on the later 40%. Standard errors across markets.
    python scripts/kalshi_vector_profit.py KALSHI_DIR [TARGET]
"""
import glob
import json
import math
import os
import statistics
import sys
from collections import defaultdict

from kalshi_moonshot.strategy import fee_per_contract as fee

KD = sys.argv[1]
TARGET = float(sys.argv[2]) if len(sys.argv) > 2 else 0.03
HOLD = 3


def f(x):
    return float(x) if x not in (None, "") else None


def entries(m):
    """Yield (minute, side, class, ask, win, scalp_hit, scalp_ret_gross_price) for one market."""
    by_t = {c["t"]: c for c in m["candles"]}
    o = m["open_ts"]
    res_yes = 1.0 if m["result"] == "yes" else 0.0
    for side in ("yes", "no"):
        mids, asks, bids_h = {}, {}, {}
        for k in range(1, 15):
            c = by_t.get(o + 60 * k)
            if not c:
                continue
            bc, ac, bh, bl = f(c["bc"]), f(c["ac"]), f(c["bh"]), f(c["bl"])
            if bc is None or ac is None or not (0 < bc < ac < 1) or ac - bc > 0.08:
                continue
            if side == "yes":
                mids[k], asks[k], bids_h[k] = (bc + ac) / 2, ac, bh
            else:
                al = f(c["al"])
                mids[k], asks[k], bids_h[k] = 1 - (bc + ac) / 2, 1 - bc, (1 - al) if al is not None else None
        for mnt in range(5, 12):
            path = [mids.get(k) for k in range(1, mnt + 1)]
            if None in path or mnt not in asks:
                continue
            a = asks[mnt]
            if not (0.55 <= a < 0.95):
                continue
            hi, lo, last = max(path), min(path), path[-1]
            pos = (last - lo) / (hi - lo) if hi > lo else 0.5
            since = mnt - 1 - path.index(hi)
            level = 0 if a < 0.70 else 1 if a < 0.85 else 2
            cls = level * 18 + min(int(pos * 3), 2) * 6 + (0 if since == 0 else 1 if since <= 2 else 2) * 2 + int(last > path[-4])
            win = res_yes if side == "yes" else 1 - res_yes
            hit = False
            for k in range(mnt + 1, min(mnt + HOLD, 14) + 1):
                bh = bids_h.get(k)
                if bh is not None and bh >= a + TARGET - 1e-9:
                    hit = True
                    break
            yield mnt, side, cls, a, win, hit


def describe(cls):
    level, rest = divmod(cls, 18)
    third, rest = divmod(rest, 6)
    since, rising = divmod(rest, 2)
    return f"{['0.55-0.70', '0.70-0.85', '0.85-0.95'][level]}, {['low', 'mid', 'top'][third]} third, high {['now', '1-2m ago', '3m+ ago'][since]}, {'rising' if rising else 'falling'}"


def ret(r, kind, fee_on):
    a, win, hit = r["ask"], r["win"], r["hit"]
    cost = a + (fee(a) if fee_on else 0.0)
    if kind == "win":
        return (win - cost) / cost
    return ((a + TARGET) - cost) / cost if hit else (win - cost) / cost


def se(rows, kind, fee_on):
    per = defaultdict(list)
    for r in rows:
        per[r["tk"]].append(ret(r, kind, fee_on))
    mm = [sum(v) / len(v) for v in per.values()]
    return 100 * math.sqrt(sum((x - sum(mm) / len(mm)) ** 2 for x in mm) / (len(mm) - 1) / len(mm)) if len(mm) > 2 else float("nan")


for path in sorted(glob.glob(os.path.join(KD, "*.jsonl"))):
    series = os.path.basename(path)[:-6]
    rows = []
    for line in open(path):
        m = json.loads(line)
        if len(m["candles"]) < 10:
            continue
        for mnt, side, cls, a, win, hit in entries(m):
            rows.append({"tk": m["ticker"], "t": m["close_ts"], "m": mnt, "side": side, "cls": cls, "ask": a, "win": win, "hit": hit})
    if len(rows) < 2000:
        print(f"\n{series}: only {len(rows)} entries so far")
        continue
    cut = sorted({r["t"] for r in rows})[int(len({r["t"] for r in rows}) * 0.6)]
    train, test = [r for r in rows if r["t"] <= cut], [r for r in rows if r["t"] > cut]
    print(f"\n{series}: {len(rows):,} entries from {len({r['tk'] for r in rows})} markets; all entries: win {statistics.mean(r['win'] for r in rows):.1%} at {100 * statistics.mean(r['ask'] for r in rows):.1f}c; scalp +{int(TARGET * 100)}c hit {statistics.mean(r['hit'] for r in rows):.0%}")
    for kind, name in (("win", "hold to the result"), ("scalp", f"scalp +{int(TARGET * 100)}c, else hold")):
        print(f"  {name}: all entries fee-free {statistics.mean(ret(r, kind, False) for r in rows):+.2%}, with fee {statistics.mean(ret(r, kind, True) for r in rows):+.2%}")
        stats = defaultdict(list)
        for r in train:
            stats[r["cls"]].append(ret(r, kind, True))
        picked = [c for c, v in stats.items() if len(v) >= 300 and statistics.mean(v) > 0.01]
        best = sorted(stats.items(), key=lambda kv: -statistics.mean(kv[1]))[:3]
        print("    best classes in the earlier 60% (with fee): " + "; ".join(f"[{describe(c)}] {statistics.mean(v):+.1%} n={len(v)}" for c, v in best if len(v) >= 100))
        if picked:
            g = [r for r in test if r["cls"] in picked]
            print(f"    classes picked on the earlier markets ({len(picked)}), scored on the later 40%: {len(g)} entries, {len({r['tk'] for r in g})} markets, return with fee {statistics.mean(ret(r, kind, True) for r in g):+.2%} +-{se(g, kind, True):.2f}, fee-free {statistics.mean(ret(r, kind, False) for r in g):+.2%}   (all later entries: {statistics.mean(ret(r, kind, True) for r in test):+.2%})")
        else:
            print("    no class cleared +1% with 300+ entries in the earlier markets")
