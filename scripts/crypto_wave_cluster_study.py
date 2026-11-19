"""When many coins are bought the same way at once, do those bets lose together? (2026-11-18, user: "The crypto coins together concept. The waves are what kills us")

One question on the same bets: every staked bet crypto_risk_score placed since the price-band fix (2026-11-17 22:47 ET), held as the lane held it (its own sale, or settlement), fee on every leg. Each bet is counted with the other staked bets of the same lane on the SAME side (yes or no) in the same 15-minute window within 60 s of it: its wave size (1 = alone). Reported by wave size: bets, how many paid, net dollars and per $100 staked, and the same by minutes in (a market's first third, the rest), plus the whole wave as one: for each wave, the mean result of its bets and how many of them lost together. No coin is named in a rule; the wave is a count of simultaneous same-side bets.
Run: scripts/cloud.sh run scripts/crypto_wave_cluster_study.py"""
import datetime
from collections import defaultdict
from zoneinfo import ZoneInfo

from kalshi_moonshot import config, db
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.load()
main = db.connect(s.db_path, "main", shared=True)
since = datetime.datetime(2026, 11, 17, 22, 47, tzinfo=ZoneInfo("America/New_York")).timestamp()
rows = main.execute("SELECT d.ts, d.ticker, d.side, d.ask, d.exit_price, d.stake_usd, d.outcome, w.close_ts FROM decisions d JOIN windows w ON w.ticker = d.ticker WHERE d.lane = 'crypto_risk_score' AND d.action = 'bet' AND d.stake_usd > 0 AND d.ts > ? AND (d.exit_price IS NOT NULL OR d.outcome IN ('yes','no')) ORDER BY d.ts", (since,)).fetchall()
bets = []
for r in rows:
    ask = float(r["ask"])
    cost = ask + fee(ask)
    if r["exit_price"] is not None:
        px = float(r["exit_price"])
        back = max(px, 0.0) - (fee(px) if px > 0 else 0.0)
    else:
        back = 1.0 if r["outcome"] == r["side"] else 0.0
    bets.append({"ts": float(r["ts"]), "win": int(r["close_ts"]), "side": r["side"], "stake": float(r["stake_usd"]), "ret": (back - cost) / cost, "in": 900.0 - (float(r["close_ts"]) - float(r["ts"])), "ticker": r["ticker"]})
for b in bets:
    b["wave"] = 1 + sum(1 for o in bets if o is not b and o["win"] == b["win"] and o["side"] == b["side"] and abs(o["ts"] - b["ts"]) <= 60.0 and o["ticker"] != b["ticker"])


def show(label, sel):
    if not sel:
        return
    stake = sum(x["stake"] for x in sel)
    net = sum(x["ret"] * x["stake"] for x in sel)
    print(f"  {label:26s} bets {len(sel):4d} | paid {sum(1 for x in sel if x['ret'] > 0):4d} ({100.0 * sum(1 for x in sel if x['ret'] > 0) / len(sel):4.0f}%) | net ${net:+8.2f} ({100.0 * net / stake:+6.1f} per $100 staked) | staked ${stake:7.2f}")


print(f"window: 2026-11-17 22:47 ET to now; {len(bets)} staked crypto_risk_score bets, held as the lane held them, fee in")
for name, lo, hi in (("alone (wave 1)", 1, 1), ("wave 2 to 3", 2, 3), ("wave 4 to 6", 4, 6), ("wave 7 or more", 7, 99)):
    show(name, [b for b in bets if lo <= b["wave"] <= hi])
print("--- by minutes into the market, and wave size")
for part, test in (("first third (under 5 min in)", lambda b: b["in"] < 300), ("rest", lambda b: b["in"] >= 300)):
    for name, lo, hi in (("wave 1 to 3", 1, 3), ("wave 4 or more", 4, 99)):
        show(f"{part[:12]}, {name}", [b for b in bets if test(b) and lo <= b["wave"] <= hi])
print("--- each wave as one bet: waves of 4 or more bets (a window and side, bets within 60 s of the first)")
waves = defaultdict(list)
for b in bets:
    waves[(b["win"], b["side"])].append(b)
big = []
for k, v in waves.items():
    v.sort(key=lambda x: x["ts"])
    grp = [x for x in v if x["ts"] - v[0]["ts"] <= 60.0]
    if len(grp) >= 4:
        big.append((k, grp))
tot = sum(sum(x["ret"] * x["stake"] for x in g) for _, g in big)
lost_all = sum(1 for _, g in big if all(x["ret"] <= 0 for x in g))
print(f"  waves of 4+ bets: {len(big)} | together ${tot:+.2f} on ${sum(sum(x['stake'] for x in g) for _, g in big):.2f} | waves where every bet lost: {lost_all} | waves where most lost: {sum(1 for _, g in big if sum(1 for x in g if x['ret'] <= 0) > len(g) / 2)}")
for k, g in sorted(big, key=lambda kv: kv[1][0]["ts"]):
    print(f"    {datetime.datetime.fromtimestamp(g[0]['ts'], ZoneInfo('America/New_York')):%m-%d %H:%M} {g[0]['side']:3s} {len(g):2d} bets, {sum(1 for x in g if x['ret'] > 0):2d} paid, net ${sum(x['ret'] * x['stake'] for x in g):+7.2f}")
print("DONE")
