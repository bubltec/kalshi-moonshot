"""How big are the live lane's losses against its wins, by entry price? (2026-11-15, user: "still losing too big")

One question, same bets (CLAUDE.md rule 4): every live crypto_multiscale bet since the width change (2026-11-14 17:02 ET) with a result (its own exit or settlement, fee on every leg), by the price paid: n, the share that made money, the average win and the average loss in dollars and as a share of the stake, the net, and the win rate each band needs to break even at its own average win and loss. Also the loss tail: the five biggest losses. Not split by curve shape. A first look: it says where the size of a loss comes from, not what to change.

Run on the host: python scripts/loss_size_by_price.py (SINCE=epoch seconds to change the start).
"""
import datetime
import os

from kalshi_moonshot import config, db
from kalshi_moonshot.strategy import fee_per_contract

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = float(os.environ.get("SINCE", datetime.datetime(2026, 11, 14, 17, 2, tzinfo=ET).timestamp()))
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
rows = []
for r in st.execute("SELECT ts, ticker, side, contracts, stake_usd, exit_price, exit_reason, outcome FROM decisions WHERE lane='crypto_multiscale' AND mode='trade' AND action='bet' AND stake_usd>0 AND ts>? ORDER BY ts", (SINCE,)).fetchall():
    n, cost = float(r["contracts"] or 0), float(r["stake_usd"])
    if n <= 0:
        continue
    if r["exit_price"] is not None:
        x = float(r["exit_price"]); v = n * (x - fee_per_contract(x)) - cost; how = "stop" if "max loss" in str(r["exit_reason"]) else "take" if "profit take" in str(r["exit_reason"]) else "exit"
    elif r["outcome"] in ("yes", "no"):
        v = (n if r["outcome"] == r["side"] else 0.0) - cost; how = "held"
    else:
        continue
    rows.append((cost / n, cost, v, how, r["ticker"][2:-3], r["ts"]))
print(f"live bets since {datetime.datetime.fromtimestamp(SINCE, ET):%m-%d %H:%M} ET with a result: {len(rows)}; net ${sum(r[2] for r in rows):+.2f} on ${sum(r[1] for r in rows):.0f}")
print("\nby entry price: n | made money | avg win $ (% of stake) | avg loss $ (% of stake) | net | win rate needed to break even")
for lo, hi in ((0.0, 0.65), (0.65, 0.72), (0.72, 0.78), (0.78, 0.9)):
    g = [r for r in rows if lo <= r[0] < hi]
    w = [r for r in g if r[2] > 0]; l = [r for r in g if r[2] <= 0]
    if not g:
        continue
    aw = sum(r[2] for r in w) / len(w) if w else 0.0; al = -sum(r[2] for r in l) / len(l) if l else 0.0
    need = al / (aw + al) if aw + al > 0 else 0.0
    print(f"  {lo:.2f}-{hi:.2f}: n {len(g):3d} | won {len(w):3d} ({len(w) / len(g):.0%}) | avg win ${aw:5.2f} ({sum(r[2] / r[1] for r in w) / len(w) * 100 if w else 0:+.0f}%) | avg loss ${al:5.2f} ({sum(-r[2] / r[1] for r in l) / len(l) * 100 if l else 0:.0f}%) | net ${sum(r[2] for r in g):+7.2f} | needs {need:.0%}")
print("\nlosses by how they ended: n | avg loss $ | avg loss % of stake")
for how in ("stop", "held", "exit", "take"):
    l = [r for r in rows if r[3] == how and r[2] <= 0]
    if l:
        print(f"  {how:5s}: n {len(l):3d} | ${sum(-r[2] for r in l) / len(l):5.2f} | {sum(-r[2] / r[1] for r in l) / len(l) * 100:.0f}%")
print("\nfive biggest losses:")
for r in sorted(rows, key=lambda r: r[2])[:5]:
    print(f"  {datetime.datetime.fromtimestamp(r[5], ET):%m-%d %H:%M} {r[4]:18s} entry {r[0]:.2f} stake ${r[1]:5.2f} result ${r[2]:+6.2f} ({r[2] / r[1] * 100:+.0f}%) {r[3]}")
print("DONE")
