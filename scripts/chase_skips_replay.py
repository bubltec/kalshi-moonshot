"""Would chasing a price that ran away have paid? (2026-11-08, user: "can we still fill it in those situations by moving the buy order further? some way to calculate it based on the increase rate per tick?")
Question: the live buys that were skipped because the ask ran over the lane's limit before the order went out ("skipped: $0.00 offered at L"), on every lane: had the order chased to the ask the tape shows at that second, what would it have returned, held to the result, against what the lane would have paid at its decision ask? Net per $1 after the full taker fee at each price; the chase = the ask at the skip minus the decision ask, in cents; bands of the chase fixed before the results (under 3c, 3 to 6c, over 6c); and, where the skip's note carries the curve model's predicted net, the win chance it implies, P = (net + 1 + fee/ask) x ask, repriced at the chase price p: P / p - 1 - fee(p) / p (the skip notes mostly do not carry it).
Reported: skips with a settled window, the chase bands, net per $1 at the decision ask and at the chase ask with standard errors, and the repriced model edge at the chase ask (mean).
Not covered: that a chase moves the fill (the order may not fill at the tape's ask), the later skips whose windows have not settled, the other reasons for a skip.

    scripts/hostrun3.sh-style: docker compose exec -T bot python - < scripts/chase_skips_replay.py
"""

import math
import re
import time

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
rows = st.db.execute(
    "SELECT d.ts,d.lane,d.ticker,d.side,d.ask,d.reason,w.result FROM decisions d JOIN windows w ON w.ticker=d.ticker "
    "WHERE d.ts>? AND d.reason LIKE 'skipped: $%offered at%' AND w.result IN ('yes','no') ORDER BY d.ts",
    (time.time() - 5 * 86400,),
).fetchall()
print("skipped buys (an ask that ran over the limit) with a settled window:", len(rows))
out = []
for r in rows:
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (r["ticker"], r["ts"] - 1.5, r["ts"] + 1.5)).fetchall()
    if not q:
        continue
    b, a = float(q[-1][1]), float(q[-1][2])
    ask_now = a if r["side"] == "yes" else 1.0 - b
    ask_d = float(r["ask"])
    won = 1.0 if r["result"] == r["side"] else 0.0
    net = lambda p, won=won: (won / p) - 1.0 - fee(p) / p  # noqa: E731
    m = re.search(r"curve model ([+-][0-9.]+) per", r["reason"] or "")
    pred = float(m.group(1)) if m else None
    rep = None
    if pred is not None:
        P = (pred + 1.0 + fee(ask_d) / ask_d) * ask_d
        rep = P / ask_now - 1.0 - fee(ask_now) / ask_now
    out.append((ask_now - ask_d, net(ask_d), net(ask_now), rep))
print("read:", len(out))


def ms(xs):
    n = len(xs)
    if n == 0:
        return "n=0"
    m = sum(xs) / n
    se = (sum((x - m) ** 2 for x in xs) / max(n - 1, 1)) ** 0.5 / math.sqrt(n)
    return f"{m:+.3f} +/- {se:.3f}"


for label, lo, hi in (("under 3c", -1, 0.03), ("3 to 6c", 0.03, 0.06), ("over 6c", 0.06, 1)):
    g = [o for o in out if lo <= o[0] < hi]
    reps = [o[3] for o in g if o[3] is not None]
    extra = f" | the model's repriced edge at the chase ask {sum(reps) / len(reps):+.3f} (n={len(reps)})" if reps else ""
    print(f"   chase {label:9s} n={len(g):3d} | net per $1 at the decision ask {ms([o[1] for o in g])} | at the chase ask {ms([o[2] for o in g])}{extra}")
g = out
print(f"   all           n={len(g):3d} | at the decision ask {ms([o[1] for o in g])} | at the chase ask {ms([o[2] for o in g])}")
print("DONE")
