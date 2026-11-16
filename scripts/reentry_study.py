"""What happens when the lane buys again on a market it already traded? (2026-11-15 night, user: look at the re-entry)

One question, same bets (CLAUDE.md rule 4): every crypto_multiscale lane bet since 2026-11-14 17:02 ET (live; paper variants separately) that was NOT the first bet of its lane on that market, i.e. a re-entry, with its result (its own exit or settlement, fee on every leg). Grouped by how the earlier bet on that market ended (a profit take, a stop, any other exit, held) and by whether the new bet took the same side or the opposite one (a flip). For each group: n, share that made money, the new bets' net and net per dollar staked, the average gap in seconds from the earlier exit to the new buy, and the price difference between the earlier sale and the new buy on the same side. Also the round trip: the earlier bet and the re-entry together against what the earlier bet alone made. Not split by curve shape. A first look at whether buying back after an exit pays, as the profit take assumes ("we can always buy back").

Run on the host: python scripts/reentry_study.py
"""
import datetime
from collections import defaultdict

from kalshi_moonshot import config, db
from kalshi_moonshot.strategy import fee_per_contract

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 14, 17, 2, tzinfo=ET).timestamp()
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
rows = st.execute("SELECT lane, mode, ts, ticker, side, contracts, stake_usd, exit_price, exit_ts, exit_reason, outcome FROM decisions WHERE lane LIKE 'crypto_multiscale%' AND action='bet' AND stake_usd>0 AND ts>? ORDER BY ts", (SINCE,)).fetchall()
by = defaultdict(list)
for r in rows:
    n, cost = float(r["contracts"] or 0), float(r["stake_usd"])
    if n <= 0:
        continue
    if r["exit_price"] is not None:
        x = float(r["exit_price"]); v = n * (x - fee_per_contract(x)) - cost
        why = str(r["exit_reason"] or "")
        kind = "profit take" if "profit take" in why else "stop" if "max loss" in why else "other exit"
    elif r["outcome"] in ("yes", "no"):
        v = (n if r["outcome"] == r["side"] else 0.0) - cost; kind = "held"; x = None
    else:
        continue
    by[(r["lane"], r["ticker"])].append(dict(ts=float(r["ts"]), side=r["side"], cost=cost, n=n, v=v, kind=kind, exit_ts=float(r["exit_ts"]) if r["exit_ts"] else None, exit_px=float(r["exit_price"]) if r["exit_price"] is not None else None, entry=cost / n))
for scope in ("live", "paper variants"):
    g = defaultdict(list)
    for (lane, tk), bets in by.items():
        if (lane == "crypto_multiscale") != (scope == "live"):
            continue
        for i in range(1, len(bets)):
            prev, new = bets[i - 1], bets[i]
            flip = "flip (opposite side)" if prev["side"] != new["side"] else "same side"
            gap = new["ts"] - prev["exit_ts"] if prev["exit_ts"] else None
            drift = (new["entry"] - prev["exit_px"]) if prev["exit_px"] is not None and prev["side"] == new["side"] else None
            g[(prev["kind"], flip)].append((new, prev, gap, drift))
    print(f"\n{scope}: re-entries by how the earlier bet ended and which side the new bet took")
    for (kind, flip), x in sorted(g.items()):
        c = sum(a[0]["cost"] for a in x)
        w = sum(1 for a in x if a[0]["v"] > 0)
        gaps = [a[2] for a in x if a[2] is not None]
        dr = [a[3] for a in x if a[3] is not None]
        rt = sum(a[0]["v"] + a[1]["v"] for a in x)
        alone = sum(a[1]["v"] for a in x)
        print(f"  earlier {kind:12s} -> {flip:22s}: n {len(x):3d} | won {w / len(x):4.0%} | new bets net ${sum(a[0]['v'] for a in x):+8.2f} ({sum(a[0]['v'] for a in x) / max(c, 1) * 100:+5.1f}% on ${c:6.0f}) | gap {sum(gaps) / len(gaps) if gaps else float('nan'):5.0f}s | rebuy price vs sale {sum(dr) / len(dr) if dr else float('nan'):+.3f} | round trip ${rt:+7.2f} vs the earlier bet alone ${alone:+7.2f}")
print("DONE")
