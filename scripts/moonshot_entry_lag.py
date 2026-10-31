"""How much of the cheap side's rise has already happened when the moonshot buys? Read-only.

For every crypto / energy moonshot buy of the last DAYS days (paper and live), the bought side's ask is read from the tape at fixed seconds before the buy (BEFORE) and just after it (AFTER). The table gives the average ask at each offset, how far the ask
sat above its own LOW of the 60 s before the buy (the rise the buy paid for), and the share of that rise that was already in the price 3 s, 6 s and 10 s before the buy. If most of the rise is in by 6 s before, the 3-second dollar count is late: the move started well before the bar
was cleared. Buys are split by whether the side won, because a late buy of a loser is the cheap mistake and a late buy of a winner is the one that costs payout.

    scripts/cloud.sh run scripts/moonshot_entry_lag.py [DAYS=7]
"""
import bisect
import os
import statistics
import time

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
DAYS = float(os.environ.get("DAYS", "7"))
BEFORE, AFTER = (60, 30, 20, 10, 6, 3, 1, 0), (2, 5)

bets = main.db.execute(
    "SELECT d.ticker, d.ts, d.lane, d.mode, d.reason, w.result FROM decisions d LEFT JOIN windows w ON w.ticker = d.ticker "
    "WHERE d.lane IN ('crypto_moonshot','energy_moonshot') AND d.action='bet' AND d.ts > ? ORDER BY d.ts", (time.time() - DAYS * 86400,)).fetchall()


def bought_side(reason):
    """The side bought: the reason says 'yes costs' or 'no costs' / 'bought YES'."""
    r = (reason or "").lower()
    if "bought yes" in r or " yes costs" in r or "yes side costs" in r:
        return "yes"
    if "bought no" in r or " no costs" in r or "no side costs" in r:
        return "no"
    return None


rows = []
for b in bets:
    side = bought_side(b["reason"])
    if side is None:
        continue
    t0 = float(b["ts"])
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid >= 0 AND ask > 0 AND ts BETWEEN ? AND ? ORDER BY ts", (b["ticker"], t0 - 75, t0 + 8)).fetchall()
    if len(q) < 5:
        continue
    ts = [float(r[0]) for r in q]

    def ask_at(t):
        i = bisect.bisect_right(ts, t) - 1
        if i < 0:
            return None
        a = float(q[i][2])
        return a if side == "yes" else 1.0 - float(q[i][1])

    series = {off: ask_at(t0 - off) for off in BEFORE}
    series.update({-off: ask_at(t0 + off) for off in AFTER})
    if series[0] is None or series[60] is None:
        continue
    window = [ask_at(t0 - k) for k in range(0, 61)]
    low = min(x for x in window if x is not None)
    rise = series[0] - low
    rows.append({"series": series, "low": low, "rise": rise, "won": b["result"] == side, "mode": b["mode"], "ticker": b["ticker"]})
print(f"{len(rows)} moonshot buys with a tape over {DAYS:g} days ({sum(1 for r in rows if r['mode'] == 'trade')} live, the rest paper)\n")


def table(rs, label):
    if not rs:
        return
    print(f"{label}: {len(rs)} buys")
    print("  average ask at   " + "  ".join(f"{('t-' + str(o)) if o >= 0 else ('t+' + str(-o)):>6}" for o in list(BEFORE) + [-a for a in AFTER]))
    cells = []
    for o in list(BEFORE) + [-a for a in AFTER]:
        vals = [r["series"][o] for r in rs if r["series"].get(o) is not None]
        cells.append(f"{statistics.mean(vals):>6.3f}" if vals else f"{'-':>6}")
    print("                   " + "  ".join(cells))
    rise = statistics.mean(r["rise"] for r in rs)
    print(f"  the buy sits {rise * 100:.1f}c above its own 60 s low on average (median {statistics.median(r['rise'] for r in rs) * 100:.1f}c)")
    for off in (10, 6, 3):
        shares = [(r["series"][off] - r["low"]) / r["rise"] for r in rs if r["rise"] > 0.005 and r["series"].get(off) is not None]
        if shares:
            print(f"  share of that rise already in the ask {off:>2} s before the buy: median {statistics.median(shares) * 100:>3.0f}%  (25% of buys under {sorted(shares)[len(shares) // 4] * 100:.0f}%, 75% under {sorted(shares)[3 * len(shares) // 4] * 100:.0f}%)")
    print()


table(rows, "all")
table([r for r in rows if r["won"]], "buys that won")
table([r for r in rows if not r["won"]], "buys that lost")
print("DONE")
