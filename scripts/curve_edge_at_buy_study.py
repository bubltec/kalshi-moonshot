"""How do the live lane's bets do by the curve edge at the buy? (2026-11-15 night, after XRP 23:00 NO was bought at 0.78 with the index 13.5 bp under the strike and a curve edge of -0.037)

One question, same bets (CLAUDE.md rule 4): every live crypto_multiscale bet since always = true went on (2026-11-15 16:31 ET) and, for reference, since the width change (2026-11-14 17:02 ET), with its result (its own exit or settlement, fee on every leg), by the "[curve edge x]" the decision note carries (the curve's chance minus the ask; the lane's floor is buy_curve_edge_min = -0.05). Buckets: under -0.03, -0.03 to 0, 0 to +0.03, over +0.03; n, share that made money, net and net per dollar staked. Not split by curve shape. A first look at whether the negative-edge buys the lane's floor allows are the ones that lose.

Run on the host: python scripts/curve_edge_at_buy_study.py
"""
import datetime
import re

from kalshi_moonshot import config, db
from kalshi_moonshot.strategy import fee_per_contract

ET = datetime.timezone(datetime.timedelta(hours=-5))
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
for label, since in (("since always = true (11-15 16:31 ET)", datetime.datetime(2026, 11, 15, 16, 31, tzinfo=ET)), ("since the width change (11-14 17:02 ET)", datetime.datetime(2026, 11, 14, 17, 2, tzinfo=ET))):
    rows = []
    for r in st.execute("SELECT contracts, stake_usd, exit_price, outcome, side, reason FROM decisions WHERE lane='crypto_multiscale' AND mode='trade' AND action='bet' AND stake_usd>0 AND ts>?", (since.timestamp(),)).fetchall():
        n, cost = float(r["contracts"] or 0), float(r["stake_usd"])
        m = re.search(r"\[curve edge ([+-]?\d+\.\d+)\]", str(r["reason"] or ""))
        if n <= 0 or not m:
            continue
        if r["exit_price"] is not None:
            x = float(r["exit_price"]); v = n * (x - fee_per_contract(x)) - cost
        elif r["outcome"] in ("yes", "no"):
            v = (n if r["outcome"] == r["side"] else 0.0) - cost
        else:
            continue
        rows.append((float(m.group(1)), v, cost))
    print(f"\n{label}: bets with a curve edge in the note and a result: {len(rows)}")
    for lo, hi in ((-9, -0.03), (-0.03, 0.0), (0.0, 0.03), (0.03, 9)):
        g = [x for x in rows if lo <= x[0] < hi]
        if g:
            c = sum(x[2] for x in g)
            print(f"  edge {lo if lo > -9 else '<'} to {hi if hi < 9 else '>'}: n {len(g):3d} | won {sum(1 for x in g if x[1] > 0) / len(g):4.0%} | net ${sum(x[1] for x in g):+8.2f} ({sum(x[1] for x in g) / c * 100:+5.1f}% on ${c:6.0f})")
print("DONE")
