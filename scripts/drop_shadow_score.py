"""How is the slow drop shadow doing against what the commodity bets actually did? (2026-11-17, user: "How's the shadow exit doing compared to our live commodities?")

One question, same bets (CLAUDE.md rule 4): every commodity bet placed since the shadow went on (2026-11-16 21:02 ET, commit 1307b4f) and settled, against what actually happened to it (the lane's own sale, or
settlement), fees on every leg. Where the shadow logged a would-sell (data/risk_shadow.jsonl, kind "drop") before the bet ended, the rule's result is a sale at the worse of the bids one and two seconds after
the signal (the recorded bid when the tape has neither), less the fee. One rule, set before any of these bets: fall 1 typical minute, a quarter of the time left, chance under one half. Live and paper lanes
are reported apart. Shapes: how far into the market the buy was and the price paid. No commodity is named in a rule.
    scripts/cloud.sh run scripts/drop_shadow_score.py
"""
import bisect
import datetime
import json
import pathlib

from kalshi_moonshot import config, db
from kalshi_moonshot.strategy import fee_per_contract as fee

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 16, 21, 2, tzinfo=ET).timestamp()
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
f = pathlib.Path(s.data_dir) / "risk_shadow.jsonl"
sig = {}
for line in f.read_text().splitlines() if f.exists() else []:
    try:
        x = json.loads(line)
    except ValueError:
        continue
    if x.get("kind") == "drop" and x["ts"] > SINCE:
        sig.setdefault((x["ticker"], x["lane"]), x)
results = {w["ticker"]: (w["result"], float(w["close_ts"])) for w in st.execute("SELECT ticker, close_ts, result FROM windows WHERE close_ts>? AND result IN ('yes','no')", (SINCE,)).fetchall()}
rows = st.execute("SELECT ts, ticker, lane, mode, side, ask, stake_usd, contracts, exit_price, exit_ts, exit_reason FROM decisions WHERE lane LIKE 'commodity%' AND action='bet' AND stake_usd>0 AND ts>? ORDER BY ts", (SINCE,)).fetchall()
out, unsettled = [], 0
for r in rows:
    if r["ticker"] not in results:
        unsettled += 1
        continue
    res, close = results[r["ticker"]]
    stake, n0, ask = float(r["stake_usd"]), float(r["contracts"] or 0.0), float(r["ask"] or 0.0)
    basis = stake / n0 if n0 > 0 else 0.0
    if not (ask - 0.06 <= basis <= ask + fee(ask) + 0.06):
        basis = ask + fee(ask)  # decisions.contracts can undercount the fills
    n = stake / basis
    won = res == r["side"]
    sold = r["exit_price"] is not None and r["exit_ts"] is not None and float(r["exit_ts"]) < close - 1
    actual = ((float(r["exit_price"]) - fee(float(r["exit_price"])) if sold else (1.0 if won else 0.0)) - basis) * n
    end = float(r["exit_ts"]) if sold else close
    x = sig.get((r["ticker"], r["lane"]))
    rule = None
    if x is not None and float(r["ts"]) < x["ts"] < end and x["side"] == r["side"]:
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (r["ticker"], x["ts"] - 30, x["ts"] + 3)).fetchall()
        qt = [float(a[0]) for a in q]
        fills = []
        for dt in (1.0, 2.0):
            i = bisect.bisect_right(qt, x["ts"] + dt) - 1
            if i >= 0:
                fills.append(float(q[i][1]) if r["side"] == "yes" else 1.0 - float(q[i][2]))
        fill = min(fills) if fills else float(x["bid"])
        rule = ((fill - fee(fill) - basis) * n, fill, x["left"])
    into = float(r["ts"]) - (close - 900.0)
    out.append({"r": r, "live": r["mode"] == "trade", "won": won, "sold": sold, "actual": actual, "rule": rule, "stake": stake,
                "into": "bought before minute 7" if into < 420 else "bought minute 7 to 10" if into < 600 else "bought after minute 10",
                "paid": "paid under 0.80" if basis < 0.80 else "paid 0.80 and over"})


def line(name, rs):
    if not rs:
        print(f"  {name:26s} n   0")
        return
    a = sum(x["actual"] for x in rs)
    b = sum(x["rule"][0] if x["rule"] else x["actual"] for x in rs)
    tk = [x for x in rs if x["rule"]]
    beat = sum(1 for x in tk if x["rule"][0] > x["actual"] + 1e-9)
    lost = sum(1 for x in tk if x["rule"][0] < x["actual"] - 1e-9)
    print(f"  {name:26s} n {len(rs):3d} | staked ${sum(x['stake'] for x in rs):8.2f} | won if held {sum(1 for x in rs if x['won']):3d} | what happened ${a:+8.2f} | with the shadow ${b:+8.2f} | change ${b - a:+7.2f} | shadow would sell {len(tk):2d}: beat {beat}, lost to {lost}")


print(f"window: commodity bets since 2026-11-16 21:02 ET to {datetime.datetime.now(ET):%m-%d %H:%M} ET; {len(out)} settled, {unsettled} not settled; drop signals on file {len(sig)}")
for label, live in (("LIVE lanes", True), ("PAPER lanes", False)):
    rs = [x for x in out if x["live"] is live]
    print(f"\n=== {label}")
    line("all", rs)
    for k in ("bought before minute 7", "bought minute 7 to 10", "bought after minute 10"):
        line(k, [x for x in rs if x["into"] == k])
    for k in ("paid under 0.80", "paid 0.80 and over"):
        line(k, [x for x in rs if x["paid"] == k])
    line("ended: held and won", [x for x in rs if not x["sold"] and x["won"]])
    line("ended: held to zero", [x for x in rs if not x["sold"] and not x["won"]])
    line("ended: sold by the lane", [x for x in rs if x["sold"]])
    for x in rs:
        if x["rule"]:
            r = x["r"]
            print(f"    {datetime.datetime.fromtimestamp(float(r['ts']), ET):%m-%d %H:%M} {r['ticker']:30s} {r['lane']:22s} {r['side']:3s} paid {float(r['ask']):.2f} | shadow sells {x['rule'][1]:.2f} with {x['rule'][2]:.0f}s left ${x['rule'][0]:+.2f} | actual ${x['actual']:+.2f} ({'sold by the lane' if x['sold'] else 'won' if x['won'] else 'lost'})")
    print("    total losses the shadow did not flag: " + str(sum(1 for x in rs if not x["sold"] and not x["won"] and not x["rule"])))
print("DONE")
