"""crypto_risk_score on the recorded risk readings: what would the lane's own buy rule have bought below 0.80, and what did those sides pay? (2026-11-18, user: "why can't you run a quick test?", on getting the lane to buy from about 65+)

One question on the same bets: for every crypto 15-minute market with recorded risk readings (risk_readings, every open market every 30 s, since 2026-11-17), the lane's buy rule as it now stands: the risk score's risk (1 minus its chance, riskscore.chance with the weights in use) at or under 0.25 now, 30 s ago and a minute ago (riskscore.low_risk, the risk_score step), the side's ask in the band, 2 to 15.6 minutes left (the lane's hours). The first reading that qualifies a side counts, one a market and side, held to settlement, taker fee on the buy. Two variants: the rule alone, and the rule with the score's chance clearing the ask and its fee by 0.01 (risk_score_edge, as runner.py applies it outside a chain). The ladder is NOT replayed (it cannot be from these rows), so this is the lane without its ladder step. Held to settlement leaves out the lane's profit take. By band of the ask and by the cushion zone the score itself uses (|cushion| under 0.5 / 0.5 to 1.5 / over 1.5). The cut points are the score's own zones, the band edges are the lane's own (0.57 floor, 0.80 where its buys sit).
Run: scripts/cloud.sh run scripts/crypto_risk_score_band_replay.py"""
import json
from collections import defaultdict

from kalshi_moonshot import config, db, riskscore
from kalshi_moonshot.strategy import fee_per_contract as fee

SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M", "KXADA15M", "KXBCH15M", "KXBNB15M", "KXHYPE15M", "KXNEAR15M", "KXZEC15M")
LEVEL, EDGE, LO, HI = 0.25, 0.01, 0.57, 0.85
s = config.load()
main = db.connect(s.db_path, "main", shared=True)
w = riskscore.weights(str(s.data_dir))
ci = riskscore.NAMES.index("cushion")
rows = main.execute("SELECT ticker, close_ts, result FROM windows WHERE result IN ('yes','no')").fetchall()
wins = [r for r in rows if r["ticker"].split("-")[0] in SERIES]
first = main.execute("SELECT min(ts) FROM risk_readings").fetchone()[0]
out = defaultdict(list)  # (variant, band label) -> [(won, net, ask)]
shape = defaultdict(list)
BANDS = ((0.57, 0.65), (0.65, 0.70), (0.70, 0.75), (0.75, 0.80), (0.80, 0.85001))
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
    for i in range(2, len(rd)):
        left = float(wd["close_ts"]) - float(rd[i]["ts"])
        if not (120.0 <= left <= 936.0):
            continue
        if any(xs[j] is None for j in (i, i - 1, i - 2)) or not (20.0 <= rd[i]["ts"] - rd[i - 2]["ts"] <= 80.0):
            continue
        yb, ya = rd[i]["yes_bid"], rd[i]["yes_ask"]
        if yb is None or ya is None or not (0 < yb <= ya < 1):
            continue
        for side, ask in (("yes", float(ya)), ("no", 1.0 - float(yb))):
            if side in done or not (LO <= ask <= HI):
                continue
            ch = [riskscore.chance(xs[j], side, w) for j in (i, i - 1, i - 2)]
            if not riskscore.low_risk(ch, LEVEL):
                continue
            done.add(side)
            won = wd["result"] == side
            net = (1.0 if won else 0.0) - ask - fee(ask)
            cush = abs(xs[i][ci])
            zone = "on the line (cushion under 0.5)" if cush < 0.5 else "fair cushion (0.5 to 1.5)" if cush < 1.5 else "wide cushion (over 1.5)"
            for variant, ok in (("rule alone", True), (f"rule + edge {EDGE:g}", ch[0] - (ask + fee(ask)) >= EDGE)):
                if not ok:
                    continue
                for lo, hi in BANDS:
                    if lo <= ask < hi:
                        out[(variant, f"ask {lo:.2f} to {min(hi, 0.85):.2f}")].append((won, net, ask))
                if ask < 0.80:
                    shape[(variant, zone)].append((won, net, ask))


def show(label, sel):
    if not sel:
        print(f"  {label:44s} bets    0")
        return
    n = len(sel)
    stake = sum(a + fee(a) for _, _, a in sel)
    print(f"  {label:44s} bets {n:4d} | win {100.0 * sum(1 for x in sel if x[0]) / n:5.1f}% vs avg ask {100.0 * sum(x[2] for x in sel) / n:5.1f}c | net {sum(x[1] for x in sel):+7.2f} ({100.0 * sum(x[1] for x in sel) / stake:+5.1f} per $100 staked)")


print(f"window: readings from 2026-11-17 to now, {used} crypto markets with a result; the lane's risk rule (risk at or under {LEVEL:.0%} held a minute), the ask {LO} to {HI}, one a market and side, held to settlement, fee in, weights {'tuned' if riskscore.tuned(str(s.data_dir)) else 'the 11-16 fit'}")
for variant in ("rule alone", f"rule + edge {EDGE:g}"):
    print(f"--- {variant}, by the ask")
    for lo, hi in BANDS:
        lab = f"ask {lo:.2f} to {min(hi, 0.85):.2f}"
        show(lab, out[(variant, lab)])
    print(f"--- {variant}, asks under 0.80 by the score's cushion zone")
    for z in ("on the line (cushion under 0.5)", "fair cushion (0.5 to 1.5)", "wide cushion (over 1.5)"):
        show(z, shape[(variant, z)])
print("DONE")
