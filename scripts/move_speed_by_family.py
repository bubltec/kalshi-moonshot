"""How fast do the 15-minute curves move, crypto against commodities? Read-only.

Per window, the per-second YES mid; for each span (3, 10, 30 s) the largest move inside the window. The table gives the share of windows with a move of at least 10c, 20c and 40c, and the median of the largest 3 s move.
    docker compose ... exec -T -e DAYS=6 bot python - < scripts/move_speed_by_family.py
"""
import bisect
import os
import re
import statistics
import time

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
DAYS = float(os.environ.get("DAYS", "6"))
FAMILIES = {"crypto": ("BTC", "ETH", "SOL", "XRP", "DOGE"), "commodity": ("GOLD", "SILVER", "WTI", "BRENT", "COPPER", "NATGAS", "PLATINUM", "PALLADIUM")}
SPANS, BARS = (3, 10, 30), (0.10, 0.20, 0.40)

wins = main.db.execute("SELECT ticker, close_ts FROM windows WHERE result IN ('yes','no') AND close_ts > ? ORDER BY close_ts", (time.time() - DAYS * 86400,)).fetchall()
rows = {f: [] for f in FAMILIES}
for w in wins:
    m = re.match(r"^KX([A-Z]+)15M-", w["ticker"])
    fam = next((f for f, cs in FAMILIES.items() if m and m.group(1) in cs), None)
    if fam is None:
        continue
    close = float(w["close_ts"])
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], close - 900, close - 20)).fetchall()
    if len(q) < 30:
        continue
    ts = [float(r[0]) for r in q]
    mid = {}
    for sec in range(int(close - 900), int(close - 20)):
        i = bisect.bisect_right(ts, sec + 0.999) - 1
        if i >= 0 and sec + 0.999 - ts[i] <= 15:
            mid[sec] = (float(q[i][1]) + float(q[i][2])) / 2
    secs = sorted(mid)
    best = {}
    for span in SPANS:
        best[span] = max((abs(mid[t] - mid[t - span]) for t in secs if t - span in mid), default=0.0)
    rows[fam].append(best)
for fam, rs in rows.items():
    print(f"{fam}: {len(rs)} windows")
    for span in SPANS:
        shares = "  ".join(f">={b * 100:.0f}c {100 * sum(1 for r in rs if r[span] >= b - 1e-9) / max(1, len(rs)):>5.1f}%" for b in BARS)
        print(f"  over {span:>2}s: {shares} | median biggest move {statistics.median(r[span] for r in rs) * 100 if rs else 0:.0f}c")
print("DONE")
