"""How the curve would have bought the commodity favorites today (2026-11-10, user: "do a comparison of how the curve model would have bought the favorite lanes today").
Question: on the favorites lanes' live bets since SINCE (default 00:00 ET 2026-11-10), what would a curve-gated buy have done? The gate: at the decision, P = the curve's chance the side pays (curvevalue.read, the lower of the 900 s and 180 s rates, against the window-open index value, the reference the watcher uses), and the bet is bought only when P - price paid (fee included in the price) >= margin. Margins 0.00 / 0.03 / 0.05 / 0.08 are fixed before the results; the choice, if any, is made after seeing them and is said to be so.
Compared on the same bets: what the bet actually made (own sales and settlement, fee on every leg) against what it would have made held. Skipped bets are counted at zero. n, net dollars, bets beaten/lost to what happened, and the result in two halves of the period. Run: scripts/cloud.sh run scripts/favorites_curve_buy_compare.py   (needs today's data, so not a cloud job)
"""

import bisect
import calendar
import math
import os

from kalshi_moonshot import config, curvevalue, db, variants
from kalshi_moonshot.store import Store

s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
SINCE = float(os.environ.get("SINCE", calendar.timegm((2026, 11, 10, 5, 0, 0))))
LANES = ("commodity_energy", "commodity_wti", "commodity_precious", "commodity_industrial", "commodity_natgas")
MARGINS = (0.0, 0.03, 0.05, 0.08)

rows = st.db.execute(
    "SELECT d.ts,d.ticker,d.lane,d.side,d.contracts,d.stake_usd,w.close_ts,w.strike,w.result FROM decisions d JOIN windows w ON w.ticker=d.ticker "
    f"WHERE d.action='bet' AND d.mode='trade' AND d.contracts>0 AND d.stake_usd>0 AND d.ts>? AND d.lane IN ({','.join('?' * len(LANES))}) ORDER BY d.ts",
    (SINCE, *LANES),
).fetchall()
seen = {}
for r in rows:
    seen[(r["ticker"], r["side"])] = r
rows = list(seen.values())


def actual(r):
    """Net dollars the bet really made: sells and settlement, fee on every leg."""
    o = st.db.execute("SELECT side,fill_count,cost_usd FROM orders WHERE ticker=? AND fill_count>0", (r["ticker"],)).fetchall()
    bought = sum(float(x["fill_count"]) for x in o if x["side"] == "buy")
    sold = sum(float(x["fill_count"]) for x in o if x["side"] == "sell")
    net_cost = sum(float(x["cost_usd"]) for x in o)
    left = bought - sold
    pay = left if r["result"] in ("yes", "no") and r["result"] == r["side"] else 0.0
    return pay - net_cost, (bought if bought else float(r["contracts"])) - (0.0)


out = []
for r in rows:
    ser = r["ticker"].split("-")[0]
    ix = variants.biquote_index(ser)
    if not ix or r["result"] not in ("yes", "no"):
        continue
    close, t0 = float(r["close_ts"]), float(r["ts"])
    tk = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts", (ix, close - 900 - 1100, t0 + 1)).fetchall()
    if len(tk) < 500:
        continue
    T = [float(x[0]) for x in tk]
    V = [float(x[1]) for x in tk]
    opened = close - 900.0
    a = bisect.bisect_left(T, opened - 5.0)
    if a >= len(T) or T[a] > opened + 20.0:
        continue
    ref = V[a]
    price = float(r["stake_usd"]) / float(r["contracts"])
    cur = curvevalue.read(T, V, t0, ref, r["side"], close - t0)
    net, _ = actual(r)
    held = (float(r["contracts"]) if r["result"] == r["side"] else 0.0) - float(r["stake_usd"])
    out.append((t0, r["lane"], ser, r["side"], price, cur, net, held, float(r["stake_usd"])))

print(f"favorites bets since {SINCE:.0f}, settled and readable: {len(out)} of {len(rows)}")
print("  time    lane                  market          side  price  curveP  cushion(typ min)  actual   held")
import time

for t0, lane, ser, side, price, cur, net, held, stake in out:
    tt = time.strftime("%H:%M", time.gmtime(t0 - 5 * 3600))
    ps = f"{cur.p:.3f}" if cur else "  n/a"
    cu = f"{cur.cushion / (cur.rate_long * math.sqrt(60)):+.2f}" if cur and cur.rate_long > 0 else "  n/a"
    print(f"  {tt}  {lane:20s}  {ser:14s}  {side:3s}  {price:.2f}  {ps}   {cu:>8s}        {net:+7.2f}  {held:+7.2f}")
print()
half = len(out) // 2
for m in MARGINS:
    keep = [x for x in out if x[5] is not None and x[5].p - x[4] >= m]
    skip = [x for x in out if x not in keep]
    k_act = sum(x[6] for x in keep)
    k_held = sum(x[7] for x in keep)
    sk_act = sum(x[6] for x in skip)
    h1 = sum(x[6] for x in keep if x in out[:half]) - sum(x[6] for x in out[:half])
    h2 = sum(x[6] for x in keep if x in out[half:]) - sum(x[6] for x in out[half:])
    beat = sum(1 for x in skip if x[6] < 0)
    lost = sum(1 for x in skip if x[6] > 0)
    print(
        f"margin {m:.2f}: buys {len(keep)} of {len(out)} | kept bets made {k_act:+.2f} (held {k_held:+.2f}) | skipped bets made {sk_act:+.2f} "
        f"({beat} losers avoided, {lost} winners missed) | gate vs all: {k_act - sum(x[6] for x in out):+.2f} (halves {h1:+.2f} / {h2:+.2f})"
    )
print(f"all bets as played: {sum(x[6] for x in out):+.2f} on {len(out)} (stake ${sum(x[8] for x in out):.2f})")
