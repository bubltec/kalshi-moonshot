"""Which lanes were really live since Monday? (2026-11-13, user: "Crypto underdog is not running live".) The earlier studies called every decision with mode='trade' a live bet and the real-cash table gave each ticker the lane of its first decision. Question: per lane, how many decisions since Monday 11-09 00:00 ET were mode 'trade' and when (first and last, ET), how many 'observe', and how many have a real buy order on the same ticker within 60 s after the decision? Then the real cash again, with an order given to a lane only when that lane has a trade-mode bet on the ticker with an order within 60 s.
    scripts/cloud_job.sh scripts/live_lane_check.py
"""
import calendar, collections, datetime as dt, os
from kalshi_moonshot import config
from kalshi_moonshot.store import Store

os.nice(10)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
ET = dt.timezone(dt.timedelta(hours=-5))
hm = lambda t: dt.datetime.fromtimestamp(t, ET).strftime("%m-%d %H:%M")  # noqa: E731
MON = calendar.timegm((2026, 11, 9, 5, 0, 0))
print("host settings: mode", s.mode, "| live_lanes", sorted(s.live_lanes) if s.live_lanes is not None else None, "| paper_lanes", s.paper_lanes)
dec = st.db.execute("SELECT ts,ticker,lane,mode,side,stake_usd FROM decisions WHERE action='bet' AND ts>? AND ticker LIKE '%15M-%' ORDER BY ts", (MON,)).fetchall()
orders = st.db.execute("SELECT ts,ticker,cost_usd,fill_count,side,contract FROM orders WHERE ts>? AND ticker LIKE '%15M-%' AND fill_count>0 ORDER BY ts", (MON,)).fetchall()
buys = collections.defaultdict(list)
for o in orders:
    if o["side"] == "buy":
        buys[o["ticker"]].append(float(o["ts"]))
rows = collections.defaultdict(lambda: collections.defaultdict(list))
for d in dec:
    rows[d["lane"]][d["mode"]].append(d)
print(f"\n{'lane':30s} {'trade':>6s} {'observe':>8s} {'trade w/ order<=60s':>20s}  trade-mode first .. last (ET)")
real = {}  # ticker -> lane that placed the real buy
for lane in sorted(rows):
    tr, ob = rows[lane].get("trade", []), rows[lane].get("observe", [])
    withorder = [d for d in tr if any(0 <= t - float(d["ts"]) <= 60 for t in buys.get(d["ticker"], ()))]
    for d in withorder:
        real.setdefault(d["ticker"], lane)
    span = f"{hm(float(tr[0]['ts']))} .. {hm(float(tr[-1]['ts']))}" if tr else "-"
    print(f"{lane:30s} {len(tr):6d} {len(ob):8d} {len(withorder):20d}  {span}")
print("other modes:", dict(collections.Counter(d["mode"] for d in dec)))
unmatched = [t for t in buys if t not in real]
print(f"\nmarkets with a real buy: {len(buys)}; matched to a lane's trade-mode bet: {len(real)}; no matching bot bet (hand-placed or other): {len(unmatched)}")
for lane in sorted({v for v in real.values()}):
    print(f"   {lane:30s} {sum(1 for v in real.values() if v == lane):4d} markets with a real buy")
print("DONE")
