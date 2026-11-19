"""What the bot itself logged at each live commodity buy, wins against losses, and the bets around the run that broke (2026-11-19, user: "something in the way we bought based on that market changed when we lost ... there has to be some kind of indicator we can look at").
My earlier replays recomputed readings from the tape; this one reads what the bot recorded AT the buy: the decision's `factors` (elapsed_min, spread, imbalance, move_90, range_90, activity_60, rise_30, rise_90, dip_90, held_min, updates_60, bid, ask) and the numbers in the `context` and `reason` text (the model's chance for the side, the width of the strike band in percent, the offset basis, the record-adjusted chance and its weight and skill). Bets: live commodity bets (mode trade, action bet) with a settled result since SINCE (default 2026-11-09 00:00 ET).
Part 1: per reading, the mean among won and among lost bets, and the gap in pooled standard deviations (d), with the bets studied. Many readings are tried, so a gap of 0.2 to 0.3 in a few of them is expected by chance; the bar is a gap that is large and shows in both halves of the period (first half of the bets, second half). Part 2: the bets around the last run of RUN_MIN (default 15) or more wins that ended in a loss, ordered, with the key readings, to see what moved before the first loss.
Run on the host: scripts/cloud.sh run scripts/commodity_buy_factors.py [SINCE=<epoch>] [RUN_MIN=15]"""
import datetime
import json
import math
import os
import re

from kalshi_moonshot import config, db

ET = datetime.timezone(datetime.timedelta(hours=-5))
SER = ("BRENT", "WTI", "NATGAS", "GOLD", "SILVER", "COPPER", "PLATINUM", "PALLADIUM")
since = float(os.environ.get("SINCE", datetime.datetime(2026, 11, 9, tzinfo=ET).timestamp()))
run_min = int(os.environ.get("RUN_MIN", "15"))
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
q = st.execute("SELECT d.ts, d.ticker, d.side, d.ask, d.stake_usd, d.lane, d.context, d.factors, d.reason, w.result FROM decisions d JOIN windows w ON w.ticker=d.ticker WHERE d.mode='trade' AND d.action='bet' AND d.ts>? AND w.result IN ('yes','no') ORDER BY d.ts", (since,)).fetchall()
rows = []
for b in q:
    if b["ticker"].split("-")[0][2:-3] not in SER or not b["ask"]:
        continue
    f = b["factors"]
    if isinstance(f, str):
        try:
            f = json.loads(f)
        except ValueError:
            f = None
    f = dict(f) if isinstance(f, dict) else {}
    ctx, reason = str(b["context"] or ""), str(b["reason"] or "")
    r = {k: float(v) for k, v in f.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
    m = re.search(r"P\(yes\) ([0-9.]+)", ctx)
    if m:
        py = float(m.group(1))
        r["model_p_side"] = py if b["side"] == "yes" else 1.0 - py
        r["model_less_ask"] = r["model_p_side"] - float(b["ask"])
    m = re.search(r"width ([0-9.]+)%", ctx)
    if m:
        r["width_pct"] = float(m.group(1))
    m = re.search(r"basis ([+-][0-9.]+)%", ctx)
    if m:
        r["basis_pct"] = float(m.group(1))
    m = re.search(r"([0-9.]+)h left", ctx)
    if m:
        r["hours_left"] = float(m.group(1))
    m = re.search(r"record-adjusted ([0-9.]+) -> ([0-9.]+) \(weight on estimate ([0-9.]+), skill (-?[0-9.]+)", reason)
    if m:
        r["rec_before"], r["rec_after"], r["rec_weight"], r["rec_skill"] = (float(x) for x in m.groups())
    r["ask"] = float(b["ask"])
    r["stake"] = float(b["stake_usd"] or 0)
    rows.append((b, r, b["result"] == b["side"]))
n = len(rows)
print(f"window: {datetime.datetime.fromtimestamp(since, ET):%m-%d %H:%M} ET to now; {n} live commodity bets, {sum(1 for x in rows if x[2])} won.")
keys = sorted({k for _, r, _ in rows for k in r})
half = n // 2


def stats(sel, k):
    v = [r[k] for _, r, w in sel if k in r]
    return v


def d_of(sel, k):
    a = [r[k] for _, r, w in sel if w and k in r]
    b = [r[k] for _, r, w in sel if not w and k in r]
    if len(a) < 20 or len(b) < 8:
        return None
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    va = sum((x - ma) ** 2 for x in a) / max(len(a) - 1, 1)
    vb = sum((x - mb) ** 2 for x in b) / max(len(b) - 1, 1)
    sd = math.sqrt(((len(a) - 1) * va + (len(b) - 1) * vb) / max(len(a) + len(b) - 2, 1))
    return (ma, mb, (ma - mb) / sd if sd > 0 else 0.0, len(a), len(b))


print("reading            won mean   lost mean   gap(sd)  | first half  second half | won/lost bets")
out = []
for k in keys:
    t = d_of(rows, k)
    if t:
        h1, h2 = d_of(rows[:half], k), d_of(rows[half:], k)
        out.append((abs(t[2]), k, t, h1, h2))
for _, k, t, h1, h2 in sorted(out, reverse=True):
    print(f"{k:16s} {t[0]:10.3f} {t[1]:11.3f} {t[2]:+8.2f}  | {h1[2] if h1 else float('nan'):+9.2f} {h2[2] if h2 else float('nan'):+11.2f}  | {t[3]}/{t[4]}")
# the last run of run_min or more wins that ended in a loss
won = [w for _, _, w in rows]
best = None
i = 0
while i < n:
    if won[i]:
        j = i
        while j + 1 < n and won[j + 1]:
            j += 1
        if j - i + 1 >= run_min and j + 1 < n:
            best = (i, j)
        i = j + 1
    else:
        i += 1
if best:
    i, j = best
    print(f"--- the last run of {run_min}+ wins that ended in a loss: {j - i + 1} wins, {datetime.datetime.fromtimestamp(float(rows[i][0]['ts']), ET):%m-%d %H:%M} to {datetime.datetime.fromtimestamp(float(rows[j][0]['ts']), ET):%m-%d %H:%M} ET, then the next bets")
    show = ("ask", "elapsed_min", "spread", "imbalance", "move_90", "range_90", "activity_60", "rise_90", "dip_90", "held_min", "model_p_side", "width_pct", "rec_weight", "rec_skill")
    print("time ET     W/L ticker      " + " ".join(f"{x[:9]:>9s}" for x in show))
    for k in range(max(i, j - 11), min(n, j + 7)):
        b, r, w = rows[k]
        print(f"{datetime.datetime.fromtimestamp(float(b['ts']), ET):%m-%d %H:%M}  {'W' if w else 'L'}  {b['ticker'].split('-')[0][2:-3]:10s} " + " ".join(f"{r.get(x, float('nan')):9.3f}" for x in show))
# Part 3: the same readings against the PRICE: does a reading change the win rate beyond what the ask already says?
print("--- Part 3: each reading cut in thirds of the bets; win rate against the average ask paid (excess = win rate less ask, points), net per $100 staked with the fee, and the excess in each half of the period")
firsthalf = set(id(x) for x in rows[:half])


def excess(sel):
    if len(sel) < 15:
        return float("nan")
    return 100.0 * (sum(1 for _, _, w in sel if w) / len(sel) - sum(r["ask"] for _, r, _ in sel) / len(sel))


for k in ("dip_90", "spread", "imbalance", "range_90", "move_90", "rise_90", "hours_left", "elapsed_min", "model_p_side", "model_less_ask", "width_pct", "held_min", "activity_60", "ask"):
    have = sorted((x for x in rows if k in x[1]), key=lambda x: x[1][k])
    if len(have) < 150:
        continue
    t = len(have) // 3
    print(f"  {k}")
    for name, sel in (("low", have[:t]), ("middle", have[t:2 * t]), ("high", have[2 * t:])):
        stake = sum(r["ask"] for _, r, _ in sel)
        net = sum((1.0 if w else 0.0) - r["ask"] - 0.07 * r["ask"] * (1 - r["ask"]) for _, r, w in sel)
        h1 = [x for x in sel if id(x) in firsthalf]
        h2 = [x for x in sel if id(x) not in firsthalf]
        print(f"    {name:6s} ({sel[0][1][k]:8.3f} to {sel[-1][1][k]:8.3f}) bets {len(sel):4d} | avg ask {100.0 * stake / len(sel):5.1f}c win {100.0 * sum(1 for _, _, w in sel if w) / len(sel):5.1f}% excess {excess(sel):+5.1f} | net {100.0 * net / stake:+5.1f} per $100 | excess first half {excess(h1):+5.1f}, second {excess(h2):+5.1f}")
print("DONE")
