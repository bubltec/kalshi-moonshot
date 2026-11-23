"""Why did the profit take not sell the bets that were ahead? (2026-11-19, user: "early buy should look for profit and take")
Read-only, on the host: the rows data/risk_shadow.jsonl holds for crypto_risk_score since the 2026-11-18 18:31 ET push, counted by kind and reason (a profit take held by the sure hold, the cushion, a still-rising bid, etc.), and for the settled bets that LOST after being held, whether any row says a take was wanted and held (and why).
    scripts/cloud.sh run scripts/risk_profit_holds.py"""
import collections
import json

from kalshi_moonshot import config

SINCE = 1795044681
LANE = "crypto_risk_score"
rows = []
for line in open(f"{config.reload().data_dir}/risk_shadow.jsonl"):
    try:
        r = json.loads(line)
    except Exception:
        continue
    if r.get("lane") == LANE and float(r.get("ts", 0)) >= SINCE:
        rows.append(r)
print(f"rows for {LANE} since the push: {len(rows)}")
kinds = collections.Counter((r.get("kind"), (r.get("action") or r.get("what") or "")[:40]) for r in rows)
for k, n in kinds.most_common(15):
    print("  ", n, k)
print("keys of one row:", sorted(rows[0].keys()) if rows else None)
held = [r for r in rows if "held" in str(r.get("kind", "")) or "hold" in str(r.get("reason", r.get("note", ""))).lower()]
by_bet = collections.Counter(r.get("ticker") for r in held)
print(f"rows that say a take was held: {len(held)} on {len(by_bet)} bets")
for r in held[:12]:
    print("  ", r.get("ticker"), r.get("kind"), str(r.get("reason", r.get("note", "")))[:160])
