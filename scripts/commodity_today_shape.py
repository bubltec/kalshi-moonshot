"""The commodity markets hour by hour over the last 30 hours, beside the live bets of each hour (2026-11-18, user: "this is clearly a good time in the market or something and it must be identifiable").
For each ET clock hour: the median over the commodity markets that CLOSED in that hour of the same readings as scripts/commodity_streak_shape.py (typical minute, changes of favourite, share of time priced 0.30-0.70, mean |mid-0.5|, contracts traded, trades, busy share, one-sided flow, spread), and the live bets bought in the hour: how many, lost, the average ask, net dollars held to settlement (fee on the buy). Descriptive: the question is whether the good hours look different from the rest, so nothing is chosen.
Run on the host: scripts/cloud.sh run scripts/commodity_today_shape.py"""
import datetime
import math
import statistics

from kalshi_moonshot import config, db

ET = datetime.timezone(datetime.timedelta(hours=-5))
SER = ("KXBRENT15M", "KXWTI15M", "KXNATGAS15M", "KXGOLD15M", "KXSILVER15M", "KXCOPPER15M", "KXPLATINUM15M", "KXPALLADIUM15M")
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
now = datetime.datetime.now(datetime.timezone.utc).timestamp()
since = now - 30 * 3600
wins = st.execute("SELECT ticker, close_ts, result FROM windows WHERE close_ts>? AND close_ts<? AND result IN ('yes','no')", (since, now)).fetchall()
wins = [w for w in wins if w["ticker"].split("-")[0] in SER]
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
    fav = [1 if yb[i] >= 0.5 else -1 if ya[i] <= 0.5 else 0 for i in range(len(ts))]
    fav = [x for x in fav if x != 0]
    flips = sum(1 for i in range(1, len(fav)) if fav[i] != fav[i - 1])
    f = tape.execute("SELECT yes_vol,no_vol,n FROM flow_1s WHERE ticker=? AND ts>=? AND ts<?", (tk, int(close - 900), int(close))).fetchall()
    yv = sum(float(x[0] or 0) for x in f); nv = sum(float(x[1] or 0) for x in f); vol = yv + nv
    rd[tk] = {"close": close, "unit": math.sqrt(sum(x * x for x in dd) / len(dd)) * math.sqrt(60.0) * 100.0, "flips": flips, "contested": sum(1 for x in m if 0.3 < x < 0.7) / len(m), "decisive": sum(abs(x - 0.5) for x in m) / len(m),
              "volume": vol, "trades": sum(float(x[2] or 0) for x in f), "busy": sum(1 for x in f if (x[0] or 0) + (x[1] or 0) > 0) / 900.0, "imbalance": abs(yv - nv) / vol if vol else 0.0, "spread": sum(ya[i] - yb[i] for i in range(len(ts))) / len(ts) * 100.0}
bets = st.execute("SELECT d.ts, d.ticker, d.side, d.ask, d.stake_usd, d.lane, w.result FROM decisions d JOIN windows w ON w.ticker=d.ticker WHERE d.mode='trade' AND d.action='bet' AND d.ts>? AND w.result IN ('yes','no')", (since,)).fetchall()
bets = [b for b in bets if b["ticker"].split("-")[0] in SER and b["ask"]]
hours = {}
for tk, r in rd.items():
    h = datetime.datetime.fromtimestamp(r["close"], ET).replace(minute=0, second=0, microsecond=0)
    hours.setdefault(h, {"m": [], "b": []})["m"].append(r)
for b in bets:
    h = datetime.datetime.fromtimestamp(float(b["ts"]), ET).replace(minute=0, second=0, microsecond=0)
    hours.setdefault(h, {"m": [], "b": []})["b"].append(b)
print(f"window: {datetime.datetime.fromtimestamp(since, ET):%m-%d %H:%M} to {datetime.datetime.fromtimestamp(now, ET):%m-%d %H:%M} ET; {len(rd)} commodity markets, {len(bets)} live bets. Per hour: median over the markets closing in the hour.")
print("hour ET        mkts | unit  flips contested decisive | volume  trades busy  imbal spread | bets lost avgask   net$")
for h in sorted(hours):
    x = hours[h]
    if not x["m"]:
        continue
    med = lambda k: statistics.median(r[k] for r in x["m"])
    nb, lost, ask, net = len(x["b"]), 0, 0.0, 0.0
    for b in x["b"]:
        a, c = float(b["ask"]), float(b["stake_usd"] or 0) / max(float(b["ask"]), 0.01)
        won = b["result"] == b["side"]
        lost += 0 if won else 1; ask += a
        net += c * ((1.0 if won else 0.0) - a - 0.07 * a * (1 - a))
    print(f"{h:%m-%d %H}:00   {len(x['m']):4d} | {med('unit'):5.2f} {med('flips'):4.1f} {med('contested'):8.2f} {med('decisive'):8.3f} | {med('volume'):7.0f} {med('trades'):6.0f} {med('busy'):5.2f} {med('imbalance'):5.2f} {med('spread'):5.2f} | {nb:4d} {lost:4d} {ask / nb if nb else 0:6.3f} {net:+7.2f}")
print("DONE")
