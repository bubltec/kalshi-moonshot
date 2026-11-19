"""The last HOURS (default 8) of commodity markets, each market's own typical minute window by window, with the live bets marked (2026-11-19, user: "oil and gas may have been too volatile. check the tape").
Per 15-minute window (the markets that close at its end) one cell per market: the market's typical minute in cents (rms one-second mid move times sqrt(60)) and its changes of favourite; a live bet in that market is marked W (won) or L (lost) with its ask. The series is a label for reading the table, not a rule: the question is whether the losing bets sit in the markets with the highest typical minute in their window (a curve reading), and whether those are the same few series.
Run on the host: scripts/cloud.sh run scripts/commodity_recent_by_market.py [HOURS=8]"""
import datetime
import math
import os

from kalshi_moonshot import config, db

ET = datetime.timezone(datetime.timedelta(hours=-5))
SER = ("BRENT", "WTI", "NATGAS", "GOLD", "SILVER", "COPPER", "PLATINUM", "PALLADIUM")
hours = float(os.environ.get("HOURS", "8"))
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
now = datetime.datetime.now(datetime.timezone.utc).timestamp()
since = now - hours * 3600
wins = [w for w in st.execute("SELECT ticker, close_ts, result FROM windows WHERE close_ts>? AND close_ts<? AND result IN ('yes','no')", (since, now)).fetchall() if w["ticker"].split("-")[0][2:-3] in SER]
bets = {}
for b in st.execute("SELECT d.ticker, d.side, d.ask, w.result FROM decisions d JOIN windows w ON w.ticker=d.ticker WHERE d.mode='trade' AND d.action='bet' AND d.ts>? AND w.result IN ('yes','no')", (since - 900,)).fetchall():
    if b["ask"]:
        bets.setdefault(b["ticker"], []).append(("W" if b["result"] == b["side"] else "L", float(b["ask"])))
cells = {}
for w in wins:
    tk, close = w["ticker"], float(w["close_ts"])
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts<? AND bid>0 AND ask>0 ORDER BY ts", (tk, close)).fetchall()
    if len(q) < 120:
        continue
    ts = [float(x[0]) for x in q]; yb = [float(x[1]) for x in q]; ya = [float(x[2]) for x in q]
    ms, j = [], 0
    for sec in range(int(close) - 900, int(close)):
        while j + 1 < len(ts) and ts[j + 1] <= sec:
            j += 1
        ms.append((yb[j] + ya[j]) / 2.0 if ts[j] <= sec else None)
    dd = [ms[i + 1] - ms[i] for i in range(len(ms) - 1) if ms[i] is not None and ms[i + 1] is not None]
    if len(dd) < 300:
        continue
    fav = [x for x in (1 if yb[i] >= 0.5 else -1 if ya[i] <= 0.5 else 0 for i in range(len(ts))) if x != 0]
    cells[tk] = (int(close), math.sqrt(sum(x * x for x in dd) / len(dd)) * math.sqrt(60.0) * 100.0, sum(1 for i in range(1, len(fav)) if fav[i] != fav[i - 1]))
print(f"window: last {hours:g} h to {datetime.datetime.fromtimestamp(now, ET):%m-%d %H:%M} ET. Cell = typical minute (cents) / changes of favourite, then W or L and the ask for a live bet.")
print("close ET    " + " ".join(f"{x:>15s}" for x in SER))
byclose = {}
for tk, (c, u, f) in cells.items():
    byclose.setdefault(c, {})[tk.split("-")[0][2:-3]] = (tk, u, f)
pu = {}
rank_loss, rank_win = [], []
for c in sorted(byclose):
    row = byclose[c]
    units = sorted(v[1] for v in row.values())
    out = []
    for sr in SER:
        if sr not in row:
            out.append(f"{'-':>15s}")
            continue
        tk, u, f = row[sr]
        bt = "".join(f" {k}{a:.2f}" for k, a in bets.get(tk, []))
        out.append(f"{u:5.1f}/{f:<2d}{bt:>7s}".rjust(15))
        for k, a in bets.get(tk, []):
            (rank_loss if k == "L" else rank_win).append(sum(1 for x in units if x <= u) / len(units))
    print(f"{datetime.datetime.fromtimestamp(c, ET):%m-%d %H:%M}  " + " ".join(out))
print(f"rank of the bet's typical minute among the 8 markets of its window (0 = calmest, 1 = wildest): lost bets {len(rank_loss)} avg {sum(rank_loss) / max(len(rank_loss), 1):.2f}; won bets {len(rank_win)} avg {sum(rank_win) / max(len(rank_win), 1):.2f}")
print("DONE")
