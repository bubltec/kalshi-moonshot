"""Do the 100% losses of crypto_favorite share an entry signature? For every live bet: how far the index was on the winning side of the strike at the buy, in the coin's own usual move over the time left (z), against the result.
Then the P&L if bets under each z floor had not been bought (the actual result of the bets kept, strike-through sales included). Places nothing.
    scripts/cloud.sh run scripts/favorite_entry_margin_study.py [SINCE=2026-10-28]
"""
import bisect
import math
import os
from datetime import datetime, timezone

from kalshi_moonshot import config, db
from kalshi_moonshot.kalshiws import index_for_series
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
from kalshi_moonshot.digest import EXIT_SLIP

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
since = datetime.fromisoformat(os.environ.get("SINCE", "2026-10-28")).replace(tzinfo=timezone.utc).timestamp()
bets = main.db.execute(
    "SELECT d.ts, d.ticker, d.side, d.ask, d.stake_usd, d.exit_price, d.outcome, w.strike, w.close_ts, w.series FROM decisions d JOIN windows w ON w.ticker = d.ticker "
    "WHERE d.lane = 'crypto_favorite' AND d.action = 'bet' AND d.mode = 'trade' AND d.ts > ? AND d.outcome IN ('yes','no') AND w.strike > 0 ORDER BY d.ts", (since,)).fetchall()
rows = []
for b in bets:
    ix = index_for_series(b["series"])
    t = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value > 0 ORDER BY ts", (ix, b["ts"] - 600, b["ts"] + 1)).fetchall()
    if len(t) < 100:
        continue
    ts, v = [x[0] for x in t], [x[1] for x in t]
    rets = [math.log(v[i] / v[i - 1]) for i in range(1, len(v)) if ts[i] - ts[i - 1] <= 3]
    sig = math.sqrt(sum(r * r for r in rets) / max(len(rets), 1))  # per tick (about 1 s)
    left = b["close_ts"] - b["ts"]
    margin = (v[-1] - b["strike"]) / b["strike"] * (1 if b["side"] == "yes" else -1)
    z = margin / (sig * math.sqrt(max(left, 1))) if sig > 0 else 99
    won = b["outcome"] == b["side"]
    cost = b["ask"] + fee(b["ask"])
    ret = ((b["exit_price"] - EXIT_SLIP - fee(b["exit_price"])) / cost - 1) if b["exit_price"] is not None else ((1.0 if won else 0.0) / cost - 1)
    rows.append({"z": z, "left": left, "won": won, "pnl": b["stake_usd"] * ret, "stake": b["stake_usd"], "ask": b["ask"], "sold": b["exit_price"] is not None, "series": b["series"][2:5]})
print(f"{len(rows)} bets\n{'z bucket':>10} {'n':>4} {'won %':>6} {'held lost':>9} {'P&L $':>8}")
edges = (-9, 0, 0.5, 1, 1.5, 2, 3, 99)
for lo, hi in zip(edges, edges[1:]):
    g = [r for r in rows if lo <= r["z"] < hi]
    if g:
        print(f"{lo:>4}..{hi:<4} {len(g):>4} {100 * sum(r['won'] for r in g) / len(g):>6.1f} {sum(1 for r in g if not r['won'] and not r['sold']):>9} {sum(r['pnl'] for r in g):>+8.2f}")
print("\nz floor: bets kept, held losses left, P&L of the kept bets")
for floor in (0, 0.5, 1, 1.5, 2, 2.5, 3):
    k = [r for r in rows if r["z"] >= floor]
    print(f"  >= {floor:<4} kept {len(k):>3} of {len(rows)}  held losses {sum(1 for r in k if not r['won'] and not r['sold']):>2}  P&L {sum(r['pnl'] for r in k):+8.2f}")
print("\nby time left at the buy (s):")
for lo, hi in ((0, 120), (120, 240), (240, 360), (360, 480), (480, 900)):
    g = [r for r in rows if lo <= r["left"] < hi]
    if g:
        print(f"  {lo:>3}-{hi:<3} n {len(g):>3} won {100 * sum(r['won'] for r in g) / len(g):>5.1f}%  held lost {sum(1 for r in g if not r['won'] and not r['sold']):>2}  P&L {sum(r['pnl'] for r in g):+8.2f}")
print("\nby coin:")
for c in sorted({r["series"] for r in rows}):
    g = [r for r in rows if r["series"] == c]
    print(f"  {c} n {len(g):>3} won {100 * sum(r['won'] for r in g) / len(g):>5.1f}% held lost {sum(1 for r in g if not r['won'] and not r['sold']):>2} P&L {sum(r['pnl'] for r in g):+8.2f}")
