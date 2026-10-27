"""WOULD HOLDING THROUGH AN EARLY DIP HAVE HELPED: every finished favorites bet on record (15-minute, 60c+, every lane pooled), replayed from its entry on the recorded quotes with no take-profit (it is off) and the 45% stop, with ONE dial moved:
how many seconds before the close the stop is allowed to act at all (the stop's wait stays the lane's calculated rule, strategy.stop_wait with a 120 s cap). 'now' is the live rule (the stop can sell the moment the line is crossed, even 10 minutes out);
'from 240/180/120' means a dip before that point is ignored, and a bid still under the line when the window opens is sold on the usual wait; 'never' is hold to the result. Per dollar staked, errors clustered by slot,
split by family and by period (the last 3 days against everything earlier), because the 3-day record disagrees with the longer one.
Read-only. Places nothing.
    docker compose exec -T bot python - < scripts/stop_early_study.py
"""
import math
import time
from datetime import datetime

from kalshi_moonshot import config, db
from kalshi_moonshot.journal import cluster_se
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import stop_wait

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
STOP, CAP = 0.45, 120.0
POLICIES = (("now (live)", 9999), ("from 240 s", 240), ("from 180 s", 180), ("from 120 s", 120), ("never", -1))
rows = main.db.execute(
    """SELECT ts, ticker, side, ask, outcome, close_time FROM decisions
       WHERE action='bet' AND ask>=0.60 AND outcome IN ('yes','no') AND ticker LIKE '%15M%' AND COALESCE(lane,'') NOT IN ('manual','burst_flip','settlement_flip','crypto_moonshot','settlement_edge')
       ORDER BY ts""").fetchall()


def simulate(entry, side, won, close_ts, path, active_left):
    """ROI per dollar: the stop (45% under the price paid) acts only when at most `active_left` seconds remain, after the calculated wait."""
    line = entry * (1.0 - STOP)
    since = None
    for ts, bid, ask in path:
        b = bid if side == "yes" else 1.0 - ask
        left = close_ts - ts
        if b <= line + 1e-9 and left <= active_left:
            if since is None:
                since = ts
            if ts - since >= stop_wait(CAP, close_ts - since, True):
                return b / entry - 1.0
        else:
            since = None
    return (1.0 / entry - 1.0) if won else -1.0


cache: dict[str, list] = {}
seen = set()
bets = []
cut = time.time() - 3 * 86400
for r in rows:
    key = (r["ticker"], round(float(r["ask"]), 2), r["side"])
    if key in seen:
        continue  # the paper lanes copy each other: one bet per market, side and price
    seen.add(key)
    if r["ticker"] not in cache:
        cache[r["ticker"]] = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid>0 AND ask>0 ORDER BY ts", (r["ticker"],)).fetchall()
    try:
        close_ts = datetime.fromisoformat(str(r["close_time"])).timestamp()
    except (TypeError, ValueError):
        continue
    path = [q for q in cache[r["ticker"]] if float(r["ts"]) <= q[0] <= close_ts]
    if len(path) < 5:
        continue
    series = r["ticker"].split("-")[0]
    fam = "crypto" if series[2:].startswith(("BTC", "ETH", "SOL", "XRP", "DOGE")) else "fx" if any(x in series for x in ("USD", "EUR", "GBP", "JPY", "AUD", "CAD")) else "commodity"
    bets.append((float(r["ask"]), r["side"] or "yes", r["outcome"] == (r["side"] or "yes"), close_ts, path, fam, float(r["ts"]) >= cut, str(close_ts)[:6]))
print(len(bets), "bets replayed;", sum(1 for b in bets if b[6]), "in the last 3 days")


def table(title, group):
    if len(group) < 25:
        return
    print(f"\n{title} ({len(group)} bets), mean ROI per dollar (+- cluster se):")
    for name, active in POLICIES:
        rois = [simulate(e, sd, wn, ct, p, active) for e, sd, wn, ct, p, *_ in group]
        pairs = [(str(int(b[3] // 900)), r) for b, r in zip(group, rois, strict=True)]
        mean = sum(rois) / len(rois)
        print(f"  {name:11} {mean:+7.1%} +-{cluster_se(pairs, mean):.1%}")


table("all", bets)
table("last 3 days", [b for b in bets if b[6]])
table("before that", [b for b in bets if not b[6]])
for fam in ("crypto", "fx", "commodity"):
    table(f"{fam}, all", [b for b in bets if b[5] == fam])
    table(f"{fam}, last 3 days", [b for b in bets if b[5] == fam and b[6]])
