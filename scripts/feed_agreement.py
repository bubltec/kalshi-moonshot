"""How often does our commodity price feed call the settled direction? (2026-11-11, user on KXCOPPER15M-26NOV110630 YES: "why doesn't curve exit pick that up?" Our BiQuote copper stayed 6 bp over its window-open value to the close while Kalshi's settlement source fell through the target.)
Question: for every settled commodity 15-minute window in the tape, does the sign of our feed's move from the window open to the close (the reading every spot exit and the curve use) match Kalshi's result? By series label (a report label only, rule 1) and by the size of our feed's move in the market's own units is not available here, so by absolute move in basis points: under 2 / 2-5 / 5-10 / 10+. Read-only.
    scripts/cloud.sh run scripts/feed_agreement.py
"""

import collections
import math

from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store

s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
wins = st.db.execute("SELECT ticker, close_ts, result FROM windows WHERE result IN ('yes','no') ORDER BY close_ts").fetchall()
by = collections.defaultdict(lambda: [0, 0])
bymove = collections.defaultdict(lambda: [0, 0])
for w in wins:
    ser = w["ticker"].split("-")[0]
    ix = variants.biquote_index(ser)
    if not ix or variants.crypto_index(ser):
        continue
    close = float(w["close_ts"])
    a = tape.execute("SELECT value FROM index_ticks WHERE index_id=? AND ts>=? AND ts<=? AND value>0 ORDER BY ts LIMIT 1", (ix, close - 905, close - 880)).fetchone()
    b = tape.execute("SELECT value FROM index_ticks WHERE index_id=? AND ts<=? AND ts>=? AND value>0 ORDER BY ts DESC LIMIT 1", (ix, close, close - 20)).fetchone()
    if not a or not b:
        continue
    bp = math.log(float(b[0]) / float(a[0])) * 1e4
    ours = "yes" if bp >= 0 else "no"
    ok = int(ours == w["result"])
    by[ser][0] += 1
    by[ser][1] += ok
    m = abs(bp)
    k = "under 2 bp" if m < 2 else "2 to 5 bp" if m < 5 else "5 to 10 bp" if m < 10 else "10 bp and up"
    bymove[k][0] += 1
    bymove[k][1] += ok
tot = [sum(v[0] for v in by.values()), sum(v[1] for v in by.values())]
print(f"settled commodity windows with our feed at the open and the close: {tot[0]} | our feed's direction matched the result on {tot[1]} ({tot[1] / tot[0]:.1%})")
for k, v in sorted(by.items()):
    print(f"  {k:16s} n {v[0]:5d} | matched {v[1] / v[0]:.1%} | wrong {v[0] - v[1]}")
print("by the size of our feed's own move over the window:")
for k in ("under 2 bp", "2 to 5 bp", "5 to 10 bp", "10 bp and up"):
    v = bymove[k]
    if v[0]:
        print(f"  {k:14s} n {v[0]:5d} | matched {v[1] / v[0]:.1%} | wrong {v[0] - v[1]}")
print("DONE")
