"""Do the crypto 15-minute markets move in a synced cycle, and does the live lane bet against it? (2026-11-15, user: "we are betting when we shouldn't, and on the wrong side ... all crypto follows this cycle and they seem to sync with each other, some tie closer to other coins")

Questions, on the same windows (CLAUDE.md rule 4): (1) Sync: each 15-minute window's result per coin (read from the Kalshi perp reference's own tape: up if its value at the close is above its value at the open, which is what the market settles on, to within the index's difference from the perp), the share of coins that went up in each window, and how often most coins went the same way. (2) Cycle: how that market-wide direction follows the window before it (the chance the next window goes the same way, by the length of the current run). (3) Ties: the correlation of each pair of coins' window results and which coin's window result best predicts another's NEXT window (lead-lag). (4) The lane: each live crypto_multiscale bet since 2026-11-14 17:02 ET with a result, whether its side agreed with the PREVIOUS window's market-wide direction (the run it joined) and with the same window's final direction, and what each group made. The coin names label the report only; the finding is a curve behavior (breadth and persistence), not a rule on a coin. Not split by curve shape. A study of what the market did, not a rule.

Run on the host: python scripts/cycle_sync_study.py (DAYS=3 to change the look back).
"""
import bisect
import datetime
import json
import math
import os
import re
import time
from collections import defaultdict

from kalshi_moonshot import config, db
from kalshi_moonshot.watcher import perp_index
from kalshi_moonshot.strategy import fee_per_contract

ET = datetime.timezone(datetime.timedelta(hours=-5))
DAYS = float(os.environ.get("DAYS", "3"))
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
now = time.time()
SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M", "KXADA15M", "KXBCH15M", "KXBNB15M", "KXHYPE15M", "KXNEAR15M", "KXZEC15M")
res: dict[tuple[str, int], int] = {}
for ser in SERIES:
    kp = perp_index(ser)
    if not kp:
        continue
    g0 = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (kp, now - DAYS * 86400 - 1000)).fetchall()
    its, ivs = [float(x[0]) for x in g0], [float(x[1]) for x in g0]

    def at(t):
        i = bisect.bisect_right(its, t) - 1
        return ivs[i] if i >= 0 and t - its[i] <= 60.0 else None
    b0 = int((now - DAYS * 86400) // 900 * 900 + 900)
    for close in range(b0, int(now - 120) // 900 * 900 + 1, 900):
        o, c = at(close - 900), at(close)
        if o is not None and c is not None and o != c:
            res[(ser[2:-3], close)] = 1 if c > o else 0
coins = sorted({c for c, _ in res})
closes = sorted({t for _, t in res})
full = [t for t in closes if sum(1 for c in coins if (c, t) in res) >= max(5, int(len(coins) * 0.7))]
print(f"coins {len(coins)}, windows with most coins readable {len(full)} over {DAYS:g} days")
breadth = {}
for t in full:
    got = [res[(c, t)] for c in coins if (c, t) in res]
    breadth[t] = sum(got) / len(got)
hist = defaultdict(int)
for t in full:
    hist[round(breadth[t] * 5) / 5] += 1
print("\n(1) share of coins that went up in a window, in fifths: windows")
for k in sorted(hist):
    print(f"  {k:.1f}: {hist[k]}")
synced = sum(1 for t in full if breadth[t] >= 0.8 or breadth[t] <= 0.2)
print(f"  windows where 80% or more of the coins went the same way: {synced} of {len(full)} ({synced / len(full):.0%})")
d = {t: (1 if breadth[t] > 0.5 else 0 if breadth[t] < 0.5 else None) for t in full}
print("\n(2) the next window against the run so far (consecutive windows only; direction = most coins)")
runs = defaultdict(lambda: [0, 0])
for i in range(len(full)):
    if d[full[i]] is None:
        continue
    k = 1
    while i - k >= 0 and full[i - k] == full[i] - 900 * k and d[full[i - k]] == d[full[i]]:
        k += 1
    if i + 1 < len(full) and full[i + 1] == full[i] + 900 and d[full[i + 1]] is not None:
        runs[min(k, 5)][0] += 1
        runs[min(k, 5)][1] += d[full[i + 1]] == d[full[i]]
for k in sorted(runs):
    n, same = runs[k]
    print(f"  after a run of {k}{'+' if k == 5 else ' '} same-way window(s): next window the same way {same}/{n} ({same / max(n, 1):.0%})")
print("\n(3) ties: correlation of two coins' window results (1 = always the same way), strongest and weakest pairs; and lead-lag (a coin's result vs another's next window)")
def corr(a, b):
    n = len(a)
    if n < 20:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    va, vb = sum((x - ma) ** 2 for x in a), sum((y - mb) ** 2 for y in b)
    return None if va * vb == 0 else sum((x - ma) * (y - mb) for x, y in zip(a, b)) / math.sqrt(va * vb)
pairs = []
lead = []
for i, a in enumerate(coins):
    for b in coins[i + 1:]:
        ts = [t for t in full if (a, t) in res and (b, t) in res]
        c = corr([res[(a, t)] for t in ts], [res[(b, t)] for t in ts])
        if c is not None:
            pairs.append((c, a, b, len(ts)))
for a in coins:
    for b in coins:
        if a == b:
            continue
        ts = [t for t in full if (a, t) in res and (b, t + 900) in res]
        c = corr([res[(a, t)] for t in ts], [res[(b, t + 900)] for t in ts])
        if c is not None:
            lead.append((c, a, b, len(ts)))
pairs.sort(reverse=True)
print("  strongest:", ", ".join(f"{a}-{b} {c:.2f}" for c, a, b, n in pairs[:6]))
print("  weakest:  ", ", ".join(f"{a}-{b} {c:.2f}" for c, a, b, n in pairs[-6:]))
lead.sort(reverse=True)
print("  a coin's window result against another's NEXT window, strongest:", ", ".join(f"{a}->{b} {c:+.2f}" for c, a, b, n in lead[:6]), "| most negative:", ", ".join(f"{a}->{b} {c:+.2f}" for c, a, b, n in lead[-4:]))
print("\n(4) the live lane's bets since 11-14 17:02 against the cycle: side vs the PREVIOUS window's market-wide direction, and vs the final direction of its own window")
g = defaultdict(lambda: [0, 0.0, 0.0, 0])
for r in st.execute("SELECT ts, ticker, side, contracts, stake_usd, exit_price, outcome FROM decisions WHERE lane='crypto_multiscale' AND mode='trade' AND action='bet' AND stake_usd>0 AND ts>?", (datetime.datetime(2026, 11, 14, 17, 2, tzinfo=ET).timestamp(),)).fetchall():
    m = re.match(r"KX([A-Z]+)15M-26NOV(\d\d)(\d\d)(\d\d)-", r["ticker"])
    n, cost = float(r["contracts"] or 0), float(r["stake_usd"])
    if not m or n <= 0:
        continue
    close = int(datetime.datetime(2026, 11, int(m.group(2)), int(m.group(3)), int(m.group(4)), tzinfo=ET).timestamp()) // 900 * 900
    if r["exit_price"] is not None:
        x = float(r["exit_price"]); v = n * (x - fee_per_contract(x)) - cost
    elif r["outcome"] in ("yes", "no"):
        v = (n if r["outcome"] == r["side"] else 0.0) - cost
    else:
        continue
    prev = d.get(close - 900)
    mine = 1 if r["side"] == "yes" else 0
    key = "no previous window" if prev is None else "joined the previous window's direction" if prev == mine else "went against the previous window's direction"
    g[key][0] += 1; g[key][1] += v; g[key][2] += cost
    fin = d.get(close)
    if fin is not None:
        k2 = "side matched the window's final direction" if fin == mine else "side was against the window's final direction"
        g[k2][0] += 1; g[k2][1] += v; g[k2][2] += cost
for k, (n, v, c, _) in sorted(g.items()):
    print(f"  {k:52s}: n {n:3d} net ${v:+8.2f} on ${c:6.0f} ({v / max(c, 1) * 100:+.1f}%)")
print("DONE")
