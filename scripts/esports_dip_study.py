"""Does buying a dip in a match market pay? CS2, Dota 2 and the two football series, replayed from the tape. Places nothing.

Every 2 minutes through the live part of each market (a market with 20 or more quote changes in the prior 10 minutes, price 25-75c) each side is a possible entry.
  dip      the side's bid is at least `DIP` under its highest bid of the prior 10 minutes
  control  every other moment in the same price band
Exit: buy at the ask, sell at the bid the first time it is `BOUNCE` over the price paid, else at the bid after `HOLD` minutes. Fees both ways.
Standard errors are taken across events (the two teams' markets and neighbouring samples move together).
    docker compose exec -T bot python - < scripts/esports_dip_study.py
"""
import bisect
import math
import statistics
from collections import defaultdict

from kalshi_moonshot import config, db
from kalshi_moonshot.strategy import fee_per_contract as fee

DIP, BOUNCE, HOLD, STEP = 0.08, 0.08, 600, 120
s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
GROUPS = {"CS2": "KXCS2GAME", "Dota 2": "KXDOTA2GAME", "Europa League (football)": "KXUELGAME", "Conference League (football)": "KXUECLGAME"}


def se(rows, f):
    per = defaultdict(list)
    for r in rows:
        per[r["event"]].append(f(r))
    means = [sum(v) / len(v) for v in per.values()]
    if len(means) < 2:
        return float("nan")
    mm = sum(means) / len(means)
    return math.sqrt(sum((x - mm) ** 2 for x in means) / (len(means) - 1) / len(means)) * 100


for name, prefix in GROUPS.items():
    tickers = [r[0] for r in tape.execute("SELECT DISTINCT ticker FROM quotes WHERE ticker LIKE ?", (prefix + "-%",)).fetchall()]
    rows = []
    for tk in tickers:
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid IS NOT NULL AND ask IS NOT NULL ORDER BY ts", (tk,)).fetchall()
        if len(q) < 100:
            continue
        ts = [r[0] for r in q]
        event = tk.rsplit("-", 1)[0]
        for side in ("yes", "no"):
            bs = [r[1] if side == "yes" else 1 - r[2] for r in q]
            ak = [r[2] if side == "yes" else 1 - r[1] for r in q]
            t = ts[0] + 600
            while t < ts[-1] - HOLD:
                i = bisect.bisect_right(ts, t) - 1
                t += STEP
                lo = bisect.bisect_left(ts, ts[i] - 600)
                if i - lo < 20:  # a quiet market: the match is not on
                    continue
                bid, ask = bs[i], ak[i]
                if not (0.25 <= ask <= 0.75) or bid <= 0 or ask - bid > 0.10:
                    continue
                peak = max(bs[lo:i + 1])
                cost = ask + fee(ask)
                sold = None
                for j in range(i + 1, len(q)):
                    if bs[j] >= ask + BOUNCE:
                        sold, bounced = bs[j], True
                        break
                    if ts[j] - ts[i] >= HOLD:
                        sold, bounced = bs[j], False
                        break
                if sold is None:
                    continue
                rows.append({"event": event, "dip": peak - bid >= DIP, "bounce": bounced, "roi": (sold - fee(sold)) / cost - 1, "spread": ask - bid})
    if not rows:
        print(f"{name}: no usable samples")
        continue
    print(f"\n{name}: {len(tickers)} markets, {len(rows)} sampled entries over {len({r['event'] for r in rows})} events; average spread {statistics.mean(r['spread'] for r in rows) * 100:.1f}c")
    for label, sel in (("after a dip (>= 8c under the 10-min high)", [r for r in rows if r["dip"]]), ("control: any other moment", [r for r in rows if not r["dip"]])):
        if len(sel) < 30:
            print(f"   {label}: too few ({len(sel)})")
            continue
        print(f"   {label}: n={len(sel)}, bounces +8c {statistics.mean(r['bounce'] for r in sel):.0%}, return per $ after fees {statistics.mean(r['roi'] for r in sel):+.1%} ±{se(sel, lambda r: r['roi']):.1f}%")
