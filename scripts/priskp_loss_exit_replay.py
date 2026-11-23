"""PriskP loss-side exit replay (2026-11-23; user: crypto markets move together, so losses arrive at once; "make sure the exit works properly", "our goal is avoiding loss", no cap per window).
QUESTION (one, on the same bets): on every filled, settled PriskP bet (paper, lane priskp, all of them recorded so far), does a tight sale in the bet's own units beat what the exit really did (own sale at the bid, or held to settlement)? Fee on every leg (0.07 p (1-p)).
RULES (stated before the run, not a sweep; the bid is the bet's own side bid from the recorded quotes; u = the typical minute of that side's mid = rms one-second change over the 900 s before the buy x sqrt(60); no unit, no rule):
  STOP: sell the first time the bid is K x u or more under the price paid. K = 1.0 (my choice; 0.5 and 2.0 shown beside it, not chosen).
  TAKE: sell the first time the net after both fees is G x u or more above zero. G = 0.5 (my choice; 0.25 and 1.0 shown, not chosen). This is the "small win over a potential loss" reading.
  BOTH: whichever comes first.
A sale is at the recorded bid of that reading; a fill is not modelled. Quotes need the bid and ask both above zero. Reported: n, net dollars (every bet at its own size), bets the rule beat and lost to what really happened, sales that went on to win (a whipsaw), and the same for the bets that really lost. A lead only: one day of paper bets, read-only. Settings chosen after seeing the results are said so.
Run: scripts/cloud.sh run scripts/priskp_loss_exit_replay.py (the host holds today's quotes; the jobs runner restores a backup up to a day old)."""
import json
import math
import os
import time

DUMP = os.environ.get("DUMP")  # a file made on the host (bets and their quotes), so the replay can run on the Mac
if DUMP:
    data = json.load(open(DUMP))
else:
    from kalshi_moonshot import config, db

    s = config.load()
    st = db.connect(s.db_path, "main", shared=True)
    tape = db.connect(s.tape_path, "tape", shared=True)


def fee(p):
    return 0.07 * p * (1.0 - p)


rows = data["bets"] if DUMP else st.execute(
    "SELECT d.ts, d.ticker, d.side, d.stake_usd, d.contracts, d.exit_price, d.exit_ts, w.result, w.close_ts "
    "FROM decisions d JOIN windows w ON w.ticker=d.ticker WHERE d.lane='priskp' AND d.action='bet' AND d.stake_usd>0 AND d.contracts>0 AND w.result IN ('yes','no') ORDER BY d.ts"
).fetchall()
KS, GS = (0.5, 1.0, 2.0), (0.25, 0.5, 1.0)
res, skipped = [], 0
for b in rows:
    t0, close = float(b["ts"]), float(b["close_ts"])
    n = float(b["contracts"])
    basis = float(b["stake_usd"]) / n
    q = data["quotes"][b["ticker"]] if DUMP else tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts<? AND bid>0 AND ask>0 ORDER BY ts", (b["ticker"], close)).fetchall()
    if len(q) < 60:
        skipped += 1
        continue
    ts = [float(x[0]) for x in q]
    yb = [float(x[1]) for x in q]
    ya = [float(x[2]) for x in q]
    sb = yb if b["side"] == "yes" else [1.0 - a for a in ya]
    sa = ya if b["side"] == "yes" else [1.0 - x for x in yb]
    mid = [(sb[i] + sa[i]) / 2.0 for i in range(len(ts))]
    ms, j = [], 0
    for sec in range(int(max(ts[0], t0 - 900)), int(t0)):
        while j + 1 < len(ts) and ts[j + 1] <= sec:
            j += 1
        ms.append(mid[j])
    dd = [ms[i + 1] - ms[i] for i in range(len(ms) - 1)]
    if len(dd) < 120:
        skipped += 1
        continue
    u = math.sqrt(sum(x * x for x in dd) / len(dd)) * math.sqrt(60.0)
    if u <= 0:
        skipped += 1
        continue
    won = b["result"] == b["side"]
    sold = b["exit_price"] is not None and b["exit_ts"] is not None and float(b["exit_ts"]) < close - 1
    settle = (float(b["exit_price"]) - fee(float(b["exit_price"]))) if sold else (1.0 if won else 0.0)
    held = (1.0 if won else 0.0) - basis - fee(basis)
    actual = settle - basis - fee(basis)
    path = [(ts[i], sb[i]) for i in range(len(ts)) if t0 < ts[i] < close - 1]

    def run(k, g):
        for _t, bid in path:
            net = bid - fee(bid) - basis - fee(basis)
            if k and bid <= basis - k * u:
                return net
            if g and net >= g * u:
                return net
        return held

    out = {"actual": actual, "hold": held, "n": n, "won": won, "ahead": any(bid - fee(bid) - basis - fee(basis) > 0 for _t, bid in path)}
    for k in KS:
        out[f"stop{k}"] = run(k, 0)
    for g in GS:
        out[f"take{g}"] = run(0, g)
    out["both"] = run(1.0, 0.5)
    res.append(out)
print(f"bets {len(res)} (left out for no quotes or no unit: {skipped}); as ran {sum(r['actual'] * r['n'] for r in res):+.2f}, held {sum(r['hold'] * r['n'] for r in res):+.2f}; lost bets {sum(1 for r in res if r['actual'] < 0)}; never ahead {sum(1 for r in res if not r['ahead'])}")
print("rule: net $ | beat / lost to what ran | sales of bets that went on to win | on bets that really lost: net $ (as ran)")
for key in [f"stop{k}" for k in KS] + [f"take{g}" for g in GS] + ["both"]:
    net = sum(r[key] * r["n"] for r in res)
    beat = sum(1 for r in res if r[key] > r["actual"] + 1e-9)
    lost = sum(1 for r in res if r[key] < r["actual"] - 1e-9)
    whip = sum(1 for r in res if r["won"] and r[key] != r["hold"])
    lo = [r for r in res if r["actual"] < 0]
    print(f"{key:8s} {net:+8.2f} | {beat:3d} / {lost:3d} | {whip:3d} | {sum(r[key] * r['n'] for r in lo):+7.2f} ({sum(r['actual'] * r['n'] for r in lo):+.2f})")
print("done", time.strftime("%H:%M"))
