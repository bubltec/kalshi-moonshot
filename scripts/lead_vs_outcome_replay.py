"""Does a commodity bet's lead over the window-open price, in typical moves left, predict whether it wins? Places nothing.

For every settled live commodity bet since the BiQuote tape began: z = the BiQuote index's move since the window opened (signed for the side held) divided by its usual one-second move
times sqrt(seconds left at the buy). Prints win rate, net dollars and total losses by z quartile, and the result of skipping bets under z of 0.25 / 0.5 / 0.75 / 1.0.
The window-open BiQuote price stands in for Kalshi's Pyth strike, which differs by a dollar or two on palladium.
2026-11-03, 115 bets over three days: no relation (closest quartile won 93%, widest 90%). Rerun with two weeks or more of ticks.
    scripts/cloud.sh run scripts/lead_vs_outcome_replay.py
"""
import bisect
import collections
import math
import statistics as S
import time

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

IX = {"GOLD": "BQ_GOLD", "SILVER": "BQ_SILVER", "PLATINUM": "BQ_PLATINUM", "PALLADIUM": "BQ_PALLADIUM", "WTI": "BQ_WTI", "BRENT": "BQ_BRENT", "NATGAS": "BQ_NATGAS", "COPPER": "BQ_COPPER"}
s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
st = Store(s.db_path, s.exclude_versions)
first = tape.execute("SELECT MIN(ts) m FROM index_ticks WHERE index_id='BQ_GOLD'").fetchone()["m"]
print(f"BiQuote ticks from {time.strftime('%Y-%m-%d', time.gmtime(first))}")
rows = st.db.execute(
    "SELECT d.ts, d.ticker, d.side, d.ask, d.stake_usd, d.exit_price, d.outcome, w.close_ts FROM decisions d JOIN windows w ON w.ticker = d.ticker "
    "WHERE d.action = 'bet' AND d.mode = 'trade' AND d.lane LIKE 'commodity%' AND d.outcome IN ('yes','no') AND d.ts > ?",
    (first + 600,),
).fetchall()
out = []
for r in rows:
    ix = IX.get(r["ticker"].split("-")[0][2:].replace("15M", ""))
    if not ix:
        continue
    op = r["close_ts"] - 900
    tk = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value > 0 ORDER BY ts", (ix, op - 300, r["ts"])).fetchall()
    if len(tk) < 60:
        continue
    t = [float(x[0]) for x in tk]
    v = [float(x[1]) for x in tk]
    i0 = bisect.bisect_left(t, op)
    if i0 >= len(t) - 5 or t[i0] - op > 20:
        continue
    rets = [math.log(v[i] / v[i - 1]) for i in range(1, len(v)) if t[i] - t[i - 1] <= 3]
    sig = S.pstdev(rets) if len(rets) > 30 else 0
    if sig <= 0:
        continue
    d = math.log(v[-1] / v[i0])
    d = d if r["side"] == "yes" else -d
    z = d / (sig * math.sqrt(r["close_ts"] - r["ts"]))
    n = r["stake_usd"] / r["ask"]
    if r["exit_price"] is not None:
        p = (r["exit_price"] - r["ask"]) * n - fee(r["exit_price"]) * n
    elif r["outcome"] == r["side"]:
        p = (1 - r["ask"]) * n
    else:
        p = -r["stake_usd"]
    out.append({"z": z, "p": p, "stake": r["stake_usd"], "full": r["exit_price"] is None and r["outcome"] != r["side"], "ask": r["ask"]})
print("bets scored", len(out))
zs = sorted(x["z"] for x in out)
cuts = [zs[int(len(zs) * k / 4)] for k in range(1, 4)]
print("z cut points", [round(c, 2) for c in cuts])
groups = collections.defaultdict(list)
for x in out:
    groups[bisect.bisect(cuts, x["z"])].append(x)
print("quartile (1 = smallest lead)    n  win%     net    per$  full losses  mean ask")
for k in sorted(groups):
    g = groups[k]
    print(f"   {k + 1}  {len(g):4d}  {sum(x['p'] > 0 for x in g) / len(g) * 100:4.0f}  {sum(x['p'] for x in g):7.1f}  {sum(x['p'] for x in g) / sum(x['stake'] for x in g) * 100:5.1f}%  {sum(x['full'] for x in g):4d}   {sum(x['ask'] for x in g) / len(g):.2f}")


def summary(g):
    return f"n {len(g)} win {sum(x['p'] > 0 for x in g) / max(len(g), 1) * 100:.0f}% net {sum(x['p'] for x in g):+.1f}"


for cut in (0.25, 0.5, 0.75, 1.0):
    print(f"z<{cut}: {summary([x for x in out if x['z'] < cut])} | z>={cut}: {summary([x for x in out if x['z'] >= cut])}")
print("DONE")
