"""Is the live lane's ladder screen too pessimistic? Its dropped candidates against what the markets did (2026-11-15).

One question, same candidates (CLAUDE.md rule 4): every candidate the live crypto_multiscale lane's edge screen dropped (screen_log) since the ladder's width became the last hour's own movement (2026-11-14 17:02 ET), with the ask, the model's chance, and what the market then did (the side's last taped bid in the final minute: at or over 0.95 = the side paid, at or under 0.05 = it did not; no clean close = skipped). By the ask's band and by the model's chance: n, the model's mean chance, the mean ask, the realized rate, and what buying every one at the ask would have netted per $1 after the fee. If the realized rate sits near the ask, the screen is right to drop them; if it sits near or over the model's chance, the model is the pessimist; if over the ask, the screen costs bets. One row per ticker and side per five minutes, so a market that sat dropped counts a few times. Not split by curve shape. Not a rule: a check on whether the screen's chance can be trusted.

Run on the host: python scripts/screen_calibration_check.py (SINCE=epoch seconds to change the start).
"""
import datetime
import os
from collections import defaultdict

from kalshi_moonshot import config, db
from kalshi_moonshot.strategy import fee_per_contract

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = float(os.environ.get("SINCE", datetime.datetime(2026, 11, 14, 17, 2, tzinfo=ET).timestamp()))
LANE = os.environ.get("LANE", "crypto_multiscale")
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
rows = st.execute("SELECT ts, ticker, side, ask, model_p, edge FROM screen_log WHERE lane=? AND ts>? AND ask IS NOT NULL AND model_p IS NOT NULL", (LANE, SINCE)).fetchall()
res_cache: dict[str, str | None] = {}


def result(ticker):
    if ticker not in res_cache:
        got = tape.execute("SELECT bid, ask FROM quotes WHERE ticker=? ORDER BY ts DESC LIMIT 1", (ticker,)).fetchone()
        res_cache[ticker] = None if not got else ("yes" if float(got[0]) >= 0.95 else "no" if float(got[1]) <= 0.05 else None)
    return res_cache[ticker]


data = []
skipped = 0
for r in rows:
    res = result(r["ticker"])
    if res is None:
        skipped += 1
        continue
    ask = float(r["ask"]); won = res == r["side"]
    data.append((ask, float(r["model_p"]), won, (1.0 if won else 0.0) - ask - fee_per_contract(ask), r["ts"]))
print(f"{LANE}: screen drops since {datetime.datetime.fromtimestamp(SINCE, ET):%m-%d %H:%M} ET: {len(rows)}; with a clean close: {len(data)}; skipped {skipped}")


def table(title, key, edges):
    print(f"\n{title}: n | mean model chance | mean ask | realized rate | net per $1 buying all at the ask")
    for lo, hi in edges:
        g = [d for d in data if lo <= key(d) < hi]
        if len(g) < 5:
            continue
        n = len(g)
        print(f"  {lo:.2f}-{hi:.2f}: n {n:4d} | model {sum(d[1] for d in g) / n:.3f} | ask {sum(d[0] for d in g) / n:.3f} | realized {sum(1 for d in g if d[2]) / n:.3f} | net {sum(d[3] for d in g) / sum(d[0] for d in g):+.3f} (${sum(d[3] for d in g):+.2f} per $1 a contract)")


table("by the ask", lambda d: d[0], [(0.57, 0.65), (0.65, 0.72), (0.72, 0.77), (0.77, 0.82), (0.82, 0.90), (0.90, 1.01)])
table("by the model's chance", lambda d: d[1], [(0.0, 0.4), (0.4, 0.5), (0.5, 0.55), (0.55, 0.6), (0.6, 0.65), (0.65, 0.7), (0.7, 0.8), (0.8, 1.01)])
table("in the lane's price band only (ask 0.57 to 0.82), by the model's chance", lambda d: d[1] if 0.57 <= d[0] <= 0.82 else -1, [(0.0, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 1.01)])
by_day = defaultdict(list)
for d in data:
    by_day[datetime.datetime.fromtimestamp(d[4], ET).strftime("%m-%d %H")[:8] + "h" + ("<12" if datetime.datetime.fromtimestamp(d[4], ET).hour < 12 else ">=12")].append(d)
print("\nin the band, by half day: n | model | ask | realized")
for k in sorted(by_day):
    g = [d for d in by_day[k] if 0.57 <= d[0] <= 0.82]
    if len(g) >= 5:
        print(f"  {k}: n {len(g):4d} | model {sum(d[1] for d in g) / len(g):.3f} | ask {sum(d[0] for d in g) / len(g):.3f} | realized {sum(1 for d in g if d[2]) / len(g):.3f}")
print("DONE")
