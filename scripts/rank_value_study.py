"""Do team rankings price CS2 matches better than Kalshi does, and is there value in the 70-90c band? Replayed from the tape. Places nothing.

For every finished CS2 match whose two teams are both in Valve's published world ranking (the file dated before the matches, so nothing is looked up after the fact):
  model     P(team A wins) = 1 / (1 + exp(-b * (points A - points B) / 100)), with b fitted on these matches (one number, so little to overfit)
  market    the mid price of team A's market 20-60 minutes before the listed start, book within 6c
Reported: how many matches qualify, Brier scores (lower is better) for the model and the market, and for sides priced 70-90c how often they won against the price and what buying
the ones the model rates higher than the ask by 5c or more would have returned after fees.
    docker compose exec -T bot python - < scripts/rank_value_study.py
"""
import bisect
import math
import re
import statistics
import time
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
print(f"Valve standings as of {when}: {len(table)} teams")
MONTHS = {m: i + 1 for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
NY = ZoneInfo("America/New_York")


def start_of(tk):
    m = re.match(r"[A-Z0-9]+-(\d\d)([A-Z]{3})(\d\d)(\d\d)(\d\d)", tk)
    return datetime(2000 + int(m[1]), MONTHS[m[2]], int(m[3]), int(m[4]), int(m[5]), tzinfo=NY).timestamp()


tickers = [r[0] for r in tape.execute("SELECT ticker FROM quotes WHERE ticker LIKE 'KXCS2GAME-%' GROUP BY ticker HAVING MAX(ts) < ? AND COUNT(*) >= 30", (time.time() - 1800,)).fetchall()]
events = {}
for tk in tickers:
    try:
        m = client.market(tk)
    except Exception:
        continue
    if m.get("result") in ("yes", "no"):
        events.setdefault(tk.rsplit("-", 1)[0], []).append((tk, re.sub(r"\s+wins\??$", "", m.get("title", "")).strip(), m["result"] == "yes"))
print(f"{len(events)} CS2 matches with a result in the tape; {sum(1 for v in events.values() if len(v) == 2)} with both teams' markets")
rows, unranked = [], 0
for ev, mk in events.items():
    if len(mk) != 2:
        continue
    (ta, na, wa), (tb, nb, wb) = mk
    ra, rb = table.get(norm(na)), table.get(norm(nb))
    if not ra or not rb:
        unranked += 1
        continue
    st = start_of(ta)
    prices = {}
    for tk, nm, w in mk:
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid > 0 ORDER BY ts", (tk, st - 3600, st - 1200)).fetchall()
        if q and q[-1][2] - q[-1][1] <= 0.06:
            prices[tk] = (q[-1][1], q[-1][2])
    if len(prices) != 2:
        continue
    rows.append({"ev": ev, "diff": (ra[1] - rb[1]) / 100.0, "sides": [(ta, wa, prices[ta]), (tb, wb, prices[tb])]})
print(f"{unranked} had a team outside the ranking; {len(rows)} matches have both teams ranked and a tight pre-match book on both markets")
if len(rows) < 8:
    raise SystemExit("too few matches to say anything")
best = min((sum((1 / (1 + math.exp(-b * r["diff"])) - (1.0 if r["sides"][0][1] else 0.0)) ** 2 for r in rows), b) for b in [i / 20 for i in range(1, 80)])
b = best[1]
brier_m = brier_k = 0.0
for r in rows:
    p = 1 / (1 + math.exp(-b * r["diff"])); y = 1.0 if r["sides"][0][1] else 0.0
    mid = sum(x for x in r["sides"][0][2]) / 2
    brier_m += (p - y) ** 2; brier_k += (mid - y) ** 2
print(f"fitted slope b = {b:.2f}; Brier on {len(rows)} matches: rankings model {brier_m / len(rows):.4f}, market {brier_k / len(rows):.4f}, coin flip {statistics.mean((0.5 - (1.0 if r['sides'][0][1] else 0.0)) ** 2 for r in rows):.4f}")
band = []
for r in rows:
    pa = 1 / (1 + math.exp(-b * r["diff"]))
    for i, (tk, w, (bid, ask)) in enumerate(r["sides"]):
        p = pa if i == 0 else 1 - pa
        if 0.70 <= ask <= 0.90:
            cost = ask + fee(ask)
            band.append({"p": p, "ask": ask, "mid": (bid + ask) / 2, "win": w, "ret": ((1 - cost) / cost) if w else -1.0})
print(f"\nsides priced 70-90c with a tight book: {len(band)}")
if band:
    print(f"  won {statistics.mean(x['win'] for x in band):.0%} at an average price of {statistics.mean(x['ask'] for x in band):.2f}; buying them all and holding returns {statistics.mean(x['ret'] for x in band):+.1%}")
    pick = [x for x in band if x["p"] - x["ask"] >= 0.05]
    skip = [x for x in band if x["p"] - x["ask"] < 0.05]
    for lab, g in (("the rankings model rates 5c+ above the ask", pick), ("the rest", skip)):
        if g:
            print(f"  {lab}: n={len(g)}, won {statistics.mean(x['win'] for x in g):.0%} at price {statistics.mean(x['ask'] for x in g):.2f}, model said {statistics.mean(x['p'] for x in g):.2f}, hold return {statistics.mean(x['ret'] for x in g):+.1%}")
