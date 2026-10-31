"""Which times of day do the commodity favorites win more than they cost? Read-only.

Two views by the ET hour of the market's CLOSE, in blocks of BLOCK hours:
  our bets   every settled live commodity bet: how many, how many won, how many the entry prices say should win, the surplus in wins (z = surplus over its standard deviation), and the profit before fees
  all markets  every settled commodity market on the tape, no bot choices: at SCAN minutes before the close, each side whose ask is in 0.78-0.92 is "bought" at that ask (fee included) and held to settlement
Each row also shows the edge of the older and the newer half of the days: a time of day only counts if both halves agree. With 24/BLOCK blocks tested, a few will look good by chance; z over 2 and agreement of both halves is the bar.

    scripts/cloud.sh run scripts/hours_of_day.py [BLOCK=3] [SCAN=6]
"""
import math
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
ET = ZoneInfo("America/New_York")
BLOCK = int(os.environ.get("BLOCK", "3"))
SCAN = float(os.environ.get("SCAN", "6"))
WANT = {"GOLD", "SILVER", "PLATINUM", "PALLADIUM", "COPPER", "WTI", "BRENT", "NATGAS"}


def blk(close_ts):
    return datetime.fromtimestamp(close_ts, ET).hour // BLOCK * BLOCK


def table(title, rows):
    """rows: (close_ts, ask, won, contracts)"""
    print(f"== {title}")
    mid = sorted(r[0] for r in rows)[len(rows) // 2] if rows else 0
    print(f"{'ET hours':>9} | {'n':>4} {'won':>4} {'exp':>6} {'z':>5} {'win%':>6} {'avg ask':>7} {'edge':>8} | {'old':>8} {'new':>8}")
    for b in range(0, 24, BLOCK):
        g = [r for r in rows if blk(r[0]) == b]
        if len(g) < 8:
            continue
        won = sum(1 for r in g if r[2])
        exp = sum(r[1] for r in g)
        var = sum(r[1] * (1 - r[1]) for r in g)
        edge = lambda x: sum((1.0 if r[2] else 0.0) - r[1] - fee_per_contract(r[1]) for r in x) / len(x) * 100  # noqa: E731
        old, new = [r for r in g if r[0] < mid], [r for r in g if r[0] >= mid]
        oe = f"{edge(old):>+7.1f}c" if len(old) >= 5 else "       -"
        ne = f"{edge(new):>+7.1f}c" if len(new) >= 5 else "       -"
        print(f"{b:>2}-{b + BLOCK:>2}     | {len(g):>4} {won:>4} {exp:>6.1f} {(won - exp) / math.sqrt(var):>+5.1f} {won / len(g):>6.1%} {exp / len(g) * 100:>6.1f}c {edge(g):>+7.1f}c | {oe} {ne}")
    print()


bets = main.db.execute("SELECT d.ask, d.side, w.result, w.close_ts FROM decisions d JOIN windows w ON w.ticker = d.ticker WHERE d.lane='commodity' AND d.action='bet' AND d.mode='trade' AND w.result IN ('yes','no') AND d.contracts > 0").fetchall()
table(f"our {len(bets)} live commodity bets", [(float(b["close_ts"]), float(b["ask"]), b["side"] == b["result"], 1.0) for b in bets])

rows = []
for w in main.db.execute("SELECT ticker, series, close_ts, result FROM windows WHERE result IN ('yes','no') AND close_ts > 0").fetchall():
    m = re.match(r"^KX([A-Z]+)15M$", w["series"] or "")
    if not m or m.group(1) not in WANT:
        continue
    close = float(w["close_ts"])
    q = tape.execute("SELECT bid, ask FROM quotes WHERE ticker=? AND ts <= ? AND ts > ? AND bid IS NOT NULL AND ask IS NOT NULL ORDER BY ts DESC LIMIT 1", (w["ticker"], close - SCAN * 60, close - SCAN * 60 - 20)).fetchone()
    if not q:
        continue
    for side, ask in (("yes", float(q[1])), ("no", 1.0 - float(q[0]))):
        if 0.78 <= ask <= 0.92:
            rows.append((close, ask, w["result"] == side, 1.0))
table(f"all commodity markets, favorite side bought {SCAN:g} min before the close ({len(rows)} sides)", rows)
print("DONE")
