"""HOW SELECTIVE SHOULD A LANE BE: for a lane's finished bets (paper and live, held to the result or exited), group by how many bets the lane made in the same 15-minute window across coins, by entry price, and by coin, with the per-dollar return after the buy fee (and the exit price where it exited). Standard errors are clustered by window because the coins move together.
    docker compose exec -T bot python - < scripts/selectivity_study.py   (LANE=crypto_favorite SINCE_DAYS=7)
"""
import math
import os
import statistics
from collections import defaultdict

from kalshi_moonshot import config
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
st = Store(s.db_path, s.exclude_versions)
lane = os.environ.get("LANE", "crypto_favorite")
days = float(os.environ.get("SINCE_DAYS", "7"))
rows = st.db.execute("""SELECT d.ts, d.ticker, d.side, d.ask, d.exit_price, d.contracts, w.result, w.close_ts FROM decisions d JOIN windows w ON w.ticker=d.ticker
    WHERE d.lane=? AND d.action='bet' AND w.result IN ('yes','no') AND d.ts > extract(epoch from now()) - ?*86400""", (lane, days)).fetchall()
bets = []
for r in rows:
    won = 1.0 if r["result"] == r["side"] else 0.0
    got = r["exit_price"] if r["exit_price"] is not None else won
    ask = float(r["ask"])
    bets.append({"win": r["close_ts"], "coin": r["ticker"].split("-")[0], "ask": ask, "ret": (got - ask - fee(ask)) / (ask + fee(ask)), "pnl": got - ask - fee(ask)})
by_win = defaultdict(list)
for b in bets:
    by_win[b["win"]].append(b)
for b in bets:
    b["n"] = len(by_win[b["win"]])


def show(name, groups):
    print(name)
    for key, g in groups:
        if len(g) < 8:
            continue
        wins = defaultdict(list)
        for b in g:
            wins[b["win"]].append(b["pnl"])
        per = [sum(v) for v in wins.values()]  # per-window sums: the clusters
        tot = sum(b["pnl"] for b in g)
        cost = sum(b["ask"] + fee(b["ask"]) for b in g)
        se = math.sqrt(len(per)) * statistics.pstdev(per) if len(per) > 1 else float("nan")
        print(f"  {key!s:12} {len(g):4} bets in {len(wins):3} windows; net {tot:+7.2f} per contract-set ({100 * tot / cost:+5.1f}% of cost, cluster se of the sum +-{se:.2f})")


print(lane, len(bets), "finished bets,", len(by_win), "windows")
show("by how many bets the lane made in that window", [(k, [b for b in bets if min(b["n"], 5) == k]) for k in (1, 2, 3, 4, 5)])
show("by entry price", [(f"{lo:.2f}-{hi:.2f}", [b for b in bets if lo <= b["ask"] < hi]) for lo, hi in ((0.60, 0.70), (0.70, 0.75), (0.75, 0.80), (0.80, 0.85), (0.85, 0.90), (0.90, 1.0))])
show("by coin", sorted(((c, [b for b in bets if b["coin"] == c]) for c in {b["coin"] for b in bets}), key=lambda x: x[0]))
