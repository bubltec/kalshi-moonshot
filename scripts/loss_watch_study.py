"""How long should the commodity hard stop (watch_loss) wait, and how deep should its line be? Replayed from the tape.

Every commodity 15-minute bet on record is replayed from its entry on the recorded quotes with only the hard loss (strategy.hard_loss)
acting: the side's bid has to reach LOSS under the price paid, stay there WAIT seconds (a print back above the line restarts the
count, as watcher._stop_hit does), and a bid more than LIFT above the low of that visit is a bounce and is held. Nothing else sells:
no take-profit, no cash rules, no burst exit, no 50c watch. A bet that is never sold settles. Returns are per dollar staked, before
fees; the sale is at the bid of the quote that satisfied the rule (a real sale lands a few cents under it).

The tape keeps about one quote per second, so a wait of 3 seconds or less is read at that resolution, not the socket's.

Read-only. Places nothing.
    scripts/cloud.sh run scripts/loss_watch_study.py [LANES=commodity] [MODES=trade,observe]
"""
import math
import os
from datetime import datetime

from kalshi_moonshot import config, db
from kalshi_moonshot.journal import cluster_se
from kalshi_moonshot.store import Store

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)

LANES = tuple(os.environ.get("LANES", "commodity").split(","))
MODES = tuple(os.environ.get("MODES", "trade,observe").split(","))
WAITS = (0.0, 3.0, 5.0, 10.0, 20.0, math.inf)
LOSSES = tuple(float(x) for x in os.environ.get("LOSSES", "0.40,0.30,0.25,0.20").split(","))
LIFTS = tuple(float(x) for x in os.environ.get("LIFTS", "0,0.08").split(","))


def simulate(entry, side, won, path, loss, wait, lift):
    """(roi per dollar, sold_early) for one bet under the hard loss with these dials."""
    line = entry * (1.0 - loss)
    since = None
    low = 0.0
    for ts, bid, ask in path:
        b = bid if side == "yes" else 1.0 - ask
        if b <= line + 1e-9:
            if since is None:
                since, low = ts, b
            low = min(low, b)
            if ts - since >= wait and not (lift > 0 and b > low + lift):
                return b / entry - 1.0, True
        else:
            since, low = None, 0.0
    return ((1.0 / entry - 1.0) if won else -1.0), False


rows = main.db.execute(
    f"""SELECT ts, ticker, side, ask, outcome, close_time, mode FROM decisions
        WHERE action='bet' AND lane IN ({','.join('?' * len(LANES))}) AND mode IN ({','.join('?' * len(MODES))})
          AND outcome IN ('yes','no') AND ticker LIKE '%15M%' ORDER BY ts""",
    LANES + MODES,
).fetchall()

bets = []
cache: dict[str, list] = {}
for r in rows:
    if r["ticker"] not in cache:
        cache[r["ticker"]] = tape.execute(
            "SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 ORDER BY ts", (r["ticker"],)
        ).fetchall()
    try:
        close_ts = datetime.fromisoformat(str(r["close_time"])).timestamp()
    except (TypeError, ValueError):
        continue
    path = [q for q in cache[r["ticker"]] if float(r["ts"]) <= q[0] <= close_ts]
    if path:
        bets.append((float(r["ask"]), r["side"] or "yes", r["outcome"] == (r["side"] or "yes"), path, str(r["close_time"])))

print(f"{len(bets)} {'/'.join(LANES)} bets replayed ({'/'.join(MODES)}), hard loss only")
losers = sum(1 for e, sd, wn, p, _ in bets if not wn)
print(f"settled as held: {len(bets) - losers} won, {losers} lost, mean "
      f"{sum(((1 / e - 1) if wn else -1.0) for e, sd, wn, p, _ in bets) / len(bets):+.1%} per dollar\n")

for lift in LIFTS:
    print(f"--- lift {lift:.2f} ({'a bounce off the low is held' if lift else 'any bid under the line can sell once the wait is up'}) ---")
    print(f"{'line':>6} {'wait':>7} {'mean ROI':>9} {'SE':>6} {'sold':>5} {'sold winners':>12} {'avg sale ROI':>12}", flush=True)
    for loss in LOSSES:
        for w in WAITS:
            rois, pairs = [], []
            sold = sold_win = 0
            sale_rois = []
            for entry, side, won, path, slot in bets:
                roi, early = simulate(entry, side, won, path, loss, w, lift)
                rois.append(roi)
                pairs.append((slot, roi))
                if early:
                    sold += 1
                    sale_rois.append(roi)
                    sold_win += 1 if won else 0
            mean = sum(rois) / len(rois)
            name = "never" if math.isinf(w) else f"{w:.0f}s"
            avg = f"{sum(sale_rois) / len(sale_rois):>+12.1%}" if sale_rois else f"{'-':>12}"
            print(f"{loss:>6.0%} {name:>7} {mean:>+9.1%} {cluster_se(pairs, mean):>6.1%} {sold:>5} {sold_win:>12} {avg}")
        print()
