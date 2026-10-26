"""Dump per-second flow features for held favorites to a JSON-lines file, for scripts/flow_pattern_study.py. Read-only (the tape and the windows). Places nothing.

For each finished 15-minute crypto window and each side whose bid is 0.80-0.97, every second from minute 8 to 10 seconds before the close:
  against1/3/10   contracts traded against the side (NO contracts for a YES holder) over the last 1, 3 and 10 seconds;  with3  contracts traded with it over the last 3 seconds
  n3              trades in the last 3 seconds;  burst  the last 3 seconds' volume against that market's own median per-second volume over the 2 minutes before (1 = normal)
  dbid5, dbid30   the side's bid now minus its bid 5 and 30 seconds ago;  spread; left (seconds to the close); bid
  drop            the side's bid falls at least 10c under the current one at some point in the next 30 seconds (the thing to warn of);  lost (the side lost); win_minus_bid after the sale fee is left to the reader
    docker compose exec -T bot python - /data/flow_feats.jsonl < scripts/flow_features_dump.py
"""
import bisect
import json
import statistics
import sys

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M")
out = open(sys.argv[1], "w")
n = 0
for m in main.db.execute(f"SELECT ticker, series, result, close_ts FROM windows WHERE series IN ({','.join('?' * len(SERIES))}) AND result IN ('yes','no') ORDER BY close_ts", SERIES).fetchall():
    close = m["close_ts"]
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts>? AND ts<=? AND bid > 0 AND ask > 0 ORDER BY ts", (m["ticker"], close - 600, close)).fetchall()
    fl = tape.execute("SELECT ts, yes_vol, no_vol, n FROM flow_1s WHERE ticker=? AND ts>? AND ts<=? ORDER BY ts", (m["ticker"], int(close - 605), int(close))).fetchall()
    if len(q) < 30 or len(fl) < 30:
        continue
    qts = [r[0] for r in q]
    fby = {int(r[0]): (r[1], r[2], r[3]) for r in fl}
    for side in ("yes", "no"):
        win = 1.0 if m["result"] == side else 0.0
        bids = [r[1] if side == "yes" else 1 - r[2] for r in q]
        asks = [r[2] if side == "yes" else 1 - r[1] for r in q]

        def adverse(t, k):
            a = w = tr = 0.0
            for j in range(k):
                f = fby.get(int(t) - j)
                if f:
                    yv, nv, c = f
                    a += (nv if side == "yes" else yv)
                    w += (yv if side == "yes" else nv)
                    tr += c
            return a, w, tr

        for sec in range(420, 9, -1):
            t = close - sec
            i = bisect.bisect_right(qts, t) - 1
            if i < 0 or t - qts[i] > 30:
                continue
            bid = bids[i]
            if not (0.80 <= bid <= 0.97):
                continue
            a1, _, _ = adverse(t, 1)
            a3, w3, n3 = adverse(t, 3)
            a10, _, _ = adverse(t, 10)
            per = [sum(fby.get(int(t) - j - 1, (0, 0, 0))[:2]) for j in range(120)]
            base = statistics.median(per)
            burst = (a3 + w3) / (3 * base + 1)
            i5, i30 = bisect.bisect_right(qts, t - 5) - 1, bisect.bisect_right(qts, t - 30) - 1
            j = bisect.bisect_right(qts, t + 30)
            nxt = bids[i + 1:j]
            drop = int(bool(nxt) and (bid - min(nxt)) >= 0.10)
            out.write(json.dumps({"tk": m["ticker"], "series": m["series"], "t": close, "left": sec, "bid": bid, "spread": asks[i] - bid, "a1": a1, "a3": a3, "w3": w3, "a10": a10, "n3": n3, "burst": burst,
                                  "dbid5": bid - bids[i5] if i5 >= 0 else 0.0, "dbid30": bid - bids[i30] if i30 >= 0 else 0.0, "drop": drop, "lost": 1.0 - win, "hold_minus_sell": win - (bid - fee(bid))}) + "\n")
            n += 1
print(n, "rows")
