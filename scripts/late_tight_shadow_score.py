"""Would the late_tight sale have beaten what the live commodity bets did? (2026-11-19, user: "build the new step as a module concept"; the lead is studies/results/2026-11-19-commodity-tight-sell-last150.txt)
One question on the same bets (CLAUDE.md rule 4): every bet on a lane with the sell step late_tight in shadow wrote one row (kind "late tight" in data/risk_shadow.jsonl) the first time its bid was K typical minutes under its high since the buy in the last share of the market (ahead or not): `bid` is what it would have sold at. What actually happened = the bet's own exit (exit_price, fee on the sale) or settlement. The buy and its fee are the same in both, so only the exit leg differs. A bet the lane's own exit sold BEFORE the row's time is left out (an earlier sale stands). Taker fee on the would-sell.
Reported: rows scored, rule less actual in dollars per contract and per $100 staked, bets it beat and lost to what happened, how many sold bets went on to win (the cost of a sale of a bet that would have paid), the bets that ended under 0.10 (near-total losses) with and without the rule, all losses and all gains with and without, and the worst bet. By lane group and by the bot's own vol tag in the buy's reason (thirds) once there are enough rows (rule 4: a positive result in each shape before it goes live). The rule retires under rule 6 if its score turns negative over a week of live bets.
Run on the host: scripts/cloud.sh run scripts/late_tight_shadow_score.py [SINCE=<epoch>]"""
import datetime
import json
import os
import pathlib
import re

from kalshi_moonshot import config, db

ET = datetime.timezone(datetime.timedelta(hours=-5))
s = config.load()
f = pathlib.Path(s.data_dir) / "risk_shadow.jsonl"
since = float(os.environ.get("SINCE", "0"))
rows = []
for text in f.read_text().splitlines() if f.exists() else []:
    try:
        x = json.loads(text)
    except ValueError:
        continue
    if x.get("kind") == "late tight" and not x.get("live") and x["ts"] >= since:
        rows.append(x)
now = datetime.datetime.now(ET)
if not rows:
    print(f"window: none yet. No late tight shadow rows in {f} as of {now:%m-%d %H:%M} ET; 0 rows. A row is written once a bet on a lane with late_tight in its sell chain has its bid K typical minutes under its high in the last sixth of the market: nothing to score.")
    print("DONE")
    raise SystemExit(0)
st = db.connect(s.db_path, "main", shared=True)


def fee(p):
    return 0.07 * p * (1.0 - p)


done = []
for x in rows:
    w = st.execute("SELECT result FROM windows WHERE ticker=? AND result IN ('yes','no')", (x["ticker"],)).fetchone()
    if not w:
        continue
    d = st.execute("SELECT ask, exit_price, exit_ts, reason FROM decisions WHERE ticker=? AND lane=? AND mode='trade' AND action='bet' AND ts<=? ORDER BY ts DESC LIMIT 1", (x["ticker"], x["lane"], x["ts"])).fetchone()
    if not d:
        continue
    won = w["result"] == x["side"]
    sold = d["exit_price"] is not None
    if sold and d["exit_ts"] is not None and float(d["exit_ts"]) < float(x["ts"]) - 1.0:
        continue  # the lane's own exit sold first
    actual = (float(d["exit_price"]) - fee(float(d["exit_price"]))) if sold else (1.0 if won else 0.0)
    rule = float(x["bid"]) - fee(float(x["bid"]))
    m = re.search(r"\[vol ([0-9.]+)x\]", str(d["reason"] or ""))
    done.append({"x": x, "ask": float(d["ask"] or x["entry"]), "won": won, "actual": actual, "rule": rule, "vol": float(m.group(1)) if m else None, "lane": x["lane"]})
print(f"window: {datetime.datetime.fromtimestamp(min(x['ts'] for x in rows), ET):%m-%d %H:%M} to {now:%m-%d %H:%M} ET; {len(rows)} shadow rows, {len(done)} scored (settled, not sold earlier by the lane's own exit). One contract a row.")


def show(label, sel):
    if not sel:
        print(f"  {label:26s} rows 0")
        return
    stake = sum(r["ask"] for r in sel)
    d = [r["rule"] - r["actual"] for r in sel]
    a = [r["actual"] - r["ask"] for r in sel]
    b = [r["rule"] - r["ask"] for r in sel]
    print(f"  {label:26s} rows {len(sel):4d} | rule less actual {sum(d):+7.2f} ({100.0 * sum(d) / stake:+5.1f} per $100) | beat {sum(1 for v in d if v > 1e-9):3d} lost {sum(1 for v in d if v < -1e-9):3d} | sold bets that won {sum(1 for r in sel if r['won']):3d} | under 0.10: {sum(1 for r in sel if r['actual'] < 0.10)} -> {sum(1 for r in sel if r['rule'] < 0.10)} | losses {sum(v for v in a if v < 0):+.2f} -> {sum(v for v in b if v < 0):+.2f} | gains {sum(v for v in a if v > 0):+.2f} -> {sum(v for v in b if v > 0):+.2f} | worst {min(a):+.2f} -> {min(b):+.2f}")


show("all", done)
vols = sorted(r["vol"] for r in done if r["vol"] is not None)
if len(vols) >= 30:
    lo, hi = vols[len(vols) // 3], vols[2 * len(vols) // 3]
    show(f"vol tag under {lo:.2f}", [r for r in done if r["vol"] is not None and r["vol"] <= lo])
    show(f"vol tag {lo:.2f} to {hi:.2f}", [r for r in done if r["vol"] is not None and lo < r["vol"] <= hi])
    show(f"vol tag over {hi:.2f}", [r for r in done if r["vol"] is not None and r["vol"] > hi])
print("DONE")
