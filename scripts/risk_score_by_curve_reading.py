"""crypto_risk_score bets by the curve readings stored on each bet (decisions.factors), held to the result, fee in.
Question (rule 4, by curve shape; user 2026-11-19: "crypto in general has been crazy today"): do the lane's bets lose in every curve shape or only where the market's own curve was wild. Cutoffs are terciles of these same bets' readings (the lane's own history, not fixed numbers); settings chosen after seeing the data.
Readings: range_90 (price range over the last 90 s), move_90/range_90 (how straight the last 90 s went), updates_60 (quote updates in 60 s, a busyness measure), elapsed_min.
Run: scripts/cloud.sh run scripts/risk_score_by_curve_reading.py"""
import collections, json, os
from kalshi_moonshot import config, db
from kalshi_moonshot.strategy import fee_per_contract as fee
s = config.reload(); st = db.connect(s.db_path, "main", shared=True)
SINCE = float(os.environ.get("SINCE", 1795044660))
res = {r["ticker"]: r["result"] for r in st.execute("SELECT ticker, result FROM windows WHERE result IN ('yes','no')").fetchall()}
bets = []
for r in st.execute("SELECT ticker, side, ask, stake_usd, exit_price, factors, ts FROM decisions WHERE lane='crypto_risk_score' AND stake_usd IS NOT NULL AND factors IS NOT NULL AND ts > ? ORDER BY ts", (SINCE,)).fetchall():
    if res.get(r["ticker"]) not in ("yes", "no"): continue
    try: f = json.loads(r["factors"])
    except Exception: continue
    ask = float(r["ask"]); win = res[r["ticker"]] == r["side"]; k = float(r["stake_usd"]) / ask
    held = k * ((1.0 if win else 0.0) - ask - fee(ask))
    sold = held if r["exit_price"] is None else k * (float(r["exit_price"]) - ask - fee(ask) - fee(float(r["exit_price"])))
    rng = f.get("range_90"); mv = f.get("move_90")
    bets.append({"stake": float(r["stake_usd"]), "held": held, "sold": sold, "win": win, "range": rng, "straight": (abs(mv) / rng) if rng and mv is not None else None, "busy": f.get("updates_60"), "ts": r["ts"]})
def terciles(key):
    v = sorted(b[key] for b in bets if b[key] is not None)
    return v[len(v) // 3], v[2 * len(v) // 3]
print(f"{len(bets)} settled bets with readings")
for key, label in (("range", "price range over 90 s"), ("straight", "straightness of the last 90 s (|move|/range)"), ("busy", "quote updates in 60 s")):
    lo, hi = terciles(key); g = collections.defaultdict(lambda: [0, 0.0, 0.0, 0, 0.0])
    for b in bets:
        if b[key] is None: continue
        name = f"low (<= {lo:.2f})" if b[key] <= lo else f"mid" if b[key] <= hi else f"high (> {hi:.2f})"
        a = g[name]; a[0] += 1; a[1] += b["held"]; a[2] += b["sold"]; a[3] += b["win"]; a[4] += b["stake"]
    print(f"--- {label}")
    for name in sorted(g, key=lambda x: (x[0] != "l", x[0] != "m")):
        a = g[name]; print(f"{name:18s} n {a[0]:4d} | held {100*a[1]/a[4]:+6.1f}% | as sold {100*a[2]/a[4]:+6.1f}% | win {a[3]/a[0]:.0%}")
