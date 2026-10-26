"""eSoccer: is the favorite (ask 70-92c) overpriced enough that buying the OTHER side at its ask pays after fees? Replayed from the tape. Places nothing.

Every 30 s in a finished match, once a third of the recorded span has passed, if a side's ask is 70-92c with a spread of 8c or less, buy the opposite side at its ask (fee at purchase, hold to the result).
Rows: price band of the favorite; all moments, and only the first moment per match; also the earlier and later half of the matches by time (does it hold up out of sample?).
Standard errors across matches.
    docker compose exec -T bot python - < scripts/esoccer_fade_study.py
"""
import bisect
import math
import statistics
import time
from collections import defaultdict

from kalshi_moonshot import config, db
from kalshi_moonshot.kalshi import KalshiClient
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
client = KalshiClient(s.base_url, s.key_id, s.private_key_path, s.max_rps, s.page_size)
SAMPLE = 30.0
rows = []
for (tk,) in tape.execute("SELECT ticker FROM quotes WHERE ticker LIKE 'KXESOCCERGAME-%' GROUP BY ticker HAVING MAX(ts) < ? AND COUNT(*) >= 60", (time.time() - 1800,)).fetchall():
    try:
        res = client.market(tk).get("result")
    except Exception:
        continue
    time.sleep(0.3)
    if res not in ("yes", "no"):
        continue
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 ORDER BY ts", (tk,)).fetchall()
    if len(q) < 60:
        continue
    ts = [r[0] for r in q]
    t = ts[0] + (ts[-1] - ts[0]) / 3
    first = set()
    while t < ts[-1] - 20:
        i = bisect.bisect_right(ts, t) - 1
        t += SAMPLE
        bid_y, ask_y = q[i][1], q[i][2]
        for fav in ("yes", "no"):
            fa = ask_y if fav == "yes" else 1 - bid_y
            fb = bid_y if fav == "yes" else 1 - ask_y
            if not (0.70 <= fa <= 0.92) or fa - fb > 0.08:
                continue
            oa = 1 - fb  # the other side's ask
            if not (0.05 <= oa <= 0.35):
                continue
            c = oa + fee(oa)
            other_wins = (res == "no") if fav == "yes" else (res == "yes")
            rows.append({"cl": tk.rsplit("-", 1)[0], "t0": ts[0], "fa": fa, "oa": oa, "win": other_wins, "ret": ((1 - c) / c) if other_wins else -1.0,
                         "first": (tk, fav) not in first})
            first.add((tk, fav))
print(f"{len(rows):,} moments in {len({r['cl'] for r in rows})} matches")


def show(name, g):
    if len(g) < 30:
        print(f"  {name:34} only {len(g)}")
        return
    per = defaultdict(list)
    for r in g:
        per[r["cl"]].append(r["ret"])
    mm = [sum(v) / len(v) for v in per.values()]
    sd = math.sqrt(sum((x - sum(mm) / len(mm)) ** 2 for x in mm) / (len(mm) - 1) / len(mm))
    print(f"  {name:34} n={len(g):5} ({len(per):3} matches) pays {statistics.mean(r['oa'] for r in g):.2f}, wins {statistics.mean(r['win'] for r in g):.0%} (breakeven {statistics.mean(r['oa'] + fee(r['oa']) for r in g):.0%}), return {statistics.mean(r['ret'] for r in g):+6.1%} +-{100 * sd:.1f}")


mid = sorted(r["t0"] for r in rows)[len(rows) // 2] if rows else 0
for label, sel in (("all moments", lambda r: True), ("first moment per match side", lambda r: r["first"])):
    print(f"\n{label}")
    for lo, hi in ((0.70, 0.80), (0.80, 0.92), (0.70, 0.92)):
        g = [r for r in rows if sel(r) and lo <= r["fa"] < hi]
        show(f"favorite {lo:.2f}-{hi:.2f}", g)
        show("   earlier matches", [r for r in g if r["t0"] <= mid])
        show("   later matches", [r for r in g if r["t0"] > mid])
