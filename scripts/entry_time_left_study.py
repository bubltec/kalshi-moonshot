"""How do the live lane's bets do by how much of the market was left at the buy? (2026-11-15, after ETH 21:45 YES and BNB 21:45 YES were bought with 279 and 166 s left and stopped at -71% and -81% within a minute)

One question, same bets (CLAUDE.md rule 4): every live crypto_multiscale bet since 2026-11-14 17:02 ET with a result (its own exit or settlement, fee on every leg), by the seconds left to the close at the buy: n, share that made money, net, per dollar staked, and the average loss and win. Also the share stopped within 60 s of the buy, since a bet bought into a move that reverses at once is stopped almost at once. Not split by curve shape. A first look at where in the market the losses are bought, not a rule.

Run on the host: python scripts/entry_time_left_study.py
"""
import datetime
from collections import defaultdict

from kalshi_moonshot import config, db
from kalshi_moonshot.strategy import fee_per_contract

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 14, 17, 2, tzinfo=ET).timestamp()
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
g = defaultdict(list)
for r in st.execute("SELECT ts, contracts, stake_usd, exit_price, exit_ts, exit_reason, outcome, side, close_time FROM decisions WHERE lane='crypto_multiscale' AND mode='trade' AND action='bet' AND stake_usd>0 AND ts>?", (SINCE,)).fetchall():
    n, cost = float(r["contracts"] or 0), float(r["stake_usd"])
    if n <= 0:
        continue
    close = datetime.datetime.fromisoformat(str(r["close_time"]).replace("Z", "+00:00")).timestamp()
    left = close - float(r["ts"])
    if r["exit_price"] is not None:
        x = float(r["exit_price"]); v = n * (x - fee_per_contract(x)) - cost
    elif r["outcome"] in ("yes", "no"):
        v = (n if r["outcome"] == r["side"] else 0.0) - cost
    else:
        continue
    quick = r["exit_ts"] is not None and float(r["exit_ts"]) - float(r["ts"]) <= 60.0 and "max loss" in str(r["exit_reason"])
    key = "under 180 s" if left < 180 else "180-300 s" if left < 300 else "300-450 s" if left < 450 else "450 s and over"
    g[key].append((v, cost, quick))
print("by seconds left at the buy: n | made money | net (per $ staked) | avg win | avg loss | stopped within 60 s of the buy")
for key in ("under 180 s", "180-300 s", "300-450 s", "450 s and over"):
    x = g.get(key, [])
    if not x:
        continue
    w = [a for a in x if a[0] > 0]; l = [a for a in x if a[0] <= 0]
    c = sum(a[1] for a in x)
    print(f"  {key:15s}: n {len(x):3d} | won {len(w) / len(x):.0%} | ${sum(a[0] for a in x):+8.2f} ({sum(a[0] for a in x) / c * 100:+5.1f}%) | win ${sum(a[0] for a in w) / max(len(w), 1):5.2f} | loss ${-sum(a[0] for a in l) / max(len(l), 1):5.2f} | quick stops {sum(1 for a in x if a[2])}")
print("DONE")
