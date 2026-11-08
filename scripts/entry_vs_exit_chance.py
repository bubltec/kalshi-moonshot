"""Does a crypto_multiscale buy pay when the exit's own chance says the price is already over fair value? (2026-11-08, user: HYPE 09:15 sold at a gain by the value exit (bid 0.67 against the curve's 0.50), then the lane bought the same side again at 0.61 and lost; "it shouldn't have bought again after selling for a profit ... it's supposed to follow the curve")

Question: on the live crypto_multiscale bets that have a confidence-shadow row at the buy (mark 0: the confidence model's chance for the side at that moment, with the ask and bid then), split by whether the value exit's own test already held at the buy: bid after the sell fee over the chance by 0.02 or more ("the exit would sell at once; the buy pays the ask over what the curve calls fair"). What did each group return, as the lane actually did (its own exit, else held to the result, both fees)?
Reported: n, dollars staked, net, per $ staked, win rate, halves, and the first buy of a market side against re-buys. The 0.02 is the value exit's own margin (lane value_exit), not chosen here.
Not covered: bets without a confidence row at the buy (the shadow logs it only for a bet seen within 90 s of its buy).

    scripts/cloud.sh run scripts/entry_vs_exit_chance.py
"""

import json
from pathlib import Path

from kalshi_moonshot import config
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
st = Store(s.db_path, s.exclude_versions)
rows = []
for line in (Path(s.data_dir) / "confidence_shadow.jsonl").read_text().splitlines():
    try:
        r = json.loads(line)
    except ValueError:
        continue
    if r.get("kind") == "signal" and r.get("mark") == 0 and r.get("lane") == "crypto_multiscale":
        rows.append(r)
print("entry readings", len(rows))
bets = st.db.execute("SELECT ts,ticker,side,ask,stake_usd,contracts,outcome,exit_price FROM decisions WHERE action='bet' AND lane='crypto_multiscale' AND mode='trade' AND stake_usd>0 AND outcome IN ('yes','no') ORDER BY ts").fetchall()
by = {}
for b in bets:
    by.setdefault((b["ticker"], b["side"]), []).append(b)
data = []
for r in rows:
    cand = [b for b in by.get((r["ticker"], r["side"]), []) if abs(float(b["ts"]) - float(r["ts"])) < 120]
    if not cand:
        continue
    b = min(cand, key=lambda b: abs(float(b["ts"]) - float(r["ts"])))
    e, c = b["ask"], b["stake_usd"] / b["ask"]
    if b["exit_price"]:
        x = float(b["exit_price"])
        pnl = (x - e) * c - (fee(e) + fee(x)) * c
    else:
        pnl = ((1 - e) * c if b["outcome"] == b["side"] else -e * c) - fee(e) * c
    over = r["bid_side"] - fee(r["bid_side"]) - r["p_side"]
    rank = by[(r["ticker"], r["side"])].index(b)
    data.append({"ts": b["ts"], "pnl": pnl, "stake": b["stake_usd"], "over": over, "rebuy": rank > 0, "p": r["p_side"], "ask": r["ask_side"]})
print("matched to bets", len(data))
mid = sorted(d["ts"] for d in data)[len(data) // 2]


def line(label, sel):
    if not sel:
        print(f"   {label:52s} n=0")
        return
    n = len(sel)
    net = sum(d["pnl"] for d in sel)
    stk = sum(d["stake"] for d in sel)
    a = [d["pnl"] for d in sel if d["ts"] <= mid]
    b = [d["pnl"] for d in sel if d["ts"] > mid]
    won = sum(1 for d in sel if d["pnl"] > 0)
    print(f"   {label:52s} n={n:4d} staked ${stk:7.2f} net ${net:+7.2f} ({100 * net / stk:+5.1f}% of stake) won {100 * won / n:4.1f}% | halves {sum(a):+.2f} (n={len(a)}) / {sum(b):+.2f} (n={len(b)})")


for m in (0.0, 0.02, 0.05):
    print(f"\nthe exit's test held at the buy (bid after fee - chance >= {m:.2f}):")
    line("held at the buy (the exit would sell at once)", [d for d in data if d["over"] >= m])
    line("did not hold", [d for d in data if d["over"] < m])
print("\nfirst buy of a side vs re-buy:")
line("first buys", [d for d in data if not d["rebuy"]])
line("re-buys", [d for d in data if d["rebuy"]])
line("re-buys where the exit's test held (>= 0.02)", [d for d in data if d["rebuy"] and d["over"] >= 0.02])
print("DONE")
