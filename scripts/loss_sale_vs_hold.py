"""The lane's own loss sales against holding (2026-11-16, user, on DOGE 09:30 YES bought at 0.59, sold by the max-loss stop at 0.36 three minutes later, and the market then settled YES: "long term looks like doge would have won, the spike held").

One question on the same bets (CLAUDE.md rule 4): every live crypto_multiscale bet since 2026-11-14 17:02 ET that the lane sold before the close, by the kind of sale (the exit reason's own label), against holding that bet to settlement. Fee on every leg; contracts from the stake over the all-in ask.
Shapes: how far into the market the sale came, and how long after the buy.

Run on the host: scripts/cloud.sh run scripts/loss_sale_vs_hold.py
"""
import datetime
import re
from collections import defaultdict

from kalshi_moonshot import config, db
from kalshi_moonshot.strategy import fee_per_contract as fee

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 14, 17, 2, tzinfo=ET).timestamp()
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
res = {w["ticker"]: (w["result"], float(w["close_ts"])) for w in st.execute("SELECT ticker, close_ts, result FROM windows WHERE close_ts>? AND result IN ('yes','no')", (SINCE,)).fetchall()}
rows = []  # (kind, sale less hold in dollars, won if held, seconds into the market at the sale, seconds held, gain at the sale)
total = 0
for r in st.execute("SELECT ts, ticker, side, ask, stake_usd, exit_price, exit_ts, exit_reason FROM decisions WHERE lane='crypto_multiscale' AND mode='trade' AND action='bet' AND stake_usd>0 AND ts>? ORDER BY ts", (SINCE,)).fetchall():
    if r["ticker"] not in res or not r["ask"] or not (0.05 < float(r["ask"]) < 0.99):
        continue
    total += 1
    result, close = res[r["ticker"]]
    if r["exit_price"] is None or r["exit_ts"] is None or float(r["exit_ts"]) >= close - 1:
        continue
    ask, px = float(r["ask"]), float(r["exit_price"])
    basis = ask + fee(ask)
    n = float(r["stake_usd"]) / basis
    won = result == r["side"]
    sale, hold = px - fee(px) - basis, (1.0 if won else 0.0) - basis
    m = re.search(r"\(([a-z ]+)\)", str(r["exit_reason"] or ""))
    kind = m.group(1) if m else str(r["exit_reason"] or "?")[:30]
    rows.append((kind, (sale - hold) * n, won, float(r["exit_ts"]) - (close - 900.0), float(r["exit_ts"]) - float(r["ts"]), sale))


def line(name, rs):
    if not rs:
        print(f"  {name:34s} n   0")
        return
    w = [x for x in rs if x[2]]
    print(f"  {name:34s} n {len(rs):3d} | would have won held {len(w):3d} ({len(w) / len(rs):4.0%}) | sale less hold ${sum(x[1] for x in rs):+8.2f} | saved ${sum(x[1] for x in rs if x[1] > 0):+8.2f} on {sum(1 for x in rs if x[1] > 0)}, gave up ${sum(x[1] for x in rs if x[1] < 0):+8.2f} on {sum(1 for x in rs if x[1] < 0)}")


print(f"window: since 2026-11-14 17:02 ET; {total} settled live crypto_multiscale bets, {len(rows)} sold before the close")
print("\n1. by the kind of sale")
kinds = defaultdict(list)
for x in rows:
    kinds[x[0]].append(x)
for k, rs in sorted(kinds.items(), key=lambda kv: -len(kv[1])):
    line(k, rs)
loss = [x for x in rows if x[5] < 0]
print("\n2. sales at a loss, by minutes into the market at the sale")
for lo, hi in ((0, 6), (6, 9), (9, 12), (12, 15)):
    line(f"{lo} to {hi} min", [x for x in loss if lo * 60 <= x[3] < hi * 60])
print("\n3. sales at a loss, by how long the bet had been held")
for lo, hi, name in ((0, 120, "under 2 min"), (120, 300, "2 to 5 min"), (300, 900, "5 min and over")):
    line(name, [x for x in loss if lo <= x[4] < hi])
print("DONE")
