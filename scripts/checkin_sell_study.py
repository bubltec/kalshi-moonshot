"""What would selling at each check-in have done? Reads position_checks (checkins.py: one row per open position at 240, 210 ... 30, 10 seconds before the close) against how each bet ended.

For every mark: the positions looked at, the return per $1 staked if each had been sold then at its recorded bid (less SLIP), against holding to the close, split by whether the index was through the strike
against the position at that moment. A rule "sell at mark M when through >= X" is then one row of the table. Places nothing.
    scripts/cloud.sh run scripts/checkin_sell_study.py [SLIP=0.02] [MIN_THROUGH=0.0]
"""
import os
from collections import defaultdict

from kalshi_moonshot import config
from kalshi_moonshot.digest import _won
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
main = Store(s.db_path, s.exclude_versions)
SLIP = float(os.environ.get("SLIP", "0.02"))
rows = main.db.execute(
    "SELECT c.ticker, c.lane, c.mark, c.left_s, c.entry, c.bid, c.through, c.through_on, c.through_kept, d.ask, d.side, d.outcome, d.exit_price, d.stake_usd "
    "FROM position_checks c JOIN decisions d ON d.ticker = c.ticker AND d.lane = c.lane AND d.action = 'bet' AND d.mode = 'trade' "
    "WHERE d.outcome IN ('yes','no') AND c.bid > 0 ORDER BY c.ticker, c.mark DESC").fetchall()
print(f"{len(rows)} check-in rows over {len({(r['ticker'], r['lane']) for r in rows})} settled live bets (slip {SLIP})")


def hold(r):
    cost = r["ask"] + fee(r["ask"])
    return (1.0 if _won(r) == 1.0 else 0.0) / cost - 1


def sold(r):
    cost = r["ask"] + fee(r["ask"])
    return (r["bid"] - SLIP - fee(r["bid"])) / cost - 1


by = defaultdict(list)
for r in rows:
    by[(r["mark"], "through" if (r["through"] or 0) > float(os.environ.get("MIN_THROUGH", "0")) else "winning side")].append(r)
print(f"\n{'mark':>5} {'index is':<13} {'n':>4} {'bid avg':>8} {'won %':>6} {'sell now $/$':>13} {'hold $/$':>9} {'sell - hold':>11}")
for (mark, state), g in sorted(by.items(), key=lambda kv: (-kv[0][0], kv[0][1])):
    sn = sum(sold(r) for r in g) / len(g)
    hd = sum(hold(r) for r in g) / len(g)
    print(f"{mark:>5} {state:<13} {len(g):>4} {sum(r['bid'] for r in g) / len(g):>8.2f} {100 * sum(_won(r) == 1.0 for r in g) / len(g):>6.1f} {sn:>+13.4f} {hd:>+9.4f} {sn - hd:>+11.4f}")
