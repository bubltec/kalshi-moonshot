"""Does the underlying's move before the buy say anything about how a commodity bet ends? Replayed from the tape.

Every commodity 15-minute bet on record is matched with its underlying's price (the instrument's symbol in the tape's feed_ticks) at the
buy and SECONDS earlier. The bet goes WITH the move when the underlying rose before a YES or fell before a NO, AGAINST it when the
reverse, and FLAT when it did not move. Each group is scored on holding to settlement (stops, bursts and cash-outs ignored, so old and
new rules compare), per dollar staked, before fees. A lane's entry_spot_seconds would skip the AGAINST group.

Read-only. Places nothing.
    scripts/cloud.sh run scripts/spot_trend_entry_study.py [SECONDS=60,120,180,300] [LANES=commodity] [MODES=trade,observe]
"""
import os

from kalshi_moonshot import config, db
from kalshi_moonshot.commodities import symbol_for
from kalshi_moonshot.journal import cluster_se
from kalshi_moonshot.store import Store

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)

SECONDS = tuple(float(x) for x in os.environ.get("SECONDS", "60,120,180,300").split(","))
LANES = tuple(os.environ.get("LANES", "commodity").split(","))
MODES = tuple(os.environ.get("MODES", "trade,observe").split(","))

rows = main.db.execute(
    f"""SELECT ts, ticker, side, ask, outcome, close_time FROM decisions
        WHERE action='bet' AND lane IN ({','.join('?' * len(LANES))}) AND mode IN ({','.join('?' * len(MODES))})
          AND outcome IN ('yes','no') AND ticker LIKE '%15M%' ORDER BY ts""",
    LANES + MODES,
).fetchall()


def px_at(symbol, t):
    got = tape.execute("SELECT px FROM feed_ticks WHERE symbol=? AND ts <= ? AND ts > ? ORDER BY ts DESC LIMIT 1", (symbol, t, t - 30.0)).fetchone()
    return float(got[0]) if got else None


bets = []
for r in rows:
    sym = symbol_for(r["ticker"].split("-")[0])
    if not sym:
        continue
    now = px_at(sym, float(r["ts"]))
    if now is None:
        continue
    side = r["side"] or "yes"
    ask = float(r["ask"])
    roi = (1.0 / ask - 1.0) if r["outcome"] == side else -1.0
    moves = {n: (None if (then := px_at(sym, float(r["ts"]) - n)) is None else now - then) for n in SECONDS}
    bets.append((side, roi, moves, str(r["close_time"])))

print(f"{len(bets)} {'/'.join(LANES)} bets with spot on the tape; hold to settlement")
print(f"{'secs':>5} {'group':>8} {'bets':>5} {'won':>5} {'mean ROI':>9} {'SE':>6}", flush=True)
for n in SECONDS:
    groups = {"WITH": [], "AGAINST": [], "FLAT": []}
    for side, roi, moves, slot in bets:
        m = moves[n]
        if m is None:
            continue
        key = "FLAT" if abs(m) < 1e-12 else ("WITH" if (m > 0) == (side == "yes") else "AGAINST")
        groups[key].append((slot, roi))
    for name, g in groups.items():
        if not g:
            continue
        mean = sum(r for _, r in g) / len(g)
        won = sum(1 for _, r in g if r > 0)
        se = cluster_se(g, mean) if len(g) > 1 else 0.0
        print(f"{n:>5.0f} {name:>8} {len(g):>5} {won:>5} {mean:>+9.1%} {se:>6.1%}")
