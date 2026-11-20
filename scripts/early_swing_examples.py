"""Examples of the early cheap-side swing's commodity bets, to read by eye (user, 2026-11-20: "show me some examples of bets that this took" / "lets identify whats wrong"). The same bets as scripts/early_swing_commodity.py (commodity windows on Kalshi's perp reference, first reading 60-240 s after the open with an ask of 0.20-0.45, either side), 30 drawn by a fixed seed (15 whose +100% sell filled, 15 that did not), each with the side's bid path after the buy (every 30 s: the bid, as a multiple of the ask), the best bid reached, when, the settlement and the net at +100% and held. Also the share of bets whose best bid before the close reached 1.25x, 1.5x, 2x and 3x the ask, and what those that reached 2x settled at. Run: SPOT=0 scripts/cloud_job.sh scripts/early_swing_examples.py"""
import bisect, random
from datetime import datetime, timezone
from kalshi_moonshot import binaryrisk as br, config, db
from kalshi_moonshot.perpref import index_for_series as pidx
from kalshi_moonshot.kalshiws import index_for_series as crypto_index
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
START = datetime(2026, 11, 9, 5, tzinfo=timezone.utc).timestamp()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
wins = st.execute("SELECT ticker, series, close_ts, result FROM windows WHERE result IN ('yes','no') AND close_ts > ? ORDER BY close_ts", (START + 7200,)).fetchall()
out = []
seen = set()
for w in wins:
    if crypto_index(w["series"]) or not pidx(w["series"]): continue
    close = float(w["close_ts"]); opened = close - 900.0
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (w["ticker"], opened - 60, close)).fetchall()
    if len(q) < 20: continue
    qt, qb, qa = [float(r[0]) for r in q], [float(r[1]) for r in q], [float(r[2]) for r in q]
    sc = 100.0 if max(qa) > 1.5 else 1.0
    qb = [x / sc for x in qb]; qa = [x / sc for x in qa]
    end = bisect.bisect_right(qt, close - 15.0)
    for age in range(60, 241, 30):
        j = bisect.bisect_right(qt, opened + age) - 1
        if j < 0 or opened + age - qt[j] > 30.0: continue
        for side, ask in (("yes", qa[j]), ("no", 1.0 - qb[j])):
            if not 0.20 <= ask < 0.45 or (w["ticker"], side) in seen: continue
            seen.add((w["ticker"], side))
            bids = [(qt[m] - qt[j], qb[m] if side == "yes" else 1.0 - qa[m]) for m in range(j + 1, end)]
            if not bids: continue
            won = (w["result"] == "yes") == (side == "yes")
            best = max(bids, key=lambda x: x[1])
            tgt = ask * 2
            hit = next((x for x in bids if x[1] >= tgt), None) if tgt < 0.99 else None
            held = ((1.0 if won else 0.0) - ask - fee(ask)) / ask
            n100 = (tgt - fee(tgt) - ask - fee(ask)) / ask if hit else held
            path = [(a, round(max(b for tt, b in bids if tt <= a + 15) / ask, 2) if any(tt <= a + 15 for tt, b in bids) else None) for a in range(30, 600, 30)]
            out.append(dict(t=w["ticker"], side=side, age=age, ask=ask, won=won, best=best[1] / ask, best_at=best[0], hit=hit is not None, hit_at=hit[0] if hit else None, held=held, n100=n100, path=path, left=close - opened - age))
print(f"{len(out)} commodity bets")
for k in (1.25, 1.5, 2.0, 3.0):
    g = [o for o in out if o["best"] >= k]
    print(f"best bid reached {k}x the ask: {len(g)}/{len(out)} = {len(g)/len(out):.2f}; of those settled in the side's favour: {sum(o['won'] for o in g)/max(len(g),1):.2f}")
hits = [o for o in out if o["hit"]]
print(f"hit 2x: {len(hits)}: median seconds to hit {sorted(o['hit_at'] for o in hits)[len(hits)//2]:.0f}, settled in favour {sum(o['won'] for o in hits)/len(hits):.2f}, mean net at +100% {sum(o['n100'] for o in hits)/len(hits):+.3f}")
miss = [o for o in out if not o["hit"]]
print(f"missed: {len(miss)}: settled in favour {sum(o['won'] for o in miss)/len(miss):.2f}, mean best bid {sum(o['best'] for o in miss)/len(miss):.2f}x ask, mean net {sum(o['n100'] for o in miss)/len(miss):+.3f}")
rnd = random.Random(7)
pick = rnd.sample(hits, min(15, len(hits))) + rnd.sample(miss, min(15, len(miss)))
for o in pick:
    print(f"\n{o['t']} buy {o['side'].upper()} at {o['ask']:.2f}, {o['age']}s after the open, {o['left']:.0f}s left | best bid {o['best']:.2f}x at +{o['best_at']:.0f}s | 2x {'filled at +%.0fs' % o['hit_at'] if o['hit'] else 'never'} | settled {'FOR' if o['won'] else 'AGAINST'} | net +100% {o['n100']:+.2f}, held {o['held']:+.2f}")
    print("   bid as a multiple of the ask, each 30 s: " + " ".join(f"{p[1]}" if p[1] is not None else "-" for p in o["path"][:12]))
