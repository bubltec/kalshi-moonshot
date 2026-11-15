"""Does the live lane's flat-market skip (buy_flat_trend_pct 0.05: no buy while the market-wide 60-minute trend is inside 0.05%) block bets that pay?

One question, same bets (CLAUDE.md rule 4): every bet of the crypto_multiscale lanes (the live lane and its paper variants, which carry the same [trend60 x%] tag on their decisions) since Mon 2026-11-09, each with its real result after the fee on every leg (its own exit or settlement, mode trade or paper alike), split by the size of the market-wide 60-minute trend tag at the buy: under 0.05% (what the live lane skips) against 0.05% and over, and by finer steps. The live lane never buys under 0.05, so the flat bets come from the lanes that have no flat skip, whose other rules differ: it is the nearest evidence, not the same bets. Not split by curve shape here. Settings chosen after seeing the results are said to be so.

Run on the host: python scripts/flat_trend_gate_study.py
"""
import datetime
import re
from collections import defaultdict

from kalshi_moonshot import config, db
from kalshi_moonshot.strategy import fee_per_contract

SINCE = datetime.datetime(2026, 11, 9, 5, 0, tzinfo=datetime.UTC).timestamp()
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
rows = st.execute("SELECT lane, mode, ts, contracts, stake_usd, exit_price, outcome, side, reason FROM decisions WHERE action='bet' AND stake_usd>0 AND lane LIKE 'crypto_multiscale%' AND ts>?", (SINCE,)).fetchall()
steps = [(0.0, 0.05), (0.05, 0.1), (0.1, 0.2), (0.2, 0.4), (0.4, 99.0)]
g = defaultdict(list)
untagged = 0
for r in rows:
    m = re.search(r"\[trend60 ([+-]?\d+(?:\.\d+)?)%\]", str(r["reason"] or ""))
    n, cost = float(r["contracts"] or 0), float(r["stake_usd"])
    if not m or n <= 0:
        untagged += 1
        continue
    if r["exit_price"] is not None:
        x = float(r["exit_price"]); v = n * (x - fee_per_contract(x)) - cost
    elif r["outcome"] in ("yes", "no"):
        v = (n if r["outcome"] == r["side"] else 0.0) - cost
    else:
        continue
    a = abs(float(m.group(1)))
    for lo, hi in steps:
        if lo <= a < hi:
            g[(lo, hi, r["lane"] if r["lane"] == "crypto_multiscale" else "variants")].append((v, cost))
print(f"bets with a trend60 tag and a result; untagged or unfilled skipped: {untagged}")
print("tag is the shown 1-decimal percent, so 0.0 means under 0.05 only roughly")
for who in ("crypto_multiscale", "variants"):
    print(f"\n{who}:")
    for lo, hi in steps:
        x = g.get((lo, hi, who), [])
        if x:
            c = sum(b for _, b in x); v = sum(a for a, _ in x)
            print(f"  |trend60| {lo:.2f} to {hi if hi < 99 else 'up'}: n {len(x):3d} net {v:+8.2f} on ${c:6.0f} ({v / c * 100:+5.1f}%) won {sum(1 for a, _ in x if a > 0)}")
print("DONE")
