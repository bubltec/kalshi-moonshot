"""How long should a stop-loss wait before it believes a dip? Calculated from the tape, not guessed.

Every favorites bet on record (15-minute, bought at 60c+) is replayed from its entry on its recorded
quotes under the live bundle's exits with one dial moved: the sale rests at 1.1x (filled at target on a
bid touch) and the 45% stop sells only after the bid has stayed under the line for WAIT seconds (a
recovery above the line resets the wait, exactly as watcher._stop_hit tracks it). WAIT runs 0 (sell on
first touch) to infinity (no stop). Returns are per dollar staked; the headline is also split by how
many seconds were left when the bid first touched the stop line, because a dip with ten minutes left
and a dip with forty seconds left are different animals. Standard errors clustered by 15-minute slot.

Read-only. Places nothing.
    docker compose exec -T bot python - < scripts/stop_wait_study.py
"""
import math
from datetime import datetime

from kalshi_moonshot import config, db
from kalshi_moonshot.journal import cluster_se
from kalshi_moonshot.store import Store

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)

LANES = ("commodity", "fx_favorite", "crypto_favorite", "crypto_socket", "crypto_guard",
         "fx_favorite_ctrl", "fx_favorite_hold", "fx_favorite_hover", "commodity_late")
STOP = 0.45
TP_X = 1.1
WAITS = (0.0, 10.0, 20.0, 30.0, 45.0, 60.0, 90.0, 120.0, math.inf)
BANDS = ((600, math.inf, ">10m"), (420, 600, "7-10m"), (300, 420, "5-7m"), (180, 300, "3-5m"),
         (120, 180, "2-3m"), (60, 120, "1-2m"), (0, 60, "<1m"))


def fitted_wait(left):
    """The rule the lanes run (strategy.stop_wait on a 15-minute market, cap 120 s): nothing inside 2 minutes, 120 s at 2-3, 45 s at 3-7, and nothing past 7 — an early dip is repricing, not noise."""
    if left > 420:
        return 0.0
    if left > 180:
        return 45.0
    if left > 120:
        return 120.0
    return 0.0


def simulate(entry, side, won, close_ts, path, wait):
    """(roi, first_touch_left) of one bet under the bundle with this stop wait. roi is per dollar in.
    `wait` is seconds, or a function of the seconds left when the current dip began."""
    target = entry * TP_X if entry * TP_X < 1.0 else None
    line = entry * (1.0 - STOP)
    since = None
    first_left = None
    for ts, bid, ask in path:
        b = bid if side == "yes" else 1.0 - ask
        if target is not None and b >= target - 1e-9:
            return target / entry - 1.0, first_left
        if b <= line + 1e-9:
            if since is None:
                since = ts
                if first_left is None:
                    first_left = close_ts - ts
            w = wait(close_ts - since) if callable(wait) else wait
            if ts - since >= w:
                return b / entry - 1.0, first_left
        else:
            since = None
    return (1.0 / entry - 1.0) if won else -1.0, first_left


rows = main.db.execute(
    f"""SELECT ts, ticker, side, ask, outcome, close_time FROM decisions
        WHERE action='bet' AND lane IN ({','.join('?' * len(LANES))}) AND ask>=0.60
          AND outcome IN ('yes','no') AND ticker LIKE '%15M%' ORDER BY ts""",
    LANES,
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
        bets.append((float(r["ask"]), r["side"] or "yes", r["outcome"] == (r["side"] or "yes"),
                     close_ts, path, str(r["close_time"])))

print(f"{len(bets)} favorites bets replayed (15-minute, 60c+, every lane's record pooled)")

# Overall: one row per wait.
print(f"\n{'stop wait':>10} {'mean ROI':>9} {'SE':>6} {'sold at target':>14} {'stopped':>8} {'held':>6}")
base = []
for w in WAITS:
    rois, held = [], 0
    sold = stopped = 0
    pairs = []
    for entry, side, won, close_ts, path, slot in bets:
        roi, _ = simulate(entry, side, won, close_ts, path, w)
        rois.append(roi)
        pairs.append((slot, roi))
        target = entry * TP_X if entry * TP_X < 1.0 else None
        if target is not None and roi >= target / entry - 1.0 - 1e-9:
            sold += 1
        elif roi <= -STOP + 0.06:
            stopped += 1
        else:
            held += 1
    mean = sum(rois) / len(rois)
    name = "no stop" if math.isinf(w) else f"{w:.0f}s"
    print(f"{name:>10} {mean:>+9.1%} {cluster_se(pairs, mean):>6.1%} {sold:>14} {stopped:>8} {held:>6}")
    base.append((w, mean))

def report(name, wait, note=""):
    rois = [simulate(e, sd, wn, ct, p, wait) for e, sd, wn, ct, p, _ in bets]
    vals = [r for r, _ in rois]
    pairs = [(slot, r) for (_, _, _, _, _, slot), (r, _) in zip(bets, rois, strict=True)]
    mean = sum(vals) / len(vals)
    print(f"{name:>10} {mean:>+9.1%} {cluster_se(pairs, mean):>6.1%}  {note}")
    return mean


report("fitted", fitted_wait, "(the rule the lanes run since 2026-10-26; the flat 10 s before it is the 10s row)")
report("cap 60", lambda left: 60.0 if left > 420 else 45.0 if left > 180 else 60.0 if left > 120 else 0.0,
       "(the previous live rule: no early-out, 2-3 minutes capped at a minute)")
report("cap 90", lambda left: 60.0 if left > 420 else 45.0 if left > 180 else 90.0 if left > 120 else 0.0,
       "(2-3 minutes left waits 90 s instead of 60)")
report("cap 120+", lambda left: 60.0 if left > 420 else 45.0 if left > 300 else 90.0 if left > 180 else 120.0 if left > 120 else 0.0,
       "(cap 120, and also 3-5 minutes to 90 s, its nominal best)")

# By seconds left at the first touch of the stop line: which wait earns most, per band.
print("\nmean ROI by seconds left when the stop line was first touched (bets that never touched are in every column alike):")
hdr = "  ".join(f"{('no stop' if math.isinf(w) else f'{w:.0f}s'):>8}" for w in WAITS)
print(f"{'left':>6} {'bets':>5}  {hdr}")
for lo, hi, label in BANDS:
    group = []
    for entry, side, won, close_ts, path, slot in bets:
        _, left = simulate(entry, side, won, close_ts, path, 0.0)
        if left is not None and lo <= left < hi:
            group.append((entry, side, won, close_ts, path, slot))
    if len(group) < 20:
        print(f"{label:>6} {len(group):>5}  (too few to read)")
        continue
    cells = []
    for w in WAITS:
        rois = [simulate(e, sd, wn, ct, p, w)[0] for e, sd, wn, ct, p, _ in group]
        cells.append(f"{sum(rois) / len(rois):>+8.1%}")
    print(f"{label:>6} {len(group):>5}  {'  '.join(cells)}")
