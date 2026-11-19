"""Export every live commodity bet since SINCE (default 2026-11-09 00:00 ET) with everything the bot logged at the buy and what happened, as CSV on stdout (2026-11-19, user: "there's so many different variables in play and you keep isolating individual features like price or time when its a combination"). For scripts/commodity_bet_combination.py, which runs on the Mac.
Columns: ts, ticker, lane, side, ask, stake, left (seconds to the close at the buy), result_win (1/0), exit_price (blank when held), exit_left, the decision's `factors` readings (elapsed_min, spread, imbalance, move_90, range_90, activity_60, rise_30, rise_90, dip_90, held_min, updates_60, bid), and the numbers in its text: model_p_side, width_pct, basis_pct, rec_weight, rec_skill, vol (the vol tag), z (the cushion tag), spike_z, mtype, variety.
Run on the host: scripts/cloud.sh run scripts/export_commodity_bets.py > data/commodity_bets_export.csv"""
import csv
import datetime
import json
import os
import re
import sys

from kalshi_moonshot import config, db

ET = datetime.timezone(datetime.timedelta(hours=-5))
since = float(os.environ.get("SINCE", datetime.datetime(2026, 11, 9, tzinfo=ET).timestamp()))
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
rows = st.execute("SELECT d.ts, d.ticker, d.lane, d.side, d.ask, d.stake_usd, d.exit_price, d.exit_ts, d.factors, d.context, d.reason, w.result, w.close_ts FROM decisions d JOIN windows w ON w.ticker=d.ticker WHERE d.mode='trade' AND d.action='bet' AND d.lane LIKE 'commodity%' AND d.lane<>'manual' AND d.ts>? AND w.result IN ('yes','no') ORDER BY d.ts", (since,)).fetchall()
FACT = ("elapsed_min", "spread", "imbalance", "move_90", "range_90", "activity_60", "rise_30", "rise_90", "dip_90", "held_min", "updates_60", "bid")
cols = ["ts", "ticker", "lane", "side", "ask", "stake", "left", "result_win", "exit_price", "exit_left", *FACT, "model_p_side", "width_pct", "basis_pct", "rec_weight", "rec_skill", "vol", "z", "spike_z", "mtype", "variety"]
w = csv.writer(sys.stdout)
w.writerow(cols)
for r in rows:
    if not r["ask"]:
        continue
    f = r["factors"]
    if isinstance(f, str):
        try:
            f = json.loads(f)
        except ValueError:
            f = {}
    f = f if isinstance(f, dict) else {}
    ctx, reason = str(r["context"] or ""), str(r["reason"] or "")
    out = {"ts": r["ts"], "ticker": r["ticker"], "lane": r["lane"], "side": r["side"], "ask": r["ask"], "stake": r["stake_usd"], "left": float(r["close_ts"]) - float(r["ts"]), "result_win": 1 if r["result"] == r["side"] else 0, "exit_price": r["exit_price"], "exit_left": (float(r["close_ts"]) - float(r["exit_ts"])) if r["exit_ts"] else ""}
    for k in FACT:
        out[k] = f.get(k)
    m = re.search(r"P\(yes\) ([0-9.]+)", ctx)
    if m:
        py = float(m.group(1))
        out["model_p_side"] = py if r["side"] == "yes" else 1.0 - py
    m = re.search(r"width ([0-9.]+)%", ctx)
    out["width_pct"] = float(m.group(1)) if m else None
    m = re.search(r"basis ([+-][0-9.]+)%", ctx)
    out["basis_pct"] = float(m.group(1)) if m else None
    m = re.search(r"weight on estimate ([0-9.]+), skill (-?[0-9.]+)", reason)
    out["rec_weight"], out["rec_skill"] = (float(m.group(1)), float(m.group(2))) if m else (None, None)
    m = re.search(r"\[vol ([0-9.]+)x\]", reason)
    out["vol"] = float(m.group(1)) if m else None
    m = re.search(r"\[z ([+-]?[0-9.]+)\]", reason)
    out["z"] = float(m.group(1)) if m else None
    m = re.search(r"\[spike view z ([+-]?[0-9.]+)", reason)
    out["spike_z"] = float(m.group(1)) if m else None
    m = re.search(r"\[mtype ([a-z+_-]+)\]", reason)
    out["mtype"] = m.group(1) if m else None
    m = re.search(r"\[variety ([A-Z][-a-z]*)\]", reason)
    out["variety"] = m.group(1) if m else None
    w.writerow([out.get(c, "") if out.get(c) is not None else "" for c in cols])
