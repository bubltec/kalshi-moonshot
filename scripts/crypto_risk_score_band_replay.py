"""crypto_risk_score's risk rule on the recorded risk readings, over the whole market and every price (2026-11-18, user: "when we test a model, it should run on the full market length, and at any price range")

One question on the same bets: for every crypto 15-minute market with recorded risk readings (risk_readings, every open market every 30 s, since 2026-11-17) and a result, at every reading from the open to the close and at every price, the first reading at which a side's risk (1 minus riskscore.chance, the weights in use) is at or under the level counts, one a market and side, held to settlement, taker fee on the buy. The rule is read at the level 0.25 (the lane's), and at 0.35 and 0.50 to see what the lower prices do. The lane's looks (0, then a thirtieth and a fifteenth of the time left, no shorter than 90 s of it) are 3 to 60 s back; the readings are 30 s apart, so the replay holds the rule at NOW alone and at now and the reading before it (about 30 s back): a fixed look is a test of the idea, not of the lane's looks. The ladder is not replayed. Held to settlement leaves out the lane's own profit take. By ask (every 0.05) and by the time left. Settings chosen after seeing the results are said to be so.
Run: scripts/cloud.sh run scripts/crypto_risk_score_band_replay.py"""
import json
from collections import defaultdict

from kalshi_moonshot import config, db, riskscore
from kalshi_moonshot.strategy import fee_per_contract as fee

SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M", "KXADA15M", "KXBCH15M", "KXBNB15M", "KXHYPE15M", "KXNEAR15M", "KXZEC15M")
LEVELS = (0.25, 0.35, 0.50)
s = config.load()
main = db.connect(s.db_path, "main", shared=True)
w = riskscore.weights(str(s.data_dir))
rows = main.execute("SELECT ticker, close_ts, result FROM windows WHERE result IN ('yes','no')").fetchall()
wins = [r for r in rows if r["ticker"].split("-")[0] in SERIES]
first = main.execute("SELECT min(ts) FROM risk_readings").fetchone()[0]
out = defaultdict(list)  # (looks, level, "ask"/"left", label) -> [(won, net, ask)]
used = 0
for wd in wins:
    if wd["close_ts"] < first:
        continue
    rd = main.execute("SELECT ts, x, yes_bid, yes_ask FROM risk_readings WHERE ticker=? ORDER BY ts", (wd["ticker"],)).fetchall()
    if len(rd) < 4:
        continue
    used += 1
    xs = []
    for r in rd:
        try:
            x = json.loads(r["x"])
        except (ValueError, TypeError):
            x = None
        xs.append(x if x and len(x) == len(w) else None)
    done = set()
    for i in range(1, len(rd)):
        if xs[i] is None:
            continue
        yb, ya = rd[i]["yes_bid"], rd[i]["yes_ask"]
        if yb is None or ya is None or not (0 < yb <= ya < 1):
            continue
        left = float(wd["close_ts"]) - float(rd[i]["ts"])
        if left < 0:
            continue
        back_ok = xs[i - 1] is not None and 20.0 <= rd[i]["ts"] - rd[i - 1]["ts"] <= 45.0
        for side, ask in (("yes", float(ya)), ("no", 1.0 - float(yb))):
            now_c = riskscore.chance(xs[i], side, w)
            two = [now_c, riskscore.chance(xs[i - 1], side, w)] if back_ok else None
            won = wd["result"] == side
            net = (1.0 if won else 0.0) - ask - fee(ask)
            for level in LEVELS:
                for looks, ok in (("now", riskscore.low_risk([now_c], level)), ("now and 30 s back", bool(two) and riskscore.low_risk(two, level))):
                    key = (looks, level, side)
                    if not ok or key + (wd["ticker"],) in done:
                        continue
                    done.add(key + (wd["ticker"],))
                    band = min(int(ask / 0.05), 19) * 0.05
                    out[(looks, level, "ask", f"ask {band:.2f} to {band + 0.05:.2f}")].append((won, net, ask))
                    lb = "under 90 s" if left < 90 else "90 to 180 s" if left < 180 else "3 to 6 min" if left < 360 else "6 to 10 min" if left < 600 else "10 to 15 min"
                    out[(looks, level, "left", lb)].append((won, net, ask))
                    out[(looks, level, "all", "all")].append((won, net, ask))


def show(label, sel):
    if not sel:
        return
    n = len(sel)
    stake = sum(a + fee(a) for _, _, a in sel)
    print(f"  {label:22s} bets {n:5d} | win {100.0 * sum(1 for x in sel if x[0]) / n:5.1f}% vs avg ask {100.0 * sum(x[2] for x in sel) / n:5.1f}c | net {sum(x[1] for x in sel):+8.2f} ({100.0 * sum(x[1] for x in sel) / stake:+6.1f} per $100 staked)")


print(f"window: readings from 2026-11-17 to now, {used} crypto markets with a result; the whole market, every price, one a market and side, held to settlement, fee in, weights {'tuned' if riskscore.tuned(str(s.data_dir)) else 'the 11-16 fit'}")
for looks in ("now", "now and 30 s back"):
    for level in LEVELS:
        print(f"=== risk at or under {level:.0%}, held at: {looks}")
        show("all", out[(looks, level, "all", "all")])
        print("  --- by the ask")
        for k in sorted(k for k in out if k[:3] == (looks, level, "ask")):
            show(k[3], out[k])
        print("  --- by the time left")
        for lb in ("10 to 15 min", "6 to 10 min", "3 to 6 min", "90 to 180 s", "under 90 s"):
            show(lb, out[(looks, level, "left", lb)])
print("DONE")
