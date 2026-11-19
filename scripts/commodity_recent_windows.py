"""The last HOURS (default 8) of commodity markets window by window beside the live bets of each window (2026-11-19, user: "let's look back over the past 8 hours. We had some bad periods").
Per 15-minute window: the readings pooled over the commodity markets that CLOSED in it (median typical minute in cents, median changes of favourite, median share of time priced 0.30-0.70, share of seconds with a trade, median spread), the same readings of the window before it (the lead-in, known before the bets), and the live bets bought in the window: how many, lost, average ask, net dollars held to settlement with the buy fee at the stake actually used. Descriptive; the bad windows are marked after seeing them (net under zero).
Run on the host: scripts/cloud.sh run scripts/commodity_recent_windows.py [HOURS=8]"""
import datetime
import math
import os
import statistics

from kalshi_moonshot import config, db

ET = datetime.timezone(datetime.timedelta(hours=-5))
SER = ("KXBRENT15M", "KXWTI15M", "KXNATGAS15M", "KXGOLD15M", "KXSILVER15M", "KXCOPPER15M", "KXPLATINUM15M", "KXPALLADIUM15M")
hours = float(os.environ.get("HOURS", "8"))
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
now = datetime.datetime.now(datetime.timezone.utc).timestamp()
since = now - hours * 3600 - 900
wins = [w for w in st.execute("SELECT ticker, close_ts, result FROM windows WHERE close_ts>? AND close_ts<? AND result IN ('yes','no')", (since, now)).fetchall() if w["ticker"].split("-")[0] in SER]
rd = {}
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
    m = [x for x in ms if x is not None]
    dd = [ms[i + 1] - ms[i] for i in range(len(ms) - 1) if ms[i] is not None and ms[i + 1] is not None]
    if len(dd) < 300 or len(m) < 300:
        continue
    fav = [x for x in (1 if yb[i] >= 0.5 else -1 if ya[i] <= 0.5 else 0 for i in range(len(ts))) if x != 0]
    f = tape.execute("SELECT yes_vol,no_vol FROM flow_1s WHERE ticker=? AND ts>=? AND ts<?", (tk, int(close - 900), int(close))).fetchall()
    rd[tk] = {"slot": int(close // 900), "unit": math.sqrt(sum(x * x for x in dd) / len(dd)) * math.sqrt(60.0) * 100.0, "flips": sum(1 for i in range(1, len(fav)) if fav[i] != fav[i - 1]),
              "contested": sum(1 for x in m if 0.3 < x < 0.7) / len(m), "busy": sum(1 for x in f if (x[0] or 0) + (x[1] or 0) > 0) / 900.0, "spread": sum(ya[i] - yb[i] for i in range(len(ts))) / len(ts) * 100.0}
bets = [b for b in st.execute("SELECT d.ts, d.ticker, d.side, d.ask, d.stake_usd, w.result, w.close_ts FROM decisions d JOIN windows w ON w.ticker=d.ticker WHERE d.mode='trade' AND d.action='bet' AND d.ts>? AND w.result IN ('yes','no')", (since,)).fetchall() if b["ticker"].split("-")[0] in SER and b["ask"]]
slots = {}
for r in rd.values():
    slots.setdefault(r["slot"], {"m": [], "b": []})["m"].append(r)
for b in bets:
    slots.setdefault(int(float(b["close_ts"]) // 900), {"m": [], "b": []})["b"].append(b)


def med(x, k):
    return statistics.median(r[k] for r in x["m"]) if x["m"] else float("nan")


print(f"window: last {hours:g} h to {datetime.datetime.fromtimestamp(now, ET):%m-%d %H:%M} ET; {len(rd)} commodity markets, {len(bets)} live bets. Window = the 15 minutes the markets close at the end of; 'lead' = the window before.")
print("close ET   mkts | unit flips contested busy spread | lead: unit flips contested busy | bets lost avgask    net$")
tot = {}
for sl in sorted(slots):
    if sl * 900 < now - hours * 3600:
        continue
    x, p = slots[sl], slots.get(sl - 1, {"m": [], "b": []})
    nb, lost, ask, net = len(x["b"]), 0, 0.0, 0.0
    for b in x["b"]:
        a = float(b["ask"]); c = float(b["stake_usd"] or 0) / max(a, 0.01); won = b["result"] == b["side"]
        lost += 0 if won else 1; ask += a; net += c * ((1.0 if won else 0.0) - a - 0.07 * a * (1 - a))
    mark = " <-- bad" if net < 0 else ""
    print(f"{datetime.datetime.fromtimestamp((sl + 1) * 900, ET):%m-%d %H:%M} {len(x['m']):4d} | {med(x, 'unit'):5.1f} {med(x, 'flips'):4.1f} {med(x, 'contested'):8.2f} {med(x, 'busy'):4.2f} {med(x, 'spread'):5.2f} | {med(p, 'unit'):9.1f} {med(p, 'flips'):4.1f} {med(p, 'contested'):8.2f} {med(p, 'busy'):4.2f} | {nb:4d} {lost:4d} {ask / nb if nb else 0:6.3f} {net:+7.2f}{mark}")
print("DONE")
