"""If the entry's leg reading starts its walk before the open, which of the lane's buys would it have waited on, and how did those do? (2026-11-27; user: "Why doesn't it read before the markets open? It buys starting at 3 mins how would it know if it's not inside a curve?"; "Can we make it look at least 10mins back?"; "the current one that only looks at the market start".)
QUESTION (one, on the lane's own bets): every live PriskP bet since 2026-11-25 15:57 ET with tape whose own reading at the buy (wavefacts.curve walked from the open, as the lane reads it) says regime 'curve' and verdict 'buy': with the walk started 10, 30 and 60 minutes before the open (the same swing sizes, the same typical minute and usual leg of the hour before the open), does the verdict stay 'buy' or become 'wait', and what did the bets in each group make held to settlement and as run. What changes is said too (the class, or the leg being at its extreme).
HONEST LIMITS: it can only show buys the longer walk would have WAITED on, not buys it would have added or made later; the verdict is read at the second of the buy from the taped index; a day and a half of bets; no asset key. 10 minutes is the user's figure, 30 and 60 are beside it to show whether the answer depends on it.
Run: PYTHONPATH=src .venv/bin/python scripts/priskp_leg_walk_back_table.py BETS.json.gz INDEX.json.gz"""
import collections
import datetime
import gzip
import json
import re
import statistics
import sys

from kalshi_moonshot import ticks, wavefacts
from kalshi_moonshot.kalshiws import index_for_series

ET = datetime.timezone(datetime.timedelta(hours=-5))
ticks.history.keep = ticks.history.slack = 1e12
for ix, (T, V) in json.load(gzip.open(sys.argv[2])).items():
    for t, v in zip(T, V, strict=True):
        ticks.history.note(ix, float(t), float(v))
fee = lambda p: 0.07 * p * (1.0 - p)  # noqa: E731
BACKS = (600.0, 1800.0, 3600.0)
tab = {b: collections.defaultdict(list) for b in BACKS}
n_curve = n_all = 0
for b in json.load(gzip.open(sys.argv[1])):
    if len(b["bid"]) != 900 or not b["strike"] or b["result"] not in ("yes", "no"):
        continue
    n_all += 1
    ix = index_for_series(re.match(r"(KX[A-Z0-9]+15M)", b["ticker"]).group(1))
    close = b["close"]
    opened, left = float(close - 900), float(close - b["ts"])
    base = wavefacts.curve(ix, opened, float(b["ts"]), b["strike"], b["side"], left)
    if base is None or base["regime"] != "curve" or base["verdict"] != "buy":
        continue
    n_curve += 1
    won = b["result"] == b["side"]
    row = (won, b["entry"], ((1.0 if won else 0.0) - b["entry"]) * b["n"], (((b["exit_price"] - fee(b["exit_price"])) if b["exit_ts"] else (1.0 if won else 0.0)) - b["entry"]) * b["n"], datetime.datetime.fromtimestamp(b["ts"], ET).strftime("%m-%d"))
    for back in BACKS:
        e = wavefacts.curve(ix, opened, float(b["ts"]), b["strike"], b["side"], left, back)
        if e is None:
            tab[back]["unread"].append(row)
        elif e["verdict"] == "buy":
            tab[back]["still BUY"].append(row)
        else:
            why = "the swing is " + e["class"] if e["class"] != "inside" else "the leg is at its extreme" if e["at_extreme"] else "the swing or the turn before it is under a typical minute" if e["regime"] == "curve" else "not a curve (the target is out of reach)"
            tab[back]["WAIT: " + why].append(row)
print(f"{n_all} bets with tape; {n_curve} of them bought on a curve 'buy' as the lane reads it (walked from the open)\n")
for back in BACKS:
    print(f"walk started {back / 60:.0f} minutes before the open:")
    for k in sorted(tab[back]):
        v = tab[back][k]
        days = {d: round(sum(x[2] for x in v if x[4] == d), 2) for d in sorted({x[4] for x in v})}
        print(f"   {k:62s} n {len(v):3d}  settled its way {sum(x[0] for x in v) / len(v):4.0%}  avg paid {statistics.mean(x[1] for x in v):.2f}  held ${sum(x[2] for x in v):8.2f} ({sum(x[2] for x in v) / len(v):+.2f} a bet)  as run ${sum(x[3] for x in v):8.2f}  by day {days}")
    print()
