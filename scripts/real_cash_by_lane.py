"""What did the account really make, by lane, from its own orders and settlements? (2026-11-13, user: "this can't be right. i have been manually betting and losing and the commodities has been replenishing it".)
Question, one: since 2026-11-12 00:00 ET (and, for the longer view, since Monday 11-09 00:00 ET), per lane, what is the real cash result of the 15-minute markets the account traded, read from the `orders` table (buys and sells that filled) and `settlements` (what was held when each market settled), not from the decisions table's own price model?
Net per market = - buy cost (cost_usd, fee included) + sale proceeds (minus the sell row's cost_usd, which is stored negative and already net of its fee: the sample lines show it) + the payout of what was still held at the close (bought minus sold, from the orders' own counts, at $1 if it is the winning contract). NOT settlements.yes_count / no_count: those include contracts already sold (353 of the 354 markets where the two differ had a sale; using them overstated the payout by $1,644 over 800 markets). Both are printed.
A market's lane is the lane of the bot decision on that ticker; a market with orders and no bot decision is a hand-placed one and is its own line, 'no bot decision (hand-placed)'. Markets still open at the tape's end are counted and left out.
Also printed, against the decisions-table result the earlier studies used for the same markets: per lane, real cash vs the modelled dollars, so the gap is visible.
    scripts/cloud_job.sh scripts/real_cash_by_lane.py
"""

import calendar
import collections
import datetime as dt
import os

from kalshi_moonshot import config
from kalshi_moonshot.runner import EXIT_SLIP
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

os.nice(10)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
ET = dt.timezone(dt.timedelta(hours=-5))
hm = lambda t: dt.datetime.fromtimestamp(t, ET).strftime("%m-%d %H:%M")  # noqa: E731
MON = calendar.timegm((2026, 11, 9, 5, 0, 0))
YDAY = calendar.timegm((2026, 11, 12, 5, 0, 0))

cols = [r[1] for r in st.db.execute("PRAGMA table_info(orders)").fetchall()] if hasattr(st.db, "execute") else []
print("orders columns:", cols)
orders = st.db.execute("SELECT ts,ticker,env,count,price,fill_count,cost_usd,fee_usd,side,contract FROM orders WHERE ts>? AND ticker LIKE '%15M-%' AND fill_count>0 ORDER BY ts", (MON,)).fetchall()
print(f"filled 15-minute orders since Monday: {len(orders)}; envs: {dict(collections.Counter(o['env'] for o in orders))}; sides: {dict(collections.Counter(o['side'] for o in orders))}")
for o in [x for x in orders if x["side"] == "sell"][:3]:
    print(f"   sample sell: count {o['count']} price {o['price']} filled {o['fill_count']} cost_usd {o['cost_usd']} fee_usd {o['fee_usd']}  (price x filled = {float(o['price']) * float(o['fill_count']):.3f})")

lane_of = {}
for r in st.db.execute("SELECT ticker,lane,mode FROM decisions WHERE action='bet' AND ts>? AND ticker LIKE '%15M-%' ORDER BY ts", (MON,)).fetchall():
    lane_of.setdefault(r["ticker"], (r["lane"], r["mode"]))
sett = {}
for r in st.db.execute("SELECT ticker,result,yes_count,no_count FROM settlements WHERE settled_ts>?", (MON,)).fetchall():
    sett[r["ticker"]] = r
res = {r["ticker"]: r["result"] for r in st.db.execute("SELECT ticker,result FROM windows WHERE close_ts>? AND result IN ('yes','no')", (MON,)).fetchall()}

per = collections.defaultdict(lambda: dict(buy=0.0, sell=0.0, pay=0.0, first=None, held={"yes": 0.0, "no": 0.0}))
for o in orders:
    p = per[o["ticker"]]
    p["first"] = p["first"] or float(o["ts"])
    p["held"][o["contract"] or "yes"] += float(o["fill_count"]) * (-1.0 if o["side"] == "sell" else 1.0)
    if o["side"] == "sell":
        p["sell"] -= float(o["cost_usd"])  # a sell row stores its net proceeds (price x count less the fee) as a NEGATIVE cost_usd: -1.374 against 0.48 x 2.97 = 1.426 less 0.052
    else:
        p["buy"] += float(o["cost_usd"])
        p["contract"] = o["contract"]
open_, done = 0, {}
for tk, p in per.items():
    if tk not in res and tk not in sett:
        open_ += 1
        continue
    r = sett.get(tk)
    result = (r["result"] if r and r["result"] else res.get(tk))
    p["pay_settlements"] = 0.0 if r is None else (float(r["yes_count"] or 0.0) if result == "yes" else float(r["no_count"] or 0.0) if result == "no" else 0.0)
    p["pay"] = max(p["held"].get(result, 0.0), 0.0) if result in ("yes", "no") else 0.0  # what the orders say was still held at the close, paid $1 each if it is the winning contract
    p["net"] = p["sell"] + p["pay"] - p["buy"]
    p["net_settlements"] = p["sell"] + p["pay_settlements"] - p["buy"]
    p["lane"] = lane_of.get(tk, ("no bot decision (hand-placed)", None))[0]
    done[tk] = p
print(f"markets traded: {len(per)}; settled: {len(done)}; open/unsettled at the tape's end: {open_}; with a settlements row: {sum(1 for t in done if t in sett)}")

pay_o = sum(p["pay"] for p in done.values())
pay_s = sum(p["pay_settlements"] for p in done.values())
print(f"payout at the close, from the orders' own counts (bought - sold): ${pay_o:.2f}; from the settlements table's yes_count/no_count: ${pay_s:.2f}; buys ${sum(p['buy'] for p in done.values()):.2f}; sales ${sum(p['sell'] for p in done.values()):.2f}")
diff = [(tk, p) for tk, p in done.items() if abs(p["pay"] - p["pay_settlements"]) > 0.01]
print(f"markets where the two payouts disagree: {len(diff)} of {len(done)}; of those, with a sale: {sum(1 for _, p in diff if p['sell'] > 0)}")
for tk, p in diff[:5]:
    print(f"   {tk} held by orders {p['held']} settlements-table payout {p['pay_settlements']:.2f}, orders payout {p['pay']:.2f}, buy {p['buy']:.2f}, sell {p['sell']:.2f}")

# modelled dollars for the same markets, the way the earlier studies booked them
mod = collections.defaultdict(float)
for d in st.db.execute(
    "SELECT d.ticker,d.lane,d.side,d.ask,d.stake_usd,d.exit_price,d.exit_reason,w.result FROM decisions d JOIN windows w ON w.ticker=d.ticker WHERE d.action='bet' AND d.mode='trade' AND d.stake_usd>0 AND d.ask>0 AND d.ts>? AND w.result IN ('yes','no')",
    (MON,),
).fetchall():
    ask, stake = float(d["ask"]), float(d["stake_usd"])
    cost = ask + fee(ask)
    if d["exit_price"] is not None:
        px = float(d["exit_price"])
        back = px if str(d["exit_reason"] or "").startswith("curve exit (resting ask)") else px - EXIT_SLIP - fee(px)
    else:
        back = 1.0 if d["result"] == d["side"] else 0.0
    mod[(d["ticker"], d["lane"])] += stake * (back / cost - 1.0)

for label, since in (("SINCE 11-12 00:00 ET (yesterday on)", YDAY), ("SINCE MONDAY 11-09 00:00 ET", MON)):
    print(f"\n=== {label}: real cash per lane (settled markets whose first order was after the start)")
    lanes = collections.defaultdict(list)
    for tk, p in done.items():
        if p["first"] >= since:
            lanes[p["lane"]].append((tk, p))
    tot = [0.0, 0.0, 0.0]
    for lane in sorted(lanes, key=lambda x: sum(p["net"] for _, p in lanes[x])):
        g = lanes[lane]
        buy, net = sum(p["buy"] for _, p in g), sum(p["net"] for _, p in g)
        m = sum(v for (tk, ln), v in mod.items() if ln == lane and tk in {t for t, _ in g})
        win = sum(1 for _, p in g if p["net"] > 0)
        print(f"   {lane:32s} markets {len(g):4d} | bought ${buy:8.2f} | real net {net:+8.2f} ({net / buy:+.1%} on the buys) | {win} up | the decisions-table model said {m:+8.2f}")
        tot[0] += buy
        tot[1] += net
        tot[2] += m
    print(f"   {'ALL':32s} bought ${tot[0]:8.2f} | real net {tot[1]:+8.2f} | model {tot[2]:+8.2f}")
    hand = [p for g in lanes.get("no bot decision (hand-placed)", []) for p in [g[1]]]
    print(f"   hand-placed markets: {len(hand)}")
print("DONE")
