"""Isolate the waves (2026-11-10, user: "we need to isolate these waves when we're looking at data, then compare to the larger picture and see what's missing").
Question: the book makes money in bursts and loses in bursts. What do the losing bursts have in common that the winning ones and the overall record do not, in curve terms (never by asset)?
Unit of a wave: the 15-minute window (every live bet that closes at the same minute, all lanes). A window's result is its net of the actual live outcomes (own sale where one happened) and its net if every bet had been held to the result. A wave is a run of consecutive windows whose 3-window rolling net has the same sign; windows with no bets are skipped.
Per bet, from its own index's ticks (no asset rule): the regime ratio at the buy (rms 1 s move over the last 180 s over the last 3600 s, 1 = a normal minute for that hour), the typical minute of the last 3600 s (u60) and the move of the index over the bet's life in units of it, the path efficiency of the index over the 5 minutes before the buy, the entry price, and how the bet ended: held / shock exit / late drop / value exit / other sale.
Output: (1) the window table, (2) the waves, (3) the wave and overall comparison: share of bets sold, by exit type the net against holding, whole-stake losses, mean entry price, mean regime ratio at the buy, mean spot efficiency, how many windows the book entered at once.
Environment: SINCE (epoch, default 10 days ago), UNTIL (default now).

    scripts/cloud_job.sh scripts/wave_review.py     # the 10 days up to the last nightly backup
    scripts/cloud.sh run scripts/wave_review.py SINCE=...   # today, on the live host (small)
"""

import bisect
import collections
import itertools
import math
import os
import time

from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee  # noqa: F401

os.nice(10)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
NOW = time.time()
SINCE = float(os.environ.get("SINCE", NOW - 10 * 86400))
UNTIL = float(os.environ.get("UNTIL", NOW))
LONG, SHORT = 3600, 180

rows = st.db.execute(
    "SELECT d.ts,d.ticker,d.lane,d.side,d.contracts,d.stake_usd,d.exit_price,d.exit_ts,d.exit_reason,COALESCE(d.outcome,w.result) AS res,w.close_ts "
    "FROM decisions d LEFT JOIN windows w ON w.ticker=d.ticker WHERE d.action='bet' AND d.mode='trade' AND d.contracts>0 AND d.stake_usd>0 AND d.ts>? AND d.ts<? ORDER BY d.ts",
    (SINCE, UNTIL),
).fetchall()
bets = {}
for r in rows:
    k = (r["ticker"], r["side"], r["lane"])
    b = bets.get(k)
    if b is None:
        bets[k] = dict(r) | {"stake": float(r["stake_usd"]), "n": float(r["contracts"])}
    else:
        b["stake"] += float(r["stake_usd"])
        b["n"] += float(r["contracts"])
        if r["exit_price"]:
            b["exit_price"], b["exit_reason"] = r["exit_price"], r["exit_reason"]
bets = [b for b in bets.values() if b["res"] in ("yes", "no") and b["close_ts"]]


def kind(b):
    if not b["exit_price"]:
        return "held"
    why = (b["exit_reason"] or "").lower()
    return "shock" if why.startswith("shock") else "late drop" if why.startswith("late drop") else "value" if why.startswith("value") else "other sale"


def features(b):
    ser = b["ticker"].split("-")[0]
    ix = variants.crypto_index(ser) or variants.biquote_index(ser)
    if not ix:
        return None
    t0, close = float(b["ts"]), float(b["close_ts"])
    tk = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts", (ix, t0 - LONG - 60, close)).fetchall()
    if len(tk) < 1500:
        return None
    T = [float(x[0]) for x in tk]
    V = [float(x[1]) for x in tk]
    base = int(T[0])
    g, j, last = [], 0, None
    for t in range(base, int(close) + 1):
        while j < len(T) and T[j] <= t:
            last = V[j]
            j += 1
        g.append(math.log(last) if last else None)
    sq = [0.0]
    for x, y in itertools.pairwise(g):
        sq.append(sq[-1] + ((y - x) ** 2 if x is not None and y is not None else 0.0))
    kb = int(t0) - base
    if kb < 1800 or kb >= len(g) or g[kb] is None or g[-1] is None:
        return None

    def unit(k, w):
        w = min(w, k)
        v = (sq[k] - sq[k - w]) / w
        return math.sqrt(v) * math.sqrt(60.0) if v > 0 else None

    ub, ul = unit(kb, SHORT), unit(kb, LONG)
    if not ub or not ul:
        return None
    seg = [x for x in g[max(kb - 300, 0) : kb + 1] if x is not None]
    path = sum(abs(y - x) for x, y in itertools.pairwise(seg))
    eff = abs(seg[-1] - seg[0]) / path if len(seg) > 30 and path > 0 else None
    sign = 1.0 if b["side"] == "yes" else -1.0
    move = (g[-1] - g[kb]) * sign / ul  # the index's move against (negative) or with (positive) the bet over its life, in typical minutes of that hour
    k60 = max(kb - 3600, 0)
    k15 = max(kb - 900, 0)
    tr60 = (g[kb] - g[k60]) * sign / ul if g[k60] is not None else None  # the hour before the buy, signed with the bet (positive = the bet goes with the hour's drift), in typical minutes
    tr15 = (g[kb] - g[k15]) * sign / ul if g[k15] is not None else None
    return ub / ul, eff, move, tr60, tr15


def pnl(b):
    won = (b["n"] if b["res"] == b["side"] else 0.0) - b["stake"]
    act = b["n"] * float(b["exit_price"]) - b["stake"] if b["exit_price"] else won
    return act, won


by_win = collections.defaultdict(list)
for b in bets:
    f = features(b)
    act, hold = pnl(b)
    by_win[int(float(b["close_ts"]))].append((b, kind(b), act, hold, f))
wins = sorted(by_win)
print(f"bets settled in range: {len(bets)} | windows with bets: {len(wins)}")
print("\nclose (UTC)  n  stake   actual   held  exits  zeros | ratio@buy  eff  move(typ min)")
tab = []
for c in wins:
    L = by_win[c]
    stake = sum(x[0]["stake"] for x in L)
    act, hold = sum(x[2] for x in L), sum(x[3] for x in L)
    fs = [x[4] for x in L if x[4]]
    rb = sum(f[0] for f in fs) / len(fs) if fs else float("nan")
    ef = [f[1] for f in fs if f[1] is not None]
    mv = sum(f[2] for f in fs) / len(fs) if fs else float("nan")
    tab.append((c, len(L), stake, act, hold, sum(1 for x in L if x[1] != "held"), sum(1 for x in L if x[2] <= -x[0]["stake"] + 1e-9), rb, sum(ef) / len(ef) if ef else float("nan"), mv))
    print(f"{time.strftime('%m-%d %H:%M', time.gmtime(c))}  {len(L):2d} {stake:6.1f} {act:+8.2f} {hold:+7.2f}  {tab[-1][5]:3d}  {tab[-1][6]:3d}   |  {rb:5.2f}  {tab[-1][8]:5.2f}  {mv:+5.2f}")

roll = [sum(t[3] for t in tab[max(0, i - 1) : i + 2]) for i in range(len(tab))]
waves, cur = [], [0]
for i in range(1, len(tab)):
    if (roll[i] >= 0) == (roll[cur[-1]] >= 0):
        cur.append(i)
    else:
        waves.append(cur)
        cur = [i]
waves.append(cur)
print("\nwaves (runs of the same sign of the 3-window rolling actual net):")
for w in waves:
    a, b_ = tab[w[0]][0], tab[w[-1]][0]
    print(f"  {time.strftime('%m-%d %H:%M', time.gmtime(a))} to {time.strftime('%H:%M', time.gmtime(b_))}: {len(w)} windows, {sum(tab[i][1] for i in w)} bets, actual {sum(tab[i][3] for i in w):+.2f}, held {sum(tab[i][4] for i in w):+.2f}")


def summarize(name, idx):
    L = [x for i in idx for x in by_win[wins[i]]]
    if not L:
        return
    n = len(L)
    stake = sum(x[0]["stake"] for x in L)
    sold = [x for x in L if x[1] != "held"]
    fs = [x[4] for x in L if x[4]]
    kinds = {}
    for x in sold:
        d = kinds.setdefault(x[1], [0, 0.0, 0, 0])
        d[0] += 1
        d[1] += x[2] - x[3]
        d[2] += 1 if x[2] > x[3] else 0
        d[3] += 1 if x[2] < x[3] else 0
    ks = " | ".join(f"{k} {v[0]} sold, {v[1]:+.2f} vs hold (helped {v[2]}, hurt {v[3]})" for k, v in sorted(kinds.items()))
    px = sum(x[0]["stake"] / x[0]["n"] for x in L) / n
    rb = sum(f[0] for f in fs) / len(fs) if fs else float("nan")
    ef = [f[1] for f in fs if f[1] is not None]
    mv = [f[2] for f in fs]
    per_win = n / max(len(idx), 1)
    print(f"\n{name}: {len(idx)} windows, {n} bets (avg {per_win:.1f} per window), ${stake:.0f} staked")
    print(f"  actual {sum(x[2] for x in L):+.2f} ({sum(x[2] for x in L) / stake:+.3f}/$), held {sum(x[3] for x in L):+.2f} | sold {len(sold)} of {n} | whole-stake losses {sum(1 for x in L if x[2] <= -x[0]['stake'] + 1e-9)}")
    print(f"  {ks}")
    print(f"  mean entry price {px:.2f} | ratio at buy {rb:.2f} | spot efficiency {sum(ef) / max(len(ef), 1):.2f} | spot move over the bet's life {sum(mv) / max(len(mv), 1):+.2f} typical minutes (negative = against the bet)")


neg = [i for w in waves if roll[w[0]] < 0 for i in w]
pos = [i for w in waves if roll[w[0]] >= 0 for i in w]
summarize("ALL windows", list(range(len(tab))))
summarize("LOSING waves", neg)
summarize("WINNING waves", pos)

# the larger picture: do the entries lean with or against the hour's drift, and does that explain the waves?
allb = [(x[2], x[3], x[0]["stake"], x[4]) for c in wins for x in by_win[c] if x[4] and x[4][3] is not None]
print("\nbets by the hour's drift at the buy, signed with the bet (terciles of this record; held-to-result and actual net per $ staked):")
tr = sorted(f[3] for _, _, _, f in allb)
lo, hi = tr[len(tr) // 3], tr[2 * len(tr) // 3]
for name, test in (("against the hour (low third)", lambda v: v <= lo), ("flat (middle)", lambda v: lo < v < hi), ("with the hour (high third)", lambda v: v >= hi)):
    g_ = [x for x in allb if test(x[3][3])]
    stake = sum(x[2] for x in g_)
    print(f"  {name} [{lo:+.2f} .. {hi:+.2f}]: n={len(g_)} actual {sum(x[0] for x in g_) / stake:+.3f}/$ held {sum(x[1] for x in g_) / stake:+.3f}/$")
print("same, by the 15 minutes before the buy:")
tr = sorted(f[4] for _, _, _, f in allb if f[4] is not None)
lo, hi = tr[len(tr) // 3], tr[2 * len(tr) // 3]
for name, test in (("against", lambda v: v <= lo), ("flat", lambda v: lo < v < hi), ("with", lambda v: v >= hi)):
    g_ = [x for x in allb if x[3][4] is not None and test(x[3][4])]
    stake = sum(x[2] for x in g_)
    print(f"  {name} [{lo:+.2f} .. {hi:+.2f}]: n={len(g_)} actual {sum(x[0] for x in g_) / stake:+.3f}/$ held {sum(x[1] for x in g_) / stake:+.3f}/$")


def lean(i):
    L = by_win[wins[i]]
    up = sum(1 for x in L if x[0]["side"] == "yes")
    return abs(up - (len(L) - up)) / len(L)


for name, idx in (("LOSING waves", neg), ("WINNING waves", pos)):
    print(f"{name}: mean one-sidedness of a window (|up - down| / bets; 1 = every bet the same direction) {sum(lean(i) for i in idx) / max(len(idx), 1):.2f}; mean hour drift at the buy {sum(x[4][3] for i in idx for x in by_win[wins[i]] if x[4] and x[4][3] is not None) / max(sum(1 for i in idx for x in by_win[wins[i]] if x[4] and x[4][3] is not None), 1):+.2f}")
print("DONE")
