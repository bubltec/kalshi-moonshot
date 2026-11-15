"""Does the lane's Nth same-side buy inside a few minutes pay worse than its first?

One question, same bets (CLAUDE.md rule 4): each live crypto_multiscale bet since Mon 2026-11-09, with its real result (its own exit,
or settlement), after the fee on every leg. For each bet, how many of the lane's other live bets of the SAME side were bought in the
WINDOW seconds before it (any market): its breadth rank k (0 = first of a cluster). Net and count by k, and what skipping every
bet of rank >= K would have changed (the skipped bet's result removed; nothing else assumed, so stake freed is not re-used).
Classified by the order of the lane's own buys, not by coin. Not split by curve shape here: this is the first look.

Run on the host: python scripts/same_side_breadth_study.py (WINDOW=480 seconds by default).
"""
import datetime
import os
from collections import defaultdict

from kalshi_moonshot import config, db
from kalshi_moonshot.strategy import fee_per_contract

WINDOW = float(os.environ.get("WINDOW", "480"))
SINCE = datetime.datetime(2026, 11, 9, 5, 0, tzinfo=datetime.UTC).timestamp()
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
rows = st.execute(
    "SELECT ts, ticker, side, contracts, stake_usd, exit_price, outcome FROM decisions "
    "WHERE lane='crypto_multiscale' AND mode='trade' AND action='bet' AND stake_usd>0 AND ts>? ORDER BY ts", (SINCE,)).fetchall()
bets = []
for r in rows:
    n, cost = float(r["contracts"]), float(r["stake_usd"])
    if r["exit_price"] is not None:
        x = float(r["exit_price"]); v = n * (x - fee_per_contract(x)) - cost
    elif r["outcome"] in ("yes", "no"):
        v = (n if r["outcome"] == r["side"] else 0.0) - cost
    else:
        continue
    bets.append(dict(t=float(r["ts"]), side=r["side"], v=v, cost=cost))
for i, b in enumerate(bets):
    b["k"] = sum(1 for o in bets[:i] if o["side"] == b["side"] and b["t"] - o["t"] <= WINDOW)
    b["opp"] = sum(1 for o in bets[:i] if o["side"] != b["side"] and b["t"] - o["t"] <= WINDOW)
print(f"live crypto_multiscale bets since Mon 11-09 with a result: {len(bets)}, net ${sum(b['v'] for b in bets):+.2f}, window {WINDOW:.0f} s")
print("\nby rank among same-side buys in the window (0 = first):")
g = defaultdict(list)
for b in bets:
    g[min(b["k"], 4)].append(b)
for k in sorted(g):
    x = g[k]; c = sum(b["cost"] for b in x)
    print(f"  rank {k}{'+' if k == 4 else ' '}: n {len(x):3d}  net {sum(b['v'] for b in x):+8.2f}  on ${c:6.0f} ({sum(b['v'] for b in x) / c * 100:+5.1f}%)  won {sum(1 for b in x if b['v'] > 0):3d}  avg win {sum(b['v'] for b in x if b['v'] > 0) / max(1, sum(1 for b in x if b['v'] > 0)):+.2f}  avg loss {sum(b['v'] for b in x if b['v'] <= 0) / max(1, sum(1 for b in x if b['v'] <= 0)):+.2f}")
print("\nskipping every bet of rank >= K (same bets, result removed):")
for K in (1, 2, 3, 4):
    sk = [b for b in bets if b["k"] >= K]
    print(f"  K={K}: skips {len(sk):3d} bets, change {-sum(b['v'] for b in sk):+8.2f}; better {sum(1 for b in sk if b['v'] < 0)}, worse {sum(1 for b in sk if b['v'] > 0)}")
print("\nby opposite-side buys in the window:")
for k in (0, 1, 2):
    x = [b for b in bets if min(b["opp"], 2) == k]
    print(f"  opposite {k}{'+' if k == 2 else ' '}: n {len(x):3d} net {sum(b['v'] for b in x):+8.2f}")
print("DONE")
