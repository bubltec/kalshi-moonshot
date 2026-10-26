"""Does 'buy the higher-ranked team when it is priced 70-90c' beat buying every 70-90c side? CS2 matches replayed from the tape. Places nothing.

Matches where both teams are in Valve's world ranking (the file dated before the matches). At three moments (30 min before the listed start, 5 min before it, 2 min after it) every side priced
70-90c (book within 10c) is a candidate. It is 'higher-ranked' when its team has the better world rank than the opponent's. Buy at the ask and hold to the result; fees at purchase only.
Standard errors are taken across matches (one match's moments and its two markets are not independent).
    docker compose exec -T bot python - < scripts/rank_filter_study.py
"""
import math
import re
import statistics
import time
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from kalshi_moonshot import config, db
from kalshi_moonshot.kalshi import KalshiClient
from kalshi_moonshot.rankings import Rankings, norm
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
client = KalshiClient(s.base_url, s.key_id, s.private_key_path, s.max_rps, s.page_size)
when, table = Rankings()._valve_table(time.time())
MONTHS = {m: i + 1 for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
NY = ZoneInfo("America/New_York")
MOMENTS = (("30 min before", -1800), ("5 min before", -300), ("2 min in", 120))


def start_of(tk):
    m = re.match(r"[A-Z0-9]+-(\d\d)([A-Z]{3})(\d\d)(\d\d)(\d\d)", tk)
    return datetime(2000 + int(m[1]), MONTHS[m[2]], int(m[3]), int(m[4]), int(m[5]), tzinfo=NY).timestamp()


def se(rows, key):
    per = defaultdict(list)
    for r in rows:
        per[r["ev"]].append(r[key])
    mm = [sum(v) / len(v) for v in per.values()]
    return math.sqrt(sum((x - sum(mm) / len(mm)) ** 2 for x in mm) / (len(mm) - 1) / len(mm)) * 100 if len(mm) > 1 else float("nan")


tickers = [r[0] for r in tape.execute("SELECT ticker FROM quotes WHERE ticker LIKE 'KXCS2GAME-%' GROUP BY ticker HAVING MAX(ts) < ? AND COUNT(*) >= 30", (time.time() - 1800,)).fetchall()]
events = {}
for tk in tickers:
    try:
        m = client.market(tk)
    except Exception:
        continue
    if m.get("result") in ("yes", "no"):
        events.setdefault(tk.rsplit("-", 1)[0], []).append((tk, re.sub(r"\s+wins\??$", "", m.get("title", "")).strip(), m["result"] == "yes"))
rows, n_both = [], 0
for ev, mk in events.items():
    if len(mk) != 2:
        continue
    ranks = [table.get(norm(nm)) for _, nm, _ in mk]
    if not all(ranks):
        continue
    n_both += 1
    st = start_of(mk[0][0])
    for i, (tk, nm, win) in enumerate(mk):
        mine, other = ranks[i][0], ranks[1 - i][0]
        for lab, off in MOMENTS:
            q = tape.execute("SELECT bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid > 0 ORDER BY ts DESC LIMIT 1", (tk, st + off - 600, st + off)).fetchone()
            if not q or q[1] - q[0] > 0.10 or not 0.70 <= q[1] <= 0.90:
                continue
            cost = q[1] + fee(q[1])
            rows.append({"ev": ev, "moment": lab, "higher": mine < other, "gap": other - mine, "ask": q[1], "win": win, "ret": ((1 - cost) / cost) if win else -1.0})
print(f"Valve standings as of {when}: {n_both} matches with both teams ranked; {len(rows)} candidate sides (70-90c, book within 10c) across {len({r['ev'] for r in rows})} matches")


def show(label, g):
    if len(g) < 5:
        print(f"  {label}: only {len(g)}")
        return
    print(f"  {label:42} n={len(g):3} ({len({r['ev'] for r in g})} matches)  price {statistics.mean(r['ask'] for r in g):.2f}  won {statistics.mean(r['win'] for r in g):.0%}  hold return {statistics.mean(r['ret'] for r in g):+.1%} ±{se(g, 'ret'):.1f}")


show("every 70-90c side", rows)
show("the higher-ranked team (the filter)", [r for r in rows if r["higher"]])
show("the lower-ranked team", [r for r in rows if not r["higher"]])
show("higher-ranked by 25+ places", [r for r in rows if r["higher"] and r["gap"] >= 25])
show("higher-ranked by 60+ places", [r for r in rows if r["higher"] and r["gap"] >= 60])
for lab, _ in MOMENTS:
    show(f"filter, {lab}", [r for r in rows if r["higher"] and r["moment"] == lab])
