"""What separates the crypto underdog lane's wins from its losses? (2026-11-10, user: "what would be good to look at are our underdog wins, and what differentiated them from the losses")
Population: every live bet of crypto_multiscale_underdog (a long shot bought at 0.19-0.40 on the curve model's net-per-dollar), settled window. A win is a bet whose side settled at $1 (held to the result); the lane's own sale (shock, value exit) is shown next to it because a long shot's win is worth about 2.5-4 dollars on a one-dollar stake and a sale at 0.05-0.30 gives it away.
Per bet, all in the market's own units (typical minute = rms one-second log move over the 900 s before the buy times sqrt(60)): the model's predicted net per $1 (from the decision's reason), entry price, seconds left at the buy, the cushion at the buy (log distance of the spot to the strike, signed with the bet, in typical minutes; negative = the bet is on the wrong side of the strike, as a long shot is), the drift of the hour and of the 15 minutes before the buy signed with the bet (typical minutes), the regime ratio (rms of the last 180 s over the last 3600 s, 1 = a normal minute), the path efficiency of the 5 minutes before the buy, and what the spot did after the buy: the best cushion it reached before the close and the move over the bet's life.
No rule is keyed on a market. Small sample (44 bets): this is a description to find the question for a proper study, not a rule.

    scripts/cloud.sh run scripts/underdog_review.py
"""

import bisect
import itertools
import math
import os
import re

from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store

os.nice(10)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)

rows = st.db.execute(
    "SELECT d.ts,d.ticker,d.side,d.contracts,d.stake_usd,d.exit_price,d.exit_reason,d.reason,COALESCE(d.outcome,w.result) AS res,w.close_ts,w.strike "
    "FROM decisions d LEFT JOIN windows w ON w.ticker=d.ticker WHERE d.action='bet' AND d.mode='trade' AND d.contracts>0 AND d.stake_usd>0 AND d.lane='crypto_multiscale_underdog' ORDER BY d.ts"
).fetchall()
out = []
for r in rows:
    if r["res"] not in ("yes", "no") or not r["close_ts"] or not r["strike"]:
        continue
    ser = r["ticker"].split("-")[0]
    ix = variants.crypto_index(ser)
    if not ix:
        continue
    t0, close, strike = float(r["ts"]), float(r["close_ts"]), float(r["strike"])
    tk = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts", (ix, t0 - 3700, close)).fetchall()
    if len(tk) < 2000:
        continue
    T = [float(x[0]) for x in tk]
    V = [float(x[1]) for x in tk]
    base = int(T[0])
    g, j, last = [], 0, None
    for t in range(base, int(close) + 1):
        while j < len(T) and T[j] <= t:
            last = V[j]
            j += 1
        g.append(math.log(last) if last else None)
    kb = int(t0) - base
    if kb < 1800 or kb >= len(g):
        continue
    sign = 1.0 if r["side"] == "yes" else -1.0
    sq = [0.0]
    for a, b in itertools.pairwise(g):
        sq.append(sq[-1] + ((b - a) ** 2 if a is not None and b is not None else 0.0))

    def unit(k, w):
        v = (sq[k] - sq[k - min(w, k)]) / min(w, k)
        return math.sqrt(v) * math.sqrt(60.0) if v > 0 else None

    u15, ush, ul = unit(kb, 900), unit(kb, 180), unit(kb, 3600)
    if not u15 or not ush or not ul:
        continue
    ref = math.log(strike)
    cush = (g[kb] - ref) * sign / u15
    seg = [x for x in g[kb - 300 : kb + 1] if x is not None]
    path = sum(abs(b - a) for a, b in itertools.pairwise(seg))
    eff = abs(seg[-1] - seg[0]) / path if path > 0 else None
    best = max((g[k] - ref) * sign for k in range(kb, len(g)) if g[k] is not None) / u15
    move = (g[-1] - g[kb]) * sign / u15
    m = re.search(r"curve model ([+-][\d.]+) per \$1", r["reason"] or "")
    n, cost = float(r["contracts"]), float(r["stake_usd"])
    held = (n if r["res"] == r["side"] else 0.0) - cost
    act = n * float(r["exit_price"]) - cost if r["exit_price"] else held
    kind = "held" if not r["exit_price"] else ((r["exit_reason"] or "").split(":")[0][:11])
    out.append(dict(
        t=r["ts"], px=cost / n, pred=float(m.group(1)) if m else float("nan"), left=close - t0, cush=cush, tr60=(g[kb] - g[kb - 3600]) * sign / ul,
        tr15=(g[kb] - g[kb - 900]) * sign / u15, ratio=ush / ul, eff=eff, best=best, move=move, won=r["res"] == r["side"], held=held, act=act, kind=kind, mkt=ser,
    ))
print(f"underdog bets settled with readings: {len(out)} | won at the result {sum(o['won'] for o in out)} | held net {sum(o['held'] for o in out):+.2f} | actual {sum(o['act'] for o in out):+.2f}")
print("\nactual outcome by how it ended:")
by = {}
for o in out:
    d = by.setdefault(o["kind"], [0, 0, 0.0, 0.0])
    d[0] += 1
    d[1] += o["won"]
    d[2] += o["act"]
    d[3] += o["held"]
for k, v in sorted(by.items()):
    print(f"  {k:12s} {v[0]:3d} bets, {v[1]:2d} would have won, actual {v[2]:+.2f}, held {v[3]:+.2f}")


def mean(xs):
    xs = [x for x in xs if x is not None and x == x]
    return sum(xs) / len(xs) if xs else float("nan")


print("\nwinners (settled on the bet's side) vs losers, means (medians in brackets):")
W, L = [o for o in out if o["won"]], [o for o in out if not o["won"]]


def med(xs):
    xs = sorted(x for x in xs if x is not None and x == x)
    return xs[len(xs) // 2] if xs else float("nan")


for key, lab in (("px", "entry price"), ("pred", "model net per $1"), ("left", "seconds left at the buy"), ("cush", "cushion at the buy (typ. min)"), ("tr60", "hour drift with the bet"), ("tr15", "15 min drift with the bet"), ("ratio", "regime ratio 180s/3600s"), ("eff", "path efficiency 5 min"), ("best", "best cushion reached after the buy"), ("move", "spot move over the bet's life")):
    print(f"  {lab:36s} wins {mean([o[key] for o in W]):+7.2f} [{med([o[key] for o in W]):+6.2f}]   losses {mean([o[key] for o in L]):+7.2f} [{med([o[key] for o in L]):+6.2f}]")
print(f"  n wins {len(W)}, n losses {len(L)}")

print("\nwin rate and held net by terciles of each reading at the buy (low / middle / high third):")
for key, lab in (("cush", "cushion"), ("tr60", "hour drift"), ("tr15", "15 min drift"), ("ratio", "regime ratio"), ("pred", "model net"), ("px", "price"), ("left", "seconds left")):
    xs = sorted(o[key] for o in out if o[key] == o[key])
    lo, hi = xs[len(xs) // 3], xs[2 * len(xs) // 3]
    parts = []
    for name, test in (("low", lambda v: v <= lo), ("mid", lambda v: lo < v < hi), ("high", lambda v: v >= hi)):
        g_ = [o for o in out if o[key] == o[key] and test(o[key])]
        parts.append(f"{name} n={len(g_)} won {sum(o['won'] for o in g_)} held {sum(o['held'] for o in g_):+.1f}")
    print(f"  {lab:13s} [{lo:+.2f} .. {hi:+.2f}]  " + " | ".join(parts))
print("DONE")
