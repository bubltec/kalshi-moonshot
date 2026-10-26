"""'Watch a match from an hour before; once a side hits 70c, buy it if it has been trending up.' CS2 and Dota 2 matches replayed from the tape. Places nothing.

For every finished market (a team to win), from 60 minutes before the listed start to the last quote, the first moment that
  - the ask is 70-90c (the book within 8c), and
  - the mid is at least `thr` higher than it was 10 minutes earlier (needs 10 minutes of history),
is the buy, at the ask, held to the result, fee at purchase only. Compared with the first moment the ask is 70-90c whatever its trend (the plain favorite), and with the first
moment it is 70-90c but NOT trending up. Pre-match entries are split from live ones. Standard errors are taken across matches.
    docker compose exec -T bot python - < scripts/momentum_70_study.py
"""
import bisect
import math
import re
import statistics
import time
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from kalshi_moonshot import config, db
from kalshi_moonshot.kalshi import KalshiClient
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
client = KalshiClient(s.base_url, s.key_id, s.private_key_path, s.max_rps, s.page_size)
MONTHS = {m: i + 1 for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
NY = ZoneInfo("America/New_York")
LOOK, WATCH = 600.0, 3600.0
THRS = (0.03, 0.05, 0.10)


def start_of(tk):
    m = re.match(r"[A-Z0-9]+-(\d\d)([A-Z]{3})(\d\d)(\d\d)(\d\d)", tk)
    return datetime(2000 + int(m[1]), MONTHS[m[2]], int(m[3]), int(m[4]), int(m[5]), tzinfo=NY).timestamp()


def se(rows, key="ret"):
    per = defaultdict(list)
    for r in rows:
        per[r["ev"]].append(r[key])
    mm = [sum(v) / len(v) for v in per.values()]
    return math.sqrt(sum((x - sum(mm) / len(mm)) ** 2 for x in mm) / (len(mm) - 1) / len(mm)) * 100 if len(mm) > 1 else float("nan")


def show(label, g):
    if len(g) < 8:
        print(f"  {label:46} only {len(g)}")
        return
    print(f"  {label:46} n={len(g):3} ({len({r['ev'] for r in g}):3} matches)  price {statistics.mean(r['ask'] for r in g):.2f}  won {statistics.mean(r['win'] for r in g):.0%}  hold return {statistics.mean(r['ret'] for r in g):+.1%} ±{se(g):.1f}")


for game, prefix in (("CS2", "KXCS2GAME"), ("Dota 2", "KXDOTA2GAME")):
    tickers = [r[0] for r in tape.execute("SELECT ticker FROM quotes WHERE ticker LIKE ? GROUP BY ticker HAVING MAX(ts) < ? AND COUNT(*) >= 40", (prefix + "-%", time.time() - 1800)).fetchall()]
    entries = {thr: [] for thr in THRS}
    plain, flat = [], []
    n_mk = 0
    for tk in tickers:
        try:
            res = client.market(tk).get("result")
        except Exception:
            continue
        if res not in ("yes", "no"):
            continue
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 ORDER BY ts", (tk,)).fetchall()
        if len(q) < 20:
            continue
        n_mk += 1
        ts = [r[0] for r in q]
        st, win, ev = start_of(tk), res == "yes", tk.rsplit("-", 1)[0]
        found = {}
        for i, (t, b, a) in enumerate(q):
            if t < st - WATCH or not (0.70 <= a <= 0.90) or a - b > 0.08:
                continue
            j = bisect.bisect_right(ts, t - LOOK) - 1
            if j < 0 or t - LOOK - ts[j] > 120 or ts[j] < st - WATCH - 300:
                continue  # no reading from 10 minutes earlier
            move = (a + b) / 2 - (q[j][1] + q[j][2]) / 2
            cost = a + fee(a)
            row = {"ev": ev, "ask": a, "win": win, "ret": ((1 - cost) / cost) if win else -1.0, "live": t >= st, "move": move}
            if "plain" not in found:
                found["plain"] = True; plain.append(row)
            if move < 0.03 and "flat" not in found:
                found["flat"] = True; flat.append(row)
            for thr in THRS:
                if move >= thr and thr not in found:
                    found[thr] = True; entries[thr].append(row)
    print(f"\n{'=' * 110}\n{game}: {n_mk} finished markets with a result")
    show("the plain favorite: first time 70-90c", plain)
    show("first time 70-90c and NOT up 3c+ in 10 min", flat)
    for thr in THRS:
        g = entries[thr]
        show(f"trending up {thr * 100:.0f}c+ over 10 min", g)
        show(f"   ... bought before the start", [r for r in g if not r["live"]])
        show(f"   ... bought during the match", [r for r in g if r["live"]])
