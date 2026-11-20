"""crypto_risk_score losses by 15-minute window: are they a few windows where many bets lost together (a market-wide burst) or spread thin?
Question (user, 2026-11-19: "short bursts ... the whole market went down big"): per window (close time), the bets, the share on one side, and the result held and as sold, fee in. Reports how much of the total loss the worst 5, 10 and 20 windows hold, and the result by how one-sided a window's bets were (share of its bets on the same side), a reading of how much of the market moved together; no coin or asset name is used.
Run: scripts/cloud.sh run scripts/risk_score_by_window.py"""
import collections, os, datetime
from kalshi_moonshot import config, db
from kalshi_moonshot.strategy import fee_per_contract as fee
s = config.reload(); st = db.connect(s.db_path, "main", shared=True)
SINCE = float(os.environ.get("SINCE", 1795044660))
res = {r["ticker"]: r["result"] for r in st.execute("SELECT ticker, result FROM windows WHERE result IN ('yes','no')").fetchall()}
SEQ = []
W = collections.defaultdict(lambda: {"n": 0, "yes": 0, "held": 0.0, "sold": 0.0, "stake": 0.0})
for r in st.execute("SELECT ticker, side, ask, stake_usd, exit_price, close_time, ts FROM decisions WHERE lane='crypto_risk_score' AND stake_usd IS NOT NULL AND ts > ? ORDER BY ts", (SINCE,)).fetchall():
    if res.get(r["ticker"]) not in ("yes", "no"): continue
    ask = float(r["ask"]); win = res[r["ticker"]] == r["side"]; k = float(r["stake_usd"]) / ask
    held = k * ((1.0 if win else 0.0) - ask - fee(ask))
    sold = held if r["exit_price"] is None else k * (float(r["exit_price"]) - ask - fee(ask) - fee(float(r["exit_price"])))
    SEQ.append((r["close_time"], held, sold, float(r["stake_usd"])))
    w = W[r["close_time"]]; w["n"] += 1; w["yes"] += r["side"] == "yes"; w["held"] += held; w["sold"] += sold; w["stake"] += float(r["stake_usd"])
ws = sorted(W.items(), key=lambda kv: kv[1]["sold"])
tot_s = sum(v["sold"] for _, v in ws); tot_h = sum(v["held"] for _, v in ws); stake = sum(v["stake"] for _, v in ws)
print(f"{len(ws)} windows, {sum(v['n'] for _, v in ws)} bets, stake {stake:.0f}: held {tot_h:+.2f}, as sold {tot_s:+.2f}")
for n in (5, 10, 20):
    print(f"worst {n:2d} windows hold {sum(v['sold'] for _, v in ws[:n]):+.2f} as sold of {tot_s:+.2f}; the other {len(ws)-n} windows: {sum(v['sold'] for _, v in ws[n:]):+.2f}")
print("worst 8 windows (close time UTC, bets, share on YES, held, as sold):")
for ct, v in ws[:8]: print(f"  {ct[:16]} n {v['n']:2d} yes {v['yes']/v['n']:.0%} held {v['held']:+7.2f} sold {v['sold']:+7.2f}")
g = collections.defaultdict(lambda: [0, 0, 0.0, 0.0, 0.0])
for ct, v in W.items():
    if v["n"] < 4: continue
    share = max(v["yes"], v["n"] - v["yes"]) / v["n"]
    name = "windows where >=85% of the bets were on one side" if share >= 0.85 else "mixed windows (< 85% on one side)"
    a = g[name]; a[0] += 1; a[1] += v["n"]; a[2] += v["held"]; a[3] += v["sold"]; a[4] += v["stake"]
print("windows with 4 or more bets:")
for name in sorted(g):
    a = g[name]; print(f"  {name:50s} windows {a[0]:3d} bets {a[1]:4d} | held {100*a[2]/a[4]:+6.1f}% | as sold {100*a[3]/a[4]:+6.1f}%")
g2 = collections.defaultdict(lambda: [0, 0, 0.0, 0.0, 0.0])
for ct, v in W.items():
    name = "a  1-5 bets in the window" if v["n"] <= 5 else "b  6-10 bets" if v["n"] <= 10 else "c  11+ bets"
    a = g2[name]; a[0] += 1; a[1] += v["n"]; a[2] += v["held"]; a[3] += v["sold"]; a[4] += v["stake"]
print("by how many bets the lane placed in the window:")
for name in sorted(g2):
    a = g2[name]; print(f"  {name:28s} windows {a[0]:3d} bets {a[1]:4d} | held {100*a[2]/a[4]:+6.1f}% | as sold {100*a[3]/a[4]:+6.1f}%")
rank = collections.Counter(); g3 = collections.defaultdict(lambda: [0, 0.0, 0.0, 0.0])
for ct, held, sold, stake in SEQ:
    rank[ct] += 1; k = rank[ct]
    name = "a  bets 1-5 of a window (in time order)" if k <= 5 else "b  bets 6-10" if k <= 10 else "c  bets 11 and later"
    a = g3[name]; a[0] += 1; a[1] += held; a[2] += sold; a[3] += stake
print("by the bet's place in its window (how many bets the lane had already placed in it):")
for name in sorted(g3):
    a = g3[name]; print(f"  {name:40s} bets {a[0]:4d} | held {100*a[1]/a[3]:+6.1f}% | as sold {100*a[2]/a[3]:+6.1f}%")
