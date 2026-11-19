"""Does the context score's shortfall predict the live bets that lose beyond their price? (2026-11-19, user: "yes", to a shadow step for the readings together; the instrument is src/kalshi_moonshot/contextscore.py)
One question on the same bets (CLAUDE.md rule 4): every settled live bet whose row carries the note "[context score (shadow): wins P against the ask A, shortfall S, Nth percentile of M training bets]". The note was written when the row was, from a model fitted on EARLIER bets only (it is refitted daily), so each score is out of sample. Scores: log loss of the model's chance against the price alone; the net per $100 staked, held to settlement (buy fee) and as traded (the bet's own exit, fee on the sale), of the bets in the top fifth of the shortfall (percentile 80 or more) against the other four fifths, and the top tenth; days positive of the days seen; and the same split by the bot's own vol tag. The rule it would become: skip a bet in the top fifth. It gates nothing; the live switch is the user's after at least a week of live bets and a positive result across the days and the vol thirds (CLAUDE.md rules 4 and 5). Retire under rule 6 if the top fifth does not lose more than the rest over its window.
Run on the host: scripts/cloud.sh run scripts/context_score_forward.py [SINCE=<epoch>]"""
import datetime
import math
import os
import re

from kalshi_moonshot import config, db

ET = datetime.timezone(datetime.timedelta(hours=-5))
since = float(os.environ.get("SINCE", "0"))
s = config.load()
st = db.connect(s.db_path, "main", shared=True)


def fee(p):
    return 0.07 * p * (1.0 - p)


rows = []
for d in st.execute("SELECT d.ts, d.ask, d.side, d.stake_usd, d.exit_price, d.reason, w.result FROM decisions d JOIN windows w ON w.ticker=d.ticker WHERE d.mode='trade' AND d.action='bet' AND d.stake_usd>0 AND d.ts>? AND w.result IN ('yes','no') AND d.reason LIKE '%[context score (shadow)%' ORDER BY d.ts", (since,)).fetchall():
    m = re.search(r"wins ([0-9.]+) against the ask ([0-9.]+), shortfall ([+-][0-9.]+), ([0-9.]+)th percentile of ([0-9]+)", str(d["reason"]))
    if not m:
        continue
    p, ask = float(m.group(1)), float(d["ask"])
    won = 1 if d["result"] == d["side"] else 0
    held = won - ask - fee(ask)
    sold = d["exit_price"] is not None
    trad = ((float(d["exit_price"]) - fee(float(d["exit_price"]))) if sold else float(won)) - ask - fee(ask)
    v = re.search(r"\[vol ([0-9.]+)x\]", str(d["reason"]))
    k = float(d["stake_usd"]) / ask
    rows.append({"ts": float(d["ts"]), "p": p, "ask": ask, "won": won, "pct": float(m.group(4)), "held": held * k, "trad": trad * k, "stake": float(d["stake_usd"]), "vol": float(v.group(1)) if v else None})
now = datetime.datetime.now(ET)
if not rows:
    print(f"window: none yet. No settled live bets with a context score note as of {now:%m-%d %H:%M} ET; 0 rows. The note is written when a bet's row is, on a lane whose buy module has the context_score step and once a model has been fitted (daily): nothing to score.")
    print("DONE")
    raise SystemExit(0)
n = len(rows)
ll = lambda ps, ys: -sum(y * math.log(min(max(p, 1e-4), 1 - 1e-4)) + (1 - y) * math.log(1 - min(max(p, 1e-4), 1 - 1e-4)) for p, y in zip(ps, ys, strict=True)) / len(ys)
print(f"window: {datetime.datetime.fromtimestamp(min(r['ts'] for r in rows), ET):%m-%d %H:%M} to {now:%m-%d %H:%M} ET; {n} settled live bets with the note. Log loss: price alone {ll([r['ask'] for r in rows], [r['won'] for r in rows]):.4f}, model {ll([r['p'] for r in rows], [r['won'] for r in rows]):.4f} (lower is better).")


def show(label, sel):
    if len(sel) < 10:
        print(f"  {label:34s} bets {len(sel):4d}")
        return
    stake = sum(r["stake"] for r in sel)
    days = {}
    for r in sel:
        days.setdefault(datetime.datetime.fromtimestamp(r["ts"], ET).date(), []).append(r["trad"])
    print(f"  {label:34s} bets {len(sel):4d} | avg ask {100.0 * sum(r['ask'] for r in sel) / len(sel):5.1f}c win {100.0 * sum(r['won'] for r in sel) / len(sel):5.1f}% | held {sum(r['held'] for r in sel):+7.2f} ({100.0 * sum(r['held'] for r in sel) / stake:+5.1f} per $100) | as traded {sum(r['trad'] for r in sel):+7.2f} ({100.0 * sum(r['trad'] for r in sel) / stake:+5.1f} per $100) | days positive as traded {sum(1 for v in days.values() if sum(v) > 0)} of {len(days)}")


show("all", rows)
show("top fifth of the shortfall (80+)", [r for r in rows if r["pct"] >= 80])
show("the other four fifths", [r for r in rows if r["pct"] < 80])
show("top tenth (90+)", [r for r in rows if r["pct"] >= 90])
vols = sorted(r["vol"] for r in rows if r["vol"] is not None)
if len(vols) >= 60:
    lo, hi = vols[len(vols) // 3], vols[2 * len(vols) // 3]
    for name, sel in ((f"vol under {lo:.2f}", [r for r in rows if r["vol"] is not None and r["vol"] <= lo]), (f"vol {lo:.2f} to {hi:.2f}", [r for r in rows if r["vol"] is not None and lo < r["vol"] <= hi]), (f"vol over {hi:.2f}", [r for r in rows if r["vol"] is not None and r["vol"] > hi])):
        show(name + ", top fifth", [r for r in sel if r["pct"] >= 80])
        show(name + ", the rest", [r for r in sel if r["pct"] < 80])
print("DONE")
