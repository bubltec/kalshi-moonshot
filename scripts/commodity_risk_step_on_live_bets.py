"""Would the risk score step have helped the live commodity lanes? (2026-11-18, user: "why is commodities doing so horrible? would the risk score lane work better for that too?"; "its a favorites style lane so i feel like it would")

One question on the same bets: the live commodity lanes' own bets since Tue 2026-11-17 00:00 ET (the two losing days; after the Monday 11-09 rewrite), each scored as it happened (the lane's own exit or its settlement, fee in). For each bet, the risk score's reading of the bought side at the buy, 30 s before and 60 s before, from the recorded readings (riskrecord.py's risk_readings, every open market every 30 s) and the weights in use. The step is crypto_risk_score's: risk at or under 25% at all three looks (riskscore.low_risk). Nothing is fitted here; 25% is that lane's existing level.
Reported: the bets the step would have kept and the ones it would have refused, with how each group did; by curve shape (the cushion zone the score itself uses) and by lane (a label only).
Run: scripts/cloud.sh run scripts/commodity_risk_step_on_live_bets.py"""
import datetime
import json
import math
from collections import defaultdict
from zoneinfo import ZoneInfo

from kalshi_moonshot import config, db, riskscore
from kalshi_moonshot.strategy import fee_per_contract

LEVEL = 0.25
s = config.load()
main = db.connect(s.db_path, "main", shared=True)
start = datetime.datetime(2026, 11, 17, tzinfo=ZoneInfo("America/New_York")).timestamp()
w = riskscore.weights(str(s.data_dir))
ci = riskscore.NAMES.index("cushion")
bets = main.execute("SELECT ts, ticker, side, ask, stake_usd, outcome, exit_price, lane FROM decisions WHERE lane LIKE 'commodity_%' AND mode='trade' AND action='bet' AND outcome IN ('yes','no') AND stake_usd>0 AND ts>? ORDER BY ts", (start,)).fetchall()


def sig(z):
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))


groups = defaultdict(list)
for b in bets:
    side = b["side"] or "yes"
    ask = float(b["ask"])
    n = float(b["stake_usd"]) / (ask + fee_per_contract(ask))
    won = b["outcome"] == side
    if b["exit_price"] is not None:
        px = float(b["exit_price"])
        net = n * (px - fee_per_contract(px)) - float(b["stake_usd"])
    else:
        net = (n if won else 0.0) - float(b["stake_usd"])
    rd = main.execute("SELECT ts, x FROM risk_readings WHERE ticker=? AND ts<=? AND ts>? ORDER BY ts DESC LIMIT 3", (b["ticker"], float(b["ts"]) + 1.0, float(b["ts"]) - 100.0)).fetchall()
    chances, cush = [], None
    for r in rd:
        try:
            x = json.loads(r["x"])
        except (ValueError, TypeError):
            continue
        if len(x) != len(w):
            continue
        p_yes = sig(sum(a * c for a, c in zip(w, x)))
        chances.append(p_yes if side == "yes" else 1.0 - p_yes)
        if cush is None:
            cush = abs(x[ci])
    if len(chances) < 3:
        verdict = "no reading"
    else:
        verdict = "kept" if riskscore.low_risk(chances, LEVEL) else "refused"
    zone = "no reading" if cush is None else "on the line (cushion under 0.5)" if cush < 0.5 else "fair cushion (0.5 to 1.5)" if cush < 1.5 else "wide cushion (over 1.5)"
    groups[("all", verdict)].append((won, net, float(b["stake_usd"]), ask))
    groups[("shape: " + zone, verdict)].append((won, net, float(b["stake_usd"]), ask))
    groups[("lane: " + b["lane"], verdict)].append((won, net, float(b["stake_usd"]), ask))

print(f"window: Tue 2026-11-17 00:00 ET to now; {len(bets)} live commodity bets; the step: risk at or under {LEVEL:.0%} at the buy, 30 s before and 60 s before; weights in use: {'tuned' if riskscore.tuned(str(s.data_dir)) else 'the 11-16 fit'}")
for key in sorted({k[0] for k in groups}, key=lambda k: (k != "all", k)):
    print(f"--- {key}")
    for verdict in ("kept", "refused", "no reading"):
        g = groups.get((key, verdict))
        if not g:
            continue
        won = sum(1 for x in g if x[0])
        print(f"  {verdict:10s} bets {len(g):4d} | right side {won:4d} ({100.0 * won / len(g):3.0f}%) | avg ask {sum(x[3] for x in g) / len(g):.2f} | staked {sum(x[2] for x in g):8.2f} | net {sum(x[1] for x in g):+8.2f}")
print("DONE")
