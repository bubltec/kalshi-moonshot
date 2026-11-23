"""Score the risk exit's shadow against what the live bets really got (2026-11-13, user: "exit first").
Question: on the live bets where the risk exit would have sold (data/risk_shadow.jsonl, one row per bet at its first would-sell), what did the sale at that bid net, against what the bet actually got (the lane's own exit, or held to settlement)? And over all live bets since the shadow started: the lane as run, against the lane with the risk exit's sales swapped in.
The sale is priced at the bid the signal logged, fee on the sale (the buy fee is in the stake). A bet not yet settled or exited is counted separately. Settings (line 0.5, max loss 0.5 x q, min profit 0.05 x q) are guesses; this is a forward score, not a fit.
Not split by curve shape yet (rule 4): the rows carry no shape reading; add the entry factors before this is used to go live.
    scripts/cloud.sh run scripts/risk_shadow_score.py [SINCE_TS=unix seconds]
"""

import json
import os
from pathlib import Path

from kalshi_moonshot import config
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
st = Store(s.db_path, s.exclude_versions)
path = Path(s.data_dir) / "risk_shadow.jsonl"
rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()] if path.exists() else []
since = float(os.environ.get("SINCE_TS", rows[0]["ts"] if rows else 0))
print(f"shadow signals: {len(rows)} since {since:.0f}")


def actual_of(r):
    n, cost = float(r["contracts"]), float(r["stake_usd"])
    if r["exit_price"] is not None:
        return n * (float(r["exit_price"]) - fee(float(r["exit_price"]))) - cost
    if r["outcome"] in ("yes", "no"):
        return (n if r["outcome"] == r["side"] else 0.0) - cost
    return None


bets = st.db.execute("SELECT ticker,lane,side,contracts,stake_usd,exit_price,outcome,exit_reason,ts FROM decisions WHERE action='bet' AND mode='trade' AND stake_usd>0 AND contracts>0 AND ts>=?", (since,)).fetchall()
by = {(b["ticker"], b["lane"]): b for b in bets}
sig = {(x["ticker"], x["lane"]): x for x in rows}
tot = {"signaled": [], "unsignaled": []}
for key, b in by.items():
    a = actual_of(b)
    if a is None:
        continue
    if key in sig:
        x = sig[key]
        shadow = float(b["contracts"]) * (float(x["bid"]) - fee(float(x["bid"]))) - float(b["stake_usd"])
        tot["signaled"].append((key, x, shadow, a))
    else:
        tot["unsignaled"].append(a)
sg = tot["signaled"]
unsettled = sum(1 for k in sig if k in by and actual_of(by[k]) is None)
print(f"live bets since: {len(by)} | signaled and settled {len(sg)} | signaled but not settled yet {unsettled} | not signaled {len(tot['unsignaled'])}")
if sg:
    for label, f in (("all", lambda x: True), ("max loss", lambda x: x["stop"]), ("line", lambda x: not x["stop"])):
        sub = [t for t in sg if f(t[1])]
        if not sub:
            continue
        sh, ac = sum(t[2] for t in sub), sum(t[3] for t in sub)
        print(f"  {label:9s} n={len(sub):3d} | shadow sale {sh:+.2f} | as run {ac:+.2f} | difference {sh - ac:+.2f} | the sale beat the bet {sum(1 for t in sub if t[2] > t[3])}, lost to it {sum(1 for t in sub if t[2] < t[3])}")
    run_all = sum(t[3] for t in sg) + sum(tot["unsignaled"])
    swap = sum(t[2] for t in sg) + sum(tot["unsignaled"])
    print(f"  all settled live bets: as run {run_all:+.2f} | with the risk exit's sales {swap:+.2f} | difference {swap - run_all:+.2f}")
print("DONE")
