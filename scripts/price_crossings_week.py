"""Do the live commodity buys made while Kalshi's own price was weaving across 50c lose? (2026-11-16, user, on KXSILVER15M-26NOV161515 YES bought at 0.60 and sold at 0.16: "Yes, trace the buy"; "Yes, build it".)
The crossing check the commodity lanes run (max_spot_crosses) counts the outside spot against the strike. The gate that counts the market's own mid against 50c already exists (lane field max_crossings,
ticks.MidHistory.crossings: the last 300 s on a 5 s grid) and no lane sets it. One question on the bot's own bets: every market the live commodity lanes bought since SINCE (default Monday 2026-11-09, the
logic rewrite), the first live buy of each, split by that gate's own count at the moment of the buy. The result is what actually happened on the market, from the settlements table (every buy and sale,
fees in). For each candidate setting: the bets the gate would have refused, how many of them lost (the rule beats what happened) and won (the rule loses to it), and the dollars. By curve shape: the
variety letter the buy's own note carries. Read-only. The lanes' settings changed several times inside the window.
    scripts/cloud.sh run scripts/price_crossings_week.py [SINCE=2026-11-09]
"""
import collections, datetime as dt, os, re
from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.ticks import MidHistory

s = config.reload(); st = Store(s.db_path, s.exclude_versions); tape = db.connect(s.tape_path, "tape", shared=True)
ET = dt.timezone(dt.timedelta(hours=-5))
since = dt.datetime.strptime(os.environ.get("SINCE", "2026-11-09"), "%Y-%m-%d").replace(tzinfo=ET).timestamp()
LANES = ("commodity_precious", "commodity_industrial", "commodity_energy", "commodity_wti", "commodity_natgas")
marks = ",".join("?" for _ in LANES)
bets = st.db.execute(f"SELECT ticker, MIN(ts) AS ts FROM decisions WHERE action='bet' AND mode='trade' AND ts>? AND lane IN ({marks}) GROUP BY ticker ORDER BY 2", (since, *LANES)).fetchall()
rows = []
for b in bets:
    tk, ts = b["ticker"], float(b["ts"])
    stl = st.db.execute("SELECT result, yes_count, no_count, yes_cost, no_cost, fee FROM settlements WHERE ticker=?", (tk,)).fetchall()
    if not stl or any(r["result"] not in ("yes", "no") for r in stl):
        continue
    pnl = sum(float(r["yes_count"] if r["result"] == "yes" else r["no_count"]) - float(r["yes_cost"]) - float(r["no_cost"]) - float(r["fee"]) for r in stl)
    note = st.db.execute("SELECT reason FROM decisions WHERE ticker=? AND ts=? AND action='bet' AND mode='trade'", (tk, b["ts"])).fetchone()["reason"] or ""
    m = re.search(r"\[variety ([A-H])", note)
    h = MidHistory()
    for qts, bid, ask in tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts>? AND ts<=? AND bid>0 AND ask>0 ORDER BY ts", (tk, ts - 330.0, ts)):
        h.note(tk, float(qts), float(bid), float(ask))
    rows.append((tk, ts, h.crossings(tk, ts), pnl, m[1] if m else "?"))

day = lambda t: dt.datetime.fromtimestamp(t, ET).strftime("%m-%d %H:%M")
print(f"live commodity markets bought {day(rows[0][1])} to {day(rows[-1][1])} ET, settled: {len(rows)}; net ${sum(r[3] for r in rows):+.2f}")
print("\ncrossings of 50c in the 300 s before the buy (the gate's own count)")
print(f"{'count':>8} {'n':>4} {'won':>4} {'net $':>9} {'per bet':>8}")
by = collections.defaultdict(list)
for r in rows:
    by["no reading" if r[2] is None else str(min(r[2], 6)) + ("+" if r[2] >= 6 else "")].append(r[3])
for k in sorted(by):
    v = by[k]
    print(f"{k:>8} {len(v):4d} {sum(1 for x in v if x > 0):4d} {sum(v):+9.2f} {sum(v) / len(v):+8.2f}")

print("\nwhat max_crossings = N would have refused (a count over N; no reading is refused too)")
print(f"{'N':>3} {'refused':>8} {'lost (rule beats)':>18} {'won (rule loses)':>17} {'their net $':>12} {'change $':>9}")
for n in (0, 1, 2, 3, 4):
    out = [r[3] for r in rows if r[2] is None or r[2] > n]
    print(f"{n:3d} {len(out):8d} {sum(1 for x in out if x <= 0):18d} {sum(1 for x in out if x > 0):17d} {sum(out):+12.2f} {-sum(out):+9.2f}")

for n in (2, 3):
    print(f"\nby the variety in the buy's note, max_crossings = {n}: kept / refused")
    print(f"{'variety':>8} {'kept n':>7} {'kept $':>9} {'refused n':>10} {'beat':>5} {'lost to':>8} {'refused $':>10}")
    for v in sorted({r[4] for r in rows}):
        kept = [r[3] for r in rows if r[4] == v and r[2] is not None and r[2] <= n]
        out = [r[3] for r in rows if r[4] == v and (r[2] is None or r[2] > n)]
        print(f"{v:>8} {len(kept):7d} {sum(kept):+9.2f} {len(out):10d} {sum(1 for x in out if x <= 0):5d} {sum(1 for x in out if x > 0):8d} {sum(out):+10.2f}")

print("\nthe refused bets at max_crossings = 3")
for tk, ts, c, pnl, v in rows:
    if c is None or c > 3:
        print(f"  {day(ts)} {tk:32s} variety {v} crossings {c} net {pnl:+.2f}")
print("\nthe bet that raised the question, and the no-reading bets by day (no quotes in the tape for the 300 s: the recorder was not writing)")
for tk, ts, c, pnl, v in rows:
    if tk.startswith("KXSILVER15M-26NOV161515"):
        print(f"  {day(ts)} {tk} variety {v} crossings {c} net {pnl:+.2f}")
nr = collections.Counter(day(r[1])[:5] for r in rows if r[2] is None)
print("  no reading: " + ", ".join(f"{d} {n}" for d, n in sorted(nr.items())) + f"; their net ${sum(r[3] for r in rows if r[2] is None):+.4f}")
print("DONE")
