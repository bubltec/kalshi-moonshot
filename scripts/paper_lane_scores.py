"""How each paper lane has done, as the bot recorded it (2026-11-22, user: "lets remove all these paper lanes that don't work"). Per lane (paper bets, mode != trade): bets settled, net dollars as really ended (own sale or settlement, fee on both legs), share ahead, first and last bet, and the same for the last 3 days. Run: SPOT=0 scripts/cloud_job.sh scripts/paper_lane_scores.py"""
import datetime
from kalshi_moonshot import config, db
from kalshi_moonshot.strategy import fee_per_contract as fee
s = config.reload(); st = db.connect(s.db_path, "main", shared=True)
W = {w["ticker"]: float(w["close_ts"]) for w in st.execute("SELECT ticker, close_ts FROM windows WHERE result IN ('yes','no')").fetchall()}
rows = st.execute("SELECT ts, ticker, lane, side, mode, outcome, exit_price, exit_ts, contracts, stake_usd FROM decisions WHERE action='bet' AND stake_usd>0 AND contracts>0 AND outcome IN ('yes','no')").fetchall()
by = {}
for r in rows:
    n = float(r["contracts"]); basis = float(r["stake_usd"]) / n
    close = W.get(r["ticker"], 0.0)
    sold = r["exit_price"] is not None and r["exit_ts"] is not None and float(r["exit_ts"]) < close - 1
    won = r["outcome"] == r["side"]
    per = ((float(r["exit_price"]) - fee(float(r["exit_price"]))) if sold else (1.0 if won else 0.0)) - basis
    by.setdefault((r["lane"], r["mode"]), []).append((float(r["ts"]), per * n, float(r["stake_usd"])))
now = max(x[0] for v in by.values() for x in v)
print("lane, mode: bets, net $, per $1 staked, ahead, first bet, last bet | last 3 days: bets, net $")
for (lane, mode), v in sorted(by.items()):
    t = lambda x: datetime.datetime.fromtimestamp(x, datetime.timezone.utc).strftime("%m-%d")
    r3 = [x for x in v if x[0] > now - 3 * 86400]
    print(f"{lane:26s} {mode:6s} n {len(v):5d} net {sum(x[1] for x in v):+9.2f} per$1 {sum(x[1] for x in v) / max(1e-9, sum(x[2] for x in v)):+.3f} ahead {100 * sum(1 for x in v if x[1] > 0) / len(v):3.0f}% {t(min(x[0] for x in v))}..{t(max(x[0] for x in v))} | 3d n {len(r3):4d} net {sum(x[1] for x in r3):+8.2f}")
