"""How high should the resting sale sit? The lanes sell at 1.1x the price paid; this asks the tape whether 1.2x, 1.3x, 1.4x would have paid more.

Every favorites bet on record (15-minute, bought at 60c+) is replayed from its entry on its recorded
quotes under the live exits — the 45% stop with the calculated wait (strategy.stop_wait), the sale at
TP_X times the price paid (a target only exists when entry * TP_X < 1.00, exactly as take_profit_hit
rules it) — with TP_X the only dial moved. Returns are per dollar staked, standard errors clustered by
15-minute slot. Gross of fees (the sale rests as a maker, so it pays none when it fills).

The second table answers "could we have gotten more?" directly: of the bets whose bid reached each
multiple, how many went on to reach the next one — and what holding to settlement paid instead.

Read-only. Places nothing.
    docker compose exec -T bot python - < scripts/tp_multiple_study.py
"""
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
GRID = (1.1, 1.15, 1.2, 1.25, 1.3, 1.4, 1.5, None)  # None = never sell early, settlement or stop


def fitted_wait(left):
    """strategy.stop_wait on a 15-minute market with the lanes' 120 s cap, as it now runs."""
    if left > 420:
        return 0.0
    if left > 180:
        return 45.0
    if left > 120:
        return 120.0
    return 0.0


def simulate(entry, side, won, close_ts, path, tp_x):
    """(roi, how) of one bet: 'target', 'stop' or 'held' (to settlement). roi is per dollar in."""
    target = entry * tp_x if tp_x is not None and entry * tp_x < 1.0 else None
    line = entry * (1.0 - STOP)
    since = None
    for ts, bid, ask in path:
        b = bid if side == "yes" else 1.0 - ask
        if target is not None and b >= target - 1e-9:
            return target / entry - 1.0, "target"
        if b <= line + 1e-9:
            if since is None:
                since = ts
            if ts - since >= fitted_wait(close_ts - since):
                return b / entry - 1.0, "stop"
        else:
            since = None
    return (1.0 / entry - 1.0) if won else -1.0, "held"


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

print(f"\n{'sale at':>8} {'mean ROI':>9} {'SE':>6} {'at target':>9} {'stopped':>8} {'held':>6} {'avg win':>8}")
for tp in GRID:
    rois, pairs, wins = [], [], []
    counts = {"target": 0, "stop": 0, "held": 0}
    for entry, side, won, close_ts, path, slot in bets:
        roi, how = simulate(entry, side, won, close_ts, path, tp)
        rois.append(roi)
        pairs.append((slot, roi))
        counts[how] += 1
        if how == "target":
            wins.append(roi)
    mean = sum(rois) / len(rois)
    name = "never" if tp is None else f"{tp:.2f}x"
    avg = f"{sum(wins) / len(wins):+.1%}" if wins else "  -"
    print(f"{name:>8} {mean:>+9.1%} {cluster_se(pairs, mean):>6.1%} {counts['target']:>9} {counts['stop']:>8} {counts['held']:>6} {avg:>8}")

# How far did the bid actually travel after entry? The ceiling on any cap, read off the same paths.
print("\nof bets whose bid reached each multiple of the price paid, how many reached the next:")
levels = (1.1, 1.2, 1.3, 1.4, 1.5)
reached = dict.fromkeys(levels, 0)
for entry, side, _won, _close_ts, path, _slot in bets:
    peak = 0.0
    for _ts, bid, ask in path:
        b = bid if side == "yes" else 1.0 - ask
        peak = max(peak, b / entry)
    for lv in levels:
        if peak >= lv - 1e-9:
            reached[lv] += 1
prev, prev_n = "entry", len(bets)
for lv in levels:
    print(f"  reached {lv:.1f}x: {reached[lv]:>5} bets ({reached[lv] / len(bets):.0%} of all, {reached[lv] / prev_n:.0%} of those at {prev})")
    prev, prev_n = f"{lv:.1f}x", max(reached[lv], 1)
